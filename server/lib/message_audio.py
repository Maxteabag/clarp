"""Read retained speech for one canonical assistant message; never synthesize."""
from __future__ import annotations

import datetime
import json
import pathlib
import re
from urllib.parse import quote

from . import agents
from .db import conn, now_ms
from .voice_markup import spoken_chunks_for_tts, spoken_for_tts

_SPEAK = re.compile(r"<speak\b[^>]*>(.*?)</speak>", re.I | re.S)


def retained_mp3_paths(*, audio_dir: pathlib.Path, max_age_ms: int, max_bytes: int) -> set[str]:
    """Protect a bounded recent reply cache; previews/heralds keep normal expiry."""
    root = audio_dir.resolve()
    rows = conn().execute(
        "SELECT c.path,MAX(q.enqueued_at) AS latest FROM clips c "
        "JOIN tts_queue q ON q.clip_id=c.clip_id AND q.agent_id=c.agent_id "
        "JOIN agents a ON a.agent_id=c.agent_id AND a.deleted_at IS NULL "
        "WHERE q.status='done' AND q.enqueued_at>=? AND c.path LIKE '%.mp3' "
        "AND COALESCE(c.producer_status,'complete')='complete' "
        "GROUP BY c.clip_id ORDER BY latest DESC,c.clip_id DESC LIMIT 10000",
        (now_ms() - max(0, max_age_ms),),
    ).fetchall()
    remaining = max(0, max_bytes)
    protected = set()
    for row in rows:
        path = pathlib.Path(row["path"]).resolve()
        if root not in path.parents:
            continue
        try:
            if not path.is_file():
                continue
            size = path.stat().st_size
        except OSError:
            continue
        if size <= remaining:
            protected.add(str(path))
            remaining -= size
    return protected


def _normalized(text: str) -> str:
    return " ".join(spoken_for_tts(text).split())


def retained_events(*, session: str, message_id: str,
                    audio_dir: pathlib.Path) -> list[dict]:
    agent = agents.get_by_session(session)
    if not agent:
        return []
    message = conn().execute(
        "SELECT text,timestamp,updated_at,kind FROM messages "
        "WHERE message_id=? AND agent_id=? AND role='assistant'",
        (message_id, agent["agent_id"]),
    ).fetchone()
    if not message or message["kind"] == "live":
        return []
    chunks = [chunk for block in _SPEAK.findall(message["text"])
              for chunk in spoken_chunks_for_tts(block)]
    if not chunks or len(chunks) > 128:
        return []
    try:
        stamp = datetime.datetime.fromisoformat(str(message["timestamp"]).replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=datetime.timezone.utc)
        target_time = int(stamp.timestamp() * 1000)
    except (ValueError, TypeError, OverflowError):
        target_time = int(message["updated_at"])
    rows = conn().execute(
        "SELECT q.queue_id,q.text,q.trace_id,q.enqueued_at,c.clip_id,c.path,c.created_at "
        "FROM tts_queue q JOIN clips c ON c.clip_id=q.clip_id AND c.agent_id=q.agent_id "
        "WHERE q.agent_id=? AND q.status='done' "
        "AND COALESCE(c.producer_status,'complete')='complete' "
        "ORDER BY q.enqueued_at DESC LIMIT 2000", (agent["agent_id"],),
    ).fetchall()
    prefix = re.compile(rf"^{re.escape(agent['persona'])} here\.\s+", re.I)
    indexed: dict[str, list] = {}
    for row in rows:
        text = _normalized(row["text"])
        for candidate in {text, prefix.sub("", text, count=1)}:
            indexed.setdefault(candidate, []).append(row)
    selected = []
    trace = None
    previous_id = 0
    for chunk in chunks:
        candidates = indexed.get(_normalized(chunk), [])
        if trace:
            candidates = [row for row in candidates if row["trace_id"] == trace]
        if not candidates:
            return []  # Never replay only part of the selected speech.
        ordered = [row for row in candidates if row["queue_id"] > previous_id]
        row = min(ordered or candidates,
                  key=lambda row: (abs(row["enqueued_at"] - target_time), row["queue_id"]))
        if not selected:
            trace = row["trace_id"]
        previous_id = row["queue_id"]
        selected.append(row)
    root = audio_dir.resolve()
    events = []
    seen = set()
    for row in selected:
        path = pathlib.Path(row["path"]).resolve()
        if not path.is_file() or root not in path.parents:
            return []
        clip_id = row["clip_id"]
        if clip_id in seen:
            continue
        seen.add(clip_id)
        event = {"type": "audio", "clip_id": clip_id, "session": session,
                 "agent_id": agent["agent_id"], "persona": agent["persona"],
                 "trace_id": row["trace_id"], "ts": row["created_at"],
                 "url": "/audio/" + quote(path.name)}
        if path.suffix.lower() == ".pcm":
            saved = conn().execute(
                "SELECT payload FROM sse_events WHERE type='audio' AND session=? "
                "AND CAST(json_extract(payload,'$.clip_id') AS INTEGER)=? "
                "ORDER BY event_id DESC LIMIT 1", (session, clip_id),
            ).fetchone()
            try:
                audio_format = json.loads(saved["payload"])["audio_format"] if saved else None
            except (ValueError, KeyError, TypeError):
                audio_format = None
            if not isinstance(audio_format, dict):
                return []
            event.update(url=f"/clips/{clip_id}/stream", stream_url=f"/clips/{clip_id}/stream",
                         delivery="raw-pcm", audio_format=audio_format)
        events.append(event)
    return events
