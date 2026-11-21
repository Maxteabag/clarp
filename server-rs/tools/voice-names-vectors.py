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
    from lib import log, voice_names

    now = 1_700_000_000
    path = root / 'age.json'
    startup = []
    for age in (None, -60, 0, 86399, 86400, 86401):
        if age is not None:
            path.write_text('{}', encoding='utf-8')
            os.utime(path, (now - age, now - age))
        startup.append({'age': age, 'due': voice_names.start_due(path, now)})

    current = {'cartesia': {'missing': 'Former name', 'c1': 'Old Cartesia'},
               'elevenlabs': {'e1': 'Old Eleven'}}
    merged = []
    for case, fetched in (
        ('new names replace only matched ids', {'cartesia': {'c1': 'New Cartesia', 'c2': 'New second'},
                                                'elevenlabs': {'e1': 'New Eleven'}}),
        ('a catalogue failure keeps every old Cartesia name', {'elevenlabs': {'e1': 'New Eleven'}}),
        ('no keys leave the file alone', {}),
        ('equal names leave file content unchanged', {'cartesia': {'c1': 'Old Cartesia'}}),
    ):
        names = root / (case.replace(' ', '-') + '.json')
        names.write_text(json.dumps(current, sort_keys=True), encoding='utf-8')
        before = names.read_bytes()
        cfg = SimpleNamespace(cartesia_key=lambda: 'test' if fetched else '',
                              eleven_key=lambda: 'test' if 'elevenlabs' in fetched else '')
        def list_all(_):
            if 'cartesia' not in fetched:
                raise RuntimeError('stand-in catalogue not available')
            return [{'id': id, 'name': name} for id, name in fetched['cartesia'].items()]
        with patch.object(log, 'log', lambda *a: None), patch.object(log, 'log_exception', lambda *a, **kw: None):
            done = voice_names.refresh(
                cfg, path=names, cartesia_all=list_all,
                cartesia_one=lambda *_: {},
                eleven_one=lambda _key, id: {'name': fetched['elevenlabs'][id]},
                voices_in_use=lambda _: {'cartesia': {'missing'}, 'elevenlabs': {'e1'}})
        merged.append({'case': case, 'current': current, 'fetched': fetched, 'providers': done,
                       'result': json.loads(names.read_text()), 'same_bytes': names.read_bytes() == before})

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
    return {'startup': startup, 'merge': merged,
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
