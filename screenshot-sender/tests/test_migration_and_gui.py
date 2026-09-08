import json
import os
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from reliability import TaskStore, Conflict
from migrate_p0_receiver import migrate

try:
    import tkinter as _tkinter
except ImportError:
    _tkinter = None

class P0ReceiverMigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name); self.old=self.root/'p0'; self.old.mkdir()
        self.store=TaskStore(self.root/'new.db')
    def ledger(self,status,image=True):
        task=str(uuid.uuid4())
        (self.old/'capture_ledger.json').write_text(json.dumps({task:{'status':status,'updated_at':time.time()}}))
        if image:
            (self.old/'capture_inbox').mkdir(exist_ok=True)
            (self.old/'capture_inbox'/f'{task}.jpg').write_bytes(b'\xff\xd8p0\xff\xd9')
        return task
    def test_processing_import_is_uncertain_not_automatically_rerun(self):
        task=self.ledger('processing'); report=migrate(self.store,self.old,'default','prompt')
        self.assertEqual(report['imported'],1); self.assertEqual(self.store.get(task)['state'],'uncertain')
        self.assertIsNone(self.store.claim(model=True)); self.assertTrue((self.old/'capture_inbox'/f'{task}.jpg').is_file())
    def test_completed_id_without_image_never_reprocessed_on_reupload(self):
        task=self.ledger('complete',image=False); migrate(self.store,self.old,'default','prompt')
        with self.assertRaises(Conflict):
            self.store.accept(task,b'\xff\xd8p0\xff\xd9',origin='new',origin_seq=1,profile='default',prompt='prompt',configured=True)
        self.assertIsNone(self.store.claim(model=True))
    def test_import_can_be_repeated_without_duplication(self):
        self.ledger('processing'); migrate(self.store,self.old,'default','prompt')
        self.assertEqual(migrate(self.store,self.old,'default','prompt')['already_present'],1)
    def test_missing_processing_image_is_reported_not_fabricated(self):
        self.ledger('processing',image=False)
        self.assertEqual(migrate(self.store,self.old,'default','prompt')['invalid_or_blocked'],1)
        self.assertEqual(self.store.snapshot()['counts'],{})
    def test_invalid_ledger_refused_not_reset(self):
        (self.old/'capture_ledger.json').write_text('{')
        with self.assertRaises(ValueError): migrate(self.store,self.old,'default','prompt')

@unittest.skipUnless(
    _tkinter is not None and (
        os.environ.get('DISPLAY') or os.environ.get('LANSHOT_RUN_GUI_TESTS') == '1'
    ),
    'GUI smoke requires Tk and a display; run with a Tk-enabled Python under Xvfb or set LANSHOT_RUN_GUI_TESTS=1',
)
class NativeGuiSmokeTests(unittest.TestCase):
    def test_visible_window_renders_result_and_acknowledges_matching_version(self):
        import tkinter as tk
        import diagnostics
        diag=mock.Mock()
        diag.view.return_value={'current':{'id':'test-id','state':'complete','answer':'Synthetic answer','code':'','profile':'default'},
                                'previous':None,'version':3,'profile':'default','stage':'complete','sender':{}}
        root=tk.Tk()
        root.after(1800,root.destroy)
        with mock.patch('tkinter.Tk',return_value=root): diagnostics.gui(diag)
        self.assertTrue(any(c.args == ('/api/displayed',{'id':'test-id','version':3}) for c in diag.request.call_args_list))
    def test_receiver_offline_window_remains_responsive(self):
        import tkinter as tk
        import diagnostics
        diag=mock.Mock()
        diag.view.return_value={'current':None,'previous':None,'version':0,'profile':'default','stage':'receiver_unavailable',
                                'sender':{'status':'degraded','queue':{'counts':{'pending':3}}}}
        root=tk.Tk(); completed=[]
        root.after(1300,lambda:(completed.append(True),root.destroy()))
        with mock.patch('tkinter.Tk',return_value=root): diagnostics.gui(diag)
        self.assertEqual(completed,[True])
        diag.request.assert_not_called()
