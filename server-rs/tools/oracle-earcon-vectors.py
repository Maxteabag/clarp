#!/usr/bin/env python3
"""Writes down the sound cues the Python Host makes for a call of the Oracle.

A cue is a short tone the Host makes itself and hands to the phone as sound: a call is put through to an
agent, the user is back with the Oracle, a handoff did not happen, work was handed on, a result is about to be
read. Each is 24 kHz mono PCM16; the chime for an agent is pitched by the agent's name. The rules are
`server/lib/oracle_earcons.py`, run here as they are: for every cue and for many names, how long the sound is,
its first and last samples and the sum of all of it, and which pitch a name has.

    <the Python Host's interpreter> server-rs/tools/oracle-earcon-vectors.py

The same file at every run. Nothing is kept anywhere but in the lists of this program, and nothing is logged.
"""
from __future__ import annotations

import array
import hashlib
import json
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parents[2]
OUT = REPO / 'server-rs' / 'crates' / 'clarp-kernel' / 'tests' / 'fixtures' / 'oracle-earcon-vectors.json'
sys.path.insert(0, str(REPO / 'server'))

from lib import oracle_earcons  # noqa: E402

SHARP_S, A_RING = chr(0xdf), chr(0xc5)
PERSONAS = [None, '', '   ', 'Theo', 'theo', ' THEO ', 'Mike', 'Nadia', 'Bella', 'Olga', 'Sweeper', 'Stra' + SHARP_S + 'e',
            'STRASSE', A_RING + 'se', 'a', 'b', 'c', 'd', 'e', 'f', 'Oracle', 'night sweeper', 7,
            # A name whose note is another where its letters are only made small and not folded.
            'Wei' + SHARP_S]


def sound(name, persona=None) -> dict:
    data = oracle_earcons.pcm(name, persona)
    samples = array.array('h')
    samples.frombytes(data)
    return {'name': name, 'persona': persona, 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest(),
            'first': list(samples[:12]), 'middle': list(samples[len(samples) // 2:len(samples) // 2 + 12]),
            'last': list(samples[-12:]), 'loudest': max(abs(sample) for sample in samples)}


def main() -> int:
    sounds = [sound(name) for name in oracle_earcons.CUES]
    sounds += [sound('connected', persona) for persona in PERSONAS]
    sounds += [sound(name, 'Theo') for name in oracle_earcons.CUES if name != 'connected']
    tints = [{'persona': persona, 'tint': oracle_earcons.tint(persona)} for persona in PERSONAS]
    try:
        oracle_earcons.pcm('no such cue')
        refused = None
    except ValueError as error:
        refused = str(error)
    answered = {'what': 'server/lib/oracle_earcons.py', 'rate': oracle_earcons.RATE, 'cues': list(oracle_earcons.CUES),
                'sounds': sounds, 'tints': tints, 'unknown': {'name': 'no such cue', 'refused': refused}}
    out = ['{']
    parts = list(answered)
    for at, part in enumerate(parts):
        rows = answered[part]
        end = ',' if at < len(parts) - 1 else ''
        if isinstance(rows, list) and rows and isinstance(rows[0], dict):
            out.append(f' {json.dumps(part)}: [')
            out.extend('  ' + json.dumps(one, ensure_ascii=True) + (',' if n < len(rows) - 1 else '') for n, one in enumerate(rows))
            out.append(' ]' + end)
        else:
            out.append(f' {json.dumps(part)}: {json.dumps(rows, ensure_ascii=True)}{end}')
    out.append('}')
    OUT.write_text('\n'.join(out) + '\n', encoding='ascii')
    print(f'{OUT.relative_to(REPO)}: {len(sounds)} sounds, {len(tints)} names; {OUT.stat().st_size} bytes')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
