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

_STARTUP_WAIT_SEC = 10.0       # let the interactive CLI boot before sending keys
_SETTLE_SEC = 8.0             # transcript must be quiet this long → compaction done
_HARD_CAP_SEC = 180.0         # absolute ceiling before we give up and kill
_NO_WATCH_WAIT_SEC = 50.0     # fixed wait when we can't watch a transcript (codex/agy)

_active: set[str] = set()
_lock = threading.Lock()
_runtime_client: Any | None = None


def configure_runtime_client(client: Any | None) -> None:
    global _runtime_client
    _runtime_client = client


def is_compacting(session: str) -> bool:
    if _runtime_client is not None:
        try:
            return session in set(
                _runtime_client.status().get("compactions") or ())
        except Exception:
            agent = agents_db.get_by_session(session)
            latest = agents_db.latest_state(agent["agent_id"]) if agent else None
            return bool(latest and latest.get("kind") == "compacting")
    with _lock:
        return session in _active


def active_sessions() -> list[str]:
    with _lock:
        return sorted(_active)


def compact_session(session: str) -> dict[str, Any]:
    """Kick off compaction for a session in the background. Returns immediately;
    the app polls the snapshot (compacting flag + context_tokens) for progress."""
    agent = agents_db.get_by_session(session)
    if agent and not agents_db.interaction_capabilities(agent)["can_restart"]:
        return {"ok": False, "error": "Janitors are managed from their configuration"}
    if _runtime_client is not None:
        return _runtime_client.compact(session)
    if not agent:
        return {"ok": False, "error": "no such agent"}
    backend = backends.normalize(agent.get("backend"))
    agent_id = agent["agent_id"]
    bsid = agents_db.live_backend_session(agent_id)
    if not bsid:
        return {"ok": False, "error": "no live session to compact"}
    try:
        strategy = backends.by_id(backend).compaction(bsid)
    except backends.Unsupported:
        return {"ok": False, "error": f"compaction unsupported for {backend}"}
    launch = list(strategy.launch)
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
        target=_run, args=(session, backend, bsid, cwd, strategy),
        daemon=True, name=f"compact-{session}",
    ).start()
    return {"ok": True, "status": "started", "backend": backend}


def _run(session: str, backend: str, bsid: str, cwd: str,
         strategy: backends.CompactionStrategy) -> None:
    tmux = f"pwa-compact-{session}"
    launch, compact_cmd = list(strategy.launch), strategy.command
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
        _wait_for_settle(backend, bsid, strategy)
        log("compactDone", f"session={session}")
    except Exception as e:  # noqa: BLE001
        log_exception("compactFail", e, detail=session)
    finally:
        subprocess.run(["tmux", "kill-session", "-t", tmux],
                       capture_output=True, check=False)
        with _lock:
            _active.discard(session)


def _wait_for_settle(backend: str, bsid: str,
                     strategy: backends.CompactionStrategy) -> None:
    """Wait until compaction finishes. When the strategy watches its
    transcript (Claude) we watch the jsonl: once it's written past the
    /compact send AND then goes quiet for _SETTLE_SEC, compaction is done.
    Otherwise (no easy transcript handle here) we wait a generous fixed
    window. Always bounded by _HARD_CAP_SEC."""
    sent = time.time()
    deadline = sent + _HARD_CAP_SEC
    jsonl = (backends.by_id(backend).find_transcript(bsid)
             if strategy.watches_transcript else None)
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
