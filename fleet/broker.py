"""Local broker service and authenticated tailnet API; never restarts the Clarp Host."""
import base64
import concurrent.futures
import contextlib
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import shlex
import socketserver
import subprocess
import threading
import time
from urllib.parse import urlsplit

from .store import Store, ACTIVE, TERMINAL

MAX_BODY=64*1024*1024


class Broker:
    def __init__(self, config, state):
        self.discovered=[];self.discovery_at=0
        self.config=config;self.state=Path(state);self.state.mkdir(parents=True,exist_ok=True);self.state.chmod(0o700)
        self.store=Store(self.state/'jobs.sqlite');self.store.peers_configure(config['peers'])
        self.peers={p['id']:p for p in config['peers']};self.stop=threading.Event()
        self.pool=concurrent.futures.ThreadPoolExecutor(max_workers=8)
        self.inflight={};self.guard=threading.Lock();self.errors={};self.last_poll={}
        (self.state/'ssh').mkdir(exist_ok=True,mode=0o700)

    def rpc(self,peer,payload,timeout=12):
        p=self.peers[peer];script=p['worker'];python=p.get('python','python3')
        argv=[python,script] if p.get('ssh')=='local' else ['ssh','-o','BatchMode=yes','-o','ConnectTimeout=4','-o','ControlMaster=auto','-o','ControlPersist=60','-o','ControlPath='+str(self.state/'ssh/%C'),p['ssh'],shlex.join([python,script])]
        proc=subprocess.run(argv,input=json.dumps(payload)+'\n',text=True,capture_output=True,timeout=timeout)
        try:result=json.loads(proc.stdout)
        except ValueError:raise RuntimeError('Peer RPC unavailable (exit '+str(proc.returncode)+')')
        if not result.get('ok'):raise RuntimeError(result.get('error','Peer rejected request'))
        return result['result']

    def discover(self):
        try:
            p=subprocess.run(['tailscale','status','--json'],capture_output=True,text=True,timeout=5);x=json.loads(p.stdout)
            self.discovered=[{'name':v.get('HostName','unknown'),'dns':v.get('DNSName','').rstrip('.'),'os':v.get('OS','unknown'),'online':bool(v.get('Online'))} for v in [x.get('Self',{})]+list(x.get('Peer',{}).values())];self.discovery_at=time.time()
        except Exception:pass

    def view(self):
        result=self.store.snapshot();enrolled={p['telemetry'].get('tailscale_dns') for p in result['peers']}
        result['discovered']=[{**p,'enrolled':p['dns'] in enrolled} for p in self.discovered];result['discovery_at']=self.discovery_at
        return result

    def refresh(self,peer):
        try:self.store.observe(peer,self.rpc(peer,{'action':'probe'}))
        except Exception as e:self.store.observe(peer,{'reachable':False,'error':str(e),'observed_at':time.time()})

    def step_job(self,job):
        peer=job['peer'];attempt=job['attempt'];base={'broker':self.store.broker_id(),'attempt':attempt}
        try:
            if job['status']=='reserved':
                self.store.transition(job['id'],attempt,'staging')
                payload=dict(job['request']);payload['files']=self.store.input_files(payload['input_ref'])
                result=self.rpc(peer,{**base,'action':'start','job_id':job['id'],'job':payload},timeout=45)
            else:
                result=self.rpc(peer,{**base,'action':'cancel' if job['cancel_requested'] else 'status','renew':True})
            status=result.get('status')
            if status=='absent':
                # A lost start response is not permission to start another copy.
                self.store.transition(job['id'],attempt,'unknown',error='Executor attempt absent; manual reconciliation required')
            elif status in TERMINAL:
                self.store.transition(job['id'],attempt,status,result=result,error=result.get('error',''))
            elif status in ('staging','running'):
                self.store.transition(job['id'],attempt,'cancelling' if job['cancel_requested'] else status)
        except Exception as e:
            self.store.transition(job['id'],attempt,'unknown',error=str(e))

    def loop(self):
        next_probe=0
        while not self.stop.wait(.5):
            if time.monotonic()>=next_probe:
                for peer in self.peers:
                    key='peer:'+peer
                    if key not in self.inflight or self.inflight[key].done():self.inflight[key]=self.pool.submit(self.refresh,peer)
                if time.time()-self.discovery_at>30 and ('discovery' not in self.inflight or self.inflight['discovery'].done()):self.inflight['discovery']=self.pool.submit(self.discover)
                next_probe=time.monotonic()+10
            self.store.expire_queued()
            while self.store.reserve_next() is not None:pass
            with self.store.db() as c:
                jobs=[self.store.get_from(c,r[0]) for r in c.execute("SELECT id FROM jobs WHERE status IN ('reserved','staging','running','cancelling','unknown')")]
            for job in jobs:
                key='job:'+job['id']
                interval=.5 if job['request']['priority']=='interactive' or job['cancel_requested'] else 2
                if (key not in self.inflight or self.inflight[key].done()) and time.monotonic()-self.last_poll.get(key,0)>=interval:
                    self.last_poll[key]=time.monotonic();self.inflight[key]=self.pool.submit(self.step_job,job)
            snapshot=self.view()
            for job in snapshot['jobs']:
                if job.get('result'):job['result'].pop('output',None)
            tmp=self.state/'snapshot.next';tmp.write_text(json.dumps(snapshot));tmp.chmod(0o600);os.replace(tmp,self.state/'snapshot.json')
            # Bound completed Future retention.
            active={j['id'] for j in jobs}
            self.inflight={k:v for k,v in self.inflight.items() if not k.startswith('job:') or k[4:] in active or not v.done()}

    def artifact(self,job,path):
        j=self.store.get(job)
        if j['status'] not in TERMINAL:raise ValueError('Job is not terminal')
        expected=next((x for x in (j['result'] or {}).get('artifacts',[]) if x['path']==path),None)
        if expected is None:raise ValueError('Unknown artifact')
        x=self.rpc(j['peer'],{'action':'artifact','broker':self.store.broker_id(),'attempt':j['attempt'],'path':path},timeout=30)
        raw=base64.b64decode(x['data'],validate=True)
        if hashlib.sha256(raw).hexdigest()!=expected['sha256'] or x['sha256']!=expected['sha256']:raise ValueError('Artifact checksum mismatch')
        return x


class UnixServer(socketserver.ThreadingMixIn,socketserver.UnixStreamServer):
    daemon_threads=True


def handler(broker,local=False):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def send_json(self,status,value):
            data=json.dumps(value).encode();self.send_response(status);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
        def do_GET(self):self.route()
        def do_POST(self):self.route()
        def route(self):
            caller=None
            if not local:
                token=self.headers.get('Authorization','').removeprefix('Bearer ')
                caller=next((host for host,key in broker.config.get('client_tokens',{}).items() if secrets.compare_digest(token,key)),None)
                if not caller:return self.send_json(401,{'error':'Authentication required'})
            try:
                length=int(self.headers.get('Content-Length','0'))
                if not 0<=length<=MAX_BODY:raise ValueError('Request too large')
                data=json.loads(self.rfile.read(length)) if length else {}
                path=urlsplit(self.path).path;parts=path.strip('/').split('/')
                if path=='/v1/snapshot' and self.command=='GET':
                    result=broker.view()
                    for j in result['jobs']:
                        if j.get('result'):j['result'].pop('output',None)
                    return self.send_json(200,result)
                if path=='/v1/plan' and self.command=='POST':return self.send_json(200,broker.store.choices(data))
                if path=='/v1/jobs' and self.command=='POST':
                    if caller and data.get('request',{}).get('parent',{}).get('host')!=caller:raise ValueError('Parent host does not match enrolled caller')
                    return self.send_json(200,{'id':broker.store.submit(data['request'],data.get('id'))})
                if len(parts)>=3 and parts[:2]==['v1','jobs']:
                    job=broker.store.get(parts[2])
                    if caller and job['request']['parent']['host']!=caller:return self.send_json(403,{'error':'Job belongs to another parent host'})
                    if len(parts)==3 and self.command=='GET':
                        job['request'].pop('files',None);return self.send_json(200,job)
                    if len(parts)==4 and self.command=='POST':
                        if parts[3]=='cancel':return self.send_json(200,broker.store.cancel(parts[2]))
                        if parts[3]=='ack':broker.store.acknowledge(parts[2],data['parent']);return self.send_json(200,{'acknowledged':True})
                        if parts[3]=='artifact':return self.send_json(200,broker.artifact(parts[2],data['path']))
                if len(parts)==4 and parts[:2]==['v1','peers'] and parts[3]=='limits' and local:
                    return self.send_json(200,broker.store.set_limits(parts[2],data))
                if len(parts)==4 and parts[:2]==['v1','peers'] and parts[3]=='drain' and local:
                    broker.store.drain(parts[2],bool(data['draining']));return self.send_json(200,{'ok':True})
                self.send_json(404,{'error':'Unknown endpoint'})
            except (ValueError,KeyError,TypeError) as e:self.send_json(400,{'error':str(e)})
            except Exception as e:self.send_json(503,{'error':str(e)})
    return Handler


def serve(config_path,state,socket_path):
    import fcntl
    config=json.loads(Path(config_path).read_text());broker=Broker(config,state)
    lock=(Path(state)/'service.lock').open('a');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    socket_path=Path(socket_path);socket_path.parent.mkdir(parents=True,exist_ok=True);socket_path.parent.chmod(0o700)
    socket_path.unlink(missing_ok=True)
    unix=UnixServer(str(socket_path),handler(broker,True));socket_path.chmod(0o600)
    threading.Thread(target=unix.serve_forever,daemon=True).start()
    address=config.get('listen')
    tcp=None
    if address:
        tcp=ThreadingHTTPServer((address,config.get('port',18766)),handler(broker));threading.Thread(target=tcp.serve_forever,daemon=True).start()
    try:broker.loop()
    finally:
        broker.stop.set();unix.shutdown()
        if tcp:tcp.shutdown()
        broker.pool.shutdown(wait=True);socket_path.unlink(missing_ok=True)
