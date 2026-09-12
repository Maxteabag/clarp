"""Install only the new fleet component using existing SSH access; never restart Clarp."""
import argparse
import base64
import hashlib
import json
from pathlib import Path
import re
import secrets
import shlex
import subprocess
import sys

from .store import Store

INSTALL = r'''
import base64,hashlib,json,os,sys
from pathlib import Path
x=json.load(sys.stdin);base=Path.home()/'.local/lib/clarp-fleet';release=base/'releases'/x['release'];release.mkdir(parents=True,exist_ok=True)
for f in x['files']:
 p=Path(f['path'])
 if p.is_absolute() or '..' in p.parts:raise ValueError('Unsafe release path')
 data=base64.b64decode(f['data'],validate=True)
 if hashlib.sha256(data).hexdigest()!=f['sha256']:raise ValueError('Release checksum mismatch')
 target=release/p;target.parent.mkdir(parents=True,exist_ok=True)
 if target.exists() and target.read_bytes()!=data:raise ValueError('Immutable release differs')
 target.write_bytes(data)
link=base/'next';link.unlink(missing_ok=True);link.symlink_to(release);os.replace(link,base/'current')
worker=Path.home()/'.local/state/clarp-fleet-worker';worker.mkdir(parents=True,exist_ok=True);worker.chmod(0o700)
old=json.loads((worker/'config.json').read_text()) if (worker/'config.json').exists() else {}
if old.get('broker_id') and old['broker_id']!=x['worker']['broker_id']:raise ValueError('Executor already belongs to a different broker')
(worker/'config.json').write_text(json.dumps({**old,**x['worker']}));(worker/'config.json').chmod(0o600)
config=Path.home()/'.config/clarp-fleet';config.mkdir(parents=True,exist_ok=True);config.chmod(0o700)
client=config/'client.json';client.write_text(json.dumps(x['client']));client.chmod(0o600)
bin=Path.home()/'.local/bin';bin.mkdir(parents=True,exist_ok=True);launcher=bin/'clarp-fleet'
if launcher.exists() and 'managed-by-clarp-fleet' not in launcher.read_text():raise ValueError('Unrelated launcher exists')
launcher.write_text('#!/bin/sh\n# managed-by-clarp-fleet\nexport PYTHONPATH="$HOME/.local/lib/clarp-fleet/current${PYTHONPATH:+:$PYTHONPATH}"\nexec '+sys.executable+' -m fleet "$@"\n');launcher.chmod(0o755)
print(json.dumps({'worker':str(release/'fleet/remote.py'),'python':sys.executable,'release':x['release']}))
'''


def invoke(peer,script,payload):
    argv=[sys.executable,'-c',script] if peer=='local' else ['ssh','-o','BatchMode=yes','-o','ConnectTimeout=5',peer,shlex.join(['python3','-c',script])]
    p=subprocess.run(argv,input=json.dumps(payload),capture_output=True,text=True,timeout=40)
    if p.returncode:raise RuntimeError('Peer setup failed: '+p.stderr[-1500:])
    return json.loads(p.stdout)


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--peer',action='append',required=True,help='id=existing-ssh-alias, or id=local');parser.add_argument('--listen',required=True,help='This broker Tailscale IPv4 address');parser.add_argument('--port',type=int,default=18766);parser.add_argument('--apply',action='store_true');parser.add_argument('--start',action='store_true');args=parser.parse_args()
    import ipaddress
    address=ipaddress.ip_address(args.listen)
    if address not in ipaddress.ip_network('100.64.0.0/10'):parser.error('Bind only a Tailscale address in100.64.0.0/10')
    peers=[]
    for entry in args.peer:
        ident,sep,alias=entry.partition('=')
        if not sep or not re.fullmatch('[a-zA-Z0-9][a-zA-Z0-9_.-]*',ident) or not re.fullmatch('[a-zA-Z0-9][a-zA-Z0-9_.-]*',alias):parser.error('Use id=SSH-alias or id=local')
        peers.append((ident,alias))
    if len({p[0] for p in peers})!=len(peers) or sum(p[1]=='local' for p in peers)!=1:parser.error('Unique peer ids and exactly one local broker are required')
    files=[];package=Path(__file__).parent
    for p in sorted(package.rglob('*')):
        if p.is_file() and '__pycache__' not in p.parts and 'tests' not in p.relative_to(package).parts and p.suffix in {'.py','.qml'}:
            raw=p.read_bytes();files.append({'path':'fleet/'+str(p.relative_to(package)),'data':base64.b64encode(raw).decode(),'sha256':hashlib.sha256(raw).hexdigest()})
    release=hashlib.sha256(json.dumps([(f['path'],f['sha256']) for f in files]).encode()).hexdigest()[:24]
    if not args.apply:
        print(json.dumps({'peers':peers,'listen':args.listen,'port':args.port,'release':release,'files':len(files),'start':args.start,'changes':'New fleet package/client/worker configs; optional new fleet service. No existing Clarp service changes.'},indent=2));return
    state=Path.home()/'.local/state/clarp-fleet';store=Store(state/'jobs.sqlite');broker=store.broker_id();config_path=Path.home()/'.config/clarp-fleet/config.json';old=json.loads(config_path.read_text()) if config_path.exists() else {}
    config={'local_id':next(i for i,a in peers if a=='local'),'listen':args.listen,'port':args.port,'peers':[],'client_tokens':old.get('client_tokens',{})}
    for ident,alias in peers:
        token=config['client_tokens'].setdefault(ident,secrets.token_urlsafe(32))
        # Initial conservative pool. Hardware discovery below can only reduce it.
        capacity={'cpu':4,'ram_mb':8192,'gpu_mb':0}
        worker={'broker_id':broker,'max_jobs':2,'capacity':capacity}
        client={'local_id':ident,'client':{'url':f'http://{args.listen}:{args.port}','token':token}}
        receipt=invoke(alias,INSTALL,{'release':release,'files':files,'worker':worker,'client':client})
        probe_cmd=[receipt['python'],receipt['worker']]
        argv=probe_cmd if alias=='local' else ['ssh','-o','BatchMode=yes',alias,shlex.join(probe_cmd)]
        p=subprocess.run(argv,input='{"action":"probe"}\n',text=True,capture_output=True,timeout=20);probe=json.loads(p.stdout)['result']
        capacity={'cpu':max(1,min(6,(probe.get('logical_cpus') or 2)-2)),'ram_mb':max(512,int(probe.get('ram_total_mb',2048)-6144)),'gpu_mb':int(probe.get('gpu_total_mb') or 0)}
        worker['capacity']=capacity
        # Agent enablement is deliberate, based on an actual existing CLI login.
        auth=probe.get('tools',{}).get('codex')
        if auth:
            login=[auth,'login','status'];check=login if alias=='local' else ['ssh','-o','BatchMode=yes',alias,shlex.join(login)]
            result=subprocess.run(check,capture_output=True,text=True,timeout=12)
            worker['agent_enabled']=result.returncode==0 and 'logged in' in (result.stdout+result.stderr).lower()
        receipt=invoke(alias,INSTALL,{'release':release,'files':files,'worker':worker,'client':client})
        peer={'id':ident,'ssh':alias,**receipt,'capacity':capacity,'max_jobs':2,'ram_headroom_mb':4096 if alias=='local' else 2048,'interactive_cpu':2,'interactive_ram_mb':2048,'placement_penalty':5 if alias=='local' else 0}
        config['peers'].append(peer)
        print(json.dumps({'peer':ident,'profiles':probe['profiles'],'capacity':capacity,'release':release}))
    config_path.parent.mkdir(parents=True,exist_ok=True);config_path.write_text(json.dumps(config,indent=2));config_path.chmod(0o600)
    unit=Path.home()/'.config/systemd/user/clarp-fleet.service';unit.parent.mkdir(parents=True,exist_ok=True)
    if unit.exists() and '# managed-by-clarp-fleet' not in unit.read_text():raise ValueError('Unrelated fleet unit exists')
    unit.write_text('''# managed-by-clarp-fleet
[Unit]
Description=Clarp fleet job broker
[Service]
Type=simple
ExecStart=%h/.local/bin/clarp-fleet serve
Restart=on-failure
RestartSec=5
MemoryMax=768M
CPUQuota=100%
[Install]
WantedBy=default.target
''')
    if args.start:
        subprocess.run(['systemctl','--user','daemon-reload'],check=True);subprocess.run(['systemctl','--user','enable','--now','clarp-fleet.service'],check=True)
    print('Fleet component installed; no existing Clarp service was restarted.')


if __name__=='__main__':main()
