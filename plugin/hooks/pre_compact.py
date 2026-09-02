#!/usr/bin/env python3
"""PreCompact hook → record AgentState.COMPACTING.

Fires before claude-code compacts its conversation history. The PWA
shows a "Compacting context…" banner from this point until the next
state event (THINKING from the next user prompt, DONE from a stop, etc.)
clears it.

There's no compaction-progress event from claude-code — just start.
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
    # PreCompact payload includes `trigger` ("manual" or "auto") and
    # `custom_instructions` — pass them through so the UI can show the
    # reason if it wants.
    trigger = payload.get("trigger") or "auto"

    session = app_session()

    try:
        agent = _agents.resolve_for_hook(
            backend_session_id=backend_session_id or None,
            session=session or None,
        )
    except Exception as e:
        _emit("precompact_hook", "resolveFail", detail={"err": str(e)})
        return 0
    if not agent:
        return 0

    try:
        _agents.record_state(
            agent["agent_id"], AgentState.COMPACTING,
            {"trigger": trigger, "backend_session_id": backend_session_id},
        )
        _emit("precompact_hook", "compacting",
              session=session or None,
              backend_session_id=backend_session_id or None,
              detail={"trigger": trigger})
    except Exception as e:
        _emit("precompact_hook", "recordFail",
              session=session or None,
              detail={"err": str(e)})
    return 0


if __name__ == "__main__":
    sys.exit(main())
