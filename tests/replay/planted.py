"""Planted changes: deliberate behaviour changes the harness must catch.

Each change is an exact text replacement (or a swap of two adjacent blocks) in a copy of a build's server tree
(never in a checkout). ``find`` must occur exactly ``count`` times, so a
change that no longer applies after code moves fails loudly instead of
silently planting nothing; update its anchor then.
"""
from __future__ import annotations

from pathlib import Path

CHANGES = [
    {
        'id': 'response-field',
        'what': 'POST /send stops returning queue_depth',
        'file': 'server/server.py',
        'find': '                "queue_depth": result.queue_depth,\n',
        'replace': '',
        'expect_channels': {'http'},
    },
    {
        'id': 'db-write',
        'what': 'a finished turn no longer writes its turn_usage row',
        'file': 'server/lib/turn_usage.py',
        'find': '    if tokens_in is None and tokens_out is None and cost is None:\n        return\n',
        'replace': '    if True:\n        return\n',
        'expect_channels': {'table'},
    },
    {
        'id': 'status-transition',
        'what': 'a user stop settles the turn as failed instead of interrupted',
        'file': 'server/lib/message_turns.py',
        'find': '"stop_requested": "interrupted"',
        'replace': '"stop_requested": "failed"',
        'expect_channels': {'table', 'http', 'sse'},
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
        'expect_channels': {'sse'},
    },
]


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
