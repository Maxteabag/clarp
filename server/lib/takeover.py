"""A turn that has produced nothing, taken over by the user's next message (issue 491, part B).

Only for a backend with its own recording of the steps below: Claude, and agy (one recorded
trial of a seeded resume, so a guard: take over only while message 1 is not yet in the
conversation's transcript, judged while the turn is frozen; ``agy_plan``). Codex runs on one
app-server that serves every Codex agent, which may not be frozen; its follow-up is steered
instead. grok, muse, OpenCode and DeepSeek wait for their own recordings; their follow-up waits
(part A).

The order is FREEZE, JUDGE, END, never "judge, then signal" (recording case 5: a SIGTERM to
a Claude turn whose request is in flight lets it print the whole answer after the signal):

1. a durable record of the group about to be frozen (``record_freeze``), so a Host that dies
   while the group is stopped leaves the next runtime what to thaw (``thaw_left_frozen``);
2. SIGSTOP to the turn's process group (the CLI, its tool children and its MCP servers: one
   group, ``start_new_session``), so it can print nothing more;
3. the drain reads what was already in the pipe (``drained``);
4. the judge: nothing printed (no word, no tool call; thinking alone is nothing) and the
   session file allows the resume (``session_facts``);
5. SIGKILL while it is still stopped (recording case 7: nothing more is printed and the
   session file holds message 1 with no answer), or SIGCONT and the turn runs on.
"""
from __future__ import annotations

import fcntl
import functools
import json
import os
import pathlib
import re
import shutil
import signal
import struct
import subprocess
import termios
import time
from typing import Any

from . import settings_store
from .clock import now_ms
from .log import log, log_exception

# The resume pair (--resume-session-at / --resume-drops-turn) exists from this version on.
MIN_CLAUDE_VERSION = (2, 1, 223)
# Antigravity: the version of the one recorded seeded resume after a pre-stdout kill.
MIN_AGY_VERSION = (1, 5, 13)
# How long the drain may take to read what the frozen CLI had printed.
DRAIN_BOUND_S = 0.5
_DRAIN_POLL_S = 0.02
# The outcome of a turn that a follow-up replaced: not stopped by the user, not failed.
REPLACED = "replaced"
_RECORD_PREFIX = "takeover.frozen."


def _cli_version(executable: str) -> tuple[int, ...] | None:
    """The installed CLI's version, or None when it is not found or does not say. The
    binary is resolved on PATH first (nothing is run when it is not there) and the answer
    is cached per binary and modification time, as the thinking-display probe does."""
    path = shutil.which(executable) or ""
    if not path:
        return None
    try:
        stamp = os.stat(path).st_mtime
    except OSError:
        return None
    return _cli_version_of(path, stamp)


@functools.lru_cache(maxsize=8)
def _cli_version_of(path: str, stamp: float) -> tuple[int, ...] | None:
    try:
        out = subprocess.run([path, "--version"], capture_output=True, text=True,
                             timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", out or "")
    return tuple(int(part) for part in match.groups()) if match else None


_cli_version.cache_clear = _cli_version_of.cache_clear     # the tests reset the cache


def claude_version_ok(executable: str) -> bool:
    """The installed Claude Code knows the resume pair. Unknown: no (the follow-up waits)."""
    version = _cli_version(executable)
    return version is not None and version >= MIN_CLAUDE_VERSION


def agy_version_ok(executable: str) -> bool:
    """The installed agy is at least the recorded one. Unknown: no (the follow-up waits)."""
    version = _cli_version(executable)
    return version is not None and version >= MIN_AGY_VERSION


def agy_plan(turns: list[dict] | None, first_text: str) -> dict | None:
    """agy's judge (one recorded trial, so a guard makes it enough): take over ONLY when
    the conversation's transcript was read (``turns``) and message 1 is NOT in it, as
    recorded after a pre-stdout hold and kill. Message 1 there, or nothing read: None."""
    if turns is None or not first_text.strip():
        return None
    words = first_text.strip()
    if any(turn.get("role") == "user" and words in str(turn.get("text") or "")
           for turn in turns):
        return None
    return {"first": first_text}


# --- the durable record ------------------------------------------------------------------

def _start_ticks(pid: int) -> int | None:
    """The process's start time (/proc/<pid>/stat field 22): a recycled pid is not it."""
    try:
        stat = pathlib.Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    try:
        return int(stat.rsplit(")", 1)[1].split()[19])
    except (IndexError, ValueError):
        return None


def record_freeze(*, agent_id: str, trace_id: str, pgid: int, pid: int) -> str:
    """Written BEFORE the SIGSTOP: a crash after it leaves this record for the next runtime."""
    key = f"{_RECORD_PREFIX}{agent_id}"
    settings_store.set_text(key, json.dumps({
        "agent_id": agent_id, "trace_id": trace_id, "pgid": int(pgid), "pid": int(pid),
        "start_ticks": _start_ticks(pid), "at": now_ms()}))
    return key


def clear_freeze(key: str) -> None:
    try:
        settings_store.delete(key)
    except Exception as exc:  # noqa: BLE001 - the next runtime thaws it again (a no-op)
        log_exception("takeoverRecordClearFail", exc, detail=key)


def thaw_left_frozen() -> int:
    """At a runtime's start: SIGCONT every group a dead Host left frozen. A group whose
    leader is gone, or whose pid now names another process, is only forgotten."""
    thawed = 0
    for key in settings_store.keys_with_prefix(_RECORD_PREFIX):
        raw = settings_store.get(key) or ""
        try:
            record = json.loads(raw)
            pgid, pid = int(record["pgid"]), int(record["pid"])
        except (ValueError, KeyError, TypeError):
            clear_freeze(key)
            continue
        ticks = _start_ticks(pid)
        if ticks is not None and ticks == record.get("start_ticks"):
            try:
                os.killpg(pgid, signal.SIGCONT)
                thawed += 1
                log("takeoverThawedAfterRestart",
                    f"agent={record.get('agent_id')} trace={record.get('trace_id')} pgid={pgid}")
            except ProcessLookupError:
                pass
        clear_freeze(key)
    return thawed


# --- freeze, drain, judge, end --------------------------------------------------------------

def freeze(handle) -> bool:
    try:
        os.killpg(handle.process_group, signal.SIGSTOP)
        return True
    except ProcessLookupError:
        return False


def thaw(handle) -> None:
    try:
        os.killpg(handle.process_group, signal.SIGCONT)
    except ProcessLookupError:
        pass


def end_frozen(handle) -> None:
    """SIGKILL while still stopped: a stopped process takes SIGKILL and prints nothing more."""
    try:
        os.killpg(handle.process_group, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _unread_bytes(stream) -> int | None:
    try:
        buf = fcntl.ioctl(stream.fileno(), termios.FIONREAD, struct.pack("i", 0))
        return struct.unpack("i", buf)[0]
    except (OSError, ValueError, AttributeError):
        return None


def drained(handle, *, bound_s: float = DRAIN_BOUND_S) -> bool:
    """True once the drain has read everything the frozen CLI had printed: the pipe is empty
    and the drain's count of lines read has stopped moving. False when that is not seen
    within ``bound_s`` (then the turn is thawed and the follow-up waits)."""
    stream = getattr(handle.proc, "stdout", None)
    if stream is None:
        return False
    deadline = time.monotonic() + bound_s
    last = -1
    quiet = 0
    while time.monotonic() < deadline:
        unread = _unread_bytes(stream)
        if unread is None:
            return False
        lines = int(getattr(handle, "lines_read", 0))
        if unread == 0 and lines == last:
            quiet += 1
            if quiet >= 3:
                return True
        else:
            quiet = 0
        last = lines
        time.sleep(_DRAIN_POLL_S)
    return False


def session_facts(path: pathlib.Path | None, drops_turn: str) -> dict[str, Any]:
    """What the session file allows. ``found``: the entry of message 1 (uuid ``drops_turn``) is
    there; ``resume_at``: the uuid of the last user or assistant entry before it (none: the
    session's first turn); ``answered``: an assistant entry follows it (then nothing is taken
    over, whatever the stream said); ``readable``."""
    facts: dict[str, Any] = {"readable": False, "found": False, "resume_at": "", "answered": False}
    if path is None:
        return facts
    try:
        lines = pathlib.Path(path).read_text(errors="replace").splitlines()
    except OSError:
        return facts
    facts["readable"] = True
    last_before = ""
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue                      # a half line a killed write left: harmless (case 7)
        if not isinstance(entry, dict):
            continue
        kind, uid = entry.get("type"), str(entry.get("uuid") or "")
        if facts["found"]:
            if kind == "assistant":
                facts["answered"] = True
            continue
        if kind == "user" and uid == drops_turn:
            facts["found"] = True
            facts["resume_at"] = last_before
            continue
        if kind in ("user", "assistant") and uid:
            last_before = uid
    if not facts["found"]:
        # Message 1 never reached the file (frozen before it was written): the resume pair is
        # accepted all the same (case 7, before-init), at the file's last entry.
        facts["resume_at"] = last_before
    return facts


def takeover_plan(facts: dict[str, Any], drops_turn: str, first_text: str) -> dict | None:
    """The launch of the turn that takes over, or None (no takeover; the follow-up waits).

    - A resumable point before message 1: drop message 1 and give both as one line.
    - No point before it (the session's first turn) and message 1 in the file: a plain resume
      with message 2 alone (case 7, first-plain).
    - Anything else (unreadable, answered, a first turn not yet in the file): None."""
    if not facts.get("readable") or facts.get("answered"):
        return None
    if facts.get("resume_at"):
        return {"first": first_text, "resume_at": facts["resume_at"], "drops_turn": drops_turn}
    if facts.get("found"):
        return {"first": first_text, "resume_at": "", "drops_turn": drops_turn}
    return None
