"""SSE broadcast hub + audio-directory janitor.

The HTTP server creates one `AudioStream` instance at startup. It exposes:
  * `subscribe()`  — returns a queue.Queue the SSE handler reads from
  * `recent()`     — list of events to replay on reconnect
  * `broadcast(ev)` — push to all live subscribers
  * `start()`      — kicks off the janitor thread that deletes old mp3s

Every `audio` event is published by whoever produced the clip: the TTS worker
or the /preview handler. Nothing scans the directory.
"""
from __future__ import annotations

from . import clip_store

import json
import pathlib
import queue
import sqlite3
import threading
import time

from .log import log_exception
from . import health
from .protocol import ClipStatus, SSEType
from .timing import SERVER_TIMING


def _emit(*a, **kw):
    """Lazy eventlog import to avoid pulling it into test fakes."""
    try:
        from . import eventlog
        eventlog.emit(*a, **kw)
    except Exception:
        pass


# Event types whose "recent" backlog should retain only the latest copy.
# These represent CURRENT STATE (e.g. focus, server version) rather than
# distinct historical occurrences (audio clips, agent state transitions),
# so a reconnecting client only needs the most recent value.
_STATEFUL_SINGLETON_TYPES = frozenset({
    SSEType.AGENT_FOCUS,
    SSEType.SERVER_VERSION,
})

# Actions describe user input at one instant and must never survive an SSE
# reconnect. Keep this read-side fence even though current producers use
# broadcast_ephemeral(): upgraded installations may still have action rows
# persisted by an older release until the SSE retention window expires.
_NON_REPLAYABLE_TYPES = frozenset({SSEType.REMOTE_ACTION})


def _replayable(events: list[dict]) -> list[dict]:
    return [event for event in events
            if event.get("type") not in _NON_REPLAYABLE_TYPES]


class SubscriberQueue(queue.Queue):
    """Per-SSE-subscriber event queue.

    `evicted` flips when broadcast() removes the subscriber for backpressure
    (queue full). The owning SSE handler must then deliver what remains and
    close the connection — an evicted queue never receives another event, and
    pinging it would present the client a healthy-looking dead stream.
    """
    evicted: bool = False


class AudioStream:
    RECENT_WINDOW_SEC = SERVER_TIMING.audio_recent_window_sec
    AUDIO_RETAIN_SEC  = SERVER_TIMING.audio_retain_sec
    JANITOR_INTERVAL_SEC = SERVER_TIMING.audio_janitor_interval_sec
    REPLAY_RETAIN_SEC = 7 * 24 * 60 * 60
    REPLAY_RETAIN_MAX_BYTES = 256 * 1024 * 1024

    def __init__(self, audio_dir: pathlib.Path, *,
                 transcript_event_min_interval_sec: float = 0.25,
                 monotonic=time.monotonic):
        self.audio_dir = audio_dir
        self.audio_dir.mkdir(parents=True, exist_ok=True)
        self._subs: list[queue.Queue] = []
        self._subs_lock = threading.Lock()
        self._recent: list[tuple[float, dict]] = []
        self._recent_lock = threading.Lock()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._transcript_event_min_interval = transcript_event_min_interval_sec
        self._monotonic = monotonic
        self._last_transcript_event: dict[str, float] = {}
        self._transcript_event_lock = threading.Lock()

    # --- subscriber API ---------------------------------------------------

    def subscribe(self, maxsize: int = 128) -> "SubscriberQueue":
        q = SubscriberQueue(maxsize=maxsize)
        with self._subs_lock:
            self._subs.append(q)
            n = len(self._subs)
        _emit("audio_stream", "subscribe", detail={"subscribers": n})
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._subs_lock:
            try:
                self._subs.remove(q)
                n = len(self._subs)
                _emit("audio_stream", "unsubscribe", detail={"subscribers": n})
            except ValueError:
                pass

    def recent(self, since_event_id: int | None = None) -> list[dict]:
        if since_event_id is not None:
            try:
                from . import agents as _agents
                return _replayable(_agents.events_after(since_event_id))
            except Exception:
                pass
        try:
            from . import agents as _agents
            return _replayable(
                _agents.recent_events(int(self.RECENT_WINDOW_SEC * 1000)))
        except Exception:
            pass
        with self._recent_lock:
            return _replayable([ev for _, ev in self._recent])

    def broadcast(self, event_dict: dict) -> None:
        event_dict = dict(event_dict)
        session = event_dict.get("session")
        if session:
            event_dict.setdefault("session", session)
        if event_dict.get("type") == SSEType.TRANSCRIPT_UPDATED:
            # Transcript writers can emit dozens of partial rows in one model
            # turn. SSE is only a wake-up hint—the canonical /log cursor is the
            # delivery mechanism—so cap identical session wake-ups instead of
            # making every token/tool delta launch work on the phone.
            key = str(session or event_dict.get("agent_id") or "")
            now = self._monotonic()
            with self._transcript_event_lock:
                previous = self._last_transcript_event.get(key)
                if (previous is not None
                        and now - previous < self._transcript_event_min_interval):
                    return
                self._last_transcript_event[key] = now
        if event_dict.get("type") == SSEType.AUDIO:
            try:
                from . import agents as _agents
                clip_store.mark_clip_status(
                    clip_id=event_dict.get("clip_id"),
                    url=event_dict.get("url"),
                    status=ClipStatus.BROADCAST,
                )
            except Exception as e:
                log_exception("clipBroadcastMarkFail", e,
                              detail=str(event_dict.get("url") or ""))
        sse_event_id = None
        try:
            from . import agents as _agents
            sse_event_id = _agents.record_sse_event(event_dict)
            event_dict["event_id"] = sse_event_id
        except Exception as e:
            log_exception("sseEventRecordFail", e,
                          detail=str(event_dict.get("type") or ""))
            health.mark_error("sse", e)
        with self._recent_lock:
            # Stateful singletons (focus, server-version): only the latest
            # value is meaningful on reconnect. Drop any prior copies so the
            # replay backlog doesn't carry stale history — without this,
            # a brief storm of focus changes would replay forever as
            # alternating focus events on every fresh SSE subscription.
            ev_type = event_dict.get("type")
            if ev_type in _STATEFUL_SINGLETON_TYPES:
                self._recent = [(t, ev) for t, ev in self._recent
                                if ev.get("type") != ev_type]
            self._recent.append((time.time(), event_dict))
            cutoff = time.time() - self.RECENT_WINDOW_SEC
            while self._recent and self._recent[0][0] < cutoff:
                self._recent.pop(0)
        self._deliver(event_dict, sse_event_id=sse_event_id)

    def broadcast_ephemeral(self, event_dict: dict) -> None:
        """Deliver live input without recording it for reconnect replay.

        Physical button edges and shortcut toggles describe an action at one
        instant, not durable state. Replaying one later can unexpectedly start
        a microphone or stop an agent.
        """
        self._deliver(dict(event_dict), sse_event_id=None)

    def _deliver(self, event_dict: dict, *, sse_event_id: int | None) -> None:
        payload = json.dumps(event_dict)
        dead = []
        with self._subs_lock:
            sub_count = len(self._subs)
            for q in self._subs:
                try:
                    q.put_nowait(payload)
                except queue.Full as e:
                    log_exception("sseSubFull", e)
                    health.mark_error("sse", e)
                    # Flag before removal: the SSE handler thread checks this
                    # and closes its connection instead of ghost-pinging a
                    # stream that will never receive another event.
                    if isinstance(q, SubscriberQueue):
                        q.evicted = True
                    dead.append(q)
            for q in dead:
                try:
                    self._subs.remove(q)
                except ValueError as e:
                    log_exception("sseSubRemoveMiss", e)
        _emit(
            "audio_stream", "broadcast",
            trace_id=event_dict.get("trace_id"),
            session=event_dict.get("session"),
            agent_id=event_dict.get("agent_id"),
            clip_id=event_dict.get("clip_id"),
            clip_url=event_dict.get("url"),
            sse_event_id=sse_event_id,
            detail={
                "type": event_dict.get("type"),
                "session": event_dict.get("session"),
                "subscribers": sub_count,
                "ephemeral": sse_event_id is None,
            },
        )
        health.mark_success("sse")

    # --- background threads ----------------------------------------------

    def start(self) -> None:
        if any(t.is_alive() for t in self._threads):
            return
        self._stop.clear()
        self._threads = [
            threading.Thread(target=self._janitor, daemon=True),
        ]
        for t in self._threads:
            t.start()

    def stop(self, timeout: float = 1.0) -> None:
        self._stop.set()
        for t in self._threads:
            if t.is_alive():
                t.join(timeout=timeout)

    def _janitor(self) -> None:
        while not self._stop.wait(self.JANITOR_INTERVAL_SEC):
            self._prune_audio()

    def _prune_audio(self) -> None:
        from .message_audio import retained_mp3_paths
        try:
            protected = retained_mp3_paths(audio_dir=self.audio_dir,
                max_age_ms=self.REPLAY_RETAIN_SEC * 1000, max_bytes=self.REPLAY_RETAIN_MAX_BYTES)
        except (sqlite3.Error, OSError) as error:
            log_exception("audioReplayRetentionLookupFail", error)
            return  # A transient metadata failure must not erase the replay cache.
        cutoff = time.time() - self.AUDIO_RETAIN_SEC
        try:
            for p in self.audio_dir.glob("*.mp3"):
                try:
                    if p.stat().st_mtime < cutoff and str(p.resolve()) not in protected:
                        p.unlink()
                        side = p.with_suffix(p.suffix + ".json")
                        try: side.unlink()
                        except FileNotFoundError: pass
                except OSError as error:
                    log_exception("audioJanitorUnlinkFail", error, detail=p.name)
        except FileNotFoundError as error:
            log_exception("audioJanitorMissingDir", error, detail=str(self.audio_dir))
