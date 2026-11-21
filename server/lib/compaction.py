"""On-demand conversation compaction: the user's button (``POST /compact``).

Nothing else calls it: the Host never compacts an agent by itself, on a timer,
at a threshold or at launch (a test pins that). The compaction takes the
agent's turn slot, so a send that arrives meanwhile waits behind it in order
and starts when it ends (``turn_dispatch.finish_compaction``); a Stop takes the
slot back and ends it.

How it is done depends on who holds the conversation:

* Codex: the Host's one shared app-server holds every Codex thread in memory,
  so only that process can shrink one. The Host asks it
  (``thread/compact/start``). A second ``codex resume`` typing ``/compact``,
  as before, left the app-server serving its old copy (R148).
* Claude Code, Antigravity, Grok: each turn is its own process, so the CLI's
  interactive ``/compact`` (``/compress`` for agy) is typed into a throwaway
  tmux session resumed on the same session id, which is then killed.

What happened is said from numbers, never from a clock (``last_compaction``
on the agent's row, and one ``compactOutcome`` log line):

* ``compacted``: Codex reported a size after that is smaller than the size
  before; Claude Code wrote its compaction record (``compact_boundary``) after
  ``/compact`` was typed (its size after is known at the agent's next call);
* ``not_compacted``: refused, failed, stopped, or the conversation did not
  shrink;
* ``unknown``: a number is missing or it did not finish in time; for a CLI
  that gives the Host no size at all, always.
"""
from __future__ import annotations

import json
import os
import select
import shutil
import signal
import subprocess
import threading
import time
from typing import Any

from . import agents as agents_db
from . import backends, settings_store
from .log import log, log_exception

_STARTUP_WAIT_SEC = 10.0       # let the interactive CLI boot before sending keys
_SETTLE_SEC = 8.0             # transcript must be quiet this long → compaction done
_HARD_CAP_SEC = 180.0         # absolute ceiling before we give up and kill
_NO_WATCH_WAIT_SEC = 50.0     # time a CLI with no transcript to watch gets before the kill
_POLL_SEC = 2.0

COMPACTED = "compacted"
NOT_COMPACTED = "not_compacted"
UNKNOWN = "unknown"
_OUTCOME_KEY = "compaction.last."
OUTCOME_PREFIX = _OUTCOME_KEY   # the snapshot reads these with its other settings

_active: set[str] = set()
# A terminal compaction's CLI that outlived its kill, by session.
_lingering: dict[str, "_Process"] = {}
_lock = threading.Lock()
_REAP_SEC = 10.0              # a killed terminal CLI gets this long to exit
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


def _refuse(session: str, error: str) -> dict[str, Any]:
    log("compactRefused", f"session={session or '∅'} reason={error}")
    return {"ok": False, "error": error}


def compact_session(session: str, *, dispatch: Any = None) -> dict[str, Any]:
    """Start the user's compaction of a session in the background. Answers at
    once: started, already compacting, or refused in words. The outcome lands
    on the agent's row (``last_compaction``) when it ends. ``dispatch`` is the
    turn dispatcher, which starts the sends that waited behind it."""
    agent = agents_db.get_by_session(session)
    if agent and not agents_db.interaction_capabilities(agent)["can_restart"]:
        return _refuse(session, "Janitors are managed from their configuration")
    from . import agent_moves
    if agent and agent_moves.moved(agent["agent_id"]) is not None:
        # Its session is the other Host's now (lib.agent_moves).
        return _refuse(session, "the agent moved to another Host")
    if _runtime_client is not None:
        return _runtime_client.compact(session)
    if not agent:
        return _refuse(session, "no such agent")
    backend = backends.normalize(agent.get("backend"))
    agent_id = agent["agent_id"]
    bsid = agents_db.live_backend_session(agent_id)
    if not bsid:
        return _refuse(session, "no live session to compact")
    try:
        strategy = backends.by_id(backend).compaction(bsid)
    except backends.Unsupported:
        return _refuse(session, f"compaction unsupported for {backend}")
    if not strategy.in_process:
        launch = list(strategy.launch)
        # A launch may set environment first (`env NAME=value program ...`).
        program = next((part for part in launch if part != "env" and "=" not in part), "")
        if shutil.which(program) is None:
            return _refuse(session, f"{program} not on PATH")
    # Don't compact while a turn is in flight on the same session — two
    # processes on one conversation would collide.
    if backends.active_handles(backend, agent_id):
        return _refuse(session, "agent is busy — try again when idle")
    # Before the slot is claimed: a raise after it left the agent compacting,
    # its sends held, until a restart (R174).
    from .launch_paths import existing_workspace_path
    cwd = str(existing_workspace_path(agent.get("cwd")))
    with _lock:
        if session in _active:
            return {"ok": True, "status": "already_compacting", "backend": backend}
    from . import turn_dispatch
    refusal = turn_dispatch.claim_for_compaction(agent_id)
    if refusal == "already compacting":
        with _lock:
            running = session in _active
        if not running:
            # Its outcome is recorded; only a process of it still ends.
            return _refuse(session, "the last compaction is still ending — try again shortly")
        return {"ok": True, "status": "already_compacting", "backend": backend}
    if refusal:
        return _refuse(session, refusal)
    with _lock:
        _active.add(session)
    try:
        threading.Thread(
            target=_run, args=(session, dict(agent), backend, bsid, cwd, strategy, dispatch),
            daemon=True, name=f"compact-{session}",
        ).start()
    except BaseException:
        with _lock:
            _active.discard(session)
        turn_dispatch.finish_compaction(dispatch, agent_id)
        raise
    log("compactStart", f"session={session} backend={backend} bsid={bsid} "
                        f"cmd={strategy.command}")
    return {"ok": True, "status": "started", "backend": backend}


# ---- the outcome ------------------------------------------------------------

def outcome(kind: str, *, before: int | None = None, after: int | None = None,
            after_is_estimate: bool = False, detail: str = "") -> dict[str, Any]:
    return {"outcome": kind, "before_tokens": before, "after_tokens": after,
            "after_is_estimate": bool(after_is_estimate and after is not None),
            "detail": detail}


def decide_codex(before: int | None, answer: dict[str, Any]) -> dict[str, Any]:
    """Codex's rule: ``compacted`` needs both sizes and after < before. The size
    after is Codex's own estimate of the new history."""
    status = str(answer.get("status") or "")
    error = str(answer.get("error") or "")
    after = answer.get("after") if answer.get("replaced") else None
    after = after if type(after) is int else None
    if before is not None and after is not None and after < before:
        return outcome(COMPACTED, before=before, after=after, after_is_estimate=True,
                       detail="Codex replaced the history; the size after is its estimate")
    if status == "refused":
        return outcome(NOT_COMPACTED, before=before, detail=error or "Codex refused")
    if status == "interrupted":
        return outcome(NOT_COMPACTED, before=before, after=after, after_is_estimate=True,
                       detail="stopped before Codex finished compacting")
    if status == "failed":
        return outcome(NOT_COMPACTED, before=before, after=after, after_is_estimate=True,
                       detail=f"Codex reported a failure: {error or 'no reason given'}")
    if status == "timeout":
        return outcome(UNKNOWN, before=before, detail=error or "Codex did not finish in time")
    if not answer.get("replaced"):
        # It ended as completed, but Codex never reported its compaction item.
        return outcome(NOT_COMPACTED, before=before,
                       detail="Codex finished without replacing the conversation's history")
    if before is not None and after is not None:
        return outcome(NOT_COMPACTED, before=before, after=after, after_is_estimate=True,
                       detail="the conversation did not shrink")
    if after is None:
        return outcome(UNKNOWN, before=before,
                       detail="Codex reported no size for the history after compacting")
    return outcome(UNKNOWN, after=after, after_is_estimate=True,
                   detail="the size before is unknown: Codex reported none since the Host "
                          "started and the rollout's last 64 KB holds no token count")


def decide_claude(before: int | None, proof: int | None, *, transcript: bool,
                  ending: str) -> dict[str, Any]:
    """Claude's rule: ``compacted`` on Claude Code's own record of it (the
    ``compact_boundary`` written after /compact was typed); its size after is
    known only at the agent's next call."""
    if not transcript:
        return outcome(UNKNOWN, before=before,
                       detail="the Host found no transcript to check the compaction in")
    if proof is not None:
        return outcome(COMPACTED, before=proof or before,
                       detail="Claude Code recorded the compaction; the size after is "
                              "known at the agent's next call")
    if ending == "stopped":
        return outcome(NOT_COMPACTED, before=before,
                       detail="stopped before Claude Code recorded a compaction")
    if ending == "cap":
        return outcome(UNKNOWN, before=before,
                       detail=f"Claude Code did not finish within {_HARD_CAP_SEC:g} s")
    return outcome(NOT_COMPACTED, before=before,
                   detail="Claude Code recorded no compaction")


def record(agent_id: str, session: str, backend: str, result: dict[str, Any]) -> None:
    """Keep the outcome for the agent's row and say it in one log line."""
    result = {**result, "backend": backend, "at": int(time.time() * 1000)}
    log("compactOutcome",
        f"session={session} backend={backend} outcome={result['outcome']} "
        f"before={result.get('before_tokens')} after={result.get('after_tokens')}"
        f"{' (estimate)' if result.get('after_is_estimate') else ''} "
        f"detail={result.get('detail') or '∅'}")
    try:
        settings_store.set_text(_OUTCOME_KEY + agent_id, json.dumps(result))
    except Exception as e:  # noqa: BLE001
        log_exception("compactOutcomeSaveFail", e, detail=session)


def last_outcomes(agent_ids: list[str]) -> dict[str, dict[str, Any]]:
    """``last_compaction`` for each agent that has one (one read for all)."""
    if not agent_ids:
        return {}
    keys = [_OUTCOME_KEY + a for a in agent_ids]
    raw: dict[str, str] = {}
    try:
        for start in range(0, len(keys), 500):   # bounded SQL parameter lists
            raw.update(settings_store.get_many(keys[start:start + 500]))
    except Exception as e:  # noqa: BLE001
        log_exception("compactOutcomeReadFail", e)
        return {}
    return outcomes_from(raw)


def outcomes_from(raw: dict[str, str]) -> dict[str, dict[str, Any]]:
    """``last_compaction`` by agent from settings rows; keys not under OUTCOME_PREFIX are skipped."""
    found: dict[str, dict[str, Any]] = {}
    for key, text in raw.items():
        if not key.startswith(_OUTCOME_KEY):
            continue
        try:
            value = json.loads(text)
        except (TypeError, ValueError):
            continue
        if isinstance(value, dict):
            found[key[len(_OUTCOME_KEY):]] = value
    return found


# ---- the run ----------------------------------------------------------------

def _run(session: str, agent: dict, backend: str, bsid: str, cwd: str,
         strategy: backends.CompactionStrategy, dispatch: Any) -> None:
    agent_id = agent["agent_id"]
    result: dict[str, Any] | None = None
    try:
        if strategy.in_process:
            result = _compact_codex(agent, bsid)
        else:
            result = _compact_in_terminal(session, agent_id, backend, bsid, cwd, strategy)
    except Exception as e:  # noqa: BLE001
        log_exception("compactFail", e, detail=session)
        result = outcome(UNKNOWN, detail=f"the compaction failed: {e}")
    finally:
        record(agent_id, session, backend, result or outcome(UNKNOWN, detail="interrupted"))
        # The compaction is over: the agent no longer reads as compacting,
        # even while a process that still holds its conversation keeps the
        # slot below (a send then waits as behind any stuck session).
        with _lock:
            _active.discard(session)
        try:
            # The slot is given back only once nothing still works on the
            # conversation: sends that waited never run on top of it.
            _hold_while_held(session, agent_id, backend)
        except Exception as e:  # noqa: BLE001
            log_exception("compactHoldFail", e, detail=session)
        with _lock:
            lingering = _lingering.pop(session, None)
        if lingering is not None:
            lingering.close()
        try:
            from . import turn_dispatch
            turn_dispatch.finish_compaction(dispatch, agent_id)
        except Exception as e:  # noqa: BLE001
            log_exception("compactFinishFail", e, detail=session)


def _alive(items) -> bool:
    for item in items or ():
        try:
            if item.is_alive():
                return True
        except Exception:  # noqa: BLE001 - can't tell: treat as alive
            return True
    return False


class _Process:
    """The terminal compaction's CLI, held by a pidfd where the system has
    one: its exit is seen even before anyone reaps it, and a signal through
    it can reach no other process, whatever pid reuse does. Without a pidfd
    (macOS, an old kernel) the Host never signals it and waits only a
    bounded time."""

    def __init__(self, pid: int, fd: int | None):
        self.pid, self.fd = pid, fd

    def exited(self) -> bool:
        if self.fd is None:
            return True             # nothing reliable to watch: never held for it
        readable, _, _ = select.select([self.fd], [], [], 0)
        return bool(readable)       # readable once it exited, zombie or not

    def kill(self) -> bool:
        if self.fd is None:
            return False
        try:
            signal.pidfd_send_signal(self.fd, signal.SIGKILL)
        except ProcessLookupError:
            pass                    # already gone
        except OSError as e:
            log_exception("compactCliKillFail", e, detail=str(self.pid))
            return False
        return True

    def close(self) -> None:
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None


def _pid_alive(pid: int) -> bool:
    """For the no-pidfd path's bounded wait only (a zombie or a reused pid
    reads alive here, so it decides nothing but when to stop waiting)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _still_held(session: str, agent_id: str, backend: str) -> bool:
    """Whether a process still works on the agent's conversation: a backend
    turn handle (a Codex compaction not ended yet), a process holding its
    session (a detached, stuck Codex process until it is recycled), or the
    interactive CLI a terminal compaction started, if it outlived its kill."""
    lingering = _lingering.get(session)
    if lingering is not None and not lingering.exited():
        return True
    return (_alive(backends.active_handles(backend, agent_id))
            or _alive(backends.turn_holders(backend, agent_id)))


def _hold_while_held(session: str, agent_id: str, backend: str) -> None:
    """Keep the compaction's slot while a process still works on the
    conversation, so a send that waited behind it starts only after. Ends at
    once when a Stop took the slot (Stop then owns what follows)."""
    from . import turn_dispatch
    logged = False
    while turn_dispatch.compaction_holds_slot(agent_id) and _still_held(session, agent_id, backend):
        if not logged:
            log("compactHoldsForProcess",
                f"session={session} backend={backend} — a process still holds the "
                "conversation; waiting messages start once it ends")
            logged = True
        time.sleep(_POLL_SEC)


def _codex_size_before(bsid: str) -> int | None:
    """What Codex last reported for the thread; after a runtime restart, the
    newest token count in the rollout's last 64 KB (read only here, on the
    user's button)."""
    from . import codex_app_server, codex_transcript
    size = codex_app_server.last_context_size(bsid)
    if size is not None:
        return size
    path = codex_transcript.find_latest_jsonl(bsid)
    return codex_transcript.last_context_size(path) if path is not None else None


def _compact_codex(agent: dict, bsid: str) -> dict[str, Any]:
    from . import codex_app_server
    before = _codex_size_before(bsid)
    answer = codex_app_server.compact_thread(agent, bsid, timeout=_HARD_CAP_SEC)
    return decide_codex(before, answer)


def _compact_in_terminal(session: str, agent_id: str, backend: str, bsid: str,
                         cwd: str, strategy: backends.CompactionStrategy) -> dict[str, Any]:
    from . import turn_dispatch
    runner = backends.by_id(backend)
    jsonl = runner.find_transcript(bsid) if strategy.watches_transcript else None
    before = offset = None
    if jsonl is not None:
        from .claude_transcript import context_tokens_from_jsonl
        before = context_tokens_from_jsonl(jsonl)
        try:
            offset = jsonl.stat().st_size
        except OSError:
            offset = 0
    tmux = f"pwa-compact-{session}"
    ending = "stopped"
    proc: _Process | None = None
    try:
        subprocess.run(["tmux", "kill-session", "-t", tmux],
                       capture_output=True, check=False)
        subprocess.run(
            ["tmux", "new-session", "-d", "-s", tmux, "-c", cwd, "--",
             *strategy.launch, bsid],
            check=True, capture_output=True, text=True,
        )
        proc = _pane_process(tmux)
        stopped = lambda: not turn_dispatch.compaction_holds_slot(agent_id)  # noqa: E731
        if _sleep(_STARTUP_WAIT_SEC, stopped):
            subprocess.run(["tmux", "send-keys", "-t", tmux, strategy.command, "Enter"],
                           check=True, capture_output=True)
            ending = _wait_for_settle(jsonl, stopped)
    finally:
        subprocess.run(["tmux", "kill-session", "-t", tmux],
                       capture_output=True, check=False)
        if proc is not None:
            if _reap(proc):
                proc.close()
            else:
                # Still running after SIGKILL: the slot is kept while it lives.
                with _lock:
                    _lingering[session] = proc
    if not strategy.watches_transcript:
        return outcome(UNKNOWN, detail=(
            f"stopped; {backend} gives the Host no size to compare" if ending == "stopped"
            else f"{backend} gives the Host no size to compare, so whether it compacted is not known"))
    from .claude_transcript import compact_boundary_since
    proof = compact_boundary_since(jsonl, offset or 0) if jsonl is not None else None
    return decide_claude(before, proof, transcript=jsonl is not None, ending=ending)


def _pane_state(tmux: str) -> tuple[int, bool]:
    """(pane pid, pane dead) of the session's only pane; (0, True) unknown."""
    try:
        out = subprocess.run(
            ["tmux", "display-message", "-p", "-t", tmux, "#{pane_pid} #{pane_dead}"],
            capture_output=True, text=True, check=False).stdout.split()
        return (int(out[0]), out[1] == "1") if len(out) == 2 and out[0].isdigit() else (0, True)
    except (OSError, ValueError):
        return 0, True


def _pane_process(tmux: str) -> _Process | None:
    """The interactive CLI (tmux runs a multi-argument command as the pane's
    process itself, without sh -c). Its pidfd is opened while the pane is
    alive, and the pane is asked again after: same pid and still alive means
    the pidfd is that CLI, not a process that reused its pid."""
    pid, dead = _pane_state(tmux)
    if not pid or dead:
        return None
    opener = getattr(os, "pidfd_open", None)
    if opener is None or not hasattr(signal, "pidfd_send_signal"):
        return _Process(pid, None)
    try:
        fd = opener(pid)
    except OSError:
        return None                 # already gone
    again, dead = _pane_state(tmux)
    if again != pid or dead:
        os.close(fd)
        return None
    return _Process(pid, fd)


def _reap(proc: _Process) -> bool:
    """After the tmux kill: wait for the CLI to exit (bounded), SIGKILL it
    through its pidfd if it does not, and say whether it is gone. A
    compaction's CLI must not keep writing the conversation while the agent's
    next turn runs. Without a pidfd: no signal, a bounded wait, then the slot
    is released with a log line."""
    if proc.fd is None:
        deadline = time.time() + _REAP_SEC
        while time.time() < deadline and _pid_alive(proc.pid):
            time.sleep(0.2)
        if _pid_alive(proc.pid):
            log("compactCliUnwatched",
                f"pid={proc.pid} — no pidfd to watch or signal it; released after "
                f"{_REAP_SEC:g} s")
        return True
    deadline = time.time() + _REAP_SEC
    while time.time() < deadline:
        if proc.exited():
            return True
        time.sleep(0.2)
    proc.kill()
    deadline = time.time() + 2.0
    while time.time() < deadline:
        if proc.exited():
            return True
        time.sleep(0.1)
    log("compactCliLingers", f"pid={proc.pid} — still running after SIGKILL")
    return False


def _sleep(seconds: float, stopped) -> bool:
    """Wait ``seconds``; False as soon as a Stop took the slot."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        if stopped():
            return False
        time.sleep(min(_POLL_SEC, max(0.0, deadline - time.time())))
    return not stopped()


def _wait_for_settle(jsonl, stopped) -> str:
    """How the wait ended: ``settled`` (the transcript was written after the
    send and then stayed quiet for _SETTLE_SEC), ``waited`` (no transcript to
    watch: the CLI got _NO_WATCH_WAIT_SEC), ``cap`` or ``stopped``. Only the
    caller's evidence says whether it compacted."""
    sent = time.time()
    deadline = sent + _HARD_CAP_SEC
    if jsonl is None:
        return "waited" if _sleep(_NO_WATCH_WAIT_SEC, stopped) else "stopped"
    advanced = False
    stable_since: float | None = None
    last_mtime = jsonl.stat().st_mtime if jsonl.exists() else 0.0
    while time.time() < deadline:
        if stopped():
            return "stopped"
        time.sleep(_POLL_SEC)
        m = jsonl.stat().st_mtime if jsonl.exists() else last_mtime
        if m > sent:
            advanced = True
        if advanced and m == last_mtime:
            if stable_since is None:
                stable_since = time.time()
            elif time.time() - stable_since >= _SETTLE_SEC:
                return "settled"
        else:
            stable_since = None
        last_mtime = m
    return "cap"
