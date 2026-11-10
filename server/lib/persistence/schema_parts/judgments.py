"""Schema of lib.judgments, moved here verbatim (docs/architecture/s1-layering.md 3.1)."""
from __future__ import annotations

SCHEMA = """
CREATE TABLE IF NOT EXISTS judgment_decisions (
    judgment_id INTEGER PRIMARY KEY AUTOINCREMENT,
    site TEXT NOT NULL,
    trace_id TEXT,
    question_ids TEXT NOT NULL DEFAULT '',
    outcome TEXT NOT NULL DEFAULT '',
    answers_json TEXT NOT NULL DEFAULT '{}',
    latency_ms INTEGER NOT NULL DEFAULT 0,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    fallback_used INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_judgment_decisions_site
    ON judgment_decisions(site, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_judgment_decisions_created
    ON judgment_decisions(created_at DESC);
"""
