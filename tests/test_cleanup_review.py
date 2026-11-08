"""Cleanup option combinations, revision invalidation and bounded checksum I/O."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from swotflow.consolidation.core import snapshot_asset
from swotflow.project import insights
from swotflow.verification import pipeline as p
from swotflow.verification.file_cache import CleanupHashCache, content_signature
from tests import test_ingestion_cleanup as ingestion_fixtures
from tests import test_resume_cleanup_lineage as resume_fixtures
from tests import test_project_insights as insight_fixtures


class CleanupPolicyReviewTests(unittest.TestCase):
    setUp = ingestion_fixtures.IngestionCleanupTests.setUp
    upload = ingestion_fixtures.IngestionCleanupTests.upload
    archived_name = ingestion_fixtures.IngestionCleanupTests.archived_name
    candidates = ingestion_fixtures.IngestionCleanupTests.candidates

    def full_receipt(self):
        self.store.record_cloud({**self.receipt,
            'asset_fingerprint': p.digest(snapshot_asset(self.asset)),
            'local_auxiliary_hashes': self.mosaic_proof.get('auxiliary_hashes', {})})

    def test_explicit_policy_enforced_independently_of_download_mode_and_status(self):
        self.full_receipt()
        self.archived_name()
        for policy in ('full_pixels', 'confirmed_ingestion'):
            self.base['cleanup']['mosaic_policy'] = policy
            for verified in (False, True):
                self.base['download']['verified_downloads'] = verified
                for status in ('EE_VERIFIED_EXISTS', 'COMPLETED', 'SKIPPED_ALREADY_EXISTS',
                               'RUNNING', 'SUBMITTED', 'SUBMITTED_PENDING_VERIFICATION',
                               'READY', 'FAILED', 'ERROR', 'UNKNOWN_AFTER_CLICK', ''):
                    with self.subTest(policy=policy, verified=verified, status=status):
                        self.upload(status)
                        allowed = insights.plan_cleanup_candidates(self.base)
                        self.assertEqual(len([c for c in allowed if c.stage == 'mosaic']),
                                         int(status in ('EE_VERIFIED_EXISTS', 'COMPLETED', 'SKIPPED_ALREADY_EXISTS')))
                        self.assertTrue(self.mosaic.exists())

    def test_explicit_strict_policy_never_accepts_presence_without_pixel_proof(self):
        self.base['cleanup']['mosaic_policy'] = 'full_pixels'
        for verified in (False, True):
            self.base['download']['verified_downloads'] = verified
            self.assertEqual(insights.plan_cleanup_candidates(self.base), [])
        self.full_receipt()
        self.assertEqual(len(insights.plan_cleanup_candidates(self.base)), 1)

    def test_strict_policy_requires_same_local_revision_and_unique_upload_owner(self):
        self.full_receipt()
        self.base['cleanup']['mosaic_policy'] = 'full_pixels'
        self.assertEqual(len(self.candidates()), 1)
        self.archived_name('FAILED')
        self.assertEqual(self.candidates(), [])
        self.archived_name()
        self.assertEqual(len(self.candidates()), 1)
        self.mosaic.write_bytes(b'MOSAIC')
        self.store.record_artifact('mosaic', self.mosaic, [self.extract], self.config, 'new', {})
        self.assertEqual(self.candidates(), [])

    def test_strict_guard_rechecks_history_and_proof_after_preview(self):
        self.full_receipt()
        self.base['cleanup']['mosaic_policy'] = 'full_pixels'
        guard = self.candidates()[0].verification_guard
        cache = {}
        self.assertTrue(p.check_cleanup_guard(guard, evidence_cache=cache))
        self.upload('FAILED', export_csv=False)
        self.assertFalse(p.check_cleanup_guard(guard, evidence_cache=cache))
        self.upload('EE_VERIFIED_EXISTS', export_csv=False)
        self.assertTrue(p.check_cleanup_guard(guard, evidence_cache=cache))
        self.store.record_artifact('mosaic', self.mosaic, [self.extract], self.config, 'new', {})
        self.assertFalse(p.check_cleanup_guard(guard, evidence_cache=cache))

    def test_strict_sidecars_follow_only_the_exact_verified_parent(self):
        from swotflow.core.sidecars import geotiff_sidecars
        self.base['cleanup']['mosaic_policy'] = 'full_pixels'
        sidecars = geotiff_sidecars(self.mosaic)
        for sidecar in sidecars:
            sidecar.write_bytes(b'original')
        self.mosaic_proof = self.store.record_artifact('mosaic', self.mosaic, [self.extract], self.config, 'new', {})
        self.full_receipt()
        allowed = p.cleanup_guards(self.base, [insights.CleanupCandidate('mosaic', path, '', '')
                                               for path in [self.mosaic, *sidecars]])
        self.assertEqual(insights.delete_cleanup_candidates(allowed)[::2], (len(sidecars) + 1, []))
        self.assertTrue(all(not path.exists() for path in [self.mosaic, *sidecars]))
        self.assertIsNotNone(self.store.artifact(self.mosaic))


class CleanupHashReviewTests(unittest.TestCase):
    setUp = ingestion_fixtures.IngestionCleanupTests.setUp
    upload = ingestion_fixtures.IngestionCleanupTests.upload
    candidates_with_sidecars = resume_fixtures.ResumeCleanupTests.candidates_with_sidecars

    def test_downstream_mosaic_hashed_once_per_preview_and_once_per_deletion_batch(self):
        config, candidates = self.candidates_with_sidecars()
        original = p.file_hash
        with mock.patch.object(p, 'file_hash', wraps=original) as hashed:
            allowed = p.cleanup_guards(config, candidates)
            self.assertEqual(sum(call.args[0] == self.mosaic for call in hashed.call_args_list), 1)
        with mock.patch.object(p, 'file_hash', wraps=original) as hashed:
            self.assertEqual(insights.delete_cleanup_candidates(allowed)[::2], (len(candidates), []))
            self.assertEqual(sum(call.args[0] == self.mosaic for call in hashed.call_args_list), 1)

    def test_downstream_change_during_batch_preserving_size_and_mtime_retains_companions(self):
        config, candidates = self.candidates_with_sidecars()
        allowed = p.cleanup_guards(config, candidates)
        before = self.mosaic.stat()
        original = Path.unlink
        def remove(path, *args, **kwargs):
            result = original(path, *args, **kwargs)
            if path == self.extract:
                self.mosaic.write_bytes(b'MOSAIC')
                os.utime(self.mosaic, ns=(before.st_atime_ns, before.st_mtime_ns))
            return result
        with mock.patch.object(Path, 'unlink', new=remove):
            count, _, errors = insights.delete_cleanup_candidates(allowed)
        self.assertEqual(count, 1)
        self.assertEqual(len(errors), len(candidates) - 1)
        self.assertTrue(all(c.path.exists() for c in candidates[1:]))

    def test_auxiliary_addition_or_receipt_change_invalidates_cached_downstream(self):
        config, candidates = self.candidates_with_sidecars()
        guard = p.cleanup_guards(config, candidates)[0].verification_guard
        cache = CleanupHashCache()
        self.assertTrue(p.check_cleanup_guard(guard, hash_cache=cache))
        sidecar = Path(str(self.mosaic) + '.msk')
        sidecar.write_bytes(b'new-mask')
        self.assertFalse(p.check_cleanup_guard(guard, hash_cache=cache))
        sidecar.unlink()
        self.assertTrue(p.check_cleanup_guard(guard, hash_cache=cache))
        self.store.record_artifact('mosaic', self.mosaic, [self.extract], self.config, 'new', {})
        self.assertFalse(p.check_cleanup_guard(guard, hash_cache=cache))

    def test_retry_revalidates_downstream_before_deleting_locked_input(self):
        config, candidates = self.candidates_with_sidecars()
        allowed = p.cleanup_guards(config, candidates[:1])
        with mock.patch.object(Path, 'unlink', side_effect=PermissionError('locked')) as remove, \
             mock.patch.object(insights.time, 'sleep', side_effect=lambda _: self.mosaic.write_bytes(b'MOSAIC')):
            count, _, errors = insights.delete_cleanup_candidates(allowed)
        self.assertEqual(count, 0)
        self.assertEqual(remove.call_count, 1)
        self.assertIn('downstream verification changed', errors[0])
        self.assertTrue(self.extract.exists())

    def test_hash_cache_is_bounded_and_rechecks_replaced_file_identity(self):
        cache = CleanupHashCache(max_entries=1)
        self.assertIsNotNone(content_signature(self.mosaic))
        previous = cache.hash(self.mosaic)
        before = self.mosaic.stat()
        other = self.mosaic.with_suffix('.replacement')
        other.write_bytes(b'MOSAIC')
        os.utime(other, ns=(before.st_atime_ns, before.st_mtime_ns))
        other.replace(self.mosaic)
        self.assertNotEqual(cache.hash(self.mosaic), previous)
        cache.hash(self.extract)
        self.assertEqual(len(cache.entries), 1)

    def test_missing_change_metadata_disables_cache_and_midread_change_fails_closed(self):
        cache = CleanupHashCache()
        with mock.patch('swotflow.verification.file_cache.content_signature', return_value=None), \
             mock.patch.object(p, 'file_hash', wraps=p.file_hash) as hashed:
            cache.hash(self.mosaic)
            cache.hash(self.mosaic)
            self.assertEqual(hashed.call_count, 2)
        with mock.patch('swotflow.verification.file_cache.content_signature', side_effect=[(1,), (2,)]):
            with self.assertRaisesRegex(ValueError, 'changed during verification'):
                cache.hash(self.mosaic)


class StatisticsRefreshReviewTests(unittest.TestCase):
    def test_metadata_only_statistics_does_not_hash_or_claim_zero_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = insight_fixtures.ProjectInsightsTests().sample_project(Path(tmp))
            with mock.patch.object(insights, 'plan_cleanup_candidates', side_effect=AssertionError('expensive cleanup')):
                result = insights.collect_project_insights(config, include_cleanup=False)
            self.assertEqual(result.metrics['Cleanup candidates'], 'Not evaluated')
            self.assertEqual(result.metrics['Cleanup candidate size'], 'Not evaluated')
            self.assertEqual(result.cleanup_candidates, [])
            self.assertEqual(result.metrics['Completed mosaics recorded'], '0')

    def test_legacy_statistics_labels_actual_compatibility_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = insight_fixtures.ProjectInsightsTests().sample_project(Path(tmp))
            result = insights.collect_project_insights(config, include_cleanup=False)
            self.assertEqual(result.metrics['Mosaic cleanup policy'], 'Legacy manifest-based cleanup')
            self.assertIn('pixel certification not required', result.metrics['Mosaic cleanup verification scope'])
            config['cleanup'] = {'mosaic_policy': 'full_pixels'}
            strict = insights.collect_project_insights(config, include_cleanup=False)
            self.assertIn('strict', strict.metrics['Mosaic cleanup policy'])
            self.assertEqual(strict.metrics['Mosaic cleanup verification scope'], 'Full pixel verification required')
