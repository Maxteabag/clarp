#!/usr/bin/env python3
"""SubagentStop hook → emit a 'subagent done' eventlog row.

Fires when a Task subagent finishes. We don't change AgentState (the
parent's overall state is still THINKING/TOOL until the parent's Stop
fires), but we emit an eventlog row that the PWA can subscribe to so
the user can see "X spawned 2 subagents, both done" in real time.
"""
from __future__ import annotations

import json
import pathlib
import sys

import _clarp_lib  # noqa: F401  — puts Clarp's `lib` on sys.path
try:
    from lib import agents as _agents             # noqa: E402
    from lib.hook_runtime import app_session  # noqa: E402
except ImportError:
    # claude-pwa not installed on this machine — hook is a no-op.
    sys.exit(0)
try:
    from lib.eventlog import emit as _emit    # noqa: E402
except ImportError:
    def _emit(*a, **kw): pass


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except Exception:
        payload = {}

    backend_session_id = (payload.get("session_id") or "").strip()
    session = app_session()

    try:
        agent = _agents.resolve_for_hook(
            backend_session_id=backend_session_id or None,
            session=session or None,
        )
    except Exception as e:
        _emit("subagent_stop_hook", "resolveFail", detail={"err": str(e)})
        return 0
    agent_id = (agent or {}).get("agent_id") if agent else None

    _emit("subagent_stop_hook", "subagentDone",
          session=session or None,
          backend_session_id=backend_session_id or None,
          detail={"agent_id": agent_id})
    return 0


if __name__ == "__main__":
    sys.exit(main())
