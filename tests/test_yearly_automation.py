"""Regression checks for optional year scheduling, resume and storage isolation."""
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from types import SimpleNamespace
import unittest
from unittest import mock

import yaml
from swotflow.processing import automation as auto
from swotflow.processing.automation_years import child_outcome, storage_reserve_failure, yearly_config


def config_at(root: Path, **options) -> auto.AutomationConfig:
    logs = root / '00_logs'
    base = {'processing': {'root': str(root), 'logs': str(logs),
                          'raw_downloads': str(root / 'raw'), 'extracted_geotiffs': str(root / 'extract'),
                          'mosaics': str(root / 'mosaics')},
            'download': {'manifest_csv': str(logs / 'download_manifest.csv')},
            'extract': {'manifest_csv': str(logs / 'extract_manifest.csv'), 'year_selection': '2021'},
            'mosaic': {'manifest_csv': str(logs / 'mosaic_manifest.csv')},
            'artifacts': {'report_csv': str(logs / 'upload_report.csv')},
            'gdal': {'python': __import__('sys').executable},
            'upload': {'completion_mode': 'submit_and_continue'},
            'destination_parent': 'projects/example/assets/test'}
    kwargs = dict(project_root=root, base_config=base, utm_tiles=['UTM30R', 'UTM31S'],
                  start_date='2023-07-01', end_date='2025-02-10', process_by_year=True,
                  run_id='annual', run_dir=logs / 'automation_runs' / 'annual',
                  min_free_space_gb=0, prevent_system_sleep=False)
    kwargs.update(options)
    return auto.normalize_automation_config(auto.AutomationConfig(**kwargs))


def preflight(config, *_args):
    state = auto.ensure_run_state(config)
    state.preflight_ok = True
    state.tile_plans = [auto.AutomationTilePlan(t, 'new', '') for t in config.utm_tiles]
    return state


class YearlyAutomationTests(unittest.TestCase):
    def harness(self, calls, stop=None, fail_year=None, upload_unresolved=False):
        stack = ExitStack()
        def execute(state, tile, stage, path=None, *_args, **_kwargs):
            calls.append((state.config.start_date[:4], tile, stage))
            if path and Path(path).exists():
                cfg = yaml.safe_load(Path(path).read_text(encoding='utf-8'))
                self.assertEqual(cfg['download']['start_date'], state.config.start_date)
                self.assertEqual(cfg['processing']['acquisition_end_date'], state.config.end_date)
            status = 'failed' if fail_year == state.config.start_date[:4] and stage == 'mosaic' else 'success'
            if stop is not None and state.config.start_date.startswith('2024') and stage == 'extract':
                stop.set()
            return auto.AutomationStageResult(state.run_id, tile, stage, status, return_code=int(status == 'failed'))
        stack.enter_context(mock.patch.object(auto, '_preflight_automation', side_effect=preflight))
        stack.enter_context(mock.patch.object(auto, 'execute_download_result',
            side_effect=lambda s, t, p, **kw: execute(s, t, 'download', p, **kw)))
        stack.enter_context(mock.patch.object(auto, 'execute_command_result', side_effect=execute))
        stack.enter_context(mock.patch.object(auto, 'execute_cleanup_result',
            side_effect=lambda s, cfg, t, stage: execute(s, t, stage)))
        stack.enter_context(mock.patch.object(auto, 'execute_deferred_upload_repair_if_needed', return_value=[]))
        stack.enter_context(mock.patch.object(auto, 'execute_final_verified_mosaic_cleanup_if_needed', return_value=None))
        stack.enter_context(mock.patch.object(auto, 'retryable_upload_candidates', return_value=[Path('pending.tif')] if upload_unresolved else []))
        stack.enter_context(mock.patch('swotflow.processing.automation_accounting.verify_window_inputs'))
        stack.enter_context(mock.patch('swotflow.processing.automation_years.refresh_parent_expectations'))
        return stack

    def test_periods_clip_first_last_year_and_off_preserves_original(self):
        with TemporaryDirectory() as tmp:
            cfg = config_at(Path(tmp))
            self.assertEqual(auto.automation_periods(cfg), [('2023-07-01','2023-12-31'),
                             ('2024-01-01','2024-12-31'),('2025-01-01','2025-02-10')])
            cfg.process_by_year = False
            self.assertEqual(auto.automation_periods(cfg), [('2023-07-01','2025-02-10')])

    def test_tile_first_order_shared_manifests_and_explicit_upload_wait(self):
        with TemporaryDirectory() as tmp:
            cfg = config_at(Path(tmp), include_upload=True)
            child = yearly_config(cfg, '2024-01-01', '2024-12-31', 'UTM30R')
            self.assertEqual(child.utm_tiles, ['UTM30R'])
            self.assertEqual(child.base_config['download']['manifest_csv'], cfg.base_config['download']['manifest_csv'])
            self.assertEqual(child.base_config['processing']['raw_downloads'], cfg.base_config['processing']['raw_downloads'])
            self.assertEqual(child.base_config['upload']['completion_mode'], 'wait_for_ee')
            self.assertEqual(child.base_config['extract']['year_selection'], 'all')
            self.assertEqual(cfg.base_config['upload']['completion_mode'], 'submit_and_continue')
            calls = []
            with self.harness(calls):
                state = auto.run_automation(cfg, preflight_state=preflight(cfg))
            self.assertEqual([v[:2] for v in calls if v[2] == 'download'],
                             [(str(y), t) for t in cfg.utm_tiles for y in (2023,2024,2025)])
            self.assertEqual([b['status'] for b in state.year_batches], ['complete'] * 6)
            self.assertEqual({r.period_start[:4] for r in state.stage_results}, {'2023','2024','2025'})
            self.assertEqual(len(state.tile_plans), 2)
            loaded = auto.load_automation_run_state(state.run_dir)
            self.assertTrue(loaded.config.process_by_year)
            self.assertEqual(loaded.config.min_free_space_gb, 0)
            self.assertEqual(loaded.year_batches, state.year_batches)

    def test_interruption_resume_keeps_completed_year_and_resumes_remaining_stages(self):
        with TemporaryDirectory() as tmp:
            cfg = config_at(Path(tmp))
            stop, calls = Event(), []
            with self.harness(calls, stop=stop):
                state = auto.run_automation(cfg, preflight_state=preflight(cfg), stop_event=stop)
            self.assertEqual([b['status'] for b in state.year_batches], ['complete','stopped'] + ['pending'] * 4)
            resumed = auto.load_automation_run_state(state.run_dir)
            calls.clear()
            with self.harness(calls):
                result = auto.run_automation(resumed.config, preflight_state=resumed)
            self.assertFalse(any(year == '2023' and tile == 'UTM30R' for year, tile, _ in calls))
            self.assertIn(('2023','UTM31S','download'), calls)
            self.assertNotIn(('2024','UTM30R','download'), calls)
            self.assertIn(('2024','UTM30R','mosaic'), calls)
            self.assertEqual([b['status'] for b in result.year_batches], ['complete'] * 6)
            self.assertFalse(result.errors)

    def test_failed_year_does_not_start_later_year_or_hide_other_tiles(self):
        with TemporaryDirectory() as tmp:
            cfg, calls = config_at(Path(tmp)), []
            with self.harness(calls, fail_year='2023'):
                state = auto.run_automation(cfg, preflight_state=preflight(cfg))
            self.assertEqual({y for y, _, _ in calls}, {'2023'})
            self.assertEqual({t for _, t, _ in calls}, set(cfg.utm_tiles))
            self.assertEqual([b['status'] for b in state.year_batches], ['incomplete','pending','pending'] * 2)
            self.assertTrue(state.errors)

    def test_low_space_pauses_before_download_and_keeps_checkpoint(self):
        with TemporaryDirectory() as tmp:
            cfg, calls = config_at(Path(tmp), min_free_space_gb=50), []
            with self.harness(calls), mock.patch('swotflow.processing.automation_years.shutil.disk_usage',
                                               return_value=SimpleNamespace(free=1)):
                state = auto.run_automation(cfg, preflight_state=preflight(cfg))
            self.assertEqual(calls, [])
            self.assertEqual([b['status'] for b in state.year_batches], ['low_space'] + ['pending'] * 5)
            self.assertTrue(state.stopped)
            self.assertTrue(state.json_path.exists())

    def test_storage_guard_checks_destination_volume_and_allows_cleanup(self):
        with TemporaryDirectory() as tmp:
            cfg = config_at(Path(tmp), min_free_space_gb=20)
            state = preflight(cfg)
            destination = Path(tmp) / 'other_volume'
            destination.mkdir()
            tile_cfg = {'extract': {'output_folder': str(destination / 'future_folder')}}
            with mock.patch('swotflow.processing.automation_years.shutil.disk_usage',
                            return_value=SimpleNamespace(free=0)) as usage:
                self.assertEqual(storage_reserve_failure(state, tile_cfg, 'UTM30R', 'extract').return_code,75)
                usage.assert_called_once_with(destination)
                self.assertIsNone(storage_reserve_failure(state, tile_cfg, 'UTM30R','raw_cleanup'))

    def test_pending_upload_stops_at_year_boundary(self):
        with TemporaryDirectory() as tmp:
            cfg, calls = config_at(Path(tmp), include_upload=True), []
            with self.harness(calls, upload_unresolved=True):
                state = auto.run_automation(cfg, preflight_state=preflight(cfg))
            self.assertEqual(state.year_batches[0]['status'], 'incomplete')
            self.assertEqual(state.unresolved_upload_count, 2)
            self.assertFalse(any(y == '2024' for y, _, _ in calls))

    def test_old_checkpoint_defaults_and_incomplete_empty_run(self):
        with TemporaryDirectory() as tmp:
            cfg = config_at(Path(tmp), process_by_year=False)
            state = preflight(cfg)
            auto.write_run_state(state)
            import json
            data = json.loads(state.json_path.read_text(encoding='utf-8'))
            del data['config']['process_by_year']
            del data['year_batches']
            state.json_path.write_text(json.dumps(data), encoding='utf-8')
            loaded = auto.load_automation_run_state(state.run_dir)
            self.assertFalse(loaded.config.process_by_year)
            self.assertEqual(loaded.year_batches, [])
            self.assertEqual(child_outcome(loaded), 'incomplete')

    def test_completed_checkpoint_rejects_changed_scientific_settings(self):
        with TemporaryDirectory() as tmp:
            cfg, calls = config_at(Path(tmp)), []
            with self.harness(calls):
                state = auto.run_automation(cfg, preflight_state=preflight(cfg))
            resumed = auto.load_automation_run_state(state.run_dir)
            resumed.config.base_config['mosaic']['target_crs'] = 'EPSG:6933'
            calls.clear()
            with self.harness(calls), self.assertRaisesRegex(ValueError, 'Saved annual scope'):
                auto.run_automation(resumed.config, preflight_state=resumed)
            self.assertEqual(calls, [])

    def test_partial_processing_limits_rejected_only_for_yearly_mode(self):
        with TemporaryDirectory() as tmp:
            for section, key in [('download', 'max_granules'), ('extract', 'limit_files')]:
                cfg = config_at(Path(tmp))
                cfg.base_config[section][key] = 1
                with self.assertRaisesRegex(ValueError, key):
                    auto.normalize_automation_config(cfg)
                cfg.process_by_year = False
                self.assertEqual(auto.normalize_automation_config(cfg).base_config[section][key], 1)

    def test_interrupted_checkpoint_write_preserves_last_readable_state(self):
        with TemporaryDirectory() as tmp:
            state = preflight(config_at(Path(tmp)))
            auto.write_run_state(state)
            before = state.json_path.read_bytes()
            state.errors.append('unsaved change')
            with mock.patch.object(auto.os, 'replace', side_effect=OSError('disk unavailable')):
                with self.assertRaises(OSError):
                    auto.write_run_state(state)
            self.assertEqual(state.json_path.read_bytes(), before)
            self.assertFalse(list(state.run_dir.glob('automation_run_*.tmp')))
            self.assertFalse(auto.load_automation_run_state(state.run_dir).errors)


if __name__ == '__main__':
    unittest.main()
