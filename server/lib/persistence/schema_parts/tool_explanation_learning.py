"""Schema of lib.tool_explanation_learning, moved here verbatim (docs/architecture/s1-layering.md 3.1)."""
from __future__ import annotations

SCHEMA = """
CREATE TABLE IF NOT EXISTS tool_explanation_learned (
    signature TEXT NOT NULL,
    level INTEGER NOT NULL,
    template_text TEXT NOT NULL DEFAULT '',
    template_id TEXT NOT NULL DEFAULT '',
    argument_index INTEGER,
    slot_names TEXT NOT NULL DEFAULT '[]',
    program TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL DEFAULT '',
    producer TEXT NOT NULL,
    parameterised INTEGER NOT NULL DEFAULT 1,
    confidence REAL,
    prompt_version INTEGER NOT NULL,
    templates_version INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    last_hit_at INTEGER,
    hits INTEGER NOT NULL DEFAULT 0,
    verified INTEGER NOT NULL DEFAULT 0,
    source_signature TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (signature, level)
);
CREATE INDEX IF NOT EXISTS tool_explanation_learned_program ON tool_explanation_learned(program, level);
CREATE TABLE IF NOT EXISTS tool_explanation_decisions (
    id INTEGER PRIMARY KEY,
    at INTEGER NOT NULL,
    agent_id TEXT NOT NULL DEFAULT '',
    signature TEXT NOT NULL DEFAULT '',
    tier TEXT NOT NULL,
    level INTEGER NOT NULL DEFAULT 0,
    parts INTEGER NOT NULL DEFAULT 1,
    jev_confidence REAL,
    latency_ms INTEGER NOT NULL DEFAULT 0,
    model TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS tool_explanation_decisions_at ON tool_explanation_decisions(at);
"""
