"""Schema of lib.oracle_memory, moved here verbatim (docs/architecture/s1-layering.md 3.1)."""
from __future__ import annotations

SCHEMA = """
CREATE TABLE IF NOT EXISTS oracle_threads (
 thread_id TEXT PRIMARY KEY, owner_principal TEXT NOT NULL, contact TEXT NOT NULL,
 active_connection TEXT NOT NULL DEFAULT '', revision INTEGER NOT NULL DEFAULT 0,
 state_json TEXT NOT NULL DEFAULT '{}', created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS oracle_threads_owner ON oracle_threads(owner_principal,contact,updated_at);
CREATE TABLE IF NOT EXISTS oracle_context_notifications (
 thread_id TEXT NOT NULL REFERENCES oracle_threads(thread_id),
 owner_principal TEXT NOT NULL,
 source_kind TEXT NOT NULL,
 source_id TEXT NOT NULL,
 reference_ts INTEGER NOT NULL,
 stale INTEGER NOT NULL DEFAULT 0,
 sent_at INTEGER NOT NULL,
 PRIMARY KEY(thread_id,source_kind,source_id)
);
CREATE INDEX IF NOT EXISTS oracle_context_notifications_owner
 ON oracle_context_notifications(owner_principal,thread_id,sent_at);
CREATE TABLE IF NOT EXISTS oracle_observations (
 sequence INTEGER PRIMARY KEY AUTOINCREMENT, thread_id TEXT NOT NULL REFERENCES oracle_threads(thread_id),
 source_key TEXT NOT NULL, event_json TEXT NOT NULL, created_at INTEGER NOT NULL,
 UNIQUE(thread_id,source_key)
);
CREATE TABLE IF NOT EXISTS oracle_thread_work (
 thread_id TEXT NOT NULL REFERENCES oracle_threads(thread_id), operation_id TEXT NOT NULL,
 created_at INTEGER NOT NULL, PRIMARY KEY(thread_id,operation_id)
);
CREATE TABLE IF NOT EXISTS oracle_admissions (
 thread_id TEXT NOT NULL REFERENCES oracle_threads(thread_id), revision INTEGER NOT NULL,
 action_index INTEGER NOT NULL, call_id TEXT NOT NULL, tool TEXT NOT NULL,
 arguments_json TEXT NOT NULL, result_json TEXT, status TEXT NOT NULL,
 created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
 PRIMARY KEY(thread_id,revision,action_index)
);
CREATE TABLE IF NOT EXISTS oracle_user_context (
 thread_id TEXT NOT NULL REFERENCES oracle_threads(thread_id), context_id TEXT NOT NULL,
 kind TEXT NOT NULL, text TEXT NOT NULL, mime_type TEXT, image BLOB,
 captured_at INTEGER, added_at INTEGER NOT NULL, removed_at INTEGER,
 content_hash TEXT NOT NULL, PRIMARY KEY(thread_id,context_id)
);
"""
