"""Realistic inventory refresh, mixed-source lineage and cleanup accounting."""
import json
import tempfile
import unittest
from pathlib import Path

from swotflow.earthengine.uploader import ReportManager
from swotflow.earthengine.upload_history import preserve_verified_submission, restore_verified_submissions
from swotflow.project.database import upsert_project_rows
from swotflow.project.insights import collect_project_insights, parse_path_list
from tests import test_project_insights as fixtures

swot_name = fixtures.swot_name
write_csv = fixtures.write_csv


class StatisticsAccountingTests(unittest.TestCase):
    def test_empty_json_paths_are_not_excluded_files(self):
        for value in ('[]', ' [ ] ', '[null, ""]', '[broken', ''):
            self.assertEqual(parse_path_list(value), [])
        self.assertEqual(parse_path_list('["a.tif", "b.tif"]'), ['a.tif', 'b.tif'])
        self.assertEqual(parse_path_list('a.tif;b.tif'), ['a.tif', 'b.tif'])

    def test_inventory_refresh_keeps_submission_in_single_and_bulk_writes(self):
        for bulk in (False, True):
            with self.subTest(bulk=bulk), tempfile.TemporaryDirectory() as tmp:
                manager = ReportManager(Path(tmp) / '00_logs/upload_report.csv')
                submitted = dict(asset_id='projects/p/assets/a', final_status='RUNNING',
                                 submit_time='2026-11-06T12:00:00Z', detected_task_name='op-1')
                manager.upsert(submitted)
                verified = dict(asset_id=submitted['asset_id'], final_status='EE_VERIFIED_EXISTS',
                                submit_time='', detected_task_name='')
                if bulk:
                    manager.upsert_many([verified])
                else:
                    manager.upsert(verified)
                reloaded = ReportManager(manager.report_path)
                reloaded.load()
                row = reloaded.rows[submitted['asset_id']]
                self.assertEqual(row['submit_time'], submitted['submit_time'])
                self.assertEqual(row['detected_task_name'], 'op-1')

    def test_legacy_event_recovery_is_exact_asset_and_does_not_invent_new_attempts(self):
        rows = [dict(asset_id=a, final_status=s, submit_time='', detected_task_name='') for a, s in
                [('a', 'EE_VERIFIED_EXISTS'), ('b', 'EE_VERIFIED_EXISTS'), ('c', 'PLANNED_UPLOAD')]]
        events = [dict(asset_id=a, submit_time='recorded-time', detected_task_name='old-task',
                       event_time='2026-11-06') for a in ('a', 'c')]
        recovered = restore_verified_submissions(rows, events)
        self.assertEqual(recovered[0]['submit_time'], 'recorded-time')
        self.assertEqual(recovered[1]['submit_time'], '')
        self.assertEqual(recovered[2]['detected_task_name'], '')
        self.assertEqual(rows[0]['submit_time'], '')

    def test_distinct_attempts_and_timezone_order_preserve_the_right_evidence(self):
        current = dict(asset_id='a', final_status='EE_VERIFIED_EXISTS', submit_time='new', detected_task_name='')
        old = dict(asset_id='a', submit_time='old', detected_task_name='old-task')
        self.assertEqual(preserve_verified_submission(current, old), current)
        events = [dict(old, event_time='2026-10-25T02:50:00+02:00'),
                  dict(asset_id='a', submit_time='later', detected_task_name='later-task',
                       event_time='2026-10-25T02:40:00+01:00')]
        current['submit_time'] = ''
        self.assertEqual(restore_verified_submissions([current], events)[0]['detected_task_name'], 'later-task')

    def test_mixed_versions_count_assets_once_per_version_and_survive_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = fixtures.ProjectInsightsTests().sample_project(root)
            logs = root / '00_logs'
            # Synthetic output counter 01 must not replace the real source versions.
            mosaic = root / '03_mosaics' / swot_name(scene='MOSA', suffix='.tif')
            mosaic.parent.mkdir(parents=True)
            mosaic.write_bytes(b'fixture')
            sources = [str(root / swot_name(tile=t, counter=c, suffix='.tif')) for t, c in
                       [('UTM34L', '02'), ('UTM34M', '02'), ('UTM34M', '03')]]
            write_csv(logs / 'mosaic_manifest.csv', [dict(
                output_file=str(mosaic), status='MOSAIC_CREATED', output_exists='yes', stale='false',
                input_files=json.dumps(sources), excluded_input_files='[]', excluded_reasons='[]')])
            aid = 'projects/p/assets/mixed'
            write_csv(logs / 'upload_report.csv', [dict(
                asset_id=aid, local_file=str(mosaic), final_status='EE_VERIFIED_EXISTS')])
            upsert_project_rows(logs / 'upload_events.csv', [dict(
                event_id='one', asset_id=aid, new_status='RUNNING', submit_time='2026-11-06T12:00:00Z',
                detected_task_name='op1', event_time='2026-11-06T12:01:00Z')], dataset='upload_events')
            insights = collect_project_insights(config)
            self.assertEqual(insights.metrics['Mosaic source files excluded'], '0')
            self.assertEqual(insights.metrics['Lineage excluded from partial mosaic'], '0')
            self.assertEqual(insights.metrics['Upload rows sent to Earth Engine'], '1')
            self.assertEqual(dict(insights.uploaded_processing_level_counts), {'PGD0_02': 1, 'PGD0_03': 1})
            for level in ('PGD0_02', 'PGD0_03'):
                self.assertIn((level, 0, 0, 0, 0, 1, 1), insights.processing_level_counts)
            mosaic.unlink()
            after = collect_project_insights(config)
            self.assertEqual(after.uploaded_processing_level_counts, insights.uploaded_processing_level_counts)
            self.assertEqual(after.metrics['Upload rows sent to Earth Engine'], '1')
            self.assertEqual(after.metrics['Completed mosaics recorded'], '1')

    def test_deleted_unuploaded_mosaic_is_not_ready_from_stale_manifest_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = fixtures.ProjectInsightsTests().sample_project(root)
            mosaic = root / '03_mosaics' / swot_name(scene='MOSA', suffix='.tif')
            mosaic.parent.mkdir(parents=True)
            mosaic.write_bytes(b'fixture')
            write_csv(root / '00_logs/mosaic_manifest.csv', [dict(output_file=str(mosaic),
                      status='MOSAIC_CREATED', output_exists='yes', stale='false')])
            self.assertEqual(collect_project_insights(config).metrics['Upload-ready mosaics not uploaded/verified'], '1')
            mosaic.unlink()
            insights = collect_project_insights(config)
            self.assertEqual(insights.metrics['Upload-ready mosaics not uploaded/verified'], '0')
            self.assertEqual(insights.metrics['Completed mosaics recorded'], '1')
