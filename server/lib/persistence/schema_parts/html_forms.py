"""Schema of lib.html_forms, moved here verbatim (docs/architecture/s1-layering.md 3.1)."""
from __future__ import annotations

SCHEMA = '''CREATE TABLE IF NOT EXISTS form_submissions (
 submission_id TEXT PRIMARY KEY,
 artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id),
 version TEXT NOT NULL,
 session TEXT NOT NULL,
 answers_json TEXT NOT NULL,
 payload_hash TEXT NOT NULL,
 prompt TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending',
 created_at INTEGER NOT NULL,
 delivered_at INTEGER,
 synthesize_audio INTEGER NOT NULL DEFAULT 0 CHECK (synthesize_audio IN (0,1))
);
CREATE INDEX IF NOT EXISTS idx_form_submission_pending ON form_submissions(status,created_at);'''
