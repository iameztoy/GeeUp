"""Recovery must be bounded, resumable and honest about incomplete EE coverage."""
import copy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import unittest
from unittest import mock

from swotflow.processing import automation as engine
from swotflow.processing import upload_recovery as recovery
from swotflow.project.insights import yearly_automation_overview
from swotflow.verification import pipeline
from tests.test_yearly_automation import config_at, preflight
from tests import test_yearly_automation as yearly_fixtures
from tests import test_pipeline_verification as proof_fixtures
from tests import test_browser_upload_recovery as browser_fixtures
from swotflow.earthengine.browser_uploads import BrowserUploadRow
from swotflow.earthengine.uploader import RetryableUIError, UNKNOWN_AFTER_CLICK_STATUS, EarthEngineOperation


class RecoveryRoundTests(unittest.TestCase):
    def exercise(self, root, *, recover=True, stop=None, fail_sync=False):
        cfg = config_at(root, include_upload=True, process_by_year=False)
        state = preflight(cfg)
        unresolved, uploads, calls = [Path('retained.tif')], [], []

        def execute(s, tile, stage, path, *args, **kwargs):
            calls.append(stage)
            status = 'success'
            if stage == 'upload':
                uploads.append(path)
                status = 'warning'
                if recover and len(uploads) == 2:
                    unresolved.clear()
                    status = 'success'
                if stop is not None:
                    stop.set()
            elif fail_sync:
                status = 'failed'
            return engine.AutomationStageResult(s.run_id, tile, stage, status, config_path=str(path))

        with mock.patch.object(engine, 'retryable_upload_candidates', side_effect=lambda *a, **kw: list(unresolved)), \
             mock.patch.object(engine, 'execute_command_result', side_effect=execute), \
             mock.patch.object(recovery.time, 'sleep') as sleep:
            results = recovery.recover_uploads(state, cfg, Path('python'), stop_event=stop)
        loaded = engine.load_automation_run_state(state.run_dir)
        self.assertEqual(len(loaded.stage_results), len(results))
        return state, calls, uploads, unresolved, sleep

    def test_second_recovery_succeeds_with_sync_before_and_after_each_round(self):
        with TemporaryDirectory() as tmp:
            state, calls, uploads, pending, sleep = self.exercise(Path(tmp))
            self.assertEqual(calls, ['ee_sync', 'upload', 'ee_sync'] * 2)
            self.assertFalse(pending)
            self.assertNotEqual(uploads[0], uploads[1])
            sleep.assert_called_once_with(30)

    def test_exhausted_retries_are_bounded_and_logs_are_not_overwritten_on_resume(self):
        with TemporaryDirectory() as tmp:
            first = self.exercise(Path(tmp), recover=False)
            second = self.exercise(Path(tmp), recover=False)
            self.assertEqual(len(first[2]), 2)
            self.assertTrue(first[3])
            self.assertNotEqual(first[2][0], second[2][0])

    def test_stop_after_step_prevents_another_round(self):
        with TemporaryDirectory() as tmp:
            state, calls, *_ = self.exercise(Path(tmp), stop=Event())
            self.assertTrue(state.stopped)
            self.assertEqual(calls, ['ee_sync', 'upload'])

    def test_failed_reconciliation_never_submits(self):
        with TemporaryDirectory() as tmp:
            _state, calls, *_ = self.exercise(Path(tmp), fail_sync=True)
            self.assertEqual(calls, ['ee_sync'])

    def test_sync_discovers_late_success_without_uploading_again(self):
        with TemporaryDirectory() as tmp:
            cfg = config_at(Path(tmp), include_upload=True)
            state = preflight(cfg)
            with mock.patch.object(engine, 'retryable_upload_candidates', side_effect=[[Path('a.tif')], []]), \
                 mock.patch.object(engine, 'execute_command_result', return_value=engine.AutomationStageResult(
                     state.run_id, 'REPAIR', 'ee_sync', 'success')) as execute:
                recovery.recover_uploads(state, cfg, Path('python'))
            self.assertEqual(execute.call_count, 1)
            self.assertEqual(execute.call_args.args[2], 'ee_sync')


class RetainedExceptionTests(unittest.TestCase):
    def setUp(self):
        proof_fixtures.ProofTests.setUp(self)
        self.inventory['assets'] = []
        with mock.patch('swotflow.catalog.history.represented_inputs', return_value=([], [])):
            self.report = pipeline.reconcile(self.snapshot, {'mosaic_manifest': []}, self.inventory, self.store)
        self.row = {'local_file': str(self.mosaic), 'asset_id': self.asset['id'], 'final_status': 'ERROR',
                    'error_message': 'File upload unsuccessful (HTTP 400)'}
        self.base = {'artifacts': {'report_csv': str(self.store.logs / 'upload_report.csv')}}

    def exceptions(self, report=None):
        with mock.patch.object(engine, 'read_project_rows', return_value=[self.row]):
            return recovery.retained_upload_exceptions(report or self.report, self.store, self.base)

    def test_intact_failed_mosaic_is_deferred_without_certifying_ee(self):
        result = self.exceptions()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['sources'], self.mosaic_proof['sources'])
        self.assertIn('not established', result[0]['reason'])
        self.assertFalse(self.report['input_accounting_complete'])
        self.assertFalse(self.report['pixel_verification_complete'])

    def test_changed_or_deleted_mosaic_cannot_be_deferred_as_intact(self):
        self.mosaic.write_bytes(b'bad')
        self.assertEqual(self.exceptions(), [])
        self.mosaic.unlink()
        self.assertEqual(self.exceptions(), [])

    def test_changed_sidecar_cannot_be_deferred(self):
        Path(str(self.mosaic) + '.aux.xml').write_text('changed', encoding='utf-8')
        self.assertEqual(self.exceptions(), [])

    def test_missing_source_unknown_owner_or_stale_catalogue_prevents_deferral(self):
        for change in ({'blockers': ['stale catalogue']},
                       {'rows': [{**self.report['rows'][0], 'state': 'download_missing_or_unverified'}]},
                       {'rows': [{**self.report['rows'][0], 'state': 'duplicate_active_inputs'}]}):
            with self.subTest(change=change):
                self.assertEqual(self.exceptions({**self.report, **change}), [])

    def test_changed_source_checksum_and_duplicate_local_owners_are_not_deferred(self):
        changed = copy.deepcopy(self.report)
        changed['rows'][0]['source_checksum']['hash'] = 'different'
        self.assertEqual(self.exceptions(changed), [])
        with mock.patch.object(engine, 'read_project_rows', return_value=[self.row, self.row]):
            self.assertEqual(recovery.retained_upload_exceptions(self.report, self.store, self.base), [])


class YearContinuationTests(unittest.TestCase):
    def test_deferred_year_continues_cleanup_then_next_year_and_resume_recovers_only_pending_year(self):
        with TemporaryDirectory() as tmp:
            cfg = config_at(Path(tmp), include_upload=True, continue_on_tile_failure=False)
            helper, calls = yearly_fixtures.YearlyAutomationTests(), []
            entry = {'asset_id': 'projects/p/assets/a', 'local_file': 'retained.tif', 'status': 'ERROR'}

            def mark_exception(child):
                if child.config.start_date.startswith('2023') and child.config.utm_tiles == ['UTM30R']:
                    child.deferred_uploads = [entry]
                    child.unresolved_upload_count = 1
                    child.unresolved_upload_tiles = ['UTM30R']

            with helper.harness(calls), mock.patch('swotflow.processing.automation_accounting.verify_window_inputs', side_effect=mark_exception):
                state = engine.run_automation(cfg, preflight_state=preflight(cfg))
            self.assertEqual([b['status'] for b in state.year_batches], ['upload_pending'] + ['complete'] * 5)
            self.assertFalse(state.errors)
            self.assertIn(('2023', 'UTM30R', 'mosaic_cleanup'), calls)
            self.assertIn(('2024', 'UTM30R', 'download'), calls)
            saved = json.loads(state.json_path.read_text(encoding='utf-8'))
            overview = yearly_automation_overview(saved, state.run_dir)
            self.assertEqual(overview['batches_complete'], 5)
            self.assertEqual(overview['batches_upload_pending'], 1)
            self.assertEqual(overview['deferred_uploads'], [{**entry, 'kind': 'retry_pending'}])
            self.assertEqual(overview['tiles_complete'], 1)
            self.assertEqual(overview['status'], 'upload_pending')
            resumed = engine.load_automation_run_state(state.run_dir)
            calls.clear()
            with helper.harness(calls), mock.patch('swotflow.processing.automation_accounting.verify_window_inputs',
                                                  side_effect=lambda child: child.deferred_uploads.clear()):
                result = engine.run_automation(resumed.config, preflight_state=resumed)
            self.assertFalse(any(stage in {'download', 'extract', 'mosaic'} for _y, _t, stage in calls))
            self.assertEqual([b['status'] for b in result.year_batches], ['complete'] * 6)
            self.assertEqual(result.deferred_uploads, [])
            self.assertEqual(result.unresolved_upload_count, 0)


class ActiveTransferTests(unittest.TestCase):
    def setup_browser(self, root):
        uploader, item = browser_fixtures.BrowserUploadRecoveryTests().make_uploader(root)
        uploader.driver = mock.Mock(window_handles=['editor'], current_window_handle='editor',
                                    current_url='https://code.earthengine.google.com/')
        return uploader, item

    def test_reuses_open_editor_without_navigation(self):
        with TemporaryDirectory() as tmp:
            uploader, _ = self.setup_browser(Path(tmp))
            uploader.open_earth_engine()
            uploader.driver.get.assert_not_called()

    def test_recovery_waits_for_active_card_before_refresh(self):
        with TemporaryDirectory() as tmp:
            uploader, item = self.setup_browser(Path(tmp))
            active = BrowserUploadRow('active', item.asset_id, False, 'Uploading')
            # Even an old operation for this same ID does not override a live local card.
            uploader.ee_operations_by_asset[item.asset_id] = EarthEngineOperation(item.asset_id, 'old', 'FAILED', True)
            events = []
            with mock.patch.object(uploader, 'read_browser_upload_rows', side_effect=[[active], []]), \
                 mock.patch('swotflow.earthengine.uploader.time.sleep', side_effect=lambda _: events.append('wait')), \
                 mock.patch.object(uploader, 'navigate_to_earth_engine', side_effect=lambda _: events.append('refresh')), \
                 mock.patch.object(uploader, 'ensure_logged_in'), mock.patch.object(uploader, 'ensure_assets_tab'):
                uploader.recover_ui_state()
            self.assertEqual(events, ['wait', 'refresh'])

    def test_active_or_unreadable_transfers_timeout_without_navigation(self):
        for rows in (None, [BrowserUploadRow('active', '', False, '(30%) upload')]):
            with TemporaryDirectory() as tmp:
                uploader, _ = self.setup_browser(Path(tmp))
                uploader.config.upload.submission_transfer_timeout_minutes = 0
                with mock.patch.object(uploader, 'read_browser_upload_rows', return_value=rows):
                    with self.assertRaises(RetryableUIError):
                        uploader.recover_ui_state()
                uploader.driver.get.assert_not_called()
                uploader.driver.quit.assert_not_called()
                self.assertTrue(uploader.browser_transfer_backlog_stalled)

    def test_guard_failure_detaches_driver_without_closing_browser_or_submitting(self):
        with TemporaryDirectory() as tmp:
            uploader, item = self.setup_browser(Path(tmp))
            driver = uploader.driver
            uploader.config.execution.dry_run = False
            uploader.config.chrome.connection_mode = 'attach'
            with mock.patch.object(uploader, 'build_upload_plan', return_value=[item]), \
                 mock.patch.object(uploader, 'confirm_before_real_upload'), \
                 mock.patch.object(uploader, 'create_driver', return_value=driver), \
                 mock.patch.object(uploader, 'ensure_logged_in'), \
                 mock.patch.object(uploader, 'wait_for_existing_browser_transfers', return_value=False), \
                 mock.patch.object(uploader, 'process_batches') as submit:
                self.assertEqual(uploader._run(), 2)
            submit.assert_not_called()
            driver.quit.assert_not_called()
            driver.service.stop.assert_called_once()

    def test_uncertain_prior_submission_is_not_retried_but_confirmed_file_error_is(self):
        for prior_status in (UNKNOWN_AFTER_CLICK_STATUS, 'ERROR'):
            with TemporaryDirectory() as tmp:
                uploader, item = self.setup_browser(Path(tmp))
                uploader.config.execution.dry_run = False
                uploader.report.rows[item.asset_id]['final_status'] = prior_status
                driver = uploader.driver
                with mock.patch.object(uploader, 'build_upload_plan', return_value=[item]), \
                     mock.patch.object(uploader, 'confirm_before_real_upload'), \
                     mock.patch.object(uploader, 'create_driver', return_value=driver), \
                     mock.patch.object(uploader, 'ensure_logged_in'), \
                     mock.patch.object(uploader, 'read_browser_upload_rows', return_value=[]), \
                     mock.patch.object(uploader, 'sync_earth_engine_inventory'), \
                     mock.patch.object(uploader, 'wait_for_all_tracked_tasks'), \
                     mock.patch.object(uploader, 'process_batches') as submit:
                    self.assertEqual(uploader._run(), 2)
                self.assertEqual(submit.call_args.args[0], [] if prior_status == UNKNOWN_AFTER_CLICK_STATUS else [item])

    def test_late_running_operation_is_not_submitted_twice(self):
        with TemporaryDirectory() as tmp:
            uploader, item = self.setup_browser(Path(tmp))
            uploader.config.execution.dry_run = False
            uploader.report.rows[item.asset_id]['final_status'] = 'ERROR'
            driver = uploader.driver
            uploader.ee_operations_by_asset[item.asset_id] = EarthEngineOperation(item.asset_id, 'live', 'RUNNING', False)
            with mock.patch.object(uploader, 'build_upload_plan', return_value=[item]), \
                 mock.patch.object(uploader, 'confirm_before_real_upload'), \
                 mock.patch.object(uploader, 'create_driver', return_value=driver), \
                 mock.patch.object(uploader, 'ensure_logged_in'), \
                 mock.patch.object(uploader, 'read_browser_upload_rows', return_value=[]), \
                 mock.patch.object(uploader, 'sync_earth_engine_inventory'), \
                 mock.patch.object(uploader, 'wait_for_all_tracked_tasks'), \
                 mock.patch.object(uploader, 'process_batches') as submit:
                uploader._run()
            self.assertEqual(submit.call_args.args[0], [])
            self.assertEqual(uploader.report.get_status(item.asset_id), 'RUNNING')
            self.assertEqual(uploader.report.rows[item.asset_id]['submit_time'], '2026-11-07T12:00:00Z')
