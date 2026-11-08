"""Offline regression checks for durable scheduling and upload interruption."""
from contextlib import ExitStack
from itertools import product
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest import mock

from swotflow.earthengine.uploader import UNKNOWN_AFTER_CLICK_STATUS, UploadItem
from swotflow.processing import automation as engine
from tests import test_yearly_automation as yearly
from tests import test_automatic_upload_recovery as upload_fixtures
from tests import test_uploader_regressions as uploader_fixtures


def crash_upload_attempt(root: Path) -> None:
    """Exit without Python cleanup after the mocked browser accepts a click."""
    uploader, item = upload_fixtures.ActiveTransferTests().setup_browser(root)
    uploader.config.execution.dry_run = False
    uploader.report.upsert(uploader.make_report_row(
        item=item, submit_time='', detected_task_name='', final_status='PLANNED_UPLOAD',
        error_message='Planned for real upload; awaiting submission.'))
    with ExitStack() as stack:
        for method in ('ensure_assets_tab', 'open_image_upload_dialog', 'populate_upload_dialog',
                       'remember_browser_upload_baseline'):
            stack.enter_context(mock.patch.object(uploader, method))
        stack.enter_context(mock.patch.object(uploader, 'submit_upload_dialog', side_effect=lambda _: os._exit(77)))
        uploader.submit_with_retries(item)


class InterruptionRegressionTests(unittest.TestCase):
    def test_process_death_at_upload_click_retains_uncertain_attempt_on_reopen(self) -> None:
        with TemporaryDirectory() as tmp:
            code = ('from pathlib import Path; from tests.test_workflow_resume_matrix import crash_upload_attempt; '
                    'import sys; crash_upload_attempt(Path(sys.argv[1]))')
            killed = subprocess.run([sys.executable, '-c', code, tmp], check=False, capture_output=True, text=True)
            self.assertEqual(killed.returncode, 77, killed.stderr)
            for mode in ('wait_for_ee', 'submit_and_continue'):
                with self.subTest(mode=mode):
                    fixture = uploader_fixtures.UploaderDialogRegressionTests()
                    uploader = fixture.uploader(fixture.uploader_config(Path(tmp), ee_sync=True))
                    row = next(iter(uploader.report.rows.values()))
                    local = Path(row['local_file'])
                    item = UploadItem(local, local.stem, row['asset_id'])
                    uploader.tracked_items[item.asset_id] = item
                    uploader.driver = mock.Mock(window_handles=['editor'], current_window_handle='editor',
                                                current_url='https://code.earthengine.google.com/')
                    self.assertEqual(uploader.report.get_status(item.asset_id), UNKNOWN_AFTER_CLICK_STATUS)
                    uploader.config.execution.dry_run = False
                    uploader.config.upload.completion_mode = mode
                    driver = uploader.driver
                    with ExitStack() as stack:
                        for method in ('confirm_before_real_upload', 'ensure_logged_in', 'sync_earth_engine_inventory',
                                       'wait_for_all_tracked_tasks', 'reconcile_submit_and_continue_uploads'):
                            stack.enter_context(mock.patch.object(uploader, method))
                        stack.enter_context(mock.patch.object(uploader, 'build_upload_plan', return_value=[item]))
                        stack.enter_context(mock.patch.object(uploader, 'create_driver', return_value=driver))
                        stack.enter_context(mock.patch.object(uploader, 'read_browser_upload_rows', return_value=[]))
                        submit = stack.enter_context(mock.patch.object(uploader, 'process_batches'))
                        self.assertEqual(uploader._run(), 2)
                    self.assertEqual(submit.call_args.args[0], [])
                    self.assertTrue(item.local_file.exists())

    def test_successful_ordinary_resume_clears_current_errors_but_retains_failed_history(self) -> None:
        with TemporaryDirectory() as tmp:
            cfg = yearly.config_at(Path(tmp), process_by_year=False)
            cfg.base_config['download']['verified_downloads'] = True
            cfg.base_config['processing']['acquisition_end_date'] = cfg.end_date
            calls = []
            helper = yearly.YearlyAutomationTests()

            def reconcile(state: engine.AutomationRunState, failed: bool) -> None:
                if failed:
                    state.errors.append('UTM30R: reconciliation incomplete: temporary inventory problem')
                engine.append_stage_result(state, engine.AutomationStageResult(
                    state.run_id, 'UTM30R', 'reconciliation', 'failed' if failed else 'success',
                    return_code=2 if failed else 0))

            with helper.harness(calls), \
                 mock.patch('swotflow.verification.pipeline.catalogue_selection', return_value=None), \
                 mock.patch.object(engine, 'reconcile_verified_run', side_effect=lambda s: reconcile(s, True)):
                first = engine.run_automation(cfg, preflight_state=yearly.preflight(cfg))
            resumed = engine.load_automation_run_state(first.run_dir)
            self.assertTrue(resumed.errors)
            with helper.harness(calls), \
                 mock.patch('swotflow.verification.pipeline.catalogue_selection', return_value=None), \
                 mock.patch.object(engine, 'reconcile_verified_run', side_effect=lambda s: reconcile(s, False)):
                result = engine.run_automation(resumed.config, preflight_state=resumed)
            self.assertEqual(result.errors, [])
            self.assertTrue(any(r.stage == 'reconciliation' and r.status == 'failed' for r in result.stage_results))
            self.assertEqual(engine.latest_stage_results_by_key(result)[('UTM30R', 'reconciliation')].status, 'success')

    def test_lost_browser_response_after_click_stays_uncertain_without_in_process_retry(self) -> None:
        from selenium.common.exceptions import WebDriverException
        with TemporaryDirectory() as tmp:
            uploader, item = upload_fixtures.ActiveTransferTests().setup_browser(Path(tmp))
            uploader.config.upload.retry_attempts = 2
            with ExitStack() as stack:
                for method in ('ensure_assets_tab', 'open_image_upload_dialog', 'populate_upload_dialog',
                               'remember_browser_upload_baseline', 'capture_debug_artifacts', 'close_dialog_if_possible'):
                    stack.enter_context(mock.patch.object(uploader, method))
                click = stack.enter_context(mock.patch.object(uploader, 'submit_upload_dialog',
                    side_effect=WebDriverException('response lost after Upload click')))
                recover = stack.enter_context(mock.patch.object(uploader, 'recover_ui_state'))
                stack.enter_context(mock.patch('swotflow.earthengine.uploader.time.sleep'))
                uploader.submit_with_retries(item)
            click.assert_called_once()
            recover.assert_not_called()
            self.assertEqual(uploader.report.get_status(item.asset_id), UNKNOWN_AFTER_CLICK_STATUS)
            self.assertTrue(item.local_file.exists())

    def test_upload_intent_flushes_sqlite_before_click_without_changing_bulk_default(self) -> None:
        from swotflow.earthengine.uploader import SubmissionOutcome
        from swotflow.project.database import ProjectDatabase
        with TemporaryDirectory() as tmp:
            uploader, item = upload_fixtures.ActiveTransferTests().setup_browser(Path(tmp))
            original_connect = ProjectDatabase.connect
            modes = []

            def traced_connect(database):
                connection = original_connect(database)
                def trace(statement):
                    if statement == 'BEGIN IMMEDIATE':
                        modes.append(connection.execute('PRAGMA synchronous').fetchone()[0])
                connection.set_trace_callback(trace)
                return connection

            def accepted(_item):
                self.assertIn(2, modes)  # SQLite FULL; committed before reaching this boundary.
                reopened = type(uploader.report)(uploader.config.artifacts.report_csv)
                reopened.load()
                self.assertEqual(reopened.get_status(item.asset_id), UNKNOWN_AFTER_CLICK_STATUS)
                return SubmissionOutcome(status='SUBMITTED_PENDING_VERIFICATION')

            with ExitStack() as stack:
                stack.enter_context(mock.patch.object(ProjectDatabase, 'connect', traced_connect))
                for method in ('ensure_assets_tab', 'open_image_upload_dialog', 'populate_upload_dialog',
                               'remember_browser_upload_baseline'):
                    stack.enter_context(mock.patch.object(uploader, method))
                click = stack.enter_context(mock.patch.object(uploader, 'submit_upload_dialog', side_effect=accepted))
                uploader.submit_with_retries(item)
            click.assert_called_once()
            self.assertEqual(modes.count(2), 1)
            self.assertIn(1, modes)  # Routine report/history writes remain NORMAL.

    def test_process_launch_failure_returns_failed_stage_and_diagnostic_log(self) -> None:
        with TemporaryDirectory() as tmp:
            state = yearly.preflight(yearly.config_at(Path(tmp), process_by_year=False))
            config_path = state.run_dir / 'UTM30R' / 'automation_config.yaml'
            with mock.patch.object(engine.subprocess, 'Popen', side_effect=FileNotFoundError('worker executable missing')):
                result = engine.execute_command_result(state, 'UTM30R', 'extract', config_path, Path('missing-python'))
            self.assertEqual(result.status, 'failed')
            self.assertNotEqual(result.return_code, 0)
            self.assertIn('worker executable missing', result.message)
            self.assertIn('worker executable missing', Path(result.log_path).read_text(encoding='utf-8'))


def workflow_harness(calls: list[tuple[str, str, str]], *, fail_stage: str = '') -> ExitStack:
    """Run real scheduling/checkpoint code with offline worker boundaries."""
    stack = ExitStack()

    def execute(state, tile, stage, path=None, *_args, **_kwargs):
        calls.append((state.config.start_date[:4], tile, stage))
        failed = stage == fail_stage
        return engine.AutomationStageResult(state.run_id, tile, stage, 'failed' if failed else 'success',
                                             return_code=int(failed), config_path=str(path or ''))

    stack.enter_context(mock.patch.object(engine, '_preflight_automation', side_effect=yearly.preflight))
    stack.enter_context(mock.patch.object(engine, 'execute_download_result',
                                          side_effect=lambda s, t, p, **kw: execute(s, t, 'download', p, **kw)))
    stack.enter_context(mock.patch.object(engine, 'execute_command_result', side_effect=execute))
    stack.enter_context(mock.patch.object(engine, 'execute_cleanup_result',
                                          side_effect=lambda s, c, t, stage: execute(s, t, stage)))
    stack.enter_context(mock.patch.object(engine, 'execute_deferred_upload_repair_if_needed', return_value=[]))
    stack.enter_context(mock.patch.object(engine, 'execute_final_verified_mosaic_cleanup_if_needed', return_value=None))
    stack.enter_context(mock.patch.object(engine, 'retryable_upload_candidates', return_value=[]))
    stack.enter_context(mock.patch.object(engine, 'reconcile_verified_run'))
    stack.enter_context(mock.patch('swotflow.verification.pipeline.catalogue_selection', return_value=None))
    stack.enter_context(mock.patch('swotflow.processing.automation_accounting.verify_window_inputs'))
    stack.enter_context(mock.patch('swotflow.processing.automation_years.refresh_parent_expectations'))
    return stack


def crash_after_stage(root: Path, yearly_mode: bool, stage: str) -> None:
    """Exit after a real JSON stage checkpoint, before normal shutdown writes."""
    cfg = yearly.config_at(root, process_by_year=yearly_mode, include_upload=True,
                          utm_tiles=['UTM30R'], start_date='2023-07-01', end_date='2024-02-10')

    def progress(_tile: str, reported_stage: str, status: str, _message: str) -> None:
        if reported_stage.split('/')[-1] == stage and status == 'success':
            os._exit(78)

    with workflow_harness([]):
        engine.run_automation(cfg, preflight_state=yearly.preflight(cfg), progress_callback=progress)


class WorkflowOptionMatrixTests(unittest.TestCase):
    def test_scheduling_upload_modes_and_independent_cleanup_matrix(self) -> None:
        for annual, upload, mode, intermediates, mosaics, policy, verified in product(
            (False, True), (False, True), ('wait_for_ee', 'submit_and_continue'),
            (False, True), (False, True), ('full_pixels', 'confirmed_ingestion'), (False, True),
        ):
            with self.subTest(annual=annual, upload=upload, mode=mode, intermediates=intermediates,
                              mosaics=mosaics, policy=policy, verified=verified), TemporaryDirectory() as tmp:
                cfg = yearly.config_at(Path(tmp), process_by_year=annual, include_upload=upload,
                                      cleanup_enabled=intermediates, utm_tiles=['UTM30R'],
                                      start_date='2023-07-01', end_date='2024-02-10')
                cfg.base_config['upload']['completion_mode'] = mode
                cfg.base_config['cleanup'] = {'mosaics_enabled': mosaics, 'mosaic_policy': policy}
                cfg.base_config['download']['verified_downloads'] = verified
                calls = []
                with workflow_harness(calls):
                    state = engine.run_automation(cfg, preflight_state=yearly.preflight(cfg))
                expected = ['download', 'duplicates', 'extract']
                if intermediates:
                    expected.append('raw_cleanup')
                expected.append('mosaic')
                if intermediates:
                    expected.append('extracted_cleanup')
                if upload:
                    expected.extend(['upload', 'ee_sync'])
                    if mosaics:
                        expected.append('mosaic_cleanup')
                self.assertEqual([stage for _, _, stage in calls], expected * (2 if annual else 1))
                self.assertFalse(state.stopped)
                self.assertFalse(state.errors)
                if annual:
                    self.assertEqual([b['status'] for b in state.year_batches], ['complete', 'complete'])
                import yaml
                for path in state.run_dir.rglob('automation_config.yaml'):
                    saved = yaml.safe_load(path.read_text(encoding='utf-8'))
                    self.assertEqual(saved['upload']['completion_mode'], mode)
                    self.assertEqual(saved['cleanup'], cfg.base_config['cleanup'])

    def test_resume_rejects_changed_science_source_or_destination_before_checkpoint_write(self) -> None:
        changes = [('extract', 'resampling_alg', 'bilinear'), ('extract', 'target_crs_mode', 'EPSG:6933'),
                   ('mosaic', 'grouping_mode', 'global'), ('mosaic', 'resampling_alg', 'cubic'),
                   ('download', 'product_version_filter', 'all'), ('', 'destination_parent', 'projects/other/assets/new')]
        for annual, verified, (section, key, value) in product((False, True), (False, True), changes):
            with self.subTest(annual=annual, verified=verified, section=section, key=key), TemporaryDirectory() as tmp:
                cfg = yearly.config_at(Path(tmp), process_by_year=annual, utm_tiles=['UTM30R'])
                cfg.base_config['download']['verified_downloads'] = verified
                with workflow_harness([]):
                    first = engine.run_automation(cfg, preflight_state=yearly.preflight(cfg))
                resumed = engine.load_automation_run_state(first.run_dir)
                before = resumed.json_path.read_bytes()
                if section:
                    resumed.config.base_config[section][key] = value
                else:
                    resumed.config.base_config[key] = value
                calls = []
                with workflow_harness(calls), self.assertRaisesRegex(ValueError, 'separate project'):
                    engine.run_automation(resumed.config, preflight_state=resumed)
                self.assertEqual(calls, [])
                self.assertEqual(resumed.json_path.read_bytes(), before)

    def test_resume_allows_worker_upload_and_cleanup_runtime_changes(self) -> None:
        for annual in (False, True):
            with self.subTest(annual=annual), TemporaryDirectory() as tmp:
                cfg = yearly.config_at(Path(tmp), process_by_year=annual, utm_tiles=['UTM30R'],
                                      cleanup_enabled=False, include_upload=True)
                with workflow_harness([]):
                    first = engine.run_automation(cfg, preflight_state=yearly.preflight(cfg))
                resumed = engine.load_automation_run_state(first.run_dir)
                resumed.config.base_config['download']['threads'] = 9
                resumed.config.base_config['extract']['workers'] = 3
                resumed.config.base_config['mosaic']['workers'] = 2
                resumed.config.base_config['upload'].update(completion_mode='wait_for_ee', batch_size=3)
                resumed.config.base_config['cleanup'] = {'mosaics_enabled': True, 'mosaic_policy': 'confirmed_ingestion'}
                resumed.config.cleanup_enabled = True
                calls = []
                with workflow_harness(calls):
                    result = engine.run_automation(resumed.config, preflight_state=resumed)
                self.assertTrue(calls)
                self.assertTrue(all(stage.endswith('_cleanup') for _, _, stage in calls))
                self.assertFalse(result.errors)

    def test_worker_failure_honors_continue_setting_in_both_schedulers(self) -> None:
        for annual, continue_on_failure in product((False, True), repeat=2):
            with self.subTest(annual=annual, continue_on_failure=continue_on_failure), TemporaryDirectory() as tmp:
                cfg = yearly.config_at(Path(tmp), process_by_year=annual, include_upload=True,
                                      continue_on_tile_failure=continue_on_failure)
                calls = []
                with workflow_harness(calls, fail_stage='mosaic'):
                    engine.run_automation(cfg, preflight_state=yearly.preflight(cfg))
                self.assertFalse(any(stage in {'upload', 'ee_sync'} for _, _, stage in calls))
                self.assertEqual({tile for _, tile, _ in calls},
                                 set(cfg.utm_tiles) if continue_on_failure else {'UTM30R'})
                self.assertEqual({year for year, _, _ in calls}, {'2023'})

    def test_actual_process_death_between_stages_resumes_completed_prefix(self) -> None:
        stages = ['download', 'duplicates', 'extract', 'raw_cleanup', 'mosaic',
                  'extracted_cleanup', 'upload', 'ee_sync', 'mosaic_cleanup']
        for annual, stage in product((False, True), stages):
            with self.subTest(annual=annual, stage=stage), TemporaryDirectory() as tmp:
                code = ('from pathlib import Path; from tests.test_workflow_resume_matrix import crash_after_stage; '
                        'import sys; crash_after_stage(Path(sys.argv[1]), sys.argv[2] == "True", sys.argv[3])')
                killed = subprocess.run([sys.executable, '-c', code, tmp, str(annual), stage],
                                        check=False, capture_output=True, text=True)
                self.assertEqual(killed.returncode, 78, killed.stderr)
                state = engine.load_automation_run_state(Path(tmp) / '00_logs' / 'automation_runs' / 'annual')
                calls = []
                with workflow_harness(calls):
                    result = engine.run_automation(state.config, preflight_state=state)
                self.assertEqual([s for year, _, s in calls if year == '2023'], stages[stages.index(stage) + 1:])
                if annual:
                    self.assertEqual([s for year, _, s in calls if year == '2024'], stages)
                    self.assertEqual([b['status'] for b in result.year_batches], ['complete', 'complete'])
                self.assertFalse(result.errors)


if __name__ == '__main__':
    unittest.main()
