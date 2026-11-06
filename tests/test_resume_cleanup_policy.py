"""Cleanup preference changes and ordinary pauses preserve pipeline evidence."""
import json
import tempfile
import unittest
from pathlib import Path
from threading import Event
from unittest import mock

from swotflow.processing import automation as auto
from swotflow.processing.automation_years import child_outcome
from tests import test_yearly_automation as yearly


class ResumeCleanupPolicyTests(unittest.TestCase):
    harness = yearly.YearlyAutomationTests.harness

    def test_stop_is_paused_not_failed_and_later_work_remains_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = yearly.config_at(Path(tmp), utm_tiles=['UTM30R'])
            event, calls = Event(), []
            event.stop_reason = 'Requested from Automation tab'
            with self.harness(calls, stop=event):
                state = auto.run_automation(cfg, preflight_state=yearly.preflight(cfg), stop_event=event)
            self.assertTrue(state.stopped)
            self.assertFalse(state.errors)
            self.assertEqual(state.stop_reason, event.stop_reason)
            gates = [r for r in state.stage_results if r.stage == 'year_gate']
            self.assertEqual([(r.status, r.return_code) for r in gates], [('paused', 0)])
            self.assertEqual([b['status'] for b in state.year_batches], ['complete', 'stopped', 'pending'])

    def test_enabling_cleanup_revisits_completed_year_without_reprocessing(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = yearly.config_at(Path(tmp), utm_tiles=['UTM30R'], cleanup_enabled=False, include_upload=True)
            event, calls = Event(), []
            with self.harness(calls, stop=event):
                first = auto.run_automation(cfg, preflight_state=yearly.preflight(cfg), stop_event=event)
            resumed = auto.load_automation_run_state(first.run_dir)
            resumed.config.cleanup_enabled = True
            calls.clear()
            with self.harness(calls):
                final = auto.run_automation(resumed.config, preflight_state=resumed)
            self.assertEqual([s for y, _, s in calls if y == '2023'],
                             ['raw_cleanup', 'extracted_cleanup', 'mosaic_cleanup'])
            self.assertNotIn(('2024', 'UTM30R', 'download'), calls)
            self.assertIn(('2024', 'UTM30R', 'raw_cleanup'), calls)
            self.assertEqual([b['status'] for b in final.year_batches], ['complete'] * 3)
            self.assertFalse(final.errors)
            child = auto.load_automation_run_state(final.year_batches[0]['run_dir'])
            self.assertEqual(child_outcome(child), 'complete')
            self.assertTrue(child.config.cleanup_enabled)
            # Disabled attempts remain in history, followed by actual cleanup.
            self.assertEqual([r.status for r in child.stage_results if r.stage == 'raw_cleanup'],
                             ['skipped', 'success'])

    def test_cleanup_catchup_can_stop_and_resume_without_repeat_or_science(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = yearly.config_at(Path(tmp), utm_tiles=['UTM30R'], end_date='2023-12-31', cleanup_enabled=False)
            with self.harness([]):
                first = auto.run_automation(cfg, preflight_state=yearly.preflight(cfg))
            first.config.cleanup_enabled = True
            calls, event = [], Event()
            def clean(state, config, tile, stage):
                calls.append(stage)
                event.set()
                return auto.AutomationStageResult(state.run_id, tile, stage, 'success', deleted_files=1)
            with self.harness([]), mock.patch.object(auto, 'execute_cleanup_result', side_effect=clean):
                stopped = auto.run_automation(first.config, preflight_state=first, stop_event=event)
            self.assertTrue(stopped.stopped)
            self.assertEqual(calls, ['raw_cleanup'])
            restored = auto.load_automation_run_state(stopped.run_dir)
            next_calls = []
            with self.harness(next_calls):
                final = auto.run_automation(restored.config, preflight_state=restored)
            self.assertEqual([s for _, _, s in next_calls], ['extracted_cleanup'])
            self.assertFalse(final.stopped)

    def test_disabled_cleanup_never_deletes_on_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = yearly.config_at(Path(tmp), utm_tiles=['UTM30R'], end_date='2023-12-31', cleanup_enabled=False)
            calls = []
            with self.harness(calls):
                first = auto.run_automation(cfg, preflight_state=yearly.preflight(cfg))
                auto.run_automation(cfg, preflight_state=first)
            self.assertFalse(any(s.endswith('_cleanup') for _, _, s in calls))

    def test_legacy_stop_correction_retains_original_failure_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = yearly.preflight(yearly.config_at(Path(tmp)))
            state.stopped = True
            message = "UTM30R / 2024 is stopped; this tile's later years remain pending. Resolve the recorded issue and resume this run."
            state.errors = [message, 'Unrelated real error']
            state.stage_results = [auto.AutomationStageResult(state.run_id, 'UTM30R', 'year_gate',
                'failed', return_code=2, message=message, period_start='2024-01-01', period_end='2024-12-31')]
            auto.write_run_state(state)
            restored = auto.load_automation_run_state(state.run_dir)
            self.assertEqual([r.status for r in restored.stage_results], ['failed', 'paused'])
            self.assertEqual(restored.errors, ['Unrelated real error'])
            auto.write_run_state(restored)
            self.assertEqual(len(auto.load_automation_run_state(state.run_dir).stage_results), 2)

    def test_cleanup_pending_receipt_is_not_completed_when_enabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = yearly.preflight(yearly.config_at(Path(tmp), cleanup_enabled=True))
            state.stage_results = [auto.AutomationStageResult(state.run_id, 'UTM30R', 'raw_cleanup',
                'skipped', message='Cleanup disabled for this automation run.')]
            self.assertNotIn(('UTM30R', 'raw_cleanup'), auto.completed_stage_keys(state))
            state.config.cleanup_enabled = False
            self.assertIn(('UTM30R', 'raw_cleanup'), auto.completed_stage_keys(state))


if __name__ == '__main__':
    unittest.main()
