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

import re
import threading
from typing import Callable

from . import agents as agents_db
from . import backends
from . import health
from . import transcript_import_cache
from . import tts_queue
from .log import log_exception
from .protocol import SSEType, TurnSource
from .transcript_log import find_latest_jsonl, parse_turns as parse_claude_turns
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
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ---- per-agent subscribe ----------------------------------------

    def reconcile_once(self) -> None:
        """Scan agents + runtimes; subscribe/unsubscribe watchers to match."""
        live = agents_db.list_agents()
        live_ids = set()
        for agent in live:
            agent_id = agent["agent_id"]
            live_ids.add(agent_id)
            if not backends.capabilities(agent.get("backend")).supports_transcript_streaming:
                self._unbind(agent_id)
                continue
            backend_session_id = _live_backend_session(agent_id)
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
        health.mark_success("transcript_streamer")

    def _bind(self, agent: dict, backend_session_id: str) -> None:
        agent_id = agent["agent_id"]
        path = find_latest_jsonl(backend_session_id)
        if path is None:
            # Transcript file doesn't exist yet — try again next reconcile.
            return
        self._unbind(agent_id)
        # New watcher: skip whatever's already in the file. Agents
        # mid-conversation shouldn't replay history.
        watcher = TranscriptWatcher(
            path,
            on_text=lambda text, a=agent: self._on_text(a, text),
            on_change=lambda a=agent, cs=backend_session_id: self._on_change(a, cs),
            start_at_end=True,
            backend_session_id=backend_session_id,
            persist_offset=True,
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
        _emit("transcript_streamer", "unbind",
              backend_session_id=prev,
              detail={"agent_id": agent_id})

    # ---- transcript change → SSE -------------------------------------

    def _on_change(self, agent: dict, backend_session_id: str) -> None:
        """Fires once per tick that consumed bytes — including tool_use
        and tool_result lines that don't go through on_text. Drives the
        client's live-update of the history pane via SSE."""
        self._import_latest(agent, backend_session_id)
        if self._stream is None:
            return
        try:
            self._stream.broadcast({
                "type":           SSEType.TRANSCRIPT_UPDATED,
                "agent_id":       agent["agent_id"],
                "session":   agent.get("session"),
                "backend_session_id": backend_session_id,
            })
        except Exception as e:
            log_exception("transcriptStreamerBroadcastFail", e,
                          detail=agent.get("agent_id"))
        try:
            from . import apns, user_notifications
            for notification in user_notifications.reclassify_recent_suppressed(
                agent_id=agent["agent_id"],
                backend_session_id=backend_session_id,
            ):
                self._stream.broadcast(user_notifications.event_payload(notification))
                apns.on_user_notification(notification)
        except Exception as e:
            log_exception("transcriptStreamerNotificationRetryFail", e,
                          detail=agent.get("agent_id"))

    def _import_latest(self, agent: dict, backend_session_id: str) -> None:
        """Keep SQLite's transcript rows current without waiting for /log.

        Push/badge policy runs server-side, so it cannot rely on a client fetch
        to import the final assistant prose after DONE.
        """
        try:
            backend = backends.normalize(agent.get("backend"))
            latest = (
                find_latest_jsonl(backend_session_id)
                if backend == backends.CLAUDE
                else backends.find_session_jsonl(backend, backend_session_id)
            )
            if latest is None:
                return
            def import_latest() -> None:
                turns = (
                    parse_claude_turns(latest)
                    if backend == backends.CLAUDE
                    else backends.parse_turns(backend, latest)
                )
                agents_db.store_transcript_turns(
                    agent_id=agent["agent_id"],
                    backend_session_id=backend_session_id,
                    source_file=str(latest),
                    turns=turns,
                )

            transcript_import_cache.import_if_changed(latest, import_latest)
        except Exception as e:
            log_exception("transcriptStreamerImportFail", e,
                          detail=agent.get("agent_id"))

    # ---- text-block → tts_queue ----------------------------------------

    def _on_text(self, agent: dict, text: str) -> None:
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
                              "chars": len(chunk), "tagged": True})
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
        # Drop any pool entries to release file handles.
        for agent_id in list(self._bound):
            self._unbind(agent_id)

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
