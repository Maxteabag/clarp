"""Schema of lib.html_form_events, moved here verbatim (docs/architecture/s1-layering.md 3.1)."""
from __future__ import annotations

SCHEMA = '''CREATE TABLE IF NOT EXISTS html_form_event_config (
 artifact_id TEXT PRIMARY KEY REFERENCES artifacts(artifact_id),
 draft_key TEXT
);
CREATE TABLE IF NOT EXISTS html_form_events (
 seq INTEGER PRIMARY KEY AUTOINCREMENT,
 artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id),
 version TEXT NOT NULL,
 event_id TEXT NOT NULL,
 client_seq INTEGER NOT NULL,
 client_at INTEGER NOT NULL,
 received_at INTEGER NOT NULL,
 event_json TEXT NOT NULL,
 payload_hash TEXT NOT NULL,
 UNIQUE(artifact_id,event_id)
);
CREATE INDEX IF NOT EXISTS idx_html_form_events_artifact_seq ON html_form_events(artifact_id,seq);
CREATE TRIGGER IF NOT EXISTS html_form_events_no_update BEFORE UPDATE ON html_form_events
 BEGIN SELECT RAISE(ABORT,'Form events are immutable'); END;
CREATE TRIGGER IF NOT EXISTS html_form_events_no_delete BEFORE DELETE ON html_form_events
 BEGIN SELECT RAISE(ABORT,'Form events are append-only'); END;'''
