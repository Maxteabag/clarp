import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

class RemoteTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        with patch.dict(os.environ,{'CLARP_FLEET_WORKER_STATE':str(self.root)}):
            spec=importlib.util.spec_from_file_location('fleet_remote_test',Path(__file__).parents[1]/'remote.py');self.m=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.m)
        self.broker='a'*32;self.attempt='b'*32
        self.m.write(self.root/'config.json',{'broker_id':self.broker,'max_jobs':1,'capacity':{'cpu':2,'ram_mb':1024,'gpu_mb':0}})
        self.req={'broker':self.broker,'attempt':self.attempt,'job_id':'test','job':{'profile':'diagnostic','resources':{'cpu':1,'ram_mb':256,'gpu_mb':0},'files':[]}}
    def test_staging_is_idempotent_and_executor_rechecks_capacity(self):
        with patch.object(self.m,'probe',return_value={'profiles':['diagnostic'],'ram_available_mb':4096}),patch.object(self.m.subprocess,'Popen',return_value=SimpleNamespace(pid=123)) as spawn:
            self.m.start(self.req);self.m.start(self.req);self.assertEqual(spawn.call_count,1)
            with self.assertRaises(ValueError):self.m.start({**self.req,'attempt':'c'*32})
            with self.assertRaises(ValueError):self.m.start({**self.req,'job_id':'different'})
    def test_input_traversal_and_hash_mismatch_are_rejected(self):
        file={'path':'../escape','data':base64.b64encode(b'x').decode(),'sha256':hashlib.sha256(b'x').hexdigest()}
        with self.assertRaises(ValueError):self.m.unpack(self.root,[file])
        with self.assertRaises(ValueError):self.m.unpack(self.root,[{**file,'path':'safe','sha256':'0'*64}])
        self.assertFalse((self.root/'safe').exists())
    def test_artifact_symlink_parent_cannot_escape(self):
        d=self.m.job_dir(self.broker,self.attempt);(d/'result').mkdir(parents=True);outside=self.root/'outside';outside.mkdir();(outside/'secret').write_text('private');(d/'result'/'link').symlink_to(outside,target_is_directory=True)
        with self.assertRaises(ValueError):self.m.rpc({'action':'artifact','broker':self.broker,'attempt':self.attempt,'path':'link/secret'})
    def test_wrong_broker_cannot_start_or_read(self):
        with self.assertRaises(ValueError):self.m.start({**self.req,'broker':'d'*32})
        with self.assertRaises(ValueError):self.m.rpc({'action':'status','broker':'d'*32,'attempt':self.attempt})

if __name__=='__main__':unittest.main()
