"""Server-side herald / raise-your-hand manager.

When an agent emits TTS audio and the user isn't currently engaged with that
agent (not their pane, not the one being awaited), we don't dump the full
reply into the queue. Instead we broadcast a short herald clip and hold the
real audio in a per-session buffer. The buffer is released only when the
user grants permission via affirmative + name in /transcribe, or when they
shift focus to that agent.

Tested via tests/unit/test_herald.py.
"""
from __future__ import annotations

import threading
import time
from contextlib import nullcontext
from dataclasses import dataclass, field
from typing import Callable, ContextManager

from . import settings_store
from .intent import classify_intent
from .log import log, log_exception


AWAITING_TTL_SEC = 60.0  # how long after /send the addressee gets exclusive right-of-way
DEFAULT_SPEAK_IF_SHORT_CHARS = 120
MAX_SPEAK_IF_SHORT_CHARS = 2000
KEY_DISABLED = "herald.disabled"
KEY_SPEAK_IF_SHORT_CHARS = "herald.speak_if_short_chars"
KEY_SHORT_REPLY_BYPASS_ENABLED = "herald.short_reply_bypass_enabled"


@dataclass
class IngestResult:
    broadcast: bool = False        # original clip was forwarded to SSE
    herald_emitted: bool = False   # a herald announcement was generated


@dataclass(frozen=True)
class _HeraldAttempt:
    session: str
    info: dict
    token: object
    meta: dict | None
    settings: "HeraldSettings"


@dataclass
class HeraldDecision:
    granted: list[str] = field(default_factory=list)
    declined: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class HeraldSettings:
    disabled: bool = False
    speak_if_short_chars: int = DEFAULT_SPEAK_IF_SHORT_CHARS
    short_reply_bypass_enabled: bool = True

    def as_dict(self) -> dict:
        return {
            "disabled": self.disabled,
            "speak_if_short_chars": self.speak_if_short_chars,
            "short_reply_bypass_enabled": self.short_reply_bypass_enabled,
        }


def get_settings() -> HeraldSettings:
    return HeraldSettings(
        disabled=settings_store.get_bool(KEY_DISABLED, default=False),
        speak_if_short_chars=settings_store.get_int(
            KEY_SPEAK_IF_SHORT_CHARS,
            default=DEFAULT_SPEAK_IF_SHORT_CHARS,
            minimum=0,
            maximum=MAX_SPEAK_IF_SHORT_CHARS,
        ),
        # Missing keys are installations from before this setting existed.
        # True preserves their established short-reply behavior.
        short_reply_bypass_enabled=settings_store.get_bool(
            KEY_SHORT_REPLY_BYPASS_ENABLED, default=True),
    )


def update_settings(data: dict) -> HeraldSettings:
    if not isinstance(data, dict):
        raise ValueError("herald settings must be an object")
    if "disabled" in data and not isinstance(data.get("disabled"), bool):
        raise ValueError("disabled must be a boolean")
    if "speak_if_short_chars" in data:
        value = data.get("speak_if_short_chars")
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("speak_if_short_chars must be an integer")
        if not 0 <= value <= MAX_SPEAK_IF_SHORT_CHARS:
            raise ValueError(
                f"speak_if_short_chars must be between 0 and {MAX_SPEAK_IF_SHORT_CHARS}")
    if ("short_reply_bypass_enabled" in data
            and not isinstance(data.get("short_reply_bypass_enabled"), bool)):
        raise ValueError("short_reply_bypass_enabled must be a boolean")
    if "disabled" in data:
        settings_store.set_bool(KEY_DISABLED, data["disabled"])
    if "speak_if_short_chars" in data:
        settings_store.set_int(KEY_SPEAK_IF_SHORT_CHARS, data["speak_if_short_chars"])
    if "short_reply_bypass_enabled" in data:
        settings_store.set_bool(
            KEY_SHORT_REPLY_BYPASS_ENABLED,
            data["short_reply_bypass_enabled"],
        )
    return get_settings()


def herald_text(persona: str) -> str:
    return f"{persona} here, ready for an update."


class HeraldManager:
    def __init__(self, *, stream, tts, agents: Callable[[], dict],
                 focus_session: Callable[[], str] | None = None,
                 focus_guard: Callable[[], ContextManager] = nullcontext,
                 awaiting_ttl: float = AWAITING_TTL_SEC,
                 clock: Callable[[], float] = time.time,
                 settings: Callable[[], HeraldSettings] = get_settings):
        self._stream = stream
        self._tts = tts
        self._agents_fn = agents
        self._focus: str = ""
        # Focus is read LIVE so it can never drift from the real (DB) focus —
        # caching it is exactly what made the herald announce the focused agent.
        # Production injects agents_db.get_focus_session; the default reads the
        # in-memory _focus (set_focus) so existing tests keep working.
        self._focus_session = focus_session or (lambda: self._focus)
        self._focus_guard = focus_guard
        self._awaiting: str = ""
        self._awaiting_at: float = 0.0
        self._awaiting_ttl = awaiting_ttl
        self._clock = clock
        self._settings_fn = settings
        self._buffers: dict[str, list[str]] = {}    # sid -> [url, ...]
        self._clip_meta: dict[str, dict] = {}        # url -> sidecar metadata
        self._pending: set[str] = set()             # sids whose herald has fired
        self._gated_buffers: set[str] = set()       # held until authoritative release
        self._herald_inflight: dict[str, object] = {}
        self._state_lock = threading.RLock()

    # ---- state setters ------------------------------------------------

    def set_focus(self, sid: str) -> None:
        with self._state_lock:
            self._set_focus_locked(sid)

    def _set_focus_locked(self, sid: str) -> None:
        self._focus = sid or ""
        # Opening a DIFFERENT agent ends the await on the previous one: you've
        # moved on, so its pending reply should raise its hand (herald) rather
        # than play through and interrupt the agent you just opened.
        if sid and self._awaiting and sid != self._awaiting:
            self._awaiting = ""
            self._awaiting_at = 0.0
        if sid and sid in self._buffers:
            self._flush(sid)

    def set_awaiting(self, sid: str) -> None:
        with self._state_lock:
            self._awaiting = sid or ""
            self._awaiting_at = self._clock() if sid else 0.0
            if sid and sid in self._buffers:
                self._flush(sid)

    def _awaiting_active(self) -> bool:
        if not self._awaiting:
            return False
        return (self._clock() - self._awaiting_at) < self._awaiting_ttl

    # ---- inspection helpers (also used by tests) ---------------------

    def pending_heralds(self) -> set[str]:
        with self._state_lock:
            return set(self._pending)

    def held_clips(self, sid: str) -> list[str]:
        with self._state_lock:
            return list(self._buffers.get(sid, []))

    # ---- ingest path --------------------------------------------------

    def ingest_clip(self, session: str, *, url: str, ts: int,
                    meta: dict | None = None) -> IngestResult:
        with self._focus_guard():
            with self._state_lock:
                outcome = self._begin_ingest_locked(
                    session, url=url, ts=ts, meta=meta)
        if isinstance(outcome, IngestResult):
            return outcome

        synth_reason, herald_url = self._synthesize_herald(
            outcome.session, outcome.info)
        with self._focus_guard():
            with self._state_lock:
                return self._finish_herald_locked(
                    outcome, synth_reason=synth_reason, herald_url=herald_url)

    def _begin_ingest_locked(self, session: str, *, url: str, ts: int,
                             meta: dict | None = None) -> IngestResult | _HeraldAttempt:
        """Decide whether the clip plays now or raises its hand.

        Rules:
          * Active awaiting (within TTL): ONLY the addressee passes through;
            everyone else (even the focused agent) heralds.
          * No active awaiting: the focused agent passes through; others herald.
        """
        if meta:
            self._clip_meta[url] = dict(meta)
        if not session:
            broadcasted = self._broadcast_direct_locked(
                url=url, session=session, ts=ts)
            self._log_decision(
                session=session, focus="", awaiting_active=False,
                action="broadcast" if broadcasted else "broadcast_failed",
                reason="no_session", meta=meta)
            return IngestResult(broadcast=broadcasted, herald_emitted=False)

        focus = self._focus_session()
        awaiting_active = self._awaiting_active()
        if awaiting_active:
            allow = session == self._awaiting
        else:
            allow = session == focus

        if allow:
            broadcasted = self._broadcast_direct_locked(
                url=url, session=session, ts=ts, release_gated=True)
            self._log_decision(
                session=session, focus=focus, awaiting_active=awaiting_active,
                action="broadcast" if broadcasted else "broadcast_failed",
                reason="awaited_session" if awaiting_active else "focused_session",
                meta=meta)
            return IngestResult(broadcast=broadcasted, herald_emitted=False)

        settings = self._settings_fn()
        if settings.disabled:
            broadcasted = self._broadcast_direct_locked(
                url=url, session=session, ts=ts)
            self._log_decision(
                session=session, focus=focus, awaiting_active=awaiting_active,
                action="broadcast" if broadcasted else "broadcast_failed",
                reason="herald_disabled", meta=meta,
                settings=settings)
            return IngestResult(broadcast=broadcasted, herald_emitted=False)
        if self._is_short_reply(meta, settings):
            broadcasted = self._broadcast_direct_locked(
                url=url, session=session, ts=ts)
            self._log_decision(
                session=session, focus=focus, awaiting_active=awaiting_active,
                action="broadcast" if broadcasted else "broadcast_failed",
                reason="short_reply_bypass", meta=meta,
                settings=settings)
            return IngestResult(broadcast=broadcasted, herald_emitted=False)

        info = (self._agents_fn() or {}).get(session)
        if not info:
            # Unknown session / unregistered — best-effort forward.
            broadcasted = self._broadcast_direct_locked(
                url=url, session=session, ts=ts)
            self._log_decision(
                session=session, focus=focus, awaiting_active=awaiting_active,
                action="broadcast" if broadcasted else "broadcast_failed",
                reason="unknown_session", meta=meta,
                settings=settings)
            return IngestResult(broadcast=broadcasted, herald_emitted=False)

        # Buffer before starting provider I/O so a concurrent focus transition
        # cannot miss the clip. Only one synthesis attempt runs per session.
        self._buffers.setdefault(session, []).append(url)
        self._gated_buffers.add(session)
        if session in self._pending:
            self._log_decision(
                session=session, focus=focus, awaiting_active=awaiting_active,
                action="hold", reason="already_pending", meta=meta,
                settings=settings)
            return IngestResult(broadcast=False, herald_emitted=False)
        if session in self._herald_inflight:
            self._log_decision(
                session=session, focus=focus, awaiting_active=awaiting_active,
                action="hold", reason="herald_inflight", meta=meta,
                settings=settings)
            return IngestResult(broadcast=False, herald_emitted=False)

        token = object()
        self._herald_inflight[session] = token
        return _HeraldAttempt(
            session=session, info=info, token=token, meta=meta,
            settings=settings)

    def _finish_herald_locked(
        self, attempt: _HeraldAttempt, *, synth_reason: str,
        herald_url: str,
    ) -> IngestResult:
        session = attempt.session
        focus = self._focus_session()
        awaiting_active = self._awaiting_active()
        if self._herald_inflight.get(session) is not attempt.token:
            return IngestResult(broadcast=False, herald_emitted=False)

        now_allowed = (
            session == self._awaiting if awaiting_active else session == focus
        )
        if now_allowed or not self._buffers.get(session):
            self._herald_inflight.pop(session, None)
            flushed = (
                self._flush(session) if self._buffers.get(session) else True
            )
            self._log_decision(
                session=session, focus=focus, awaiting_active=awaiting_active,
                action="broadcast" if flushed else "hold",
                reason=("herald_superseded" if flushed else
                        "superseded_flush_failed"),
                meta=attempt.meta, settings=attempt.settings)
            return IngestResult(broadcast=flushed, herald_emitted=False)

        if synth_reason != "synthesized":
            self._herald_inflight.pop(session, None)
            self._log_decision(
                session=session, focus=focus, awaiting_active=awaiting_active,
                action="hold", reason=synth_reason, meta=attempt.meta,
                settings=attempt.settings)
            return IngestResult(broadcast=False, herald_emitted=False)

        herald_emitted = self._broadcast_audio(
            url=herald_url, session=session, ts=0, herald=True)
        self._herald_inflight.pop(session, None)
        if herald_emitted:
            self._pending.add(session)
            log("heraldEmitted", f"{session} → {herald_url}")
            reason = "herald_emitted"
        else:
            reason = "herald_broadcast_failed"
        self._log_decision(
            session=session, focus=focus, awaiting_active=awaiting_active,
            action="hold", reason=reason, meta=attempt.meta,
            settings=attempt.settings)
        return IngestResult(broadcast=False, herald_emitted=herald_emitted)

    def _is_short_reply(self, meta: dict | None, settings: HeraldSettings) -> bool:
        if not settings.short_reply_bypass_enabled:
            return False
        if not meta:
            return False
        try:
            text_len = int(meta.get("text_len"))
        except (TypeError, ValueError):
            return False
        return text_len <= settings.speak_if_short_chars

    def _log_decision(
        self, *, session: str, focus: str, awaiting_active: bool,
        action: str, reason: str, meta: dict | None,
        settings: HeraldSettings | None = None,
    ) -> None:
        raw_text_len = (meta or {}).get("text_len")
        text_len = raw_text_len if isinstance(raw_text_len, int) else "unknown"
        threshold = settings.speak_if_short_chars if settings else "unknown"
        bypass = settings.short_reply_bypass_enabled if settings else "unknown"
        log(
            "heraldDecision",
            f"session={session or 'none'} action={action} reason={reason} "
            f"focus={focus or 'none'} awaiting={self._awaiting or 'none'} "
            f"awaiting_active={str(awaiting_active).lower()} "
            f"text_len={text_len} short_threshold={threshold} "
            f"short_bypass={str(bypass).lower() if isinstance(bypass, bool) else bypass}",
        )

    # ---- user-text path ----------------------------------------------

    def on_user_text(self, text: str) -> HeraldDecision:
        with self._state_lock:
            return self._on_user_text_locked(text)

    def _on_user_text_locked(self, text: str) -> HeraldDecision:
        """Apply intent classification against currently-held agents and
        flush / drop buffers accordingly."""
        if not self._pending:
            return HeraldDecision()
        agents = self._agents_fn() or {}
        # Map persona-name → sid so we can translate intent grants back.
        name_to_sid = {(agents.get(sid) or {}).get("name", ""): sid
                       for sid in self._pending}
        candidates = [n for n in name_to_sid if n]
        intent = classify_intent(text, candidates)

        granted_sids = [name_to_sid[n] for n in intent.grants if n in name_to_sid]
        declined_sids = [name_to_sid[n] for n in intent.declines if n in name_to_sid]
        for sid in granted_sids:
            self._flush(sid)
        for sid in declined_sids:
            log("heraldDeclined", sid)
            # Buffer stays; future grant / focus shift will still release.
        return HeraldDecision(granted=granted_sids, declined=declined_sids)

    # ---- internals ----------------------------------------------------

    def _broadcast_direct_locked(
        self, *, url: str, session: str, ts: int,
        release_gated: bool = False,
    ) -> bool:
        if self._buffers.get(session):
            gated = (
                session in self._gated_buffers
            )
            if gated and not release_gated:
                self._buffers[session].append(url)
                log("heraldDirectQueued", f"{session or 'none'} url={url}")
                return False
            if not self._flush(session, herald=session in self._pending):
                self._buffers.setdefault(session, []).append(url)
                log("heraldDirectRetained", f"{session or 'none'} url={url}")
                return False
        if self._broadcast_audio(url=url, session=session, ts=ts):
            return True
        self._buffers.setdefault(session, []).append(url)
        log("heraldDirectRetained", f"{session or 'none'} url={url}")
        return False

    def _flush(self, sid: str, *, herald: bool = True) -> bool:
        with self._state_lock:
            urls = list(self._buffers.get(sid, []))
            was_pending = sid in self._pending
            delivered = 0
            for url in urls:
                if not self._broadcast_audio(
                    url=url, session=sid, ts=0, herald=herald
                ):
                    break
                delivered += 1
            failed = urls[delivered:]
            if failed:
                self._buffers[sid] = failed
                if was_pending:
                    self._pending.add(sid)
                else:
                    self._pending.discard(sid)
                event = "heraldFlushPartial" if delivered else "heraldFlushFailed"
                log(event, f"{sid} delivered={delivered} failed={len(failed)}")
                return False
            self._buffers.pop(sid, None)
            self._pending.discard(sid)
            self._gated_buffers.discard(sid)
            log("heraldFlushed", f"{sid} count={len(urls)}")
            return True

    def _broadcast_audio(self, *, url: str, session: str, ts: int,
                         herald: bool = False) -> bool:
        name = url.rsplit("/", 1)[-1] if "/" in url else url
        meta = self._clip_meta.get(url, {})
        event = {
            "type": "audio", "url": url, "name": name, "session": session,
        }
        # Cross-agent clips (the "ready for an update" herald + just-granted
        # held clips) must play regardless of which agent the client is
        # focused on, and with priority — flag them so the client doesn't
        # starve them behind the focused conversation.
        if herald:
            event["herald"] = True
        if meta.get("clip_id"):
            event["clip_id"] = meta["clip_id"]
        if meta.get("trace_id"):
            event["trace_id"] = meta["trace_id"]
        if meta.get("persona"):
            event["persona"] = meta["persona"]
        if meta.get("agent_id"):
            event["agent_id"] = meta["agent_id"]
        # Preserve the producer-selected delivery URL and any delivery-
        # specific fields (delivery=hls + playlist_url, or stream_url for
        # the chunked-file and raw-pcm paths).
        if meta.get("streamable"):
            event["streamable"] = True
            if meta.get("delivery"):
                event["delivery"] = meta["delivery"]
            if meta.get("playlist_url"):
                event["playlist_url"] = meta["playlist_url"]
            if meta.get("stream_url"):
                event["stream_url"] = meta["stream_url"]
            if meta.get("complete_url"):
                event["complete_url"] = meta["complete_url"]
            if meta.get("audio_format"):
                event["audio_format"] = meta["audio_format"]
        try:
            self._stream.broadcast(event)
            self._clip_meta.pop(url, None)
            return True
        except Exception as e:
            log_exception("heraldBroadcastFail", e, detail=url)
            return False

    def _synthesize_herald(self, sid: str, info: dict) -> tuple[str, str]:
        persona = (info.get("name") or sid).strip()
        voice_id = (info.get("voice_id") or "").strip()
        text = herald_text(persona)
        try:
            herald_url = self._tts.synthesize_herald(
                text, voice_id, session=sid)
        except Exception as e:
            log_exception("heraldSynthFail", e, detail=persona)
            return "synthesis_failed", ""
        if not herald_url:
            return "synthesis_failed", ""
        # The TTS engine returns a filesystem path; the client needs the
        # `/audio/<name>` form so its <audio> element can fetch via the
        # server's audio route.
        import pathlib as _pl
        herald_url_str = str(herald_url)
        if "/" in herald_url_str and not herald_url_str.startswith("/audio/"):
            herald_url_str = f"/audio/{_pl.Path(herald_url_str).name}"
        return "synthesized", herald_url_str
