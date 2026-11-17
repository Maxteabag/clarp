#!/usr/bin/env python3
"""Writes the vectors the Rust Host's catalogue of personas is tested against.

A persona is who an agent can be: a name with its voices, how it speaks, the kind of tool it
runs on, and a face. The Python Host has many built in (the names in its roster, each with the
voices the owner's file or the Host's own lists give it) and lets the owner make more. An app
reads them in `GET /agents/snapshot` (`personas`) to offer them when an agent is made.

This asks the Python Host for its built-in personas under several made-up owner's files and
writes what it answered to `crates/clarp-kernel/tests/fixtures/persona-vectors.json`, with the
lists the Host carries as data (`crates/clarp-kernel/src/persona-lists.json`): the roster and its
voices, the personalities, and which names belong to which kind of tool.

    "$(sed -n 1p ~/.local/share/clarp/current/SERVICE_PYTHON)" server-rs/tools/persona-vectors.py

Each case runs in a process of its own whose every home, data, config and cache directory is a
new empty directory (the replay harness's own isolation, `tests/replay/host.py`), with no way
out of the machine. The database a case touches is a new one in that directory. No Host is
touched or started.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
import tomllib

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[1]
OUT = HERE.parent / 'crates' / 'clarp-kernel' / 'tests' / 'fixtures' / 'persona-vectors.json'
LISTS = HERE.parent / 'crates' / 'clarp-kernel' / 'src' / 'persona-lists.json'

CASES = [
    {'name': 'nothing of the owner', 'config': ''},
    {'name': 'the owner names voices and one more persona',
     'config': '[roster]\nZed = "voice-zed"\nMike = "voice-mike-of-the-owner"\n"Zoë Å" = "voice-zoe"\n'
               '[cartesia]\napi_key = "made-up"\n[cartesia.voices]\nZed = "c-zed"\nmike = "c-not-mike-by-its-letters"\n'
               'Rachel = "c-rachel-of-the-owner"\n'
               '[gemini_tts]\napi_key = "made-up"\n[gemini_tts.voices]\nmike = "Puck"\nRACHEL = "Kore"\nZed = " Zephyr "\n'
               '"zoë å" = "Leda"\n'},
    {'name': 'a persona named as an agent nobody named is',
     'config': '[roster]\nClaude-5628 = "voice-anonymous"\nMike-00ff = ""\nmike-zzzz = "voice-not-hex"\n'
               '[gemini_tts.voices]\nMike = "Charon"\n'},
]

# What an owner does with personas of his own, in a room with this owner's file: each step is
# made, changed or removed by the Python Host's own functions, and what it answered (the persona
# as an app is told it, or the words it was refused with) is written down, with the voice the
# next persona would be given were it to name none.
PICTURE = 'YSBtYWRlLXVwIHBpY3R1cmU='          # "a made-up picture"
ANOTHER = 'YW5vdGhlciBwaWN0dXJl'              # "another picture"
OWN = [
    {'name': 'personas of the owner, on a Host with nothing of his in its file', 'config': '', 'steps': [
        {'create': {'name': '  Nova  ', 'voice_id': '', 'personality': 'Personality: brisk.'}},
        {'create': {'name': 'nova'}},
        {'create': {'name': 'MIKE'}},
        {'create': {'name': ''}},
        {'create': {'name': 'x' * 61}},
        {'create': {'name': 'x' * 60, 'voice_id': '{"gemini":"Kore"}', 'avatar_symbol': 's' * 70,
                    'personality': 'p' * 4100}},
        {'create': {'name': 'Pictured', 'voice_id': 'plain-eleven-id', 'avatar_base64': PICTURE}},
        {'create': {'name': 'Zoë', 'avatar_symbol': 'star.fill'}},
        {'create': {'name': 'ZOË'}},
        {'update': {'original_name': 'nobody-of-the-kind', 'name': 'New'}},
        {'update': {'original_name': 'Nova', 'name': '', 'voice_id': ''}},
        {'update': {'original_name': 'Nova', 'name': 'Pictured', 'voice_id': ''}},
        {'update': {'original_name': 'nova', 'name': 'Nova Prime', 'voice_id': '', 'personality': 'Personality: slower.',
                    'avatar_symbol': 'moon'}},
        {'update': {'original_name': 'Nova Prime', 'name': 'NOVA PRIME', 'voice_id': '{"cartesia":"c-chosen"}'}},
        {'update': {'original_name': 'Pictured', 'name': 'Pictured', 'voice_id': 'plain-eleven-id',
                    'avatar_base64': ANOTHER}},
        {'update': {'original_name': 'Mike', 'name': 'Michael', 'voice_id': 'ignored', 'avatar_symbol': 'bolt',
                    'personality': 'ignored'}},
        {'update': {'original_name': 'Rachel', 'name': 'Rachel', 'voice_id': '', 'avatar_base64': PICTURE}},
        {'delete': 'Mike'},
        {'delete': 'nobody-of-the-kind'},
        {'delete': 'zoë'},
        {'delete': 'Zoë'},
        {'delete': 'pictured'},
        {'create': {'name': 'Pictured'}},
    ]},
    {'name': 'a voice of the owner that nobody has', 'config':
        '[cartesia.voices]\nspare = "c-spare-of-the-owner"\nRachel = "c-rachel-of-the-owner"\n', 'steps': [
        {'create': {'name': 'First'}},
        {'create': {'name': 'Second'}},
        {'create': {'name': 'Third', 'voice_id': '{}'}},
        {'delete': 'First'},
        {'create': {'name': 'Fourth'}},
    ]},
]

# Names asked which kind of tool they belong to, and how they speak.
NAMES = ['Mike', 'mike', ' MIKE ', 'Rachel', 'Codex', 'codex default', 'Claude', 'Grok', 'Gemini', 'DeepSeek',
         'Nobody', '', 'Mike 2', 'Mike B', 'Mike 22', 'Mike two', 'Mike 2 3', 'Astra', 'Astra 7', 'Rivet',
         'Valve', 'Zoë', 'Koko', 'Claire']


def one(case: dict) -> dict:
    """Runs in a process of its own: what the Python Host lists for one case."""
    spec = importlib.util.spec_from_file_location('replay_host', REPO / 'tests' / 'replay' / 'host.py')
    host = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(host)
    root = pathlib.Path(tempfile.mkdtemp(prefix='clarp-persona-vectors-', dir='/var/tmp'))
    (root / 'scenario-config.toml').write_text(case['config'])
    host.isolate(root)
    host.loopback_only()
    sys.path.insert(0, str(REPO / 'server'))
    from lib import config, personas, roster
    # By their ids. The Python Host lists them by when each was written into its table, and
    # those it has built in are written within a millisecond or two of each other, so that
    # their order is another from one start to the next: it is no part of what is held.
    listed = sorted((personas.public(row) for row in personas.list_all()), key=lambda persona: persona['id'])
    lists = {
        'roster': [[name, voice] for name, voice in config.DEFAULT_ROSTER.items()],
        'cartesia': dict(config.DEFAULT_CARTESIA_VOICES),
        'personalities': dict(config.PERSONA_PERSONALITIES),
        'tiers': dict(roster.CONTACT_TIERS),
        'minted': dict(roster.MINTED_CONTACT_TIERS),
    }
    asked = [{'name': name, 'tier': roster.tier_for_contact(name), 'personality': config.persona_personality(name)}
             for name in NAMES]
    shutil.rmtree(root, ignore_errors=True)
    return {'personas': listed, 'lists': lists, 'asked': asked}


def own(room: dict) -> dict:
    """Runs in a process of its own: what an owner does with personas of his own."""
    spec = importlib.util.spec_from_file_location('replay_host', REPO / 'tests' / 'replay' / 'host.py')
    host = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(host)
    root = pathlib.Path(tempfile.mkdtemp(prefix='clarp-persona-vectors-', dir='/var/tmp'))
    (root / 'scenario-config.toml').write_text(room['config'])
    host.isolate(root)
    host.loopback_only()
    sys.path.insert(0, str(REPO / 'server'))
    from lib import contact_minting, personas
    personas.ensure_builtins()
    # The ids the Host draws for an owner's own personas: numbered in the order they are
    # drawn, so that the same steps give the same file.
    import secrets
    drawn = iter(range(1, 1_000_000))
    secrets.token_hex = lambda count=32: f'{next(drawn):0{2 * count}x}'
    done = []
    for step in room['steps']:
        (what, given), = step.items()
        try:
            if what == 'create':
                answered = personas.public(personas.create(**given))
            elif what == 'update':
                answered = personas.public(personas.update(**{'name': '', 'voice_id': '', **given}))
            else:
                answered = personas.delete(given)
            refused = None
        except (ValueError, OSError) as error:
            answered, refused = None, str(error)
        changed = [personas.public(row) for row in personas.list_all()
                   if not row['builtin'] or row.get('avatar_symbol') or row.get('avatar_path')]
        done.append({'step': step, 'answered': answered, 'refused': refused, 'changed': changed,
                     'next_voice': contact_minting.pick_available_voice()})
    shutil.rmtree(root, ignore_errors=True)
    return {'steps': done}


def owner_of(config_text: str) -> dict:
    """What the owner's file names, as a Host reads it: three tables of texts, in the file's order."""
    data = tomllib.loads(config_text) if config_text else {}
    table = lambda value: [[str(key), str(item)] for key, item in value.items()] if isinstance(value, dict) else []
    return {'roster': table(data.get('roster')), 'cartesia': table((data.get('cartesia') or {}).get('voices')),
            'gemini': table((data.get('gemini_tts') or {}).get('voices'))}


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == '--case':
        print(json.dumps(one(CASES[int(sys.argv[2])])))
        return 0
    if len(sys.argv) > 1 and sys.argv[1] == '--own':
        print(json.dumps(own(OWN[int(sys.argv[2])])))
        return 0
    cases, lists, asked = [], None, None
    for index, case in enumerate(CASES):
        ran = subprocess.run([sys.executable, '-I', __file__, '--case', str(index)], capture_output=True, text=True,
                             env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}, check=False)
        if ran.returncode != 0:
            print(ran.stderr[-2000:], file=sys.stderr)
            return 1
        told = json.loads(ran.stdout.splitlines()[-1])
        assert lists in (None, told['lists']), 'the lists are the same for every case'
        lists, asked = told['lists'], told['asked']
        cases.append({**case, 'owner': owner_of(case['config']), 'personas': told['personas']})
    rooms = []
    for index, room in enumerate(OWN):
        ran = subprocess.run([sys.executable, '-I', __file__, '--own', str(index)], capture_output=True, text=True,
                             env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}, check=False)
        if ran.returncode != 0:
            print(ran.stderr[-2000:], file=sys.stderr)
            return 1
        told = json.loads(ran.stdout.splitlines()[-1])
        rooms.append({'name': room['name'], 'owner': owner_of(room['config']), 'steps': told['steps']})
    out = ['{', ' "about": "Written by server-rs/tools/persona-vectors.py from server/lib/personas.py, config.py and '
                'roster.py. Do not edit by hand.",', ' "asked": [']
    out.extend('  ' + json.dumps(row, ensure_ascii=False) + (',' if at < len(asked) - 1 else '') for at, row in enumerate(asked))
    out.append(' ],')
    out.append(' "cases": [')
    for at, case in enumerate(cases):
        personas = case.pop('personas')
        out.append('  {"case": ' + json.dumps(case, ensure_ascii=False) + ', "personas": [')
        out.extend('    ' + json.dumps(row, ensure_ascii=False) + (',' if place < len(personas) - 1 else '')
                   for place, row in enumerate(personas))
        out.append('  ]}' + (',' if at < len(cases) - 1 else ''))
    out.append(' ],')
    out.append(' "own": [')
    for at, room in enumerate(rooms):
        steps = room.pop('steps')
        out.append('  {"room": ' + json.dumps(room, ensure_ascii=False) + ', "steps": [')
        out.extend('    ' + json.dumps(step, ensure_ascii=False) + (',' if place < len(steps) - 1 else '')
                   for place, step in enumerate(steps))
        out.append('  ]}' + (',' if at < len(rooms) - 1 else ''))
    out.append(' ]')
    out.append('}')
    OUT.write_text('\n'.join(out) + '\n')
    LISTS.write_text(json.dumps(lists, indent=1, ensure_ascii=False) + '\n')
    counts = {name: len(rows) for name, rows in lists.items()}
    print(f'wrote {OUT.relative_to(REPO)} ({len(cases)} cases, {[len(json.loads(l)["personas"]) if False else 0 for l in []]}) '
          f'and {LISTS.relative_to(REPO)}: {counts}; {OUT.stat().st_size} + {LISTS.stat().st_size} bytes')
    return 0


if __name__ == '__main__':
    sys.exit(main())
