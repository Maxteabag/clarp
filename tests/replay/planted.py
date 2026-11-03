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
        'file': 'server/server.py',
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
]

BY_ID = {c['id']: c for c in CHANGES}


def apply(change: dict, root: Path) -> None:
    path = root / change['file']
    text = path.read_text()
    if 'swap' in change:
        first, second = change['swap']
        change = {**change, 'find': first + second, 'replace': second + first}
    found = text.count(change['find'])
    expected = change.get('count', 1)
    if found != expected:
        raise RuntimeError(f"planted change {change['id']}: anchor found {found} times in "
                           f"{change['file']}, expected {expected}; update the anchor")
    path.write_text(text.replace(change['find'], change['replace']))


def caught(change: dict, differences: list[dict]) -> list[dict]:
    """The differences that show this specific planted change."""
    want = change['expect']
    pattern = re.compile(want['locator'])
    return [d for d in differences if d['channel'] == want['channel'] and d['kind'] in want['kinds']
            and pattern.search(d['locator'])]
