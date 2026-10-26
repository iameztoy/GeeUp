import csv
import json
import logging
import tempfile
import threading
import tkinter as tk
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from swotflow.earthengine.uploader import EarthEngineUIUploader, active_upload_dialog_helper_js
from swotflow.project.database import ProjectDatabase, read_project_rows
from swotflow.project.insights import build_update_coverage_tile_rows, collect_project_insights, write_project_insights_snapshot, load_project_insights_snapshot
from swotflow.project.lock import automation_lock
from swotflow.project.updates import record_update_expected_rows, campaign_rows, set_campaign_archived
from swotflow.processing.automation import (AutomationConfig, AutomationRunState, AutomationTilePlan,
                                preflight_automation, latest_automation_run_dir, reconcile_preflight_completion)
from swotflow.ui.main import LauncherApp
from tests.test_automation import base_config, fake_granule
from tests.test_project_insights import swot_name
from swotflow.processing.download import DownloadPreview, build_download_query
from swotflow.ui.utm_map import UTMPipelineStatusMap


class UpdateRecoveryTests(unittest.TestCase):
    def coverage(self, expected_counter="01", processed_counter="02", upload="EE_VERIFIED_EXISTS", mode="best"):
        old = swot_name(crid="PID0", counter=expected_counter)
        new = swot_name(crid="PID0", counter=processed_counter)
        tif = new.replace(".nc", ".tif")
        mosaic = swot_name(scene="MOSA", crid="PID0", counter=processed_counter, suffix=".tif")
        return build_update_coverage_tile_rows(
            config={"download": {"start_date": "2026-01-01", "end_date": "2026-01-31", "utm_tiles": ["UTM34M"], "product_version_filter": mode}},
            download_preview_rows=[{"file_name": old, "utm_tile": "UTM34M"}],
            download_rows=[{"file_name": old, "downloaded": "no", "last_status": "EXCLUDED_OLDER_VERSION"}, {"file_name": new, "downloaded": "yes"}],
            extract_rows=[{"input_nc": new, "output_tif": tif, "status": "written"}],
            mosaic_rows=[{"output_file": mosaic, "input_files": json.dumps([tif]), "status": "MOSAIC_CREATED"}],
            upload_rows=[{"local_file": mosaic, "final_status": upload}],
        )[0]

    def test_higher_version_satisfies_old_expectation_only_with_downstream_proof(self):
        row = self.coverage()
        self.assertEqual(row[1:6], (1, 1, 1, 1, 1))
        self.assertEqual(row[-1], "complete")
        self.assertEqual(self.coverage(upload="SUBMITTED")[-1], "pending_upload")
        self.assertEqual(self.coverage(upload="ERROR")[-1], "pending_upload")

    def test_older_uploaded_version_does_not_satisfy_new_requirement(self):
        row = self.coverage(expected_counter="02", processed_counter="01")
        self.assertEqual(row[1:6], (1, 0, 0, 0, 0))
        self.assertNotEqual(row[-1], "complete")

    def test_all_versions_mode_keeps_exact_version_requirements(self):
        self.assertEqual(self.coverage(mode="all")[2:6], (0, 0, 0, 0))

    def test_archive_preserves_expected_records_and_snapshot_history(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = base_config(root)
            cid = record_update_expected_rows(config, [{"file_name": swot_name(), "utm_tile": "UTM34M"}], source="test")
            path = root / "00_logs" / "update_expected.csv"
            before = read_project_rows(path)
            run = root / "00_logs" / "automation_runs" / "run"
            run.mkdir(parents=True)
            run_json = {"config": {"base_config": config, "start_date": "2026-01-01", "end_date": "2026-01-31"}, "stage_results": []}
            (run / "automation_run.json").write_text(json.dumps(run_json), encoding="utf-8")
            set_campaign_archived(config, cid, True, "Mistaken date range")
            self.assertEqual(read_project_rows(path), before)
            self.assertEqual(campaign_rows(config)[0]["archived"], "yes")
            self.assertIsNone(latest_automation_run_dir(config, root))
            self.assertEqual(json.loads((run / "automation_run.json").read_text()), run_json)
            insights = collect_project_insights(config)
            self.assertEqual(insights.active_update_campaign_id, "")
            self.assertEqual(len(insights.update_coverage_campaign_rows), 1)
            write_project_insights_snapshot(config, insights)
            loaded, _ = load_project_insights_snapshot(config)
            self.assertEqual(loaded.update_campaign_details[0]["archived"], "yes")
            self.assertTrue((root / "00_logs/statistics/project_statistics_update_windows.csv").exists())
            set_campaign_archived(config, cid, False)
            self.assertEqual(latest_automation_run_dir(config, root), run)

    def test_operation_lock_rejects_overlap_and_releases_after_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with self.assertRaisesRegex(ValueError, "test failure"):
                with automation_lock(root):
                    with self.assertRaisesRegex(RuntimeError, "Another preflight"):
                        with automation_lock(root):
                            self.fail("Concurrent lock acquired")
                    raise ValueError("test failure")
            with automation_lock(root):
                pass

    def test_concurrent_csv_exports_use_private_temporary_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db = ProjectDatabase(root / "swotflow.sqlite3")
            db.upsert_rows("update_runs", [{"run_id": "a", "source": "test"}])
            path = root / "update_runs.csv"
            gate = threading.Barrier(4)
            def export(_):
                gate.wait(timeout=10)
                return db.export_csv("update_runs", path)
            with ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(export, range(4)))
            with path.open(newline="", encoding="utf-8") as handle:
                self.assertEqual(list(csv.DictReader(handle))[0]["run_id"], "a")
            self.assertEqual(list(root.glob("*.tmp")), [])

    def test_fresh_preflight_queries_remote_even_with_cached_rows(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = base_config(root)
            granule = fake_granule("UTM34M")
            granule.selected_for_download = True
            record_update_expected_rows(config, [{"file_name": granule.file_name, "utm_tile": "UTM34M"}], source="test")
            request = AutomationConfig(root, config, ["UTM34M"], "2026-01-01", "2026-01-31", min_free_space_gb=0, refresh_cmr=True)
            def preview(cfg):
                return DownloadPreview(query=build_download_query(cfg), granules=[granule])
            builder = mock.Mock(side_effect=preview)
            with mock.patch("swotflow.processing.automation.windows_automation_reboot_warnings", return_value=[]):
                result = preflight_automation(request, preview_builder=builder)
            builder.assert_called_once()
            self.assertTrue(result.preflight_ok, result.errors)

    def test_preflight_proof_uses_newer_version_and_requires_verified_upload(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = base_config(root)
            old = fake_granule("UTM34M").file_name
            new = old.replace("PGD0_01", "PGD0_02")
            tif = new.replace(".nc", ".tif")
            mosaic = tif.replace("_001A_", "_MOSA_")
            request = AutomationConfig(root, config, ["UTM34M"], "2026-01-01", "2026-01-31", include_upload=True)
            state = AutomationRunState("test", root, request)
            stages = {
                "extract_manifest": [{"input_nc": new, "output_tif": tif, "status": "written"}],
                "mosaic_manifest": [{"output_file": mosaic, "input_files": json.dumps([tif]), "status": "MOSAIC_CREATED"}],
                "upload_report": [{"local_file": mosaic, "final_status": "EE_VERIFIED_EXISTS"}],
            }
            with mock.patch("swotflow.processing.automation.read_project_rows", side_effect=lambda path, dataset: stages[dataset]):
                for status, expected_class in [("EE_VERIFIED_EXISTS", "already complete"), ("SUBMITTED", "partial/resumable")]:
                    stages["upload_report"][0]["final_status"] = status
                    state.tile_plans = [AutomationTilePlan("UTM34M", "needs update", "old filename missing", pending_downloads=1)]
                    reconcile_preflight_completion(state, [{"file_name": old, "utm_tile": "UTM34M"}],
                                                   {"new": {"file_name": new, "downloaded": "yes"}})
                    self.assertEqual(state.tile_plans[0].classification, expected_class)
                    self.assertEqual(state.tile_plans[0].pending_downloads, 0)

    def test_gui_preflight_guard_blocks_second_request(self):
        app = LauncherApp.__new__(LauncherApp)
        app.automation_running = False
        app.automation_preflight_running = True
        with mock.patch("swotflow.ui.main.messagebox.showwarning") as warning, mock.patch.object(app, "automation_config_from_ui") as config:
            app.run_automation_preflight()
        warning.assert_called_once()
        config.assert_not_called()

    def test_open_project_does_not_replace_requested_dates_with_old_run(self):
        app = LauncherApp.__new__(LauncherApp)
        app.current_project_root_var = mock.Mock()
        app.current_project_root_var.get.return_value = "project"
        app.automation_start_date_var = mock.Mock()
        app.automation_end_date_var = mock.Mock()
        app.automation_start_date_var.get.return_value = "2026-06-30"
        app.automation_end_date_var.get.return_value = "2026-08-10"
        app.automation_process_by_year_var = tk.BooleanVar(master=tk.Tcl(), value=False)
        app.automation_selected_tiles = ["UTM34M"]
        app.automation_status_var = mock.Mock()
        state = mock.Mock()
        state.config.start_date = "2026-08-10"
        state.config.end_date = "2026-09-10"
        state.config.utm_tiles = ["UTM34M"]
        state.config.process_by_year = False
        with mock.patch.object(app, "build_config", return_value={}), mock.patch("swotflow.ui.main.load_latest_automation_run_state", return_value=state), mock.patch.object(app, "apply_automation_run_state") as apply:
            self.assertFalse(app.load_latest_automation_run_for_project())
            apply.assert_not_called()
            with mock.patch("swotflow.ui.main.messagebox.askyesno", return_value=False) as confirm:
                self.assertFalse(app.load_latest_automation_run_for_project(notify=True))
                confirm.assert_called_once()
                apply.assert_not_called()

    def test_map_preserves_viewed_window_and_hides_archived(self):
        root = tk.Tk()
        root.withdraw()
        try:
            widget = UTMPipelineStatusMap(root)
            campaigns = [("new", "Aug-Sep", "2026-08-10", "2026-09-10", "", 1, 1),
                         ("old", "Jun-Aug", "2026-06-30", "2026-08-10", "", 1, 1)]
            widget.set_update_campaigns(campaigns, {}, "old", [{"campaign_id": "new", "archived": "yes"}])
            self.assertEqual(widget.active_update_campaign_id, "old")
            self.assertNotIn("new", widget.update_campaign_label_by_id)
            widget.show_archived_var.set(True)
            widget.refresh_campaign_choices()
            self.assertIn("new", widget.update_campaign_label_by_id)
            widget.set_update_campaigns(campaigns, {}, "new", [{"campaign_id": "new", "archived": "yes"}])
            self.assertEqual(widget.active_update_campaign_id, "old")
        finally:
            root.destroy()

    def test_hidden_upload_page_reactivated_without_waits(self):
        up = EarthEngineUIUploader.__new__(EarthEngineUIUploader)
        up.driver = mock.Mock()
        up.logger = logging.getLogger("test-rendering")
        with mock.patch.object(up, "execute_script", return_value="hidden"):
            up.ensure_upload_page_rendering()
        self.assertEqual(up.driver.execute_cdp_cmd.call_args_list, [
            mock.call("Emulation.setFocusEmulationEnabled", {"enabled": True}),
            mock.call("Page.setWebLifecycleState", {"state": "active"}),
        ])
        up.driver.reset_mock()
        with mock.patch.object(up, "execute_script", return_value="visible"):
            up.ensure_upload_page_rendering()
        up.driver.execute_cdp_cmd.assert_not_called()
        self.assertIn("innerDialog.shadowRoot", active_upload_dialog_helper_js())
        self.assertNotIn("return uploadElementVisible(dialog) ||", active_upload_dialog_helper_js())


if __name__ == "__main__":
    unittest.main()
