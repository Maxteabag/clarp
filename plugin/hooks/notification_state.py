#!/usr/bin/env python3
"""Notification hook → record AgentState.WAITING with the message.

Fires when claude-code wants to surface something to the user — most
often a permission prompt ("Claude wants to run X"), or an idle-too-long
nag. The PWA renders a toast / banner with the message text so the user
knows the agent is paused waiting for them.
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
    message = (payload.get("message") or "").strip()
    title = (payload.get("title") or "").strip()

    session = app_session()

    try:
        agent = _agents.resolve_for_hook(
            backend_session_id=backend_session_id or None,
            session=session or None,
        )
    except Exception as e:
        _emit("notification_hook", "resolveFail", detail={"err": str(e)})
        return 0
    if not agent:
        return 0

    try:
        _agents.record_state(
            agent["agent_id"], AgentState.WAITING,
            {"message": message, "title": title,
             "backend_session_id": backend_session_id},
        )
        _emit("notification_hook", "waiting",
              session=session or None,
              backend_session_id=backend_session_id or None,
              detail={"title": title, "message": message[:200]})
    except Exception as e:
        _emit("notification_hook", "recordFail",
              session=session or None,
              detail={"err": str(e)})
    return 0


if __name__ == "__main__":
    sys.exit(main())
