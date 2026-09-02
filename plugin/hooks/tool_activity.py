#!/usr/bin/env python3
"""PreToolUse hook: records readable live tool activity for the PWA."""
from __future__ import annotations

import json
import pathlib
import sys

import _clarp_lib  # noqa: F401  — puts Clarp's `lib` on sys.path
try:
    from lib import agents as agents_db  # noqa: E402
    from lib.activity import summarize_tool_activity, tool_input_from_hook_payload  # noqa: E402
    from lib.hook_runtime import app_session  # noqa: E402
    from lib.protocol import ActivityStatus, AgentState  # noqa: E402
except ImportError:
    # claude-pwa not installed on this machine — hook is a no-op.
    sys.exit(0)


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0

    backend_session_id = (payload.get("session_id") or "").strip()
    session = app_session()
    tool_name = (
        payload.get("tool_name")
        or (payload.get("tool") or {}).get("name")
        or "tool"
    )
    tool_input = tool_input_from_hook_payload(payload)

    try:
        agent = agents_db.resolve_for_hook(
            backend_session_id=backend_session_id or None,
            session=session or None,
        )
    except Exception:
        agent = None
    if not agent:
        return 0

    summary = summarize_tool_activity(tool_name, tool_input)
    try:
        agents_db.record_state(agent["agent_id"], AgentState.TOOL, {
            "phase": "tool_started",
            "status": ActivityStatus.RUNNING,
            "tool": tool_name,
            "input": tool_input,
            **summary,
        })
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
