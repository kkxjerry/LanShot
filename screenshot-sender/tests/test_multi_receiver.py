import contextlib
import hashlib
import json
import os
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from unittest import mock
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from reliability import TaskStore, PROTOCOL, Conflict, RetryDecision, LeaseLost, InstanceLock
from multi_receiver import (Endpoint, MultiReceiverClient, HTTPBackend, EmbeddedBackend,
                            OutcomeUnknown, NoReceiverAvailable, EndpointMismatch, NoRedirect, build_client)
from receiver_service import ReceiverState, AnalysisService, create_server
from receiver_runtime import ReceiverRuntime
from display_bridge import DisplayBridge
import manage_services as manager
from sender_service import Config, SenderService

IMAGE=b'\xff\xd8synthetic-test-only\xff\xd9'
PROMPT='synthetic prompt'
SIG=hashlib.sha256(PROMPT.encode()).hexdigest()


def eventually(function, timeout=5):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        if function():return
        time.sleep(.02)
    raise AssertionError('condition not reached')


class StubBackend:
    def __init__(self,name,cluster=None,kind='local',shared=None):
        self.cluster=cluster or str(uuid.uuid4())
        self.endpoint=Endpoint(name,kind,'' if kind=='embedded' else ('https://example.test' if kind=='remote' else 'http://127.0.0.1:43210'),
                               'LANSHOT_REMOTE_TOKEN' if kind=='remote' else 'LANSHOT_LOCAL_TOKEN',self.cluster)
        self.rows=shared if shared is not None else {}
        self.posts=0;self.probes=0;self.failure=None;self.available=True;self.bad_ack=False;self.bad_prompt=False
    def health(self):
        self.probes+=1
        if not self.available:raise ConnectionRefusedError()
        return {'service':'lanshot-receiver','protocol':PROTOCOL,'route_protocol':1,'profile':'default',
                'prompt_sha256':'wrong' if self.bad_prompt else SIG,'cluster_id':self.cluster,'status':'ready',
                'storage':'ready','analyzer':'configured_not_verified'}
    def submit(self,job):
        self.posts+=1
        if self.failure=='refused':raise ConnectionRefusedError()
        if self.failure!='timeout_before_commit':
            self.rows[job['id']]={**{k:job[k] for k in ('id','origin','origin_seq','profile','created','updated','expires','digest')},
                                  'state':'complete','answer':'answer-'+job['id'],'code':''}
        if self.failure in ('timeout','timeout_before_commit'):raise TimeoutError()
        return {'service':'lanshot-receiver','protocol':PROTOCOL,'capture_id':job['id'],
                'sha256':'bad' if self.bad_ack else job['digest'],'cluster_id':self.cluster,'durable':True}
    def lookup(self,task_id):
        if not self.available:raise ConnectionRefusedError()
        row=self.rows.get(task_id)
        return {'service':'lanshot-receiver','protocol':PROTOCOL,'cluster_id':self.cluster,'durable':True,'task':row} if row else None


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.store=TaskStore(self.root/'sender.db')
    def router(self,backends,**kwargs):
        return MultiReceiverClient(self.store,backends,profile='default',prompt_sha256=SIG,**kwargs)
    def job(self,router):
        task=str(uuid.uuid4());self.store.enqueue(task,IMAGE,target=router.queue_target)
        return self.store.claim(target=router.queue_target)
    def test_embedded_priority_no_broadcast(self):
        a,b,c=StubBackend('embedded',kind='embedded'),StubBackend('local'),StubBackend('remote',kind='remote')
        router=self.router([a,b,c],allow_remote=True);job=self.job(router);router.upload_record(job)
        self.assertEqual([a.posts,b.posts,c.posts],[1,0,0])
    def test_preflight_failure_uses_second_service(self):
        a,b=StubBackend('a'),StubBackend('b');a.available=False
        router=self.router([a,b]);router.upload_record(self.job(router))
        self.assertEqual([a.posts,b.posts],[0,1])
    def test_connection_refused_before_post_can_switch_independent_cluster(self):
        a,b=StubBackend('a'),StubBackend('b');a.failure='refused'
        router=self.router([a,b]);job=self.job(router);router.upload_record(job)
        self.assertEqual(router.book.get(job['id'])['cluster_id'],b.cluster)
    def test_timeout_never_sends_to_independent_backup(self):
        a,b=StubBackend('a'),StubBackend('remote',kind='remote');a.failure='timeout'
        router=self.router([a,b],allow_remote=True);job=self.job(router)
        with self.assertRaises(OutcomeUnknown):router.upload_record(job)
        self.assertEqual(b.posts,0);self.assertEqual(router.book.get(job['id'])['cluster_id'],a.cluster)
    def test_lost_ack_reconciles_across_same_cluster_replica(self):
        a=StubBackend('a');a.failure='timeout';b=StubBackend('b',cluster=a.cluster,shared=a.rows)
        router=self.router([a,b]);job=self.job(router);ack=router.upload_record(job)
        self.assertTrue(ack['reconciled']);self.assertEqual([a.posts,b.posts],[1,0])
    def test_timeout_without_commit_retries_only_same_cluster(self):
        a=StubBackend('a');a.failure='timeout_before_commit';b=StubBackend('b',cluster=a.cluster,shared=a.rows)
        c=StubBackend('remote',kind='remote');router=self.router([a,b,c],allow_remote=True)
        router.upload_record(self.job(router));self.assertEqual([a.posts,b.posts,c.posts],[1,1,0])
    def test_route_pin_survives_sender_restart(self):
        a,b=StubBackend('a'),StubBackend('b');a.failure='timeout'
        router=self.router([a,b]);job=self.job(router)
        with self.assertRaises(OutcomeUnknown):router.upload_record(job)
        self.store.release(job,RetryDecision('retry','test',0))
        fresh=TaskStore(self.root/'sender.db');new=MultiReceiverClient(fresh,[a,b],profile='default',prompt_sha256=SIG)
        claimed=fresh.claim(target=new.queue_target);ack=new.upload_record(claimed)
        self.assertTrue(ack['reconciled']);self.assertEqual(a.posts,1);self.assertEqual(b.posts,0)
    def test_accepted_owner_offline_does_not_reassign(self):
        a,b=StubBackend('a'),StubBackend('b');router=self.router([a,b]);job=self.job(router);router.upload_record(job)
        a.available=False
        with self.assertRaises(OutcomeUnknown):router.upload_record(job)
        self.assertEqual(b.posts,0)
    def test_remote_disabled_never_contacted(self):
        a=StubBackend('remote',kind='remote');router=self.router([a]);job=self.job(router)
        with self.assertRaises(NoReceiverAvailable):router.upload_record(job)
        self.assertEqual((a.probes,a.posts),(0,0))
    def test_remote_used_when_local_fails_before_submission(self):
        a,b=StubBackend('a'),StubBackend('remote',kind='remote');a.available=False
        router=self.router([a,b],allow_remote=True);router.upload_record(self.job(router));self.assertEqual(b.posts,1)
    def test_invalid_ack_keeps_pin_no_independent_failover(self):
        a,b=StubBackend('a'),StubBackend('b');a.bad_ack=True
        router=self.router([a,b]);job=self.job(router)
        with self.assertRaises(OutcomeUnknown):router.upload_record(job)
        self.assertEqual(b.posts,0)
    def test_prompt_mismatch_never_posts(self):
        a=StubBackend('a');a.bad_prompt=True;router=self.router([a])
        with self.assertRaises(NoReceiverAvailable):router.upload_record(self.job(router))
        self.assertEqual(a.posts,0)
    def test_cluster_recreated_cannot_absorb_old_pin(self):
        a=StubBackend('a');a.failure='timeout';router=self.router([a]);job=self.job(router)
        with self.assertRaises(OutcomeUnknown):router.upload_record(job)
        a.cluster=str(uuid.uuid4());a.failure=None
        with self.assertRaises(OutcomeUnknown):router.upload_record(job)
        self.assertEqual(a.posts,1)
    def test_result_digest_tamper_not_displayed(self):
        a=StubBackend('a');router=self.router([a]);job=self.job(router);router.upload_record(job)
        a.rows[job['id']]['digest']='tampered'
        self.assertEqual(router.lookup_result(job['id']),(None,False))
    def test_policy_change_does_not_redirect_old_queue(self):
        a,b=StubBackend('a'),StubBackend('b');r1=self.router([a]);r2=self.router([a,b])
        self.store.enqueue(str(uuid.uuid4()),IMAGE,target=r1.queue_target)
        self.assertNotEqual(r1.queue_target,r2.queue_target)
        self.assertIsNone(self.store.claim(target=r2.queue_target))
    def test_stale_sender_lease_cannot_pin_route(self):
        a=StubBackend('a');router=self.router([a]);job=self.job(router)
        with self.store.tx() as con:con.execute('UPDATE jobs SET lease_until=0')
        self.store.claim(target=router.queue_target)
        with self.assertRaises(LeaseLost):router.upload_record(job)
        self.assertEqual(a.posts,0)
    def test_circuit_opens_and_probe_recovers_after_cooldown(self):
        now=[0.];a=StubBackend('a');a.available=False;router=self.router([a],clock=lambda:now[0])
        for _ in range(3):
            with self.assertRaises(NoReceiverAvailable):router.health()
        self.assertEqual(a.probes,2)
        a.available=True;now[0]=11;self.assertEqual(router.health()['cluster_id'],a.cluster)
    def test_insecure_remote_rejected(self):
        with self.assertRaises(ValueError):Endpoint('remote','remote','http://example.test',token_env='REMOTE_TOKEN')
    def test_local_token_never_reused_for_remote(self):
        with self.assertRaises(ValueError):Endpoint('remote','remote','https://example.test')
    def test_non_loopback_cannot_masquerade_as_local(self):
        with self.assertRaises(ValueError):Endpoint('local','local','http://example.test')
    def test_url_credentials_rejected(self):
        with self.assertRaises(ValueError):Endpoint('remote','remote','https://u:p@example.test',token_env='REMOTE_TOKEN')
    def test_redirect_refused(self):
        with self.assertRaises(EndpointMismatch):NoRedirect().redirect_request(None,None,302,'',{},'https://other.test')
    def test_remote_requires_token_before_network(self):
        b=HTTPBackend(Endpoint('remote','remote','https://example.test',token_env='MISSING_TEST_REMOTE_TOKEN'))
        with mock.patch.dict(os.environ,{},clear=True),mock.patch.object(b.opener,'open') as op:
            with self.assertRaises(EndpointMismatch):b.health()
            op.assert_not_called()


class RealReplicaTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.resources=[];self.addCleanup(self.close_resources)
        self.store=TaskStore(self.root/'sender.sqlite3')
    def close_resources(self):
        for kind,item in reversed(self.resources):
            if kind=='server':item.shutdown();item.server_close()
            elif kind=='thread':item.join(2)
            else:item.close()
    def state(self,root=None):
        root=root or self.root/'receiver';state=ReceiverState(root/'latest.jpg',root/'latest.txt');state.default_prompt=PROMPT;return state
    def server(self,state,client,node,context=None):
        analyzer=AnalysisService(state,client,PROMPT)
        server=create_server('127.0.0.1',0,state,analyzer,node=node,tls_context=context)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        self.resources.extend([('thread',thread),('server',server)])
        return server
    def http(self,server,name):
        return HTTPBackend(Endpoint(name,'local',f'http://127.0.0.1:{server.server_port}',expected_cluster=server.runtime.cluster_id))
    def test_two_http_receivers_share_one_model_execution(self):
        client=mock.Mock();client.analyze.return_value='done'
        a=self.server(self.state(),client,'a');b=self.server(self.state(),client,'b')
        router=MultiReceiverClient(self.store,[self.http(a,'a'),self.http(b,'b')],profile='default',prompt_sha256=SIG)
        task=str(uuid.uuid4());self.store.enqueue(task,IMAGE,target=router.queue_target);job=self.store.claim(target=router.queue_target)
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda backend:backend.submit(job),[self.http(a,'a'),self.http(b,'b')]*2))
        eventually(lambda:a.state.store.get(task)['state']=='complete')
        self.assertEqual(client.analyze.call_count,1)
    def test_primary_http_shutdown_backup_delivers(self):
        client=mock.Mock();client.analyze.return_value='backup result'
        a=self.server(self.state(),client,'a');b=self.server(self.state(),client,'b')
        first,second=self.http(a,'a'),self.http(b,'b');a.shutdown();a.server_close()
        router=MultiReceiverClient(self.store,[first,second],profile='default',prompt_sha256=SIG)
        task=str(uuid.uuid4());self.store.enqueue(task,IMAGE,target=router.queue_target);job=self.store.claim(target=router.queue_target)
        router.upload_record(job);eventually(lambda:b.state.store.get(task)['state']=='complete')
        self.assertEqual(client.analyze.call_count,1);self.assertEqual(router.book.get(task)['backend'],'b')
    def test_embedded_runs_without_any_http_listener(self):
        client=mock.Mock();client.analyze.return_value='in process'
        state=self.state();runtime=ReceiverRuntime(state,AnalysisService(state,client,PROMPT),node='embedded');self.resources.append(('runtime',runtime))
        backend=EmbeddedBackend(Endpoint('embedded','embedded',expected_cluster=runtime.cluster_id),runtime)
        router=MultiReceiverClient(self.store,[backend],profile='default',prompt_sha256=SIG,local_state=state)
        task=str(uuid.uuid4());self.store.enqueue(task,IMAGE,target=router.queue_target);job=self.store.claim(target=router.queue_target)
        with mock.patch('socket.create_connection',side_effect=AssertionError('unexpected network')):
            router.upload_record(job);eventually(lambda:state.store.get(task)['state']=='complete')
        self.assertEqual(client.analyze.call_count,1)
    def test_standby_does_not_recover_live_model_as_uncertain(self):
        started,release=threading.Event(),threading.Event();self.addCleanup(release.set)
        def analyze(*args):started.set();release.wait(3);return 'done'
        client=mock.Mock();client.analyze.side_effect=analyze
        a=self.server(self.state(),client,'a');backend=self.http(a,'a')
        task=str(uuid.uuid4());self.store.enqueue(task,IMAGE,target='x');job=self.store.claim(target='x');backend.submit(job)
        self.assertTrue(started.wait(2));b=self.server(self.state(),client,'b')
        self.assertEqual(b.state.store.get(task)['state'],'processing');self.assertEqual(client.analyze.call_count,1)
        release.set();eventually(lambda:b.state.store.get(task)['state']=='complete')
    def test_local_replicas_reject_different_prompt(self):
        state=self.state();runtime=ReceiverRuntime(state,AnalysisService(state,mock.Mock(),PROMPT),node='a');self.resources.append(('runtime',runtime))
        other=self.state()
        with self.assertRaises(Conflict):ReceiverRuntime(other,AnalysisService(other,mock.Mock(),'different'),node='b')
    def test_result_lookup_is_read_only_and_validates_id(self):
        client=mock.Mock();client.analyze.return_value='done';s=self.server(self.state(),client,'a');backend=self.http(s,'a')
        self.assertIsNone(backend.lookup(str(uuid.uuid4())));client.analyze.assert_not_called()
        with self.assertRaises(urllib.error.HTTPError) as error:backend.lookup('not-a-uuid')
        self.assertEqual(error.exception.code,400);error.exception.close()
    def test_authenticated_https_remote_after_local_preflight_failure(self):
        cert,key=self.root/'cert.pem',self.root/'key.pem'
        subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1','-keyout',str(key),'-out',str(cert),
                        '-subj','/CN=localhost','-addext','subjectAltName=DNS:localhost,IP:127.0.0.1'],check=True,capture_output=True)
        context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);context.load_cert_chain(cert,key)
        client=mock.Mock();client.analyze.return_value='remote result';s=self.server(self.state(self.root/'remote'),client,'remote',context)
        endpoint=Endpoint('remote','remote',f'https://127.0.0.1:{s.server_port}',token_env='TEST_REMOTE_TOKEN',expected_cluster=s.runtime.cluster_id)
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect(),urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=str(cert))))
        remote=HTTPBackend(endpoint,opener=opener)
        local=StubBackend('local');local.available=False
        router=MultiReceiverClient(self.store,[local,remote],profile='default',prompt_sha256=SIG,allow_remote=True)
        task=str(uuid.uuid4());self.store.enqueue(task,IMAGE,target=router.queue_target);job=self.store.claim(target=router.queue_target)
        with mock.patch.dict(os.environ,{'LANSHOT_LOCAL_TOKEN':'test-only-token','TEST_REMOTE_TOKEN':'test-only-token'}):
            router.upload_record(job);eventually(lambda:s.state.store.get(task)['state']=='complete')
            row,fresh=router.lookup_result(task)
        self.assertTrue(fresh);self.assertEqual(row['answer'],'remote result');self.assertEqual(client.analyze.call_count,1)
    def test_receiver_unauthorized_request_cannot_query_results(self):
        client=mock.Mock();s=self.server(self.state(),client,'a');backend=self.http(s,'a')
        with mock.patch.dict(os.environ,{'LANSHOT_LOCAL_TOKEN':'server-token','EMPTY_CLIENT_TOKEN':''}):
            backend.endpoint=Endpoint('a','local',backend.endpoint.url,token_env='EMPTY_CLIENT_TOKEN')
            with self.assertRaises(urllib.error.HTTPError) as error:backend.health()
            self.assertEqual(error.exception.code,401);error.exception.close()


class NativeDisplayBridgeTests(unittest.TestCase):
    router = RoutingTests.router
    job = RoutingTests.job
    def setUp(self):
        RoutingTests.setUp(self);self.backend=StubBackend('local');self.router_instance=self.router([self.backend]);self.bridge=DisplayBridge(self.router_instance,self.root/'display')
    def complete(self):
        job=self.job(self.router_instance);self.router_instance.upload_record(job);self.store.delivered(job);return job
    def test_native_projection_contains_completed_task(self):
        job=self.complete();view=self.bridge.step();self.assertIn('answer-'+job['id'],view['text']);self.assertEqual(view['current']['state'],'complete')
    def test_new_queued_task_labels_previous_answer(self):
        first=self.complete();self.bridge.step();second=self.job(self.router_instance);view=self.bridge.step()
        self.assertEqual(view['current']['id'],second['id']);self.assertIn('不是本次答案',view['text']);self.assertIn(first['id'],view['text'])
    def test_old_late_result_cannot_replace_new_current(self):
        first=self.complete();self.bridge.step();second=self.complete();view=self.bridge.step();self.assertEqual(view['current']['id'],second['id'])
        self.backend.rows[first['id']]['answer']='LATE OLD ANSWER';view=self.bridge.step();self.assertNotIn('LATE OLD ANSWER',view['text'])
    def test_display_receipt_requires_matching_stream_id_version_and_task(self):
        job=self.complete();view=self.bridge.step();ack={'consumer':'native-overlay','stream_id':view['stream_id'],'task_id':job['id'],'version':view['version']}
        path=self.root/'display/display_ack.json';path.write_text(json.dumps(ack));self.assertTrue(self.bridge.consume_receipt(view))
        for key,value in [('stream_id','wrong'),('task_id','wrong'),('version',view['version']+1),('consumer','tk')]:
            path.write_text(json.dumps({**ack,key:value}));self.assertFalse(self.bridge.consume_receipt(view))
    def test_capture_failure_does_not_masquerade_as_previous_success(self):
        first=self.complete();self.bridge.step();failed=str(uuid.uuid4());status={'last_capture':{'id':failed,'state':'failed','code':'CaptureError','at':time.time()+.01}}
        (self.root/'sender_status.json').write_text(json.dumps(status));view=self.bridge.step()
        self.assertEqual(view['current']['id'],failed);self.assertIn('本次截图失败',view['text']);self.assertIn('不是本次答案',view['text'])
    def test_projection_version_stable_when_content_unchanged(self):
        self.complete();a=self.bridge.step();b=self.bridge.step();self.assertEqual(a['version'],b['version']);self.assertGreaterEqual(b['at'],a['at'])
    def test_offline_result_is_labeled_cached(self):
        self.complete();self.bridge.step();self.backend.available=False;view=self.bridge.step();self.assertIn('未实时确认',view['text'])
    def test_manual_menu_request_is_idempotent(self):
        state=ReceiverState(self.root/'receiver/latest.jpg');self.router_instance.local_state=state
        directory=self.root/'display';directory.mkdir();task=str(uuid.uuid4());(directory/'capture_request.json').write_text(json.dumps({'id':task,'at':time.time()}))
        self.bridge.process_capture_request();self.bridge.process_capture_request()
        self.assertEqual(state.store.snapshot()['counts'],{'awaiting_capture':1})
    def test_old_menu_click_not_replayed_after_restart(self):
        state=ReceiverState(self.root/'receiver/latest.jpg');self.router_instance.local_state=state
        directory=self.root/'display';directory.mkdir();(directory/'capture_request.json').write_text(json.dumps({'id':str(uuid.uuid4()),'at':time.time()-600}))
        self.bridge.process_capture_request();self.assertEqual(state.store.snapshot()['counts'],{})

class SettingsAndCleanupTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name);self.prompt=self.root/'prompt.txt';self.prompt.write_text(PROMPT)
        self.settings=self.root/'settings.json'
    def configure(self,**kwargs):
        manager.configure(self.settings,state_dir=self.root/'state',port=8788,profile='default',prompt=self.prompt,**kwargs)
        return manager.read_settings(self.settings)
    def test_redundant_launches_two_receivers_sender_and_display(self):
        data=self.configure();self.assertEqual(manager.active_roles(data),('receiver','receiver_backup','sender','display'))
        spec=json.loads(Path(data['routes_file']).read_text());self.assertEqual(len(spec['endpoints']),3);self.assertFalse(spec['allow_remote'])
    def test_monolith_needs_no_http_receiver_process(self):
        data=self.configure(mode='monolith');self.assertEqual(manager.active_roles(data),('sender','display'))
        self.assertEqual([x['kind'] for x in json.loads(Path(data['routes_file']).read_text())['endpoints']],['embedded'])
    def test_sender_and_native_bridge_share_display_directory(self):
        data=self.configure();config=Config.load(Path(data['sender_config']));self.assertEqual(config.display_dir,Path(data['display_dir']))
    def test_add_remote_requires_explicit_consent(self):
        self.configure()
        with self.assertRaises(Exception):manager.add_remote(self.settings,url='https://remote.example',token_env='REMOTE_TOKEN',allow_remote_images=False)
    def test_remote_config_contains_references_not_secret(self):
        data=self.configure()
        with mock.patch.dict(os.environ,{'REMOTE_TOKEN':'DO-NOT-WRITE-SECRET'}):
            manager.add_remote(self.settings,url='https://remote.example',token_env='REMOTE_TOKEN',allow_remote_images=True)
        text=Path(data['routes_file']).read_text();self.assertIn('REMOTE_TOKEN',text);self.assertNotIn('DO-NOT-WRITE-SECRET',text)
    def test_add_remote_refuses_running_configuration(self):
        data=self.configure();data['enabled']=True;self.settings.write_text(json.dumps(data))
        with self.assertRaises(Conflict):manager.add_remote(self.settings,url='https://remote.example',token_env='REMOTE_TOKEN',allow_remote_images=True)
    def test_old_overlay_binary_rejected(self):
        data=self.configure()
        with self.assertRaises(Exception):manager.validate_overlay(data)
    def test_tk_removed_native_display_retained(self):
        root=Path(__file__).resolve().parents[1];source=(root/'diagnostics.py').read_text();self.assertNotIn('tkinter',source);self.assertNotIn('def gui(',source)
        swift=(root.parent/'capture-exclusion-demo/CaptureExclusionDemo.swift').read_text()
        for value in ('latest_state.json','display_ack.json','--lanshot-display-dir','func pageDown','func pageUp'):
            self.assertIn(value,swift)
        self.assertIn('--open-display',(root/'start_assessment.command').read_text())
    def test_build_client_starts_real_embedded_runtime_no_network(self):
        data=self.configure(mode='monolith');config=Config.load(Path(data['sender_config']))
        with mock.patch.dict(os.environ,{'DASHSCOPE_API_KEY':'test-not-used'}):
            client=build_client(config)
        self.addCleanup(client.close);self.assertEqual(client.health()['node'],'embedded')
    def test_read_only_display_client_cannot_submit(self):
        data=self.configure();config=Config.load(Path(data['sender_config']));client=build_client(config,start_embedded=False);self.addCleanup(client.close)
        self.assertEqual(client.runtimes,[])
        with self.assertRaises(Conflict):client.backends[0].submit({'id':'not-important'})


class ProcessFailoverTests(unittest.TestCase):
    def test_sigkill_active_owner_marks_uncertain_without_another_model_call(self):
        import select
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);task=str(uuid.uuid4())
            code = """import sys,time
from pathlib import Path
from receiver_service import ReceiverState,AnalysisService
from receiver_runtime import ReceiverRuntime
class Model:
 def analyze(self,*args):
  print('MODEL_STARTED',flush=True);time.sleep(30);return 'late'
r=Path(sys.argv[1]);s=ReceiverState(r/'latest.jpg');s.default_prompt='synthetic prompt'
s.store.accept(sys.argv[2],b'image',origin='source',origin_seq=1,profile='default',prompt='synthetic prompt',configured=True)
x=ReceiverRuntime(s,AnalysisService(s,Model(),'synthetic prompt'),node='primary')
time.sleep(30)
"""
            process=subprocess.Popen([sys.executable,'-c',code,str(root),task],cwd=Path(__file__).resolve().parents[1],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
            backup=None
            try:
                self.assertTrue(select.select([process.stdout],[],[],5)[0]);self.assertEqual(process.stdout.readline().strip(),'MODEL_STARTED')
                state=ReceiverState(root/'latest.jpg');state.default_prompt=PROMPT
                client=mock.Mock();client.analyze.return_value='must-not-be-called'
                backup=ReceiverRuntime(state,AnalysisService(state,client,PROMPT),node='backup')
                self.assertEqual(state.store.get(task)['state'],'processing')
                process.kill();process.wait(3)
                eventually(lambda:state.store.get(task)['state']=='uncertain')
                client.analyze.assert_not_called()
            finally:
                if process.poll() is None:process.kill();process.wait(3)
                process.stdout.close();process.stderr.close()
                if backup:backup.close()
    def test_queued_work_survives_owner_process_death(self):
        import select
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);state=ReceiverState(root/'latest.jpg');state.default_prompt=PROMPT
            task=str(uuid.uuid4());state.store.accept(task,IMAGE,origin='s',origin_seq=1,profile='default',prompt=PROMPT,configured=True)
            code="from reliability import InstanceLock; from pathlib import Path; import sys,time; lock=InstanceLock(Path(sys.argv[1]));lock.__enter__();print('READY',flush=True);time.sleep(30)"
            process=subprocess.Popen([sys.executable,'-c',code,str(root/'analysis-owner.lock')],cwd=Path(__file__).resolve().parents[1],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
            runtime=None
            try:
                self.assertTrue(select.select([process.stdout],[],[],5)[0]);self.assertEqual(process.stdout.readline().strip(),'READY')
                client=mock.Mock();client.analyze.return_value='recovered queued job'
                runtime=ReceiverRuntime(state,AnalysisService(state,client,PROMPT),node='backup')
                self.assertEqual(state.store.get(task)['state'],'queued')
                process.kill();process.wait(3);eventually(lambda:state.store.get(task)['state']=='complete')
                self.assertEqual(client.analyze.call_count,1)
            finally:
                if process.poll() is None:process.kill();process.wait(3)
                process.stdout.close();process.stderr.close()
                if runtime:runtime.close()

if __name__=='__main__':unittest.main()


class ConfigurationConsistencyTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name); self.settings=self.root/'settings.json'
        prompt=self.root/'prompt.txt'; prompt.write_text(PROMPT)
        manager.configure(self.settings,state_dir=self.root/'state',port=8788,profile='default',prompt=prompt)
    def update(self,key,value):
        data=json.loads(self.settings.read_text());data[key]=value
        self.settings.write_text(json.dumps(data))
    def test_display_dir_mismatch_rejected(self):
        self.update('display_dir',str(self.root/'other-display'))
        with self.assertRaises(ValueError): manager.read_settings(self.settings)
    def test_routes_dir_mismatch_rejected(self):
        data=manager.read_settings(self.settings);path=Path(data['routes_file'])
        spec=json.loads(path.read_text());spec['receiver_dir']=str(self.root/'other-store')
        path.write_text(json.dumps(spec))
        with self.assertRaises(ValueError): manager.read_settings(self.settings)
    def test_backup_same_port_rejected(self):
        self.update('backup_port',8788)
        with self.assertRaises(ValueError): manager.read_settings(self.settings)
    def test_old_settings_start_rejected_before_any_mutation(self):
        data=json.loads(self.settings.read_text());data.pop('mode');self.settings.write_text(json.dumps(data))
        before=self.settings.read_bytes()
        with self.assertRaises(ValueError): manager.control('start',self.settings,dry_run=True)
        self.assertEqual(before,self.settings.read_bytes())
    def test_corrupt_local_store_does_not_prevent_remote_route_creation(self):
        data=manager.read_settings(self.settings);config=Config.load(Path(data['sender_config']))
        (Path(data['receiver_dir'])/'receiver_tasks.sqlite3').write_bytes(b'not a database')
        manager.add_remote(self.settings,url='https://receiver.example.test:9443',token_env='LANSHOT_REMOTE_TOKEN',allow_remote_images=True)
        router=build_client(config,start_embedded=False)
        self.addCleanup(router.close)
        self.assertIsNone(router.local_state)
        self.assertEqual(router.backends[-1].endpoint.kind,'remote')
        self.assertIsNotNone(router.store)
