"""Stable read caches, real evidence invalidation, and safe TIFF/TFW cleanup."""
from contextlib import closing
import json
import os
import sqlite3
import unittest
from pathlib import Path
from unittest import mock

from swotflow.consolidation.core import snapshot_asset
from swotflow.core.sidecars import geotiff_sidecars
from swotflow.processing import automation, mosaic
from swotflow.project import insights
from swotflow.project.database import database_path_for
from swotflow.verification import pipeline as p, retention
from swotflow.verification.resume import verified_resume_stages
from tests import test_ingestion_cleanup as fixtures
from tests.test_project_insights import write_csv


class CleanupEvidenceCacheTests(unittest.TestCase):
    def setUp(self):
        fixtures.IngestionCleanupTests.setUp(self)
        self.enterContext(mock.patch('swotflow.consolidation.register.default_path',
                                     return_value=self.root / 'register.sqlite3'))

    upload = fixtures.IngestionCleanupTests.upload
    candidates = fixtures.IngestionCleanupTests.candidates

    def evidence(self, cache):
        return retention.current_cleanup_evidence(self.store.logs, self.inventory['collection'],
            self.base['cleanup']['mosaic_policy'], self.base['artifacts']['report_csv'], cache)

    def test_reader_shm_changes_do_not_rebuild_evidence_under_either_policy(self):
        self.store.record_cloud({**self.receipt, 'asset_fingerprint': p.digest(snapshot_asset(self.asset)),
                                 'local_auxiliary_hashes': self.mosaic_proof.get('auxiliary_hashes', {})})
        for policy in ('confirmed_ingestion', 'full_pixels'):
            with self.subTest(policy=policy):
                self.base['cleanup']['mosaic_policy'] = policy
                guard = self.candidates()[0].verification_guard
                cache = {}
                with mock.patch.object(p, 'active_proofs', wraps=p.active_proofs) as built:
                    for _ in range(10):
                        self.store.artifact(self.mosaic)  # Real read-only WAL receipt access.
                        for path in self.store.logs.glob('*-shm'):
                            stat = path.stat()
                            os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
                        self.assertTrue(p.check_cleanup_guard(guard, evidence_cache=cache))
                    self.assertEqual(built.call_count, 1)

    def test_readonly_empty_wal_creation_and_removal_do_not_invalidate(self):
        # Open/close a quiescent WAL DB as read-only; its zero-byte WAL is not evidence.
        key = retention._cleanup_history_signature(self.store.logs, self.inventory['collection'], '')
        self.store.artifact(self.mosaic)
        self.assertEqual(key, retention._cleanup_history_signature(self.store.logs, self.inventory['collection'], ''))
        empty = self.store.logs / 'unused.sqlite3-wal'
        empty.touch()
        self.assertEqual(key, retention._cleanup_history_signature(self.store.logs, self.inventory['collection'], ''))
        empty.unlink()
        self.assertEqual(key, retention._cleanup_history_signature(self.store.logs, self.inventory['collection'], ''))

    def test_committed_wal_status_change_invalidates_before_checkpoint(self):
        guard = self.candidates()[0].verification_guard
        cache = {}
        report = Path(self.base['artifacts']['report_csv'])
        db_path = database_path_for(report)
        with closing(sqlite3.connect(db_path)) as keeper:
            keeper.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            keeper.execute('BEGIN')
            keeper.execute('SELECT * FROM records').fetchall()  # Pin a reader snapshot.
            self.assertTrue(p.check_cleanup_guard(guard, evidence_cache=cache))
            before = db_path.read_bytes()
            with closing(sqlite3.connect(db_path)) as writer:
                writer.execute('PRAGMA wal_autocheckpoint=0')
                for status, allowed in [('FAILED', False), ('EE_VERIFIED_EXISTS', True)]:
                    key, payload = writer.execute(
                        "SELECT record_key, payload_json FROM records WHERE dataset='upload_report'"
                    ).fetchone()
                    row = json.loads(payload)
                    row['final_status'] = status
                    writer.execute("UPDATE records SET status=?, payload_json=? "
                                   "WHERE dataset='upload_report' AND record_key=?",
                                   (status, json.dumps(row), key))
                    writer.commit()
                    self.assertEqual(db_path.read_bytes(), before)  # WAL-only commit.
                    self.assertGreater(Path(str(db_path) + '-wal').stat().st_size, 0)
                    self.assertEqual(p.check_cleanup_guard(guard, evidence_cache=cache), allowed)
            keeper.rollback()

    def test_same_size_restored_mtime_and_atomic_replacement_invalidate(self):
        path = self.store.logs / 'verification_note.json'
        path.write_bytes(b'AAAA')
        cache = {}
        with mock.patch.object(p, 'active_proofs', wraps=p.active_proofs) as built:
            self.evidence(cache)
            stat = path.stat()
            path.write_bytes(b'BBBB')
            os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
            self.evidence(cache)
            replacement = path.with_suffix('.new')
            replacement.write_bytes(b'CCCC')
            os.utime(replacement, ns=(stat.st_atime_ns, stat.st_mtime_ns))
            replacement.replace(path)
            self.evidence(cache)
            self.assertEqual(built.call_count, 3)

    def test_missing_change_metadata_disables_reuse(self):
        cache = {}
        with mock.patch('swotflow.verification.file_cache.content_signature', return_value=None), \
             mock.patch.object(p, 'active_proofs', wraps=p.active_proofs) as built:
            for _ in range(3):
                self.assertTrue(self.evidence(cache)[0])
            self.assertEqual(built.call_count, 3)
            self.assertEqual(cache, {})

    def test_change_during_build_retries_and_does_not_reuse_mixed_evidence(self):
        original = p.active_proofs
        changed = False
        def read_then_change(*args, **kwargs):
            nonlocal changed
            result = original(*args, **kwargs)
            if not changed:
                self.upload('FAILED', export_csv=False)
                changed = True
            return result
        guard = self.candidates()[0].verification_guard
        with mock.patch.object(p, 'active_proofs', side_effect=read_then_change) as built:
            self.assertFalse(p.check_cleanup_guard(guard, evidence_cache={}))
            self.assertEqual(built.call_count, 2)

    def test_continuously_changing_evidence_retains_target(self):
        original = p.active_proofs
        path = self.store.logs / 'changing.json'
        path.write_text('0', encoding='utf-8')
        def unstable(*args, **kwargs):
            result = original(*args, **kwargs)
            path.write_text(path.read_text(encoding='utf-8') + '1', encoding='utf-8')
            return result
        guard = self.candidates()[0].verification_guard
        with mock.patch.object(p, 'active_proofs', side_effect=unstable) as built:
            self.assertFalse(p.check_cleanup_guard(guard, evidence_cache={}))
            self.assertEqual(built.call_count, 2)
            self.assertTrue(self.mosaic.exists())

    def test_cache_does_not_skip_target_hash_or_inventory_age_checks(self):
        guard = self.candidates()[0].verification_guard
        cache = {}
        self.assertTrue(p.check_cleanup_guard(guard, evidence_cache=cache))
        self.mosaic.write_bytes(b'MOSAIC')
        self.assertFalse(p.check_cleanup_guard(guard, evidence_cache=cache))
        self.mosaic.write_bytes(b'mosaic')
        with mock.patch('swotflow.catalog.history.recent', return_value=False):
            self.assertFalse(p.check_cleanup_guard(guard, evidence_cache=cache))

    def companion_candidates(self):
        # Real SWOT name permits both ordinary and annual scope filtering.
        self.mosaic = self.mosaic.rename(self.mosaic.with_name(self.raw.stem.replace('_001F_', '_MOSA_') + '.tif'))
        companions = geotiff_sidecars(self.mosaic)
        for path in companions:
            path.write_text('verified companion', encoding='utf-8')
        proof = self.store.record_artifact('mosaic', self.mosaic, [self.extract],
                                           mosaic.parse_config(self.base, self.root), 'with-companions', {})
        receipt = self.store.prepare_ingestion(self.asset['id'], proof)
        self.store.record_ingestion({**receipt, 'phase': 'submitted'})
        self.asset['properties'] = p.ingestion_properties(receipt)
        p.save_inventory(self.store, self.inventory)
        self.upload('EE_VERIFIED_EXISTS')
        write_csv(Path(self.base['mosaic']['manifest_csv']), [{'output_file': str(self.mosaic),
            'input_files': json.dumps([str(self.extract)]), 'status': 'MOSAIC_CREATED', 'input_count': '1'}])
        return companions, p.cleanup_guards(self.base, [insights.CleanupCandidate('mosaic', path, '', '')
                                                       for path in [self.mosaic, *companions]])

    def test_parent_and_companions_share_evidence_but_recheck_bytes_and_ownership(self):
        companions, candidates = self.companion_candidates()
        unrelated = self.mosaic.parent / 'unrelated.tfw'
        unrelated.write_text('keep', encoding='utf-8')
        with mock.patch.object(p, 'active_proofs', wraps=p.active_proofs) as built:
            count, _, errors = insights.delete_cleanup_candidates(candidates)
            self.assertEqual((count, errors), (1 + len(companions), []))
            self.assertEqual(built.call_count, 1)
        self.assertTrue(unrelated.exists())
        self.assertIsNotNone(self.store.artifact(self.mosaic))

    def test_late_status_change_after_parent_deletion_retains_companions(self):
        companions, candidates = self.companion_candidates()
        original = Path.unlink
        def remove_then_change(path, *args, **kwargs):
            result = original(path, *args, **kwargs)
            if path == self.mosaic:
                self.upload('FAILED', export_csv=False)
            return result
        with mock.patch.object(Path, 'unlink', new=remove_then_change):
            count, _, errors = insights.delete_cleanup_candidates(candidates)
        self.assertEqual(count, 1)
        self.assertEqual(len(errors), len(companions))
        self.assertTrue(all(path.exists() for path in companions))

    def test_orphan_companions_retain_current_parent_and_changed_world_file(self):
        companions, candidates = self.companion_candidates()
        guards = {c.path: c.verification_guard for c in candidates}
        world = self.mosaic.with_suffix('.tfw')
        self.assertFalse(p.check_cleanup_guard(guards[world], evidence_cache={}))
        self.assertTrue(p.check_cleanup_guard(guards[self.mosaic], evidence_cache={}))
        self.mosaic.unlink()
        world.write_text('changed world file', encoding='utf-8')
        self.assertFalse(p.check_cleanup_guard(guards[world], evidence_cache={}))

    def test_interrupted_cleanup_resume_and_statistics_in_both_schedulers(self):
        # Run fresh fixtures per scheduler; actual cleanup touches only temporary files.
        for annual in (False, True):
            case = CleanupEvidenceCacheTests('runTest')
            case.setUp()
            try:
                with self.subTest(yearly=annual):
                    companions, candidates = case.companion_candidates()
                    case.base.setdefault('automation', {}).update(process_by_year=annual, yearly_batch=annual)
                    before = insights.collect_project_insights(case.base, include_cleanup=False)
                    original = Path.unlink
                    def interrupted(path, *args, **kwargs):
                        result = original(path, *args, **kwargs)
                        if path == case.mosaic:
                            raise KeyboardInterrupt('simulated shutdown after verified TIFF deletion')
                        return result
                    with mock.patch.object(Path, 'unlink', new=interrupted), self.assertRaises(KeyboardInterrupt):
                        insights.delete_cleanup_candidates(candidates)
                    count, _, errors = automation.cleanup_stage(case.base, 'UTM30R', 'mosaic')
                    self.assertEqual((count, errors), (len(companions), []))
                    self.assertTrue(all(not path.exists() for path in companions))
                    after = insights.collect_project_insights(case.base, include_cleanup=False)
                    self.assertEqual(before.upload_status_counts, after.upload_status_counts)
                    self.assertEqual(after.metrics['Mosaic GeoTIFFs on disk'], '0')
                    self.assertIsNotNone(case.store.artifact(case.mosaic))
                    # A completed source-cleanup prefix has already removed these rasters.
                    case.raw.unlink()
                    case.extract.unlink()
                    completed = {'download', 'duplicates', 'extract', 'raw_cleanup', 'mosaic', 'extracted_cleanup'}
                    self.assertEqual(verified_resume_stages(case.base, case.base, completed, case.root)[0], completed)
            finally:
                case.doCleanups()


if __name__ == '__main__':
    unittest.main()
