"""Schema of lib.goal_ledger, moved here verbatim (docs/architecture/s1-layering.md 3.1)."""
from __future__ import annotations

SCHEMA = """
CREATE TABLE IF NOT EXISTS goal_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id TEXT NOT NULL,
    at INTEGER NOT NULL,
    kind TEXT NOT NULL,
    subject TEXT NOT NULL DEFAULT 'goal',
    actor_kind TEXT NOT NULL CHECK (actor_kind IN
        ('owner','user','delegate','system','peer','legacy','unattributed')),
    actor_agent_id TEXT NOT NULL DEFAULT '',
    delegation_id TEXT NOT NULL DEFAULT '',
    basis TEXT NOT NULL DEFAULT '' CHECK (basis IN ('','observed','claimed','inferred')),
    source_refs TEXT NOT NULL DEFAULT '[]',
    reason TEXT NOT NULL DEFAULT '',
    prior_json TEXT,
    new_json TEXT,
    plan_revision INTEGER,
    idempotency_key TEXT NOT NULL,
    UNIQUE (plan_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_goal_events_plan ON goal_events(plan_id, event_id);
CREATE TRIGGER IF NOT EXISTS goal_events_no_update BEFORE UPDATE ON goal_events
BEGIN SELECT RAISE(ABORT, 'goal events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS goal_events_no_delete BEFORE DELETE ON goal_events
BEGIN SELECT RAISE(ABORT, 'goal events are append-only'); END;

CREATE TABLE IF NOT EXISTS goal_subgoals (
    plan_id TEXT NOT NULL,
    subgoal_id TEXT NOT NULL,
    title TEXT NOT NULL,
    intent TEXT NOT NULL DEFAULT '',
    criteria_json TEXT NOT NULL DEFAULT '[]',
    owner TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL CHECK (status IN
        ('proposed','active','blocked','done','retired','unknown')),
    evidence_json TEXT NOT NULL DEFAULT '[]',
    current_action TEXT NOT NULL DEFAULT '',
    next_dependency TEXT NOT NULL DEFAULT '',
    last_observed_at INTEGER,
    revision INTEGER NOT NULL DEFAULT 1,
    created_actor_kind TEXT NOT NULL,
    created_agent_id TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (plan_id, subgoal_id)
);
CREATE TRIGGER IF NOT EXISTS goal_subgoals_no_delete BEFORE DELETE ON goal_subgoals
BEGIN SELECT RAISE(ABORT, 'subgoals are retired, never deleted'); END;

CREATE TABLE IF NOT EXISTS goal_delegations (
    delegation_id TEXT PRIMARY KEY,
    principal_agent_id TEXT NOT NULL,
    delegate_agent_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active','stopped')),
    token_hash TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    created_reason TEXT NOT NULL,
    stopped_at INTEGER,
    stopped_reason TEXT NOT NULL DEFAULT '',
    message_through INTEGER NOT NULL DEFAULT 0,
    goal_event_through INTEGER NOT NULL DEFAULT 0,
    dispatched_message_through INTEGER NOT NULL DEFAULT 0,
    dispatched_goal_event_through INTEGER NOT NULL DEFAULT 0,
    last_wake_id TEXT NOT NULL DEFAULT '',
    last_wake_at INTEGER,
    last_applied_at INTEGER,
    last_source_at INTEGER,
    last_heartbeat_at INTEGER,
    last_error TEXT NOT NULL DEFAULT '',
    job_handle TEXT NOT NULL DEFAULT '',
    unapplied_since INTEGER
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_goal_delegations_principal
    ON goal_delegations(principal_agent_id) WHERE status = 'active';
CREATE UNIQUE INDEX IF NOT EXISTS idx_goal_delegations_delegate
    ON goal_delegations(delegate_agent_id) WHERE status = 'active';
"""
