"""VoiceTimelineRoutes: Handler routes moved out of server.py by tools/refactor/s3_apply.py
(group "voice_timeline"). Generated: change tools/refactor/s3_moves.toml, not this file."""

from __future__ import annotations

from lib import clip_store
from lib import eventlog
from lib import voice_events
from lib.log import log_exception
from lib.routes.helpers import _PLAY_EVENTS, _clip_session_and_trace


class VoiceTimelineRoutes:
    def _handle_clip_ack(self):
        data = self._read_json()
        if data is None:
            return self._json_error(400, "bad json")
        status = (data.get("status") or "").strip()
        clip_id = data.get("clip_id")
        try:
            clip_id = int(clip_id) if clip_id not in (None, "") else None
        except (TypeError, ValueError):
            return self._json_error(400, "bad clip_id")
        url = (data.get("url") or "").strip()
        error = (data.get("error") or "").strip() or None
        trace_id = (data.get("trace_id") or None)
        self._trace_id = trace_id
        try:
            ok = clip_store.mark_clip_status(
                clip_id=clip_id,
                url=url,
                status=status,
                error=error,
            )
        except ValueError:
            return self._json_error(400, "bad status")
        eventlog.emit("client", "clipAck",
                      trace_id=trace_id,
                      clip_id=clip_id,
                      clip_url=url or None,
                      detail={"clip_id": clip_id, "status": status,
                              "updated": ok, "error": error})
        play_event = _PLAY_EVENTS.get(status)
        if play_event:
            clip_session, clip_trace = _clip_session_and_trace(clip_id)
            self._record_voice(
                play_event, session=clip_session, trace_id=trace_id or clip_trace,
                detail={"clip_id": clip_id, "url": url or None, "error": error})
        self._json_ok({"ok": True, "updated": ok})

    def _handle_message_clips(self):
        from lib.message_audio import retained_events
        query = self._query()
        session = str(query.get("session", [""])[0]).strip()
        message_id = str(query.get("message_id", [""])[0]).strip()
        if not session or not message_id or len(session) > 256 or len(message_id) > 1024:
            return self._json_error(400, "session and message_id required")
        events = retained_events(session=session, message_id=message_id, audio_dir=self.ctx.audio_dir)
        if not events:
            return self._json_error(404, "Retained audio is unavailable for this message")
        self._json_ok({"events": events})

    def _handle_recoverable_clips(self):
        query = self._query()
        session = str(query.get("session", [""])[0] or "").strip()
        events = clip_store.recoverable_events(session=session)
        self._json_ok({"events": events})

    def _handle_voice_events_get(self):
        """GET /voice-events?session=&since=&until=&utterance_id=&trace_id=&event=&limit="""
        params = self._voice_query_params()
        limit = params.pop("limit") or 500
        rows = voice_events.query(limit=limit, **params)
        self._json_ok({"events": rows})

    def _handle_voice_events_post(self):
        """Client half of the voice timeline (lib/voice_events.py).

        Body: {"source": "ios"|"pwa", "client_id": "...", "sent_at": <client
        wall-clock ms>, "session": "<default>", "events": [{"event", "ts",
        "mono_ms", "utterance_id", "trace_id", "session", "duration_ms",
        "level_db", "peak_db", "text", "detail"}]}. The server measures the
        clock offset from sent_at and stores corrected timestamps.
        """
        data = self._read_json()
        if data is None:
            return self._json_error(400, "bad json")
        events = data.get("events")
        if not isinstance(events, list):
            return self._json_error(400, "events must be a list")
        if len(events) > voice_events.MAX_BATCH:
            return self._json_error(413, "batch too large")
        source = str(data.get("source") or "other").strip().lower()
        result = voice_events.record_batch(
            source=source, events=events,
            client_id=(str(data.get("client_id") or "").strip() or None),
            sent_at=data.get("sent_at"),
            default_session=(str(data.get("session") or "").strip() or None),
        )
        body = {"ok": True, "stored": result["stored"],
                "clock_offset_ms": result["clock_offset_ms"],
                "server_now": voice_events.now_ms()}
        self._json_ok(body)

    def _handle_voice_utterances_get(self):
        """GET /voice-events/utterances?session=&since=&until=&limit= — one row per utterance."""
        params = self._voice_query_params()
        rows = voice_events.utterances(
            session=params["session"], since=params["since"],
            until=params["until"], limit=params["limit"] or 100)
        self._json_ok({"utterances": rows})

    def _record_voice(self, event: str, **fields) -> None:
        """Best-effort voice-timeline row; never fails the request."""
        try:
            session = fields.pop("session", None)
            if session is None:
                session = self._focused_session() or self.ctx.default_session or None
            voice_events.record(event, source="server", session=session, **fields)
        except Exception as e:  # noqa: BLE001
            log_exception("voiceEventRecordFail", e, detail=event)

    def _record_voice_corrupt(self, utterance_id: str, client_ts: int | None,
                              reason: str, **detail) -> None:
        self._record_voice("corrupt", utterance_id=utterance_id or None,
                           client_ts=client_ts,
                           detail={"reasons": [reason], **detail})

    def _voice_query_params(self) -> dict:
        qs = self._query()
        def one(name):
            return (qs.get(name, [""])[0] or "").strip()
        def num(name):
            raw = one(name)
            if not raw:
                return None
            try:
                return int(float(raw))
            except ValueError:
                return None
        return {"session": one("session") or None, "since": num("since"),
                "until": num("until"), "utterance_id": one("utterance_id") or None,
                "trace_id": one("trace_id") or None, "event": one("event") or None,
                "limit": num("limit")}
