"""Selected upload mode must survive scheduling, retries, cleanup and Resume."""
import copy
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import unittest
from unittest import mock

import yaml
from swotflow.processing import automation as engine, upload_recovery as recovery
from swotflow.processing.automation_years import yearly_config, settings_identity
from swotflow.project.insights import yearly_automation_overview, CleanupCandidate
from swotflow.verification import pipeline
from tests import test_yearly_automation as yearly
from tests import test_automatic_upload_recovery as fixtures


class SchedulingTests(unittest.TestCase):
    def test_both_modes_reach_every_year_and_repair_config_without_mutating_parent(self):
        for mode in ('wait_for_ee', 'submit_and_continue'):
            with self.subTest(mode=mode), TemporaryDirectory() as tmp:
                cfg = yearly.config_at(Path(tmp), include_upload=True)
                cfg.base_config['upload']['completion_mode'] = mode
                original = copy.deepcopy(cfg.base_config)
                for start, end in engine.automation_periods(cfg):
                    child = yearly_config(cfg, start, end, cfg.utm_tiles[0])
                    repair = engine.build_upload_repair_config(child.base_config, child.utm_tiles)
                    self.assertEqual(child.base_config['upload']['completion_mode'], mode)
                    self.assertEqual(repair['upload']['completion_mode'], mode)
                self.assertEqual(cfg.base_config, original)

    def test_absent_mode_keeps_uploader_default_instead_of_enabling_throughput(self):
        with TemporaryDirectory() as tmp:
            cfg = yearly.config_at(Path(tmp), include_upload=True)
            cfg.base_config.pop('upload')
            child = yearly_config(cfg, '2024-01-01', '2024-12-31', 'UTM30R')
            self.assertNotIn('completion_mode', child.base_config.get('upload', {}))

    def test_mode_change_on_resume_keeps_scientific_checkpoints_and_reaches_new_configs(self):
        with TemporaryDirectory() as tmp:
            cfg = yearly.config_at(Path(tmp), include_upload=True)
            cfg.base_config['upload']['completion_mode'] = 'wait_for_ee'
            helper, calls, stop = yearly.YearlyAutomationTests(), [], Event()
            with helper.harness(calls, stop=stop):
                first = engine.run_automation(cfg, preflight_state=yearly.preflight(cfg), stop_event=stop)
            resumed = engine.load_automation_run_state(first.run_dir)
            identity = settings_identity(resumed.config)
            resumed.config.base_config['upload']['completion_mode'] = 'submit_and_continue'
            self.assertEqual(settings_identity(resumed.config), identity)
            calls.clear()
            with helper.harness(calls):
                result = engine.run_automation(resumed.config, preflight_state=resumed)
            self.assertFalse(any(y == '2023' and t == 'UTM30R' for y, t, _ in calls))
            self.assertNotIn(('2024', 'UTM30R', 'download'), calls)
            self.assertIn(('2024', 'UTM30R', 'upload'), calls)
            self.assertEqual([b['status'] for b in result.year_batches], ['complete'] * 6)
            # Inspect configs actually written for the resumed child workers.
            configs = list((result.run_dir / 'tiles' / 'UTM30R' / 'years' / '2024').rglob('automation_config.yaml'))
            self.assertTrue(configs)
            for path in configs:
                self.assertEqual(yaml.safe_load(path.read_text())['upload']['completion_mode'], 'submit_and_continue')


class ModeRecoveryTests(unittest.TestCase):
    def exercise(self, status, *, late_status=None, mode='submit_and_continue'):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        cfg = yearly.config_at(root, include_upload=True, process_by_year=False)
        cfg.base_config['upload']['completion_mode'] = mode
        path = root / 'SWOT_L2_HR_Raster_100m_UTM30R_N_x_x_x_012_180_MOSA_20240313T034642_20240313T034903_PGD0_01.tif'
        path.write_bytes(b'fixture')
        row = {'local_file': str(path), 'final_status': status}
        state = yearly.preflight(cfg)
        calls = []

        def execute(s, tile, stage, config_path, *_args, **_kwargs):
            calls.append(stage)
            self.assertEqual(yaml.safe_load(config_path.read_text())['upload']['completion_mode'], mode)
            if stage == 'ee_sync' and late_status:
                row['final_status'] = late_status
            return engine.AutomationStageResult(s.run_id, tile, stage, 'success')

        with mock.patch.object(engine, 'read_project_rows', return_value=[row]), \
             mock.patch.object(engine, 'execute_command_result', side_effect=execute), \
             mock.patch.object(recovery.time, 'sleep') as sleep:
            recovery.recover_uploads(state, cfg, Path('python'))
        return calls, sleep

    def test_throughput_does_not_retry_healthy_or_uncertain_submissions(self):
        for status in ('SUBMITTED', 'SUBMITTED_PENDING_VERIFICATION', 'READY', 'RUNNING', 'UNKNOWN_AFTER_CLICK'):
            with self.subTest(status=status):
                calls, sleep = self.exercise(status)
                self.assertEqual(calls, [])
                sleep.assert_not_called()

    def test_throughput_still_recovers_confirmed_failures_with_two_bounded_rounds(self):
        calls, sleep = self.exercise('ERROR')
        self.assertEqual(calls, ['ee_sync', 'upload', 'ee_sync'] * 2)
        sleep.assert_called_once_with(recovery.RECOVERY_DELAY_SECONDS)

    def test_late_running_or_verified_status_prevents_another_uploader(self):
        for status in ('RUNNING', 'EE_VERIFIED_EXISTS', 'UNKNOWN_AFTER_CLICK'):
            with self.subTest(status=status):
                calls, _ = self.exercise('ERROR', late_status=status)
                self.assertEqual(calls, ['ee_sync'])

    def test_wait_mode_retains_its_pending_confirmation_recovery(self):
        calls, _ = self.exercise('SUBMITTED_PENDING_VERIFICATION', mode='wait_for_ee')
        self.assertEqual(calls, ['ee_sync', 'upload', 'ee_sync'] * 2)


class UploaderGateTests(unittest.TestCase):
    def test_throughput_drains_local_transfers_but_does_not_wait_for_ee_ingestion(self):
        for mode in ('wait_for_ee', 'submit_and_continue'):
            with self.subTest(mode=mode), TemporaryDirectory() as tmp:
                uploader, item = fixtures.ActiveTransferTests().setup_browser(Path(tmp))
                uploader.config.upload.completion_mode = mode
                with mock.patch.object(uploader, 'submit_with_retries'), \
                     mock.patch.object(uploader, 'pending_browser_upload_asset_ids', return_value=[]), \
                     mock.patch.object(uploader, 'wait_for_browser_upload_capacity', return_value=True) as transfer, \
                     mock.patch.object(uploader, 'wait_for_batch_gate') as cloud_wait, \
                     mock.patch.object(uploader, 'reconcile_batch_without_waiting') as sync:
                    uploader.process_batches([item])
                transfer.assert_called_once_with(target_pending=0)
                self.assertEqual(cloud_wait.call_count, int(mode == 'wait_for_ee'))
                self.assertEqual(sync.call_count, int(mode == 'submit_and_continue'))


class PendingEvidenceTests(unittest.TestCase):
    def setUp(self):
        fixtures.RetainedExceptionTests.setUp(self)

    def test_running_task_retains_mosaic_and_source_history_without_becoming_complete(self):
        self.row['final_status'] = 'RUNNING'
        with mock.patch.object(engine, 'read_project_rows', return_value=[self.row]):
            entries = recovery.retained_upload_exceptions(self.report, self.store, self.base)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]['kind'], 'verification_pending')
        self.assertIn('not an upload failure', entries[0]['reason'])
        self.assertFalse(self.report['input_accounting_complete'])
        # Same real cleanup guard for both modes; no cloud pixel proof => retain.
        for mode in ('wait_for_ee', 'submit_and_continue'):
            config = {**self.base, 'processing': {'logs': str(self.store.logs)},
                      'download': {'verified_downloads': True, 'manifest_csv': str(self.store.logs / 'download_manifest.csv')},
                      'destination_parent': self.inventory['collection'],
                      'upload': {'completion_mode': mode}}
            pipeline.save_inventory(self.store, self.inventory)
            candidates = [CleanupCandidate('mosaic', self.mosaic, 'submitted', 'fixture')]
            self.assertEqual(pipeline.cleanup_guards(config, candidates), [])
        self.assertTrue(self.mosaic.exists())
        self.assertEqual(self.store.artifact(self.mosaic)['sources'], self.mosaic_proof['sources'])

    def test_pending_year_continues_and_resume_reconciles_without_repeating_scientific_work(self):
        with TemporaryDirectory() as tmp:
            cfg = yearly.config_at(Path(tmp), include_upload=True, continue_on_tile_failure=False)
            helper, calls = yearly.YearlyAutomationTests(), []
            entry = {'asset_id': self.asset['id'], 'local_file': str(self.mosaic), 'status': 'RUNNING'}

            def pending(child):
                if child.config.start_date.startswith('2023') and child.config.utm_tiles == ['UTM30R']:
                    child.deferred_uploads = [entry]
                    child.unresolved_upload_count = 1

            with helper.harness(calls), mock.patch('swotflow.processing.automation_accounting.verify_window_inputs', side_effect=pending):
                result = engine.run_automation(cfg, preflight_state=yearly.preflight(cfg))
            self.assertEqual([b['status'] for b in result.year_batches], ['upload_pending'] + ['complete'] * 5)
            self.assertIn(('2024', 'UTM30R', 'download'), calls)
            self.assertIn(('2023', 'UTM30R', 'mosaic_cleanup'), calls)
            resumed = engine.load_automation_run_state(result.run_dir)
            overview = yearly_automation_overview(__import__('json').loads(resumed.json_path.read_text()), result.run_dir)
            self.assertEqual(overview['uploads_verification_pending'], 1)
            self.assertEqual(overview['uploads_retry_pending'], 0)
            self.assertEqual(overview['batches_complete'], 5)
            self.assertEqual(overview['tiles_complete'], 1)
            calls.clear()
            with helper.harness(calls), mock.patch('swotflow.processing.automation_accounting.verify_window_inputs',
                                                  side_effect=lambda child: child.deferred_uploads.clear()):
                done = engine.run_automation(resumed.config, preflight_state=resumed)
            self.assertFalse(any(stage in {'download', 'extract', 'mosaic'} for _, _, stage in calls))
            self.assertEqual(done.unresolved_upload_count, 0)
            self.assertFalse(done.deferred_uploads)
            self.assertEqual([b['status'] for b in done.year_batches], ['complete'] * 6)
