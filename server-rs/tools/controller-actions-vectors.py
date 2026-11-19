#!/usr/bin/env python3
"""Controller action rules and durable answers from the reference in an isolated store."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'server-rs/crates/clarp-kernel/tests/fixtures/controller-actions-vectors.json'


def generate(root):
    spec = importlib.util.spec_from_file_location('replay_host', ROOT/'tests/replay/host.py')
    host = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(host)
    (root/'scenario-config.toml').touch()
    host.isolate(root)
    host.loopback_only()
    sys.path.insert(0, str(ROOT/'server'))
    from lib import controller_actions as ca
    rows = []
    entries = ['https://service.invalid/api', 'http://service.invalid:8123/line/']
    cases = [('http://127.0.0.1', ''), ('http://localhost:123/x', ''), ('http://[::1]:80/', ''),
             ('https://localhost/', ''), ('https://service.invalid/api?q=x', ''),
             ('https://service.invalid/api#part', ''), ('https://service.invalid/api/sub', ''),
             ('https://service.invalid/apix', ''), ('http://service.invalid:8123/line/a',''),
             ('http://service.invalid:8124/line/a',''), ('http://service.invalid/line/',''),
             ('http://service.invalid:8123/other',''), ('ftp://localhost/a',''), ('not-an-address',''),
             ('http://[',''), ('https://service.invalid/api:late','colon after path prefix'),
             ('http://fixture@localhost/', ''), ('http://@localhost/', ''),
             ('http://service.invalid/api',''), ('https://unlisted.invalid/api',''),
             ('https://service.invalid/api',''), ('https://service.invalid/api/one',''),
             ('http://service.invalid:65536/', ''), ('http:///','')]
    for url, narrowed in cases:
        answer = ca.url_allowed(url,entries)
        if narrowed:
            assert answer
        rows.append(dict(kind='address',url=url,entries=entries,reference=answer,narrowed=narrowed))
    http = dict(id='hook',title=' Hook ',type='http_post',url='http://localhost',body={'n':3})
    ask = dict(id='note',title='Note',type='agent_prompt',session=' unit ',text=' hello ')
    def result(fn):
        try:
            return [200,fn()]
        except ca.ActionError as exc:
            return [exc.status,str(exc)]
    inputs = [[],None,{},http,ask,dict(http,enabled=False),dict(http,type='other')]
    for field, values in {
            'id':['',False,12,'a'*64,'a'*65,'-prefix','_prefix','Upper','x.y','x-y_z'],
            'title':[None,{},True,' ','a'*79,'a'*80,'a'*81],
            'enabled':[None,0,1,'false',True],
            'url':[None,False,'','https://remote.invalid/', 'http://localhost/'+'x'*1983,
                   'http://localhost/'+'x'*1984],
            'body':[None,[],{'s':'x'*8183},{'s':'x'*8184},{'s':chr(0x1f308)*683}],
            'type':[None,False,'',' shell ', ' http_post '],
    }.items():
        inputs.extend(dict(http,**{field:v}) for v in values)
    inputs.append({k:v for k,v in http.items() if k!='body'})
    for field,limit in [('session',128),('text',4000)]:
        inputs.extend(dict(ask,**{field:x}) for x in [None,[],0,'',' ','x'*(limit-1),'x'*limit,'x'*(limit+1)])
    for data in inputs:
        rows.append(dict(kind='definition',data=data,expected=result(lambda:ca._validate(data,entries))))
    now = 1000
    def step(op,data):
        nonlocal now
        now = 1000 if len(rows)%2 else 900
        before=ca.registry()
        with patch.object(ca,'now_ms',return_value=now):
            def change():
                if op=='create': return {'ok':True,'action':ca.create_action(data,actor='administrator',allowed_urls=entries)}
                if op=='update': return {'ok':True,'action':ca.update_action(data['id'],data,actor='administrator',allowed_urls=entries)}
                if op=='delete': return {'ok':True,**ca.delete_action(data['id'],actor='administrator')}
                return {'ok':True,'mapping':ca.set_mapping(**data,actor='administrator')}
            answer=result(change)
            after=ca.registry()
            audit=ca.audit_log(limit=1)[0] if after!=before else None
        rows.append(dict(kind='change',op=op,data=data,at=now,expected=answer,state=after,audit=audit))
    step('create',ask);step('create',ask);step('update',{'id':'absent'});step('delete',{'id':'absent'})
    step('update',{'id':'note','title':'Renamed'});step('update',{'id':'note','type':'http_post'})
    step('update',dict(http,id='note'));step('create',dict(ask,id='other'))
    def mapping(device='*',button='secondary',gesture='swipe-up',action_id='note'):
        step('mappings',dict(device=device,button=button,gesture=gesture,action_id=action_id))
    for device in ['', 'one','*','two', 'a'*128,'a'*129,'0','-bad','bad name','-bad','A:2._-']:
        mapping(device)
    for gesture in [*ca.MAPPABLE_GESTURES,'down','up','unknown']:
        mapping(gesture=gesture)
    for button in ['primary','third']:mapping(button=button)
    mapping(action_id='absent');mapping(device='one', action_id='other');mapping(device='two',action_id=None)
    step('delete',{'id':'note'});step('update',dict(http,id='other'));step('update',dict(ask,id='other'))
    step('create',http)
    for status, body in [(199,''),(200,'success'),(299,'ok'),(300,'moved'),(500,'x'*501)]:
        answer=ca._execute(http,allowed_urls=entries,http_post=lambda *_:(status,body),send_prompt=None)
        rows.append(dict(kind='http_result',status=status,body=body,expected=answer))
    # Actual monotonic clock injection, no sleeping: both sides of the debounce boundary.
    for elapsed in [0,1499,1500,1501]:
        ca._reset_runtime_state()
        with patch.object(ca.time,'monotonic',return_value=0):
            first=ca.run('hook',actor='fixture',request_id=f'A-{elapsed}',http_post=lambda *_:(200,'fixture'))
        with patch.object(ca.time,'monotonic',return_value=elapsed/1000):
            second=ca.run('hook',actor='fixture',request_id=f'B-{elapsed}',http_post=lambda *_:(200,'again'))
        rows.append(dict(kind='debounce',action=http,first=first,elapsed=elapsed,
                         expected=second if second.get('debounced') else None))
    return rows


if __name__=='__main__':
    if '--isolated' in sys.argv:
        with tempfile.TemporaryDirectory() as tmp:
            print(json.dumps(generate(Path(tmp)),ensure_ascii=True))
    else:
        ran=subprocess.run([sys.executable,'-I',__file__,'--isolated'],env={'PATH':'/usr/bin:/bin','LANG':'C.UTF-8'},
                           capture_output=True,text=True)
        if ran.returncode: sys.exit(ran.stderr)
        rows=json.loads(ran.stdout.splitlines()[-1])
        OUT.write_text('[\n'+',\n'.join(json.dumps(row,ensure_ascii=True) for row in rows)+'\n]\n')
