"""Real GDAL acceptance of extraction, paired overlap and independent source replay."""
from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
try:
    from osgeo import gdal, osr
    import numpy as np
except ImportError:
    gdal = None


@unittest.skipIf(gdal is None, "Run with the configured GDAL environment")
class SourceReplayTests(unittest.TestCase):
    def test_verified_pipeline_and_oracle_detects_injected_shift(self) -> None:
        import swotflow.verification.download as download_integrity
        from swotflow.verification.pipeline import VerificationStore
        from swotflow.verification.source import check_sources
        from swotflow.processing.download import normalize_granule
        from swotflow.processing.extract import ExtractConfig, run_extract
        from swotflow.processing.mosaic import MosaicConfig, run_mosaic
        from tests.test_download_tool import FakeGranule, granule_name
        gdal.UseExceptions()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            raw = root / "raw"
            raw.mkdir()
            logs = root / "00_logs"
            for index in range(2):
                name = granule_name(scene=f"{index+1:03}F")
                path = raw / name
                mem = gdal.GetDriverByName("MEM").Create("", 8, 6, 2, gdal.GDT_Float32)
                crs = osr.SpatialReference()
                crs.ImportFromEPSG(32630)
                mem.SetProjection(crs.ExportToWkt())
                mem.SetGeoTransform((500050+index*400,100,0,900050,0,-100))
                for b, variable, value in ((1,"wse",100+index*100),(2,"wse_qual",2-index)):
                    band = mem.GetRasterBand(b)
                    band.SetMetadataItem("NETCDF_VARNAME",variable)
                    band.SetNoDataValue(-9999 if b == 1 else 255)
                    band.Fill(value)
                    # Distinct real-product fills and cells invalid in either
                    # band must remain masked after the standalone TIFF reopens.
                    band.WriteArray(np.array([[-9999 if b == 1 else 255]],np.float32),0,0)
                nc = gdal.GetDriverByName("netCDF").CreateCopy(str(path),mem,options=["FORMAT=NC4"])
                nc = mem = band = None
                md = gdal.OpenEx(str(path),gdal.OF_MULTIDIM_RASTER|gdal.OF_UPDATE)
                group = md.GetRootGroup()
                for variable, number in (("wse",1),("wse_qual",2)):
                    array = group.OpenMDArray(variable+str(number))
                    array.Rename(variable)
                    array = None
                group = md = None
                fake = FakeGranule(name,concept_id=str(index))
                fake["umm"]["DataGranule"] = {"ArchiveAndDistributionInformation":[{"Name":name,
                    "SizeInBytes":path.stat().st_size,"Checksum":{"Algorithm":"MD5","Value":hashlib.md5(path.read_bytes()).hexdigest()}}]}
                self.assertTrue(download_integrity.verified_file(path,normalize_granule(fake),logs))
            ec = ExtractConfig(raw,root/"extracted",workers=2,verified_workflow=True,
                               manifest_csv=logs/"extract_manifest.csv",errors_csv=logs/"extract_errors.csv")
            code,_,rows,errors = run_extract(ec)
            self.assertEqual((code,errors),(0,[]))
            store = VerificationStore(logs)
            for row in rows:
                self.assertTrue(check_sources(store,Path(row["output_tif"]))["passed"])
            mc = MosaicConfig(root/"extracted",root/"mosaics",verified_workflow=True,require_all_inputs=True,
                              manifest_csv=logs/"mosaic_manifest.csv",report_csv=logs/"mosaic_report.csv")
            # Leftover older extraction has a better QA code but must not compete
            # with the preferred processing version of the same observation.
            import shutil
            current = Path(rows[0]["output_tif"])
            stale = current.with_name(current.name.replace("PID0", "PIC0"))
            self.assertNotEqual(stale, current)
            shutil.copy2(current, stale)
            ds = gdal.Open(str(stale), gdal.GA_Update)
            ds.GetRasterBand(1).Fill(900)
            ds.GetRasterBand(2).Fill(0)
            ds = None
            code, mosaics = run_mosaic(mc)
            self.assertEqual(code,0,mosaics)
            self.assertIn("FILTERED_PROCESSING_VERSION", {r["status"] for r in mosaics})
            output = Path(next(r["output_file"] for r in mosaics if r["output_file"]))
            check = check_sources(store,output)
            self.assertTrue(check["passed"])
            self.assertEqual(check["totals"]["overlap_cells"],23)
            self.assertEqual(check["totals"]["local_valid"],71)
            ds = gdal.Open(str(output))
            self.assertEqual(ds.GetRasterBand(1).GetNoDataValue(),ds.GetRasterBand(2).GetNoDataValue())
            np.testing.assert_array_equal(ds.GetRasterBand(1).GetMaskBand().ReadAsArray(),
                                          ds.GetRasterBand(2).GetMaskBand().ReadAsArray())
            self.assertEqual(ds.GetRasterBand(2).GetMaskBand().ReadAsArray()[0,0],0)
            ds = None
            # A repeated run must reuse exact current inputs/bytes, not re-extract.
            self.assertEqual(run_extract(ec)[0],0)
            self.assertIn("SKIPPED_EXISTS", {r["status"] for r in run_mosaic(mc)[1]})
            # Model a faulty processor producing a correctly checksummed but shifted
            # block: provenance/hashes alone must not pass the independent oracle.
            ds = gdal.Open(str(output),gdal.GA_Update)
            ds.GetRasterBand(1).WriteArray(np.array([[999]],np.float32),4,0)
            ds = None
            prior = store.artifact(output)
            store.record_artifact("mosaic",output,[Path(s) for s in prior["inputs"]],mc,mc.verification_run_id,prior["grid"])
            bad = check_sources(store,output)
            self.assertFalse(bad["passed"])
            self.assertEqual(bad["totals"]["wse_mismatch"],1)
            self.assertEqual(bad["totals"]["quality_mismatch"],0)


if __name__ == "__main__":
    unittest.main()
