"""Browser transfer failures must not block other files or poison a later attempt."""
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from swotflow.earthengine.browser_uploads import (
    BrowserUploadRow, current_transfer_failures, normalize_browser_upload_rows,
)
from swotflow.earthengine.uploader import UploadItem, EarthEngineOperation
from tests import test_uploader_regressions as fixtures


class BrowserUploadRecoveryTests(unittest.TestCase):
    def row(self, ident="new", asset="projects/p/assets/a", failed=True):
        return BrowserUploadRow(ident, asset, failed, "File upload unsuccessful (Error: HTTP error 400.)")

    def test_normalization_requires_exact_asset_and_element_identity(self):
        rows = normalize_browser_upload_rows([
            {"element": SimpleNamespace(id="card-1"), "asset_id": "/projects/p/assets/a", "failed": True},
            {"asset_id": "projects/p/assets/b", "failed": True},
            {"element": SimpleNamespace(id="card-3"), "asset_id": "a", "failed": True},
        ])
        self.assertEqual([(r.element_id, r.asset_id, r.failed) for r in rows],
                         [("card-1", "projects/p/assets/a", True), ("card-3", "", True)])
        self.assertIsNone(normalize_browser_upload_rows(None))
        self.assertEqual(normalize_browser_upload_rows([]), [])

    def test_confirmed_new_failure_is_released_but_other_assets_are_not(self):
        result = current_transfer_failures([self.row(), self.row(asset="projects/other/assets/a")],
                                          ["projects/p/assets/a", "projects/p/assets/a_extra"],
                                          {"projects/p/assets/a": set(), "projects/p/assets/a_extra": set()})
        self.assertEqual(list(result), ["projects/p/assets/a"])

    def test_old_failure_is_ignored_for_retried_asset(self):
        asset = "projects/p/assets/a"
        rows = [self.row("old"), self.row("current", failed=False)]
        self.assertEqual(current_transfer_failures(rows, [asset], {asset: {"old"}}), {})
        rows[1] = self.row("current", failed=True)
        self.assertEqual(list(current_transfer_failures(rows, [asset], {asset: {"old"}})), [asset])

    def test_unknown_baseline_and_ambiguous_active_card_remain_pending(self):
        asset = "projects/p/assets/a"
        self.assertEqual(current_transfer_failures([self.row()], [asset], {}), {})
        self.assertEqual(current_transfer_failures([self.row()], [asset], {asset: None}), {})
        self.assertEqual(current_transfer_failures([self.row(), self.row("active", failed=False)],
                                                  [asset], {asset: set()}), {})

    def test_collapsed_active_card_with_same_name_prevents_false_failure(self):
        asset = "projects/p/assets/a"
        rows = [self.row(), BrowserUploadRow("new-active", "", False, "Uploading", "a")]
        self.assertEqual(current_transfer_failures(rows, [asset], {asset: set()}), {})

    def make_uploader(self, root):
        fixture = fixtures.UploaderDialogRegressionTests()
        uploader = fixture.uploader(fixture.uploader_config(root, ee_sync=True))
        local = uploader.config.input_folder / "SWOT_L2_HR_Raster_100m_UTM34M_N_x_x_x_001_180_MOSA_20240313T034642_20240313T034903_PGD0_01.tif"
        local.write_bytes(b"preserve me")
        item = UploadItem(local_file=local, asset_name=local.stem,
                          asset_id=uploader.config.destination_parent + "/" + local.stem)
        uploader.tracked_items[item.asset_id] = item
        uploader.report.upsert(uploader.make_report_row(item=item, submit_time="2026-11-07T12:00:00Z",
                              detected_task_name="", final_status="SUBMITTED_PENDING_VERIFICATION", error_message=""))
        uploader.browser_upload_baselines[item.asset_id] = set()
        return uploader, item

    def test_transfer_gate_drains_confirmed_failure_without_resubmitting_or_deleting(self):
        with tempfile.TemporaryDirectory() as tmp:
            uploader, item = self.make_uploader(Path(tmp))
            row = self.row(asset=item.asset_id)
            with mock.patch.object(uploader, "refresh_earth_engine_operations"), \
                 mock.patch.object(uploader, "read_browser_upload_rows", return_value=[row]), \
                 mock.patch.object(uploader, "submit_with_retries") as submit, \
                 mock.patch("swotflow.earthengine.uploader.time.sleep"):
                self.assertTrue(uploader.wait_for_browser_upload_capacity(target_pending=0))
            submit.assert_not_called()
            self.assertEqual(uploader.report.get_status(item.asset_id), "ERROR")
            self.assertIn("HTTP error 400", uploader.report.rows[item.asset_id]["error_message"])
            self.assertEqual(item.local_file.read_bytes(), b"preserve me")
            self.assertFalse(uploader.browser_transfer_backlog_stalled)

    def test_remote_acknowledgement_overrides_browser_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            uploader, item = self.make_uploader(Path(tmp))
            uploader.ee_operations_by_asset[item.asset_id] = EarthEngineOperation(
                asset_id=item.asset_id, operation_name="op", state="RUNNING", done=False)
            with mock.patch.object(uploader, "read_browser_upload_rows", return_value=[self.row(asset=item.asset_id)]):
                self.assertEqual(uploader.reconcile_browser_upload_failures(), 0)
            self.assertEqual(uploader.report.get_status(item.asset_id), "SUBMITTED_PENDING_VERIFICATION")

    def test_fail_fast_option_still_stops_after_recording_the_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            uploader, item = self.make_uploader(Path(tmp))
            uploader.config.upload.fail_fast = True
            with mock.patch.object(uploader, "read_browser_upload_rows", return_value=[self.row(asset=item.asset_id)]):
                with self.assertRaisesRegex(RuntimeError, "Fail-fast"):
                    uploader.reconcile_browser_upload_failures()
            self.assertEqual(uploader.report.get_status(item.asset_id), "ERROR")

    def test_ordinary_uploads_continue_after_a_file_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            uploader, first = self.make_uploader(Path(tmp))
            second = UploadItem(local_file=first.local_file, asset_name="second", asset_id="projects/p/assets/second")
            uploader.config.upload.batch_size = 1
            uploader.report.rows[first.asset_id]["final_status"] = "ERROR"
            with mock.patch.object(uploader, "submit_with_retries") as submit, \
                 mock.patch.object(uploader, "wait_for_browser_upload_capacity", return_value=True), \
                 mock.patch.object(uploader, "wait_for_batch_gate"):
                uploader.process_batches([first, second])
            self.assertEqual([c.args[0].asset_id for c in submit.call_args_list], [first.asset_id, second.asset_id])

    def test_recovery_retains_old_uncertainty_but_submits_all_later_batches(self):
        """A settled prior attempt must not drain-block each new recovery batch."""
        with tempfile.TemporaryDirectory() as tmp:
            uploader, prior = self.make_uploader(Path(tmp))
            uploader.config.execution.dry_run = False
            uploader.config.upload.completion_mode = "submit_and_continue"
            uploader.config.upload.batch_size = 1
            uploader.report.rows[prior.asset_id]["final_status"] = "UNKNOWN_AFTER_CLICK"
            original_time = uploader.report.rows[prior.asset_id]["submit_time"]
            # New workers have no DOM baseline for the old attempt.
            uploader.browser_upload_baselines.clear()
            new_items = [UploadItem(prior.local_file, name, "projects/p/assets/" + name)
                         for name in ("second", "third")]
            for item in new_items:
                uploader.tracked_items[item.asset_id] = item

            def submit(item):
                uploader.report.upsert(uploader.make_report_row(
                    item=item, submit_time="2026-11-12T08:00:00Z", detected_task_name="",
                    final_status="SUBMITTED_PENDING_VERIFICATION", error_message=""))

            def acknowledge():
                for item in new_items:
                    if uploader.report.get_status(item.asset_id) == "SUBMITTED_PENDING_VERIFICATION":
                        uploader.ee_operations_by_asset[item.asset_id] = EarthEngineOperation(
                            item.asset_id, "op/" + item.asset_name, "RUNNING", False)

            with (
                mock.patch.object(uploader, "build_upload_plan", return_value=[prior, *new_items]),
                mock.patch.object(uploader, "confirm_before_real_upload"),
                mock.patch.object(uploader, "create_driver", return_value=mock.Mock()),
                mock.patch.object(uploader, "open_earth_engine"),
                mock.patch.object(uploader, "ensure_logged_in"),
                mock.patch.object(uploader, "wait_for_existing_browser_transfers", return_value=True),
                mock.patch.object(uploader, "sync_earth_engine_inventory"),
                mock.patch.object(uploader, "refresh_earth_engine_operations", side_effect=acknowledge),
                mock.patch.object(uploader, "read_browser_upload_rows", return_value=[]),
                mock.patch.object(uploader, "submit_with_retries", side_effect=submit) as submitted,
                mock.patch.object(uploader, "reconcile_batch_without_waiting"),
                mock.patch.object(uploader, "reconcile_submit_and_continue_uploads"),
                mock.patch("swotflow.earthengine.uploader.time.sleep") as sleep,
                mock.patch("swotflow.earthengine.uploader.time.monotonic",
                           side_effect=[0, 1, 2000, 2001, 4000, 4001]),
            ):
                self.assertEqual(uploader._run(), 2)  # Uncertainty remains a warning.
            self.assertEqual([call.args[0] for call in submitted.call_args_list], new_items)
            self.assertEqual(sleep.call_count, 2)  # Only real new transfers were polled.
            self.assertEqual(uploader.pending_browser_upload_asset_ids(), set())
            self.assertFalse(uploader.browser_transfer_backlog_stalled)
            self.assertEqual(uploader.report.get_status(prior.asset_id), "UNKNOWN_AFTER_CLICK")
            self.assertEqual(uploader.report.rows[prior.asset_id]["submit_time"], original_time)
            self.assertTrue(prior.local_file.exists())
            uploader.report.write()
            rows = fixtures.read_project_rows(uploader.config.artifacts.report_csv, "upload_report")
            self.assertEqual(next(row for row in rows if row["asset_id"] == prior.asset_id)
                             ["final_status"], "UNKNOWN_AFTER_CLICK")

    def test_current_uncertain_transfer_still_blocks_and_preserves_browser(self):
        """No timeout exemption for an attempt made by the current worker."""
        with tempfile.TemporaryDirectory() as tmp:
            uploader, item = self.make_uploader(Path(tmp))
            uploader.report.rows[item.asset_id]["final_status"] = "UNKNOWN_AFTER_CLICK"
            with mock.patch("swotflow.earthengine.uploader.time.monotonic", side_effect=[0, 2000]), \
                 mock.patch.object(uploader, "submit_with_retries") as submit:
                self.assertFalse(uploader.wait_for_browser_upload_capacity(target_pending=0))
            self.assertEqual(uploader.pending_browser_upload_asset_ids(), {item.asset_id})
            self.assertTrue(uploader.browser_transfer_backlog_stalled)
            self.assertTrue(item.local_file.exists())
            submit.assert_not_called()

    def test_deferred_prior_attempt_still_accepts_late_cloud_acknowledgement(self):
        """Transfer-capacity exclusion must not remove the item from tracking."""
        with tempfile.TemporaryDirectory() as tmp:
            uploader, item = self.make_uploader(Path(tmp))
            uploader.report.rows[item.asset_id]["final_status"] = "UNKNOWN_AFTER_CLICK"
            uploader.deferred_prior_upload_asset_ids.add(item.asset_id)
            original_time = uploader.report.rows[item.asset_id]["submit_time"]
            uploader.ee_operations_by_asset[item.asset_id] = EarthEngineOperation(
                item.asset_id, "op/late", "RUNNING", False)
            uploader.reconcile_tracked_operations()
            row = uploader.report.rows[item.asset_id]
            self.assertEqual(row["final_status"], "RUNNING")
            self.assertEqual(row["detected_task_name"], "op/late")
            self.assertEqual(row["submit_time"], original_time)
            self.assertTrue(item.local_file.exists())
            self.assertIn(item.asset_id, uploader.tracked_items)
            self.assertEqual(uploader.pending_browser_upload_asset_ids(), set())

    def test_unsettled_existing_transfer_cannot_be_deferred_or_retried(self):
        """The startup browser gate must succeed before excluding old uncertainty."""
        with tempfile.TemporaryDirectory() as tmp:
            uploader, item = self.make_uploader(Path(tmp))
            uploader.config.execution.dry_run = False
            uploader.report.rows[item.asset_id]["final_status"] = "UNKNOWN_AFTER_CLICK"
            with (
                mock.patch.object(uploader, "build_upload_plan", return_value=[item]),
                mock.patch.object(uploader, "confirm_before_real_upload"),
                mock.patch.object(uploader, "create_driver", return_value=mock.Mock()),
                mock.patch.object(uploader, "open_earth_engine"),
                mock.patch.object(uploader, "ensure_logged_in"),
                mock.patch.object(uploader, "wait_for_existing_browser_transfers", return_value=False),
                mock.patch.object(uploader, "process_batches") as process,
            ):
                self.assertEqual(uploader._run(), 2)
            process.assert_not_called()
            self.assertEqual(uploader.deferred_prior_upload_asset_ids, set())
            self.assertEqual(uploader.report.get_status(item.asset_id), "UNKNOWN_AFTER_CLICK")


@unittest.skipUnless(os.environ.get("SWOTFLOW_BROWSER_TESTS") == "1", "Opt-in isolated Chrome DOM test")
class BrowserUploadDOMTests(unittest.TestCase):
    def test_observed_shadow_dom_collapsed_failure_and_new_attempt(self):
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        from swotflow.earthengine.browser_uploads import BROWSER_UPLOAD_ROWS_SCRIPT
        options = Options()
        options.add_argument("--headless=new")
        with tempfile.TemporaryDirectory() as tmp:
            options.add_argument("--user-data-dir=" + tmp)
            driver = webdriver.Chrome(options=options)
            try:
                driver.get("data:text/html,<ee-task-pane></ee-task-pane>")
                driver.execute_script("""
                    const pane=document.querySelector('ee-task-pane').attachShadow({mode:'open'});
                    pane.innerHTML='<div class="upload-task-list"></div>';
                    window.addCard = (failed) => {
                      const card=document.createElement('div');
                      card.className='task legacy type-UPLOAD'+(failed?' failed':'');
                      card.innerHTML='<div class="content info-toggle">a upload</div><span class="toggle-indicator"></span>';
                      card.querySelector('.info-toggle').onclick=()=>{
                        card.querySelector('.toggle-indicator').classList.add('expanded');
                        if(!card.querySelector('.info')) card.insertAdjacentHTML('beforeend',
                          '<div class="info"><div class="assetId">Destination asset: <b>/projects/p/assets/a</b></div>'+ 
                          '<div class="phase">Phase: Failed</div><div class="error">File upload unsuccessful (HTTP error 400.)</div></div>');
                      };
                      pane.querySelector('.upload-task-list').append(card);
                      return card;
                    };
                    window.addCard(true);
                """)
                initial = normalize_browser_upload_rows(driver.execute_script(BROWSER_UPLOAD_ROWS_SCRIPT, False))
                self.assertEqual(len(initial), 1)
                self.assertEqual(initial[0].asset_id, '')  # collapsed, but identity retained
                baseline = {row.element_id for row in initial}
                expanded = normalize_browser_upload_rows(driver.execute_script(BROWSER_UPLOAD_ROWS_SCRIPT, True))
                self.assertEqual(expanded[0].asset_id, 'projects/p/assets/a')
                self.assertEqual(current_transfer_failures(expanded, ['projects/p/assets/a'],
                                                          {'projects/p/assets/a': baseline}), {})
                driver.execute_script('window.addCard(true);')
                after = normalize_browser_upload_rows(driver.execute_script(BROWSER_UPLOAD_ROWS_SCRIPT, True))
                self.assertEqual(len(after), 2)
                self.assertEqual(list(current_transfer_failures(after, ['projects/p/assets/a'],
                                                               {'projects/p/assets/a': baseline})), ['projects/p/assets/a'])
            finally:
                driver.quit()
