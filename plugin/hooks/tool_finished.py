#!/usr/bin/env python3
"""PostToolUse hook: records that a tool call finished.

PreToolUse (tool_activity.py) records the tool starting; this records it
returning, so the PWA's live activity line advances instead of sticking on
the last-started tool. State only — audio is driven server-side by the
transcript streamer, not by hooks.
"""
import sys, json, pathlib

import _clarp_lib  # noqa: F401  — puts Clarp's `lib` on sys.path
try:
    from lib import agents as agents_db                  # noqa: E402
    from lib.activity import (                           # noqa: E402
        summarize_tool_activity,
        tool_input_from_hook_payload,
        tool_status_from_hook_payload,
    )
    from lib.hook_runtime import HookLogger, app_session  # noqa: E402
    from lib.paths import RuntimePaths                  # noqa: E402
    from lib.protocol import AgentState                 # noqa: E402
except ImportError:
    # claude-pwa not installed on this machine — hook is a no-op.
    sys.exit(0)

PATHS = RuntimePaths.from_home(pathlib.Path.home())

try:
    from lib.eventlog import emit as _emit_event
except ImportError:
    def _emit_event(*a, **kw): pass

_LOGGER = HookLogger("toolend", PATHS.hook_log, emit=_emit_event)


def _log(msg):
    _LOGGER.log(msg)


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    backend_session_id = payload.get("session_id") or ""
    tool_name = (payload.get("tool_name") or
                 (payload.get("tool") or {}).get("name") or "?")
    _log(f"fire tool={tool_name} session={backend_session_id[:8]}")
    if not backend_session_id:
        return 0

    session = app_session()
    try:
        agent = agents_db.resolve_for_hook(
            backend_session_id=backend_session_id,
            session=session or None,
        )
    except Exception:
        return 0
    if not agent:
        return 0

    tool_input = tool_input_from_hook_payload(payload)
    summary = summarize_tool_activity(tool_name, tool_input)
    try:
        agents_db.record_state(agent["agent_id"], AgentState.TOOL,
                               {"phase": "tool_finished",
                                "status": tool_status_from_hook_payload(payload),
                                "tool": tool_name,
                                "input": tool_input,
                                **summary})
    except Exception as e:
        _log(f"record fail: {e}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main() or 0)
    except SystemExit:
        raise
    except BaseException as e:
        _log(f"crash {type(e).__name__}: {e}")
        sys.exit(0)
