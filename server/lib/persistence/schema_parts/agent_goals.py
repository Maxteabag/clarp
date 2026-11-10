"""Schema of lib.agent_goals, moved here verbatim (docs/architecture/s1-layering.md 3.1)."""
from __future__ import annotations

SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_goals (
    agent_id TEXT PRIMARY KEY,
    session TEXT NOT NULL,
    backend TEXT NOT NULL,
    objective TEXT NOT NULL,
    status TEXT NOT NULL,
    token_budget INTEGER,
    tokens_used INTEGER NOT NULL DEFAULT 0,
    time_used_seconds INTEGER NOT NULL DEFAULT 0,
    native INTEGER NOT NULL DEFAULT 1,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
"""
