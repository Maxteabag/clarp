"""TDD reproduction: HTTP authentication availability and error diagnostics. Implementation pending."""
from __future__ import annotations

import sqlite3

from lib import db, device_pairing


def test_paired_device_authentication_remains_readable_during_writer_contention():
    """Authenticating a read request must not require acquiring the write lock.

    The iOS client uses paired-device credentials. ``authenticate`` currently
    updates ``last_seen_at`` on every request, so one unrelated long writer
    turns otherwise WAL-readable GETs into ``database is locked`` failures.
    """
    issued = device_pairing.issue(device_name="iPhone", scope="full")
    paired = device_pairing.exchange(issued["code"])
    # Fail quickly instead of waiting the production busy timeout.
    db.conn().execute("PRAGMA busy_timeout = 1")
    blocker = sqlite3.connect(str(db.DB_PATH), isolation_level=None)
    blocker.execute("PRAGMA journal_mode = WAL")
    blocker.execute("BEGIN IMMEDIATE")
    try:
        authenticated = device_pairing.authenticate(paired["token"])
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()

    assert authenticated is not None
    assert authenticated["device_id"] == paired["device_id"]
