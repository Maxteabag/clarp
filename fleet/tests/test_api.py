import http.client
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest
from fleet.broker import Broker,handler

class ApiTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.broker=Broker({'peers':[],'client_tokens':{'parent-a':'test-a','parent-b':'test-b'}},self.temp.name)
        self.server=ThreadingHTTPServer(('127.0.0.1',0),handler(self.broker))
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.addCleanup(self.broker.pool.shutdown,wait=True);self.addCleanup(self.server.server_close);self.addCleanup(self.server.shutdown)
    def call(self,path,token=None,data=None):
        c=http.client.HTTPConnection('127.0.0.1',self.server.server_port)
        try:
            headers={'Content-Type':'application/json'}
            if token:headers['Authorization']='Bearer '+token
            c.request('POST' if data is not None else 'GET',path,json.dumps(data) if data is not None else None,headers);r=c.getresponse();return r.status,json.loads(r.read())
        finally:c.close()
    def test_no_auth_and_other_parent_cannot_read_job(self):
        self.assertEqual(self.call('/v1/snapshot')[0],401)
        req={'profile':'diagnostic','parent':{'host':'parent-a','agent':'a','task':'t'}}
        status,x=self.call('/v1/jobs','test-a',{'id':'test','request':req});self.assertEqual(status,200)
        self.assertEqual(self.call('/v1/jobs/test','test-b')[0],403)
        self.assertEqual(self.call('/v1/jobs/test','test-a')[0],200)
    def test_parent_spoof_and_remote_admin_are_rejected(self):
        req={'profile':'diagnostic','parent':{'host':'parent-b','agent':'a','task':'t'}}
        self.assertEqual(self.call('/v1/jobs','test-a',{'request':req})[0],400)
        self.assertEqual(self.call('/v1/peers/any/drain','test-a',{'draining':True})[0],404)

if __name__=='__main__':unittest.main()
