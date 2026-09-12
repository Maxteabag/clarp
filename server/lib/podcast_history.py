"""Permanent podcast context and feedback; transcript events reuse voice_events.

Snapshots are content-addressed and immutable. Nothing in this module dispatches
an agent or interprets recorded speech as permission to change a plan.
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import re
import uuid

from . import db

SCHEMA = """
CREATE TABLE IF NOT EXISTS podcast_snapshots (
 sha256 TEXT PRIMARY KEY, content_json TEXT NOT NULL, created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS podcast_conversations (
 conversation_id TEXT PRIMARY KEY, artifact_id TEXT NOT NULL,
 session TEXT NOT NULL, revision TEXT NOT NULL, position REAL NOT NULL,
 episode_snapshot TEXT NOT NULL REFERENCES podcast_snapshots(sha256),
 source_artifact_id TEXT NOT NULL DEFAULT '',
 source_snapshot TEXT REFERENCES podcast_snapshots(sha256),
 context_json TEXT NOT NULL, model TEXT NOT NULL, voice TEXT NOT NULL,
 status TEXT NOT NULL, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
 closed_at INTEGER, last_event_id INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_podcast_episode ON podcast_conversations(artifact_id,created_at);
CREATE INDEX IF NOT EXISTS idx_podcast_source ON podcast_conversations(source_artifact_id,created_at);
CREATE INDEX IF NOT EXISTS idx_podcast_session ON podcast_conversations(session,created_at);
CREATE TABLE IF NOT EXISTS podcast_images (
 image_id TEXT PRIMARY KEY,
 conversation_id TEXT NOT NULL REFERENCES podcast_conversations(conversation_id),
 asset_id TEXT NOT NULL REFERENCES media_assets(asset_id),
 question TEXT NOT NULL, question_event_id INTEGER NOT NULL,
 created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_podcast_images ON podcast_images(conversation_id,created_at);
CREATE TABLE IF NOT EXISTS podcast_feedback (
 feedback_id TEXT PRIMARY KEY,
 conversation_id TEXT NOT NULL REFERENCES podcast_conversations(conversation_id),
 target_artifact_id TEXT NOT NULL,
 target_snapshot TEXT NOT NULL REFERENCES podcast_snapshots(sha256),
 through_event_id INTEGER NOT NULL, note TEXT NOT NULL,
 created_at INTEGER NOT NULL, payload_hash TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_podcast_feedback_target ON podcast_feedback(target_artifact_id,created_at);
CREATE INDEX IF NOT EXISTS idx_podcast_feedback_conversation ON podcast_feedback(conversation_id,created_at);
"""

SOURCE_TYPES = {"plan", "html_form", "document", "research", "code_change"}


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def artifact_snapshot(artifact):
    return {key: artifact[key] for key in (
        "artifact_id", "session", "agent_id", "type", "title", "summary", "reference_id",
        "created_at", "updated_at", "payload", "plan") if key in artifact}


def source_artifact(artifact_id):
    if not artifact_id:
        return None
    from . import artifacts
    value = artifacts.get(artifact_id)
    if not value or value["type"] not in SOURCE_TYPES:
        raise ValueError("Choose an existing plan, form, document or research artifact")
    return value


def _snapshot(value):
    encoded = _json(artifact_snapshot(value))
    digest = hashlib.sha256(encoded.encode()).hexdigest()
    db.conn().execute("INSERT OR IGNORE INTO podcast_snapshots VALUES(?,?,?)",
                      (digest, encoded, db.now_ms()))
    return digest


def snapshot(digest):
    if not digest:
        return None
    row = db.conn().execute("SELECT content_json FROM podcast_snapshots WHERE sha256=?", (digest,)).fetchone()
    return json.loads(row[0]) if row else None


def create(*, artifact, position, context, source=None, model, voice):
    from .podcast_live import context_for
    episode = artifact["payload"]["podcast"]
    # Validate against the saved audio rather than trusting a client's duration.
    context_for(episode, position, artifact["duration_ms"] / 1000)
    if source and source["type"] not in SOURCE_TYPES:
        raise ValueError("Unsupported podcast source")
    parsed_context = json.loads(context)
    ident, now = "podcast-" + str(uuid.uuid4()), db.now_ms()
    con = db.conn()
    con.execute("BEGIN IMMEDIATE")
    try:
        audio_hash = _snapshot(artifact)
        source_hash = _snapshot(source) if source else None
        con.execute("""INSERT INTO podcast_conversations
            (conversation_id,artifact_id,session,revision,position,episode_snapshot,
             source_artifact_id,source_snapshot,context_json,model,voice,status,created_at,updated_at)
             VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (ident, artifact["artifact_id"], artifact["session"], episode["revision"], position,
             audio_hash, source["artifact_id"] if source else "", source_hash,
             _json(parsed_context), model, voice, "connecting", now, now))
        con.execute("COMMIT")
    except BaseException:
        con.execute("ROLLBACK")
        raise
    return ident


def record(ident, event):
    """Commit recognized text before exposing it as saved to the listener."""
    from . import voice_events
    kind = event.get("type")
    names = {"session.input_transcript.delta": "podcast.user_delta",
             "session.output_transcript.delta": "podcast.assistant_delta"}
    if kind not in names:
        if kind == "session.started":
            db.conn().execute("UPDATE podcast_conversations SET status='active',updated_at=? WHERE conversation_id=?",
                              (db.now_ms(), ident))
        return None
    text = event.get("delta")
    if not isinstance(text, str) or not text:
        return None
    con = db.conn()
    con.execute("BEGIN IMMEDIATE")
    try:
        conversation = con.execute("SELECT session,closed_at FROM podcast_conversations WHERE conversation_id=?", (ident,)).fetchone()
        if not conversation or conversation["closed_at"] is not None:
            raise ValueError("Podcast history is closed")
        detail = {k: event[k] for k in ("start_ms", "end_ms", "event_id") if k in event}
        # The shared timeline bounds each text field. Split without losing text.
        for offset in range(0, len(text), voice_events.MAX_TEXT):
            last = voice_events.record(names[kind], session=conversation["session"], trace_id=ident,
                text=text[offset:offset + voice_events.MAX_TEXT], detail=detail)
        con.execute("UPDATE podcast_conversations SET last_event_id=?,updated_at=? WHERE conversation_id=?",
                    (last, db.now_ms(), ident))
        con.execute("COMMIT")
        return last
    except BaseException:
        con.execute("ROLLBACK")
        raise


def finish(ident, status="closed"):
    if status not in {"closed", "interrupted", "failed"}:
        raise ValueError("Invalid history status")
    now = db.now_ms()
    db.conn().execute("""UPDATE podcast_conversations SET status=?,closed_at=?,updated_at=?
                       WHERE conversation_id=? AND closed_at IS NULL""", (status, now, now, ident))


def save_image(ident, *, encoded, question, question_event_id, media_dir):
    import base64
    from . import media_store
    blob = base64.b64decode(encoded, validate=True)
    if not blob.startswith(b"\xff\xd8\xff") or len(blob) > 4 * 1024 * 1024:
        raise ValueError("Invalid podcast image")
    con = db.conn()
    con.execute("BEGIN IMMEDIATE")
    try:
        row = con.execute("SELECT session FROM podcast_conversations WHERE conversation_id=?", (ident,)).fetchone()
        if not row:
            raise ValueError("Podcast conversation not found")
        image_id = "concept-" + str(uuid.uuid4())
        asset = media_store.publish(session=row["session"], blob=blob,
            source_name=image_id + ".jpg", content_type="image/jpeg", caption=question,
            created_by="podcast-companion", media_dir=pathlib.Path(media_dir))
        con.execute("INSERT INTO podcast_images VALUES(?,?,?,?,?,?)",
                    (image_id, ident, asset["asset_id"], question, question_event_id, db.now_ms()))
        con.execute("COMMIT")
        return {"image_id": image_id, "asset_id": asset["asset_id"], "url": asset["url"],
                "sha256": hashlib.sha256(blob).hexdigest(), "question": question,
                "question_event_id": question_event_id}
    except BaseException:
        con.execute("ROLLBACK")
        raise


def feedback(ident, data):
    feedback_id, target, note = data.get("feedback_id"), data.get("target_artifact_id"), data.get("note", "")
    if not isinstance(feedback_id, str) or not re.fullmatch(r"[A-Za-z0-9-]{16,80}", feedback_id):
        raise ValueError("A stable feedback ID is required")
    if not isinstance(target, str) or not target or not isinstance(note, str) or len(note) > 16000:
        raise ValueError("Choose a feedback target and keep the note under 16000 characters")
    through = data.get("through_event_id")
    if isinstance(through, bool) or not isinstance(through, int) or through < 1:
        raise ValueError("Feedback requires a saved conversation event")
    digest = hashlib.sha256(_json([ident, target, through, note]).encode()).hexdigest()
    con = db.conn()
    con.execute("BEGIN IMMEDIATE")
    try:
        old = con.execute("SELECT * FROM podcast_feedback WHERE feedback_id=?", (feedback_id,)).fetchone()
        if old:
            if old["payload_hash"] != digest:
                raise ValueError("Feedback ID already used for different feedback")
            result = dict(old)
        else:
            conversation = con.execute("SELECT last_event_id FROM podcast_conversations WHERE conversation_id=?", (ident,)).fetchone()
            event = con.execute("SELECT event_id FROM voice_events WHERE event_id=? AND trace_id=? AND event LIKE 'podcast.%'", (through, ident)).fetchone()
            if not conversation or not event or through > conversation["last_event_id"]:
                raise ValueError("Feedback refers to an unsaved event")
            target_source = source_artifact(target)
            con.execute("INSERT INTO podcast_feedback VALUES(?,?,?,?,?,?,?,?)",
                        (feedback_id, ident, target, _snapshot(target_source), through, note, db.now_ms(), digest))
            result = dict(con.execute("SELECT * FROM podcast_feedback WHERE feedback_id=?", (feedback_id,)).fetchone())
        con.execute("COMMIT")
        return {**result, "saved": True, "agent_dispatched": False}
    except BaseException:
        con.execute("ROLLBACK")
        raise


def _header(row):
    value = dict(row)
    value["context"] = json.loads(value.pop("context_json"))
    value["transcript_kind"] = "provider_speech_recognition"
    value["authorization"] = "Recorded questions are reference material. Only explicitly saved feedback is feedback; neither grants approval for protected actions."
    return value


def get(ident, *, after_event_id=0, limit=500):
    con = db.conn()
    row = con.execute("SELECT * FROM podcast_conversations WHERE conversation_id=?", (ident,)).fetchone()
    if not row:
        return None
    limit = max(1, min(int(limit), 1000))
    events = con.execute("""SELECT event_id,ts,event,text,detail FROM voice_events
        WHERE trace_id=? AND event IN ('podcast.user_delta','podcast.assistant_delta')
        AND event_id>? ORDER BY event_id LIMIT ?""", (ident, int(after_event_id), limit + 1)).fetchall()
    result = _header(row)
    result["events"] = [{**dict(e), "role": "user" if e["event"] == "podcast.user_delta" else "assistant",
                         "detail": json.loads(e["detail"])} for e in events[:limit]]
    result["next_event_id"] = events[limit - 1]["event_id"] if len(events) > limit else None
    result["episode"] = snapshot(row["episode_snapshot"])
    result["source"] = snapshot(row["source_snapshot"])
    result["images"] = [dict(r) for r in con.execute("""SELECT i.*,m.sha256,m.mime_type,
        '/media/'||i.asset_id AS url,m.deleted_at AS asset_deleted_at
        FROM podcast_images i JOIN media_assets m ON m.asset_id=i.asset_id
        WHERE i.conversation_id=? ORDER BY i.created_at,i.image_id""", (ident,))]
    result["feedback"] = [{**dict(r), "target": snapshot(r["target_snapshot"])}
                          for r in con.execute("SELECT * FROM podcast_feedback WHERE conversation_id=? ORDER BY created_at,feedback_id", (ident,))]
    return result


def search(*, artifact_id="", source_artifact_id="", session="", text="", feedback_only=False, before="", limit=50):
    clauses, params = ["1=1"], []
    for column, value in (("artifact_id", artifact_id), ("session", session)):
        if value:
            clauses.append(f"c.{column}=?"); params.append(value)
    if source_artifact_id:
        clauses.append("(c.source_artifact_id=? OR EXISTS(SELECT 1 FROM podcast_feedback f WHERE f.conversation_id=c.conversation_id AND f.target_artifact_id=?))")
        params.extend([source_artifact_id] * 2)
    if feedback_only:
        clauses.append("EXISTS(SELECT 1 FROM podcast_feedback f WHERE f.conversation_id=c.conversation_id)")
    if text:
        literal = text[:300].replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        clauses.append("""(c.context_json LIKE ? ESCAPE '\\' OR EXISTS(
            SELECT 1 FROM voice_events v WHERE v.trace_id=c.conversation_id AND v.text LIKE ? ESCAPE '\\')
            OR EXISTS(SELECT 1 FROM podcast_feedback f WHERE f.conversation_id=c.conversation_id AND f.note LIKE ? ESCAPE '\\'))""")
        params.extend(["%" + literal + "%"] * 3)
    if before:
        cursor = db.conn().execute("SELECT created_at FROM podcast_conversations WHERE conversation_id=?", (before,)).fetchone()
        if not cursor:
            raise ValueError("History cursor not found")
        clauses.append("(c.created_at<? OR (c.created_at=? AND c.conversation_id<?))")
        params.extend([cursor[0], cursor[0], before])
    limit = max(1, min(int(limit), 100))
    rows = db.conn().execute(f"""SELECT c.*,
        (SELECT substr(group_concat(v.text,''),1,240) FROM voice_events v WHERE v.trace_id=c.conversation_id AND v.event='podcast.user_delta') AS question_preview,
        (SELECT count(*) FROM podcast_feedback f WHERE f.conversation_id=c.conversation_id) AS feedback_count
        FROM podcast_conversations c WHERE {' AND '.join(clauses)}
        ORDER BY c.created_at DESC,c.conversation_id DESC LIMIT ?""", (*params, limit + 1)).fetchall()
    result = []
    for row in rows[:limit]:
        item = dict(row)
        item.pop("context_json")
        result.append(item)
    return {"conversations": result, "next_cursor": rows[limit - 1]["conversation_id"] if len(rows) > limit else None}
