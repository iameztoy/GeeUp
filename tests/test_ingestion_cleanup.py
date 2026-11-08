"""Opt-in provenance cleanup retains uncertain data and preserves operational reuse."""
import copy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest import mock

from swotflow.processing import automation as auto
from swotflow.project.insights import CleanupCandidate, collect_project_insights, delete_cleanup_candidates
from swotflow.verification import pipeline as p
from swotflow.verification.retention import mosaic_cleanup_policy, CONFIRMED_INGESTION
from swotflow.verification.resume import verified_resume_stages
from tests import test_pipeline_verification as fixtures
from tests import test_resume_cleanup_lineage as resume_fixtures
from tests.test_project_insights import write_csv
from tests import test_yearly_automation as yearly


class IngestionCleanupTests(unittest.TestCase):
    def setUp(self):
        fixtures.ProofTests.setUp(self)
        self.base = resume_fixtures.ResumeCleanupTests.config_and_selection(self)
        self.base['cleanup'] = {'mosaic_policy': CONFIRMED_INGESTION, 'mosaics_enabled': True}
        self.base['destination_parent'] = self.inventory['collection']
        self.base['artifacts']['report_csv'] = str(self.store.logs / 'upload_report.csv')
        self.mosaic_proof = self.store.artifact(self.mosaic)
        self.ingestion = self.store.prepare_ingestion(self.asset['id'], self.mosaic_proof)
        self.store.record_ingestion({**self.ingestion, 'phase': 'submitted', 'task_id': 'fixture-task'})
        self.asset['properties'] = p.ingestion_properties(self.ingestion)
        p.save_inventory(self.store, self.inventory)
        self.upload('EE_VERIFIED_EXISTS')

    def upload(self, status, *, export_csv=True):
        from swotflow.project.database import upsert_project_rows
        upsert_project_rows(Path(self.base['artifacts']['report_csv']), [{'asset_id': self.asset['id'],
            'local_file': str(self.mosaic), 'final_status': status, 'source_utm_tiles': '["UTM30R"]'}],
            dataset='upload_report', export_csv=export_csv)

    def candidates(self):
        return p.cleanup_guards(self.base, [CleanupCandidate('mosaic', self.mosaic, 'uploaded', 'fixture')])

    def test_default_is_strict_and_ingestion_does_not_certify_pixels(self):
        self.assertEqual(mosaic_cleanup_policy({}), 'full_pixels')
        with self.assertRaises(ValueError):
            mosaic_cleanup_policy({'cleanup': {'mosaic_policy': 'anything'}})
        strict = {**self.base, 'cleanup': {'mosaic_policy': 'full_pixels'}}
        self.assertEqual(p.cleanup_guards(strict, [CleanupCandidate('mosaic', self.mosaic, '', '')]), [])
        self.assertEqual(p.active_proofs(self.store, self.inventory['collection']), {})
        self.assertEqual(len(self.candidates()), 1)
        self.assertEqual(self.store.cloud_receipts(), {})

    def test_confirmed_mosaic_deleted_without_erasing_receipts_or_redownloading_sources(self):
        retained = self.store.artifact(self.mosaic)
        candidates = self.candidates()
        self.assertEqual(delete_cleanup_candidates(candidates)[::2], (1, []))
        self.assertFalse(self.mosaic.exists())
        self.assertEqual(self.store.artifact(self.mosaic), retained)
        self.assertTrue(p.download_proof(self.store.logs, self.name)['valid'])
        self.assertEqual(p.reusable_download_groups([self.g], self.store, self.inventory['collection']), {self.name})
        self.raw.unlink(); self.extract.unlink()
        completed = {'download', 'duplicates', 'extract', 'raw_cleanup', 'mosaic', 'extracted_cleanup'}
        for policy in ('confirmed_ingestion', 'full_pixels'):
            self.base['cleanup']['mosaic_policy'] = policy
            self.assertEqual(verified_resume_stages(self.base, self.base, completed, self.root)[0], completed)
        self.asset['updateTime'] = 'changed revision'
        p.save_inventory(self.store, self.inventory)
        self.assertEqual(verified_resume_stages(self.base, self.base, completed, self.root)[0], set())
        self.assertEqual(p.reusable_download_groups([self.g], self.store, self.inventory['collection']), set())

    def test_pending_failed_unknown_and_missing_upload_records_retain_file(self):
        for status in ('SUBMITTED', 'SUBMITTED_PENDING_VERIFICATION', 'RUNNING', 'READY', 'ERROR', 'FAILED', 'UNKNOWN_AFTER_CLICK', ''):
            with self.subTest(status=status):
                self.upload(status)
                self.assertEqual(self.candidates(), [])
                self.assertTrue(self.mosaic.exists())

    def test_asset_presence_wrong_revision_or_unconfirmed_history_cannot_authorize_deletion(self):
        for phase in ('prepared', 'submitted', 'failed'):
            self.store.record_ingestion({**self.ingestion, 'phase': phase})
            self.assertEqual(self.candidates(), [])
        self.store.record_ingestion({**self.ingestion, 'phase': 'submitted'})
        p.save_inventory(self.store, self.inventory)
        self.assertEqual(len(self.candidates()), 1)
        self.asset['properties']['swotflow_sha256'] = 'different'
        p.save_inventory(self.store, self.inventory)
        self.assertEqual(self.candidates(), [])

    def test_changed_bytes_sidecar_or_source_provenance_retain_file(self):
        self.mosaic.write_bytes(b'changed')
        self.assertEqual(self.candidates(), [])
        self.mosaic.write_bytes(b'mosaic')
        sidecar = Path(str(self.mosaic) + '.aux.xml')
        sidecar.write_text('changed mask')
        self.assertEqual(self.candidates(), [])
        sidecar.unlink()
        row = self.store.ingestions()[self.asset['id']]
        row['sources'] = {self.name: {**self.mosaic_proof['sources'][self.name], 'hash': 'changed'}}
        self.store.record_ingestion(row)
        self.assertEqual(self.candidates(), [])

    def test_known_pixel_failure_stale_inventory_and_duplicate_owners_block_cleanup(self):
        self.store.record_cloud({**self.receipt, 'passed': False,
            'asset_fingerprint': p.digest(__import__('swotflow.consolidation.core', fromlist=['snapshot_asset']).snapshot_asset(self.asset))})
        self.assertEqual(self.candidates(), [])
        # A historical failure for another revision does not taint this one.
        self.store.record_cloud({**self.receipt, 'passed': False, 'asset_fingerprint': 'old revision'})
        self.assertEqual(len(self.candidates()), 1)
        self.inventory['finished_utc'] = '2020-01-01T00:00:00Z'
        p.save_inventory(self.store, self.inventory)
        self.assertEqual(self.candidates(), [])
        self.inventory['finished_utc'] = p.utc_now()
        duplicate = copy.deepcopy(self.asset); duplicate['id'] += '_duplicate'
        row = self.store.prepare_ingestion(duplicate['id'], self.mosaic_proof)
        self.store.record_ingestion({**row, 'phase': 'submitted'})
        duplicate['properties'] = p.ingestion_properties(row)
        self.inventory['assets'].append(duplicate)
        p.save_inventory(self.store, self.inventory)
        self.assertEqual(self.candidates(), [])

    def test_delete_rechecks_current_status_and_metadata_even_with_cache(self):
        guard = self.candidates()[0].verification_guard
        cache = {}
        self.assertTrue(p.check_cleanup_guard(guard, evidence_cache=cache))
        self.upload('RUNNING', export_csv=False)
        self.assertFalse(p.check_cleanup_guard(guard, evidence_cache=cache))
        self.upload('EE_VERIFIED_EXISTS')
        self.assertTrue(p.check_cleanup_guard(guard, evidence_cache=cache))
        self.inventory['assets'] = []
        p.save_inventory(self.store, self.inventory)
        count, _, errors = delete_cleanup_candidates([CleanupCandidate('mosaic', self.mosaic, '', '', verification_guard=guard)])
        self.assertEqual(count, 0); self.assertTrue(errors); self.assertTrue(self.mosaic.exists())

    def test_cache_tracks_custom_upload_database_and_ambiguous_destination(self):
        from swotflow.project.database import upsert_project_rows
        self.base['artifacts']['report_csv'] = str(self.root / 'custom_history' / 'uploads.csv')
        self.upload('EE_VERIFIED_EXISTS')
        guard = self.candidates()[0].verification_guard
        cache = {}
        self.assertTrue(p.check_cleanup_guard(guard, evidence_cache=cache))
        self.upload('FAILED', export_csv=False)
        self.assertFalse(p.check_cleanup_guard(guard, evidence_cache=cache))
        self.upload('EE_VERIFIED_EXISTS', export_csv=False)
        self.assertTrue(p.check_cleanup_guard(guard, evidence_cache=cache))
        upsert_project_rows(Path(self.base['artifacts']['report_csv']), [{
            'asset_id': self.asset['id'] + '_other', 'local_file': str(self.mosaic),
            'final_status': 'EE_VERIFIED_EXISTS'}], dataset='upload_report', export_csv=False)
        self.assertFalse(p.check_cleanup_guard(guard, evidence_cache=cache))

    def test_new_local_revision_after_preview_requires_new_preview(self):
        guard = self.candidates()[0].verification_guard
        # Same output bytes, a newer processing receipt: do not delete based on an old preview.
        self.store.record_artifact('mosaic', self.mosaic, [self.extract], self.config, 'new run', {})
        self.assertFalse(p.check_cleanup_guard(guard))
        self.assertTrue(self.mosaic.exists())

    def test_mosaic_sidecars_are_removed_only_with_the_confirmed_parent(self):
        from swotflow.core.sidecars import geotiff_sidecars
        paths = geotiff_sidecars(self.mosaic)
        for path in paths:
            path.write_text('companion', encoding='utf-8')
        proof = self.store.record_artifact('mosaic', self.mosaic, [self.extract], self.config, 'run', {})
        receipt = self.store.prepare_ingestion(self.asset['id'], proof)
        self.store.record_ingestion({**receipt, 'phase': 'submitted'})
        self.asset['properties'] = p.ingestion_properties(receipt)
        p.save_inventory(self.store, self.inventory)
        candidates = p.cleanup_guards(self.base, [CleanupCandidate('mosaic', path, '', '') for path in [self.mosaic, *paths]])
        count, _, errors = delete_cleanup_candidates(candidates)
        self.assertEqual((count, errors), (1 + len(paths), []))
        self.assertTrue(all(not path.exists() for path in [self.mosaic, *paths]))

    def test_future_new_input_rebuilds_its_whole_group(self):
        extra = copy.deepcopy(self.g)
        extra.file_name = self.name.replace('001F', '002F')
        self.assertNotEqual(extra.file_name, self.name)
        extra.source['umm']['DataGranule']['ArchiveAndDistributionInformation'][0]['Name'] = extra.file_name
        self.assertEqual(p.reusable_download_groups([self.g, extra], self.store, self.inventory['collection']), set())

    def test_statistics_retains_historical_counts_and_labels_policy_after_real_cleanup(self):
        write_csv(Path(self.base['mosaic']['manifest_csv']), [{'output_file': str(self.mosaic),
            'input_files': json.dumps([str(self.extract)]), 'status': 'MOSAIC_CREATED', 'input_count': '1'}])
        before = collect_project_insights(self.base)
        count, _, errors = delete_cleanup_candidates(self.candidates())
        self.assertEqual((count, errors), (1, []))
        after = collect_project_insights(self.base)
        self.assertEqual(before.upload_status_counts, after.upload_status_counts)
        self.assertEqual(before.metrics['Uploaded/already-existing assets recorded'], after.metrics['Uploaded/already-existing assets recorded'])
        self.assertEqual(before.metrics['Mosaic GeoTIFFs on disk'], '1')
        self.assertEqual(after.metrics['Mosaic GeoTIFFs on disk'], '0')
        self.assertIn('provenance', after.metrics['Mosaic cleanup policy'])
        self.assertIn('pixel certification separate', after.metrics['Mosaic cleanup verification scope'])
        self.assertFalse(self.mosaic.exists())


class PolicyResumeTests(unittest.TestCase):
    def test_completed_year_cleanup_policy_change_does_not_repeat_scientific_work(self):
        with TemporaryDirectory() as tmp:
            cfg = yearly.config_at(Path(tmp), utm_tiles=['UTM30R'], include_upload=True, end_date='2023-12-31')
            helper, calls = yearly.YearlyAutomationTests(), []
            with helper.harness(calls):
                state = auto.run_automation(cfg, preflight_state=yearly.preflight(cfg))
            restored = auto.load_automation_run_state(state.run_dir)
            restored.config.base_config['cleanup'] = {'mosaic_policy': CONFIRMED_INGESTION}
            calls.clear()
            with helper.harness(calls):
                result = auto.run_automation(restored.config, preflight_state=restored)
            self.assertEqual(calls, [('2023', 'UTM30R', 'mosaic_cleanup')])
            self.assertEqual(result.year_batches[0]['status'], 'complete')
            self.assertFalse(result.errors)

    def test_independent_toggles_work_in_yearly_and_ordinary_automation(self):
        for annual in (False, True):
            for intermediates, mosaics in ((False, False), (True, False), (False, True), (True, True)):
                with self.subTest(annual=annual, raw=intermediates, mosaic=mosaics), TemporaryDirectory() as tmp:
                    cfg = yearly.config_at(Path(tmp), utm_tiles=['UTM30R'], include_upload=True,
                        process_by_year=annual, end_date='2023-12-31', cleanup_enabled=intermediates)
                    cfg.base_config['cleanup'] = {'mosaics_enabled': mosaics, 'mosaic_policy': CONFIRMED_INGESTION}
                    cfg.base_config['processing'].update(acquisition_start_date=cfg.start_date, acquisition_end_date=cfg.end_date)
                    helper, calls = yearly.YearlyAutomationTests(), []
                    with helper.harness(calls):
                        state = auto.run_automation(cfg, preflight_state=yearly.preflight(cfg))
                    stages = {s for _, _, s in calls}
                    self.assertEqual('raw_cleanup' in stages, intermediates)
                    self.assertEqual('extracted_cleanup' in stages, intermediates)
                    self.assertEqual('mosaic_cleanup' in stages, mosaics)
                    self.assertFalse(state.errors)

    def test_cleanup_policy_record_roundtrips_in_run_json_and_csv(self):
        with TemporaryDirectory() as tmp:
            state = yearly.preflight(yearly.config_at(Path(tmp)))
            state.stage_results.append(auto.AutomationStageResult(state.run_id, 'UTM30R', 'mosaic_cleanup', 'success',
                                       deleted_files=1, cleanup_policy=CONFIRMED_INGESTION))
            auto.write_run_state(state)
            self.assertEqual(auto.load_automation_run_state(state.run_dir).stage_results[0].cleanup_policy, CONFIRMED_INGESTION)
            self.assertIn(CONFIRMED_INGESTION, state.csv_path.read_text())
