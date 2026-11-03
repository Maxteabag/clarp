"""Schema of lib.janitor_autonomy, moved here verbatim (docs/architecture/s1-layering.md 3.1)."""
from __future__ import annotations

SCHEMA='''CREATE TABLE IF NOT EXISTS janitor_continuity (
 target_id TEXT PRIMARY KEY, snapshot TEXT NOT NULL, run_id TEXT NOT NULL,
 decision_json TEXT NOT NULL, due_at INTEGER NOT NULL, status TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS janitor_quota_receipts (
 receipt_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, payload_json TEXT NOT NULL,
 delivery_json TEXT, created_at INTEGER NOT NULL);'''
