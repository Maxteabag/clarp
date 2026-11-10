"""Schema of lib.tool_explanation_mappings, moved here verbatim (docs/architecture/s1-layering.md 3.1)."""
from __future__ import annotations

SCHEMA = """
CREATE TABLE IF NOT EXISTS tool_explanation_mappings (
    signature TEXT PRIMARY KEY,
    template_id TEXT NOT NULL,
    library_version INTEGER NOT NULL,
    status TEXT NOT NULL,
    evidence_json TEXT NOT NULL DEFAULT '[]',
    conflicts INTEGER NOT NULL DEFAULT 0,
    reason TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    promoted_at INTEGER
);
CREATE INDEX IF NOT EXISTS tool_explanation_mappings_status ON tool_explanation_mappings(status, library_version);
"""
