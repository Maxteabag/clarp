#!/usr/bin/env python3
"""Stop hook → record AgentState.DONE.

Fires when claude-code finishes its turn. We record an explicit `done`
state so the PWA can light the dock badge ONLY on real turn completion —
not on every interim assistant chunk or tool result.

turn_dispatch records DONE too, from the CLI's own `result` event. This hook
is the fallback that covers turns the server did not dispatch: an interactive
/terminal session, or a bare `claude` run in a shell.
"""
from __future__ import annotations

import json
import pathlib
import sys

import _clarp_lib  # noqa: F401  — puts Clarp's `lib` on sys.path
try:
    from lib import agents as _agents             # noqa: E402
    from lib.hook_runtime import app_session  # noqa: E402
    from lib.protocol import AgentState           # noqa: E402
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
        _emit("stop_hook", "resolveFail", detail={"err": str(e)})
        return 0
    if not agent:
        return 0

    agent_id = agent["agent_id"]
    try:
        _agents.record_state(
            agent_id, AgentState.DONE,
            {"backend_session_id": backend_session_id, "source": "stop_hook"},
        )
        _emit("stop_hook", "done",
              session=session or None,
              backend_session_id=backend_session_id or None)
    except Exception as e:
        _emit("stop_hook", "recordFail",
              session=session or None,
              detail={"err": str(e)})
    return 0


if __name__ == "__main__":
    sys.exit(main())
