"""Schema of lib.model_fallbacks, moved here verbatim (docs/architecture/s1-layering.md 3.1)."""
from __future__ import annotations

SCHEMA = """
CREATE TABLE IF NOT EXISTS model_fallback_attempts (
 agent_id TEXT NOT NULL REFERENCES agents(agent_id), request_id TEXT NOT NULL,
 attempt INTEGER NOT NULL, backend TEXT NOT NULL, model TEXT NOT NULL, effort TEXT NOT NULL,
 status TEXT NOT NULL, reason TEXT NOT NULL, result_json TEXT,
 started_at INTEGER NOT NULL, finished_at INTEGER, runtime_id INTEGER,
 PRIMARY KEY(agent_id,request_id,attempt)
);
"""
