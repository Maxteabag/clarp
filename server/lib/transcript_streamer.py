"""Lifecycle glue: DB agent state ↔ InotifyDispatcher.

Watches the agents table for changes (via the existing StateLogWatcher's
tick cadence, plus a periodic reconcile). For each agent that has a live
runtime with a backend_session_id UUID, subscribes a TranscriptWatcher to
the corresponding ~/.claude/projects/.../<uuid>.jsonl. When that file
gains new assistant text, scans for <speak>...</speak> regions and
enqueues ONLY those in tts_queue — running commentary stays silent so
the user isn't drowned in "I'll now check the file" narration.

Net effect: progressive TTS for explicitly-tagged voice content. Each
time Claude finishes a <speak> block, audio synthesis starts — without
waiting for the Stop hook, and without speaking every intermediate
chatter line.
"""
from __future__ import annotations

import datetime
import itertools
import re
import threading
import time
from dataclasses import dataclass, replace
from typing import Callable

from . import agents as agents_db
from . import backends, events
from . import health
from . import transcript_import_cache, transcript_incremental
from . import tts_queue
from .clock import now_ms
from .log import log_exception
from .protocol import TurnSource
from .transcript_watcher import (
    InotifyDispatcher, TranscriptWatcher, WatcherPool,
)
from .voice_markup import spoken_chunks_for_tts

# Tag the agent wraps voice-bound content in. Everything outside the
# tag is silent (still visible in the history pane, just not spoken).
# Non-greedy + DOTALL so a multi-line block is captured wholesale, and
# multiple blocks in one text payload each fire their own enqueue.
_SPEAK_RE = re.compile(r"<speak>(.*?)</speak>", re.DOTALL | re.IGNORECASE)


def _emit(*a, **kw):
    try:
        from . import eventlog
        eventlog.emit(*a, **kw)
    except Exception:
        pass


# Bind generations come from one process-wide counter that is never reset, so an
# unbind and rebind (even to the same backend session) always yields a larger one.
_BIND_GENERATIONS = itertools.count(1)


@dataclass(frozen=True)
class Intent:
    """The latest transcript change of a bound agent whose post-import step has
    not run yet. ``resubmits``/``next_at`` pace the reconcile retries."""
    bsid: str
    gen: int
    seq: int
    resubmits: int = 0
    next_at: float = 0.0


# How often to scan the agents table for new backend_session_id bindings.
# inotify gives us instant file-modify wakeups; this is just for the
# bookkeeping of "an agent's backend_session_id just became known."
RECONCILE_INTERVAL_SEC = 1.0


class TranscriptStreamer:
    """Owns a WatcherPool + InotifyDispatcher and keeps them in sync with
    the DB. Started once at server startup, stopped at shutdown."""

    def __init__(self, *, enqueue: Callable[..., int] | None = None,
                 reconcile_interval_sec: float = RECONCILE_INTERVAL_SEC,
                 stream=None):
        self.pool = WatcherPool()
        self.dispatcher = InotifyDispatcher(self.pool)
        self._reconcile_interval = reconcile_interval_sec
        self._enqueue = enqueue or tts_queue.enqueue
        # Optional SSE stream — when set, the streamer broadcasts a
        # `transcript-updated` event per tick that consumed bytes, so the
        # client can refetch /log and refresh the history pane live.
        self._stream = stream
        # agent_id → backend_session_id currently bound to a watcher. Used to
        # detect changes (resume, fork) without re-watching every tick.
        self._bound: dict[str, str] = {}
        # agent_id → its generation at the latest bind (never removed).
        self._bind_generation: dict[str, int] = {}
        # agent_id → the latest change still waiting for its post-import step.
        # Every read-modify-write holds _dirty_lock (never across import or I/O).
        self._dirty: dict[str, Intent] = {}
        self._seq: dict[str, int] = {}
        self._dirty_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ---- per-agent subscribe ----------------------------------------

    def reconcile_once(self) -> None:
        """Scan agents + runtimes; subscribe/unsubscribe watchers to match."""
        live = agents_db.list_agents()
        sessions = _live_backend_sessions()
        live_ids = set()
        for agent in live:
            agent_id = agent["agent_id"]
            live_ids.add(agent_id)
            if not backends.capabilities(agent.get("backend")).supports_transcript_streaming:
                self._unbind(agent_id)
                continue
            backend_session_id = sessions.get(agent_id)
            if not backend_session_id:
                # Agent exists but hasn't received its first prompt yet.
                # Nothing to watch.
                self._unbind(agent_id)
                continue
            if self._bound.get(agent_id) == backend_session_id:
                continue                    # already correctly bound
            self._bind(agent, backend_session_id)
        # Agents that disappeared (soft-deleted): drop their watchers.
        for stale in set(self._bound) - live_ids:
            self._unbind(stale)
        self._resubmit_unresolved()
        health.mark_success("transcript_streamer")

    def _bind(self, agent: dict, backend_session_id: str) -> None:
        agent_id = agent["agent_id"]
        path = backends.by_id(agent.get("backend")).find_transcript(backend_session_id)
        if path is None:
            # Transcript file doesn't exist yet — try again next reconcile.
            return
        self._unbind(agent_id)
        # New watcher: skip whatever's already in the file. Agents
        # mid-conversation shouldn't replay history.
        watcher = TranscriptWatcher(
            path,
            on_text=lambda text, line_ts, a=agent: self._on_text(a, text, line_ts=line_ts),
            on_change=lambda a=agent, cs=backend_session_id: self._on_change(a, cs),
            start_at_end=True,
            backend_session_id=backend_session_id,
            persist_offset=True,
            pass_line_timestamp=True,
        )
        self.pool.add(agent_id, watcher)
        try:
            self.dispatcher.watch(agent_id, path)
        except OSError as e:
            log_exception("transcriptStreamerWatchFail", e,
                          detail=str(path))
            self.pool.remove(agent_id)
            return
        self._bound[agent_id] = backend_session_id
        self._bind_generation[agent_id] = next(_BIND_GENERATIONS)
        _emit("transcript_streamer", "bind",
              session=agent.get("session"),
              backend_session_id=backend_session_id,
              detail={"agent_id": agent_id, "path": str(path),
                      "persona": agent.get("persona")})

    def _unbind(self, agent_id: str) -> None:
        if agent_id not in self._bound:
            return
        self.dispatcher.unwatch(agent_id)
        self.pool.remove(agent_id)
        prev = self._bound.pop(agent_id, None)
        with self._dirty_lock:
            self._dirty.pop(agent_id, None)
        _emit("transcript_streamer", "unbind",
              backend_session_id=prev,
              detail={"agent_id": agent_id})

    # ---- transcript change → SSE -------------------------------------

    def _on_change(self, agent: dict, backend_session_id: str) -> None:
        """Fires once per tick that consumed bytes — including tool_use
        and tool_result lines that don't go through on_text. Drives the
        client's live-update of the history pane via SSE.

        Runs on the one inotify thread that voices every agent, so it only
        broadcasts and queues: the transcript import (it re-reads the whole
        file) and the notification retry that needs its rows run on the import
        worker, and the change stays pending until that step has run."""
        if self._stream is not None:
            try:
                events.broadcast(self._stream, events.transcript_updated(
                    agent_id=agent["agent_id"],
                    session=agent.get("session"),
                    backend_session_id=backend_session_id,
                ))
            except Exception as e:
                log_exception("transcriptStreamerBroadcastFail", e,
                              detail=agent.get("agent_id"))
        self._submit(agent, self._note_change(agent, backend_session_id))

    def _note_change(self, agent: dict, backend_session_id: str) -> Intent:
        """Record this change as the agent's latest pending one."""
        agent_id = agent["agent_id"]
        with self._dirty_lock:
            seq = self._seq.get(agent_id, 0) + 1
            self._seq[agent_id] = seq
            intent = Intent(backend_session_id, self._bind_generation.get(agent_id, 0), seq,
                            next_at=time.monotonic() + 1.0)
            self._dirty[agent_id] = intent
        return intent

    def _submit(self, agent: dict, intent: Intent) -> bool:
        """Queue the import and its post-import step on the import worker.
        False when refused; the intent stays pending either way."""
        agent_id = agent["agent_id"]
        try:
            backend = backends.by_id(agent.get("backend"))
            latest = backend.find_transcript(intent.bsid)
            if latest is None:
                return False

            def import_latest() -> None:
                transcript_incremental.import_transcript(
                    backend=backend.id, path=latest,
                    agent_id=agent_id,
                    backend_session_id=intent.bsid,
                    parse=backend.parse_transcript)

            return transcript_import_cache.schedule_import(
                latest, import_latest, owner=agent_id,
                after=lambda: self._after_import(agent_id, intent.bsid, intent.gen, intent.seq))
        except Exception as e:
            log_exception("transcriptStreamerImportFail", e, detail=agent_id)
            return False

    def _resubmit_unresolved(self) -> None:
        """Reconcile's retry of pending changes the worker no longer holds
        (refused, or the import failed), paced 1, 2, 4 … 60 s per intent."""
        now = time.monotonic()
        with self._dirty_lock:
            due = [(agent_id, intent) for agent_id, intent in self._dirty.items()
                   if self._bound.get(agent_id) == intent.bsid
                   and self._bind_generation.get(agent_id) == intent.gen
                   and now >= intent.next_at]
        for agent_id, intent in due:
            agent = agents_db.get_by_agent_id(agent_id)
            if agent is None:
                continue
            try:
                latest = backends.by_id(agent.get("backend")).find_transcript(intent.bsid)
            except Exception as e:
                log_exception("transcriptStreamerImportFail", e, detail=agent_id)
                continue
            if latest is None or transcript_import_cache.is_active(latest, owner=agent_id):
                continue
            self._submit(agent, intent)
            with self._dirty_lock:
                if self._dirty.get(agent_id) is intent:
                    resubmits = intent.resubmits + 1
                    self._dirty[agent_id] = replace(
                        intent, resubmits=resubmits, next_at=now + min(60, 2 ** resubmits))

    def _admit(self, agent_id: str, backend_session_id: str, generation: int) -> bool:
        """Whether the binding a post-import step was queued for is current."""
        return (not self._stop.is_set()
                and self._bound.get(agent_id) == backend_session_id
                and self._bind_generation.get(agent_id) == generation)

    def _after_import(self, agent_id: str, backend_session_id: str,
                      generation: int, seq: int) -> None:
        """On the import worker, after the import returned: tell clients the
        imported rows are there, and retry recently suppressed notifications.
        Nothing is admitted for a binding that is no longer current; its own
        pending change is cleared in every case, a newer one never."""
        try:
            if not self._admit(agent_id, backend_session_id, generation) or self._stream is None:
                return
            try:
                agent = agents_db.get_by_agent_id(agent_id)
                events.broadcast(self._stream, events.transcript_updated(
                    agent_id=agent_id,
                    session=(agent or {}).get("session"),
                    backend_session_id=backend_session_id,
                ))
            except Exception as e:
                log_exception("transcriptStreamerBroadcastFail", e, detail=agent_id)
            try:
                from . import apns, user_notifications
                admit = lambda: self._admit(agent_id, backend_session_id, generation)  # noqa: E731
                for notification in user_notifications.reclassify_recent_suppressed(
                    agent_id=agent_id,
                    backend_session_id=backend_session_id,
                    admit=admit,
                ):
                    if not admit():
                        break
                    events.broadcast(self._stream, events.as_event(
                        user_notifications.event_payload(notification)))
                    apns.on_user_notification(notification)
            except Exception as e:
                log_exception("transcriptStreamerNotificationRetryFail", e, detail=agent_id)
        finally:
            with self._dirty_lock:
                current = self._dirty.get(agent_id)
                if current is not None and (current.bsid, current.gen, current.seq) == (
                        backend_session_id, generation, seq):
                    del self._dirty[agent_id]

    # ---- text-block → tts_queue ----------------------------------------

    def _on_text(self, agent: dict, text: str, *, line_ts: str | None = None) -> None:
        """Called by the watcher when a new assistant text block lands.
        Extract <speak>...</speak> regions and enqueue each one as its
        own TTS clip. Untagged text is silent — it still streams to the
        history pane via the on_change path, but the voice channel only
        carries what Claude explicitly marked as speakable."""
        agent_id = agent["agent_id"]
        # Live state lookup — agent may have been deleted between watcher
        # callbacks; the soft-delete column doesn't propagate to existing
        # watcher closures.
        a = agents_db.get_by_agent_id(agent_id)
        if a is None:
            self._unbind(agent_id)
            return

        # Source check: only voice 'pwa' turns. The latest turn row tells
        # us whether the user is at the phone or the laptop right now.
        source = agents_db.latest_turn_source(agent_id) or TurnSource.LOCAL
        if source != TurnSource.PWA:
            return
        if not agents_db.latest_turn_synthesize_audio(agent_id):
            return

        spoken: list[tuple[str, bool]] = []
        for match in _SPEAK_RE.finditer(text):
            spoken.extend(
                (chunk, index == 0)
                for index, chunk in enumerate(
                    spoken_chunks_for_tts(match.group(1).strip()))
            )
        if not spoken:
            return

        persona = a.get("persona") or ""
        focused = agents_db.get_focus()
        voice_id = a.get("voice_id") or ""
        session = a.get("session") or ""
        trace_id = agents_db.get_trace(agent_id)
        timing = _read_timing(line_ts)

        for chunk, first_chunk in spoken:
            # Persona prefix when this isn't the focused agent (same rule
            # the hooks use). Don't double-prefix if Claude already said
            # the name inside the <speak>.
            payload = chunk
            if (first_chunk and persona and agent_id != focused
                    and not chunk.lower().startswith(persona.lower())):
                payload = f"{persona} here. {chunk}"
            try:
                qid = self._enqueue(
                    agent_id=agent_id,
                    text=payload,
                    voice_id=voice_id,
                    session=session,
                    source=TurnSource.PWA,
                    trace_id=trace_id,
                    synthesize_audio=True,
                )
                _emit("transcript_streamer", "enqueue",
                      session=session,
                      detail={"queue_id": qid, "agent_id": agent_id,
                              "chars": len(chunk), "tagged": True,
                              # The trace is the agent's marker when the block
                              # is READ, not proof of the turn that wrote it.
                              "selected_trace": trace_id,
                              "trace_source": "read_time_marker",
                              **timing})
            except Exception as e:
                log_exception("transcriptStreamerEnqueueFail", e,
                              detail=str(agent_id))

    # ---- lifecycle ---------------------------------------------------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self.dispatcher.start()
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._reconcile_loop, daemon=True,
            name="transcript-streamer")
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)
        self.dispatcher.stop(timeout=timeout)
        # Drop any pool entries to release file handles. Unbinding every agent
        # (and its pending change) is what keeps a stopped streamer's queued
        # post-import steps from acting; keep it.
        for agent_id in list(self._bound):
            self._unbind(agent_id)
        with self._dirty_lock:
            self._dirty.clear()

    def _reconcile_loop(self) -> None:
        # Run one reconcile pass immediately so a server start picks up
        # already-active agents.
        try:
            self.reconcile_once()
        except Exception as e:
            health.mark_error("transcript_streamer", e)
            log_exception("transcriptStreamerReconcileFail", e)
        while not self._stop.wait(self._reconcile_interval):
            try:
                self.reconcile_once()
            except Exception as e:
                health.mark_error("transcript_streamer", e)
                log_exception("transcriptStreamerReconcileFail", e)


def _read_timing(line_ts: str | None) -> dict:
    """How late a block was read, from the producer's own line timestamp.

    Diagnostics only. A missing, unparseable or future timestamp is an unknown
    clock (``ts_status`` absent/invalid/future), never a lag, and nothing here
    decides which turn a block belongs to."""
    if line_ts is None:
        return {"line_ts": None, "read_lag_ms": None, "ts_status": "absent"}
    try:
        stamp = datetime.datetime.fromisoformat(line_ts.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            raise ValueError("naive timestamp")
        written_ms = int(stamp.timestamp() * 1000)
    except (ValueError, OverflowError):
        return {"line_ts": None, "read_lag_ms": None, "ts_status": "invalid"}
    lag = now_ms() - written_ms
    if lag < 0:
        return {"line_ts": line_ts, "read_lag_ms": None, "ts_status": "future"}
    return {"line_ts": line_ts, "read_lag_ms": lag, "ts_status": "ok"}


def _live_backend_sessions() -> dict[str, str]:
    """Most recent live runtime's backend_session_id per agent, in one query.

    The reconcile pass runs every second for every agent; one lookup per agent
    was about a hundred queries a second on a busy host.
    """
    try:
        from . import db
        rows = db.conn().execute(
            """SELECT agent_id, backend_session_id FROM runtimes
                WHERE ended_at IS NULL ORDER BY started_at""").fetchall()
    except Exception as e:
        health.mark_error("transcript_streamer", e)
        log_exception("transcriptStreamerRuntimeLookupFail", e)
        return {}
    # Later rows started later, so the last write per agent wins.
    return {row["agent_id"]: row["backend_session_id"]
            for row in rows if row["backend_session_id"]}


def _live_backend_session(agent_id: str) -> str | None:
    """Look up the most-recent live runtime's backend_session_id for an agent."""
    try:
        from . import db
        row = db.conn().execute(
            """SELECT backend_session_id FROM runtimes
                WHERE agent_id = ? AND ended_at IS NULL
                ORDER BY started_at DESC LIMIT 1""",
            (agent_id,),
        ).fetchone()
        if row and row["backend_session_id"]:
            return row["backend_session_id"]
    except Exception as e:
        health.mark_error("transcript_streamer", e)
        log_exception("transcriptStreamerRuntimeLookupFail", e, detail=agent_id)
    return None
