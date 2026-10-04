#!/usr/bin/env python3
"""PreToolUse hook: records readable live tool activity for the PWA."""
from __future__ import annotations

import json
import os
import pathlib
import sys

import _clarp_lib  # noqa: F401  — puts Clarp's `lib` on sys.path
try:
    from lib import agents as agents_db  # noqa: E402
    from lib import turn_lifecycle  # noqa: E402
    from lib.activity import summarize_tool_activity, tool_input_from_hook_payload  # noqa: E402
    from lib.hook_runtime import app_session  # noqa: E402
    from lib.protocol import ActivityStatus  # noqa: E402
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
        turn_lifecycle.hook_transition(
            agent["agent_id"], turn_lifecycle.TurnEvent.TOOL_STARTED, {
            "phase": "tool_started",
            "call_id": payload.get("tool_use_id"),
            "cwd": payload.get("cwd"),
            "status": ActivityStatus.RUNNING,
            "tool": tool_name,
            "input": tool_input,
            **summary,
        })
    except Exception:
        pass
    if tool_name == "Bash":
        from lib.backend.claude_background_provenance import (
            background_task_context, tool_input_with_origin)
        from lib.provider_background_jobs import TURN_ENV, record_task_turn
        tool_use_id = payload.get("tool_use_id")
        updated = tool_input_with_origin(agent, backend_session_id,
                                         tool_use_id, tool_input)
        # Only a Clarp-run turn (the runner set its token) ends with its
        # reply; an interactive `claude --resume` of the same session does not.
        turn = os.environ.get(TURN_ENV, "")
        context = background_task_context(tool_input) if turn else None
        if context:
            # This process launched it; its exit is when the task ends.
            try:
                record_task_turn(agent["agent_id"], backend_session_id, tool_use_id, turn)
            except Exception:
                pass
        output = {"hookEventName": "PreToolUse"}
        if updated is not None:
            output["updatedInput"] = updated
        if context:
            output["additionalContext"] = context
        if len(output) > 1:
            print(json.dumps({"hookSpecificOutput": output}))
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
