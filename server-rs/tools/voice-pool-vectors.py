#!/usr/bin/env python3
"""Vectors from the reference's V2b pool and name-preserving rename rules."""
from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess
import sys
from collections import Counter
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import scratch

REPO = pathlib.Path(__file__).resolve().parents[2]
OUT = REPO / 'server-rs/crates/clarp-kernel/tests/fixtures/voice-pool-vectors.json'
CART = 'cartesia'


@scratch.case('clarp-voice-pool-vectors-')
def reference(root):
    spec = importlib.util.spec_from_file_location('replay_host', REPO / 'tests/replay/host.py')
    host = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(host)
    (root / 'scenario-config.toml').write_text('')
    host.isolate(root)
    host.loopback_only()
    sys.path.insert(0, str(REPO / 'server'))
    from lib import config, log, settings_store, stt_language, voice_names, voice_pool, voice_profile

    picks = []
    for name, candidates, taken, reserved in (
        ('no pool', [], {}, []),
        ('Case name', ['a', 'b', 'c', 'd'], {}, []),
        ('  CASE NAME  ', ['a', 'b', 'c', 'd'], {}, []),
        ('name sharp ' + chr(0xdf), ['a', 'b', 'c', 'd', 'e'], {}, []),
        ('skip occupied', ['a', 'b', 'c', 'd'], {'a': 1, 'c': 1}, []),
        ('least shared', ['a', 'b', 'c'], {'a': 4, 'b': 3, 'c': 1}, []),
        ('reserve even a free voice', ['a', 'b', 'c'], {'b': 2, 'c': 5}, ['a']),
        ('all reserved use the walk', ['a', 'b'], {}, ['a','b']),
        ('a tie keeps walk order', ['a', 'b', 'c'], {'a': 1, 'b': 1, 'c': 1}, []),
    ):
        with patch.object(voice_pool, 'pool', lambda *_: candidates), patch.object(log, 'log', lambda *a: None):
            result = voice_pool.pick(config.Config(), CART, name, Counter(taken), keep=set(reserved))
        picks.append({'name': name, 'pool': candidates, 'taken': taken, 'keep': reserved, 'result': result})

    def stored(provider, id):
        return json.dumps({provider:id}, separators=(',',':'))

    builtin = {'name':'Built In', 'voice_id':stored(CART,'reserved'), 'builtin':True}
    active = {'persona':'Has Voice', 'voice_id':stored(CART,'c1')}
    retired = {**active, 'archived_at':1}
    janitor = {**active, 'is_janitor':1}
    custom = {'name':'Custom','voice_id':stored(CART,'c1')}
    assignments = []
    assignment_names = root / "assignment-voice-names.json"
    for name, raw, extra, known, agents, contacts in (
        ('Free', '{}', {}, [], [], []),
        ('Free', '{}', {}, [], [active], [custom, builtin]),
        ('Free', '{}', {}, [], [retired, janitor], [builtin]),
        ('Free', '{}', {}, [], [], [builtin]),
        ('Catalogue', '{}', {'cartesia_pool': ()}, ['c2','c1','reserved'], [], [builtin]),
        ('Catalogue', '{}', {'cartesia_pool': ('c2','c1','c2')}, ['not-used'], [], []),
        ('Exhausted', '{}', {'cartesia_pool':('c1','c2')}, [],
         [active,active,{'persona':'Other','voice_id':stored(CART,'c2')}], []),
        ('Ada', 'e-stored', {'cartesia_voices':{'Ada':'c1'}}, [], [], []),
        ('Other', stored(CART,'auto-123'), {}, [], [], []),
        ('Other', 'c0000000-0000-4000-8000-0000000000ad', {}, [], [], []),
        ('Other', 'nb-NO-Chirp3-HD-Kore', {}, [], [], []),
        ('Gemini Override', '{}', {'tts_agent_overrides':{'gemini override':{'provider':'gemini'}}}, [], [], []),
        ('Fallback', '{}', {'tts_fallback':'elevenlabs'}, [], [], []),
        ('Fallback', stored('elevenlabs','some-valid-id'), {'tts_fallback':'elevenlabs'}, [], [], []),
        ('Fallback', stored('elevenlabs','some invalid id'), {'tts_fallback':'elevenlabs'}, [], [], []),
        ('Silent Host', '{}', {'tts_provider':'none'}, [], [], []),
        ('Deepgram', '{}', {'tts_provider':'deepgram'}, [], [], []),
        ('Google', '{}', {'tts_provider':'google','tts_fallback':'cartesia'}, [], [], []),
    ):
        settings = dict(tts_provider='cartesia', tts_fallback='none', cartesia_pool=('reserved','c1','c2'),
                        cartesia_voices={'Built In':'reserved'}, gemini_voices={}, roster={},
                        **{k:v for k,v in extra.items() if k not in ('tts_provider','tts_fallback','cartesia_pool','cartesia_voices','gemini_voices','roster')})
        settings.update(extra)
        cfg = config.Config(**settings)
        catalogue = {voice: {'language': 'en', 'public': True, 'owner': False} for voice in known}
        assignment_names.write_text(json.dumps({'_catalogue': {CART: catalogue}}), encoding='utf-8')
        logs = []
        with patch.object(voice_names, 'names_path', lambda: assignment_names), \
                patch.object(log, 'log', lambda kind, msg: logs.append([kind, msg])):
            assigned = voice_pool.assign(cfg,name,raw,agents=agents,contacts=contacts)
        assignments.append({'name':name,'stored':raw,'settings':settings,'catalogue':known,
                            'agents':agents,'contacts':contacts,'result':assigned,'logs':logs})
    cfg = config.Config(tts_provider='cartesia', cartesia_voices={'Ada':'c1'})
    pinned = voice_pool.pinned_before_rename(cfg,{'persona':'Ada','voice_id':'{}'})
    janitor = voice_profile.speech(voice_profile.HostView.build(cfg, stored={}),
                                   {'persona':'Ada','voice_id':'{}','is_janitor':1})
    default = voice_profile.defaults(voice_profile.HostView.build(cfg, stored={}))
    path = root / 'private-voice-names.json'
    def row(lang, public=True, owner=False):
        return {'language':lang, 'public':public, 'owner':owner}
    file = {'cartesia': {'remembered': 'Remembered'}, '_gone': {'cartesia':['deleted']},
            '_catalogue': {'cartesia': {
                'en':row('EN_us'), 'owned':row('en', owner=True), 'private':row('en', public=False),
                'nb':row('nb-NO'), 'nn':row('nn'), 'no':row('no'), 'de':row('de'),
                'deleted':row('en'), 'unmarked':row(''),
                'missing-owner':{'language':'en', 'public':True},
                'string-public':row('en', public='true'), 'number-owner':row('en', owner=0),
            }}}
    path.write_text(json.dumps(file), encoding='utf-8')
    cfg = config.Config(tts_provider=CART, cartesia_voices={})
    def selection(language):
        lines = []
        with patch.object(log, 'log', lambda *args: lines.append(args)):
            ids = voice_pool._cartesia_catalogue(language)
        miss = None
        if lines:
            [(event, detail)] = lines
            assert event == 'voicePoolNoCatalogueVoice'
            fields = dict(word.split('=', 1) for word in detail.split())
            assert fields['provider'] == CART and fields['eligible'] == '0'
            miss = {'language': fields['language'], 'total': int(fields['total'])}
        return {'language':language, 'ids':ids, 'miss':miss}

    with patch.object(voice_names, 'names_path', lambda: path):
        languages = [selection(language) for language in ('en', 'nb-NO', 'NN_no', 'de', ' ZH_cn ', '')]
        settings_store.set_text(stt_language.AGENT_LANGUAGES_KEY, json.dumps({'ola':'nn,en'}))
        ola = voice_pool.assign(cfg, 'Ola', '{}', agents=[], contacts=[])
        quiet = []
        for quiet_file in ({}, {'_catalogue': {CART: {}}}):
            path.write_text(json.dumps(quiet_file), encoding='utf-8')
            quiet.append({'file':quiet_file, **selection('en')})
    catalogue = {'file':file,'languages':languages,'ola':ola,'quiet':quiet}
    return {'picks':picks,'assignments':assignments,'pinned':pinned,
            'janitor':janitor,'default_assignment':default['speech']['assignment'],
            'catalogue':catalogue}


def main():
    if sys.argv[1:] == ['--child']:
        print(json.dumps(reference(),ensure_ascii=True))
        return
    ran=subprocess.run([sys.executable,'-I',__file__,'--child'],capture_output=True,text=True,
                       env=scratch.child_env({'PATH':'/usr/bin:/bin','LANG':'C.UTF-8'}),check=False)
    if ran.returncode:
        print(ran.stderr[-3000:],file=sys.stderr)
        raise SystemExit(ran.returncode)
    data=json.loads(ran.stdout.splitlines()[-1])
    OUT.write_text(json.dumps(data,sort_keys=True,ensure_ascii=True,indent=2)+'\n',encoding='ascii')
    print(f"voice-pool vectors: {len(data['picks'])} walks, {len(data['assignments'])} assignments")

if __name__ == '__main__':
    main()
