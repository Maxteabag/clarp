"""Schema of lib.html_report_revisions, moved here verbatim (docs/architecture/s1-layering.md 3.1)."""
from __future__ import annotations

SCHEMA = '''CREATE TABLE IF NOT EXISTS html_report_revisions (
 artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id),
 version TEXT NOT NULL,
 payload_json TEXT NOT NULL,
 title TEXT NOT NULL,
 summary TEXT NOT NULL,
 created_at INTEGER NOT NULL,
 PRIMARY KEY(artifact_id, version)
);'''
