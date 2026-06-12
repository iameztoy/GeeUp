import tempfile
import unittest
from datetime import date
from pathlib import Path

from build_spatial_presets import normalize_grid_token
from swotflow_project import (
    PROJECT_FILE_NAME,
    append_download_history,
    config_for_project,
    create_project,
    load_builtin_tile_presets,
    load_project,
    load_project_tile_profiles,
    prepare_update_dates,
    project_paths,
    save_tile_profile,
)


class ProjectTests(unittest.TestCase):
    def sample_config(self) -> dict:
        return {
            "input_folder": "./SWOT_Processing/03_mosaics",
            "destination_parent": "projects/example/assets/collection",
            "processing": {
                "root": "./SWOT_Processing",
                "raw_downloads": "./SWOT_Processing/01_raw_downloads",
                "extracted_geotiffs": "./SWOT_Processing/02_extracted_geotiffs",
                "mosaics": "./SWOT_Processing/03_mosaics",
                "logs": "./SWOT_Processing/00_logs",
            },
            "download": {
                "collection_short_name": "SWOT_L2_HR_Raster_100m_D",
                "collection_version_label": "Version D active",
                "start_date": "2026-01-01",
                "end_date": "2026-01-31",
                "utm_tiles": ["UTM30R"],
            },
            "duplicates": {},
            "extract": {},
            "mosaic": {},
        }

    def test_create_load_project_and_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "Okavango"
            project = create_project(root, "Okavango Delta", self.sample_config())

            self.assertTrue((root / PROJECT_FILE_NAME).exists())
            for path in project_paths(root).values():
                self.assertTrue(path.exists())
            self.assertEqual(
                project.config["download"]["output_folder"],
                str(root / "01_raw_downloads"),
            )

            loaded = load_project(root)

            self.assertEqual(loaded.name, "Okavango Delta")
            self.assertEqual(loaded.config["extract"]["output_folder"], str(root / "02_extracted_geotiffs"))

    def test_config_for_project_repoints_workflow_folders(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "Africa"

            config = config_for_project(self.sample_config(), root)

            self.assertEqual(config["processing"]["root"], str(root))
            self.assertEqual(config["download"]["report_csv"], str(root / "00_logs" / "download_preview.csv"))
            self.assertEqual(config["processing"]["database"], str(root / "swotflow.sqlite3"))
            self.assertEqual(config["download"]["manifest_csv"], str(root / "00_logs" / "download_manifest.csv"))
            self.assertEqual(config["duplicates"]["input_folder"], str(root / "01_raw_downloads"))
            self.assertEqual(config["mosaic"]["input_folder"], str(root / "02_extracted_geotiffs"))
            self.assertEqual(config["mosaic"]["manifest_csv"], str(root / "00_logs" / "mosaic_manifest.csv"))
            self.assertEqual(config["artifacts"]["logs_dir"], str(root / "00_logs"))
            self.assertEqual(config["artifacts"]["artifacts_dir"], str(root / "00_logs" / "upload_artifacts"))
            self.assertEqual(config["artifacts"]["report_csv"], str(root / "00_logs" / "upload_report.csv"))
            self.assertEqual(config["artifacts"]["ee_asset_inventory_csv"], str(root / "00_logs" / "ee_asset_inventory.csv"))
            self.assertEqual(config["input_folder"], str(root / "03_mosaics"))

    def test_project_tile_profiles_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)

            path = save_tile_profile(root, "UTM30R test", ["utm30r", "UTM29R"])
            presets = load_project_tile_profiles(root)

            self.assertTrue(path.exists())
            self.assertIn("UTM30R test", presets)
            self.assertEqual(presets["UTM30R test"].tiles, ["UTM30R", "UTM29R"])

    def test_append_download_history(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "History"
            create_project(root, "History", self.sample_config())

            append_download_history(
                root,
                {
                    "status": "success",
                    "start_date": "2026-01-01",
                    "end_date": "2026-01-31",
                    "failed_count": 0,
                },
            )
            loaded = load_project(root)

            self.assertEqual(len(loaded.download_history), 1)
            self.assertEqual(loaded.download_history[0]["end_date"], "2026-01-31")

    def test_builtin_continent_presets_load(self) -> None:
        presets = load_builtin_tile_presets()

        self.assertIn("Africa", presets)
        self.assertIn("UTM34M", presets["Africa"].tiles)
        self.assertGreater(len(presets["Africa"].tiles), 50)

    def test_update_dates_use_last_successful_download_end(self) -> None:
        history = [
            {"status": "failed", "end_date": "2026-01-15", "failed_count": 2},
            {"status": "success", "end_date": "2026-02-01", "failed_count": 0},
            {"status": "success", "end_date": "2026-03-01", "failed_count": 0},
        ]

        start, end = prepare_update_dates(history, today=date(2026, 5, 16))

        self.assertEqual(start, "2026-03-01")
        self.assertEqual(end, "2026-05-16")

    def test_normalize_grid_token_rejects_invalid_rows(self) -> None:
        self.assertEqual(normalize_grid_token(33.0, "L"), "UTM33L")
        self.assertEqual(normalize_grid_token("3", "c"), "UTM03C")
        self.assertIsNone(normalize_grid_token(0, "A"))
        self.assertIsNone(normalize_grid_token(30, "I"))


if __name__ == "__main__":
    unittest.main()
