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
EPOCH_TEXT = re.compile(r'(?<![\d.])1[5-9]\d{8}(\.\d+)?(?![\d.])|(?<!\d)1[5-9]\d{11}(?!\d)')
# A clip's file name starts with its creation time in ms: '<epoch ms>__<session>.mp3'
# (and its '.mp3.json' sidecar). Only the clips this run created (files captured
# under cache/audio/) are rewritten, and only as a whole value that is the name or
# a path or URL ending in it; any other text, even one shaped like a clip, is content.
CLIP_NAME = re.compile(r'^(.*/)?(1[5-9][0-9]{11})(__[A-Za-z0-9._-]+\.mp3)(\.json)?$')
CLIP_FILE = re.compile(r'^cache/audio/(1[5-9][0-9]{11}__[A-Za-z0-9._-]+\.mp3)(?:\.json)?$')


def generated_clips(files: list[dict]) -> set[str]:
    """The clip names ('<epoch ms>__<session>.mp3') this run wrote under cache/audio/."""
    return {m.group(1) for f in files for m in [CLIP_FILE.match(str(f.get('path') or ''))] if m}
LOOPBACK = re.compile(r'127\.0\.0\.1(:|%3A)\d+')
# A short generated tail after a word prefix, e.g. the goal turn's trace id 'goal-1c4f4f95';
# applied only where ids are expected (id keys, paths, ID_COLUMNS), never to free text.
SHORT_PREFIXED = re.compile(r'(?<![0-9A-Za-z])([a-z][a-z0-9]{0,15}-)([0-9a-f]{8,11})(?![0-9A-Za-z])')
PREFIXED = re.compile(r'^([a-z][a-z0-9]{0,15}[-_])([0-9a-f]{12,64}|[0-9A-Za-z]{20,})$')
# Clock readings, by field name only where unchanged replays showed them
# (review s0-harness-57d175ff finding 1): an epoch-range number or an ISO
# timestamp under one of these keys becomes <time>; anywhere else it is content.
CLOCK_KEYS = frozenset({
    'admitted_at', 'at', 'broadcast_at', 'completed_at', 'exited_at', 'available_at', 'claimed_at', 'created_at', 'ended_at', 'expires_at', 'finished_at',
    'last_run_at', 'last_seen_at', 'used_at', 'done_ts', 'ended_at_ms', 'enqueued_at', 'last_activity',
    'last_lap_ms', 'last_turn_end', 'latest_state_ts', 'latest_ts', 'observed_at', 'opened_at',
    'received_at', 'server_now_ms', 'settled_at', 'since_ms', 'started_at', 'started_at_ms',
    'timestamp', 'ts', 'turn_started_ms', 'updated_at',
    # Push notifications (push-notifications): device rows, Live Activity rows and payloads.
    'last_push_at', 'registered_at', 'updated_at_ms', 'stale-date'})
# (deadline key, base key) in one object: the deadline is kept as its offset from the base.
RELATIVE_CLOCKS = (('stale-date', 'timestamp'),)
# Text that holds serialized JSON, by storage location only where it was seen.
# Parsed values are compared as JSON plus their serialization form and key order.
JSON_COLUMNS = frozenset({
    ('agents', 'mcp_servers'), ('janitor_attachments', 'config_json'),
    ('janitor_configs', 'execution_json'), ('janitor_configs', 'options_json'),
    ('janitor_configs', 'scope_json'), ('janitor_demand_results', 'result_json'), ('janitor_runs', 'configuration_json'), ('janitor_trigger_definitions', 'defaults_json'),
    ('messages', 'display_cells_json'), ('messages', 'tools_json'), ('messages', 'turn_json'),
    ('personas', 'voice_id'), ('settings', 'value'), ('state_log', 'detail'),
    ('vocab_runs', 'dropped_json'), ('vocab_runs', 'included_json'), ('voice_events', 'detail')})
JSON_HTTP_KEYS = frozenset({'voice_id'})
DURATION_KEYS = frozenset({'duration_ms', 'worked_ms', 'elapsed_ms', 'source_delta_ms', 'latency_ms',
                           'time_used_seconds', 'timeUsedSeconds', 'age_ms', 'age_sec'})
# request_hash, cache_key: hashes over content that includes generated ids;
# group: a live tool group named after its first item id (explore:cx:<id>).
ID_KEYS = frozenset({'turn_token', 'focus', 'dedupe_key', 'request_hash', 'cache_key', 'group', 'code_hash', 'token_hash', 'id', 'conv', 'epoch', 'cause', 'source', 'thread', 'threadId', 'turnId',
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
# A process identity, '<boot id>:<process start time>' (provider_turns.start_token):
# one surrogate for the whole value, so the same process keeps the same name.
PROCESS_KEYS = frozenset({'start_token'})
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


def is_explainer_patch(data: dict) -> bool:
    """A live frame from the tool explainer (live_explain.py), not from the turn:
    every op patches only a tool item's explanation. The explainer runs on its own
    thread, so where its patches fall among the turn's frames is timing (G2, 5 Nov:
    'failed' landed before the reply's done in one run, after the turn in the next)."""
    ops = data.get('ops')
    return (data.get('type') == 'live' and isinstance(ops, list) and bool(ops)
            and all(isinstance(op, dict) and op.get('op') == 'upsert' and op.get('kind') == 'tool'
                    and isinstance(op.get('item'), dict) and set(op['item']) == {'tool'}
                    and isinstance(op['item']['tool'], dict) and set(op['item']['tool']) == {'explain'}
                    for op in ops))


def lane_key(frame: dict, turn_segment: int = 0) -> str:
    """The producer lane of an SSE frame (or an sse_events row's payload).
    ``turn_segment`` anchors an explainer patch to the turn it arrived in: past
    the turn's own done/completed frames is timing, into the next turn is not (a
    client's live view resets there, and a late patch brings the old tool back)."""
    data = frame.get('data') if isinstance(frame.get('data'), dict) else {}
    if is_explainer_patch(data):
        return f"live-explainer|{data.get('session') or ''}|turn{turn_segment}"
    producer = PRODUCERS.get(str(data.get('type')))
    if producer and (data.get('type') == 'agent-activity' or is_watcher_state(data)):
        return f"{producer}|{data.get('session') or ''}"
    return '|'.join([str(frame.get('event') or ''), str(data.get('type') or ''),
                     ','.join(sorted(data)), str(data.get('session') or '')])


def _turn_ids(data: dict) -> list[str]:
    """The turn identities a live frame's turn ops name, in order."""
    if data.get('type') != 'live':
        return []
    return [str((op.get('turn') or {}).get('turn_id') or '') for op in data.get('ops') or ()
            if isinstance(op, dict) and op.get('op') == 'turn' and isinstance(op.get('turn'), dict)]


def lane_names(frames: list[dict]) -> list[str]:
    """Stable per-frame names: type, position in its lane, lane hash."""
    seen: dict[str, int] = {}
    turn: dict[str, tuple[str, int]] = {}  # session -> (current turn id, segment)
    names = []
    for frame in frames:
        data = frame.get('data') if isinstance(frame.get('data'), dict) else {}
        session = str(data.get('session') or '')
        for turn_id in _turn_ids(data):
            current, segment = turn.get(session, ('', 0))
            if turn_id != current:
                turn[session] = (turn_id, segment + 1)
        lane = lane_key(frame, turn.get(session, ('', 0))[1])
        seen[lane] = seen.get(lane, 0) + 1
        names.append(f"{data.get('type') or frame.get('event') or '?'}#{seen[lane]}@"
                     f"{hashlib.sha1(lane.encode()).hexdigest()[:6]}")
    return names


def json_form(raw: str, parsed) -> str:
    """How a JSON text was serialized (indent, separators, key sorting, ASCII escaping)."""
    for indent in (None, 2, 4):
        for separators, name in ((None, 'default'), ((',', ':'), 'compact')):
            if indent is not None and separators is not None:
                continue
            for sort_keys in (False, True):
                for ascii_ in (True, False):
                    if json.dumps(parsed, indent=indent, separators=separators, sort_keys=sort_keys,
                                  ensure_ascii=ascii_) == raw:
                        return (name + (f'-indent{indent}' if indent else '') + ('-sorted' if sort_keys else '')
                                + ('' if ascii_ else '-utf8'))
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
        self.clips: set[str] = set()  # generated_clips() of this capture
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
        clip = CLIP_NAME.match(value)
        if clip and clip.group(2) + clip.group(3) in self.clips:
            value = (clip.group(1) or '') + '<clip-time>' + clip.group(3) + (clip.group(4) or '')
        return LOOPBACK.sub(lambda m: f'127.0.0.1{m.group(1)}<port>', value)

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
            out = {k: self.value(v, k) for k, v in value.items()}
            for later, base in RELATIVE_CLOCKS:
                # A deadline set relative to another clock in the same object (a Live
                # Activity's stale-date from its timestamp): the interval is behaviour.
                if is_epoch(value.get(later)) and is_epoch(value.get(base)):
                    out[later] = f'<{base}>{value[later] - value[base]:+}'
            return out
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
        if key in PROCESS_KEYS and isinstance(value, str):
            return self._id('process:' + value)
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
    n.clips = generated_clips(capture['files'])
    db = capture['db']
    db = {**db, 'tables': dict(db['tables'])}
    if STATE_TABLE in db['tables']:
        rows = [dict(r) for r in sorted(db['tables'][STATE_TABLE], key=lambda r: r.get('state_id') or 0)]
        canonical_state_runs(rows, lambda r: r.get('detail'), lambda r, d: r.__setitem__('detail', d),
                             lambda r: _state_key(r.get('agent_id'), r.get('kind'), r.get('detail')))
        db['tables'][STATE_TABLE] = rows
    live_frames, live_summary = canonical_live(capture['sse'])
    frames, frame_alias = coalesce(_canonical_frames(live_frames))
    log_rows = sorted(db['tables'].get('sse_events', []), key=lambda r: r.get('event_id') or 0)
    log_live, log_live_summary = canonical_live(
        [{'event': None, 'data': _payload(r), 'row': r} for r in log_rows])
    log_frames, log_alias = coalesce(_canonical_frames(log_live))
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
            # Only an object or array is stored JSON: a scalar stays text, so a hex id
            # that happens to read as a number ('5533263533985e85') is still an id.
            if (table, k) in JSON_COLUMNS and isinstance(v, str) and v.lstrip()[:1] in '{[' \
                    and parse_json(v) is not None:
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
                     # Small text files: compared as text with generated ids (UUIDs, hex
                     # tokens) as surrogates and epoch readings (the claude source
                     # markers: 'pwa-voice <session> <epoch> <trace> 0') as <time>.
                     'text': text_file(n, f['path'], f['text']) if f.get('kind') == 'text' else None}
                    for f in capture['files']]
    n.in_stream = True
    # The wire id: named through the same event-id relation as payload references,
    # so a wire id that no longer matches its event shows (finding 2).
    out['sse'] = [{'event': f.get('event'),
                   'id': (n._eid(f['id']) if str(f['id']).isdigit() else f['id']) if f.get('id') is not None else None,
                   'data': n.value(f.get('data'))}
                  for f in frames]
    # The canonicalized frames (live merges, the thinking pair), not the raw payloads.
    out['sse_log'] = [{'event': None, 'id': None, 'data': n.value(f['data']),
                       'row': n.value({k: v for k, v in f['row'].items() if k != 'payload'})} for f in log_frames]
    n.in_stream = False
    out['sse_health'] = capture.get('sse_health')
    out['live'] = {'stream': n.value(live_summary), 'log': n.value(log_live_summary)}
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


TEXT_APPEND_KEYS = frozenset({'op', 'conv', 'id', 'kind', 'rev', 'field', 'chunk'})


def _text_append(op) -> bool:
    """The observed streamed-text shape: an append of a text chunk and nothing else."""
    return (isinstance(op, dict) and op.get('op') == 'append' and op.get('field') == 'text'
            and isinstance(op.get('chunk'), str) and set(op) <= TEXT_APPEND_KEYS)


def live_contract(live: list[dict]) -> dict:
    """The live sequence rules, checked on the raw frames (docs/live-items.md):
    per stream (session, conversation, epoch) lseq is an integer rising by
    exactly 1 per frame; per item, every op's rev is an integer one above the
    item's previous rev. Streams are named by order of appearance."""
    streams: dict = {}
    lseq_state: dict = {}
    last_lseq: dict = {}
    revs: dict = {}
    rev_faults = 0
    for f in live:
        data = f['data']
        scope = (data.get('session'), data.get('conv'), data.get('epoch'))
        name = streams.setdefault(scope, f'stream{len(streams) + 1}')
        lseq = data.get('lseq')
        if not isinstance(lseq, int) or isinstance(lseq, bool):
            lseq_state[name] = 'non-integer'
        else:
            if name in last_lseq and lseq != last_lseq[name] + 1:
                lseq_state.setdefault(name, 'gap')
                if lseq_state[name] == 'ok':
                    lseq_state[name] = 'gap'
            lseq_state.setdefault(name, 'ok')
            last_lseq[name] = lseq
        for op in data.get('ops') or []:
            if not isinstance(op, dict) or 'rev' not in op:
                continue
            key = (scope, op.get('id'))
            rev = op['rev']
            if not isinstance(rev, int) or isinstance(rev, bool) or (key in revs and rev != revs[key] + 1):
                rev_faults += 1
            if isinstance(rev, int):
                revs[key] = rev
    return {'lseq': dict(sorted(lseq_state.items())), 'rev_faults': rev_faults} if streams else {}


def _same_but_chunk(a: dict, b: dict) -> bool:
    """Two text appends that differ only by timing: every field but chunk and rev
    identical, keys included."""
    return ({k: v for k, v in a.items() if k not in ('chunk', 'rev')}
            == {k: v for k, v in b.items() if k not in ('chunk', 'rev')})


def _envelope_ok(frame: dict, into: dict | None) -> bool:
    """A frame may be dropped after merging only with an envelope that carries
    nothing of its own: no SSE event name; a wire id present exactly when the
    frame it merges into has one, and then naming the frame's own event; and the
    same payload fields as that frame, where a clock reading may differ only
    between two valid clock readings (an epoch-range number under CLOCK_KEYS)."""
    data = frame['data']
    wire = frame.get('id')
    if frame.get('event') is not None or into is None or into.get('event') is not None:
        return False
    if (wire is None) != (into.get('id') is None):
        return False
    if wire is not None and str(wire) != str(data.get('event_id')):
        return False

    def fields(d):
        return {k: ('<clock>' if k in CLOCK_KEYS and is_epoch(v) else v)
                for k, v in d.items() if k not in ('ops', 'lseq', 'event_id')}
    return fields(data) == fields(into['data'])


def canonical_live(frames: list[dict]) -> tuple[list[dict], dict]:
    """How many text 'append' ops the live hub sends for one streamed reply
    depends on timing (one 27-character append in one run, 8 + 19 in the next).
    After the raw frames pass live_contract (whose result is compared), a text
    append is merged into the text append directly before it when every other
    field is identical (_same_but_chunk); a frame emptied by that is dropped only
    if its envelope carries nothing of its own (_envelope_ok), otherwise it is
    kept unmerged. Each item's rev is renumbered and an integer lseq becomes
    <lseq>. Any other op, including tool-output appends, is kept as is."""
    live = [f for f in frames if isinstance(f.get('data'), dict) and f['data'].get('type') == 'live']
    summary = live_contract(live)
    out, previous, previous_frame, revs = [], None, None, {}
    for f in frames:
        data = f.get('data') if isinstance(f.get('data'), dict) else None
        if not data or data.get('type') != 'live' or not isinstance(data.get('ops'), list):
            out.append(f)
            continue
        raw_ops = [dict(op) if isinstance(op, dict) else op for op in data['ops']]
        merges = []  # (target op, chunk) decided for this frame
        keep = []
        last, last_frame = previous, previous_frame
        for op in raw_ops:
            if _text_append(op) and _text_append(last) and _same_but_chunk(last, op):
                merges.append((last, op['chunk']))
                continue
            keep.append(op)
            last = op if isinstance(op, dict) else None
        if raw_ops and not keep and not _envelope_ok(f, last_frame):
            merges, keep = [], raw_ops  # an envelope of its own: keep the frame as it was
        for target, chunk in merges:
            target['chunk'] = target['chunk'] + chunk
        if keep or not raw_ops:
            for op in keep:
                if isinstance(op, dict) and isinstance(op.get('rev'), int):
                    key = (data.get('session'), data.get('conv'), data.get('epoch'), op.get('id'))
                    revs[key] = revs.get(key, 0) + 1
                    op['rev'] = revs[key]
            lseq = '<lseq>' if isinstance(data.get('lseq'), int) else data.get('lseq')
            frame = {**f, 'data': {**data, 'ops': keep, 'lseq': lseq}}
            out.append(frame)
            previous_frame = f
            previous = next((op for op in reversed(keep) if isinstance(op, dict)), None)
    return out, summary


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


# Text files whose content is generated, and how (each seen in unchanged replays):
# the instance generation token (a UUID) and the claude source markers
# ('pwa-voice <session> <epoch> <trace> 0'). Every other text file is content:
# only the scratch, build and harness paths and pairing secrets are rewritten.
GENERATED_TEXT = (('share/clarp/backups/instance.generation', 'uuid'), ('cache/source-markers/', 'marker'))


def text_file(n: 'Normalizer', path: str, text: str) -> str:
    text = SECRET.sub(lambda m: n._secret(m.group(0)), n.paths_in(text))
    for prefix, kind in GENERATED_TEXT:
        if path == prefix or (prefix.endswith('/') and path.startswith(prefix)):
            if kind == 'uuid':
                return UUID.sub(lambda m: n._id(m.group(0).lower()), text)
            return EPOCH_TEXT.sub('<time>', n.ids_in(text))
    return text


def _payload(row: dict):
    raw = row.get('payload')
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except ValueError:
            return {'<unparsed>': raw}
    return raw
