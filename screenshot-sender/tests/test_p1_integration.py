import contextlib
import hashlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import sender_service as sender
import receiver_service as receiver
from reliability import TaskStore, QueueFull, Heartbeat, RetryDecision, PROTOCOL, BUILD

IMAGE = b'\xff\xd8synthetic-fixture-no-user-data\xff\xd9'
ROOT = Path(__file__).resolve().parents[1]

def eventually(function, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if function(): return
        time.sleep(0.01)
    raise AssertionError('condition not reached before deadline')

class FakeScreenshotter:
    def __init__(self, directory): self.directory=directory; self.count=0
    def capture(self, task_id):
        self.count+=1; path=self.directory/f'{task_id}.jpg'; path.write_bytes(IMAGE); return path

class SenderFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup); self.root=Path(self.tmp.name)
        self.config=sender.Config('http://127.0.0.1:8788',spool_dir=self.root,retry_delays=())
        self.client=mock.Mock(); self.screenshotter=FakeScreenshotter(self.root)
        self.service=sender.SenderService(self.config,self.screenshotter,self.client)
    def test_capture_only_queues_until_independent_worker_runs(self):
        task=self.service.handle_manual_capture()
        self.client.upload_record.assert_not_called()
        self.assertEqual(self.service.store.get(task)['payload'],IMAGE)
        self.service._retry_pending_once()
        self.client.upload_record.assert_called_once()
        self.assertEqual(self.service.store.get(task)['state'],'delivered')
    def test_blocked_upload_does_not_hold_capture_lock(self):
        entered,release=threading.Event(),threading.Event()
        def upload(job): entered.set(); release.wait(3)
        self.client.upload_record.side_effect=upload
        self.service.handle_manual_capture()
        worker=threading.Thread(target=self.service._retry_pending_once)
        worker.start(); self.addCleanup(release.set)
        self.assertTrue(entered.wait(1))
        captured=threading.Event()
        thread=threading.Thread(target=lambda:(self.service.handle_manual_capture(),captured.set()))
        thread.start()
        try:
            self.assertTrue(captured.wait(0.8),'new capture blocked behind network upload')
        finally:
            release.set(); thread.join(2); worker.join(2)
        self.assertEqual(self.screenshotter.count,2)
    def test_upload_failure_survives_new_sender_instance(self):
        self.client.upload_record.side_effect=ConnectionRefusedError()
        task=self.service.handle_manual_capture(); self.service._retry_pending_once()
        self.assertEqual(self.service.store.get(task)['payload'],IMAGE)
        fresh=sender.SenderService(self.config,self.screenshotter,mock.Mock())
        with fresh.store.tx() as con: con.execute('UPDATE jobs SET next_at=0')
        fresh._retry_pending_once()
        self.assertEqual(fresh.store.get(task)['state'],'delivered')
    def test_remote_reissued_request_does_not_recapture(self):
        task=uuid.uuid4(); self.service.handle_remote_task(task); self.service.handle_remote_task(task)
        self.assertEqual(self.screenshotter.count,1)
    def test_queue_failure_retains_redacted_capture_file(self):
        with mock.patch.object(self.service.store,'enqueue',side_effect=sqlite3.OperationalError('disk full')):
            with self.assertRaises(sqlite3.OperationalError): self.service.handle_manual_capture()
        self.assertEqual(len(list(self.root.glob('*.jpg'))),1)
        self.assertEqual(self.service.last_capture['state'],'failed')
    def test_busy_hotkey_is_visible_in_trace(self):
        self.service._hotkey_capture_running=True
        with mock.patch('sender_service.trace') as trace:
            self.service._trigger_hotkey_capture()
        self.assertEqual(trace.call_args.args[0],'HOTKEY_IGNORED_BUSY')
    def test_normal_keyboard_debug_logging_removed(self):
        source=(ROOT/'sender_service.py').read_text()
        self.assertNotIn('raw key:',source)
    def test_no_task_done_log_for_upload_only(self):
        task=self.service.handle_manual_capture()
        with mock.patch('sender_service.trace') as trace:
            self.service._retry_pending_once()
        events=[c.args[0] for c in trace.call_args_list]
        self.assertIn('UPLOAD_ACKNOWLEDGED',events); self.assertNotIn('TASK_DONE',events)

class HTTPCase(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.root=Path(self.tmp.name)
        self.state=receiver.ReceiverState(self.root/'latest.jpg')
        self.client=mock.Mock(); self.client.analyze.return_value='synthetic answer'
        self.analyzer=receiver.AnalysisService(self.state,self.client,'synthetic prompt')
        self.server=receiver.create_server('127.0.0.1',0,self.state,self.analyzer)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True); self.thread.start()
        self.url='http://127.0.0.1:'+str(self.server.server_address[1])
    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(2); self.tmp.cleanup()
    def request(self,path,data=None,headers=None):
        request=urllib.request.Request(self.url+path,data=data,headers=headers or {},method='POST' if data is not None else 'GET')
        try:
            with urllib.request.urlopen(request,timeout=3) as response:
                body=response.read(); return response.status,json.loads(body) if body else None
        except urllib.error.HTTPError as error:
            try: return error.code,json.loads(error.read())
            finally: error.close()
    def upload(self,task=None,sequence=1,image=IMAGE,remote=False,profile='default',**extra):
        task=task or str(uuid.uuid4())
        headers={'Content-Type':'image/jpeg','X-LanShot-Capture-ID':task,'X-LanShot-Origin':'test-sender',
                 'X-LanShot-Sequence':str(sequence),'X-LanShot-Profile':profile,**extra}
        path=f'/api/v1/tasks/{task}/image' if remote else '/api/v1/images'
        return task,self.request(path,image,headers)
    def wait_complete(self,task):
        eventually(lambda:self.state.store.get(task)['state']=='complete')
        eventually(lambda:task in self.state.latest_answer.read_text() and 'synthetic answer' in self.state.latest_answer.read_text())

class ReceiverHTTPP1Tests(HTTPCase):
    def test_scheduled_duplicate_is_analyzed_once(self):
        task,(status,ack)=self.upload()
        self.assertEqual(status,201); self.assertTrue(ack['durable'])
        self.wait_complete(task)
        _,(status,duplicate)=self.upload(task)
        self.assertEqual(status,200); self.assertEqual(duplicate['status'],'duplicate')
        self.assertEqual(self.client.analyze.call_count,1)
    def test_concurrent_duplicate_in_progress_has_one_model_call(self):
        entered,release=threading.Event(),threading.Event()
        self.client.analyze.side_effect=lambda *_:(entered.set(),release.wait(3),'synthetic answer')[-1]
        task=str(uuid.uuid4())
        try:
            with ThreadPoolExecutor(max_workers=6) as pool:
                results=list(pool.map(lambda _:self.upload(task),range(6)))
            self.assertTrue(entered.wait(1))
            self.assertEqual(sum(x[1][0]==201 for x in results),1)
            self.assertEqual(self.client.analyze.call_count,1)
            self.assertTrue(all(x[1][1]['durable'] for x in results))
        finally: release.set()
        self.wait_complete(task)
    def test_remote_endpoint_has_same_durable_dedup_contract(self):
        status,requested=self.request('/api/capture',b'{}',{'Content-Type':'application/json'})
        self.assertEqual(status,202); task=requested['id']
        _,(status,ack)=self.upload(task,remote=True)
        self.assertEqual(status,201); self.assertEqual(ack['capture_id'],task)
        self.wait_complete(task)
        _,(status,ack)=self.upload(task,remote=True)
        self.assertEqual(status,200); self.assertEqual(self.client.analyze.call_count,1)
    def test_duplicate_after_ack_lost_does_not_call_model_again(self):
        task=str(uuid.uuid4()); self.upload(task)  # simulate caller losing/ignoring this ACK
        self.wait_complete(task)
        self.upload(task)
        self.assertEqual(self.client.analyze.call_count,1)
    def test_different_payload_same_id_is_conflict(self):
        task,_=self.upload(); self.wait_complete(task)
        _,(status,body)=self.upload(task,image=b'\xff\xd8different\xff\xd9')
        self.assertEqual(status,409); self.assertFalse(body['durable'])
        self.assertEqual(self.state.store.get(task)['digest'],hashlib.sha256(IMAGE).hexdigest())
    def test_profile_mismatch_is_not_accepted(self):
        _,(status,body)=self.upload(profile='written')
        self.assertEqual(status,409); self.client.analyze.assert_not_called()
    def test_invalid_capture_id_does_not_persist(self):
        _,(status,_)=self.upload('not-uuid')
        self.assertEqual(status,400); self.assertEqual(self.state.store.snapshot()['counts'],{})
    def test_storage_failure_returns_no_durable_ack(self):
        with mock.patch.object(self.state.store,'accept',side_effect=sqlite3.OperationalError('disk full')):
            _,(status,body)=self.upload()
        self.assertEqual(status,503); self.assertFalse(body['durable']); self.client.analyze.assert_not_called()
    def test_model_timeout_is_uncertain_and_not_retried(self):
        self.client.analyze.side_effect=TimeoutError('secret provider message')
        task,_=self.upload()
        eventually(lambda:self.state.store.get(task)['state']=='uncertain')
        self.server.wake.set(); time.sleep(0.1)
        self.assertEqual(self.client.analyze.call_count,1)
        status,_=self.request('/api/retry',json.dumps({'id':task}).encode(),{'Content-Type':'application/json'})
        self.assertEqual(status,409)
        self.client.analyze.side_effect=None
        status,_=self.request('/api/retry',json.dumps({'id':task,'confirm_uncertain':True}).encode(),{'Content-Type':'application/json'})
        self.assertEqual(status,200); self.wait_complete(task)
        self.assertEqual(self.client.analyze.call_count,2)
    def test_old_arrival_after_newer_never_overwrites_current(self):
        newer,_=self.upload(sequence=20); self.wait_complete(newer)
        older,_=self.upload(sequence=19)
        eventually(lambda:self.state.store.get(older)['state']=='complete')
        self.assertEqual(self.state.analysis()['current']['id'],newer)
        self.assertIn(newer,self.state.latest_answer.read_text()); self.assertNotIn(older,self.state.latest_answer.read_text())
    def test_failed_new_task_does_not_masquerade_as_previous_answer(self):
        first,_=self.upload(); self.wait_complete(first)
        self.client.analyze.side_effect=ValueError('bad model result')
        second,_=self.upload(sequence=2)
        eventually(lambda:self.state.store.get(second)['state']=='paused')
        eventually(lambda:'不是本次答案' in self.state.latest_answer.read_text())
        result=self.state.analysis()
        self.assertEqual(result['answer'],''); self.assertEqual(result['previous']['id'],first)
        self.assertIn(second,self.state.latest_answer.read_text())
        self.assertEqual(self.state.latest_speech.read_text().strip(),'')
    def test_render_error_does_not_revoke_model_completion(self):
        with mock.patch.object(self.state,'render',side_effect=OSError('disk full')):
            task,_=self.upload(); eventually(lambda:self.state.store.get(task)['state']=='complete')
        self.upload(task)
        self.assertEqual(self.client.analyze.call_count,1)
    def test_history_error_does_not_retry_model(self):
        with mock.patch.object(self.state,'archive',side_effect=OSError('history full')):
            task,_=self.upload(); self.wait_complete(task)
        self.assertEqual(self.client.analyze.call_count,1)
        self.assertEqual(self.state.store.get(task)['state'],'complete')
    def test_health_reports_identity_and_storage_not_real_model_verification(self):
        status,body=self.request('/api/health')
        self.assertEqual(status,200); self.assertEqual(body['protocol'],PROTOCOL)
        self.assertEqual(body['service'],'lanshot-receiver')
        self.assertEqual(body['analyzer'],'configured_not_verified')
        self.client.analyze.assert_not_called()
    def test_health_degraded_on_storage_failure(self):
        with mock.patch.object(self.state,'storage_ready',return_value=False):
            _,health=self.request('/api/health')
        self.assertEqual(health['status'],'degraded'); self.assertEqual(health['storage'],'failed')
    def test_cross_origin_request_rejected(self):
        status,_=self.request('/api/capture',b'{}',{'Content-Type':'application/json','Origin':'https://attacker.example'})
        self.assertEqual(status,403)
    def test_auth_token_wrong_rejected(self):
        with mock.patch.dict(os.environ,{'LANSHOT_LOCAL_TOKEN':'secret-for-test'}):
            status,_=self.request('/api/status')
        self.assertEqual(status,401)
    def test_stale_display_ack_rejected(self):
        task,_=self.upload(); self.wait_complete(task); version=self.state.analysis()['version']
        status,_=self.request('/api/displayed',json.dumps({'id':task,'version':version-1}).encode(),{'Content-Type':'application/json'})
        self.assertEqual(status,409)
        status,_=self.request('/api/displayed',json.dumps({'id':task,'version':version}).encode(),{'Content-Type':'application/json'})
        self.assertEqual(status,200)
    def test_unconfigured_receiver_keeps_prompt_and_does_not_fake_completion(self):
        self.server.analyzer=None
        task,(status,ack)=self.upload()
        self.assertEqual(status,201); self.assertTrue(ack['durable'])
        job=self.state.store.get(task)
        self.assertEqual(job['state'],'unconfigured')
        self.assertEqual(job['prompt'],self.state.default_prompt)
        self.client.analyze.assert_not_called()

    def test_status_endpoint_excludes_answer(self):
        task,_=self.upload(); self.wait_complete(task)
        _,body=self.request('/api/status'); self.assertNotIn('synthetic answer',json.dumps(body))
    def test_real_sender_to_receiver_protocol(self):
        spool=self.root/'sender'; spool.mkdir()
        config=sender.Config(self.url,spool_dir=spool,retry_delays=())
        service=sender.SenderService(config,FakeScreenshotter(spool),sender.HTTPClient(config))
        task=service.handle_manual_capture(); service._retry_pending_once()
        self.assertEqual(service.store.get(task)['state'],'delivered')
        self.wait_complete(task); self.assertEqual(self.client.analyze.call_count,1)

class ProtocolAckTests(unittest.TestCase):
    def test_legacy_receiver_is_rejected_before_upload_post(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); config=sender.Config('http://127.0.0.1:8788',spool_dir=root,retry_delays=())
            response=mock.MagicMock(); response.__enter__.return_value=response
            response.status=200; response.read.return_value=b'{"status":"ok"}'
            opener=mock.Mock(return_value=response)
            client=sender.HTTPClient(config,opener=opener)
            service=sender.SenderService(config,FakeScreenshotter(root),client)
            task=service.handle_manual_capture(); service._retry_pending_once()
            self.assertEqual(opener.call_count,1)
            self.assertEqual(opener.call_args.args[0].get_method(),'GET')
            self.assertEqual(service.store.get(task)['state'],'paused')

    def test_wrong_200_body_keeps_sender_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); config=sender.Config('http://127.0.0.1:8788',spool_dir=root,retry_delays=())
            response=mock.MagicMock(); response.__enter__.return_value=response
            response.status=200; response.read.side_effect=[json.dumps({"service":"lanshot-receiver","protocol":PROTOCOL,"profile":"default","storage":"ready","worker":"running","status":"ready"}).encode(), b'{"status":"ok"}']
            client=sender.HTTPClient(config,opener=mock.Mock(return_value=response))
            service=sender.SenderService(config,FakeScreenshotter(root),client)
            task=service.handle_manual_capture(); service._retry_pending_once()
            self.assertEqual(service.store.get(task)['state'],'paused')
            self.assertEqual(service.store.get(task)['payload'],IMAGE)

class SubprocessCrashTests(unittest.TestCase):
    def test_sigkill_preserves_claim_and_requires_uncertain_recovery(self):
        import select
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'tasks.db'; task=str(uuid.uuid4())
            code="from pathlib import Path; from reliability import TaskStore; import sys,time; s=TaskStore(Path(sys.argv[1])); s.accept(sys.argv[2],b'fixture',origin='s',origin_seq=1,profile='default',prompt='test',configured=True); s.claim(model=True); print('ready',flush=True); time.sleep(30)"
            child=subprocess.Popen([sys.executable,'-c',code,str(path),task],cwd=ROOT,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
            try:
                self.assertTrue(select.select([child.stdout],[],[],5)[0])
                self.assertEqual(child.stdout.readline().strip(),'ready')
                child.kill(); child.wait(timeout=5)
                store=TaskStore(path); self.assertEqual(store.recover_model('default'),1)
                self.assertEqual(store.get(task)['payload'],b'fixture')
                self.assertEqual(store.get(task)['state'],'uncertain')
                self.assertIsNone(store.claim(model=True))
            finally:
                if child.poll() is None: child.kill(); child.wait(timeout=5)
                child.stdout.close(); child.stderr.close()

    def test_abrupt_exit_after_model_claim_recovers_as_uncertain(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'tasks.db'; task=str(uuid.uuid4())
            code="""from pathlib import Path
from reliability import TaskStore
import os,sys
s=TaskStore(Path(sys.argv[1])); s.accept(sys.argv[2],b'fixture',origin='s',origin_seq=1,profile='default',prompt='test',configured=True)
s.claim(model=True); os._exit(70)
"""
            result=subprocess.run([sys.executable,'-c',code,str(path),task],cwd=ROOT,timeout=5)
            self.assertEqual(result.returncode,70)
            store=TaskStore(path); self.assertEqual(store.recover_model('default'),1)
            self.assertEqual(store.get(task)['state'],'uncertain')
            self.assertIsNone(store.claim(model=True))
    def test_abrupt_exit_inside_uncommitted_transaction_is_rolled_back(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'tasks.db'; TaskStore(path)
            code="""from pathlib import Path
from reliability import TaskStore
import os,sys
s=TaskStore(Path(sys.argv[1]))
with s.tx() as c:
 c.execute(\"INSERT INTO meta VALUES ('uncommitted','should-not-exist')\")
 os._exit(71)
"""
            result=subprocess.run([sys.executable,'-c',code,str(path)],cwd=ROOT,timeout=5)
            self.assertEqual(result.returncode,71)
            with TaskStore(path).tx() as con:
                self.assertIsNone(con.execute("SELECT * FROM meta WHERE key='uncommitted'").fetchone())
