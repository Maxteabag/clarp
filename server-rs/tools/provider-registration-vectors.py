#!/usr/bin/env python3
"""Record worker registration provenance and task output from an isolated Host."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scratch

ROOT=Path(__file__).resolve().parents[2]
FIXTURES=ROOT/'server-rs/crates/clarp-kernel/tests/fixtures'


def record(root):
    spec=importlib.util.spec_from_file_location('isolated_host',ROOT/'tests/replay/host.py')
    isolated=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(isolated)
    isolated.isolate(root)
    isolated.loopback_only()
    sys.path.insert(0,str(ROOT/'server'))
    from lib import agents, background_jobs as jobs, db, provider_background_jobs as provider
    with patch.object(agents,'_new_agent_id',return_value='fixture-agent'):
        agents.create_agent(persona='Fixture',voice_id='',cwd=str(root),session='fixture')
    agent=agents.get_by_session('fixture')
    c=db.conn()
    c.execute('insert into runtimes(agent_id,session,backend_session_id,started_at) values(?,?,?,?)',
              (agent['agent_id'],'fixture','native',1))
    origins=[]
    base={'agent_id':'fixture-agent','provider':'claude','native_session_id':'native','tool_use_id':'call'}
    def check(raw,bound):
        with patch.dict(os.environ,{'CLARP_BACKGROUND_ORIGIN':raw}):
            origins.append([raw,bound,jobs._native_origin(agent)])
    for bound in [True,False]:
        c.execute('update runtimes set ended_at=?',(None if bound else 2,))
        for key in base:
            for val in [None,False,{},0,'','other',chr(0xe9)*159,chr(0xe9)*160,chr(0xe9)*161]:
                check(json.dumps({**base,key:val},ensure_ascii=False),bound)
        raw=json.dumps(base)
        for size in [1999,2000,2001]:
            check(raw.ljust(size),bound)
        for raw in ['', '[]', 'true','{}','{"',json.dumps(base)]:check(raw,bound)

    # Only the reference module's task base is substituted. Its own path
    # checks, log-tail reader and redaction all run on fabricated files.
    out=root/'output-fixture'
    store=out/('claude-'+str(os.getuid()))
    directory=store/'project'/'native'/'tasks'
    directory.mkdir(parents=True)
    path=directory/'job.output'
    meta={'provider_project':'project','native_session_id':'native','provider_task_id':'job'}
    outputs=[]
    def output(case,metadata=meta):
        with patch.object(provider,'pathlib',SimpleNamespace(Path=lambda _:out)):
            reply=provider.output({'metadata':metadata})
        if reply['path']:
            reply['path']=reply['path'].replace(str(store),'STORE')
        outputs.append([case,metadata,reply])
    for k in meta:
        for v in [False,'',None,'../bad','a.b','a'*161]:output('invalid',{**meta,k:v})
    output('missing')
    path.mkdir();output('directory');path.rmdir()
    path.write_text('first\npassword=fixture\n');output('small')
    path.write_bytes(b'x'*65536+b'\ntoken=fixture\nend\n');output('truncated')
    foreign=out/'other';foreign.write_text('outside fixture')
    path.unlink();path.symlink_to(foreign);output('outside-file')
    directory.rename(store/'moved');directory.symlink_to(store/'moved',target_is_directory=True);output('linked-root')
    return origins,outputs


if __name__=='__main__':
    if '--record' in sys.argv:
        with scratch.directory() as td:print(json.dumps(record(Path(td))))
    else:
        data=json.loads(subprocess.check_output([sys.executable,'-I',__file__,'--record'],env=scratch.child_env({'PATH':os.defpath,'TZ':'UTC','LANG':'C.UTF-8'})))
        for name,rows in zip(['provider-origins','provider-output'],data):
            (FIXTURES/(name+'.json')).write_text('[\n'+',\n'.join(json.dumps(r) for r in rows)+'\n]\n',encoding='ascii')
            print(name,len(rows))
