"""Bounded transfer scheduling, ownership, recovery and accounting regressions."""
from dataclasses import replace
import json
from pathlib import Path
import threading
import time
import unittest
from unittest import mock

from tests import test_cloud_upload as fixtures
from tests.test_cloud_upload import NAME
from swotflow.earthengine.uploader import UploadItem
from swotflow.verification.pipeline import file_hash
from swotflow.earthengine.cloud.journal import cloud_metrics
from swotflow.earthengine.cloud.transport import GoogleTransport


class CloudParallelTests(unittest.TestCase):
    def test_transfer_uses_isolated_credentials_and_session_without_initializing_ee(self):
        transport = GoogleTransport.__new__(GoogleTransport)
        transport.config = self.config.cloud
        transport.http = object()
        credentials = object()
        with mock.patch('google.auth.default', return_value=(credentials, 'project')) as auth, \
             mock.patch('google.auth.transport.requests.AuthorizedSession') as session, \
             mock.patch.object(GoogleTransport, 'upload', autospec=True, return_value='generation') as upload, \
             mock.patch('ee.Initialize') as initialize:
            result = transport.upload_isolated({'asset_id': 'test'}, self.path, self.worker.secrets, None)
        self.assertEqual(result, 'generation')
        auth.assert_called_once_with(scopes=['https://www.googleapis.com/auth/cloud-platform'])
        session.assert_called_once_with(credentials)
        isolated = upload.call_args.args[0]
        self.assertIsNot(isolated, transport)
        self.assertIsNot(isolated.http, transport.http)
        session.return_value.__exit__.assert_called_once()
        initialize.assert_not_called()

    def setUp(self):
        fixtures.CloudWorkflowTests.setUp(self)
        self.config.cloud = replace(self.config.cloud, concurrent_uploads=2)
        self.worker.cloud = self.config.cloud
        self.config.execution.task_poll_seconds = .001
        self.config.execution.wait_timeout_minutes = .1
        self.transport.upload_isolated = self.transport.upload
        self.items = [self.item]

    def add_items(self, count):
        for n in range(1, count):
            path = self.path.with_name(NAME.replace('_003_555_', f'_003_{555+n:03}_'))
            path.write_bytes(self.path.read_bytes())
            proof = {**self.proof, 'path': str(path.resolve()), 'sha256': file_hash(path),
                     'sources': {f'source{n}.nc': {'algorithm': 'sha256', 'hash': str(n)}}}
            with self.worker.store.connection(True) as db:
                db.execute('INSERT INTO artifacts(path,stage,utc,payload) VALUES (?,?,?,?)',
                           (str(path.resolve()), 'mosaic', proof['utc'], json.dumps(proof)))
            self.items.append(UploadItem(path, path.stem, self.config.destination_parent+'/'+path.stem))

    def run_worker(self):
        with mock.patch.object(self.worker, 'build_upload_plan', return_value=self.items):
            return self.worker._run()

    def test_eight_transfers_overlap_but_only_main_thread_writes_history(self):
        self.add_items(8)
        self.config.cloud = replace(self.config.cloud, concurrent_uploads=8)
        self.worker.cloud = self.config.cloud
        self.config.upload.max_active_ingestions = 8
        self.transport.complete_after = 10000
        barrier = threading.Barrier(8, timeout=3)
        main = threading.get_ident()
        writer_threads = []
        original_save = self.worker.save
        def save(row):
            writer_threads.append(threading.get_ident()); original_save(row)
        def transfer(*args):
            self.assertNotEqual(threading.get_ident(), main)
            barrier.wait()
            return self.transport.upload(*args)
        self.transport.upload_isolated = transfer
        with mock.patch.object(self.worker, 'save', side_effect=save):
            self.assertEqual(self.run_worker(), 0)
        self.assertEqual(set(writer_threads), {main})
        self.assertEqual(len(self.transport.operations), 8)
        self.assertTrue(all(r['state']=='INGESTING' for r in self.worker.rows()))
        self.assertEqual(len(self.transport.objects), 8)

    def test_ingestion_cap_is_separate_and_verified_copies_are_cleaned(self):
        self.add_items(4)
        self.config.upload.max_active_ingestions = 1
        self.config.upload.completion_mode = 'wait_for_ee'
        self.transport.complete_after = 0
        original_submit = self.transport.submit
        def submit(rid, manifest):
            active = [op for op in self.transport.operations.values() if op['manifest']['name'] not in self.transport.assets]
            self.assertEqual(len(active), 0)
            return original_submit(rid, manifest)
        self.transport.submit = submit
        self.assertEqual(self.run_worker(), 0)
        self.assertFalse(self.transport.objects)
        self.assertTrue(all(r['state']=='VERIFIED' and r['deleted_at'] for r in self.worker.rows()))
        metrics = cloud_metrics(self.config.artifacts.report_csv)
        self.assertEqual(metrics['Cloud staging copies removed'], '4')
        before = list(self.transport.requests)
        self.assertEqual(self.run_worker(), 0)
        self.assertEqual(self.transport.requests, before)

    def test_staging_budget_reserves_bytes_of_inflight_transfers(self):
        self.add_items(2)
        self.config.cloud = replace(self.config.cloud, staging_limit_gib=self.path.stat().st_size/2**30)
        self.worker.cloud = self.config.cloud
        self.config.upload.completion_mode = 'wait_for_ee'
        self.transport.complete_after = 0
        self.assertEqual(self.run_worker(), 0)
        events = [e[0] for e in self.transport.events]
        self.assertLess(events.index('delete'), len(events)-1-events[::-1].index('transfer'))

    def test_deadline_drains_transfer_without_submitting_or_deleting_pending_bytes(self):
        self.config.execution.wait_timeout_minutes = .002
        def slow(*args):
            time.sleep(.2)
            return self.transport.upload(*args)
        self.transport.upload_isolated = slow
        self.assertEqual(self.run_worker(), 1)
        row = self.worker.rows()[0]
        self.assertEqual(row['state'], 'STAGED')
        self.assertTrue(self.transport.objects)
        self.assertFalse(self.transport.requests)
        self.assertTrue(self.path.exists())

    def test_fail_fast_preserves_other_running_transfer_until_it_settles(self):
        self.add_items(2); self.config.upload.fail_fast=True
        barrier=threading.Barrier(2, timeout=3)
        def transfer(row,*args):
            barrier.wait()
            if row['asset_id']==self.item.asset_id:
                raise RuntimeError('network interruption')
            time.sleep(.1)
            return self.transport.upload(row,*args)
        self.transport.upload_isolated=transfer
        self.assertEqual(self.run_worker(),1)
        self.assertEqual(self.worker.journal.get(self.items[1].asset_id)['state'],'STAGED')
        self.assertFalse(self.transport.requests)
        self.assertEqual(len(self.transport.objects),1)

    def test_lost_submission_response_reuses_persisted_request_id(self):
        self.config.upload.retry_attempts=2
        self.transport.complete_after=10000
        self.transport.lost_response=True
        self.assertEqual(self.run_worker(),0)
        self.assertEqual(len(self.transport.requests),2)
        self.assertEqual(len(set(self.transport.requests)),1)
        self.assertEqual(len(self.transport.operations),1)

    def test_exhausted_unknown_submission_does_not_block_staged_files_for_hours(self):
        self.add_items(2); self.config.upload.max_active_ingestions=1
        self.transport.submit=mock.Mock(side_effect=RuntimeError('lost response'))
        started=time.monotonic()
        self.assertEqual(self.run_worker(),1)
        self.assertLess(time.monotonic()-started,3)
        self.assertEqual({r['state'] for r in self.worker.rows()},{'SUBMITTING','STAGED'})
        self.assertEqual(len(self.transport.objects),2)

    def test_no_budget_does_not_start_transfers(self):
        self.config.cloud=replace(self.config.cloud,staging_limit_gib=1e-9)
        self.worker.cloud=self.config.cloud
        self.assertEqual(self.run_worker(),1)
        self.assertFalse(self.transport.objects)
        self.assertFalse(self.transport.requests)

    def test_restart_staged_copy_does_not_retransfer(self):
        row=self.worker.prepare(self.item)
        self.worker.prepare_transfer(row)
        generation=self.transport.upload(row,self.path,self.worker.secrets)
        # Simulate a crash after upload before the main thread records completion.
        self.assertEqual(row['state'],'TRANSFERRING')
        def resume(row,*args):
            return self.transport.check_object(row,self.transport.object(row))
        self.transport.upload_isolated=resume
        self.assertEqual(self.run_worker(),0)
        self.assertEqual(len([e for e in self.transport.events if e[0]=='transfer']),1)
        self.assertEqual(self.worker.rows()[0]['generation'],generation)
