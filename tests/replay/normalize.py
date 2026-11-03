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
- Inside the SSE channels (the stream and the sse_events table), event ids are named after the frame that carries them: its lane (event
  name, type, payload key set, session; see ``lane_key``) and its position in
  that lane (``<eid:agent-state#2@1a2b3c>``). Producers interleave
  differently between runs, a lane's own order does not; any reference to
  the same event id gets the same name.
- Lists the Host orders by a tied sort key (``SORTED_LISTS``: the persona and
  roster lists, ordered by ``created_at`` with every built-in seeded in the
  same millisecond) are compared sorted. Registered as a pre-existing
  nondeterminism.
- Process ids and the loopback port; the scratch, build and harness paths.

Schema SQL is compared raw. JSON held in a text column is parsed and compared
as JSON (key order and whitespace of the stored string are not compared).
Every rule exists because a replay of unchanged code showed that noise; add a
rule only with that evidence, and never one that hides a planted change.
"""
from __future__ import annotations

import hashlib
import json
import re

UUID = re.compile(r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}')
HEX = re.compile(r'(?<![0-9A-Za-z])[0-9a-f]{12,64}(?![0-9A-Za-z])')
ISO = re.compile(r'^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?$')
# Pairing codes and device tokens (device_pairing): random secrets in a fixed format.
SECRET = re.compile(r'(?<![0-9A-Za-z])(clp|cld)_[A-Za-z0-9_-]{30,}')
LOOPBACK = re.compile(r'127\.0\.0\.1:\d+')
# A short generated tail after a word prefix, e.g. the goal turn's trace id 'goal-1c4f4f95';
# applied only where ids are expected (id keys, paths, ID_COLUMNS), never to free text.
SHORT_PREFIXED = re.compile(r'(?<![0-9A-Za-z])([a-z][a-z0-9]{0,15}-)([0-9a-f]{8,11})(?![0-9A-Za-z])')
PREFIXED = re.compile(r'^([a-z][a-z0-9]{0,15}[-_])([0-9a-f]{12,64}|[0-9A-Za-z]{20,})$')
# Clock readings, by field name only where unchanged replays showed them
# (review s0-harness-57d175ff finding 1): an epoch-range number or an ISO
# timestamp under one of these keys becomes <time>; anywhere else it is content.
CLOCK_KEYS = frozenset({
    'admitted_at', 'at', 'available_at', 'claimed_at', 'created_at', 'ended_at', 'expires_at', 'finished_at',
    'last_run_at', 'done_ts', 'ended_at_ms', 'enqueued_at', 'last_activity',
    'last_lap_ms', 'last_turn_end', 'latest_state_ts', 'latest_ts', 'observed_at', 'opened_at',
    'received_at', 'server_now_ms', 'settled_at', 'since_ms', 'started_at', 'started_at_ms',
    'timestamp', 'ts', 'turn_started_ms', 'updated_at'})
# Text that holds serialized JSON, by storage location only where it was seen.
# Parsed values are compared as JSON plus their serialization form and key order.
JSON_COLUMNS = frozenset({
    ('agents', 'mcp_servers'), ('janitor_attachments', 'config_json'),
    ('janitor_configs', 'execution_json'), ('janitor_configs', 'options_json'),
    ('janitor_configs', 'scope_json'), ('janitor_runs', 'configuration_json'), ('janitor_trigger_definitions', 'defaults_json'),
    ('messages', 'display_cells_json'), ('messages', 'tools_json'), ('messages', 'turn_json'),
    ('personas', 'voice_id'), ('settings', 'value'), ('state_log', 'detail'),
    ('vocab_runs', 'dropped_json'), ('vocab_runs', 'included_json'), ('voice_events', 'detail')})
JSON_HTTP_KEYS = frozenset({'voice_id'})
DURATION_KEYS = frozenset({'duration_ms', 'worked_ms', 'elapsed_ms', 'source_delta_ms', 'latency_ms',
                           'time_used_seconds', 'timeUsedSeconds', 'age_ms', 'age_sec'})
# request_hash, cache_key: hashes over content that includes generated ids;
# group: a live tool group named after its first item id (explore:cx:<id>).
ID_KEYS = frozenset({'focus', 'dedupe_key', 'request_hash', 'cache_key', 'group', 'id', 'conv', 'epoch', 'cause', 'source', 'thread', 'threadId', 'turnId',
                     'native_session_id', 'backend_session_id', 'cause_message_id', 'client_msg_id'})
# Lanes whose frames are pure invalidation signals ("refetch now"). How many the
# Host sends, and whether one names the backend session, varies with timing
# (1-3 per turn in unchanged replays), so each agent's signals are compared by
# presence: one frame per (type, session, agent) keeping only those fields.
COALESCED_TYPES = frozenset({'transcript-updated'})
# Table columns that hold generated ids outside an id-named column.
ID_COLUMNS = frozenset({('settings', 'key'), ('settings', 'value'),
                        # 'x:tool:<command> #<hash over the item id>'
                        ('tool_explanation_decisions', 'signature')})
# Two writers record the same transition for one turn (the dispatcher's spawn row
# and the runner's provider turn-start row, both 'thinking'); their order races.
# Consecutive state rows of one agent with the same kind and trace are compared
# with their details sorted (pre-existing nondeterminism, reported to the lead).
STATE_TABLE, STATE_TYPE = 'state_log', 'agent-state'
SORTED_LISTS = frozenset({'personas', 'roster'})
PID_KEYS = frozenset({'pid', 'pgid', 'ppid', 'port'})
PATH_KEYS = frozenset({'source_file', 'file', 'cwd', 'path', 'file_path', 'transcript_path', 'root', 'audio_path'})


# Event types one producer emits in sequence from one thread share a lane, so
# their relative order is compared: state_watcher._poll_once broadcasts each
# state_log row as agent-state then agent-activity (the dispatcher's own
# agent-state carries client_msg_id and is a different producer).
# The watcher's agent-state always carries status_text and never client_msg_id;
# the Stop path and the dispatcher broadcast agent-state of their own.
PRODUCERS = {'agent-state': 'state-watcher', 'agent-activity': 'state-watcher'}


def is_watcher_state(data: dict) -> bool:
    return data.get('type') == 'agent-state' and 'status_text' in data and 'client_msg_id' not in data


def lane_key(frame: dict) -> str:
    """The producer lane of an SSE frame (or an sse_events row's payload)."""
    data = frame.get('data') if isinstance(frame.get('data'), dict) else {}
    producer = PRODUCERS.get(str(data.get('type')))
    if producer and (data.get('type') == 'agent-activity' or is_watcher_state(data)):
        return f"{producer}|{data.get('session') or ''}"
    return '|'.join([str(frame.get('event') or ''), str(data.get('type') or ''),
                     ','.join(sorted(data)), str(data.get('session') or '')])


def lane_names(frames: list[dict]) -> list[str]:
    """Stable per-frame names: type, position in its lane, lane hash."""
    seen: dict[str, int] = {}
    names = []
    for frame in frames:
        lane = lane_key(frame)
        seen[lane] = seen.get(lane, 0) + 1
        data = frame.get('data') if isinstance(frame.get('data'), dict) else {}
        names.append(f"{data.get('type') or frame.get('event') or '?'}#{seen[lane]}@"
                     f"{hashlib.sha1(lane.encode()).hexdigest()[:6]}")
    return names


def json_form(raw: str, parsed) -> str:
    """How a JSON text was serialized (separators, key sorting, ASCII escaping)."""
    for separators, name in ((None, 'default'), ((',', ':'), 'compact')):
        for sort_keys in (False, True):
            for ascii_ in (True, False):
                if json.dumps(parsed, separators=separators, sort_keys=sort_keys, ensure_ascii=ascii_) == raw:
                    return name + ('-sorted' if sort_keys else '') + ('' if ascii_ else '-utf8')
    return 'other'


def key_order(value) -> list:
    """Every object's key sequence (keys are field names, never values)."""
    if isinstance(value, dict):
        return [list(value), *[key_order(v) for v in value.values()]]
    if isinstance(value, list):
        return [key_order(v) for v in value]
    return []


def parse_json(raw):
    """A stored JSON text as {'<json>': value, '<form>': how, '<keys>': key order}."""
    if not isinstance(raw, str):
        return None
    try:
        parsed = json.loads(raw)
    except ValueError:
        return None
    return {'<json>': parsed, '<form>': json_form(raw, parsed),
            '<keys>': hashlib.sha1(json.dumps(key_order(parsed)).encode()).hexdigest()[:12]}


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
        self.eid_names: dict[str, str] = {}
        self.secrets: dict[str, str] = {}
        self.in_stream = False  # event ids are SSE ids only inside the SSE channels
        self.json_here = False  # JSON_HTTP_KEYS are parsed only in HTTP bodies

    def _id(self, token: str) -> str:
        if token not in self.ids:
            self.ids[token] = f'<id:{len(self.ids) + 1}>'
        return self.ids[token]

    def _eid(self, value) -> str:
        key = str(value)
        if key in self.eid_names:
            return f'<eid:{self.eid_names[key]}>'
        if key not in self.eids:
            self.eids[key] = f'<eid:?{len(self.eids) + 1}>'
        return self.eids[key]

    def paths_in(self, value: str) -> str:
        for path, name in self.paths:
            value = value.replace(path, name)
        return LOOPBACK.sub('127.0.0.1:<port>', value)

    def ids_in(self, value: str) -> str:
        value = UUID.sub(lambda m: self._id(m.group(0).lower()), value)
        value = HEX.sub(lambda m: self._id(m.group(0)), value)
        return SHORT_PREFIXED.sub(lambda m: m.group(1) + self._id(m.group(2)), value)

    def _secret(self, token: str) -> str:
        if token not in self.secrets:
            self.secrets[token] = f'<secret:{len(self.secrets) + 1}>'
        return self.secrets[token]

    def string(self, value: str, key: str) -> str:
        value = SECRET.sub(lambda m: self._secret(m.group(0)), value)
        if key in CLOCK_KEYS and ISO.match(value.strip()):
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
            items = [self.value(v, key) for v in value]
            if key in SORTED_LISTS:
                items.sort(key=lambda v: json.dumps(v, sort_keys=True))
            return items
        if isinstance(value, bool) or value is None:
            return value
        if (self.in_stream and (key == 'event_id' or key.endswith('_event_id'))
                and isinstance(value, (int, str)) and str(value).isdigit()):
            return self._eid(value)
        if key in DURATION_KEYS and isinstance(value, (int, float)):
            return '<duration>'
        if key in PID_KEYS and isinstance(value, int):
            return '<pid>'
        if key in CLOCK_KEYS and is_epoch(value):
            return '<time>'
        if isinstance(value, str):
            if key in JSON_HTTP_KEYS and self.json_here:
                parsed = parse_json(value)
                if parsed is not None:
                    return self.value(parsed, key)
            return self.string(value, key)
        return value


def normalize(capture: dict, harness_root: str) -> dict:
    n = Normalizer({'<ROOT>': capture['root'], '<BUILD>': capture['build'], '<HARNESS>': harness_root})
    db = capture['db']
    db = {**db, 'tables': dict(db['tables'])}
    if STATE_TABLE in db['tables']:
        rows = [dict(r) for r in sorted(db['tables'][STATE_TABLE], key=lambda r: r.get('state_id') or 0)]
        canonical_state_runs(rows, lambda r: r.get('detail'), lambda r, d: r.__setitem__('detail', d),
                             lambda r: _state_key(r.get('agent_id'), r.get('kind'), r.get('detail')))
        db['tables'][STATE_TABLE] = rows
    frames, frame_alias = coalesce(_canonical_frames(capture['sse']))
    log_rows = sorted(db['tables'].get('sse_events', []), key=lambda r: r.get('event_id') or 0)
    log_frames, log_alias = coalesce(_canonical_frames(
        [{'event': None, 'data': _payload(r), 'row': r} for r in log_rows]))
    log_rows = [f['row'] for f in log_frames]
    # Event ids are named by lane position, from the stream first, then the stored log.
    for frame, name in zip(frames, lane_names(frames)):
        data = frame.get('data') if isinstance(frame.get('data'), dict) else {}
        if data.get('event_id') is not None:
            n.eid_names.setdefault(str(data['event_id']), name)
    for row, name in zip(log_rows, lane_names(log_frames)):
        if row.get('event_id') is not None:
            n.eid_names.setdefault(str(row['event_id']), 'log:' + name)
    for dropped, kept in [*frame_alias.items(), *log_alias.items()]:
        if kept in n.eid_names:
            n.eid_names.setdefault(dropped, n.eid_names[kept])
    out = {'scenario': capture['scenario'], 'error': capture['error']}
    # Id surrogates by first appearance, deterministic channels first: the
    # harness-driven HTTP sequence, then tables by name, then files; the
    # interleaved streams last.
    n.json_here = True
    out['http'] = [{'step': h['step'], 'method': h['method'], 'path': h['path'], 'status': h['status'],
                    'headers': n.value(h.get('headers', {})), 'body': n.value(h['body'])}
                   for h in capture['http']]
    n.json_here = False
    out['schema'] = db['schema']
    def row(table, r):
        out = {}
        for k, v in r.items():
            if (table, k) in JSON_COLUMNS and parse_json(v) is not None:
                v = parse_json(v)
            elif (table, k) in ID_COLUMNS and isinstance(v, str):
                v = n.ids_in(v)
            out[k] = v
        return n.value(out)
    out['tables'] = {name: {'pk': db.get('pk', {}).get(name, []), 'rows': [row(name, r) for r in rows]}
                     for name, rows in sorted(db['tables'].items()) if name != 'sse_events'}
    # Size only for opaque bytes: JSON and text embed scratch paths of varying length
    # and are compared by their normalized content instead.
    out['files'] = [{'path': n.string(f['path'], 'path'), 'bytes': f['bytes'] if f.get('kind') == 'bytes' else None,
                     'digest': f['digest'] if f.get('kind') == 'bytes' else None,
                     'json': n.value(f['json']) if f.get('kind') == 'json' else None,
                     # Small text files: compared as text with UUIDs (generated tokens) as surrogates.
                     'text': UUID.sub(lambda m: n._id(m.group(0).lower()), n.paths_in(f['text']))
                     if f.get('kind') == 'text' else None}
                    for f in capture['files']]
    n.in_stream = True
    # The wire id: named through the same event-id relation as payload references,
    # so a wire id that no longer matches its event shows (finding 2).
    out['sse'] = [{'event': f.get('event'),
                   'id': (n._eid(f['id']) if str(f['id']).isdigit() else f['id']) if f.get('id') is not None else None,
                   'data': n.value(f.get('data'))}
                  for f in frames]
    out['sse_log'] = [{'event': None, 'id': None, 'data': n.value(_payload(r)),
                       'row': n.value({k: v for k, v in r.items() if k != 'payload'})} for r in log_rows]
    n.in_stream = False
    out['sse_health'] = capture.get('sse_health')
    out['cli'] = [n.value(c) for c in capture.get('cli', [])]
    return out


def _detail(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def canonical_state_runs(items: list[dict], get, put, key) -> None:
    """Within runs of items sharing ``key(item)``, reassign details in sorted order."""
    run: list[dict] = []

    def flush():
        # Only the exact pair: one spawn row and one provider-start row.
        if len(run) == 2 and {frozenset(_detail(get(i))) for i in run} == {SPAWN_KEYS, START_KEYS}:
            details = sorted((get(i) for i in run), key=lambda d: json.dumps(_detail(d), sort_keys=True))
            for item, detail in zip(run, details):
                put(item, detail)
        run.clear()
    for item in items:
        k = key(item)
        if run and (k is None or k != key(run[-1])):
            flush()
        if k is not None:
            run.append(item)
    flush()


# The demonstrated race, positively identified by its two detail shapes: the
# dispatcher's spawn row and the runner's provider turn-start row of one trace.
SPAWN_KEYS = frozenset({'source', 'dispatch', 'origin', 'trace_id', 'backend_session_id'})
START_KEYS = frozenset({'dispatch', 'origin', 'trace_id'})


def _state_key(agent, kind, detail):
    detail = _detail(detail)
    if kind != 'thinking' or not isinstance(detail, dict) or not detail.get('trace_id'):
        return None
    if set(detail) not in (SPAWN_KEYS, START_KEYS):
        return None
    return (agent, kind, detail['trace_id'])


def coalesce(frames: list[dict]) -> tuple[list[dict], dict[str, str]]:
    """Drop a COALESCED_TYPES frame equal (but for its event id) to an earlier
    one in its lane; returns the kept frames and {dropped event id: kept event id}."""
    kept, alias, seen = [], {}, {}
    for frame in frames:
        data = frame.get('data') if isinstance(frame.get('data'), dict) else {}
        if data.get('type') in COALESCED_TYPES:
            key = (data.get('type'), data.get('session'), data.get('agent_id'))
            if key in seen:
                if data.get('event_id') is not None and seen[key] is not None:
                    alias[str(data['event_id'])] = str(seen[key])
                continue
            seen[key] = data.get('event_id')
            frame = {**frame, 'data': {k: data[k] for k in ('type', 'session', 'agent_id', 'event_id')
                                       if k in data}}
        kept.append(frame)
    return kept, alias


def _canonical_frames(frames: list[dict]) -> list[dict]:
    """The watcher's agent-state frames, canonicalized like state_log rows
    (only agent-state frames take part; others in between do not break a run)."""
    frames = [{**f, 'data': dict(f['data'])} if isinstance(f.get('data'), dict) else f for f in frames]
    states = [f for f in frames if isinstance(f.get('data'), dict) and is_watcher_state(f['data'])]
    canonical_state_runs(states, lambda f: f['data'].get('detail'),
                         lambda f, d: f['data'].__setitem__('detail', d),
                         lambda f: _state_key(f['data'].get('session'), f['data'].get('kind'),
                                              f['data'].get('detail')))
    return frames


def _payload(row: dict):
    raw = row.get('payload')
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except ValueError:
            return {'<unparsed>': raw}
    return raw
