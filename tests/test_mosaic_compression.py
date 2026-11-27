"""Lossless values/masks, codec errors and workflow compatibility regressions."""
from copy import deepcopy
from pathlib import Path
import json
import subprocess
import tempfile
import unittest
from types import SimpleNamespace

from swotflow.core.gdal import DEFAULT_GDAL_PYTHON, build_gdal_runtime_env, check_gdal_runtime
from swotflow.processing.encoding import normalize_compression, lossless_creation_options
from swotflow.processing.mosaic import MosaicConfig, parse_config
from swotflow.processing.automation import build_tile_config_dict, validate_resume_settings
from swotflow.processing.automation_years import yearly_config
from swotflow.verification.pipeline import settings_key
from tests.test_yearly_automation import config_at, preflight


class CompressionConfigTests(unittest.TestCase):
    def test_old_config_defaults_and_invalid_codecs(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data = {"mosaic": {"input_folder": str(root), "output_folder": str(root / 'out')}}
            self.assertEqual(parse_config(data, root).compression, 'none')
            for codec in ('ZSTD', 'deflate'):
                data['mosaic']['compression'] = codec
                self.assertEqual(parse_config(data, root).compression, codec.lower())
            for value in ('jpeg', 'sparse', 'invalid'):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    normalize_compression(value)

    def test_missing_codec_is_explicit_and_uncompressed_needs_no_driver(self):
        driver = SimpleNamespace(GetMetadataItem=lambda _key: '<CreationOptionList><Option name="COMPRESS"><Value>DEFLATE</Value></Option></CreationOptionList>')
        gdal = SimpleNamespace(GetDriverByName=lambda _name: driver)
        self.assertEqual(lossless_creation_options('none', None, None), [])
        with self.assertRaisesRegex(ValueError, 'does not support ZSTD'):
            lossless_creation_options('zstd', gdal, None)

    def test_scientific_identity_and_yearly_ordinary_resume_keep_completed_work(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            old = MosaicConfig(root, root / 'out')
            new = deepcopy(old); new.compression = 'zstd'
            self.assertEqual(settings_key('mosaic', old), settings_key('mosaic', new))
            for annual in (False, True):
                cfg = config_at(root, process_by_year=annual)
                saved = preflight(cfg)
                requested = deepcopy(cfg)
                requested.base_config['mosaic']['compression'] = 'zstd'
                validate_resume_settings(requested, saved)
                child = yearly_config(requested, '2024-01-01', '2024-12-31', 'UTM30R') if annual else requested
                tile = build_tile_config_dict(child.base_config, 'UTM30R', root / 'run',
                                              start_date=child.start_date, end_date=child.end_date,
                                              include_upload=child.include_upload)
                self.assertEqual(tile['mosaic']['compression'], 'zstd')
                changed = deepcopy(requested)
                changed.base_config['mosaic']['grouping_mode'] = 'pass_date_common_crs'
                with self.assertRaises(ValueError):
                    validate_resume_settings(changed, saved)


def exercise_pixels(root: Path) -> dict:
    """Run actual mosaic writers, checking reopened bytes and masks in bounded fixtures."""
    import numpy as np
    from osgeo import gdal, osr
    from swotflow.processing import mosaic
    gdal.UseExceptions()
    checks = []
    for hemisphere, epsg, bands, fill in [('north', 32635, ('N', 'P'), -9999.),
                                         ('south', 32735, ('L', 'M'), float('nan'))]:
        inputs = root / hemisphere / 'inputs'; inputs.mkdir(parents=True)
        for i, offset in enumerate((0, 16, 1024)):
            filename = f'SWOT_L2_HR_Raster_100m_UTM35{bands[0] if i == 0 else bands[1]}_N_x_x_x_011_555_{83+i:03d}F_20240305T010{i}00_20240305T010{i}30_PGD0_01.tif'
            ds = gdal.GetDriverByName('GTiff').Create(str(inputs / filename), 32, 32, 2, gdal.GDT_Float32)
            sr = osr.SpatialReference(); sr.ImportFromEPSG(epsg)
            ds.SetProjection(sr.ExportToWkt()); ds.SetGeoTransform((400000 + offset*100, 100, 0, 1200000 if hemisphere == 'north' else 9500000, 0, -100))
            w = np.full((32,32), 75+i*50, np.float32); q = np.ones_like(w)
            if i == 0:
                q[:16] = 2; w[1,1] = 0; q[1,1] = 0; w[2,1] = -15.25
            if i == 1:
                q[:16] = 0; w[3,3] = np.nan; q[4,3] = 255
            if i == 2:
                w[:] = fill; q[:] = fill; w[10,10] = 111.125; q[10,10] = 3
            for index, array in ((1,w),(2,q)):
                ds.GetRasterBand(index).SetNoDataValue(fill); ds.GetRasterBand(index).WriteArray(array)
            ds = None
        for singleton in (False, True):
            reference = None
            for codec in ('none', 'zstd', 'deflate'):
                cfg = MosaicConfig(inputs, root / hemisphere / f'{singleton}_{codec}',
                                   grouping_mode='utm_zone_hemisphere', compression=codec)
                group = mosaic.build_mosaic_plan(cfg).groups[0]
                if singleton:
                    group.sources = group.sources[:1]
                    mosaic.copy_singleton(group, cfg)
                else:
                    mosaic.merge_group(group, cfg)
                ds = gdal.Open(str(group.output_file))
                a = ds.ReadAsArray(); masks = np.stack([ds.GetRasterBand(i).GetMaskBand().ReadAsArray() for i in (1,2)])
                signature = (ds.GetProjection(), ds.GetGeoTransform(), ds.GetMetadata(), tuple(ds.GetRasterBand(i).GetDescription() for i in (1,2)))
                assert np.array_equal(masks[0], masks[1])
                assert a[0,1,1] == 0 and a[1,1,1] == 0 and a[0,2,1] == -15.25
                if not singleton:
                    assert a[0,10,20] == 125 and a[1,10,20] == 0  # Lower QA takes both values.
                    assert a[0,20,20] == 75 and a[1,20,20] == 1  # Equal QA keeps first input.
                    assert not masks[:,10,500].any()  # Interior gap stays NoData.
                if reference is None:
                    reference = (a.copy(), masks.copy(), signature)
                else:
                    assert a.tobytes() == reference[0].tobytes(), (hemisphere, codec, 'values')
                    assert masks.tobytes() == reference[1].tobytes(), (hemisphere, codec, 'masks')
                    assert signature == reference[2]
                if codec != 'none':
                    assert ds.GetMetadata('IMAGE_STRUCTURE')['COMPRESSION'] == codec.upper()
                    assert ds.GetRasterBand(1).GetBlockSize() == [256,256]
                assert group.output_file.with_suffix('.tfw').exists()
                checks.append((hemisphere, singleton, codec))
                ds = None
    # Integer singleton files must use a supported predictor too.
    for codec in ('zstd', 'deflate'):
        options = mosaic.geotiff_creation_options(MosaicConfig(root, root/'int', compression=codec), gdal.GDT_Byte)
        assert 'PREDICTOR=2' in options and 'PREDICTOR=3' not in options
        ds = gdal.GetDriverByName('GTiff').Create(str(root/f'byte_{codec}.tif'), 2, 2, 1, gdal.GDT_Byte, options=options)
        values = np.array([[0,1],[254,255]],np.uint8); ds.GetRasterBand(1).WriteArray(values); ds = None
        ds = gdal.Open(str(root/f'byte_{codec}.tif')); assert np.array_equal(ds.ReadAsArray(), values); ds = None
    return {'cases': len(checks), 'exact_values_and_masks': True, 'integer_codecs': 2}


class CompressionRasterTests(unittest.TestCase):
    def test_real_gdal_singletons_overlaps_gaps_and_both_hemispheres(self):
        runtime = check_gdal_runtime(DEFAULT_GDAL_PYTHON)
        if not runtime.ok:
            self.skipTest(runtime.stderr)
        with tempfile.TemporaryDirectory() as temp:
            code = 'from pathlib import Path; import sys,json; from tests.test_mosaic_compression import exercise_pixels; print(json.dumps(exercise_pixels(Path(sys.argv[1]))))'
            result = subprocess.run([str(DEFAULT_GDAL_PYTHON), '-c', code, temp],
                                    cwd=Path(__file__).resolve().parents[1], env=build_gdal_runtime_env(DEFAULT_GDAL_PYTHON),
                                    capture_output=True, text=True, encoding='utf-8')
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(json.loads(result.stdout)['cases'], 12)


if __name__ == '__main__':
    unittest.main()
