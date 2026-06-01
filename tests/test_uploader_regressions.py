import csv
import logging
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from selenium.common.exceptions import InvalidSessionIdException, TimeoutException, WebDriverException

from ee_ui_uploader import (
    EE_VERIFIED_EXISTS_STATUS,
    FILTERED_UTM_STATUS,
    RESUME_SKIP_STATUSES,
    TERMINAL_STATUSES,
    UNKNOWN_AFTER_CLICK_STATUS,
    EarthEngineUIUploader,
    RetryableUIError,
    UploadItem,
    active_upload_dialog_helper_js,
    is_invalid_browser_session,
    normalize_dialog_error_messages,
    parse_config,
)


class UploaderDialogRegressionTests(unittest.TestCase):
    def uploader_config(
        self,
        root: Path,
        *,
        scope: str = "all",
        tiles: list[str] | None = None,
        ee_sync: bool = False,
    ):
        input_folder = root / "inputs"
        input_folder.mkdir(parents=True, exist_ok=True)
        logs = root / "logs"
        return parse_config(
            {
                "input_folder": str(input_folder),
                "destination_parent": "projects/example/assets/collection",
                "upload": {
                    "scope": scope,
                    "utm_tiles": tiles or [],
                    "ee_sync_before_upload": ee_sync,
                    "batch_size": 10,
                    "max_active_ingestions": 0,
                },
                "execution": {
                    "resume": True,
                    "dry_run": True,
                },
                "metadata": {
                    "enabled": True,
                    "require_match": False,
                },
                "mosaic": {
                    "manifest_csv": str(logs / "mosaic_manifest.csv"),
                },
                "artifacts": {
                    "logs_dir": str(logs),
                    "artifacts_dir": str(logs / "artifacts"),
                    "report_csv": str(logs / "upload_report.csv"),
                    "ee_asset_inventory_csv": str(logs / "ee_asset_inventory.csv"),
                },
            },
            root,
        )

    def uploader(self, config) -> EarthEngineUIUploader:
        logger = logging.getLogger(f"test-uploader-{id(config)}")
        logger.addHandler(logging.NullHandler())
        return EarthEngineUIUploader(config, logger, assume_yes=True)

    def read_report_rows(self, path: Path) -> list[dict[str, str]]:
        with path.open("r", encoding="utf-8", newline="") as handle:
            return list(csv.DictReader(handle))

    def test_active_upload_dialog_helper_filters_visible_dialogs(self) -> None:
        script = active_upload_dialog_helper_js()

        self.assertIn("findActiveUploadDialog", script)
        self.assertIn("getClientRects", script)
        self.assertIn("getBoundingClientRect", script)
        self.assertIn("uploadDialogLooksOpen", script)

    def test_current_ui_metadata_scripts_use_active_dialog_helper(self) -> None:
        click_script = EarthEngineUIUploader.current_ui_metadata_click_script()
        fill_script = EarthEngineUIUploader.current_ui_metadata_fill_script()

        self.assertIn("findActiveUploadDialog", click_script)
        self.assertIn("findActiveUploadDialog", fill_script)
        self.assertNotIn("dialogs[dialogs.length - 1]", click_script)
        self.assertNotIn("dialogs[dialogs.length - 1]", fill_script)

    def test_asset_tree_name_is_not_treated_as_dialog_error(self) -> None:
        message = normalize_dialog_error_messages(["testtiles-461713"])

        self.assertEqual(message, "")

    def test_explicit_dialog_error_is_preserved(self) -> None:
        message = normalize_dialog_error_messages(
            ["Asset already exists: projects/example/assets/testtiles-461713"]
        )

        self.assertIn("already exists", message)

    def test_resume_skips_submitted_but_not_error_or_unknown_after_click(self) -> None:
        self.assertIn("SUBMITTED", RESUME_SKIP_STATUSES)
        self.assertIn("READY", RESUME_SKIP_STATUSES)
        self.assertIn("RUNNING", RESUME_SKIP_STATUSES)
        self.assertIn("COMPLETED", RESUME_SKIP_STATUSES)
        self.assertIn("SKIPPED_ALREADY_EXISTS", RESUME_SKIP_STATUSES)
        self.assertNotIn("ERROR", RESUME_SKIP_STATUSES)
        self.assertNotIn(UNKNOWN_AFTER_CLICK_STATUS, RESUME_SKIP_STATUSES)
        self.assertIn(UNKNOWN_AFTER_CLICK_STATUS, TERMINAL_STATUSES)

    def test_invalid_session_is_fatal(self) -> None:
        self.assertTrue(is_invalid_browser_session(InvalidSessionIdException()))
        self.assertTrue(is_invalid_browser_session(WebDriverException("invalid session id")))
        self.assertFalse(is_invalid_browser_session(WebDriverException("stale element reference")))

    def test_current_ui_destination_keeps_collection_in_asset_trailer(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = self.uploader_config(root)
            item = UploadItem(
                local_file=root / "inputs" / "a.tif",
                asset_name="a",
                asset_id="projects/example/assets/collection/a",
            )

            asset_root, asset_trailer = self.uploader(config).split_current_ui_destination(item)

            self.assertEqual(asset_root, "projects/example/assets/")
            self.assertEqual(asset_trailer, "collection/a")

    def test_recovery_failure_marks_current_upload_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "inputs" / "a.tif"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"1")
            config = self.uploader_config(root)
            config.upload.retry_attempts = 2
            uploader = self.uploader(config)
            item = UploadItem(
                local_file=path,
                asset_name="a",
                asset_id="projects/example/assets/collection/a",
            )

            with (
                mock.patch.object(uploader, "ensure_assets_tab"),
                mock.patch.object(uploader, "open_image_upload_dialog"),
                mock.patch.object(uploader, "populate_upload_dialog"),
                mock.patch.object(uploader, "submit_upload_dialog", side_effect=RetryableUIError("Please provide an asset ID.")),
                mock.patch.object(uploader, "recover_ui_state", side_effect=TimeoutException("renderer timeout")),
                mock.patch.object(uploader, "capture_debug_artifacts"),
            ):
                uploader.submit_with_retries(item)

            rows = self.read_report_rows(config.artifacts.report_csv)
            self.assertEqual(rows[0]["final_status"], "ERROR")
            self.assertIn("renderer timeout", rows[0]["error_message"])

    def test_open_earth_engine_continues_when_timeout_page_is_visible(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            uploader = self.uploader(self.uploader_config(root))
            uploader.driver = mock.Mock()
            uploader.driver.get.side_effect = TimeoutException("slow page load")

            with mock.patch.object(uploader, "wait_for_any_selector", return_value=mock.Mock()) as wait:
                uploader.open_earth_engine()

            wait.assert_called_once()
            uploader.driver.execute_script.assert_called_once_with("window.stop();")

    def test_open_earth_engine_reraises_timeout_when_ui_is_not_visible(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            uploader = self.uploader(self.uploader_config(root))
            uploader.driver = mock.Mock()
            uploader.driver.get.side_effect = TimeoutException("slow page load")

            with mock.patch.object(
                uploader,
                "wait_for_any_selector",
                side_effect=TimeoutException("no Earth Engine UI"),
            ):
                with self.assertRaises(TimeoutException):
                    uploader.open_earth_engine()

    def test_all_file_upload_scope_preserves_current_planning(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "inputs" / "a.tif").parent.mkdir(parents=True, exist_ok=True)
            (root / "inputs" / "a.tif").write_bytes(b"1")
            (root / "inputs" / "b.tiff").write_bytes(b"2")

            config = self.uploader_config(root, scope="all", ee_sync=False)
            plan = self.uploader(config).build_upload_plan()

            self.assertEqual([item.local_file.name for item in plan], ["a.tif", "b.tiff"])
            self.assertTrue(all(item.upload_filter_status == "selected_all" for item in plan))

    def test_selected_utm_scope_filters_original_crs_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            folder = root / "inputs"
            folder.mkdir(parents=True, exist_ok=True)
            keep = folder / (
                "SWOT_L2_HR_Raster_100m_UTM34M_N_x_x_x_001_002_0001_"
                "20260102T000000_20260102T010000_PGC0_01.tif"
            )
            drop = folder / (
                "SWOT_L2_HR_Raster_100m_UTM35M_N_x_x_x_001_002_0001_"
                "20260102T000000_20260102T010000_PGC0_01.tif"
            )
            keep.write_bytes(b"1")
            drop.write_bytes(b"2")

            config = self.uploader_config(
                root,
                scope="selected_utm",
                tiles=["UTM34M"],
                ee_sync=False,
            )
            plan = self.uploader(config).build_upload_plan()
            rows = self.read_report_rows(config.artifacts.report_csv)

            self.assertEqual([item.local_file.name for item in plan], [keep.name])
            self.assertEqual(rows[0]["final_status"], FILTERED_UTM_STATUS)
            self.assertEqual(rows[0]["upload_selected"], "no")
            self.assertEqual(rows[0]["output_grid"], "UTM35M")

    def test_selected_utm_scope_uses_mosaic_manifest_source_tiles_for_common_crs(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            folder = root / "inputs"
            logs = root / "logs"
            folder.mkdir(parents=True, exist_ok=True)
            logs.mkdir(parents=True, exist_ok=True)
            source = folder / (
                "SWOT_L2_HR_Raster_100m_UTM34M_N_x_x_x_001_002_0001_"
                "20260102T000000_20260102T010000_PGC0_01.tif"
            )
            output = folder / (
                "SWOT_L2_HR_Raster_100m_LAEA_N_x_x_x_001_002_MOSA_"
                "20260102T000000_20260102T010000_PGC0_01.tif"
            )
            output.write_bytes(b"1")
            with (logs / "mosaic_manifest.csv").open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=["output_file", "input_files"])
                writer.writeheader()
                writer.writerow({"output_file": str(output), "input_files": f'["{source}"]'})

            config = self.uploader_config(
                root,
                scope="selected_utm",
                tiles=["UTM34M"],
                ee_sync=False,
            )
            plan = self.uploader(config).build_upload_plan()

            self.assertEqual([item.local_file.name for item in plan], [output.name])
            self.assertEqual(plan[0].output_grid, "LAEA")
            self.assertEqual(plan[0].source_utm_tiles, ["UTM34M"])

    def test_ee_inventory_pagination_marks_existing_assets_verified(self) -> None:
        class FakeData:
            def __init__(self) -> None:
                self.calls: list[dict[str, str]] = []

            def listAssets(self, params):
                self.calls.append(dict(params))
                if "pageToken" not in params:
                    return {
                        "assets": [
                            {
                                "id": "projects/example/assets/collection/a",
                                "type": "IMAGE",
                            }
                        ],
                        "nextPageToken": "next",
                    }
                return {"assets": [], "nextPageToken": ""}

        class FakeEe:
            def __init__(self) -> None:
                self.data = FakeData()

            def Initialize(self, project=None):
                self.project = project

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            folder = root / "inputs"
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "a.tif").write_bytes(b"1")
            fake_ee = FakeEe()
            config = self.uploader_config(root, scope="all", ee_sync=True)

            with mock.patch("ee_ui_uploader.import_ee", return_value=fake_ee):
                plan = self.uploader(config).build_upload_plan()

            rows = self.read_report_rows(config.artifacts.report_csv)
            inventory_rows = self.read_report_rows(config.artifacts.ee_asset_inventory_csv)
            self.assertEqual(plan, [])
            self.assertEqual(rows[0]["final_status"], EE_VERIFIED_EXISTS_STATUS)
            self.assertEqual(rows[0]["ee_asset_exists"], "yes")
            self.assertEqual(inventory_rows[0]["asset_id"], "projects/example/assets/collection/a")
            self.assertEqual(fake_ee.data.calls[1]["pageToken"], "next")

    def test_active_report_status_is_not_ee_verified_when_asset_absent(self) -> None:
        class FakeData:
            def listAssets(self, params):
                return {"assets": []}

        class FakeEe:
            data = FakeData()

            def Initialize(self, project=None):
                pass

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            folder = root / "inputs"
            logs = root / "logs"
            folder.mkdir(parents=True, exist_ok=True)
            logs.mkdir(parents=True, exist_ok=True)
            (folder / "a.tif").write_bytes(b"1")
            report = logs / "upload_report.csv"
            with report.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=["local_file", "asset_id", "final_status"])
                writer.writeheader()
                writer.writerow(
                    {
                        "local_file": str(folder / "a.tif"),
                        "asset_id": "projects/example/assets/collection/a",
                        "final_status": "RUNNING",
                    }
                )
            config = self.uploader_config(root, scope="all", ee_sync=True)

            with mock.patch("ee_ui_uploader.import_ee", return_value=FakeEe()):
                plan = self.uploader(config).build_upload_plan()

            rows = self.read_report_rows(config.artifacts.report_csv)
            self.assertEqual(plan, [])
            self.assertEqual(rows[0]["final_status"], "RUNNING")

    def test_completed_report_status_is_retried_when_ee_sync_does_not_find_asset(self) -> None:
        class FakeData:
            def listAssets(self, params):
                return {"assets": []}

        class FakeEe:
            data = FakeData()

            def Initialize(self, project=None):
                pass

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            folder = root / "inputs"
            logs = root / "logs"
            folder.mkdir(parents=True, exist_ok=True)
            logs.mkdir(parents=True, exist_ok=True)
            (folder / "a.tif").write_bytes(b"1")
            with (logs / "upload_report.csv").open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=["local_file", "asset_id", "final_status"])
                writer.writeheader()
                writer.writerow(
                    {
                        "local_file": str(folder / "a.tif"),
                        "asset_id": "projects/example/assets/collection/a",
                        "final_status": "COMPLETED",
                    }
                )
            config = self.uploader_config(root, scope="all", ee_sync=True)

            with mock.patch("ee_ui_uploader.import_ee", return_value=FakeEe()):
                plan = self.uploader(config).build_upload_plan()

            self.assertEqual([item.asset_id for item in plan], ["projects/example/assets/collection/a"])

    def test_selected_utm_filter_preserves_existing_submitted_status(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            folder = root / "inputs"
            logs = root / "logs"
            folder.mkdir(parents=True, exist_ok=True)
            logs.mkdir(parents=True, exist_ok=True)
            path = folder / (
                "SWOT_L2_HR_Raster_100m_UTM34M_N_x_x_x_001_002_MOSA_"
                "20260102T000000_20260102T010000_PGC0_01.tif"
            )
            path.write_bytes(b"1")
            with (logs / "upload_report.csv").open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=["local_file", "asset_id", "final_status"])
                writer.writeheader()
                writer.writerow(
                    {
                        "local_file": str(path),
                        "asset_id": "projects/example/assets/collection/" + path.stem,
                        "final_status": "SUBMITTED",
                    }
                )
            config = self.uploader_config(
                root,
                scope="selected_utm",
                tiles=["UTM35M"],
                ee_sync=False,
            )

            plan = self.uploader(config).build_upload_plan()
            rows = self.read_report_rows(config.artifacts.report_csv)

            self.assertEqual(plan, [])
            self.assertEqual(rows[0]["final_status"], "SUBMITTED")

    def test_sync_existing_assets_to_report_ignores_selected_tile_filter(self) -> None:
        class FakeData:
            def listAssets(self, params):
                return {
                    "assets": [
                        {
                            "id": "projects/example/assets/collection/"
                            "SWOT_L2_HR_Raster_100m_UTM34M_N_x_x_x_001_002_MOSA_"
                            "20260102T000000_20260102T010000_PGC0_01",
                            "type": "IMAGE",
                        }
                    ]
                }

        class FakeEe:
            data = FakeData()

            def Initialize(self, project=None):
                pass

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            folder = root / "inputs"
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / (
                "SWOT_L2_HR_Raster_100m_UTM34M_N_x_x_x_001_002_MOSA_"
                "20260102T000000_20260102T010000_PGC0_01.tif"
            )
            path.write_bytes(b"1")
            config = self.uploader_config(
                root,
                scope="selected_utm",
                tiles=["UTM35M"],
                ee_sync=False,
            )

            with mock.patch("ee_ui_uploader.import_ee", return_value=FakeEe()):
                count = self.uploader(config).sync_existing_assets_to_report()

            rows = self.read_report_rows(config.artifacts.report_csv)
            self.assertEqual(count, 1)
            self.assertEqual(rows[0]["final_status"], EE_VERIFIED_EXISTS_STATUS)
            self.assertEqual(rows[0]["source_utm_tiles"], '["UTM34M"]')


if __name__ == "__main__":
    unittest.main()
