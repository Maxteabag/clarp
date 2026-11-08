"""Frozen synthetic transcript recipe for937. Authoring only until source review.

This module imports no Clarp code. Generate ONLY in an admitted buildbox setup
phase. Native seed/reference APIs and physical-copy validation are separate.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from pathlib import Path

COUNTS = {'S': (1000, 1000, 1000), 'L': (1000, 6000, 10000)}
INITIAL_BYTES = {'S': 2686680, 'L': 15286680}
FINAL_BYTES = {'S': 2702880, 'L': 15302904}
PAIR_CONSTANT_BYTES = 884
APPEND_EXCHANGES = 6
EPOCH = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)


def record(agent: str, number: int, role: str) -> dict:
    if agent not in ('synthetic-0', 'synthetic-1', 'synthetic-2'):
        raise ValueError('unfrozen agent identity')
    if type(number) is not int or number < 0 or role not in ('user', 'assistant'):
        raise ValueError('invalid record identity')
    stamp = EPOCH + dt.timedelta(seconds=60 * number + (2 if role == 'assistant' else 0))
    text = (f'request {agent} {number}' if role == 'user'
            else f'answer {agent} {number} ' + 'x' * 512)
    return {'type': role, 'uuid': f'{agent}-{number}-{role}', 'sessionId': 'bs-' + agent,
            'timestamp': stamp.isoformat().replace('+00:00', 'Z'),
            'message': {'role': role, 'content': ([{'type': 'text', 'text': text}]
                                                 if role == 'assistant' else text)}}


def encoded(agent: str, number: int, role: str) -> bytes:
    return (json.dumps(record(agent, number, role), separators=(',', ':'), ensure_ascii=True) + '\n').encode('ascii')


def expected_bytes(count: int) -> int:
    if type(count) is not int or count < 0 or count > 10006:
        raise ValueError('unfrozen count')
    return count * PAIR_CONSTANT_BYTES + 4 * sum(len(str(i)) for i in range(count))


def emit(root: Path, size: str) -> dict:
    """Exclusive fresh corpus only; never overwrite an old fixture or live path."""
    if size not in COUNTS or not root.is_absolute() or root.is_symlink():
        raise ValueError('invalid owned recipe root/size')
    root.mkdir(mode=0o700, exist_ok=False)
    files = []
    for i, count in enumerate(COUNTS[size]):
        agent = 'synthetic-' + str(i)
        path = root / ('bs-' + agent + '.jsonl')
        digest = hashlib.sha256()
        nbytes = 0
        with path.open('xb') as stream:
            for number in range(count):
                for role in ('user', 'assistant'):
                    raw = encoded(agent, number, role)
                    digest.update(raw)
                    nbytes += stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        if nbytes != expected_bytes(count) or path.stat().st_size != nbytes:
            raise RuntimeError('serializer/input bytes differ from frozen recipe')
        files.append({'file': path.name, 'agent': agent, 'exchanges': count,
                      'records': 2 * count, 'bytes': nbytes, 'sha256': digest.hexdigest(),
                      'required_final_exchanges': count + APPEND_EXCHANGES})
    if sum(row['bytes'] for row in files) != INITIAL_BYTES[size]:
        raise RuntimeError('aggregate recipe mismatch')
    return {'size': size, 'files': files, 'append_each': APPEND_EXCHANGES,
            'required_final_bytes': FINAL_BYTES[size],
            'scope': 'synthetic source only; NOT native seed/reference/readiness proof'}
