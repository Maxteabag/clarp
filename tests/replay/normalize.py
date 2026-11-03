"""Normalize a capture so two runs of the same code compare equal.

Only values that differ by nature between runs are rewritten: clock readings,
generated ids (UUIDs, hex tokens), process ids, ports and the scratch paths.
Generated ids become ordinal surrogates by first appearance (``<id:3>``), so
two events naming the same id still name the same surrogate, and an id that
moves to another place shows up as a difference.
"""
from __future__ import annotations

import json
import re

UUID = re.compile(r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}')
HEX = re.compile(r'(?<![0-9A-Za-z])[0-9a-f]{12,64}(?![0-9A-Za-z])')
ISO = re.compile(r'\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?')
CLOCK = re.compile(r'\b\d{2}:\d{2}:\d{2}\b')
EPOCH_MS = re.compile(r'(?<!\d)1[6-9]\d{11}(?!\d)')
LOOPBACK = re.compile(r'127\.0\.0\.1:\d+')
# Keys whose values are clock readings or durations, whatever their type.
TIME_KEY = re.compile(
    r'(^|_)(ts|at|ms|time|now|since|started|ended|updated|created|deadline|expires|elapsed|'
    r'duration|worked|age|seconds|latency|last_seen|heartbeat|mtime|clock)(_|$)|(_at|_ms|_ts|_sec|_s)$',
    re.I)
PID_KEY = re.compile(r'(^|_)(pid|pgid|port)$', re.I)
# Ids the server mints at random (agent ids, trace ids, notification ids) carry a
# readable prefix and a random tail: keep the prefix, number the tail.
PREFIXED = re.compile(r'(?<![0-9A-Za-z])([a-z][a-z0-9]{0,15}[-_])([0-9a-f]{12,64}|[0-9A-Za-z]{20,})(?![0-9A-Za-z])')


class Normalizer:
    def __init__(self, paths: dict[str, str]):
        # Longest first so a nested path is replaced before its parent.
        self.paths = sorted(((v, k) for k, v in paths.items() if v), key=lambda p: -len(p[0]))
        self.ids: dict[str, str] = {}

    def _id(self, token: str) -> str:
        if token not in self.ids:
            self.ids[token] = f'<id:{len(self.ids) + 1}>'
        return self.ids[token]

    def text(self, value: str) -> str:
        for path, name in self.paths:
            value = value.replace(path, name)
        value = UUID.sub(lambda m: self._id(m.group(0).lower()), value)
        value = PREFIXED.sub(lambda m: m.group(1) + self._id(m.group(2)), value)
        value = HEX.sub(lambda m: self._id(m.group(0)), value)
        value = ISO.sub('<iso>', value)
        value = EPOCH_MS.sub('<epoch>', value)
        value = LOOPBACK.sub('127.0.0.1:<port>', value)
        value = CLOCK.sub('<clock>', value)
        return value

    def value(self, value, key: str = ''):
        if isinstance(value, dict):
            return {k: self.value(v, k) for k, v in value.items()}
        if isinstance(value, list):
            return [self.value(v, key) for v in value]
        if isinstance(value, bool) or value is None:
            return value
        if key and TIME_KEY.search(key) and isinstance(value, (int, float, str)):
            return '<time>' if value not in ('', 0) else value
        if key and PID_KEY.search(key) and isinstance(value, int):
            return '<pid>'
        if key == 'event_id':
            return '<event_id>'
        if isinstance(value, (int, float)):
            if 1.5e9 < value < 2.2e9 or 1.5e12 < value < 2.2e12:
                return '<epoch>'
            return value
        if isinstance(value, str):
            stripped = value.strip()
            if stripped[:1] in '{[' and len(stripped) > 1:
                try:
                    return {'<json>': self.value(json.loads(stripped), key)}
                except ValueError:
                    pass
            return self.text(value)
        return value


def normalize(capture: dict, harness_root: str) -> dict:
    n = Normalizer({'<ROOT>': capture['root'], '<BUILD>': capture['build'], '<HARNESS>': harness_root})
    out = {'scenario': capture['scenario'], 'error': capture['error']}
    # Order matters for the id surrogates: the wire first, then the database.
    out['http'] = [{'step': h['step'], 'method': h['method'], 'path': h['path'],
                    'status': h['status'], 'body': n.value(h['body'])} for h in capture['http']]
    out['sse'] = [n.value(e) for e in capture['sse']]
    db = capture['db']
    out['schema'] = [{k: (n.text(v) if isinstance(v, str) else v) for k, v in row.items()} for row in db['schema']]
    tables = {}
    for name, rows in db['tables'].items():
        normalized = [n.value(r) for r in rows]
        # Row order within a table is insertion order, which concurrent writers
        # may interleave differently; compare tables as multisets.
        tables[name] = sorted(normalized, key=lambda r: json.dumps(r, sort_keys=True))
    out['tables'] = tables
    out['files'] = [{'path': n.text(f['path']), 'bytes': f['bytes']} for f in capture['files']]
    out['cli'] = [n.value(c) for c in capture.get('cli', [])]
    return out
