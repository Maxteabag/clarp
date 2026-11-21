"""Planted changes: deliberate changes to a copy of the Python server that this suite must see.

A conformance suite that cannot fail proves nothing. Each change here is an
exact text replacement in a copy of the reference server (never in a
checkout); the suite passes its proof only if every one of them shows up as a
difference of the named kind in what a client sees. A reviewer who did not
write the suite adds changes of their own below the line.

`find` must occur exactly `count` times in exactly one of the files, so a
change whose anchor has moved fails loudly instead of planting nothing.
"""
from __future__ import annotations

SEQUENCE = {'sequence-replace', 'sequence-insert', 'sequence-delete'}

CHANGES = [
    {
        'id': 'response-field',
        'what': 'POST /send stops returning queue_depth',
        'file': ('server/server.py', 'server/lib/routes/send.py'),
        'find': '                "queue_depth": result.queue_depth,\n',
        'replace': '',
        'expect': {'channel': 'http', 'locator': r'\.body\.queue_depth$', 'kinds': {'field-removed'}},
    },
    {
        'id': 'response-value',
        'what': 'POST /stop counts one more ended turn than there was',
        'file': 'server/server.py',
        'find': 'return self._json_ok({"ok": True, "terminated": n, "stop_actor": actor,',
        'replace': 'return self._json_ok({"ok": True, "terminated": n + 1, "stop_actor": actor,',
        'expect': {'channel': 'http', 'locator': r'\.body\.terminated$', 'kinds': {'changed'}},
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
        'id': 'event-missing',
        'what': 'a change of the queue is no longer announced',
        'file': 'server/server.py',
        'find': (
            '        state = turn_queue.state(agent_id)\n'
            '        event = events.queue_updated(\n'
            '            session=session, agent_id=agent_id,\n'
            '            queue_depth=state["count"], queue_paused=state["paused"],\n'
            '            queue_started=False, queue_revision=state["revision"],\n'
            '            **({"client_msg_id": client_msg_id} if client_msg_id else {}),\n'
            '            **({"queue_change": change} if change else {}),\n'
            '            **({"text": text} if text is not None else {}),\n'
            '            **(provenance or {}))\n'
            '        events.broadcast(self.ctx.stream, event)\n'
        ),
        'replace': '        return\n',
        'expect': {'channel': 'sse', 'locator': r'queue-updated', 'kinds': SEQUENCE},
    },
    {
        'id': 'event-envelope',
        'what': 'events lose their id: line (data unchanged)',
        'file': 'server/server.py',
        'find': '                        self.wfile.write(f"id: {event_id}\\n".encode())\n',
        'count': 2,
        'replace': '                        pass\n',
        'expect': {'channel': 'sse', 'locator': r'\]\.id$', 'kinds': {'changed'}},
    },
    {
        'id': 'event-wire-id',
        'what': 'events carry a wire id one past their own (data unchanged)',
        'file': 'server/server.py',
        'find': '                        self.wfile.write(f"id: {event_id}\\n".encode())\n',
        'count': 2,
        'replace': '                        self.wfile.write(f"id: {int(event_id) + 1}\\n".encode())\n',
        'expect': {'channel': 'sse', 'locator': r'\]\.id$', 'kinds': {'changed'}},
    },
    {
        'id': 'event-order',
        'what': 'a state change is announced as agent-activity before agent-state',
        'file': 'server/lib/state_watcher.py',
        'swap': (
            '            events.broadcast(self.stream, events.agent_state(\n'
            '                agent_id=r["agent_id"],\n'
            '                session=r["session"],\n'
            '                persona=r["persona"],\n'
            '                kind=r["kind"],\n'
            '                ts=int(r["ts"]),\n'
            '                detail=coordination.veil_detail(detail, veil),\n'
            '                status_text=r["custom_status"] or "",\n'
            '                **working,\n'
            '            ))\n',
            "            # Clients draw a row's activity from this event: a coordination turn its chat\n"
            '            # hides shows who the agent works with, never what it does (as the snapshot does).\n'
            '            events.broadcast(self.stream, events.as_event(coordination.veil_activity(state_activity_event(\n'
            '                agent_id=r["agent_id"],\n'
            '                session=r["session"],\n'
            '                persona=r["persona"],\n'
            '                kind=r["kind"],\n'
            '                ts=int(r["ts"]),\n'
            '                detail=detail,\n'
            '            ), veil)))\n',
        ),
        'expect': {'channel': 'sse', 'locator': r'agent-state|agent-activity', 'kinds': SEQUENCE},
    },
    {
        'id': 'state-transition',
        'what': "a user's Stop leaves the agent `stopped` instead of `interrupted`",
        'file': 'server/lib/turn_lifecycle.py',
        'find': '    TurnEvent.STOP_REQUESTED: (AgentState.INTERRUPTED, STARTED),\n',
        'replace': '    TurnEvent.STOP_REQUESTED: (AgentState.STOPPED, STARTED),\n',
        'expect': {'channel': None, 'locator': r'latest_state|\.kind$|agent-state', 'kinds': None},
    },
    {
        'id': 'render-spec-row',
        'what': "a render spec's row carries its spec, in every list and in what a client that cannot see is read",
        'file': 'server/lib/render_spec_store.py',
        'find': '    return PublicRow(item, meta)\n',
        'replace': '    item["spec"] = stored["spec"]\n    return PublicRow(item, meta)\n',
        'expect': {'channel': 'http', 'locator': r'\.spec$', 'kinds': {'field-added'}},
    },
    {
        'id': 'router-holds-a-command',
        'what': 'a command the router refuses is held, and a yes that follows gives its words to the agent',
        'file': 'server/lib/orchestrator.py',
        'find': ('                    final_action = FINAL_CLARIFY\n'
                 '                    if aloud:\n'
                 '                        self._speak_as_session(refusal, requested_session)\n'),
        'replace': ('                    if aloud:\n'
                    '                        self._speak_as_session(refusal, requested_session)\n'),
        'expect': {'channel': 'http', 'locator': r'\.body\.turns\[\d+\]\.text$', 'kinds': {'changed'}},
    },
    # ---- changes planted by the reviewer go below this line ----
]

BY_ID = {change['id']: change for change in CHANGES}
