"""Activity sensors for registered AGY conversations run outside Clarp.

AGY 1.1.21 uses camelCase lifecycle payloads and holds an exclusive flock on
presence/<conversationId>.lock for the lifetime of its CLI. Lifecycle events
say whether it is working; the lock only prevents repairing live work to idle.
"""
from __future__ import annotations

import fcntl
import json
import os
import shlex
import tempfile
from pathlib import Path
from uuid import UUID

from . import agents, agy_transcript, backends
from .protocol import AgentState


def record_event(event: str, payload: object) -> None:
    # stream-json owns state and terminal callbacks for all managed runs,
    # including isolated runs. Hooks must never bypass that ownership fence.
    if os.environ.get("CLARP_AGY_MANAGED_TURN") == "1":
        return
    if event not in {"PreInvocation", "Stop"} or not isinstance(payload, dict):
        return
    conversation_id = payload.get("conversationId")
    if not isinstance(conversation_id, str) or not conversation_id:
        return
    agent = agents.get_by_backend_session(conversation_id)
    if not agent or backends.normalize(agent.get("backend")) != backends.AGY:
        return
    detail = {"source": "agy_hook",
              "backend_session_id": conversation_id}
    kind = AgentState.THINKING
    if event == "Stop":
        reason = str(payload.get("terminationReason") or "").upper()
        if payload.get("error") or reason not in {"NO_TOOL_CALL", "MODEL_STOP"}:
            kind = AgentState.INTERRUPTED
            detail["message"] = "Antigravity turn interrupted — send again to resume"
        elif payload.get("fullyIdle") is False:
            kind = AgentState.BACKGROUND
        else:
            kind = AgentState.DONE
    agents.record_state(agent["agent_id"], kind, detail)


def has_live_work(agent_id: str) -> bool:
    state = agents.latest_state(agent_id) or {}
    detail = state.get("detail") or {}
    if (state.get("kind") not in AgentState.busy_states()
            or not isinstance(detail, dict) or detail.get("source") != "agy_hook"):
        return False
    conversation_id = agents.live_backend_session(agent_id)
    if not conversation_id or conversation_id != detail.get("backend_session_id"):
        return False
    try:
        UUID(conversation_id)  # Never use an arbitrary session string as a path.
        presence = agy_transcript._agy_home().parent / "presence" / f"{conversation_id}.lock"
        with presence.open("rb") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
    except (OSError, ValueError):
        pass
    return False


# Resolve the interpreter on each invocation, just like the managed wrappers.
# The shell fallback also makes rollback to a pre-hook release a harmless no-op.
_LAUNCH = """\
if [ -f "$1/plugin/hooks/agy_state.py" ] && [ -r "$1/SERVICE_PYTHON" ]; then
    IFS= read -r hook_python < "$1/SERVICE_PYTHON"
    if [ -x "$hook_python" ]; then
        exec "$hook_python" "$1/plugin/hooks/agy_state.py" "$2"
    fi
fi
printf '{}\\n'
"""


def hook_configuration(share: Path) -> dict:
    return {
        event: [{"type": "command", "timeout": 5, "command": shlex.join([
            "env", f"CLARP_SHARE_DIR={share}",
            f"CLAUDE_PWA_DB={share / 'state.sqlite'}",
            "sh", "-c", _LAUNCH, "clarp-agy-hook", str(share / "current"), event])}]
        for event in ("PreInvocation", "Stop")
    }


def _write_hooks(path: Path, hooks: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".clarp-hooks-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(hooks, handle, indent=2)
            handle.write("\n")
        if path.exists():
            os.chmod(temporary, path.stat().st_mode & 0o777)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def configure_hooks(share: Path, home: Path, *, remove: bool = False) -> bool:
    """Merge only our named lifecycle sensor, preserving other user hooks.

    Commands point through current so an update/rollback follows the active
    release. Refuse malformed files, symlinks, and a user-owned clarp-status key.
    """
    path = home / ".gemini/config/hooks.json"
    if path.is_symlink():
        return False
    try:
        hooks = json.loads(path.read_text())
    except FileNotFoundError:
        hooks = {}
    except (OSError, ValueError):
        return False
    expected = hook_configuration(share)
    if not isinstance(hooks, dict):
        return False
    if "clarp-status" in hooks and hooks["clarp-status"] != expected:
        return False
    if remove:
        if "clarp-status" not in hooks:
            return True
        del hooks["clarp-status"]
    else:
        if hooks.get("clarp-status") == expected:
            return True
        hooks["clarp-status"] = expected
    try:
        _write_hooks(path, hooks)
    except OSError:
        return False
    return True
