"""Permanent voice timeline: what happened in a voice exchange, and when.

Every row is one moment in the life of an utterance, from the phone's VAD
hearing speech to the reply's first audible sample. Unlike
`diagnostic_events` (telemetry.sqlite, pruned after a day) this table lives in
state.sqlite and is never pruned: it is the user's own record of how car
mode behaved, meant for after-the-fact investigation of latency, volume, and
corrupted captures.

Clients POST batches to /voice-events with their own wall-clock and
monotonic timestamps plus `sent_at`; the server measures the clock offset
per batch and stores corrected `ts` next to the raw `client_ts`, so phone
and server rows sort on one timeline. Server-side producers (transcribe,
send, clip acks) record straight into the table.

Event vocabulary (`event` column). Client-produced unless noted:

  listen_start / listen_stop   hands-free listening toggled
  speech_start                 VAD onset. detail.silence_ms = gap since the
                               previous speech_end when known
  speech_end                   VAD offset. duration_ms = utterance length,
                               level_db / peak_db = RMS and peak dBFS
  silence                      explicit gap row, duration_ms = gap length
  level                        a level sample; server emits one per
                               transcription with decoded-audio metrics
  upload_start / upload_end    /transcribe request timing, detail.bytes
  transcript                   (server) STT result. duration_ms = STT time,
                               text = what was heard
  retry                        (server) idempotent /transcribe replay
  transcript_received          round trip as the client saw it
  send                         (server) the utterance became a turn
  barge_in                     user spoke over playback
  play_start / play_end /      (server, from /clips/ack) reply playback
  play_fail
  corrupt                      something was wrong with the capture;
                               detail.reasons lists why
  error                        anything else that failed, detail.message

`utterance_id` ties client rows to the transcription job id the client sends
as X-Transcription-ID (or X-Utterance-ID) on /transcribe; `trace_id` ties the
server side of the turn (transcript, send, playback) together.
"""
from __future__ import annotations

import json
import time
from typing import Any

from . import db

SOURCES = ("ios", "pwa", "server", "other")
MAX_BATCH = 500
MAX_TEXT = 4000
MAX_DETAIL = 8000
# A phone clock that disagrees with the server by more than this is treated
# as unset: we keep the raw client_ts but stamp ts with receipt time.
MAX_PLAUSIBLE_OFFSET_MS = 6 * 60 * 60 * 1000


def now_ms() -> int:
    return int(time.time() * 1000)


def _num(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if out != out or out in (float("inf"), float("-inf")):
        return None
    return out


def _int(value: Any) -> int | None:
    out = _num(value)
    return int(out) if out is not None else None


def _text(value: Any, limit: int) -> str | None:
    """Identifiers: empty reads as absent."""
    if value is None:
        return None
    s = str(value)
    return s[:limit] if s else None


def _body(value: Any, limit: int) -> str | None:
    """Transcript text: an empty string is a finding (nothing was heard)."""
    if value is None:
        return None
    return str(value)[:limit]


def _detail_json(detail: Any) -> str:
    if not isinstance(detail, dict) or not detail:
        return "{}"
    raw = json.dumps(detail, separators=(",", ":"), default=str)
    if len(raw) > MAX_DETAIL:
        raw = json.dumps({"truncated": True, "head": raw[:MAX_DETAIL - 64]},
                         separators=(",", ":"))
    return raw


def record(event: str, *, source: str = "server", ts: int | None = None,
           client_ts: int | None = None, mono_ms: int | None = None,
           received_at: int | None = None, clock_offset_ms: int | None = None,
           client_id: str | None = None, session: str | None = None,
           utterance_id: str | None = None, trace_id: str | None = None,
           duration_ms: float | None = None, level_db: float | None = None,
           peak_db: float | None = None, text: str | None = None,
           detail: dict | None = None) -> int:
    """Insert one row and return its event_id."""
    event = str(event or "").strip()[:64]
    if not event:
        raise ValueError("voice event needs a name")
    received_at = received_at if received_at is not None else now_ms()
    ts = ts if ts is not None else received_at
    source = source if source in SOURCES else "other"
    cur = db.conn().execute(
        """INSERT INTO voice_events
           (ts, client_ts, mono_ms, received_at, clock_offset_ms, source,
            client_id, session, utterance_id, trace_id, event, duration_ms,
            level_db, peak_db, text, detail)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (int(ts), _int(client_ts), _int(mono_ms), int(received_at),
         _int(clock_offset_ms), source, _text(client_id, 128),
         _text(session, 128), _text(utterance_id, 128), _text(trace_id, 128),
         event, _num(duration_ms), _num(level_db), _num(peak_db),
         _body(text, MAX_TEXT), _detail_json(detail)),
    )
    return int(cur.lastrowid)


def clock_offset(sent_at: int | None, received_at: int) -> int | None:
    """Server minus client wall clock, from the batch's send time.

    One-way latency inflates it by the transit time (tens of ms on a good
    link); that is far below what a VAD timeline needs, and the raw client
    timestamps are kept for anyone who wants to refine it.
    """
    if sent_at is None:
        return None
    offset = int(received_at) - int(sent_at)
    if abs(offset) > MAX_PLAUSIBLE_OFFSET_MS:
        return None
    return offset


def record_batch(*, source: str, events: list, client_id: str | None = None,
                 sent_at: int | None = None, received_at: int | None = None,
                 default_session: str | None = None) -> dict:
    """Store a client batch. Returns {"stored": n, "clock_offset_ms": o}."""
    received_at = received_at if received_at is not None else now_ms()
    offset = clock_offset(_int(sent_at), received_at)
    stored = 0
    for item in list(events)[:MAX_BATCH]:
        if not isinstance(item, dict):
            continue
        name = str(item.get("event") or "").strip()
        if not name:
            continue
        client_ts = _int(item.get("ts"))
        if client_ts is not None and offset is not None:
            ts = client_ts + offset
        else:
            ts = received_at
        record(
            name, source=source, ts=ts, client_ts=client_ts,
            mono_ms=_int(item.get("mono_ms")), received_at=received_at,
            clock_offset_ms=offset, client_id=client_id,
            session=item.get("session") or default_session,
            utterance_id=item.get("utterance_id"),
            trace_id=item.get("trace_id"),
            duration_ms=_num(item.get("duration_ms")),
            level_db=_num(item.get("level_db")),
            peak_db=_num(item.get("peak_db")),
            text=item.get("text"),
            detail=item.get("detail") if isinstance(item.get("detail"), dict) else None,
        )
        stored += 1
    return {"stored": stored, "clock_offset_ms": offset}


def _row_dict(row) -> dict:
    out = dict(row)
    try:
        out["detail"] = json.loads(out.get("detail") or "{}")
    except (TypeError, ValueError):
        out["detail"] = {}
    return out


def query(*, session: str | None = None, since: int | None = None,
          until: int | None = None, utterance_id: str | None = None,
          trace_id: str | None = None, event: str | None = None,
          limit: int = 500) -> list[dict]:
    """Rows on the corrected timeline, oldest first."""
    clauses, params = [], []
    if session:
        clauses.append("session = ?"); params.append(session)
    if since is not None:
        clauses.append("ts >= ?"); params.append(int(since))
    if until is not None:
        clauses.append("ts <= ?"); params.append(int(until))
    if utterance_id:
        clauses.append("utterance_id = ?"); params.append(utterance_id)
    if trace_id:
        clauses.append("trace_id = ?"); params.append(trace_id)
    if event:
        clauses.append("event = ?"); params.append(event)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    limit = max(1, min(int(limit), 5000))
    rows = db.conn().execute(
        f"""SELECT * FROM (
                SELECT * FROM voice_events {where}
                 ORDER BY ts DESC, event_id DESC LIMIT ?)
            ORDER BY ts ASC, event_id ASC""",
        (*params, limit),
    ).fetchall()
    return [_row_dict(r) for r in rows]


def _first(events: list[dict], name: str) -> dict | None:
    for ev in events:
        if ev["event"] == name:
            return ev
    return None


def _last(events: list[dict], name: str) -> dict | None:
    for ev in reversed(events):
        if ev["event"] == name:
            return ev
    return None


def _delta(a: dict | None, b: dict | None) -> int | None:
    if a is None or b is None:
        return None
    return int(b["ts"]) - int(a["ts"])


def utterances(*, session: str | None = None, since: int | None = None,
               until: int | None = None, limit: int = 100) -> list[dict]:
    """One row per utterance: the timeline folded into latencies and flags.

    Rows carrying only a trace_id (send, playback) join the utterance whose
    transcript or send carried the same trace. Newest first.
    """
    # Server rows may lack a session (no focused agent when /transcribe ran),
    # so group first and filter utterances by any row's session afterwards.
    rows = query(since=since, until=until, limit=20000)
    by_utterance: dict[str, list[dict]] = {}
    trace_to_utterance: dict[str, str] = {}
    for ev in rows:
        uid = ev.get("utterance_id")
        if uid:
            by_utterance.setdefault(uid, []).append(ev)
            if ev.get("trace_id"):
                trace_to_utterance.setdefault(ev["trace_id"], uid)
    for ev in rows:
        if ev.get("utterance_id"):
            continue
        uid = trace_to_utterance.get(ev.get("trace_id") or "")
        if uid:
            by_utterance[uid].append(ev)
    out = []
    for uid, events in by_utterance.items():
        if session and not any(e.get("session") == session for e in events):
            continue
        events.sort(key=lambda e: (e["ts"], e["event_id"]))
        speech_start = _first(events, "speech_start")
        speech_end = _last(events, "speech_end")
        transcript = _first(events, "transcript")
        send = _first(events, "send")
        play_start = _first(events, "play_start")
        play_end = _last(events, "play_end")
        upload_start = _first(events, "upload_start")
        upload_end = _last(events, "upload_end")
        level = _last(events, "level")
        silence = _first(events, "silence")
        corrupt = [e for e in events if e["event"] == "corrupt"]
        reasons: list[str] = []
        for c in corrupt:
            got = c["detail"].get("reasons") or c["detail"].get("reason")
            if isinstance(got, list):
                reasons.extend(str(r) for r in got)
            elif got:
                reasons.append(str(got))
        speech_ms = (speech_end or {}).get("duration_ms")
        if speech_ms is None:
            speech_ms = _delta(speech_start, speech_end)
        level_row = speech_end if speech_end and speech_end.get("level_db") is not None else level
        out.append({
            "utterance_id": uid,
            "session": next((e["session"] for e in events if e.get("session")), None),
            "trace_id": next((e["trace_id"] for e in events if e.get("trace_id")), None),
            "started_at": int((speech_start or events[0])["ts"]),
            "ended_at": int(speech_end["ts"]) if speech_end else None,
            "speech_ms": speech_ms,
            "silence_before_ms": (silence or {}).get("duration_ms")
                if silence else (speech_start or {}).get("detail", {}).get("silence_ms"),
            "level_db": (level_row or {}).get("level_db"),
            "peak_db": (level_row or {}).get("peak_db"),
            "upload_ms": (upload_end or {}).get("duration_ms")
                if upload_end and upload_end.get("duration_ms") is not None
                else _delta(upload_start, upload_end),
            "stt_ms": (transcript or {}).get("duration_ms"),
            "transcript": (transcript or {}).get("text"),
            "speech_to_text_ms": _delta(speech_end, transcript),
            "text_to_send_ms": _delta(transcript, send),
            "send_to_play_ms": _delta(send, play_start),
            "speech_to_play_ms": _delta(speech_end, play_start),
            "play_ms": _delta(play_start, play_end),
            "barge_in": any(e["event"] == "barge_in" for e in events),
            "corrupt": reasons,
            "events": len(events),
        })
    out.sort(key=lambda r: r["started_at"], reverse=True)
    return out[: max(1, min(int(limit), 2000))]
