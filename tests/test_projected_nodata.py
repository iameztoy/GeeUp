"""Reopened projected TIFFs must preserve WSE fill and categorical masks."""
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from swotflow.core.gdal import DEFAULT_GDAL_PYTHON, build_gdal_runtime_env, check_gdal_runtime
from swotflow.processing import extract
from swotflow.processing.extract import ExtractConfig
PROJECTED_NODATA_POLICY = "shared-wse-fill-v1"
from swotflow.verification.pipeline import ALGORITHM, VerificationStore, settings_key


class ProjectedNodataTests(unittest.TestCase):
    def test_only_projected_receipts_invalidate_old_mask_policy(self):
        native = ExtractConfig(Path('in'), Path('out'))
        projected = ExtractConfig(Path('in'), Path('out'), target_crs_mode='africa_laea')
        self.assertEqual(settings_key('extract', native), {
            'algorithm': ALGORITHM, 'target_crs_mode': 'original', 'resampling_alg': 'near'})
        self.assertEqual(extract.VALID_TARGET_CRS_MODES, {'original', 'africa_laea', 'wgs84'})
        self.assertEqual(extract.PROJECTED_NODATA_POLICY, PROJECTED_NODATA_POLICY)
        self.assertEqual(settings_key('extract', projected)['nodata_policy'], PROJECTED_NODATA_POLICY)

    def test_old_projected_byte_receipt_cannot_reuse_before_mask_check(self):
        for mode in ('africa_laea', 'wgs84'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                config = ExtractConfig(root, root, target_crs_mode=mode)
                old = {'stage': 'extract', 'settings': {
                    'algorithm': ALGORITHM, 'target_crs_mode': mode, 'resampling_alg': 'near'}}
                store = VerificationStore(root / 'logs')
                with mock.patch.object(store, 'artifact', return_value=old), \
                     mock.patch('swotflow.verification.pipeline.artifact_current',
                                side_effect=AssertionError('old policy must invalidate before byte reuse')):
                    self.assertFalse(store.reusable('extract', root / 'old.tif', [root / 'old.nc'], config))

    @unittest.skipUnless(check_gdal_runtime(DEFAULT_GDAL_PYTHON).ok, 'GDAL unavailable')
    def test_reopened_masks_match_independently_warped_bands(self):
        script = r'''
import sys
from pathlib import Path
from unittest import mock
import numpy as np
from osgeo import gdal, osr
from swotflow.processing.extract import (ExtractConfig, ExtractSource, export_geotiff_from_vrt,
    build_output_path, process_one_file)
PROJECTED_NODATA_POLICY = "shared-wse-fill-v1"
gdal.UseExceptions()
root = Path(sys.argv[1])
fill = float(np.float32(9.969209968386869e36))
source = gdal.GetDriverByName('MEM').Create('', 32, 32, 2, gdal.GDT_Float32)
srs = osr.SpatialReference(); srs.ImportFromEPSG(32630)
source.SetProjection(srs.ExportToWkt()); source.SetGeoTransform((500000,100,0,5000000,0,-100))
wse = np.tile(np.array([255, fill, 100, fill], dtype='float32'), (32,8))
qa = np.tile(np.array([0,3,255,255], dtype='float32'), (32,8))
for i, data, nodata in ((1,wse,fill),(2,qa,255)):
    source.GetRasterBand(i).WriteArray(data); source.GetRasterBand(i).SetNoDataValue(nodata)
for mode in ('africa_laea','wgs84'):
    config = ExtractConfig(root,root,target_crs_mode=mode)
    path = root/(mode+'.tif')
    export_geotiff_from_vrt(source,path,config,gdal)
    ds = gdal.Open(str(path)); t = ds.GetGeoTransform()
    bounds = [t[0], t[3]+ds.RasterYSize*t[5], t[0]+ds.RasterXSize*t[1], t[3]]
    for i in (1,2):
        single = gdal.Translate('',source,format='MEM',bandList=[i])
        ref = gdal.Warp('',single,format='MEM',dstSRS=ds.GetProjectionRef(),outputBounds=bounds,
            width=ds.RasterXSize,height=ds.RasterYSize,errorThreshold=0,resampleAlg='near',dstNodata=fill)
        band = ds.GetRasterBand(i); a = band.ReadAsArray(); mask = band.GetMaskBand().ReadAsArray()!=0
        assert band.GetNoDataValue()==fill, (mode,i,band.GetNoDataValue())
        np.testing.assert_array_equal(a,ref.ReadAsArray(),err_msg=mode+str(i))
        np.testing.assert_array_equal(mask,ref.GetRasterBand(1).GetMaskBand().ReadAsArray()!=0)
        assert not np.any(mask & (abs(a)>1e30))
    assert np.any((ds.GetRasterBand(1).ReadAsArray()==255)&(ds.GetRasterBand(1).GetMaskBand().ReadAsArray()!=0))
    assert ds.GetMetadataItem('SWOTFLOW_NODATA_POLICY') == PROJECTED_NODATA_POLICY
    ds=None
# A pre-fix projected manifest must not skip regeneration, even with an existing TIFF.
nc=root/'old.nc'; nc.touch()
config=ExtractConfig(root,root,target_crs_mode='africa_laea')
output=build_output_path(nc,root,config.target_crs_mode)
gdal.Translate(str(output),source,format='GTiff')
old={'old.nc|africa_laea':{'status':'written'}}
gdal.FileFromMemBuffer('/vsimem/test.vrt', b'test fixture')
with mock.patch('swotflow.processing.extract.build_two_band_vrt',return_value=(source,'/vsimem/test.vrt')):
    result=process_one_file(ExtractSource(nc,{}),config,gdal,old)
assert result['status']=='rewritten_invalid_existing', result
assert result['nodata_policy']==PROJECTED_NODATA_POLICY
with mock.patch('swotflow.processing.extract.build_two_band_vrt',side_effect=AssertionError('should reuse')):
    reused=process_one_file(ExtractSource(nc,{}),config,gdal,{result['record_id']:result})
assert reused['status']=='skipped_existing', reused
# A current verified receipt must recover a lost/stale projected manifest without deleting its TIFF.
from dataclasses import replace
verified = replace(config, verified_workflow=True, manifest_csv=root/'logs'/'extract_manifest.csv')
for manifest in ({}, old):
    before = output.read_bytes()
    with mock.patch('swotflow.verification.pipeline.VerificationStore.reusable', return_value=True), \
         mock.patch('swotflow.processing.extract.build_two_band_vrt', side_effect=AssertionError('valid receipt must reuse')):
        recovered = process_one_file(ExtractSource(nc,{}), verified, gdal, manifest)
    assert recovered['status'] == 'skipped_existing', recovered
    assert recovered['nodata_policy'] == PROJECTED_NODATA_POLICY
    assert output.read_bytes() == before
print('Both supported projected modes: independent band masks, valid WSE=255, old manifest invalidation and current receipt recovery passed')
'''
        with tempfile.TemporaryDirectory() as temp:
            result = subprocess.run([str(DEFAULT_GDAL_PYTHON), '-c', script, temp],
                cwd=Path(__file__).resolve().parents[1], env=build_gdal_runtime_env(DEFAULT_GDAL_PYTHON),
                text=True, capture_output=True, timeout=90)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
