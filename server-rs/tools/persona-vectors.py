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
    listed = [personas.public(row) for row in personas.list_all()]
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
