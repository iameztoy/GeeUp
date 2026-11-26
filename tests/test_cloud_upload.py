"""Cloud backend regression tests: no network, billing or production data."""
import copy
import json
import logging
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from swotflow.earthengine.cloud.config import CloudConfig
from swotflow.earthengine.cloud.journal import CloudJournal, cloud_metrics
from swotflow.earthengine.cloud.manifest import build_manifest
from swotflow.earthengine.cloud.transport import GoogleTransport, file_checksums, CloudHTTPError
from swotflow.earthengine.cloud.worker import CloudUploader
from swotflow.earthengine.uploader import UploadItem, parse_config, EarthEngineUIUploader
from swotflow.verification.pipeline import ingestion_properties, file_hash
from swotflow.project.database import read_project_rows
from tests import test_uploader_regressions as browser_regressions

NAME = 'SWOT_L2_HR_Raster_100m_UTM35L_N_x_x_x_003_555_MOSA_20230920T182643_20230920T182904_PGD0_01.tif'

class MemorySecrets:
    def __init__(self): self.values = {}
    def save(self,k,v): self.values[k]=v
    def load(self,k): return self.values.get(k,'')
    def remove(self,k): self.values.pop(k,None)

class FakeTransport:
    check_object = staticmethod(GoogleTransport.check_object)
    def __init__(self):
        self.ee = mock.Mock()
        self.objects, self.assets, self.operations = {}, {}, {}
        self.requests, self.events = [], []
        self.lost_response = False
        self.delete_error = False
        self.complete_after = 2
    def bucket_info(self): return {'storageClass':'STANDARD'}
    def object(self,row): return self.objects.get(row['object'])
    def upload(self,row,path,secrets,progress=lambda offset: None):
        obj={'size':str(row['size']), 'md5Hash':row['md5'], 'generation':'1',
             'metadata':{'sha256':row['sha256'],'swotflow_owner':row['owner']}}
        self.objects[row['object']]=obj; self.events.append(('transfer',row['asset_id']))
        progress(row['size'])
        return '1'
    def asset(self,aid):
        return {"type":"IMAGE_COLLECTION"} if aid == "projects/example/assets/collection" else self.assets.get(aid)
    def submit(self,rid,manifest):
        self.requests.append(rid)
        self.operations.setdefault(rid,{'manifest':copy.deepcopy(manifest),'polls':0})
        self.events.append(('submit',manifest['name']))
        if self.lost_response:
            self.lost_response=False
            raise RuntimeError('response lost')
        return rid
    def operation(self,rid):
        op=self.operations[rid]; op['polls']+=1
        if op.get('failure'): return {'done':True,'error':{'message':'upstream ingestion failed'}}
        if op['polls']>=self.complete_after:
            m=op['manifest']; self.assets[m['name']]={**m,'id':m['name'],'type':'IMAGE'}
            return {'done':True}
        return {'done':False}
    def delete(self,row):
        if self.delete_error: raise RuntimeError('no delete permission')
        self.check_object(row,self.objects[row['object']])
        self.objects.pop(row['object']); self.events.append(('delete',row['asset_id']))


class CloudWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.config=browser_regressions.UploaderDialogRegressionTests().uploader_config(self.root, completion_mode='submit_and_continue')
        self.config.verified_workflow=True; self.config.execution.dry_run=False
        self.config.upload.backend='gcs'; self.config.upload.retry_wait_seconds=0
        self.config.upload.max_active_ingestions=2
        self.config.cloud=CloudConfig('example-project','swotflow-test-bucket')
        self.transport=FakeTransport()
        self.worker=CloudUploader(self.config, logging.getLogger('cloud-test'), assume_yes=True,
                                  transport=self.transport,secrets=MemorySecrets())
        self.path=self.config.input_folder/NAME; self.path.write_bytes(b'fixture raster')
        self.proof={'stage':'mosaic','path':str(self.path.resolve()),'sha256':file_hash(self.path),
                    'size':self.path.stat().st_size,'sources':{'original.nc':{'algorithm':'sha256','hash':'original-hash'}},
                    'auxiliary_hashes':{},'grid':{'bands':2},'utc':'2026-01-01T00:00:00Z'}
        with self.worker.store.connection(True) as db:
            db.execute('INSERT INTO artifacts(path,stage,utc,payload) VALUES (?,?,?,?)',
                (str(self.path.resolve()),'mosaic',self.proof['utc'],json.dumps(self.proof)))
        self.item=UploadItem(self.path,self.path.stem,self.config.destination_parent+'/'+self.path.stem,
                             source_utm_tiles=['UTM35L'])
        self.worker.connect()
        self.worker.sync_earth_engine_inventory=mock.Mock()
    def prepare(self): return self.worker.prepare(self.item)
    def complete(self,row):
        self.worker.advance(row)
        self.transport.complete_after=0
        self.worker.reconcile(); self.worker.reconcile()

    def test_manifest_matches_browser_properties_and_times_without_reprojection(self):
        row=self.prepare()
        with mock.patch.object(self.worker,'is_current_upload_dialog_open',return_value=True), \
             mock.patch.object(self.worker,'_apply_current_ui_metadata_properties') as apply:
            self.worker.apply_metadata_properties(self.item)
        self.assertEqual(row['manifest']['properties'],apply.call_args.args[0].properties)
        self.assertEqual(row['manifest']['startTime'],'2023-09-20T18:26:43Z')
        self.assertEqual(row['manifest']['endTime'],'2023-09-20T18:29:04Z')
        self.assertEqual([b['id'] for b in row['manifest']['bands']],['b1','b2'])
        self.assertNotIn('crs',json.dumps(row['manifest']))
        self.assertNotIn('missingData',json.dumps(row['manifest']))

    def test_per_band_policies_explicitly_preserved(self):
        self.config.upload.pyramiding_policy.default='MEAN'
        self.config.upload.pyramiding_policy.per_band={'b2':'SAMPLE'}
        row=self.prepare()
        self.assertEqual([b['pyramidingPolicy'] for b in row['manifest']['bands']],['MEAN','SAMPLE'])

    def test_crash_after_submission_reuses_request_id_not_a_second_ingestion(self):
        row=self.prepare(); self.transport.lost_response=True
        with self.assertRaises(RuntimeError): self.worker.advance(row)
        saved=self.worker.journal.get(self.item.asset_id)
        self.assertEqual(saved['state'],'SUBMITTING')
        self.worker.advance(saved)
        self.assertEqual(len(set(self.transport.requests)),1)
        self.assertEqual(len(self.transport.operations),1)
        self.assertEqual(self.worker.journal.get(self.item.asset_id)['state'],'INGESTING')
        self.assertNotIn('session_uri',saved)

    def test_verified_ingestion_records_provenance_and_removes_only_staging(self):
        row=self.prepare(); self.complete(row)
        saved=self.worker.journal.get(self.item.asset_id)
        self.assertEqual(saved['state'],'VERIFIED'); self.assertTrue(saved['deleted_at'])
        self.assertTrue(self.path.exists())
        self.assertEqual(self.worker.store.ingestions()[self.item.asset_id]['phase'],'confirmed')
        records=read_project_rows(self.config.artifacts.report_csv,'upload_report')
        self.assertEqual(records[0]['final_status'],'EE_VERIFIED_EXISTS')
        metrics=cloud_metrics(self.config.artifacts.report_csv)
        self.assertEqual(metrics['Cloud upload tracked mosaics'],'1')
        self.assertEqual(metrics['Cloud EE ingestions verified'],'1')

    def test_wrong_metadata_blocks_and_retains_every_copy(self):
        row=self.prepare(); self.worker.advance(row)
        asset={**row['manifest'],'id':row['asset_id'],'type':'IMAGE'}
        asset['properties']={**asset['properties'],'swotflow_sha256':'wrong'}
        self.transport.assets[row['asset_id']]=asset
        self.worker.reconcile()
        self.assertEqual(self.worker.journal.get(row['asset_id'])['state'],'BLOCKED')
        self.assertTrue(self.path.exists()); self.assertTrue(self.transport.objects)
        self.assertEqual(self.worker.store.ingestions()[row['asset_id']]['phase'], 'rejected')

    def test_cleanup_failure_is_separate_from_verified_upload(self):
        self.transport.delete_error=True
        row=self.prepare(); self.complete(row)
        saved=self.worker.journal.get(row['asset_id'])
        self.assertEqual(saved['state'],'VERIFIED'); self.assertIn('cleanup_error',saved)
        self.assertTrue(self.transport.objects)
        self.transport.delete_error=False
        self.worker._terminal_reviewed.clear()  # A later explicit sync retries cleanup.
        self.worker._initial_reconcile_done = False
        self.path.unlink()  # Local cleanup may already have independently verified/deleted the TIFF.
        self.worker.reconcile()
        self.assertTrue(self.worker.journal.get(row['asset_id'])['deleted_at'])
        self.assertEqual(len(self.transport.operations),1)

    def test_changed_generation_is_not_deleted(self):
        row=self.prepare(); self.worker.advance(row)
        self.transport.objects[row['object']]['generation']='2'
        self.transport.assets[row['asset_id']]={**row['manifest'],'id':row['asset_id'],'type':'IMAGE'}
        self.worker.reconcile()
        self.assertTrue(self.transport.objects)
        self.assertIn('cleanup_error',self.worker.journal.get(row['asset_id']))

    def test_remote_read_failure_is_not_absence_or_failure(self):
        row=self.prepare(); self.worker.advance(row)
        self.transport.asset=mock.Mock(side_effect=RuntimeError('permission denied'))
        self.worker.reconcile()
        self.assertEqual(self.worker.journal.get(row['asset_id'])['state'],'INGESTING')
        self.assertEqual(len(self.transport.requests),1)

    def test_definitive_failure_requires_fresh_request_only_on_resume(self):
        row=self.prepare(); self.worker.advance(row)
        self.transport.operations[row['request_id']]['failure']=True
        self.worker.reconcile()
        failed=self.worker.journal.get(row['asset_id'])
        self.assertEqual(failed['state'],'FAILED'); self.assertTrue(self.transport.objects)
        retried=self.worker.prepare(self.item)
        self.assertNotEqual(retried['request_id'],row['request_id'])
        self.assertEqual(retried['object'],row['object'])

    def test_submit_and_continue_does_not_wait_for_ingestion_or_clean_pending(self):
        self.transport.complete_after=10000
        with mock.patch.object(self.worker,'build_upload_plan',return_value=[self.item]), mock.patch('swotflow.earthengine.cloud.worker.time.sleep') as sleep:
            self.assertEqual(self.worker._run(),0)
        self.assertEqual(self.worker.journal.get(self.item.asset_id)['state'],'INGESTING')
        sleep.assert_not_called(); self.assertTrue(self.transport.objects); self.assertTrue(self.path.exists())

    def test_conservative_mode_waits_and_records_verification(self):
        self.config.upload.completion_mode='wait_for_ee'
        with mock.patch.object(self.worker,'build_upload_plan',return_value=[self.item]), mock.patch('swotflow.earthengine.cloud.worker.time.sleep'):
            self.assertEqual(self.worker._run(),0)
        self.assertEqual(self.worker.journal.get(self.item.asset_id)['state'],'VERIFIED')

    def test_browser_driver_is_never_started_for_cloud(self):
        with mock.patch.object(EarthEngineUIUploader,'create_driver', side_effect=AssertionError('Chrome must not start')), \
             mock.patch.object(self.worker,'build_upload_plan',return_value=[self.item]):
            self.assertEqual(self.worker._run(),0)

    def test_inventory_cannot_reapprove_rejected_time_metadata(self):
        from swotflow.verification.pipeline import save_inventory
        row=self.prepare(); self.worker.advance(row)
        asset={**row['manifest'],'id':row['asset_id'],'type':'IMAGE','endTime':'2024-01-01T00:00:00Z'}
        self.transport.assets[row['asset_id']]=asset
        self.worker.reconcile()
        save_inventory(self.worker.store,{'collection':self.config.destination_parent,'finished_utc':'now','assets':[asset]})
        self.assertEqual(self.worker.store.ingestions()[row['asset_id']]['phase'],'rejected')
        self.assertEqual(self.worker.report.get_status(row['asset_id']),'ERROR')
        self.assertTrue(self.transport.objects)

    def test_cap_blocks_transfer_without_deleting_unverified_files(self):
        self.config.cloud=CloudConfig('example-project','swotflow-test-bucket',staging_limit_gib=0.000000001)
        self.worker.cloud=self.config.cloud
        with mock.patch.object(self.worker,'build_upload_plan',return_value=[self.item]):
            self.assertEqual(self.worker._run(),1)
        self.assertFalse(self.transport.requests); self.assertFalse(self.transport.objects); self.assertTrue(self.path.exists())

    def test_dry_run_never_submits_or_cleans_even_existing_pending_queue(self):
        row=self.prepare(); self.worker.advance(row)
        self.config.execution.dry_run=True
        with mock.patch.object(self.worker,'build_upload_plan',return_value=[self.item]):
            self.assertEqual(self.worker._run(),0)
        self.assertTrue(self.transport.objects); self.assertEqual(len(self.transport.requests),1)

    def test_exhausted_uncertain_submission_exits_without_long_monitoring_wait(self):
        self.transport.submit=mock.Mock(side_effect=RuntimeError('response uncertain'))
        self.config.upload.retry_attempts=2
        with mock.patch.object(self.worker,'build_upload_plan',return_value=[self.item]), \
             mock.patch('swotflow.earthengine.cloud.worker.time.sleep') as sleep:
            self.assertEqual(self.worker._run(),1)
        self.assertEqual(self.transport.submit.call_count,2)
        self.assertEqual(len({call.args[0] for call in self.transport.submit.call_args_list}),1)
        self.assertTrue(all(call.args[0] == 0 for call in sleep.call_args_list))
        self.assertEqual(self.worker.journal.get(self.item.asset_id)['state'],'SUBMITTING')
        self.assertTrue(self.transport.objects)

    def test_retained_verified_objects_are_not_rechecked_on_every_queue_iteration(self):
        self.config.cloud=CloudConfig('example-project','swotflow-test-bucket',delete_after_verification=False)
        self.worker.cloud=self.config.cloud
        row=self.prepare(); self.complete(row)
        with mock.patch.object(self.transport,'asset', wraps=self.transport.asset) as fetch:
            self.worker.reconcile(); self.worker.reconcile()
        fetch.assert_not_called()
        self.assertTrue(self.transport.objects)

    def test_second_file_submits_while_first_is_still_ingesting(self):
        second_path=self.path.with_name(NAME.replace('_003_555_', '_003_556_'))
        second_path.write_bytes(b'second raster')
        second_proof={**self.proof,'path':str(second_path.resolve()),'sha256':file_hash(second_path),
                      'size':second_path.stat().st_size,'sources':{'second.nc':{'algorithm':'sha256','hash':'two'}}}
        with self.worker.store.connection(True) as db:
            db.execute('INSERT INTO artifacts(path,stage,utc,payload) VALUES (?,?,?,?)',
                       (str(second_path.resolve()),'mosaic',second_proof['utc'],json.dumps(second_proof)))
        second=UploadItem(second_path,second_path.stem,self.config.destination_parent+'/'+second_path.stem)
        self.transport.complete_after=10000
        with mock.patch.object(self.worker,'build_upload_plan',return_value=[self.item,second]):
            self.assertEqual(self.worker._run(),0)
        self.assertEqual(len(self.transport.operations),2)
        self.assertEqual([e[0] for e in self.transport.events],['transfer','submit','transfer','submit'])

    def test_blocked_file_does_not_prevent_later_file_consideration(self):
        row=self.prepare(); row['state']='BLOCKED'; self.worker.save(row)
        with mock.patch.object(self.worker,'build_upload_plan',return_value=[self.item]):
            self.assertEqual(self.worker._run(),1)
        self.assertFalse(self.transport.requests)

    def test_existing_verified_revision_reuses_bytes_without_new_submission(self):
        row=self.prepare(); self.complete(row)
        self.assertEqual(self.worker.prepare(self.item)['state'],'VERIFIED')
        self.assertEqual(len(self.transport.requests),1)

    def test_transfer_progress_polls_prior_ingestions_without_another_writer(self):
        row=self.prepare()
        with mock.patch('swotflow.earthengine.cloud.worker.time', SimpleNamespace(monotonic=mock.Mock(side_effect=[0,30]))), \
             mock.patch.object(self.worker,'reconcile') as reconcile:
            self.worker.advance(row)
        reconcile.assert_called_once()
        self.assertEqual(len(self.transport.operations),1)

    def test_target_must_exist_as_image_collection(self):
        self.worker.connection_checked=False
        self.transport.asset=mock.Mock(return_value={'type':'FOLDER'})
        with self.assertRaisesRegex(ValueError,'ImageCollection'): self.worker.connect()

    def test_changed_local_bytes_cannot_resume(self):
        row=self.prepare(); self.path.write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError,'changed'): self.worker.advance(row)
        self.assertFalse(self.transport.objects)

    def test_wrong_time_or_band_blocks_verification(self):
        row=self.prepare()
        for updates in ({'startTime':'2024-01-01T00:00:00Z'},{'bands':[{'id':'b1'}]}):
            with self.subTest(updates=updates):
                with self.assertRaises(ValueError):
                    self.worker.validate_asset(row,{**row['manifest'],'id':row['asset_id'],'type':'IMAGE',**updates})

    def test_readonly_sync_no_new_transfer_or_ingestion(self):
        row=self.prepare(); self.worker.advance(row)
        self.transport.complete_after=10000
        with mock.patch.object(EarthEngineUIUploader,'sync_existing_assets_to_report',return_value=0):
            self.worker.sync_existing_assets_to_report()
        self.assertEqual(len(self.transport.requests),1)


class ResumableHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'mosaic.tif'; self.path.write_bytes(b'abcdefgh')
        sha,md5=file_checksums(self.path)
        self.row={'bucket':'test','object':'prefix/file.tif','size':8,'md5':md5,'sha256':sha,'owner':'owner','secret_id':'key'}
        self.obj={'generation':'8','md5Hash':md5,'size':'8','metadata':{'sha256':sha,'swotflow_owner':'owner'}}
        self.t=GoogleTransport.__new__(GoogleTransport); self.t.config=CloudConfig(chunk_mib=1)
        self.secrets=MemorySecrets()
    def response(self,code,body=None,headers=None): return SimpleNamespace(status_code=code,json=lambda:body,headers=headers or {})
    def test_restart_queries_offset_and_sends_only_remaining_bytes(self):
        self.secrets.save('key','https://storage.googleapis.com/upload?upload_id=secret')
        self.t.request=mock.Mock(side_effect=[self.response(404),self.response(308,headers={'Range':'bytes=0-3'}),self.response(200,self.obj)])
        self.assertEqual(self.t.upload(self.row,self.path,self.secrets),'8')
        self.assertEqual(self.t.request.call_args.kwargs['data'],b'efgh')
        self.assertEqual(self.t.request.call_args.kwargs['headers']['Content-Range'],'bytes 4-7/8')
        self.assertFalse(self.secrets.values)
    def test_completed_object_skips_transfer_only_when_owned_and_checksum_matches(self):
        self.t.request=mock.Mock(return_value=self.response(200,self.obj))
        self.assertEqual(self.t.upload(self.row,self.path,self.secrets),'8')
        self.assertEqual(self.t.request.call_count,1)
        self.obj['md5Hash']='wrong'
        with self.assertRaises(ValueError): self.t.upload(self.row,self.path,self.secrets)
    def test_new_transfer_uses_generation_precondition(self):
        self.t.request=mock.Mock(side_effect=[self.response(404),self.response(200,headers={'Location':'https://storage.googleapis.com/upload?upload_id=s'}),self.response(200,self.obj)])
        self.t.upload(self.row,self.path,self.secrets)
        self.assertEqual(self.t.request.call_args_list[1].kwargs['params']['ifGenerationMatch'],'0')
    def test_delete_is_generation_guarded(self):
        self.row['generation']='8'
        self.t.request=mock.Mock(side_effect=[self.response(200,self.obj),self.response(204)])
        self.t.delete(self.row)
        self.assertEqual(self.t.request.call_args.kwargs['params'],{'ifGenerationMatch':'8'})
    def test_session_urls_never_reach_error_logs(self):
        import requests
        self.t.http=mock.Mock(); self.t.http.request.side_effect=requests.ConnectionError('https://secret?upload_id=credential')
        with self.assertRaises(RuntimeError) as ctx:
            self.t.request('PUT','https://secret?upload_id=credential',action='Transfer')
        self.assertNotIn('credential',str(ctx.exception)); self.assertIsNone(ctx.exception.__cause__)
    def test_expired_session_restarts_with_new_preconditioned_object(self):
        self.secrets.save('key','https://storage.googleapis.com/upload?upload_id=expired')
        self.t.request=mock.Mock(side_effect=[self.response(404), self.response(410),
            self.response(200,headers={'Location':'https://storage.googleapis.com/upload?upload_id=new'}), self.response(200,self.obj)])
        self.assertEqual(self.t.upload(self.row,self.path,self.secrets),'8')
        self.assertEqual(self.t.request.call_args_list[2].kwargs['params']['ifGenerationMatch'],'0')

    def test_completed_response_lost_is_recovered_without_resubmitting_bytes(self):
        self.secrets.save('key','https://storage.googleapis.com/upload?upload_id=old')
        self.t.request=mock.Mock(side_effect=[self.response(404),self.response(200,self.obj)])
        self.assertEqual(self.t.upload(self.row,self.path,self.secrets),'8')
        self.assertEqual(self.t.request.call_args_list[1].kwargs['data'],b'')

    def test_ingestion_error_keeps_actionable_reason_but_removes_credentials(self):
        from swotflow.earthengine.cloud.transport import CloudServiceError
        self.t.ee=mock.Mock()
        self.t.ee.data.startIngestion.side_effect=RuntimeError('Permission denied https://example/?upload_id=secret Authorization: Bearer private')
        with self.assertRaises(CloudServiceError) as ctx:
            self.t.submit('same-request',{})
        self.assertIn('Permission denied',str(ctx.exception))
        self.assertNotIn('private',str(ctx.exception)); self.assertNotIn('secret',str(ctx.exception))

    def test_untrusted_saved_session_cannot_receive_credentials(self):
        self.secrets.save('key','https://attacker.invalid/upload')
        self.t.request=mock.Mock(return_value=self.response(404))
        with self.assertRaises(ValueError): self.t.upload(self.row,self.path,self.secrets)
        self.assertEqual(self.t.request.call_count,1)

class CloudConfigurationTests(unittest.TestCase):
    def test_both_automation_scopes_preserve_backend_and_metadata(self):
        from swotflow.processing.automation import build_tile_config_dict
        with tempfile.TemporaryDirectory() as temp:
            base={'processing':{'project_root':temp},'upload':{'backend':'gcs','completion_mode':'submit_and_continue'},
                  'cloud_upload':{'project':'project-id','bucket':'bucket-id','concurrent_uploads':8},'metadata':{'enabled':True},
                  'download':{'verified_downloads':True}}
            for start,end in [('2023-01-01','2023-12-31'),('2023-01-01','2026-11-26')]:
                config=build_tile_config_dict(base,'UTM36P',Path(temp)/'runs',start_date=start,end_date=end,include_upload=True)
                self.assertEqual(config['upload']['backend'],'gcs')
                self.assertEqual(config['upload']['completion_mode'],'submit_and_continue')
                self.assertEqual(config['cloud_upload'],base['cloud_upload'])
                self.assertEqual(config['metadata'],base['metadata'])
            self.assertNotIn('utm_tiles',base['upload'])
    def test_new_project_defaults_browser_and_keeps_existing_history_settings(self):
        from swotflow.project.model import new_project_defaults
        result=new_project_defaults({'upload':{'backend':'gcs'},'cloud_upload':{'bucket':'mine'}})
        self.assertEqual(result['upload']['backend'],'browser')
        self.assertEqual(result['cloud_upload']['bucket'],'mine')
    def test_interrupted_index_backfill_cannot_hide_pending_uploads(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'journal.sqlite3'
            journal=CloudJournal(path)
            row={'asset_id':'asset','collection':'collection','state':'INGESTING','bucket':'bucket',
                 'object':'object','size':15}
            journal.put(row)
            with journal.connection() as db:
                db.execute('UPDATE uploads SET state=NULL,collection=NULL'); db.commit()
            restored=CloudJournal(path)
            self.assertEqual(restored.rows('collection',{'INGESTING'})[0]['asset_id'],'asset')
            self.assertEqual(restored.live_bytes('bucket'),15)
            row['state']='VERIFIED'; row['deleted_at']='now'; restored.put(row)
            self.assertFalse(restored.rows('collection',{'INGESTING'}))
            self.assertEqual(restored.live_bytes('bucket'),0)

    def test_journal_rejects_session_secrets(self):
        with tempfile.TemporaryDirectory() as temp:
            j=CloudJournal(Path(temp)/'journal.sqlite3')
            with self.assertRaises(ValueError): j.put({'asset_id':'asset','session_uri':'secret'})


class CloudSecretTests(unittest.TestCase):
    def test_session_round_trip_and_removal(self):
        import os
        from swotflow.earthengine.cloud.secrets import SessionSecrets
        with tempfile.TemporaryDirectory() as temp, mock.patch.dict(os.environ,{'LOCALAPPDATA':temp}):
            secrets=SessionSecrets('isolated-project')
            value='https://storage.googleapis.com/upload?upload_id=sensitive'
            secrets.save('test',value)
            self.assertEqual(secrets.load('test'),value)
            if os.name == 'nt':
                self.assertNotIn(value.encode(), secrets.path('test').read_bytes())
            secrets.remove('test'); self.assertEqual(secrets.load('test'),'')


class CloudDispatchTests(unittest.TestCase):
    def test_cli_cloud_dispatch_uses_shared_lock_and_never_constructs_browser(self):
        from swotflow.earthengine import uploader
        with tempfile.TemporaryDirectory() as temp:
            config=browser_regressions.UploaderDialogRegressionTests().uploader_config(Path(temp))
            config.upload.backend='gcs'
            with mock.patch.object(uploader,'load_config_file',return_value=config), \
                 mock.patch.object(uploader,'configure_logging',return_value=logging.getLogger('test')), \
                 mock.patch('swotflow.earthengine.cloud.worker.CloudUploader') as cloud, \
                 mock.patch.object(uploader,'EarthEngineUIUploader') as browser:
                cloud.return_value.run.return_value=0
                self.assertEqual(uploader.main(['--config',str(Path(temp)/'config.yaml'),'--yes']),0)
                cloud.assert_called_once(); browser.assert_not_called()
            self.assertTrue((config.artifacts.report_csv.parent/'.upload.lock').exists())

    def test_active_worker_lock_blocks_a_second_backend(self):
        from swotflow.project.lock import _operation_lock
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'.upload.lock'
            with _operation_lock(path,'busy'):
                with self.assertRaisesRegex(RuntimeError,'busy'):
                    with _operation_lock(path,'busy'):
                        self.fail('second upload acquired the lock')

    def test_cloud_backend_does_not_validate_unused_browser_attach_settings(self):
        with tempfile.TemporaryDirectory() as temp:
            config=parse_config({'input_folder':temp,'destination_parent':'projects/example/assets/collection',
                'upload':{'backend':'gcs'},'cloud_upload':{'project':'example-project','bucket':'some-bucket'},
                'chrome':{'connection_mode':'unused','headless':True,'remote_debugging_port':0}},Path(temp))
            self.assertEqual(config.upload.backend,'gcs')
