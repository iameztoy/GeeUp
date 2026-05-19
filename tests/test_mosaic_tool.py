import csv
import json
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

import ee_mosaic_tool
from ee_mosaic_tool import (
    ERROR_DISK_SPACE_STATUS,
    GROUPING_MODE_PASS_DATE_COMMON_CRS,
    GROUPING_MODE_UTM_ZONE_HEMISPHERE,
    MosaicConfig,
    build_mosaic_plan,
    cleanup_temporary_output,
    group_source_signature,
    is_disk_space_error,
    promote_temporary_output,
    run_mosaic,
    temporary_mosaic_path,
    world_file_path,
    write_mosaic_manifest,
)
from gdal_runtime import DEFAULT_GDAL_PYTHON, build_gdal_runtime_env, check_gdal_runtime
from swot_metadata import parse_swot_l2_hr_raster_metadata


GDAL_CHECK = check_gdal_runtime(DEFAULT_GDAL_PYTHON)
GDAL_AVAILABLE = GDAL_CHECK.ok


def swot_name(
    *,
    utm: str = "UTM36M",
    scene: str = "000A",
    start: str = "20250707T010000",
    end: str = "20250707T010100",
    crid: str = "PID0",
    counter: str = "01",
) -> str:
    return (
        "SWOT_L2_HR_Raster_"
        f"100m_{utm}_N_x_x_x_"
        f"035_225_{scene}_{start}_{end}_{crid}_{counter}_wse.tif"
    )


class MosaicPlanningTests(unittest.TestCase):
    def test_groups_by_cycle_pass_start_date_and_exact_utm_token(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / swot_name(scene="000A")).touch()
            (root / swot_name(scene="001A", start="20250707T011000", end="20250707T011100")).touch()
            (root / swot_name(utm="UTM36N", scene="002A")).touch()

            config = MosaicConfig(
                input_folder=root,
                output_folder=root / "out",
                report_csv=root / "report.csv",
            )
            plan = build_mosaic_plan(config)

            group_sizes = sorted(len(group.sources) for group in plan.groups)
            utm_tokens = sorted(group.key.coordinate_system for group in plan.groups)
            self.assertEqual(group_sizes, [1, 2])
            self.assertEqual(utm_tokens, ["UTM36M", "UTM36N"])

    def test_common_crs_grouping_ignores_original_utm_token(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / swot_name(utm="UTM36M", scene="000A")).touch()
            (root / swot_name(utm="UTM36N", scene="001A", start="20250707T011000", end="20250707T011100")).touch()

            config = MosaicConfig(
                input_folder=root,
                output_folder=root / "out",
                report_csv=root / "report.csv",
                grouping_mode=GROUPING_MODE_PASS_DATE_COMMON_CRS,
                target_crs_label="LAEA",
            )
            plan = build_mosaic_plan(config)

            self.assertEqual(len(plan.groups), 1)
            self.assertEqual(len(plan.groups[0].sources), 2)
            self.assertEqual(plan.groups[0].key.coordinate_system, "LAEA")
            self.assertEqual(plan.groups[0].key.descriptor, "100m_LAEA_N_x_x_x")

    def test_utm_zone_hemisphere_grouping_combines_northern_latitude_bands(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / swot_name(utm="UTM30P", scene="000A")).touch()
            (root / swot_name(utm="UTM30Q", scene="001A", start="20250707T011000", end="20250707T011100")).touch()
            (root / swot_name(utm="UTM30M", scene="002A")).touch()

            config = MosaicConfig(
                input_folder=root,
                output_folder=root / "out",
                report_csv=root / "report.csv",
                grouping_mode=GROUPING_MODE_UTM_ZONE_HEMISPHERE,
            )
            plan = build_mosaic_plan(config)

            group_sizes = sorted(len(group.sources) for group in plan.groups)
            coordinate_tokens = sorted(group.key.coordinate_system for group in plan.groups)
            descriptors = sorted(group.key.descriptor for group in plan.groups)
            self.assertEqual(group_sizes, [1, 2])
            self.assertEqual(coordinate_tokens, ["UTM30N", "UTM30S"])
            self.assertEqual(descriptors, ["100m_UTM30N_N_x_x_x", "100m_UTM30S_N_x_x_x"])

    def test_utm_zone_hemisphere_output_name_remains_swot_parseable(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / swot_name(utm="UTM30P", scene="000A")).touch()
            (root / swot_name(utm="UTM30Q", scene="001A", start="20250707T011000", end="20250707T011100")).touch()

            config = MosaicConfig(
                input_folder=root,
                output_folder=root / "out",
                report_csv=root / "report.csv",
                grouping_mode=GROUPING_MODE_UTM_ZONE_HEMISPHERE,
            )
            plan = build_mosaic_plan(config)
            parsed = parse_swot_l2_hr_raster_metadata(plan.groups[0].output_file.name)

            self.assertIsNotNone(parsed)
            assert parsed is not None
            self.assertEqual(parsed.properties["swot_coordinate_system"], "UTM30N")
            self.assertEqual(parsed.properties["swot_scene_id"], "MOSA")

    def test_output_name_remains_swot_parseable(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / swot_name(scene="000A")).touch()
            (root / swot_name(scene="001A", start="20250707T011000", end="20250707T011100")).touch()

            config = MosaicConfig(
                input_folder=root,
                output_folder=root / "out",
                report_csv=root / "report.csv",
            )
            plan = build_mosaic_plan(config)

            parsed = parse_swot_l2_hr_raster_metadata(plan.groups[0].output_file.name)

            self.assertIsNotNone(parsed)
            assert parsed is not None
            self.assertEqual(parsed.properties["swot_scene_id"], "MOSA")
            self.assertEqual(parsed.properties["swot_cycle_id"], "035")
            self.assertEqual(parsed.properties["swot_pass_id"], "225")
            self.assertEqual(parsed.properties["swot_coordinate_system"], "UTM36M")
            self.assertFalse(plan.groups[0].output_file.stem.endswith("_mosaic"))

    def test_common_crs_output_name_remains_swot_parseable(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / swot_name(utm="UTM36M", scene="000A")).touch()
            (root / swot_name(utm="UTM36N", scene="001A", start="20250707T011000", end="20250707T011100")).touch()

            config = MosaicConfig(
                input_folder=root,
                output_folder=root / "out",
                report_csv=root / "report.csv",
                grouping_mode=GROUPING_MODE_PASS_DATE_COMMON_CRS,
                target_crs_label="WGS84",
            )
            plan = build_mosaic_plan(config)

            parsed = parse_swot_l2_hr_raster_metadata(plan.groups[0].output_file.name)

            self.assertIsNotNone(parsed)
            assert parsed is not None
            self.assertEqual(parsed.properties["swot_scene_id"], "MOSA")
            self.assertEqual(parsed.properties["swot_coordinate_system"], "WGS84")
            self.assertEqual(parsed.properties["swot_cycle_id"], "035")
            self.assertEqual(parsed.properties["swot_pass_id"], "225")

    def test_mixed_crid_output_name_uses_mixd_and_remains_parseable(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / swot_name(scene="000A", crid="PID0")).touch()
            (root / swot_name(scene="001A", crid="PIC2", start="20250707T011000", end="20250707T011100")).touch()

            config = MosaicConfig(
                input_folder=root,
                output_folder=root / "out",
                report_csv=root / "report.csv",
            )
            plan = build_mosaic_plan(config)

            self.assertIn("_MIXD_01.tif", plan.groups[0].output_file.name)
            parsed = parse_swot_l2_hr_raster_metadata(plan.groups[0].output_file.name)

            self.assertIsNotNone(parsed)
            assert parsed is not None
            self.assertEqual(parsed.properties["swot_crid"], "MIXD")
            self.assertEqual(parsed.properties["swot_scene_id"], "MOSA")

    def test_mixed_crid_diagnostics_are_reported_in_dry_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            mixed_report = root / "mixed_crid_mosaics.csv"
            (root / swot_name(scene="000A", crid="PID0", counter="02")).touch()
            (root / swot_name(scene="001A", crid="PIC2", counter="01", start="20250707T011000", end="20250707T011100")).touch()

            config = MosaicConfig(
                input_folder=root,
                output_folder=root / "out",
                report_csv=root / "report.csv",
                mixed_crid_report_csv=mixed_report,
            )

            exit_code, rows = run_mosaic(config, dry_run=True)

            self.assertEqual(exit_code, 0)
            self.assertEqual(rows[0]["status"], "PLANNED_MOSAIC")
            self.assertEqual(rows[0]["crid"], "MIXD")
            self.assertEqual(rows[0]["mixed_crid"], "true")
            self.assertEqual(json.loads(rows[0]["source_crids"]), ["PID0", "PIC2"])
            self.assertEqual(json.loads(rows[0]["source_product_counters"]), ["02", "01"])
            self.assertEqual(rows[0]["dominant_crid"], "PID0")
            self.assertEqual(rows[0]["preferred_crid"], "PID0")
            with mixed_report.open("r", encoding="utf-8", newline="") as handle:
                mixed_rows = list(csv.DictReader(handle))
            self.assertEqual(len(mixed_rows), 1)
            self.assertEqual(mixed_rows[0]["mixed_crid"], "true")

    def test_invalid_filename_is_reported_without_gdal_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "not_a_swot_file.tif").touch()
            config = MosaicConfig(
                input_folder=root,
                output_folder=root / "out",
                report_csv=root / "report.csv",
            )

            exit_code, rows = run_mosaic(config, dry_run=True)

            self.assertEqual(exit_code, 2)
            self.assertEqual(rows[0]["status"], "INVALID_FILENAME")

    def test_manifest_skip_avoids_rebuilding_deleted_mosaic(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / swot_name(scene="000A")).touch()
            config = MosaicConfig(
                input_folder=root,
                output_folder=root / "out",
                report_csv=root / "report.csv",
                manifest_csv=root / "mosaic_manifest.csv",
            )
            plan = build_mosaic_plan(config)
            group = plan.groups[0]
            signature = group_source_signature(group)
            write_mosaic_manifest(
                config,
                [
                    {
                        "status": "COPIED_SINGLETON",
                        "output_file": str(group.output_file),
                        "input_count": "1",
                        "source_signature": signature,
                    }
                ],
            )

            exit_code, rows = run_mosaic(config, dry_run=False)

            self.assertEqual(exit_code, 0)
            self.assertEqual(rows[0]["status"], "SKIPPED_MANIFEST")
            self.assertEqual(rows[0]["known_from_manifest"], "yes")
            self.assertFalse(group.output_file.exists())
            self.assertTrue((root / "workflow_manifest.csv").exists())

    def test_disk_write_error_stops_mosaic_run_early(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / swot_name(scene="000A")).touch()
            (root / swot_name(scene="001A")).touch()
            (root / swot_name(scene="002A", start="20250708T010000", end="20250708T010100")).touch()
            (root / swot_name(scene="003A", start="20250708T010000", end="20250708T010100")).touch()
            config = MosaicConfig(
                input_folder=root,
                output_folder=root / "out",
                report_csv=root / "report.csv",
                manifest_csv=root / "mosaic_manifest.csv",
            )

            with mock.patch.object(ee_mosaic_tool, "validate_raster_group"):
                with mock.patch.object(
                    ee_mosaic_tool,
                    "merge_group",
                    side_effect=RuntimeError("TIFFAppendToStrip:Write error at scanline 42"),
                ):
                    exit_code, rows = run_mosaic(config, dry_run=False)

            self.assertEqual(exit_code, 2)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["status"], ERROR_DISK_SPACE_STATUS)
            self.assertIn("stopped early", rows[0]["message"])
            with (root / "report.csv").open("r", encoding="utf-8", newline="") as handle:
                report_rows = list(csv.DictReader(handle))
            self.assertEqual(len(report_rows), 1)
            self.assertEqual(report_rows[0]["status"], ERROR_DISK_SPACE_STATUS)

    def test_temporary_output_is_promoted_or_cleaned(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            final = root / "mosaic.tif"
            config = MosaicConfig(input_folder=root, output_folder=root, report_csv=root / "report.csv")
            temp_output = temporary_mosaic_path(final)
            temp_output.write_bytes(b"complete")
            world_file_path(temp_output).write_text("world", encoding="utf-8")

            promote_temporary_output(temp_output, final, config)

            self.assertEqual(final.read_bytes(), b"complete")
            self.assertFalse(temp_output.exists())
            self.assertTrue(world_file_path(final).exists())

            temp_output.write_bytes(b"partial")
            world_file_path(temp_output).write_text("partial world", encoding="utf-8")
            cleanup_temporary_output(final)

            self.assertFalse(temp_output.exists())
            self.assertFalse(world_file_path(temp_output).exists())

    def test_disk_space_error_detection_matches_gdal_write_errors(self) -> None:
        self.assertTrue(is_disk_space_error(RuntimeError("No space left on device")))
        self.assertTrue(is_disk_space_error(RuntimeError("TIFFAppendToStrip:Write error at scanline 42")))
        self.assertFalse(is_disk_space_error(RuntimeError("Invalid projection")))

    def test_existing_mosaic_with_changed_source_signature_is_stale(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / swot_name(scene="000A")).touch()
            config = MosaicConfig(
                input_folder=root,
                output_folder=root / "out",
                report_csv=root / "report.csv",
                manifest_csv=root / "mosaic_manifest.csv",
            )
            plan = build_mosaic_plan(config)
            group = plan.groups[0]
            group.output_file.parent.mkdir(parents=True, exist_ok=True)
            group.output_file.touch()
            write_mosaic_manifest(
                config,
                [
                    {
                        "status": "MOSAIC_CREATED",
                        "output_file": str(group.output_file),
                        "input_count": "1",
                        "source_signature": "old-signature",
                    }
                ],
            )

            exit_code, rows = run_mosaic(config, dry_run=False)

            self.assertEqual(exit_code, 2)
            self.assertEqual(rows[0]["status"], "STALE_EXISTS")
            self.assertEqual(rows[0]["stale"], "true")


@unittest.skipUnless(GDAL_AVAILABLE, f"GDAL runtime unavailable: {GDAL_CHECK.stderr}")
class MosaicGdalRuntimeTests(unittest.TestCase):
    def run_gdal_python(self, code: str, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(DEFAULT_GDAL_PYTHON), "-c", code, *args],
            cwd=str(Path(__file__).resolve().parents[1]),
            env=build_gdal_runtime_env(DEFAULT_GDAL_PYTHON),
            text=True,
            capture_output=True,
            check=True,
        )

    def write_geotiff(
        self,
        path: Path,
        value: int,
        *,
        x_origin: float = 0,
        y_origin: float = 2,
        epsg: int = 32636,
    ) -> None:
        code = r"""
from pathlib import Path
import sys
from osgeo import gdal, osr

path = Path(sys.argv[1])
value = int(sys.argv[2])
x_origin = float(sys.argv[3])
y_origin = float(sys.argv[4])
epsg = int(sys.argv[5])
driver = gdal.GetDriverByName("GTiff")
dataset = driver.Create(str(path), 2, 2, 1, gdal.GDT_Byte, options=["COMPRESS=DEFLATE"])
dataset.SetGeoTransform((x_origin, 1, 0, y_origin, 0, -1))
srs = osr.SpatialReference()
srs.ImportFromEPSG(epsg)
dataset.SetProjection(srs.ExportToWkt())
band = dataset.GetRasterBand(1)
band.SetNoDataValue(0)
band.Fill(value)
dataset.FlushCache()
dataset = None
"""
        self.run_gdal_python(
            code,
            str(path),
            str(value),
            str(x_origin),
            str(y_origin),
            str(epsg),
        )

    def write_two_band_geotiff(
        self,
        path: Path,
        band1_value: int,
        band2_value: int,
        *,
        x_origin: float = 0,
        y_origin: float = 2,
        epsg: int = 32636,
    ) -> None:
        code = r"""
from pathlib import Path
import sys
from osgeo import gdal, osr

path = Path(sys.argv[1])
band1_value = int(sys.argv[2])
band2_value = int(sys.argv[3])
x_origin = float(sys.argv[4])
y_origin = float(sys.argv[5])
epsg = int(sys.argv[6])
driver = gdal.GetDriverByName("GTiff")
dataset = driver.Create(str(path), 2, 2, 2, gdal.GDT_Byte, options=["COMPRESS=DEFLATE"])
dataset.SetGeoTransform((x_origin, 1, 0, y_origin, 0, -1))
srs = osr.SpatialReference()
srs.ImportFromEPSG(epsg)
dataset.SetProjection(srs.ExportToWkt())
band1 = dataset.GetRasterBand(1)
band1.SetNoDataValue(255)
band1.SetDescription("wse")
band1.Fill(band1_value)
band2 = dataset.GetRasterBand(2)
band2.SetNoDataValue(255)
band2.SetDescription("wse_qual")
band2.Fill(band2_value)
dataset.FlushCache()
dataset = None
"""
        self.run_gdal_python(
            code,
            str(path),
            str(band1_value),
            str(band2_value),
            str(x_origin),
            str(y_origin),
            str(epsg),
        )

    def write_custom_two_band_geotiff(
        self,
        path: Path,
        band1_rows: list[list[int]],
        band2_rows: list[list[int]],
        *,
        x_origin: float = 0,
        y_origin: float = 2,
        epsg: int = 32636,
    ) -> None:
        code = r"""
from pathlib import Path
import json
import sys
import numpy as np
from osgeo import gdal, osr

path = Path(sys.argv[1])
band1_rows = json.loads(sys.argv[2])
band2_rows = json.loads(sys.argv[3])
x_origin = float(sys.argv[4])
y_origin = float(sys.argv[5])
epsg = int(sys.argv[6])
height = len(band1_rows)
width = len(band1_rows[0])
driver = gdal.GetDriverByName("GTiff")
dataset = driver.Create(str(path), width, height, 2, gdal.GDT_Byte, options=["COMPRESS=DEFLATE"])
dataset.SetGeoTransform((x_origin, 1, 0, y_origin, 0, -1))
srs = osr.SpatialReference()
srs.ImportFromEPSG(epsg)
dataset.SetProjection(srs.ExportToWkt())
band1 = dataset.GetRasterBand(1)
band1.SetNoDataValue(255)
band1.SetDescription("wse")
band1.WriteArray(np.array(band1_rows, dtype="uint8"))
band2 = dataset.GetRasterBand(2)
band2.SetNoDataValue(255)
band2.SetDescription("wse_qual")
band2.WriteArray(np.array(band2_rows, dtype="uint8"))
dataset.FlushCache()
dataset = None
"""
        self.run_gdal_python(
            code,
            str(path),
            json.dumps(band1_rows),
            json.dumps(band2_rows),
            str(x_origin),
            str(y_origin),
            str(epsg),
        )

    def read_first_band(self, path: Path) -> list[list[int]]:
        code = r"""
import json
import sys
from osgeo import gdal

dataset = gdal.Open(sys.argv[1])
print(json.dumps(dataset.ReadAsArray().tolist()))
"""
        result = self.run_gdal_python(code, str(path))
        return json.loads(result.stdout)

    def read_all_bands(self, path: Path) -> dict[str, object]:
        code = r"""
import json
import sys
from osgeo import gdal

dataset = gdal.Open(sys.argv[1])
payload = {
    "band_count": dataset.RasterCount,
    "array": dataset.ReadAsArray().tolist(),
}
print(json.dumps(payload))
"""
        result = self.run_gdal_python(code, str(path))
        return json.loads(result.stdout)

    def read_image_structure(self, path: Path) -> dict[str, str]:
        code = r"""
import json
import sys
from osgeo import gdal

dataset = gdal.Open(sys.argv[1])
print(json.dumps(dataset.GetMetadata("IMAGE_STRUCTURE")))
"""
        result = self.run_gdal_python(code, str(path))
        return json.loads(result.stdout)

    def run_mosaic_cli(
        self,
        root: Path,
        *,
        grouping_mode: str = "utm_zone",
        target_crs_label: str = "",
    ) -> tuple[int, list[dict[str, str]], str]:
        config = root / "config.yaml"
        report = root / "report.csv"
        config.write_text(
            "\n".join(
                [
                    "mosaic:",
                    f"  input_folder: \"{root.as_posix()}\"",
                    f"  output_folder: \"{(root / 'out').as_posix()}\"",
                    f"  grouping_mode: \"{grouping_mode}\"",
                    f"  target_crs_label: \"{target_crs_label}\"",
                    "  overwrite: true",
                    f"  report_csv: \"{report.as_posix()}\"",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        result = subprocess.run(
            [
                str(DEFAULT_GDAL_PYTHON),
                str(Path(__file__).resolve().parents[1] / "ee_mosaic_tool.py"),
                "--config",
                str(config),
            ],
            cwd=str(Path(__file__).resolve().parents[1]),
            env=build_gdal_runtime_env(DEFAULT_GDAL_PYTHON),
            text=True,
            capture_output=True,
            check=False,
        )
        with report.open("r", encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
        return result.returncode, rows, result.stdout + result.stderr

    def test_check_gdal_command_confirms_required_drivers(self) -> None:
        result = subprocess.run(
            [
                str(DEFAULT_GDAL_PYTHON),
                str(Path(__file__).resolve().parents[1] / "ee_mosaic_tool.py"),
                "--check-gdal",
            ],
            cwd=str(Path(__file__).resolve().parents[1]),
            env=build_gdal_runtime_env(DEFAULT_GDAL_PYTHON),
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("GTiff,VRT,netCDF", result.stdout)

    def test_adjacent_tiles_are_merged(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.write_geotiff(root / swot_name(scene="000A"), 1, x_origin=0)
            self.write_geotiff(
                root / swot_name(scene="001A", start="20250707T011000", end="20250707T011100"),
                2,
                x_origin=2,
            )

            exit_code, rows, output = self.run_mosaic_cli(root)

            self.assertEqual(exit_code, 0, output)
            output_path = Path(rows[0]["output_file"])
            merged = self.read_first_band(output_path)
            self.assertEqual(merged, [[1, 1, 2, 2], [1, 1, 2, 2]])
            self.assertTrue(output_path.with_suffix(".tfw").exists())
            self.assertNotIn("COMPRESSION", self.read_image_structure(output_path))

    def test_two_band_extraction_outputs_are_mosaicked_with_both_bands(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.write_two_band_geotiff(
                root / swot_name(scene="000A"),
                10,
                1,
                x_origin=0,
            )
            self.write_two_band_geotiff(
                root / swot_name(scene="001A", start="20250707T011000", end="20250707T011100"),
                20,
                2,
                x_origin=2,
            )

            exit_code, rows, output = self.run_mosaic_cli(root)

            self.assertEqual(exit_code, 0, output)
            payload = self.read_all_bands(Path(rows[0]["output_file"]))
            self.assertEqual(payload["band_count"], 2)
            self.assertEqual(
                payload["array"],
                [
                    [[10, 10, 20, 20], [10, 10, 20, 20]],
                    [[1, 1, 2, 2], [1, 1, 2, 2]],
                ],
            )

    def test_quality_uses_lower_priority_valid_value_when_higher_priority_has_class_3_with_wse_nodata(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.write_custom_two_band_geotiff(
                root / swot_name(scene="000A"),
                [[10, 255], [10, 255]],
                [[0, 3], [0, 3]],
                x_origin=0,
            )
            self.write_custom_two_band_geotiff(
                root / swot_name(scene="001A", start="20250707T011000", end="20250707T011100"),
                [[20, 20], [20, 20]],
                [[1, 1], [1, 1]],
                x_origin=1,
            )

            exit_code, rows, output = self.run_mosaic_cli(root)

            self.assertEqual(exit_code, 0, output)
            payload = self.read_all_bands(Path(rows[0]["output_file"]))
            self.assertEqual(
                payload["array"],
                [
                    [[10, 20, 20], [10, 20, 20]],
                    [[0, 1, 1], [0, 1, 1]],
                ],
            )

    def test_quality_keeps_first_sorted_value_when_lower_priority_tile_has_class_3(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.write_custom_two_band_geotiff(
                root / swot_name(scene="000A"),
                [[10, 10], [10, 10]],
                [[1, 1], [1, 1]],
                x_origin=0,
            )
            self.write_custom_two_band_geotiff(
                root / swot_name(scene="001A", start="20250707T011000", end="20250707T011100"),
                [[20, 20], [20, 20]],
                [[3, 3], [3, 3]],
                x_origin=1,
            )

            exit_code, rows, output = self.run_mosaic_cli(root)

            self.assertEqual(exit_code, 0, output)
            payload = self.read_all_bands(Path(rows[0]["output_file"]))
            self.assertEqual(
                payload["array"],
                [
                    [[10, 10, 20], [10, 10, 20]],
                    [[1, 1, 3], [1, 1, 3]],
                ],
            )

    def test_quality_keeps_first_sorted_value_when_both_tiles_have_valid_quality_values(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.write_custom_two_band_geotiff(
                root / swot_name(scene="000A"),
                [[10, 10], [10, 10]],
                [[1, 1], [1, 1]],
                x_origin=0,
            )
            self.write_custom_two_band_geotiff(
                root / swot_name(scene="001A", start="20250707T011000", end="20250707T011100"),
                [[20, 20], [20, 20]],
                [[2, 2], [2, 2]],
                x_origin=1,
            )

            exit_code, rows, output = self.run_mosaic_cli(root)

            self.assertEqual(exit_code, 0, output)
            payload = self.read_all_bands(Path(rows[0]["output_file"]))
            self.assertEqual(
                payload["array"],
                [
                    [[10, 10, 20], [10, 10, 20]],
                    [[1, 1, 2], [1, 1, 2]],
                ],
            )

    def test_quality_preserves_class_3_when_it_is_the_only_valid_quality_with_valid_wse(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.write_custom_two_band_geotiff(
                root / swot_name(scene="000A"),
                [[10, 10], [10, 10]],
                [[3, 3], [3, 3]],
                x_origin=0,
            )
            self.write_custom_two_band_geotiff(
                root / swot_name(scene="001A", start="20250707T011000", end="20250707T011100"),
                [[255, 20], [255, 20]],
                [[1, 1], [1, 1]],
                x_origin=1,
            )

            exit_code, rows, output = self.run_mosaic_cli(root)

            self.assertEqual(exit_code, 0, output)
            payload = self.read_all_bands(Path(rows[0]["output_file"]))
            self.assertEqual(
                payload["array"],
                [
                    [[10, 10, 20], [10, 10, 20]],
                    [[3, 3, 1], [3, 3, 1]],
                ],
            )

    def test_quality_writes_nodata_when_no_source_has_valid_wse(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.write_custom_two_band_geotiff(
                root / swot_name(scene="000A"),
                [[10, 255], [10, 255]],
                [[0, 3], [0, 3]],
                x_origin=0,
            )
            self.write_custom_two_band_geotiff(
                root / swot_name(scene="001A", start="20250707T011000", end="20250707T011100"),
                [[255, 20], [255, 20]],
                [[1, 1], [1, 1]],
                x_origin=1,
            )

            exit_code, rows, output = self.run_mosaic_cli(root)

            self.assertEqual(exit_code, 0, output)
            payload = self.read_all_bands(Path(rows[0]["output_file"]))
            self.assertEqual(
                payload["array"],
                [
                    [[10, 255, 20], [10, 255, 20]],
                    [[0, 255, 1], [0, 255, 1]],
                ],
            )

    def test_common_crs_mode_merges_across_original_utm_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.write_geotiff(root / swot_name(utm="UTM36M", scene="000A"), 1, x_origin=0, epsg=4326)
            self.write_geotiff(
                root / swot_name(utm="UTM36N", scene="001A", start="20250707T011000", end="20250707T011100"),
                2,
                x_origin=2,
                epsg=4326,
            )

            exit_code, rows, output = self.run_mosaic_cli(
                root,
                grouping_mode=GROUPING_MODE_PASS_DATE_COMMON_CRS,
                target_crs_label="WGS84",
            )

            self.assertEqual(exit_code, 0, output)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["status"], "MOSAIC_CREATED")
            self.assertEqual(rows[0]["coordinate_system"], "WGS84")
            merged = self.read_first_band(Path(rows[0]["output_file"]))
            self.assertEqual(merged, [[1, 1, 2, 2], [1, 1, 2, 2]])

    def test_utm_zone_hemisphere_mode_merges_same_actual_utm_crs(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.write_geotiff(root / swot_name(utm="UTM30P", scene="000A"), 1, x_origin=0, epsg=32630)
            self.write_geotiff(
                root / swot_name(utm="UTM30Q", scene="001A", start="20250707T011000", end="20250707T011100"),
                2,
                x_origin=2,
                epsg=32630,
            )

            exit_code, rows, output = self.run_mosaic_cli(
                root,
                grouping_mode=GROUPING_MODE_UTM_ZONE_HEMISPHERE,
            )

            self.assertEqual(exit_code, 0, output)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["coordinate_system"], "UTM30N")
            merged = self.read_first_band(Path(rows[0]["output_file"]))
            self.assertEqual(merged, [[1, 1, 2, 2], [1, 1, 2, 2]])

    def test_overlap_keeps_first_sorted_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.write_geotiff(root / swot_name(scene="000A"), 7)
            self.write_geotiff(
                root / swot_name(scene="001A", start="20250707T011000", end="20250707T011100"),
                9,
            )

            exit_code, rows, output = self.run_mosaic_cli(root)

            self.assertEqual(exit_code, 0, output)
            merged = self.read_first_band(Path(rows[0]["output_file"]))
            self.assertEqual(merged, [[7, 7], [7, 7]])

    def test_singleton_group_is_copied(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.write_geotiff(root / swot_name(scene="000A"), 5)

            exit_code, rows, output = self.run_mosaic_cli(root)

            self.assertEqual(exit_code, 0, output)
            self.assertEqual(rows[0]["status"], "COPIED_SINGLETON")
            output_path = Path(rows[0]["output_file"])
            copied = self.read_first_band(output_path)
            self.assertEqual(copied, [[5, 5], [5, 5]])
            self.assertTrue(output_path.with_suffix(".tfw").exists())
            self.assertNotIn("COMPRESSION", self.read_image_structure(output_path))

    def test_incompatible_crs_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.write_geotiff(root / swot_name(scene="000A"), 1, epsg=32636)
            self.write_geotiff(
                root / swot_name(scene="001A", start="20250707T011000", end="20250707T011100"),
                2,
                epsg=32637,
            )

            exit_code, rows, _output = self.run_mosaic_cli(root)

            self.assertEqual(exit_code, 2)
            self.assertEqual(rows[0]["status"], "SKIPPED_INCOMPATIBLE")

    def test_common_crs_mode_reports_incompatible_crs(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.write_geotiff(root / swot_name(utm="UTM36M", scene="000A"), 1, epsg=32636)
            self.write_geotiff(
                root / swot_name(utm="UTM36N", scene="001A", start="20250707T011000", end="20250707T011100"),
                2,
                epsg=32637,
            )

            exit_code, rows, _output = self.run_mosaic_cli(
                root,
                grouping_mode=GROUPING_MODE_PASS_DATE_COMMON_CRS,
                target_crs_label="LAEA",
            )

            self.assertEqual(exit_code, 2)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["status"], "SKIPPED_INCOMPATIBLE")


if __name__ == "__main__":
    unittest.main()
