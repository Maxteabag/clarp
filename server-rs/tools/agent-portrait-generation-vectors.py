#!/usr/bin/env python3
"""Record portrait routes and paid edit decisions with an isolated store and no provider."""
from contextlib import ExitStack
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

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'server-rs/crates/clarp-kernel/tests/fixtures/portrait-generation.json'


def starts(root, gen):
    """Drive the real handler and launcher branches; no process or request is started."""
    from lib.routes.personas import PersonaRoutes
    rows=[]
    for body in [None, [], {}, {'session':'fixture'}, {'session':'missing'}, {'session':'janitor'}]:
        for prior in [None,'queued','running','succeeded','failed','cancelled']:
            for able in (False,True):
                with ExitStack() as guards:
                    agent={'agent_id':'fixture-id','persona':'Fixture','session':'fixture'}
                    guards.enter_context(patch.object(gen.agents,'get_by_session',
                        side_effect=lambda session: agent if session=='fixture' else None))
                    known={'job_id':gen.job_id_for('fixture-id'),'generation':1,'status':prior} if prior else None
                    guards.enter_context(patch.object(gen,'capability',return_value={
                        'available':able,'reason':'' if able else 'Add or select a primary portrait first','job':known}))
                    guards.enter_context(patch.object(gen.background_jobs,'get',return_value=known))
                    queued={'job_id':gen.job_id_for('fixture-id'),'generation':2 if known else 1,'status':'queued'}
                    register=guards.enter_context(patch.object(gen.background_jobs,'upsert_computer',return_value=queued))
                    guards.enter_context(patch.object(gen.background_jobs,'claim_queued_launch',return_value=True))
                    guards.enter_context(patch.object(gen.server_identity,'get_server_info',return_value={'server_id':'fixture-computer'}))
                    guards.enter_context(patch.dict(os.environ,{'CLARP_DEPLOYMENT_MODE':'container'}))
                    launched=guards.enter_context(patch.object(gen.subprocess,'Popen'))
                    answers=[]
                    handler=SimpleNamespace(_read_json=lambda:body,ctx=SimpleNamespace(default_session='fixture',media_dir=root),
                        _json_error=lambda code,why: answers.append([code,why]),
                        _json=lambda code,reply: answers.append([code,None]))
                    def janitor(data):
                        if isinstance(data,dict) and data.get('session')=='janitor':
                            answers.append([403,'janitor_inspection_only'])
                            return True
                        return False
                    handler._reject_janitor_control=janitor
                    PersonaRoutes._handle_agent_portrait_generation_start(handler)
                    rows.append([body,prior,able,*answers[0],register.called,launched.called])
    return rows


def record(root):
    spec = importlib.util.spec_from_file_location('replay_host', ROOT / 'tests/replay/host.py')
    replay = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(replay)
    replay.isolate(root)
    replay.loopback_only()
    sys.path.insert(0, str(ROOT / 'server'))
    from lib import avatar_settings, portrait_generation as gen
    from lib.routes.settings import SettingsRoutes

    preferences = []
    for value in (None, '', '1', 'true', 'YES', ' on ', '0', 'false', '2', 'enabled'):
        stored = {} if value is None else {avatar_settings.KEY: value}
        preferences.append([value, avatar_settings.get(stored=stored)])
    updates = []
    for body in (None, {}, {'model_avatars': None}, {'model_avatars': 1},
                 {'model_avatars': 'true'}, {'model_avatars': True}, {'model_avatars': False}):
        answers = []
        handler = SimpleNamespace(_read_json=lambda: body,
                                  _json_error=lambda code, why: answers.append([code, {'error': why}]),
                                  _json_ok=lambda result: answers.append([200, result]))
        SettingsRoutes._handle_avatar_settings_post(handler)
        updates.append([body, answers[0]])
    png = b'\x89PNG\r\n\x1a\nfixture'
    source = root / 'source.png'
    source.write_bytes(png)
    primary = {'storage_path': str(source), 'mime_type': 'image/png'}
    fake_agent = {'agent_id': 'fixture', 'session': 'fixture', 'persona': 'Fixture'}
    cases = []
    with ExitStack() as stack:
        lookup = stack.enter_context(patch.object(gen.agents, 'get_by_session'))
        config = stack.enter_context(patch.object(gen.config, 'load'))
        stack.enter_context(patch.object(gen.background_jobs, 'get', return_value=None))
        stack.enter_context(patch.object(gen.agent_portraits, 'list_for_session',
                                        return_value={'primary_portrait_id': 'primary'}))
        content = stack.enter_context(patch.object(gen.agent_portraits, 'get_content'))
        for known, key, present, mime, size in [
                (False, False, False, '', 0), (True, False, False, '', 0),
                (True, True, False, '', 0), (True, True, True, 'image/gif', 1),
                *((True, True, True, m, n) for m in ['image/png','image/jpeg','image/webp']
                  for n in [0, gen.MAX_SOURCE_BYTES-1, gen.MAX_SOURCE_BYTES, gen.MAX_SOURCE_BYTES+1])]:
            with source.open('wb') as f:
                f.truncate(size)
            lookup.return_value = fake_agent if known else None
            config.return_value = SimpleNamespace(openai_key=lambda: 'fixture-only' if key else '')
            content.return_value = dict(primary, mime_type=mime) if present else None
            cap = gen.capability('fixture', media_dir=root)
            cases.append([known,key,present,mime,size,cap])
        source.write_bytes(png)
        content.return_value = primary
        edits = []
        for results in (['ok','ok'],['empty'],['invalid'],['raise'],['ok','invalid'],['ok','raise'],['ok','duplicate']):
            calls = []
            def request(**asked):
                calls.append(asked['prompt'])
                result = results[len(calls)-1]
                if result == 'raise':
                    raise gen.GenerationError('fixture refusal')
                return {'ok': png+bytes([len(calls)]), 'empty': b'',
                        'invalid': b'not an image', 'duplicate': png+b'\x01'}[result]
            published = []
            def publish(**what):
                published.append(what['source_name'])
                return {'asset_id': what['source_name']}
            with patch.object(gen.media_store,'publish',side_effect=publish), patch.object(
                    gen.agent_portraits,'replace_alternates_with_media_assets',return_value={}) as replace:
                try:
                    gen.generate_two('fixture',handle='bg1:1:example',media_dir=root,request=request)
                    error = None
                except gen.GenerationError as exc:
                    error = str(exc)
                edits.append([results,len(calls),published,replace.called,error])
    return dict(starts=starts(root, gen),preferences=preferences,updates=updates,capabilities=cases,edits=edits,
                ids=[[agent,gen.job_id_for(agent)] for agent in ('','A','a','fixture',chr(233))],
                prompts=list(gen.PROMPTS),
                multipart=[gen._multipart('fixture',{'model':gen.MODEL,'prompt':prompt,
                           'size':'1024x1024','quality':'medium'},source=b'fixture',source_mime=mime).decode()
                           for prompt in gen.PROMPTS for mime in ('image/png','image/jpeg','image/webp')])


if __name__ == '__main__':
    if '--record' in sys.argv:
        with scratch.directory() as area:
            print(json.dumps(record(Path(area))))
    else:
        env = scratch.child_env({'PATH':os.defpath,'LANG':'C.UTF-8','TZ':'UTC'})
        data=json.loads(subprocess.check_output([sys.executable,'-I',__file__,'--record'], env=env))
        OUT.write_text(json.dumps(data,indent=2,ensure_ascii=True)+'\n', encoding='ascii')
        print({key:len(rows) for key,rows in data.items()})
