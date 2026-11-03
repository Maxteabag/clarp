"""Schema of lib.provider_background_jobs, moved here verbatim (docs/architecture/s1-layering.md 3.1)."""
from __future__ import annotations

SCHEMA = """CREATE TABLE IF NOT EXISTS provider_job_cursors (
 agent_id TEXT NOT NULL, native_id TEXT NOT NULL, path TEXT NOT NULL,
 file_identity TEXT NOT NULL, position INTEGER NOT NULL,
 discarding INTEGER NOT NULL DEFAULT 0,
 action_offset INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(agent_id,native_id))"""


TURN_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS provider_turns (
 turn_token TEXT PRIMARY KEY, agent_id TEXT NOT NULL, provider TEXT NOT NULL,
 pid INTEGER, start_token TEXT NOT NULL DEFAULT '',
 started_at INTEGER NOT NULL, exited_at INTEGER)""",
    """CREATE INDEX IF NOT EXISTS idx_provider_turns_open
 ON provider_turns(started_at) WHERE exited_at IS NULL""",
    """CREATE TABLE IF NOT EXISTS provider_task_turns (
 agent_id TEXT NOT NULL, native_id TEXT NOT NULL, tool_use_id TEXT NOT NULL,
 turn_token TEXT NOT NULL, recorded_at INTEGER NOT NULL,
 PRIMARY KEY(agent_id,native_id,tool_use_id))""",
    """CREATE INDEX IF NOT EXISTS idx_provider_task_turns_token
 ON provider_task_turns(turn_token)""",
    """CREATE INDEX IF NOT EXISTS idx_background_jobs_agent_active
 ON background_jobs(agent_id) WHERE terminal_at IS NULL""",
)
