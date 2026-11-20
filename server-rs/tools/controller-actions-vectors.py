#!/usr/bin/env python3
"""Controller action rules and durable answers from the reference in an isolated store."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scratch as tool_scratch

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
    from lib import config, url_destination
    entries = ['https://service.invalid/api', 'http://service.invalid:8123/line/']
    # Where an action may send, and what is sent (R133, sendable_url): the URL rebuilt from its parsed
    # parts, or None. Every host is this machine's own (in any of its spellings) or a made-up name under
    # .invalid.
    cases = ['http://127.0.0.1', 'http://localhost:123/x', 'http://[::1]:80/', 'https://localhost/',
             'https://service.invalid/api?q=x', 'https://service.invalid/api#part', 'https://service.invalid/api/sub',
             'https://service.invalid/apix', 'http://service.invalid:8123/line/a', 'http://service.invalid:8124/line/a',
             'http://service.invalid/line/', 'http://service.invalid:8123/other', 'ftp://localhost/a', 'not-an-address',
             'http://[', 'https://service.invalid/api:late', 'http://fixture@localhost/', 'http://@localhost/',
             'http://service.invalid/api', 'https://unlisted.invalid/api', 'https://service.invalid/api',
             'https://service.invalid/api/one', 'http://service.invalid:65536/', 'http:///',
             # (a) a prefix that names no port admits the scheme's own port alone
             'https://service.invalid:443/api/x', 'https://service.invalid:8443/api', 'http://localhost:9/x',
             # (b) the port is named only where it is not the scheme's
             'http://127.0.0.1:80/x', 'http://127.0.0.1:8080/x', 'https://service.invalid:443/api',
             # (d) what a host may be: plain labels or an IPv6 literal; another spelling of an address is a name
             'http://2130706433:8123/x', 'http://127.1/x', 'http://0x7f.0.0.1/x', 'http://0177.0.0.1/x',
             'http://LOCALHOST:9/x', 'HTTPS://SERVICE.INVALID/api', 'https://service.invalid./api',
             'https://service_x.invalid/api', 'https://-service.invalid/api', 'http://loc%61lhost/x',
             'http://[0:0:0:0:0:0:0:1]/x', 'http://[0::1]:8080/x', 'http://[::FFFF:127.0.0.1]/x', 'http://[::1%25lo]/x',
             'http://[127.0.0.1]/x', 'http://[v1.service.invalid]/x', 'http://[::1]x/', 'http://x[::1]/', 'http://[::1/',
             'http://someone@127.0.0.1:8123/x', 'http://someone:secret@localhost/x', 'http://localhost:/x',
             'http://localhost:080/x', 'http://localhost:x/', 'http://localhost:\u0661/x', 'http://localhost /x',
             'http://localhost/x y', 'http://localhost/\u00a0', 'http://localhost/x\x7f', 'http://localhost/x\x01', 'http://localhost:+80/x',
             # (e) the path's boundary is of the parsed path alone
             'http://service.invalid:8123/line', 'http://service.invalid:8123/line?x=1', 'http://service.invalid:8123/linex',
             'https://service.invalid/api?', 'https://service.invalid?api', 'https://service.invalid/api/..x', 'https://service.invalid/api/x..',
             'https://service.invalid/api/...', 'https://service.invalid/api/%2e%2ex']
    for url in cases:
        rows.append(dict(kind='address', url=url, entries=entries, reference=ca.sendable_url(url, entries)))
    # AWAITING R139 (the release owner's PR to come): a path with a dot segment, raw or percent-encoded, is
    # refused. Not on his main yet, so what is written here is the reference as it is, which admits them; the
    # Rust Host refuses them already, and its test asserts that these rows still say the reference admits
    # them: when his PR is merged and this runs again, that assertion fails, and the mark goes.
    r139 = ['https://service.invalid/api/../x', 'https://service.invalid/api/./x', 'https://service.invalid/api/%2e%2e/x',
            'https://service.invalid/api/%2E%2e/x', 'https://service.invalid/api/.%2E/x', 'https://service.invalid/api/%2e/x',
            'https://service.invalid/api/sub/../one', 'https://service.invalid/api/..', 'http://127.0.0.1:8123/a/../b']
    for url in r139:
        rows.append(dict(kind='address', url=url, entries=entries, reference=ca.sendable_url(url, entries),
                         awaiting='R139'))

    def shown(found):
        return None if found is None else dict(scheme=found.scheme, host=found.host, port=found.port,
                                               path=found.path, query=found.query, url=found.url())
    # (c) a prefix with a query or a fragment is refused at configuration, counted in one line.
    prefixes = ['https://service.invalid/api', ' https://service.invalid/api ', 'https://service.invalid/q?x=1',
                'https://service.invalid/f#top', 'https://service.invalid/f#', 'https://service.invalid/q?',
                'https://u@service.invalid', 'https://service.invalid:abc', 'not a url', 'https://', '',
                'http://service.invalid:8123/line/', 'HTTP://Service.Invalid:80/', 'http://[::1]:8123']
    for text in prefixes:
        rows.append(dict(kind='parse', text=text, destination=shown(url_destination.destination(text)),
                         prefix=shown(url_destination.prefix(text))))
    for url in cases:
        rows.append(dict(kind='parse', text=url, destination=shown(url_destination.destination(url)),
                         prefix=shown(url_destination.prefix(url))))
    for url in r139:
        rows.append(dict(kind='parse', text=url, destination=shown(url_destination.destination(url)),
                         prefix=shown(url_destination.prefix(url)), awaiting='R139'))
    told = []
    with patch('lib.log.log', lambda event, detail='', *a, **k: told.append([event, detail])):
        for given in (prefixes, ['https://service.invalid/api'], [], [' ', 'https://service.invalid/q?x=1']):
            told.clear()
            kept = config._controller_action_urls(given)
            rows.append(dict(kind='config', given=given, kept=list(kept), told=list(told)))
    # What a prefix that does not parse admits: nothing.
    for text in ('https://service.invalid/q?x=1', 'https://u@service.invalid'):
        rows.append(dict(kind='address', url='https://service.invalid/q?x=1', entries=[text],
                         reference=ca.sendable_url('https://service.invalid/q?x=1', [text])))
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
    # R132: A runs; B inside the window is answered debounced; B repeated after the window is that answer
    # again with duplicate: true, and nothing runs a second time.
    ca._reset_runtime_state()
    calls = []
    def post(*_):
        calls.append(1)
        return 200, 'fixture'
    with patch.object(ca.time,'monotonic',return_value=100):
        first=ca.run('hook',actor='fixture',request_id='R-A',http_post=post)
    with patch.object(ca.time,'monotonic',return_value=100.5):
        debounced=ca.run('hook',actor='fixture',request_id='R-B',http_post=post)
    with patch.object(ca.time,'monotonic',return_value=110):
        again=ca.run('hook',actor='fixture',request_id='R-B',http_post=post)
    rows.append(dict(kind='repeat',first=first,debounced=debounced,again=again,runs=len(calls)))
    return rows


if __name__=='__main__':
    if '--isolated' in sys.argv:
        with tool_scratch.directory() as tmp:
            print(json.dumps(generate(Path(tmp)),ensure_ascii=True))
    else:
        ran=subprocess.run([sys.executable,'-I',__file__,'--isolated'],env=tool_scratch.child_env({'PATH':'/usr/bin:/bin','LANG':'C.UTF-8'}),
                           capture_output=True,text=True)
        if ran.returncode: sys.exit(ran.stderr)
        rows=json.loads(ran.stdout.splitlines()[-1])
        OUT.write_text('[\n'+',\n'.join(json.dumps(row,ensure_ascii=True) for row in rows)+'\n]\n')
