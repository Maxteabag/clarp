#!/usr/bin/env python3
"""Record call-scoped speech policy using the Python Host in an empty replay home."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scratch as tool_scratch

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'server-rs/crates/clarp-kernel/tests/fixtures/call-transcription-vectors.json'


def record():
    spec = importlib.util.spec_from_file_location('host', ROOT / 'tests/replay/host.py')
    host = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(host)
    with tool_scratch.directory() as temp:
        root = Path(temp)
        (root / 'scenario-config.toml').write_text('')
        host.isolate(root)
        host.loopback_only()
        sys.path.insert(0, str(ROOT / 'server'))
        from lib import call_transcription as rule, config, stt_providers, stt_language
        config.load = lambda: SimpleNamespace(whisper_provider='faster-whisper', whisper_model='small.en')
        nova, google, scribe = 'deepgram:nova-3', 'google:chirp_3', 'elevenlabs:scribe_v2'
        settings, plans, scopes, resolutions = [], [], [], []
        models = [nova, google, scribe, 'cartesia:ink-2', 'faster-whisper:small', 'whisper.cpp:base.en',
                  'local', 'server-default', '', 'other:no', 'odd.en']
        for lang in ['en', 'no', 'no,en', 'en,no']:
            for order in [[m] for m in models] + [[google,nova], [scribe,google,nova]]:
                plans.append(dict(language=lang, engines=order, steps=rule.plan(order,lang),
                                  plain=rule.unchanged_plan(order)))
        for old_lang, old_engines in [('',[]),('en,no',[google]),('no',[nova])]:
            rule.call_languages = lambda: old_lang
            rule.call_engines = lambda: old_engines
            for lang in [None, '', ' ', ' EN,no,en ', 'da', 'eng', 'not-a-code', 'en,no,da,sv', 7]:
                for engines in [None, '', ' , ', nova, google, scribe, f' {google}, {scribe}', 'local', 'unknown', 7]:
                    for default in ['local',google]:
                        case=dict(old_language=old_lang, old_engines=old_engines,language=lang,engines=engines,default=default)
                        try:
                            case['ok']=rule.validate_settings(lang,engines,is_valid=lambda e: stt_providers._model_row(e) is not None,global_engine=default)
                        except ValueError as e:
                            case['error']=str(e)
                        settings.append(case)
        ident = 'gcl_'+'1'*32
        rule._server_id=lambda:'here'
        for given in ['', 'gcl_'+ '1'*31, 'gcl_'+'a'*32, 'gcl_'+'A'*32, 'gcl_'+'g'*32, 'gcl_'+'a'*31+'z', ident.upper(),ident+'0',' '+ident+' ',ident]:
            for owner, host_id, state, reason in [('','here','live',''),('other','here','live',''),('me','there','live',''),('me','here','ended',''),('me','here','ended','replaced'),('me','here','live','')]:
                for part in ['active','invited','on_hold','left']:
                    for session in ['','unknown',' a ']:
                        call=dict(call_id=ident,host_id=host_id,state=state,reason=reason,participants=[dict(session='a',state=part)])
                        rule._find_call=lambda k: (owner,call) if owner and k==ident else None
                        case=dict(id=given,owner=owner,call=call,session=session)
                        try:
                            case['ok']=rule.validate_scope('me',given,session)
                        except rule.CallError as e:
                            case['error']=[e.status,e.body()]
                        scopes.append(case)
        for lang in ['', 'no,en']:
            for engines in [[],[google,nova]]:
                for ownlang in ['', 'pt']:
                    for own in [[],[scribe]]:
                        for model in ['', 'local',nova]:
                            for glob in ['local',nova]:
                                stored = {rule.CALL_LANGUAGES_KEY: lang, rule.CALL_ENGINES_KEY: ','.join(engines),
                                          stt_providers.ENGINE_KEY: glob}
                                with patch.object(rule.settings_store, 'get_many', return_value=stored):
                                    policy = rule.policy_snapshot()
                                stt_language._for_session=lambda entries,session: ownlang
                                stt_language.agent_languages=lambda: {}
                                stt_language.engines_for=lambda session: own
                                stt_language.selected=lambda:'en'
                                resolved=rule.resolve('a',in_call=True,requested_model=model,policy=policy)
                                resolutions.append(dict(policy=policy,ownlang=ownlang,own=own,requested=model,resolved=resolved,
                                    view=rule.participants_view({'participants':[{'session':'a'}]},policy)))
        from lib import transcription_pipeline as pipeline
        attempts = []
        ticks=[0.0]
        class Engine:
            ready = SimpleNamespace(is_set=lambda: True)
            def transcribe_model_bytes(self, model, *_args, **_kwargs):
                ticks[0]+=0.125
                if model in noted:
                    stt_language.note_handed(stt_language.current_language())
                if model in fails:
                    raise RuntimeError('failed ' + model + 'x' * 210)
                return ('heard', False, 1.0)
        for lang in ['no','en,no']:
            for configured in [False,True]:
                for fails in [[],[scribe],[scribe,nova]]:
                    for noted in [[],[scribe,nova]]:
                        order=[scribe,nova]
                        steps=rule.plan(order,lang) if configured else rule.unchanged_plan(order)
                        fields={'language': {'value':lang,'source':'agent'},'engines':{'value':order,'source':'agent'},
                                'requested_model':None}
                        with patch.object(pipeline,'time',SimpleNamespace(monotonic=lambda:ticks[0])), patch.object(pipeline,'log_exception'):
                            try:
                                pipeline._run_call_steps(Engine(),steps,fields,focus='a',audio_bytes=b'a',ctype='audio/webm',prompt='')
                            except RuntimeError:
                                pass
                        attempts.append(dict(language=lang,configured=configured,fails=fails,noted=noted,
                                             report=fields['report']))
        from lib import group_calls
        views=[]
        for error in ['', 'invalid']:
            for state in ['live','ended']:
                for mode in ['', 'say','listen_first']:
                    for own_language in ['', 'da']:
                        for lang, engines in [('',[]),('no',[]),('',[google]),('no',[google])]:
                            configured=bool(lang or engines)
                            policy=dict(languages=lang,engines=engines,
                                        global_engine='local',configured=configured,error=error)
                            rule.policy_snapshot=lambda: policy
                            stt_language._for_session=lambda entries,session: own_language if session=='a' else None
                            stt_language.engines_for=lambda session: []
                            call=dict(state=state,participants=[dict(session='a'),dict(session='b')],_ring=dict(opening_mode=mode))
                            views.append(dict(policy=policy,state=state,mode=mode,language=own_language,
                                              view=group_calls.public(call).get('transcription')))
        return dict(settings=settings,plans=plans,scopes=scopes,resolutions=resolutions,attempts=attempts,views=views)


if __name__=='__main__':
    if sys.argv[1:]==['--record']:
        for k,v in record().items():
            print(json.dumps(k),json.dumps(v,ensure_ascii=True,separators=(',',':')))
    else:
        data=subprocess.check_output([sys.executable,'-I',__file__,'--record'],env=tool_scratch.child_env({'PATH':'/usr/bin:/bin','LANG':'C.UTF-8'}))
        rows={}
        for line in data.decode().splitlines():
            k,v=line.split(' ',1)
            rows[json.loads(k)]=json.loads(v)
        OUT.write_text('{\n'+',\n'.join(json.dumps(k)+':[\n'+',\n'.join(json.dumps(item,ensure_ascii=True,separators=(',',':')) for item in v)+'\n]' for k,v in rows.items())+'\n}\n')
        print({k:len(v) for k,v in rows.items()})
