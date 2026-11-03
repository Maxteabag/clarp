"""Schema of lib.podcast_history, moved here verbatim (docs/architecture/s1-layering.md 3.1)."""
from __future__ import annotations

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
 context_json TEXT NOT NULL, configuration_json TEXT NOT NULL,
 model TEXT NOT NULL, voice TEXT NOT NULL, owner_instance TEXT NOT NULL,
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
 image_ids_json TEXT NOT NULL,
 created_at INTEGER NOT NULL, payload_hash TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_podcast_feedback_target ON podcast_feedback(target_artifact_id,created_at);
CREATE INDEX IF NOT EXISTS idx_podcast_feedback_conversation ON podcast_feedback(conversation_id,created_at);
"""
