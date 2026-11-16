#!/usr/bin/env python3
"""Writes the vectors the Rust Host's catalogue of voices is tested against.

An app's voice picker asks the Host which engines speak and which voices each has
(`GET /voice-catalog`): the engine every agent speaks with and the one tried after it, whether
each can be used, and for each voice whose it is. The Python Host builds that answer in
`server/lib/voice_catalog.py` from its config, its agents and the lists of voices it ships with.
This calls that function for made-up configs and fleets and writes what it answered to
`crates/clarp-kernel/tests/fixtures/catalog-vectors.json`, with the lists themselves, which the
Rust Host carries as data (`crates/clarp-kernel/src/voice-lists.json`).

    "$(sed -n 1p ~/.local/share/clarp/current/SERVICE_PYTHON)" server-rs/tools/catalog-vectors.py

The catalogue leans on much of the server (the config, the provider table, the adapters), so it
cannot be taken out function by function. Each case therefore runs in a process of its own
whose every home, data, config and cache directory is a new empty directory (the replay
harness's own isolation, `tests/replay/host.py`), with no key in its environment and no way out
of the machine. No Host is touched or started, and no provider is asked: Cartesia's list of
voices, which the Python Host fetches, is a made-up one here.
"""
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[1]
OUT = HERE.parent / 'crates' / 'clarp-kernel' / 'tests' / 'fixtures' / 'catalog-vectors.json'
LISTS = HERE.parent / 'crates' / 'clarp-kernel' / 'src' / 'voice-lists.json'

# Cartesia's own list of voices, as its interface gives one: made up.
CARTESIA_LIVE = [
    {'id': 'c-voice-one', 'name': 'One', 'description': 'A made-up voice', 'tagline': 'Not this'},
    {'id': 'c-voice-two', 'name': 'Two', 'tagline': 'Only a tagline'},
    {'id': 'c-voice-three', 'name': 'Three'},
]

KEYS = ('[gemini_tts]\napi_key = "made-up"\n[gemini_tts.voices]\nKoko = "voice_designed_koko"\n'
        'Mochi = "Puck"\nclaire = "voice_designed_claire"\n[cartesia]\napi_key = "made-up"\n'
        '[cartesia.voices]\nKoko = "c-voice-two"\n')

CASES = [
    {'name': 'nothing is configured', 'config': '', 'agents': {}, 'session': ''},
    {'name': 'speech is off', 'config': '[tts]\nprovider = "none"\n' + KEYS,
     'agents': {'koko': {'name': 'Koko', 'voice_id': ''}}, 'session': 'koko'},
    {'name': 'gemini speaks, cartesia after it',
     'config': '[tts]\nprovider = "gemini"\nfallback = "cartesia"\n' + KEYS,
     'agents': {
         'koko': {'name': 'Koko', 'persona': 'Koko', 'voice_id': ''},
         'claire': {'name': 'Claire', 'persona': 'Claire', 'voice_id': '{"gemini": "Kore", "cartesia": "c-voice-one"}'},
         'mochi-12ab': {'persona': 'Mochi', 'voice_id': '{"elevenlabs": "21m00Tcm4TlvDq8ikWAM"}'},
         'old': {'name': 'Old', 'voice_id': 'nPczCjzI2devNBz1zQrb'},
         'empty': None,
     }, 'session': 'koko'},
    {'name': 'the same fleet, seen for another agent',
     'config': '[tts]\nprovider = "gemini"\nfallback = "cartesia"\n' + KEYS,
     'agents': {
         'koko': {'name': 'Koko', 'persona': 'Koko', 'voice_id': ''},
         'claire': {'name': 'Claire', 'persona': 'Claire', 'voice_id': '{"gemini": "Kore", "cartesia": "c-voice-one"}'},
         'mochi-12ab': {'persona': 'Mochi', 'voice_id': '{"elevenlabs": "21m00Tcm4TlvDq8ikWAM"}'},
     }, 'session': 'claire'},
    {'name': 'for nobody',
     'config': '[tts]\nprovider = "cartesia"\nfallback = "gemini"\n' + KEYS,
     'agents': {'claire': {'name': 'Claire', 'voice_id': '{"deepgram": "aura-2-luna-en", "google": "nb-NO-Chirp3-HD-Kore"}'}},
     'session': ''},
    {'name': 'cartesia speaks and has no key',
     'config': '[tts]\nprovider = "cartesia"\n[gemini_tts]\napi_key = "made-up"\n',
     'agents': {'koko': {'name': 'Koko', 'voice_id': '{"gemini": "Zephyr"}'}}, 'session': 'koko'},
    {'name': 'an engine nobody has is chosen',
     'config': '[tts]\nprovider = "elevenlabs"\nfallback = "deepgram"\n' + KEYS,
     'agents': {}, 'session': 'nobody'},
]


def one(case: dict) -> dict:
    """Runs in a process of its own: the catalogue for one case."""
    spec = importlib.util.spec_from_file_location('replay_host', REPO / 'tests' / 'replay' / 'host.py')
    host = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(host)
    root = pathlib.Path(tempfile.mkdtemp(prefix='clarp-catalog-vectors-', dir='/var/tmp'))
    (root / 'scenario-config.toml').write_text(case['config'])
    host.isolate(root)
    host.loopback_only()
    sys.path.insert(0, str(REPO / 'server'))
    from lib import cartesia_voices, config, google_tts, tts_providers, voice_catalog, voices
    cfg = config.load()
    # Cartesia's list is fetched with its key: with one, the made-up list; without, none.
    cartesia_voices.english_voices = lambda **_: [dict(row) for row in CARTESIA_LIVE] if cfg.cartesia_key() else []
    answer = voice_catalog.catalog(case['agents'], case['session'])
    lists = {
        'providers': [dict(row) for row in tts_providers.CATALOG],
        'gemini': [list(row) for row in voice_catalog.GEMINI_VOICES],
        'flux': [list(row) for row in voice_catalog.FLUX_VOICES],
        'aura2': [list(row) for row in voice_catalog.AURA2_VOICES],
        'elevenlabs': [dict(row) for row in voices.VOICE_CATALOG],
        'google': list(google_tts.VOICES),
    }
    import shutil
    shutil.rmtree(root, ignore_errors=True)
    return {'answer': answer, 'lists': lists}


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == '--case':
        print(json.dumps(one(CASES[int(sys.argv[2])])))
        return 0
    cases, lists = [], None
    for index, case in enumerate(CASES):
        # Nothing of this machine's own environment goes in: no key, no home.
        ran = subprocess.run([sys.executable, '-I', __file__, '--case', str(index)], capture_output=True, text=True,
                             env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}, check=False)
        if ran.returncode != 0:
            print(ran.stderr[-2000:], file=sys.stderr)
            return 1
        told = json.loads(ran.stdout.splitlines()[-1])
        assert lists in (None, told['lists']), 'the lists are the same for every case'
        lists = told['lists']
        cases.append({**case, 'cartesia_live': CARTESIA_LIVE, 'answer': told['answer']})
    vectors = {
        'written_by': f'server-rs/tools/catalog-vectors.py with Python {sys.version_info.major}.{sys.version_info.minor}',
        'cases': cases,
    }
    OUT.write_text(json.dumps(vectors, indent=1, ensure_ascii=False, sort_keys=True) + '\n')
    LISTS.write_text(json.dumps(lists, indent=1, ensure_ascii=False) + '\n')
    counts = {name: len(rows) for name, rows in lists.items()}
    print(f'wrote {OUT.relative_to(REPO)} ({len(cases)} cases) and {LISTS.relative_to(REPO)}: {counts}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
