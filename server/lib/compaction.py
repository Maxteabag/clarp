"""On-demand conversation compaction.

Print mode (`clarp -p`) never auto-compacts, so a Claude conversation grows
unbounded until the model calls stall. (Codex/agy auto-compact in their own
loops, but we expose manual compaction for them too.) This drives the backend
CLI's *interactive* compaction — `/compact` (claude, codex) or `/compress`
(agy) — in a throwaway tmux session resumed on the SAME backend session id,
waits for it to settle, then kills the tmux. The session is left compacted in
place (same id), so the next PWA turn resumes the small conversation.
"""
from __future__ import annotations

import pathlib
import shutil
import subprocess
import threading
import time
from typing import Any

from . import agents as agents_db
from . import backends
from .log import log, log_exception
from .transcript_log import find_latest_jsonl

# (interactive launch argv prefix, compact command) per backend. The backend
# session id is appended to the launch argv.
_COMPACT: dict[str, tuple[list[str], str]] = {
    adapter.id: (list(adapter.compact_launch), adapter.compact_command)
    for adapter in backends.adapters()
    if adapter.compact_launch and adapter.compact_command
}

_STARTUP_WAIT_SEC = 10.0       # let the interactive CLI boot before sending keys
_SETTLE_SEC = 8.0             # transcript must be quiet this long → compaction done
_HARD_CAP_SEC = 180.0         # absolute ceiling before we give up and kill
_NO_WATCH_WAIT_SEC = 50.0     # fixed wait when we can't watch a transcript (codex/agy)

_active: set[str] = set()
_lock = threading.Lock()


def is_compacting(session: str) -> bool:
    with _lock:
        return session in _active


def compact_session(session: str) -> dict[str, Any]:
    """Kick off compaction for a session in the background. Returns immediately;
    the app polls the snapshot (compacting flag + context_tokens) for progress."""
    agent = agents_db.get_by_session(session)
    if not agent:
        return {"ok": False, "error": "no such agent"}
    backend = backends.normalize(agent.get("backend"))
    agent_id = agent["agent_id"]
    bsid = agents_db.live_backend_session(agent_id)
    if not bsid:
        return {"ok": False, "error": "no live session to compact"}
    spec = _COMPACT.get(backend)
    if spec is None:
        return {"ok": False, "error": f"compaction unsupported for {backend}"}
    launch, _cmd = spec
    if shutil.which(launch[0]) is None:
        return {"ok": False, "error": f"{launch[0]} not on PATH"}
    # Don't drive an interactive session while a turn is in-flight on the same
    # session — two processes on one transcript would collide.
    if backends.active_handles(backend, agent_id):
        return {"ok": False, "error": "agent is busy — try again when idle"}
    with _lock:
        if session in _active:
            return {"ok": True, "status": "already_compacting", "backend": backend}
        _active.add(session)
    from .launch_paths import existing_workspace_path
    cwd = str(existing_workspace_path(agent.get("cwd")))
    threading.Thread(
        target=_run, args=(session, backend, bsid, cwd, launch, _cmd),
        daemon=True, name=f"compact-{session}",
    ).start()
    return {"ok": True, "status": "started", "backend": backend}


def _run(session: str, backend: str, bsid: str, cwd: str,
         launch: list[str], compact_cmd: str) -> None:
    tmux = f"pwa-compact-{session}"
    try:
        subprocess.run(["tmux", "kill-session", "-t", tmux],
                       capture_output=True, check=False)
        subprocess.run(
            ["tmux", "new-session", "-d", "-s", tmux, "-c", cwd, "--",
             *launch, bsid],
            check=True, capture_output=True, text=True,
        )
        log("compactStart",
            f"session={session} backend={backend} bsid={bsid} cmd={compact_cmd}")
        time.sleep(_STARTUP_WAIT_SEC)
        subprocess.run(["tmux", "send-keys", "-t", tmux, compact_cmd, "Enter"],
                       check=True, capture_output=True)
        _wait_for_settle(backend, bsid)
        log("compactDone", f"session={session}")
    except Exception as e:  # noqa: BLE001
        log_exception("compactFail", e, detail=session)
    finally:
        subprocess.run(["tmux", "kill-session", "-t", tmux],
                       capture_output=True, check=False)
        with _lock:
            _active.discard(session)


def _wait_for_settle(backend: str, bsid: str) -> None:
    """Wait until compaction finishes. For Claude we watch the transcript jsonl:
    once it's written past the /compact send AND then goes quiet for _SETTLE_SEC,
    compaction is done. For codex/agy (no easy transcript handle here) we wait a
    generous fixed window. Always bounded by _HARD_CAP_SEC."""
    sent = time.time()
    deadline = sent + _HARD_CAP_SEC
    jsonl = find_latest_jsonl(bsid) if backend == backends.CLAUDE else None
    if jsonl is None:
        while time.time() < deadline and time.time() - sent < _NO_WATCH_WAIT_SEC:
            time.sleep(2)
        return
    advanced = False
    stable_since: float | None = None
    last_mtime = jsonl.stat().st_mtime if jsonl.exists() else 0.0
    while time.time() < deadline:
        time.sleep(2)
        m = jsonl.stat().st_mtime if jsonl.exists() else last_mtime
        if m > sent:
            advanced = True
        if advanced and m == last_mtime:
            if stable_since is None:
                stable_since = time.time()
            elif time.time() - stable_since >= _SETTLE_SEC:
                return
        else:
            stable_since = None
        last_mtime = m
