"""Command interface used identically by local and remote master agents."""
import argparse
import base64
import hashlib
import http.client
import io
import json
import os
import platform
from pathlib import Path
import socket
import sys
import tarfile
import subprocess
import time
import urllib.request
import uuid

CONFIG=Path(os.environ.get('CLARP_FLEET_CONFIG',str(Path.home()/'.config/clarp-fleet/config.json')))
STATE=Path(os.environ.get('CLARP_FLEET_STATE',str(Path.home()/'.local/state/clarp-fleet')))
SOCKET=Path(os.environ.get('CLARP_FLEET_SOCKET',str(Path.home()/'.cache/clarp-fleet/broker.sock')))


class BrokerUnavailable(RuntimeError):
    """A retryable read failure, never permission to repeat a mutation."""


class UnixConnection(http.client.HTTPConnection):
    def connect(self):
        self.sock=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);self.sock.settimeout(self.timeout);self.sock.connect(str(SOCKET))


def api(path,data=None):
    body=json.dumps(data).encode() if data is not None else None
    if SOCKET.exists():
        c=UnixConnection('localhost',timeout=50)
        try:
            c.request('POST' if body is not None else 'GET',path,body,{'Content-Type':'application/json'});r=c.getresponse();x=json.loads(r.read())
            if r.status>=400:raise (BrokerUnavailable if r.status in (502,503,504) else RuntimeError)(x.get('error','Broker error'))
            return x
        finally:c.close()
    config=json.loads(CONFIG.with_name('client.json').read_text());client=config.get('client',{})
    req=urllib.request.Request(client['url']+path,data=body,headers={'Authorization':'Bearer '+client['token'],'Content-Type':'application/json'},method='POST' if body is not None else 'GET')
    try:
        with urllib.request.urlopen(req,timeout=50) as r:return json.load(r)
    except urllib.error.HTTPError as e:raise (BrokerUnavailable if e.code in (502,503,504) else RuntimeError)(json.load(e).get('error','Broker error'))


def files(args):
    result=[];total=0
    def add(name,data,executable=False):
        nonlocal total
        total+=len(data)
        if total>32*1024*1024:raise ValueError('Input bundle limit32MiB; stage a smaller immutable source')
        result.append({'path':name,'data':base64.b64encode(data).decode(),'sha256':hashlib.sha256(data).hexdigest(),'executable':executable})
    source={}
    if args.repo:
        repo=Path(args.repo).resolve();sha=subprocess.check_output(['git','-C',str(repo),'rev-parse','--verify',args.commit+'^{commit}'],text=True).strip()
        raw=subprocess.check_output(['git','-C',str(repo),'archive','--format=tar',sha])
        with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
            for member in archive.getmembers():
                if member.isfile():add(member.name,archive.extractfile(member).read(),bool(member.mode&0o111))
                elif member.issym():
                    # Instruction-file aliases do not need to be executable links.
                    target=(Path(member.name).parent/member.linkname).as_posix()
                    if '..' in Path(target).parts or target.startswith('/'):raise ValueError('External source symlink is unsupported')
                    linked=archive.getmember(target)
                    if not linked.isfile():raise ValueError('Non-file source symlink is unsupported')
                    add(member.name,archive.extractfile(linked).read())
        source={'commit':sha,'tree':subprocess.check_output(['git','-C',str(repo),'rev-parse',sha+'^{tree}'],text=True).strip()}
    for entry in args.file or []:
        name,sep,path=entry.partition('=')
        if not sep:raise ValueError('Files use job/path=/absolute/local/path')
        add(name,Path(path).read_bytes())
    source['manifest_sha256']=hashlib.sha256(json.dumps([(x['path'],x['sha256']) for x in result],sort_keys=True).encode()).hexdigest()
    return result,source


def request(args):
    cfg=json.loads(CONFIG.read_text()) if CONFIG.exists() else json.loads(CONFIG.with_name('client.json').read_text()) if CONFIG.with_name('client.json').exists() else {}
    inputs,source=files(args)
    return {'profile':args.profile,'resources':{'cpu':args.cpu if args.cpu is not None else 2 if args.profile.startswith('whisper') else 1,'ram_mb':args.ram_mb if args.ram_mb is not None else 2048 if args.profile.startswith('whisper') else 512,'gpu_mb':args.gpu_mb if args.gpu_mb is not None else 1024 if args.profile=='whisper-gpu' else 256 if args.profile=='render-gpu' else 0},'parent':{'host':cfg.get('local_id',socket.gethostname()),'agent':args.parent_agent or os.environ.get('CLAUDE_PWA_SESSION') or os.environ.get('CLARP_SESSION') or 'cli','task':args.parent_task or args.id or uuid.uuid4().hex},'files':inputs,'source':source,'peer':args.peer,'priority':args.priority or ('interactive' if args.profile.startswith('whisper') else 'background'),'timeout':args.timeout,'seconds':args.seconds,'model':args.model,'effort':args.effort,'os':args.os or (platform.system() if args.profile in {'compile-cpp','cmake'} else None),'arch':args.arch or (platform.machine() if args.profile in {'compile-cpp','cmake'} else None)}



def transcribe(args):
    cfg=json.loads(CONFIG.read_text()) if CONFIG.exists() else json.loads(CONFIG.with_name('client.json').read_text())
    raw=Path(args.audio).read_bytes()
    if len(raw)>16*1024*1024:raise ValueError('Audio limit16MiB')
    digest=hashlib.sha256(raw).hexdigest();job_id=args.id or uuid.uuid4().hex;args.id=job_id
    parent={'host':cfg['local_id'],'agent':args.parent_agent or os.environ.get('CLAUDE_PWA_SESSION') or os.environ.get('CLARP_SESSION') or 'cli','task':args.parent_task or job_id}
    try:
        existing=api('/v1/jobs/'+job_id)
        if existing['request']['parent']!=parent or existing['request']['source'].get('audio_sha256')!=digest:raise ValueError('Transcription id belongs to different input or parent')
    except RuntimeError as e:
        if str(e)!='Unknown job':raise
        existing=None
    if existing is None:
        profiles=['whisper-gpu','whisper'] if args.engine=='auto' else ['whisper-gpu' if args.engine=='gpu' else 'whisper']
        candidates=[]
        for profile in profiles:
            req={'profile':profile,'resources':{'cpu':2,'ram_mb':2048,'gpu_mb':1024 if profile=='whisper-gpu' else 0},'priority':'interactive','parent':parent,'peer':args.peer,'timeout':args.timeout,'queue_timeout':10,'source':{'audio_sha256':digest,'total_bytes':len(raw)}}
            choices=api('/v1/plan',req)
            match=next((c for c in choices if c['eligible']),None)
            if match:candidates.append((match['score'],req))
        if not candidates:raise RuntimeError('No inference peer currently has compatible reserved capacity')
        _,req=min(candidates,key=lambda x:x[0]);req['files']=[{'path':'audio.wav','data':base64.b64encode(raw).decode(),'sha256':digest}]
        api('/v1/jobs',{'request':req,'id':job_id})
    deadline=time.monotonic()+args.timeout+15
    while True:
        job=api('/v1/jobs/'+job_id)
        if job['status'] in ['failed','cancelled','unknown']:raise RuntimeError('Transcription '+job['status']+': '+(job['error'] or str((job.get('result') or {}).get('output',''))[-1000:]))
        if job['status']=='succeeded':
            payload=json.loads(job['result']['output'].strip().splitlines()[-1]);api('/v1/jobs/'+job_id+'/ack',{'parent':parent})
            return {**payload,'job_id':job_id,'peer':job['peer'],'parent':parent}
        if time.monotonic()>deadline:
            api('/v1/jobs/'+job_id+'/cancel',{});raise TimeoutError('Transcription deadline exceeded; cancellation requested for '+job_id)
        time.sleep(.3)

def main():
    p=argparse.ArgumentParser(description=__doc__);sub=p.add_subparsers(dest='command',required=True)
    s=sub.add_parser('serve');s.add_argument('--config',default=str(CONFIG));s.add_argument('--state',default=str(STATE));s.add_argument('--socket',default=str(SOCKET))
    sub.add_parser('peers');sub.add_parser('snapshot');sub.add_parser('badge')
    for name in ('plan','submit'):
        s=sub.add_parser(name);s.add_argument('--profile',required=True);s.add_argument('--cpu',type=float);s.add_argument('--ram-mb',type=int);s.add_argument('--gpu-mb',type=int);s.add_argument('--peer');s.add_argument('--priority',choices=['interactive','background']);s.add_argument('--timeout',type=int,default=300);s.add_argument('--seconds',type=int,default=2);s.add_argument('--file',action='append');s.add_argument('--repo');s.add_argument('--commit',default='HEAD');s.add_argument('--parent-agent');s.add_argument('--parent-task');s.add_argument('--model');s.add_argument('--effort');s.add_argument('--os');s.add_argument('--arch');s.add_argument('--id');s.add_argument('--notify-parent',action='store_true')
    for name in ('status','wait','cancel','ack'):
        s=sub.add_parser(name);s.add_argument('id');s.add_argument('--wait-timeout',type=int,default=3600)
    s=sub.add_parser('artifact');s.add_argument('id');s.add_argument('path');s.add_argument('--output',required=True)
    s=sub.add_parser('drain');s.add_argument('peer');s.add_argument('--resume',action='store_true')
    s=sub.add_parser('limits');s.add_argument('peer');s.add_argument('--cpu',type=float);s.add_argument('--ram-mb',type=int);s.add_argument('--interactive-cpu',type=float);s.add_argument('--interactive-ram-mb',type=int)
    s=sub.add_parser('transcribe');s.add_argument('audio');s.add_argument('--engine',choices=['auto','cpu','gpu'],default='auto');s.add_argument('--peer');s.add_argument('--timeout',type=int,default=60);s.add_argument('--json',action='store_true');s.add_argument('--id');s.add_argument('--parent-agent');s.add_argument('--parent-task')
    args=p.parse_args()
    try:
        if args.command=='transcribe':
            x=transcribe(args);print(json.dumps(x) if args.json else x['text']);return
        if args.command=='serve':
            from .broker import serve
            serve(args.config,args.state,args.socket);return
        if args.command=='badge':
            try:
                x=json.loads((STATE/'snapshot.json').read_text());age=time.time()-x['observed_at'];online=sum(p['telemetry'].get('reachable',False) and p['age_seconds']<30 for p in x['peers']);running=sum(x.get('counts',{}).get(k,0) for k in ['reserved','staging','running','cancelling','unknown']);queued=x.get('counts',{}).get('queued',0)
                x={'text':'Fleet ?' if age>30 else f'Fleet {online}/{len(x["peers"])}','class':'stale' if age>30 else 'ready','tooltip':f'{running} running · {queued} queued. Online executors, not free capacity.\nClick for reservations and results. Snapshot age '+str(round(age,1))+' seconds'}
            except (OSError,ValueError,KeyError):x={'text':'Fleet ?','class':'stale','tooltip':'Fleet service unavailable'}
        elif args.command in ['snapshot','peers']:
            x=api('/v1/snapshot');x=x['peers'] if args.command=='peers' else x
        elif args.command in ['plan','submit']:
            if args.command=='submit' and not args.id:args.id=uuid.uuid4().hex
            x=request(args)
            parent=x['parent']
            if args.command=='submit' and args.notify_parent:
                from .notify import check_parent
                check_parent(x['parent'])
            x=api('/v1/plan',x) if args.command=='plan' else api('/v1/jobs',{'request':x,'id':args.id})
            if args.command=='submit' and args.notify_parent:
                from .notify import start_watcher
                x['notification']=start_watcher(args.id,parent)
        elif args.command=='limits':x=api('/v1/peers/'+args.peer+'/limits',{k:getattr(args,k) for k in ['cpu','ram_mb','interactive_cpu','interactive_ram_mb'] if getattr(args,k) is not None})
        elif args.command=='drain':x=api('/v1/peers/'+args.peer+'/drain',{'draining':not args.resume})
        elif args.command=='artifact':
            x=api('/v1/jobs/'+args.id+'/artifact',{'path':args.path});data=base64.b64decode(x['data'],validate=True)
            if hashlib.sha256(data).hexdigest()!=x['sha256']:raise ValueError('Artifact checksum mismatch')
            out=Path(args.output);out.parent.mkdir(parents=True,exist_ok=True);out.write_bytes(data);x={'path':str(out),'sha256':x['sha256'],'size':len(data)}
        elif args.command=='cancel':x=api('/v1/jobs/'+args.id+'/cancel',{})
        else:
            deadline=time.monotonic()+args.wait_timeout
            while True:
                x=api('/v1/jobs/'+args.id)
                if args.command!='wait' or x['status'] in ['succeeded','failed','cancelled','unknown']:break
                if time.monotonic()>deadline:raise TimeoutError('Wait timed out; job continues with the same id')
                time.sleep(1)
            if args.command=='ack':x=api('/v1/jobs/'+args.id+'/ack',{'parent':x['request']['parent']})
        print(json.dumps(x,indent=None if args.command=='badge' else 2))
        if args.command=='wait' and x.get('status')!='succeeded':sys.exit(2)
    except Exception as error:
        print(json.dumps({'error':str(error),'id':getattr(args,'id',None)}),file=sys.stderr);sys.exit(1)


if __name__=='__main__':main()
