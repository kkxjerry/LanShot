import argparse
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from deployment.remote_receiver import receiver_arguments


class RemoteEntryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        root=Path(self.tmp.name)
        for name in ('prompt.txt','cert.pem','key.pem'): (root/name).write_text('test data\n')
        self.args=argparse.Namespace(host='0.0.0.0',port=9443,public_host='receiver.example.test',profile='default',
            prompt_file=root/'prompt.txt',state_dir=root/'state',tls_cert=root/'cert.pem',tls_key=root/'key.pem')
    def test_credentials_not_copied_into_receiver_argv(self):
        with mock.patch.dict(os.environ,{'DASHSCOPE_API_KEY':'secret-model','LANSHOT_LOCAL_TOKEN':'secret-local'}):
            args=receiver_arguments(self.args)
        self.assertNotIn('secret-',str(args))
        self.assertEqual(args[args.index('--prompt')+1],'test data\n')
        self.assertEqual(args[args.index('--public-host')+1],'receiver.example.test')
    def test_missing_token_rejected(self):
        with mock.patch.dict(os.environ,{'DASHSCOPE_API_KEY':'key'},clear=True):
            with self.assertRaises(ValueError): receiver_arguments(self.args)
    def test_missing_key_rejected(self):
        with mock.patch.dict(os.environ,{'LANSHOT_LOCAL_TOKEN':'token'},clear=True):
            with self.assertRaises(ValueError): receiver_arguments(self.args)
    def test_missing_tls_files_rejected(self):
        self.args.tls_cert.unlink()
        with mock.patch.dict(os.environ,{'DASHSCOPE_API_KEY':'key','LANSHOT_LOCAL_TOKEN':'token'}):
            with self.assertRaises(ValueError): receiver_arguments(self.args)
