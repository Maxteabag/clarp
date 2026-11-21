#!/usr/bin/env python3
"""Exercise the reference's voice-names refresh with scratch files and stand-ins."""
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import scratch

REPO = pathlib.Path(__file__).resolve().parents[2]
OUT = REPO / 'server-rs/crates/clarp-kernel/tests/fixtures/voice-names-vectors.json'


@scratch.case('clarp-voice-names-vectors-')
def reference(root):
    spec = importlib.util.spec_from_file_location('replay_host', REPO / 'tests/replay/host.py')
    host = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(host)
    (root / 'scenario-config.toml').write_text('')
    host.isolate(root)
    host.loopback_only()
    sys.path.insert(0, str(REPO / 'server'))
    from lib import config, log, voice_names

    now = 1_700_000_000
    startup = []
    for age, content in (
        (None, None), (-60, '{"_format":2}'), (0, '{"_format":2}'),
        (86399, '{"_format":2}'), (86400, '{"_format":2}'), (86401, '{"_format":2}'),
        (0, '{}'), (0, '{"_format":1}'), (0, '{"_format":3}'),
        (0, '{"_format":"2"}'), (0, '{broken'),
    ):
        path = root / f'age-{len(startup)}.json'
        if age is not None:
            path.write_text(content, encoding='utf-8')
            os.utime(path, (now - age, now - age))
        startup.append({'age': age, 'content': content, 'due': voice_names.start_due(path, now)})

    old = {'cartesia': {'missing': 'Former name', 'c1': 'Old Cartesia'},
           'elevenlabs': {'e1': 'Old Eleven'},
           '_catalogue': {'cartesia': {'retired': {'language': 'en', 'public': False, 'owner': True}}}}
    latest = {**old, '_format': 2}
    merged = []
    for case, current, cartesia, eleven, fetched in (
        ('new names replace only matched ids', old, True, True,
         {'cartesia': {'c1': 'New Cartesia', 'c2': 'New second'}, 'elevenlabs': {'e1': 'New Eleven'}}),
        ('a catalogue failure keeps every old Cartesia name', old, True, True,
         {'elevenlabs': {'e1': 'New Eleven'}}),
        ('failed catalogue drops stale rows', old, True, False, {}),
        ('failed catalogue keeps current rows', latest, True, False, {}),
        ('failed first fetch leaves no file', None, True, False, {}),
        ('no keys upgrade the old file', old, False, False, {}),
        ('no keys leave a current file alone', latest, False, False, {}),
        ('no keys create the first file', None, False, False, {}),
        ('ElevenLabs alone upgrades the old file', old, False, True,
         {'elevenlabs': {'e1': 'New Eleven'}}),
        ('equal names still publish a fresh catalogue', old, True, False,
         {'cartesia': {'c1': 'Old Cartesia'}}),
    ):
        names = root / (case.replace(' ', '-') + '.json')
        if current is not None:
            names.write_text(json.dumps(current, sort_keys=True), encoding='utf-8')
        cfg = SimpleNamespace(cartesia_key=lambda: 'test' if cartesia else '',
                              eleven_key=lambda: 'test' if eleven else '')
        def list_all(_):
            if 'cartesia' not in fetched:
                raise RuntimeError('stand-in catalogue not available')
            return [{'id': id, 'name': name, 'is_owner': False} for id, name in fetched['cartesia'].items()]
        with patch.object(log, 'log', lambda *a: None), patch.object(log, 'log_exception', lambda *a, **kw: None):
            done = voice_names.refresh(
                cfg, path=names, cartesia_all=list_all,
                cartesia_one=lambda *_: {},
                eleven_one=lambda _key, id: {'name': fetched['elevenlabs'][id]},
                voices_in_use=lambda _: {'cartesia': {'missing'}, 'elevenlabs': {'e1'}})
        update = dict(fetched)
        if 'cartesia' in fetched:
            update['_catalogue'] = {
                'cartesia': {id: {'language': '', 'public': False, 'owner': False}
                             for id in fetched['cartesia']},
            }
        if fetched:
            update['_gone'] = {provider: [] for provider in fetched}
        due = voice_names.start_due(names)
        with patch.object(voice_names, 'start_due', lambda: due):
            wait = voice_names.VoiceNamesWorker().next_wait()
        merged.append({'case': case, 'current': current, 'fetched': update, 'providers': done,
                       'has_key': cartesia or eleven, 'failed': cartesia and 'cartesia' not in fetched,
                       'result': json.loads(names.read_text()) if names.exists() else None,
                       'due': due, 'wait': wait})

    deleted_path = root / 'deleted.json'
    previous = {
        'cartesia': {'missing': 'Former name', 'retired': 'Keep the old label'},
        '_catalogue': {'cartesia': {'retired': {'language': 'no', 'public': False, 'owner': True}}},
    }
    update = {
        'cartesia': {'c1': 'Current voice'},
        '_catalogue': {'cartesia': {'c1': {'language': 'en', 'public': True, 'owner': False}}},
        '_gone': {'cartesia': ['missing']},
    }
    deleted_path.write_text(json.dumps(previous), encoding='utf-8')
    gone_lines = []
    def missing(_key, _id):
        raise type('Gone', (Exception,), {'status': 404})()
    with patch.object(voice_names, 'holders', return_value=['Fixture']), \
            patch.object(log, 'log', lambda *a: gone_lines.append(list(a)) if a[0] == 'voiceGone' else None):
        for _ in range(2):
            voice_names.refresh(
                SimpleNamespace(cartesia_key=lambda: 'test', eleven_key=lambda: ''),
                path=deleted_path, cartesia_all=lambda _: [
                    {'id': 'c1', 'name': 'Current voice', 'language': 'en', 'is_public': True, 'is_owner': False}],
                cartesia_one=missing, voices_in_use=lambda _: {'cartesia': {'missing'}})

    access_path = root / 'access.json'
    access_rows = [
        {'id': 'access', 'access': ' PUBLIC ', 'is_owner': False, 'language': 'en-US'},
        {'id': 'visible', 'visibility': ' All ', 'is_owner': False},
        {'id': 'public-visibility', 'visibility': ' PUBLIC ', 'is_owner': False},
        {'id': 'legacy', 'is_public': True, 'is_owner': False},
        {'id': 'legacy-string', 'is_public': 'true', 'is_owner': False},
        {'id': 'legacy-number', 'is_public': 1, 'is_owner': False},
        {'id': 'private', 'access': 'private', 'visibility': 'owner', 'is_owner': False},
        {'id': 'owned', 'access': 'public', 'is_owner': True},
        {'id': 'owner-missing', 'access': 'public'},
        {'id': 'owner-null', 'access': 'public', 'is_owner': None},
        {'id': 'owner-number', 'access': 'public', 'is_owner': 0},
        {'id': 'owner-string', 'access': 'public', 'is_owner': 'false'},
    ]
    with patch.object(log, 'log', lambda *a: None):
        voice_names.refresh(
            SimpleNamespace(cartesia_key=lambda: 'test', eleven_key=lambda: ''),
            path=access_path, cartesia_all=lambda _: access_rows, voices_in_use=lambda _: {})
    access = {'rows': access_rows, 'result': json.loads(access_path.read_text())}

    list_path = root / 'limit.json'
    used = {f'voice{i:03}' for i in range(109)}
    pages = [{'id': 'voice001', 'name': 'Listed'}, {'id': 'voice002', 'name': 'Listed as well'}]
    looked_up = []
    cfg = SimpleNamespace(cartesia_key=lambda: 'test', eleven_key=lambda: '')
    def by_id(_key, id):
        looked_up.append(id)
        return {'id': id, 'name': 'By ID'}
    with patch.object(log, 'log', lambda *a: None):
        voice_names.refresh(cfg, path=list_path, cartesia_all=lambda _: pages,
                            cartesia_one=by_id, voices_in_use=lambda _: {'cartesia': used})
    refusals = []
    for provider, status in (('cartesia', 401), ('cartesia', 403), ('cartesia', 503),
                             ('elevenlabs', 401), ('elevenlabs', 403)):
        refused_path = root / f'{provider}-{status}.json'
        calls = []
        refusal_events = []

        def cfg(mark):
            return SimpleNamespace(cartesia_key=lambda: mark if provider == 'cartesia' else '',
                                   eleven_key=lambda: mark if provider == 'elevenlabs' else '')

        def reject(key, *_):
            calls.append(1)
            raise type('Http', (Exception,), {'status': status})()

        def refresh(mark, succeeds=False):
            def found(key, *_):
                calls.append(1)
                return {'id': 'voice', 'name': 'Test voice'}
            voice_names.refresh(
                cfg(mark), path=refused_path,
                cartesia_all=(lambda key: [found(key)]) if succeeds else reject,
                cartesia_one=lambda *_: {},
                eleven_one=found if succeeds else reject,
                voices_in_use=lambda _: {'elevenlabs': {'e1', 'e2'}})

        with patch.object(log, 'log', lambda *a: refusal_events.append(a[1]) if a[0] == 'voiceNamesKeyRefused' else None), \
                patch.object(log, 'log_exception', lambda *a, **kw: None):
            refresh('fixture-old')
            saved = json.loads(refused_path.read_text()) if refused_path.exists() else {}
            refresh('fixture-old')
            repeat_count = len(calls)
            with patch.object(config, 'load', return_value=cfg('fixture-old')):
                same_due = voice_names.start_due(refused_path)
            with patch.object(config, 'load', return_value=cfg('fixture-new')):
                changed_due = voice_names.start_due(refused_path)
            refresh('fixture-new', succeeds=True)
        new = json.loads(refused_path.read_text())
        refusals.append({'provider': provider, 'status': status, 'marks': saved.get('_refused', {}),
                         'same_due': same_due, 'changed_due': changed_due, 'repeat_count': repeat_count,
                         'events': refusal_events, 'after': new.get('_refused', {})})

    worker = voice_names.VoiceNamesWorker()
    backoff = []
    for due in (True, True, True, True, True, False, True):
        with patch.object(voice_names, 'start_due', lambda: due):
            backoff.append({'failed': due, 'wait': worker.next_wait(), 'retry': worker._retries > 0})

    return {'startup': startup, 'merge': merged, 'access': access,
            'refusals': refusals, 'backoff': backoff,
            'deleted': {'current': previous, 'fetched': update, 'result': json.loads(deleted_path.read_text()),
                        'logged': gone_lines},
            'lookup': {'used': sorted(used), 'listed': {row['id']: row['name'] for row in pages},
                       'asked': looked_up}}


def main():
    if sys.argv[1:] == ['--child']:
        print(json.dumps(reference(), ensure_ascii=True))
        return
    ran = subprocess.run([sys.executable, '-I', __file__, '--child'], capture_output=True, text=True,
                         env=scratch.child_env({'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}), check=False)
    if ran.returncode:
        print(ran.stderr[-3000:], file=sys.stderr)
        raise SystemExit(ran.returncode)
    cases = json.loads(ran.stdout.splitlines()[-1])
    OUT.write_text(json.dumps(cases, ensure_ascii=True, sort_keys=True, indent=2) + '\n', encoding='ascii')
    print(f"{len(cases['startup'])} ages, {len(cases['merge'])} merges, {len(cases['lookup']['asked'])} bounded lookups")


if __name__ == '__main__':
    main()
