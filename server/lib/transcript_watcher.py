"""Per-agent transcript tail.

Tails Claude Code transcript JSONL files. When a new
{type:"assistant"} entry with text content lands, fires a callback.
The callback typically enqueues the text into tts_queue (the same Phase A
queue the Stop hook uses), so text-input streaming reuses the existing
producer→worker→delivery pipeline.

Two layers:

  * `TranscriptWatcher` — pure read+parse logic. `tick()` consumes
    available bytes, emits one callback per new text block. Easy to
    unit-test: call tick() directly, no event loop or threads.
  * `InotifyDispatcher` — push-driven scheduler. Subscribes the watcher
    paths to inotify(7), wakes up the instant Claude Code writes to a
    transcript file, and calls the matching watcher's tick(). Zero CPU
    when idle, sub-millisecond wakeup latency.

Offset state persists in `cursor_positions.position` keyed by
backend_session_id, so a server restart doesn't re-emit text we already
processed before the crash.
"""
from __future__ import annotations

import json
import pathlib
import sys
import time
from typing import Callable, Iterable

from .log import log, log_exception

# A backend streams a reply as many small appends. Gathering a burst's events
# for this long before ticking imports each agent once per burst instead of
# once per append; every import re-reads the whole transcript.
INOTIFY_COALESCE_SEC = 0.1
# While a cursor save is pending (it failed, for example on 'database is
# locked'), the dispatcher wakes this often to retry it even if the transcript
# stays quiet: a stale persisted cursor makes a restart re-read old text (TW-01).
SAVE_RETRY_SEC = 5.0
# After the first failure (logged with its traceback), at most one line this often.
SAVE_FAIL_LOG_EVERY_SEC = 3600.0

TextHandler = Callable[[str], None]


# ---- Single-file watcher -----------------------------------------------


class TranscriptWatcher:
    """Tail one JSONL file. Stateful — keeps the byte offset between ticks.

    Construction is cheap and side-effect-free. Call `tick()` periodically
    (or from a scheduler thread) to advance the offset and emit new
    text blocks via `on_text`.
    """

    def __init__(self, path: pathlib.Path, *, on_text: TextHandler,
                 start_offset: int = 0,
                 start_at_end: bool = False,
                 backend_session_id: str | None = None,
                 persist_offset: bool = False,
                 on_change: Callable[[], None] | None = None,
                 pass_line_timestamp: bool = False):
        self.path = pathlib.Path(path)
        self.on_text = on_text
        # When set, on_text(text, line_ts) also gets the line's own "timestamp"
        # as written (or None). Diagnostics only: it is the producer's clock.
        self.pass_line_timestamp = pass_line_timestamp
        self.on_change = on_change       # fires once per tick that consumed bytes,
                                          # regardless of what JSON type they were
        self.byte_offset = start_offset
        self._partial = b""              # leftover bytes from a partial JSONL line
        self._first_tick = True
        self._start_at_end = start_at_end
        self.backend_session_id = backend_session_id
        self.persist_offset = persist_offset
        self._save_pending = False
        self._save_fail_logged_at: float | None = None
        # If start_at_end, snapshot the size NOW so any writes after this
        # point are considered "new." Otherwise the snapshot at first
        # tick would include writes that landed between construction and
        # the first inotify wakeup — and we'd skip them as 'history'.
        if start_at_end and start_offset == 0:
            try:
                self.byte_offset = self.path.stat().st_size
            except OSError:
                self.byte_offset = 0

    def tick(self) -> int:
        """Read new bytes, parse new JSONL lines, fire on_text for each new
        text block. Returns the number of text blocks emitted this tick.

        Offset model:
          * `byte_offset` = bytes we've fully CONSUMED (complete lines
            successfully parsed). Persisted to disk for restart safety.
          * `_partial`    = trailing bytes we've READ but not yet
            consumed (a half-written JSONL line). Held in memory; not
            persisted, because on restart the new watcher will simply
            re-read those bytes from the file.

        Disk read starts at `byte_offset + len(_partial)`, so we don't
        double-read the partial chunk on the next tick.
        """
        if self._save_pending:
            self._save_offset()
        try:
            size = self.path.stat().st_size
        except FileNotFoundError:
            return 0
        except OSError:
            return 0

        self._first_tick = False

        read_start = self.byte_offset + len(self._partial)
        if size <= read_start:
            return 0

        try:
            with self.path.open("rb") as f:
                f.seek(read_start)
                new_bytes = f.read(size - read_start)
        except OSError:
            return 0

        buf = self._partial + new_bytes
        lines = buf.split(b"\n")
        self._partial = lines[-1]                    # may be b"" if file ended on \n
        complete = lines[:-1]

        consumed = sum(len(l) for l in complete) + len(complete)
        self.byte_offset += consumed
        self._save_offset()

        emitted = 0
        for raw in complete:
            texts, line_ts = _texts_and_timestamp(raw)
            for text in texts:
                if self.pass_line_timestamp:
                    self.on_text(text, line_ts)
                else:
                    self.on_text(text)
                emitted += 1
        # Fire the generic change callback ONCE per tick that consumed
        # bytes — used by the history-update SSE broadcast so the client
        # refetches /log when ANY transcript activity lands (tool calls
        # and tool results, not just speakable text).
        if consumed > 0 and self.on_change is not None:
            try:
                self.on_change()
            except Exception:
                pass
        return emitted

    # ---- offset persistence ------------------------------------------

    @property
    def save_pending(self) -> bool:
        """The persisted cursor is behind the in-memory one (a save failed)."""
        return self._save_pending

    def retry_save(self) -> None:
        if self._save_pending:
            self._save_offset()

    def _save_offset(self) -> None:
        if not (self.persist_offset and self.backend_session_id):
            return
        try:
            from . import db
            db.conn().execute(
                """INSERT INTO cursor_positions
                       (backend_session_id, position, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(backend_session_id) DO UPDATE SET
                     position = excluded.position,
                     updated_at = excluded.updated_at""",
                (self.backend_session_id, self.byte_offset, db.now_ms()),
            )
        except Exception as exc:  # noqa: BLE001 - the watcher must never crash on a DB hiccup
            # Retried on the next tick (and by the dispatcher while idle).
            self._save_pending = True
            now = time.monotonic()
            if self._save_fail_logged_at is None:
                self._save_fail_logged_at = now
                log_exception("transcriptCursorSaveFail", exc,
                              detail=self.backend_session_id)
            elif now - self._save_fail_logged_at >= SAVE_FAIL_LOG_EVERY_SEC:
                self._save_fail_logged_at = now
                log("transcriptCursorSaveFail",
                    f"session={self.backend_session_id} still failing: "
                    f"{type(exc).__name__}: {exc}")
            return
        self._save_pending = False
        self._save_fail_logged_at = None


# ---- JSONL parsing helpers ---------------------------------------------


def _texts_and_timestamp(raw: bytes) -> tuple[list[str], str | None]:
    """Every assistant text block in one JSONL line, with the line's own
    ``timestamp`` string when it has one."""
    line = raw.strip()
    if not line:
        return [], None
    try:
        entry = json.loads(line)
    except json.JSONDecodeError:
        return [], None
    if not isinstance(entry, dict):
        return [], None
    stamp = entry.get("timestamp")
    return list(_texts_in_entry(entry)), stamp if isinstance(stamp, str) else None


def _texts_in_line(raw: bytes) -> Iterable[str]:
    """Yield every assistant text block in one JSONL line. Skips thinking
    blocks, tool_use blocks, user entries, malformed JSON, and empty
    strings."""
    yield from _texts_and_timestamp(raw)[0]


def _texts_in_entry(entry: dict) -> Iterable[str]:
    if entry.get("type") != "assistant":
        return
    msg = entry.get("message") or {}
    content = msg.get("content")
    if isinstance(content, str):
        text = content.strip()
        if text:
            yield text
        return
    if not isinstance(content, list):
        return
    for c in content:
        if not isinstance(c, dict):
            continue
        # ONLY type=='text' is voiceable. Skip thinking, tool_use,
        # tool_result, image, etc.
        if c.get("type") != "text":
            continue
        text = (c.get("text") or "").strip()
        if text:
            yield text


# ---- Multi-agent pool ---------------------------------------------------


class WatcherPool:
    """Holds one watcher per agent. The server's scheduler thread calls
    tick_all() at the polling interval; per-agent watchers fire their
    callbacks independently."""

    def __init__(self) -> None:
        self._watchers: dict[str, TranscriptWatcher] = {}

    def add(self, agent_id: str, watcher: TranscriptWatcher) -> None:
        self._watchers[agent_id] = watcher

    def remove(self, agent_id: str) -> None:
        self._watchers.pop(agent_id, None)

    def has(self, agent_id: str) -> bool:
        return agent_id in self._watchers

    def tick_all(self) -> dict[str, int]:
        """Tick every watcher. Returns {agent_id: blocks_emitted} for
        the agents that emitted at least one block this tick."""
        emitted: dict[str, int] = {}
        for agent_id, w in list(self._watchers.items()):
            try:
                n = w.tick()
                if n:
                    emitted[agent_id] = n
            except Exception:
                # One watcher's failure must not stop the others.
                pass
        return emitted

    def agent_ids(self) -> list[str]:
        return list(self._watchers.keys())

    def watcher_for(self, agent_id: str) -> TranscriptWatcher | None:
        return self._watchers.get(agent_id)

    def pending_saves(self) -> list[TranscriptWatcher]:
        """Watchers whose persisted cursor is behind (a save failed)."""
        return [w for w in list(self._watchers.values()) if w.save_pending]


# ---- inotify-driven dispatcher -----------------------------------------


class InotifyDispatcher:
    """Push-driven scheduler over a WatcherPool.

    inotify(7) wakes up the moment a watched file is modified. We map
    the inotify watch descriptor → agent_id, then call that watcher's
    tick(). Zero CPU when no agent is writing.

    Sits behind a thread because inotify_simple's read() blocks. The
    server holds one InotifyDispatcher; its start() spawns a daemon
    thread, stop() drains via a self-pipe.

    Linux-only by design. The TranscriptWatcher logic above is the same
    primitive that polling-based or kqueue-based drivers would call;
    everything outside this class is platform-agnostic.
    """
    def __init__(self, pool: WatcherPool):
        self.pool = pool
        self._inotify = None
        self._wd_to_agent: dict[int, str] = {}
        self._agent_to_wd: dict[str, int] = {}
        self._thread = None
        self._stop_pipe_r: int | None = None
        self._stop_pipe_w: int | None = None
        self._stop = False
        self._polling = sys.platform != "linux"
        self._polling_agents: set[str] = set()

    def watch(self, agent_id: str, path: pathlib.Path) -> None:
        """Subscribe an agent's transcript file for write events.
        Idempotent — re-watching the same agent rebinds to the new path."""
        path = pathlib.Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(exist_ok=True)
        if self._polling:
            self._polling_agents.add(agent_id)
            self._wake()
            return
        import inotify_simple
        # Initialize the inotify FD lazily but ALSO eagerly at start() so
        # the loop thread can select() on it from the beginning. Either
        # path is fine.
        if self._inotify is None:
            self._inotify = inotify_simple.INotify()
            # Wake the loop so it picks up the new fileno() to select on.
            self._wake()
        # Tear down any prior watch for this agent so we don't double-fire.
        self.unwatch(agent_id)
        # inotify needs the file to exist; it was created above.
        flags = (inotify_simple.flags.MODIFY
                 | inotify_simple.flags.CLOSE_WRITE
                 | inotify_simple.flags.DELETE_SELF
                 | inotify_simple.flags.MOVE_SELF)
        wd = self._inotify.add_watch(str(path), flags)
        self._wd_to_agent[wd] = agent_id
        self._agent_to_wd[agent_id] = wd

    def _wake(self) -> None:
        """Write one byte into the self-pipe so the loop's select returns
        and re-evaluates state. Idempotent; safe to call from any thread."""
        import os
        if self._stop_pipe_w is not None:
            try: os.write(self._stop_pipe_w, b".")
            except OSError: pass

    def unwatch(self, agent_id: str) -> None:
        self._polling_agents.discard(agent_id)
        wd = self._agent_to_wd.pop(agent_id, None)
        if wd is None:
            return
        self._wd_to_agent.pop(wd, None)
        if self._inotify is not None:
            try:
                self._inotify.rm_watch(wd)
            except OSError:
                pass

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        import os, threading
        self._stop = False
        self._stop_pipe_r, self._stop_pipe_w = os.pipe()
        self._thread = threading.Thread(
            target=self._loop, daemon=True,
            name="transcript-poll" if self._polling else "transcript-inotify")
        self._thread.start()

    def stop(self, timeout: float = 1.0) -> None:
        import os
        self._stop = True
        if self._stop_pipe_w is not None:
            try: os.write(self._stop_pipe_w, b"x")
            except OSError: pass
        if self._thread:
            self._thread.join(timeout=timeout)
        for fd in (self._stop_pipe_r, self._stop_pipe_w):
            if fd is not None:
                try: os.close(fd)
                except OSError: pass
        self._stop_pipe_r = self._stop_pipe_w = None
        if self._inotify is not None:
            try: self._inotify.close()
            except OSError: pass
            self._inotify = None

    @staticmethod
    def _wait(fds: list[int], timeout: float | None) -> set[int]:
        """The fds in ``fds`` that are readable within ``timeout`` seconds (None: wait).
        poll(2), not select(2): select() rejects any fd >= FD_SETSIZE (1024), and a
        process with many open files hands the dispatcher fds above that."""
        import errno, select
        poller = select.poll()
        for fd in fds:
            poller.register(fd, select.POLLIN)
        events = poller.poll(None if timeout is None else int(timeout * 1000))
        if any(ev & select.POLLNVAL for _fd, ev in events):
            raise OSError(errno.EBADF, "a watched fd is not open")
        return {fd for fd, _ev in events}

    def _wait_failed(self, exc: BaseException, fds: list[int]) -> None:
        # Closing the fds is how stop() ends a wait; any other failure must not end
        # the thread silently, or file changes stop arriving with no trace.
        if not self._stop:
            log_exception("transcriptWatcherWaitFail", exc, detail=f"fds={len(fds)}")

    def _pending_saves(self) -> list:
        """Watchers with an unsaved cursor; never ends the loop (a pool
        without the method, or one that raises, counts as none)."""
        pending = getattr(self.pool, "pending_saves", None)
        if pending is None:
            return []
        try:
            return list(pending())
        except Exception as exc:  # noqa: BLE001 - the loop must keep delivering events
            log_exception("transcriptWatcherPendingSavesFail", exc)
            return []

    def _loop(self) -> None:
        import os
        # Tick once on startup so any data that landed between watch() and
        # the first inotify event still gets consumed.
        self.pool.tick_all()
        while not self._stop:
            fds = [self._stop_pipe_r]
            if self._inotify is not None:
                fds.append(self._inotify.fileno())
            pending = bool(self._pending_saves())
            try:
                ready = self._wait(fds, 0.25 if self._polling else
                                   (SAVE_RETRY_SEC if pending else None))
            except (OSError, ValueError, TypeError) as exc:
                self._wait_failed(exc, fds)
                return
            if self._stop:
                return
            # Drain wake bytes (idempotent — we ignore them; their purpose
            # is just to unblock the select).
            if self._stop_pipe_r in ready:
                try: os.read(self._stop_pipe_r, 1024)
                except OSError: pass
                if self._stop:
                    return
            if self._polling:
                self.pool.tick_all()
                continue
            for w in self._pending_saves():
                w.retry_save()          # a quiet transcript still catches up (TW-01)
            if self._inotify is None or self._inotify.fileno() not in ready:
                continue
            # Let the rest of the burst land; a stop request still ends the wait.
            try:
                self._wait([self._stop_pipe_r], INOTIFY_COALESCE_SEC)
            except (OSError, ValueError, TypeError) as exc:
                self._wait_failed(exc, [self._stop_pipe_r])
                return
            if self._stop:
                return
            try:
                events = self._inotify.read(timeout=0)
            except OSError:
                continue
            # Dedupe: many MODIFY events may fire for one logical write.
            # Tick each affected agent exactly once per drain.
            touched: set[str] = set()
            for ev in events:
                aid = self._wd_to_agent.get(ev.wd)
                if aid:
                    touched.add(aid)
            for aid in touched:
                w = self.pool.watcher_for(aid)
                if w is None:
                    continue
                try:
                    w.tick()
                except Exception as exc:  # noqa: BLE001 - one agent must not stop the rest
                    log_exception("transcriptWatcherTickFail", exc, detail=aid)
