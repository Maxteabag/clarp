"""Planted changes: deliberate behaviour changes the harness must catch.

Each change is an exact text replacement (or a swap of two adjacent blocks)
in a copy of a build's server tree, never in a checkout. ``find`` must occur
exactly ``count`` times, so a change that no longer applies after code moves
fails loudly instead of silently planting nothing; update its anchor then.

``expect`` names the specific observable the change must produce: a channel,
a locator pattern and the difference kinds that count (review finding 8). A
difference elsewhere does not count as catching it.
"""
from __future__ import annotations

from pathlib import Path
import re

CHANGES = [
    {
        'id': 'response-field',
        'what': 'POST /send stops returning queue_depth',
        # S3 recipe 11 moves Handler._send_direct to the send routes module on the
        # integration track; main keeps it in server.py until that lands.
        'file': ('server/server.py', 'server/lib/routes/send.py'),
        'find': '                "queue_depth": result.queue_depth,\n',
        'replace': '',
        'expect': {'channel': 'http', 'locator': r'\.body\.queue_depth$', 'kinds': {'field-removed'}},
    },
    {
        'id': 'db-write',
        'what': 'a finished turn no longer writes its turn_usage row',
        'file': 'server/lib/turn_usage.py',
        'find': '    if tokens_in is None and tokens_out is None and cost is None:\n        return\n',
        'replace': '    if True:\n        return\n',
        'expect': {'channel': 'table', 'locator': r'^turn_usage', 'kinds': {'row-removed', 'changed'}},
    },
    {
        'id': 'status-transition',
        'what': 'a user stop settles the turn as failed instead of interrupted',
        'file': 'server/lib/message_turns.py',
        'find': '"stop_requested": "interrupted"',
        'replace': '"stop_requested": "failed"',
        'expect': {'channel': 'table', 'locator': r'^turns\[.*\.outcome$', 'kinds': {'changed'}},
    },
    {
        'id': 'sse-order',
        'what': 'a state change broadcasts agent-activity before agent-state',
        'file': 'server/lib/state_watcher.py',
        'swap': (
            '            events.broadcast(self.stream, events.agent_state(\n'
            '                agent_id=r["agent_id"],\n'
            '                session=r["session"],\n'
            '                persona=r["persona"],\n'
            '                kind=r["kind"],\n'
            '                ts=int(r["ts"]),\n'
            '                detail=detail,\n'
            '                status_text=r["custom_status"] or "",\n'
            '            ))\n',
            '            events.broadcast(self.stream, events.as_event(state_activity_event(\n'
            '                agent_id=r["agent_id"],\n'
            '                session=r["session"],\n'
            '                persona=r["persona"],\n'
            '                kind=r["kind"],\n'
            '                ts=int(r["ts"]),\n'
            '                detail=detail,\n'
            '            )))\n',
        ),
        'expect': {'channel': 'sse', 'locator': r'^lane\[(agent-state|agent-activity)@',
                   'kinds': {'sequence-replace', 'sequence-insert', 'sequence-delete'}},
    },
    {
        'id': 'header-only',
        'what': 'responses say Cache-Control no-cache instead of no-store (body unchanged)',
        'file': 'server/server.py',
        'find': '        if "Cache-Control" not in headers:\n            self.send_header("Cache-Control", "no-store")\n',
        'replace': '        if "Cache-Control" not in headers:\n            self.send_header("Cache-Control", "no-cache")\n',
        'expect': {'channel': 'http', 'locator': r'\.headers\.cache-control$', 'kinds': {'changed'}},
    },
    {
        'id': 'sse-envelope',
        'what': 'SSE frames lose their id: line (data unchanged)',
        'file': 'server/server.py',
        'find': '                        self.wfile.write(f"id: {event_id}\\n".encode())\n',
        'count': 2,
        'replace': '                        pass\n',
        'expect': {'channel': 'sse', 'locator': r'\]\.id$', 'kinds': {'changed'}},
    },
    {
        'id': 'sse-wire-id',
        'what': 'SSE frames carry a wire id one past their event id (data unchanged)',
        'file': 'server/server.py',
        'find': '                        self.wfile.write(f"id: {event_id}\\n".encode())\n',
        'count': 2,
        'replace': '                        self.wfile.write(f"id: {int(event_id) + 1}\\n".encode())\n',
        'expect': {'channel': 'sse', 'locator': r'\]\.id$', 'kinds': {'changed'}},
    },
    {
        'id': 'live-activity-unthrottled',
        'what': 'the Live Activity pusher drops its 15 s minimum interval between pushes',
        'file': 'server/lib/live_activity.py',
        'find': '                 min_interval: float = 15.0,\n',
        'replace': '                 min_interval: float = 0.0,\n',
        # The witness is how many requests the recording relay received (more pushes
        # without the throttle): the length of provider/push-relay.jsonl, whose locator
        # is the hash of that path (diff._hash). A change inside one push is not it.
        'expect': {'scenario': 'push-notifications', 'channel': 'files',
                   'locator': r'^files\[1c67a382\]\.json#len$', 'kinds': {'changed'}},
    },
    {
        'id': 'queue-edit-lost',
        'what': 'editing a held queued send answers ok but keeps its old text',
        'file': 'server/lib/turn_queue.py',
        'find': '            (value, queue_id),\n        )\n        if cursor.rowcount:\n',
        'replace': '            (row["text"], queue_id),\n        )\n        if cursor.rowcount:\n',
        # The witness is the held queue read back after the edit (stop-holds-then-edit-queue).
        'expect': {'scenario': 'stop-holds-then-edit-queue', 'channel': 'http',
                   'locator': r'\.body\.items\[0\]\.text$', 'kinds': {'changed'}},
    },
    {
        'id': 'on-chunk-signature',
        'what': "the TTS worker hands adapters a one-argument chunk callback, as VP-01's candidate daaad27c did "
                '(adapters call on_chunk(index, bytes) and swallow the TypeError)',
        'file': 'server/lib/tts_worker.py',
        'find': '            on_chunk=paced_feed,\n',
        'replace': '            on_chunk=lambda chunk: paced_feed(0, chunk),\n',
        # The witness is the live stream itself, the 'clip live stream' exchange:
        # either that exchange alone is replaced (the stream is refused, 404 when the
        # producer fails) or its body changes (the clip's bytes are lost).
        # {i} and {j} are that step's index and the next one among the HTTP exchanges.
        'expect': {'scenario': 'voice-streamed-clip', 'channel': 'http', 'http_step': 'clip live stream',
                   'locator': r'^(\[{i}\]\.body(\.|$)|\[{i}:{j}\]->\[[0-9]+:[0-9]+\]$)',
                   'kinds': {'changed', 'field-added', 'field-removed', 'sequence-replace'}},
    },
]

BY_ID = {c['id']: c for c in CHANGES}


def _http_index(scenario_id: str, step_name: str) -> int:
    """The position of a named HTTP step among a scenario's HTTP exchanges."""
    import scenarios
    steps = [s for s in scenarios.BY_ID[scenario_id]['steps'] if s['op'] in ('get', 'post', 'put', 'delete', 'patch')]
    return next(i for i, s in enumerate(steps) if s.get('name') == step_name)


def files(change: dict) -> tuple[str, ...]:
    """The files a change may live in: one path, or candidates for code that one
    refactor track moves and the other does not (the anchor must match in one)."""
    return (change['file'],) if isinstance(change['file'], str) else tuple(change['file'])


def apply(change: dict, root: Path) -> None:
    if 'swap' in change:
        first, second = change['swap']
        change = {**change, 'find': first + second, 'replace': second + first}
    expected = change.get('count', 1)
    counts = {name: (root / name).read_text().count(change['find'])
              for name in files(change) if (root / name).is_file()}
    hits = [name for name, n in counts.items() if n]
    if len(hits) != 1 or counts[hits[0]] != expected:
        where = ', '.join(f'{n} in {name}' for name, n in counts.items()) or 'no candidate file exists'
        raise RuntimeError(f"planted change {change['id']}: anchor found {where}; expected {expected} "
                           f"in exactly one of {', '.join(files(change))}; update the anchor")
    path = root / hits[0]
    path.write_text(path.read_text().replace(change['find'], change['replace']))


def caught(change: dict, differences: list[dict]) -> list[dict]:
    """The differences that show this specific planted change."""
    want = change['expect']
    locator = want['locator']
    if want.get('http_step'):
        i = _http_index(want['scenario'], want['http_step'])
        locator = locator.replace('{i}', str(i)).replace('{j}', str(i + 1))
    pattern = re.compile(locator)
    return [d for d in differences
            if (want.get('scenario') is None or d['scenario'] == want['scenario'])
            and (want['channel'] is None or d['channel'] == want['channel'])
            and (want['kinds'] is None or d['kind'] in want['kinds'])
            and pattern.search(d['locator'])]
