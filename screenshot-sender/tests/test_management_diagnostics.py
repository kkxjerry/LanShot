import json
import os
import plistlib
import tempfile
import time
import unittest
import zipfile
import sys
from pathlib import Path
from unittest import mock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import manage_services as manager
import diagnostics
from reliability import BUILD, Conflict, atomic_json

class ManagerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name); self.settings=self.root/'settings.json'; self.prompt=self.root/'prompt.txt'; self.prompt.write_text('test')
        manager.configure(self.settings,state_dir=self.root/'state',port=8788,profile='default',prompt=self.prompt)
    def test_configure_is_disabled_and_separate_from_p0(self):
        data=manager.read_settings(self.settings)
        self.assertFalse(data['enabled']); self.assertEqual(data['port'],8788)
        self.assertIn('state',data['sender_config'])
    def test_configure_refuses_existing_settings(self):
        before=self.settings.read_bytes()
        with self.assertRaises(Conflict): manager.configure(self.settings,state_dir=self.root/'other',port=8789,profile='written',prompt=self.prompt)
        self.assertEqual(self.settings.read_bytes(),before)
    def test_plist_keeps_only_failed_processes_alive_and_throttles(self):
        plist=manager.launch_agent('sender',self.settings,self.root)
        self.assertEqual(plist['KeepAlive'],{'SuccessfulExit':False})
        self.assertEqual(plist['ThrottleInterval'],30)
        self.assertEqual(plist['Umask'],0o077)
        plistlib.loads(plistlib.dumps(plist))
    def test_plist_contains_no_credentials(self):
        with mock.patch.dict(os.environ,{'DASHSCOPE_API_KEY':'secret-key','LANSHOT_VOICE_TOKEN':'secret-token'}):
            plist=manager.launch_agent('receiver',self.settings,self.root)
        self.assertNotIn('secret',str(plist)); self.assertNotIn('EnvironmentVariables',plist)
    def test_start_dry_run_has_no_writes_or_launch_calls(self):
        before={p:p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        runner=mock.Mock()
        result=manager.control('start',self.settings,dry_run=True,runner=runner)
        self.assertEqual(result['side_effects'],'none'); runner.assert_not_called()
        self.assertEqual(before,{p:p.read_bytes() for p in self.root.rglob('*') if p.is_file()})
    def test_redundant_readiness_requires_both_receiver_nodes(self):
        data=manager.read_settings(self.settings);config=manager.Config.load(Path(data['sender_config']))
        now=time.time();config.spool_dir.mkdir(parents=True,exist_ok=True);Path(data['display_dir']).mkdir(parents=True,exist_ok=True)
        atomic_json(config.spool_dir/'sender_status.json',{'build':BUILD,'status':'running','at':now,'receiver':'ready','input_error':None})
        atomic_json(Path(data['display_dir'])/'display_status.json',{'build':BUILD,'status':'running','at':now})
        self.assertIsNone(manager.current_readiness(data))
        receiver_dir=Path(data['receiver_dir'])
        atomic_json(receiver_dir/'receiver_status.json',{'build':BUILD,'status':'running','at':now})
        self.assertIsNone(manager.current_readiness(data))
        atomic_json(receiver_dir/'receiver_backup_status.json',{'build':BUILD,'status':'running','at':now})
        self.assertEqual(manager.current_readiness(data)['status'],'ready')
    def test_stop_only_own_launchd_labels_and_persists_disabled(self):
        data=manager.read_settings(self.settings); data['enabled']=True; atomic_json(self.settings,data)
        runner=mock.Mock()
        with mock.patch('manage_services.sys.platform','darwin'):
            result=manager.control('stop',self.settings,runner=runner,launch_dir=self.root/'launch')
        self.assertEqual(result['status'],'stopped_by_user')
        self.assertFalse(manager.read_settings(self.settings)['enabled'])
        commands=[c.args[0] for c in runner.call_args_list]
        self.assertEqual(sum(c[1]=='bootout' for c in commands),4)
        self.assertTrue(all(c[1] in ('bootout','print') and 'com.lanshot.p1.' in c[2] for c in commands))
    def test_failed_stop_is_degraded_not_fake_stopped(self):
        runner=mock.Mock(return_value=mock.Mock(returncode=0))
        with mock.patch('manage_services.sys.platform','darwin'):
            result=manager.control('stop',self.settings,runner=runner,launch_dir=self.root/'launch')
        self.assertEqual(result['status'],'degraded')
        self.assertEqual(set(result['still_loaded']),set(manager.ROLES))

    def test_legacy_install_does_not_touch_p0_agents(self):
        import sender_service
        with mock.patch('sender_service.subprocess.run') as run:
            with self.assertRaises(sender_service.ConfigError): sender_service.install_launch_agent(self.settings)
            with self.assertRaises(sender_service.ConfigError): sender_service.uninstall_launch_agent()
        run.assert_not_called()

    def test_foreign_launchagent_not_removed(self):
        launch=self.root/'launch'; launch.mkdir()
        path=launch/(manager.LABELS['sender']+'.plist'); path.write_bytes(plistlib.dumps({'ProgramArguments':['other-app']}))
        with mock.patch('manage_services.sys.platform','darwin'):
            with self.assertRaises(Conflict): manager.control('uninstall',self.settings,runner=mock.Mock(),launch_dir=launch)
        self.assertTrue(path.exists())
    def test_restart_budget_stops_sixth_restart(self):
        path=self.root/'budget.json'
        for i in range(5): self.assertTrue(manager.restart_allowed(path,now=1000+i))
        self.assertFalse(manager.restart_allowed(path,now=1006))
        self.assertTrue(manager.restart_allowed(path,now=1400))
    def test_corrupt_restart_budget_fails_closed(self):
        path=self.root/'budget.json'; path.write_text('{')
        self.assertFalse(manager.restart_allowed(path,now=1000))
    def test_disabled_child_does_not_call_keychain_or_start_services(self):
        with mock.patch('manage_services.keychain') as keychain:
            self.assertEqual(manager.run_child('sender',self.settings),0)
        keychain.assert_not_called()
    def test_keychain_secret_never_part_of_command_arguments(self):
        runner=mock.Mock(return_value=mock.Mock(returncode=0,stdout='secret-value\n'))
        self.assertEqual(manager.keychain('DASHSCOPE_API_KEY',runner=runner),'secret-value')
        self.assertNotIn('secret-value',str(runner.call_args))
    def test_settings_target_profile_mismatch_rejected(self):
        data=manager.read_settings(self.settings); data['profile']='written'; atomic_json(self.settings,data)
        with self.assertRaises(Exception): manager.read_settings(self.settings)

class DiagnosticsTests(unittest.TestCase):
    setUp = ManagerTests.setUp
    def test_export_excludes_secrets_images_answers_and_raw_errors(self):
        diag=diagnostics.Diagnostics(self.settings)
        raw={'build':BUILD,'sender':{'status':'running','age_seconds':0,'heartbeat_fresh':True,'input_error':None,'secret':'API-SECRET'},
             'receiver':{'current':{'id':'id','state':'complete','answer':'PRIVATE-ANSWER','payload':'PRIVATE-IMAGE','prompt':'PRIVATE-PROMPT'},
                         'previous':None,'counts':{'complete':1},'version':3}}
        root=Path(diag.data['state_dir']); (root/'receiver.log').write_text('raw secret API-SECRET\ntrace='+json.dumps({'event':'TEST','task_id':'id','message':'SECRET-MESSAGE','payload':'PRIVATE-IMAGE'})+'\n')
        with mock.patch.object(diag,'status',return_value=raw): archive=diag.export(self.root/'diagnostics.zip')
        with zipfile.ZipFile(archive) as z:
            text='\n'.join(z.read(n).decode() for n in z.namelist())
            self.assertEqual(set(z.namelist()),{'status.json','events.jsonl','PRIVACY.txt'})
        for secret in ('API-SECRET','PRIVATE-ANSWER','PRIVATE-IMAGE','PRIVATE-PROMPT','SECRET-MESSAGE'):
            self.assertNotIn(secret,text)
        self.assertIn('TEST',text)
    def test_export_refuses_to_overwrite_file(self):
        diag=diagnostics.Diagnostics(self.settings); output=self.root/'exists.zip'; output.write_bytes(b'existing')
        with mock.patch.object(diag,'status',return_value={'sender':{},'receiver':{}}):
            with self.assertRaises(FileExistsError): diag.export(output)
        self.assertEqual(output.read_bytes(),b'existing')
    def test_local_capture_test_has_no_upload(self):
        diag=diagnostics.Diagnostics(self.settings)
        def fake_capture(screenshotter,task):
            path=screenshotter.spool_dir/'image.jpg'; path.write_bytes(b'\xff\xd8fake\xff\xd9'); return path
        with mock.patch('diagnostics.Screenshotter.capture',fake_capture), mock.patch.object(diag,'request') as request:
            result=diag.local_capture_test()
        self.assertEqual(result['uploaded'],'no'); request.assert_not_called()
    def test_stale_heartbeat_not_reported_fresh(self):
        path=self.root/'heartbeat.json'; path.write_text(json.dumps({'at':1,'status':'running'}))
        self.assertFalse(diagnostics.Diagnostics._read_status(path)['heartbeat_fresh'])

    def test_export_includes_backup_route_without_answer_or_credentials(self):
        diag=diagnostics.Diagnostics(self.settings)
        raw={'sender':{},'receiver':{},'routes':[{'id':'task','backend':'local-backup','cluster_id':'cluster',
             'state':'accepted','answer':'PRIVATE-ANSWER','token':'PRIVATE-TOKEN'}],
             'display':{'status':'running','answer':'PRIVATE-ANSWER'},
             'receiver_nodes':{'receiver_backup':{'status':'running','token':'PRIVATE-TOKEN'}}}
        with mock.patch.object(diag,'status',return_value=raw): archive=diag.export(self.root/'routes.zip')
        with zipfile.ZipFile(archive) as z: text=z.read('status.json').decode()
        self.assertIn('local-backup',text);self.assertIn('receiver_backup',text)
        self.assertNotIn('PRIVATE-ANSWER',text);self.assertNotIn('PRIVATE-TOKEN',text)
    def test_export_reads_backup_log_but_not_raw_fields(self):
        diag=diagnostics.Diagnostics(self.settings);root=Path(diag.data['state_dir'])
        (root/'receiver_backup.log').write_text('trace='+json.dumps({'event':'RECEIVER_OWNER_ACQUIRED','node':'receiver_backup',
            'message':'PRIVATE-RAW','token':'PRIVATE-TOKEN'})+'\n')
        with mock.patch.object(diag,'status',return_value={'sender':{},'receiver':{}}): archive=diag.export(self.root/'backup.zip')
        with zipfile.ZipFile(archive) as z: text=z.read('events.jsonl').decode()
        self.assertIn('RECEIVER_OWNER_ACQUIRED',text);self.assertIn('receiver_backup',text)
        self.assertNotIn('PRIVATE',text)
