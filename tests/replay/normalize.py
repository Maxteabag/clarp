"""Normalize a capture so two runs of the same code compare equal.

The contract is explicit and narrow (review s0-harness-7daeae37 finding 1):
only values that differ by nature between runs are rewritten, and only where
they are known to occur.

- Clock readings: a number in the epoch range (seconds or milliseconds) or an
  ISO timestamp string becomes ``<time>``. A setting such as ``timeout_ms:
  30000`` is not in that range and stays as is.
- Durations the Host measures: only the keys in ``DURATION_KEYS``.
- Generated ids: a UUID, or a hex token of 12-64 characters, under an id key
  (``id``, ``*_id``, ``*_ids``, ``ID_KEYS``), in a path, or as the random tail
  of a prefixed id under an id key. Each becomes an ordinal surrogate by first
  appearance (``<id:3>``), so the same id keeps the same surrogate within a run
  and an id that moves shows up. Hex in free text is left alone.
- Event ids become ``<eid:n>`` by first appearance, keeping their aliasing.
- Process ids and the loopback port; the scratch, build and harness paths.

Schema SQL is compared raw. JSON held in a text column is parsed and compared
as JSON (key order and whitespace of the stored string are not compared).
Every rule exists because a replay of unchanged code showed that noise; add a
rule only with that evidence, and never one that hides a planted change.
"""
from __future__ import annotations

import json
import re

UUID = re.compile(r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}')
HEX = re.compile(r'(?<![0-9A-Za-z])[0-9a-f]{12,64}(?![0-9A-Za-z])')
ISO = re.compile(r'^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?$')
LOOPBACK = re.compile(r'127\.0\.0\.1:\d+')
PREFIXED = re.compile(r'^([a-z][a-z0-9]{0,15}[-_])([0-9a-f]{12,64}|[0-9A-Za-z]{20,})$')
DURATION_KEYS = frozenset({'duration_ms', 'worked_ms', 'elapsed_ms', 'source_delta_ms', 'latency_ms',
                           'time_used_seconds', 'timeUsedSeconds', 'age_ms', 'age_sec'})
ID_KEYS = frozenset({'id', 'conv', 'epoch', 'cause', 'source', 'thread', 'threadId', 'turnId',
                     'native_session_id', 'backend_session_id', 'cause_message_id', 'client_msg_id'})
PID_KEYS = frozenset({'pid', 'pgid', 'ppid', 'port'})
PATH_KEYS = frozenset({'cwd', 'path', 'file_path', 'transcript_path', 'root', 'audio_path'})


def is_id_key(key: str) -> bool:
    return key in ID_KEYS or key.endswith(('_id', '_ids', 'Id', 'Ids'))


def is_epoch(value) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and (1.5e9 < value < 2.2e9 or 1.5e12 < value < 2.2e12))


class Normalizer:
    def __init__(self, paths: dict[str, str]):
        # Longest first so a nested path is replaced before its parent.
        self.paths = sorted(((v, k) for k, v in paths.items() if v), key=lambda p: -len(p[0]))
        self.ids: dict[str, str] = {}
        self.eids: dict[str, str] = {}

    def _id(self, token: str) -> str:
        if token not in self.ids:
            self.ids[token] = f'<id:{len(self.ids) + 1}>'
        return self.ids[token]

    def _eid(self, value) -> str:
        key = str(value)
        if key not in self.eids:
            self.eids[key] = f'<eid:{len(self.eids) + 1}>'
        return self.eids[key]

    def paths_in(self, value: str) -> str:
        for path, name in self.paths:
            value = value.replace(path, name)
        return LOOPBACK.sub('127.0.0.1:<port>', value)

    def ids_in(self, value: str) -> str:
        value = UUID.sub(lambda m: self._id(m.group(0).lower()), value)
        return HEX.sub(lambda m: self._id(m.group(0)), value)

    def string(self, value: str, key: str) -> str:
        if ISO.match(value.strip()):
            return '<time>'
        replaced = self.paths_in(value)
        if is_id_key(key):
            prefixed = PREFIXED.match(replaced)
            if prefixed:
                return prefixed.group(1) + self._id(prefixed.group(2))
            return self.ids_in(replaced)
        if key in PATH_KEYS or replaced != value:
            # A scratch path carries generated ids in its file names (rollouts, clips).
            return self.ids_in(replaced)
        return replaced

    def value(self, value, key: str = ''):
        if isinstance(value, dict):
            return {k: self.value(v, k) for k, v in value.items()}
        if isinstance(value, list):
            return [self.value(v, key) for v in value]
        if isinstance(value, bool) or value is None:
            return value
        if key == 'event_id':
            return self._eid(value)
        if key in DURATION_KEYS and isinstance(value, (int, float)):
            return '<duration>'
        if key in PID_KEYS and isinstance(value, int):
            return '<pid>'
        if is_epoch(value):
            return '<time>'
        if isinstance(value, str):
            stripped = value.strip()
            if stripped[:1] in '{[' and len(stripped) > 1:
                try:
                    return {'<json>': self.value(json.loads(stripped), key)}
                except ValueError:
                    pass
            return self.string(value, key)
        return value


def normalize(capture: dict, harness_root: str) -> dict:
    n = Normalizer({'<ROOT>': capture['root'], '<BUILD>': capture['build'], '<HARNESS>': harness_root})
    out = {'scenario': capture['scenario'], 'error': capture['error']}
    # Order matters for the id surrogates: the wire first, then the database.
    out['http'] = [{'step': h['step'], 'method': h['method'], 'path': h['path'], 'status': h['status'],
                    'headers': n.value(h.get('headers', {})), 'body': n.value(h['body'])}
                   for h in capture['http']]
    out['sse'] = [{'event': f.get('event'), 'has_id': f.get('has_id'), 'data': n.value(f.get('data'))}
                  for f in capture['sse']]
    out['sse_health'] = capture.get('sse_health')
    db = capture['db']
    out['schema'] = db['schema']
    out['tables'] = {name: {'pk': db.get('pk', {}).get(name, []), 'rows': [n.value(r) for r in rows]}
                     for name, rows in db['tables'].items()}
    out['files'] = [{'path': n.string(f['path'], 'path'), 'bytes': f['bytes'],
                     'digest': f['digest'] if f.get('kind') != 'json' else None,
                     'json': n.value(f['json']) if f.get('kind') == 'json' else None}
                    for f in capture['files']]
    out['cli'] = [n.value(c) for c in capture.get('cli', [])]
    return out
