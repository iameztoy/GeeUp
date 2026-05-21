import subprocess
import tempfile
import unittest
from pathlib import Path

from gdal_runtime import DEFAULT_GDAL_PYTHON, build_gdal_runtime_env, check_gdal_runtime
from swot_extract_tool import (
    ExtractConfig,
    ExtractSource,
    TARGET_CRS_AFRICA_LAEA,
    TARGET_CRS_ORIGINAL,
    TARGET_CRS_WGS84,
    build_extraction_plan,
    build_output_path,
    load_config_file,
    normalize_year_selection,
    parse_filename,
    process_one_file,
    run_extract,
)


GDAL_CHECK = check_gdal_runtime(DEFAULT_GDAL_PYTHON)
GDAL_AVAILABLE = GDAL_CHECK.ok


def nc_name(
    *,
    utm: str = "36M",
    scene: str = "000A",
    start: str = "20250707T010000",
    end: str = "20250707T010100",
    crid: str = "PID0",
    counter: str = "01",
) -> str:
    return (
        "SWOT_L2_HR_Raster_"
        f"100m_UTM{utm}_N_x_x_x_"
        f"035_225_{scene}_{start}_{end}_{crid}_{counter}.nc"
    )


class ExtractPlanningTests(unittest.TestCase):
    def test_parse_filename_matches_notebook_pattern(self) -> None:
        metadata = parse_filename(Path(nc_name()))

        self.assertIsNotNone(metadata)
        assert metadata is not None
        self.assertEqual(metadata["utm_zone"], "36")
        self.assertEqual(metadata["mgrs_band"], "M")
        self.assertEqual(metadata["cycle"], "035")
        self.assertEqual(metadata["pass"], "225")
        self.assertEqual(metadata["year"], 2025)
        self.assertEqual(metadata["date"], "20250707")

    def test_parse_filename_rejects_non_swot_name(self) -> None:
        self.assertIsNone(parse_filename(Path("not_a_swot_file.nc")))

    def test_parse_filename_accepts_podaac_swot_suffix(self) -> None:
        metadata = parse_filename(Path(nc_name().replace(".nc", "_swot.nc")))

        self.assertIsNotNone(metadata)
        assert metadata is not None
        self.assertEqual(metadata["utm_zone"], "36")
        self.assertEqual(metadata["mgrs_band"], "M")

    def test_normalize_year_selection(self) -> None:
        self.assertIsNone(normalize_year_selection("all"))
        self.assertEqual(normalize_year_selection(2025), {2025})
        self.assertEqual(normalize_year_selection("2024, 2025"), {2024, 2025})

    def test_output_paths_match_notebook_suffixes(self) -> None:
        nc_path = Path(nc_name())
        out = Path("out")

        self.assertEqual(
            build_output_path(nc_path, out, TARGET_CRS_ORIGINAL).name,
            f"{nc_path.stem}.tif",
        )
        self.assertEqual(
            build_output_path(nc_path, out, TARGET_CRS_AFRICA_LAEA).name,
            f"{nc_path.stem}_africa_laea.tif",
        )
        self.assertEqual(
            build_output_path(nc_path, out, TARGET_CRS_WGS84).name,
            f"{nc_path.stem}_wgs84.tif",
        )

    def test_build_plan_applies_year_filter_and_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / nc_name(scene="000A", start="20240707T010000", end="20240707T010100")).touch()
            (root / nc_name(scene="001A")).touch()
            (root / nc_name(scene="002A", start="20250707T011000", end="20250707T011100")).touch()
            (root / "unmatched.nc").touch()

            config = ExtractConfig(
                input_folder=root,
                output_folder=root / "out",
                year_selection="2025",
                limit_files=1,
                manifest_csv=root / "manifest.csv",
                errors_csv=root / "errors.csv",
            )
            plan = build_extraction_plan(config)

            self.assertEqual(plan.total_nc_files, 4)
            self.assertEqual(plan.available_years, [2024, 2025])
            self.assertEqual(len(plan.selected), 1)
            self.assertEqual(len(plan.unmatched), 1)
            self.assertEqual(plan.selected[0].metadata["year"], 2025)

    def test_dry_run_does_not_write_csvs(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / nc_name()).touch()
            config = ExtractConfig(
                input_folder=root,
                output_folder=root / "out",
                manifest_csv=root / "manifest.csv",
                errors_csv=root / "errors.csv",
            )

            exit_code, plan, results, errors = run_extract(
                config,
                dry_run=True,
                check_gdal=False,
            )

            self.assertEqual(exit_code, 0)
            self.assertEqual(len(plan.selected), 1)
            self.assertEqual(results, [])
            self.assertEqual(errors, [])
            self.assertFalse((root / "manifest.csv").exists())
            self.assertFalse((root / "errors.csv").exists())

    def test_load_config_file_uses_extract_section(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config_path = root / "config.yaml"
            config_path.write_text(
                "\n".join(
                    [
                        "extract:",
                        f"  input_folder: \"{root.as_posix()}\"",
                        f"  output_folder: \"{(root / 'out').as_posix()}\"",
                        "  target_crs_mode: \"wgs84\"",
                        "  year_selection: \"2024,2025\"",
                        "  limit_files: 5",
                        "  skip_existing: false",
                        "  workers: 3",
                        f"  manifest_csv: \"{(root / 'manifest.csv').as_posix()}\"",
                        f"  errors_csv: \"{(root / 'errors.csv').as_posix()}\"",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            config = load_config_file(config_path)

            self.assertEqual(config.input_folder, root)
            self.assertEqual(config.output_folder, root / "out")
            self.assertEqual(config.target_crs_mode, TARGET_CRS_WGS84)
            self.assertEqual(config.year_selection, "2024,2025")
            self.assertEqual(config.limit_files, 5)
            self.assertFalse(config.skip_existing)
            self.assertEqual(config.workers, 3)

    def test_process_one_file_can_skip_from_manifest_without_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            input_path = root / nc_name()
            input_path.touch()
            metadata = parse_filename(input_path)
            assert metadata is not None
            config = ExtractConfig(
                input_folder=root,
                output_folder=root / "out",
                manifest_csv=root / "manifest.csv",
                errors_csv=root / "errors.csv",
            )
            source = ExtractSource(path=input_path, metadata=metadata)
            record_id = f"{input_path.name}|original"

            row = process_one_file(
                source,
                config,
                gdal=None,
                existing_manifest={
                    record_id: {
                        "record_id": record_id,
                        "status": "written",
                        "xsize": "10",
                        "ysize": "20",
                        "band_count": "2",
                    }
                },
            )

            self.assertEqual(row["status"], "skipped_manifest")
            self.assertEqual(row["known_from_manifest"], "yes")
            self.assertEqual(row["output_exists"], "no")


@unittest.skipUnless(GDAL_AVAILABLE, f"GDAL runtime unavailable: {GDAL_CHECK.stderr}")
class ExtractGdalRuntimeTests(unittest.TestCase):
    def test_check_gdal_command_confirms_required_drivers(self) -> None:
        result = subprocess.run(
            [
                str(DEFAULT_GDAL_PYTHON),
                str(Path(__file__).resolve().parents[1] / "swot_extract_tool.py"),
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


if __name__ == "__main__":
    unittest.main()
