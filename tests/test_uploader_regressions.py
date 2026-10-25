import csv
from datetime import datetime, timedelta
import logging
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from selenium.common.exceptions import InvalidSessionIdException, TimeoutException, WebDriverException

from swotflow.earthengine.uploader import (
    BLOCKED_PRODUCT_VERSION_CONFLICT_STATUS,
    EE_VERIFIED_EXISTS_STATUS,
    FILTERED_LOCAL_PRODUCT_VERSION_SUPERSEDED_STATUS,
    FILTERED_UTM_STATUS,
    RESUME_SKIP_STATUSES,
    SUBMITTED_PENDING_VERIFICATION_STATUS,
    TERMINAL_STATUSES,
    UNKNOWN_AFTER_CLICK_STATUS,
    EarthEngineUIUploader,
    EarthEngineOperation,
    RetryableUIError,
    UploadItem,
    active_upload_dialog_helper_js,
    is_invalid_browser_session,
    normalize_dialog_error_messages,
    normalize_ee_operation,
    parse_config,
)
from swotflow.project.database import read_project_rows


class UploaderDialogRegressionTests(unittest.TestCase):
    def test_existing_asset_with_unbound_revision_cannot_skip_verified_upload(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            uploader = self.uploader(self.uploader_config(root))
            uploader.config.verified_workflow = True
            local = root / "inputs" / "test.tif"
            local.write_bytes(b"new revision")
            aid = uploader.config.destination_parent + "/test"
            uploader.ee_inventory_synced = True
            uploader.ee_existing_asset_ids.add(aid)
            uploader._verified_inventory = {aid: {"id": aid}}
            proof = {"stage": "mosaic", "sources": {}, "sha256": "new"}
            with mock.patch.object(uploader, "sync_earth_engine_inventory"),\
                 mock.patch("swotflow.verification.pipeline.VerificationStore.artifact", return_value=proof),\
                 mock.patch("swotflow.verification.pipeline.artifact_current", return_value=True):
                with self.assertRaisesRegex(ValueError, "Existing EE revision is unverified"):
                    uploader.build_upload_plan()
                self.assertEqual(uploader.existing_revision_status(local, aid), "EXISTING_REVISION_UNVERIFIED")

    def test_upload_properties_bind_prepared_revision(self) -> None:
        from swotflow.verification.pipeline import ingestion_properties
        from tests.test_download_tool import granule_name
        with tempfile.TemporaryDirectory() as temp:
            uploader = self.uploader(self.uploader_config(Path(temp)))
            uploader.config.verified_workflow = True
            local = Path(temp) / Path(granule_name()).with_suffix(".tif")
            item = UploadItem(local, local.stem, "asset")
            item.ingestion_revision = {"token": "unique", "local_sha256": "hash", "sources": {"source": "checksum"}}
            with mock.patch.object(uploader, "is_current_upload_dialog_open", return_value=True),\
                 mock.patch.object(uploader, "_apply_current_ui_metadata_properties") as apply:
                uploader.apply_metadata_properties(item)
                self.assertEqual({k: apply.call_args.args[0].properties[k] for k in ingestion_properties(item.ingestion_revision)},
                                 ingestion_properties(item.ingestion_revision))

    def test_pyramiding_javascript_is_constructed_and_conflicting_bands_are_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            uploader = self.uploader(self.uploader_config(Path(temp)))
            uploader.config.upload.pyramiding_policy.default = "SAMPLE"
            uploader.config.upload.pyramiding_policy.per_band = {"b1":"SAMPLE","b2":"SAMPLE"}
            with mock.patch.object(uploader,"is_current_upload_dialog_open",return_value=True),\
                 mock.patch.object(uploader,"execute_script",return_value={"ok":True}) as script:
                self.assertTrue(uploader._apply_current_ui_pyramiding_policy())
                self.assertIn("${arguments[0]}",script.call_args.args[0])
                uploader.config.upload.pyramiding_policy.per_band["b1"] = "MEAN"
                with self.assertRaisesRegex(ValueError,"different per-band"):
                    uploader._apply_current_ui_pyramiding_policy()

    def test_verified_resume_does_not_trust_historical_success_for_missing_asset(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            uploader = self.uploader(self.uploader_config(Path(temp)))
            uploader.config.verified_workflow = True
            uploader.ee_inventory_synced = True
            aid = uploader.config.destination_parent + "/missing"
            for status in ("EE_VERIFIED_EXISTS", "READY", "RUNNING", "COMPLETED"):
                self.assertFalse(uploader.should_resume_skip_existing_status(status,aid))
            uploader.ee_existing_asset_ids.add(aid)
            self.assertTrue(uploader.should_resume_skip_existing_status("EE_VERIFIED_EXISTS",aid))

    def test_verified_upload_blocks_missing_proof_and_partial_duplicate(self) -> None:
        from tests.test_download_tool import granule_name
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            uploader = self.uploader(self.uploader_config(root))
            uploader.config.verified_workflow = True
            (root/"inputs"/"test.tif").write_bytes(b"test")
            first, second = granule_name(scene="001F"), granule_name(scene="002F")
            proof = {"stage":"mosaic","sources":{first:{"hash":"a"},second:{"hash":"b"}}}
            with mock.patch.object(uploader,"sync_earth_engine_inventory"),\
                 mock.patch("swotflow.verification.pipeline.VerificationStore.artifact",return_value=None):
                with self.assertRaisesRegex(ValueError,"mosaic byte proof"):
                    uploader.build_upload_plan()
            with mock.patch.object(uploader,"sync_earth_engine_inventory"),\
                 mock.patch("swotflow.verification.pipeline.VerificationStore.artifact",return_value=proof),\
                 mock.patch("swotflow.verification.pipeline.artifact_current",return_value=True),\
                 mock.patch("swotflow.verification.pipeline.active_proofs",return_value={first:{"checksum":{"hash":"a"},"asset_id":"old"}}):
                with self.assertRaisesRegex(ValueError,"partially duplicated mosaic"):
                    uploader.build_upload_plan()
                uploader._verified_active_inputs[second] = {"checksum":{"hash":"b"},"asset_id":"old"}
                self.assertEqual(uploader.build_upload_plan(),[])
                self.assertEqual(next(iter(uploader.report.rows.values()))["final_status"],"SKIPPED_VERIFIED_REPLACEMENT")

    def uploader_config(
        self,
        root: Path,
        *,
        scope: str = "all",
        tiles: list[str] | None = None,
        ee_sync: bool = False,
        completion_mode: str = "wait_for_ee",
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
                    "completion_mode": completion_mode,
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
        return read_project_rows(path, "upload_report")

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

    def test_cannot_overwrite_task_is_treated_as_existing_asset(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            uploader = self.uploader(self.uploader_config(Path(temp)))

            row = uploader.parse_task_row(
                "Asset name: projects/example/assets/collection/image\n"
                "Phase: Failed\n"
                "Error: Cannot overwrite asset 'projects/example/assets/collection/image'."
            )

            self.assertEqual(row.normalized_status, "SKIPPED_ALREADY_EXISTS")

    def test_resume_skips_submitted_but_not_error_or_unknown_after_click(self) -> None:
        self.assertIn("SUBMITTED", RESUME_SKIP_STATUSES)
        self.assertIn(SUBMITTED_PENDING_VERIFICATION_STATUS, RESUME_SKIP_STATUSES)
        self.assertIn("READY", RESUME_SKIP_STATUSES)
        self.assertIn("RUNNING", RESUME_SKIP_STATUSES)
        self.assertIn("COMPLETED", RESUME_SKIP_STATUSES)
        self.assertIn("SKIPPED_ALREADY_EXISTS", RESUME_SKIP_STATUSES)
        self.assertNotIn("ERROR", RESUME_SKIP_STATUSES)
        self.assertNotIn(UNKNOWN_AFTER_CLICK_STATUS, RESUME_SKIP_STATUSES)
        self.assertIn(UNKNOWN_AFTER_CLICK_STATUS, TERMINAL_STATUSES)

    def test_wait_for_ee_pending_verification_returns_warning_code(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = self.uploader_config(root, completion_mode="wait_for_ee")
            config.execution.dry_run = False
            uploader = self.uploader(config)
            item = UploadItem(
                local_file=root / "inputs" / "a.tif",
                asset_name="a",
                asset_id="projects/example/assets/collection/a",
            )

            def mark_pending(_items):
                uploader.report.merge_row(
                    {
                        "local_file": str(item.local_file),
                        "asset_id": item.asset_id,
                        "final_status": SUBMITTED_PENDING_VERIFICATION_STATUS,
                    }
                )

            with (
                mock.patch.object(uploader, "build_upload_plan", return_value=[item]),
                mock.patch.object(uploader, "confirm_before_real_upload"),
                mock.patch.object(uploader, "create_driver", return_value=mock.Mock()),
                mock.patch.object(uploader, "open_earth_engine"),
                mock.patch.object(uploader, "ensure_logged_in"),
                mock.patch.object(uploader, "process_batches", side_effect=mark_pending),
                mock.patch.object(uploader, "wait_for_all_tracked_tasks"),
            ):
                self.assertEqual(uploader._run(), 2)

    def test_submit_and_continue_pending_verification_remains_success_code(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = self.uploader_config(root, completion_mode="submit_and_continue")
            config.execution.dry_run = False
            uploader = self.uploader(config)
            item = UploadItem(
                local_file=root / "inputs" / "a.tif",
                asset_name="a",
                asset_id="projects/example/assets/collection/a",
            )

            def mark_pending(_items):
                uploader.report.merge_row(
                    {
                        "local_file": str(item.local_file),
                        "asset_id": item.asset_id,
                        "final_status": SUBMITTED_PENDING_VERIFICATION_STATUS,
                    }
                )

            with (
                mock.patch.object(uploader, "build_upload_plan", return_value=[item]),
                mock.patch.object(uploader, "confirm_before_real_upload"),
                mock.patch.object(uploader, "create_driver", return_value=mock.Mock()),
                mock.patch.object(uploader, "open_earth_engine"),
                mock.patch.object(uploader, "ensure_logged_in"),
                mock.patch.object(uploader, "process_batches", side_effect=mark_pending),
                mock.patch.object(uploader, "reconcile_submit_and_continue_uploads"),
            ):
                self.assertEqual(uploader._run(), 0)

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
                mock.patch.object(uploader, "close_dialog_if_possible") as close_dialog,
            ):
                uploader.submit_with_retries(item)

            rows = self.read_report_rows(config.artifacts.report_csv)
            self.assertEqual(rows[0]["final_status"], "ERROR")
            self.assertIn("renderer timeout", rows[0]["error_message"])
            close_dialog.assert_called_once()

    def test_open_dialog_after_click_timeout_is_retried(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            uploader = self.uploader(self.uploader_config(root))
            item = UploadItem(
                local_file=root / "inputs" / "a.tif",
                asset_name="a",
                asset_id="projects/example/assets/collection/a",
            )
            with (
                mock.patch.object(uploader, "is_current_upload_dialog_open", return_value=True),
                mock.patch.object(uploader, "_click_current_ui_upload_button"),
                mock.patch.object(uploader, "read_dialog_error_message", return_value=""),
                mock.patch.object(
                    uploader,
                    "wait_for_current_upload_dialog_close",
                    side_effect=RetryableUIError("remained open without progress"),
                ),
                mock.patch("swotflow.earthengine.uploader.time.sleep"),
            ):
                with self.assertRaises(RetryableUIError) as raised:
                    uploader.submit_upload_dialog(item)

            self.assertIn("remained open", str(raised.exception))

    def test_closed_current_dialog_returns_pending_verification(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            uploader = self.uploader(self.uploader_config(root))
            item = UploadItem(
                local_file=root / "inputs" / "a.tif",
                asset_name="a",
                asset_id="projects/example/assets/collection/a",
            )
            with (
                mock.patch.object(
                    uploader,
                    "is_current_upload_dialog_open",
                    return_value=True,
                ),
                mock.patch.object(uploader, "_click_current_ui_upload_button"),
                mock.patch.object(uploader, "read_dialog_error_message", return_value=""),
                mock.patch.object(
                    uploader,
                    "wait_for_current_upload_dialog_close",
                ),
                mock.patch("swotflow.earthengine.uploader.time.sleep"),
            ):
                outcome = uploader.submit_upload_dialog(item)

            self.assertEqual(outcome.status, SUBMITTED_PENDING_VERIFICATION_STATUS)
            self.assertEqual(outcome.task_name, "")

    def test_dialog_close_wait_accepts_normal_closure_without_task_lookup(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            uploader = self.uploader(self.uploader_config(root))
            item = UploadItem(
                local_file=root / "inputs" / "a.tif",
                asset_name="a",
                asset_id="projects/example/assets/collection/a",
            )
            with (
                mock.patch.object(
                    uploader,
                    "is_current_upload_dialog_open",
                    side_effect=[True, False],
                ),
                mock.patch.object(uploader, "current_upload_progress_signature", return_value=""),
                mock.patch.object(uploader, "read_dialog_error_message", return_value=""),
                mock.patch("swotflow.earthengine.uploader.time.sleep"),
            ):
                uploader.wait_for_current_upload_dialog_close(
                    item,
                    idle_timeout_seconds=30,
                )

    def test_wait_for_task_name_raises_instead_of_returning_blank(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            uploader = self.uploader(self.uploader_config(root))
            item = UploadItem(
                local_file=root / "inputs" / "a.tif",
                asset_name="a",
                asset_id="projects/example/assets/collection/a",
            )
            with mock.patch.object(uploader, "collect_task_rows", return_value=[]):
                with self.assertRaises(TimeoutException):
                    uploader.wait_for_task_name(item, timeout_seconds=0)

    def test_submit_with_retries_records_pending_verification_after_dialog_close(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "inputs" / "a.tif"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"1")
            config = self.uploader_config(root)
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
                mock.patch.object(
                    uploader,
                    "submit_upload_dialog",
                    return_value=mock.Mock(
                        status=SUBMITTED_PENDING_VERIFICATION_STATUS,
                        task_name="",
                    ),
                ),
            ):
                uploader.submit_with_retries(item)

            rows = self.read_report_rows(config.artifacts.report_csv)
            self.assertEqual(
                rows[0]["final_status"],
                SUBMITTED_PENDING_VERIFICATION_STATUS,
            )
            self.assertIn("awaiting", rows[0]["error_message"].lower())

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

    def test_upload_plan_keeps_highest_ranked_local_product_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            folder = root / "inputs"
            folder.mkdir(parents=True, exist_ok=True)
            older = folder / (
                "SWOT_L2_HR_Raster_100m_UTM34M_N_x_x_x_001_002_MOSA_"
                "20260102T000000_20260102T010000_PID0_01.tif"
            )
            newer = folder / (
                "SWOT_L2_HR_Raster_100m_UTM34M_N_x_x_x_001_002_MOSA_"
                "20260102T000000_20260102T010000_PGD0_01.tif"
            )
            older.write_bytes(b"old")
            newer.write_bytes(b"new")
            config = self.uploader_config(root, scope="all", ee_sync=False)

            plan = self.uploader(config).build_upload_plan()
            rows = self.read_report_rows(config.artifacts.report_csv)

            self.assertEqual([item.local_file.name for item in plan], [newer.name])
            self.assertEqual(rows[0]["final_status"], FILTERED_LOCAL_PRODUCT_VERSION_SUPERSEDED_STATUS)
            self.assertEqual(rows[0]["upload_selected"], "no")
            self.assertEqual(rows[0]["best_processing_level"], "PGD0_01")

    def test_upload_plan_blocks_same_identity_existing_ee_asset_with_new_level(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            folder = root / "inputs"
            logs = root / "logs"
            folder.mkdir(parents=True, exist_ok=True)
            logs.mkdir(parents=True, exist_ok=True)
            old_stem = (
                "SWOT_L2_HR_Raster_100m_UTM34M_N_x_x_x_001_002_MOSA_"
                "20260102T000000_20260102T010000_PID0_01"
            )
            newer = folder / (
                "SWOT_L2_HR_Raster_100m_UTM34M_N_x_x_x_001_002_MOSA_"
                "20260102T000000_20260102T010000_PGD0_01.tif"
            )
            newer.write_bytes(b"new")
            config = self.uploader_config(root, scope="all", ee_sync=False)
            with config.artifacts.report_csv.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=["local_file", "asset_id", "final_status"])
                writer.writeheader()
                writer.writerow(
                    {
                        "local_file": "",
                        "asset_id": f"projects/example/assets/collection/{old_stem}",
                        "final_status": EE_VERIFIED_EXISTS_STATUS,
                    }
                )

            plan = self.uploader(config).build_upload_plan()
            rows = {
                row["asset_id"]: row
                for row in self.read_report_rows(config.artifacts.report_csv)
            }
            blocked = rows[f"projects/example/assets/collection/{newer.stem}"]

            self.assertEqual(plan, [])
            self.assertEqual(blocked["final_status"], BLOCKED_PRODUCT_VERSION_CONFLICT_STATUS)
            self.assertEqual(blocked["conflicting_asset_id"], f"projects/example/assets/collection/{old_stem}")

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

            with mock.patch("swotflow.earthengine.uploader.import_ee", return_value=fake_ee):
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

            with mock.patch("swotflow.earthengine.uploader.import_ee", return_value=FakeEe()):
                plan = self.uploader(config).build_upload_plan()

            rows = self.read_report_rows(config.artifacts.report_csv)
            self.assertEqual(plan, [])
            self.assertEqual(rows[0]["final_status"], "RUNNING")

    def test_submitted_report_status_is_retried_when_ee_sync_does_not_find_asset(self) -> None:
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
                        "final_status": "SUBMITTED",
                    }
                )
            config = self.uploader_config(root, scope="all", ee_sync=True)

            with mock.patch("swotflow.earthengine.uploader.import_ee", return_value=FakeEe()):
                plan = self.uploader(config).build_upload_plan()

            self.assertEqual([item.asset_id for item in plan], ["projects/example/assets/collection/a"])

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

            with mock.patch("swotflow.earthengine.uploader.import_ee", return_value=FakeEe()):
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

            with mock.patch("swotflow.earthengine.uploader.import_ee", return_value=FakeEe()):
                count = self.uploader(config).sync_existing_assets_to_report()

            rows = self.read_report_rows(config.artifacts.report_csv)
            self.assertEqual(count, 1)
            self.assertEqual(rows[0]["final_status"], EE_VERIFIED_EXISTS_STATUS)
            self.assertEqual(rows[0]["source_utm_tiles"], '["UTM34M"]')

    def test_wait_for_tracked_tasks_uses_ee_inventory_when_task_rows_do_not_match(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            folder = root / "inputs"
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / (
                "SWOT_L2_HR_Raster_100m_UTM34M_N_x_x_x_001_002_MOSA_"
                "20260102T000000_20260102T010000_PGC0_01.tif"
            )
            path.write_bytes(b"1")
            config = self.uploader_config(root, ee_sync=True)
            uploader = self.uploader(config)
            asset_name = path.stem
            asset_id = f"{config.destination_parent}/{asset_name}"
            item = UploadItem(local_file=path, asset_name=asset_name, asset_id=asset_id, batch_number=1)
            uploader.tracked_items[asset_id] = item
            uploader.report.upsert(
                uploader.make_report_row(
                    item=item,
                    submit_time="2026-01-02T00:00:00",
                    detected_task_name="",
                    final_status="SUBMITTED",
                    error_message="Submitted in browser.",
                )
            )

            def fake_sync_inventory() -> None:
                uploader.ee_existing_asset_ids = {asset_id}

            with mock.patch.object(uploader, "collect_task_rows", return_value=[]):
                with mock.patch.object(uploader, "sync_earth_engine_inventory", side_effect=fake_sync_inventory):
                    with mock.patch("swotflow.earthengine.uploader.time.sleep") as sleep:
                        uploader.wait_for_all_tracked_tasks()

            rows = self.read_report_rows(config.artifacts.report_csv)
            self.assertEqual(rows[0]["final_status"], EE_VERIFIED_EXISTS_STATUS)
            self.assertEqual(rows[0]["ee_asset_exists"], "yes")
            self.assertEqual(rows[0]["verification_source"], "ee.data.listAssets")
            sleep.assert_not_called()

    def test_submit_and_continue_mode_does_not_wait_for_batch_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = self.uploader_config(
                root,
                completion_mode="submit_and_continue",
            )
            uploader = self.uploader(config)
            path = config.input_folder / (
                "SWOT_L2_HR_Raster_100m_UTM34M_N_x_x_x_001_002_MOSA_"
                "20260102T000000_20260102T010000_PGC0_01.tif"
            )
            path.write_bytes(b"1")
            item = UploadItem(
                local_file=path,
                asset_name=path.stem,
                asset_id=f"{config.destination_parent}/{path.stem}",
                batch_number=1,
            )

            with mock.patch.object(uploader, "submit_with_retries") as submit:
                with mock.patch.object(uploader, "wait_for_batch_gate") as wait_gate:
                    with mock.patch.object(uploader, "reconcile_batch_without_waiting") as reconcile:
                        uploader.process_batches([item])

            submit.assert_called_once_with(item)
            wait_gate.assert_not_called()
            reconcile.assert_called_once()

    def test_browser_transfer_gate_waits_for_ee_operation_acknowledgement(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = self.uploader_config(root, ee_sync=True)
            uploader = self.uploader(config)
            items = []
            for number in (1, 2):
                path = config.input_folder / (
                    "SWOT_L2_HR_Raster_100m_UTM34M_N_x_x_x_001_"
                    f"00{number}_MOSA_20260102T000000_20260102T010000_PID0_01.tif"
                )
                path.write_bytes(b"1")
                item = UploadItem(
                    local_file=path,
                    asset_name=path.stem,
                    asset_id=f"{config.destination_parent}/{path.stem}",
                )
                items.append(item)
                uploader.tracked_items[item.asset_id] = item
                uploader.report.upsert(
                    uploader.make_report_row(
                        item=item,
                        submit_time=datetime.now().astimezone().isoformat(timespec="seconds"),
                        detected_task_name="",
                        final_status=SUBMITTED_PENDING_VERIFICATION_STATUS,
                        error_message="Dialog closed.",
                    )
                )

            def acknowledge_one() -> dict[str, EarthEngineOperation]:
                item = items[0]
                operation = EarthEngineOperation(
                    asset_id=item.asset_id,
                    operation_name="projects/example/operations/123",
                    state="RUNNING",
                    done=False,
                )
                uploader.ee_operations_by_asset[item.asset_id] = operation
                return {item.asset_id: operation}

            with mock.patch.object(
                uploader,
                "refresh_earth_engine_operations",
                side_effect=acknowledge_one,
            ) as refresh:
                with mock.patch("swotflow.earthengine.uploader.time.sleep"):
                    drained = uploader.wait_for_browser_upload_capacity(target_pending=1)

            self.assertTrue(drained)
            self.assertEqual(
                uploader.report.get_status(items[0].asset_id),
                "RUNNING",
            )
            self.assertEqual(
                uploader.pending_browser_upload_asset_ids(),
                {items[1].asset_id},
            )
            refresh.assert_called_once()

    def test_normalize_ee_operation_extracts_destination_asset(self) -> None:
        asset_id = "projects/example/assets/collection/test_asset"
        operation = normalize_ee_operation(
            {
                "name": "projects/example/operations/123",
                "done": False,
                "metadata": {
                    "type": "INGEST_IMAGE",
                    "state": "RUNNING",
                    "destinationUris": [
                        f"https://code.earthengine.google.com/?asset={asset_id}"
                    ],
                },
            }
        )
        self.assertEqual(operation.asset_id, asset_id)
        self.assertEqual(operation.state, "RUNNING")

    def test_unconfirmed_submission_without_asset_or_operation_becomes_retryable(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = self.uploader_config(root, ee_sync=True)
            uploader = self.uploader(config)
            path = config.input_folder / (
                "SWOT_L2_HR_Raster_100m_UTM34M_N_x_x_x_001_002_MOSA_"
                "20260102T000000_20260102T010000_PID0_01.tif"
            )
            path.write_bytes(b"1")
            item = UploadItem(
                local_file=path,
                asset_name=path.stem,
                asset_id=f"{config.destination_parent}/{path.stem}",
            )
            old_submit_time = (
                datetime.now().astimezone() - timedelta(minutes=5)
            ).isoformat(timespec="seconds")
            uploader.report.upsert(
                uploader.make_report_row(
                    item=item,
                    submit_time=old_submit_time,
                    detected_task_name="",
                    final_status=SUBMITTED_PENDING_VERIFICATION_STATUS,
                    error_message="Dialog closed.",
                )
            )
            uploader.ee_inventory_synced = True
            uploader.ee_existing_asset_ids = set()
            uploader.ee_operations_by_asset = {}

            uploader.reconcile_unconfirmed_report_rows()

            self.assertEqual(
                uploader.report.get_status(item.asset_id),
                UNKNOWN_AFTER_CLICK_STATUS,
            )
            event_rows = read_project_rows(
                config.artifacts.report_csv.with_name("upload_events.csv"),
                "upload_events",
            )
            self.assertTrue(
                any(row.get("new_status") == UNKNOWN_AFTER_CLICK_STATUS for row in event_rows)
            )

    def test_active_ee_operation_keeps_upload_out_of_retry_plan(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = self.uploader_config(root, ee_sync=True)
            uploader = self.uploader(config)
            asset_id = f"{config.destination_parent}/test_asset"
            uploader.ee_inventory_synced = True
            uploader.ee_operations_by_asset = {
                asset_id: EarthEngineOperation(
                    asset_id=asset_id,
                    operation_name="projects/example/operations/123",
                    state="RUNNING",
                    done=False,
                )
            }
            uploader.report.rows[asset_id] = {
                "asset_id": asset_id,
                "final_status": SUBMITTED_PENDING_VERIFICATION_STATUS,
                "submit_time": "2026-01-01T00:00:00+00:00",
            }

            self.assertTrue(
                uploader.should_resume_skip_existing_status(
                    SUBMITTED_PENDING_VERIFICATION_STATUS,
                    asset_id,
                )
            )


if __name__ == "__main__":
    unittest.main()
