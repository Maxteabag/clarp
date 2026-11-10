"""Moved out of server/lib/message_writes.py by tools/refactor/split_apply.py (split 'message-import-writer', recipe tools/refactor/splits/wave2-message-writes.toml).
message_writes.py imports these names back. Generated: change the recipe, not this file."""

from __future__ import annotations

import time
from .db import note_import_holds_lock


def _facade():
    """The `message_store` facade module, looked up at call time so a
    monkeypatch on it reaches this code path."""
    from . import message_store
    return message_store


_WRITE_VERBS = frozenset({"INSERT", "UPDATE", "DELETE", "REPLACE"})


class _TailInvalidated(Exception):
    """Cached carry inputs changed; the dispatcher must run a full import."""


class _WriteNeeded(Exception):
    """The probe pass of an import found a row to change."""


class _ImportWriter:
    """The import's connection, in one of two passes.

    An import re-reads the whole transcript on every watcher tick, and almost
    every time nothing changed (11,385 of 11,396 upserts over twelve live
    imports on 2026-10-10 rewrote an identical row). The probe pass reads
    one snapshot (a deferred BEGIN) without the write lock and stops at the
    first write it would make; it records nothing, so an unchanged transcript
    writes nothing at all. Otherwise the import runs again as the locked
    pass: the prepared prelude is revalidated inside BEGIN IMMEDIATE, before
    any row decision can act on it. Remaining row reads and writes stay in
    that transaction, committed in budgeted batches. Only the locked pass runs the recording
    helpers (team messages, Oracle delegations, Dreaming, heartbeats), once.
    """

    def __init__(self, database, *, locked: bool):
        self._database = database
        self.locked = locked
        self.began_at: float | None = None
        self.began_seq = 0
        self.seq = 0
        self.held_since_fair_yield = 0.0
        self._released_connection = None
        self._released_version = None
        self._awaiting_reacquire = False
        self._publication_refusals: set[str] = set()
        self._finalizing = False

    def _fence_version(self):
        """Dirty signal ONLY: a matching version never certifies any input."""
        try:
            return int(self._database.execute("PRAGMA data_version").fetchone()[0])
        except Exception:  # noqa: BLE001 - failure refuses publication, not committed products
            self._publication_refusals.add("data_version_read_failure")
            from .log import log
            log("transcriptTailFenceRead", "code=data_version_read_failure count=1")
            return None

    def checkpoint_allowed(self, status=None) -> bool:
        if self.locked and self.began_at is None:
            # Admit the final transaction and finish any pending dirty-window
            # check BEFORE deciding whether its savepoint may publish.
            self.execute("SELECT 1")
        if self._publication_refusals:
            if status is not None:
                status["refused"] = tuple(sorted(self._publication_refusals))
            return False
        return True

    def execute(self, sql, parameters=()):
        if not self.locked:
            if str(sql).lstrip().split(None, 1)[0].upper() in _WRITE_VERBS:
                raise _WriteNeeded
            if not self._database.in_transaction:
                # One snapshot for the whole probe: separate autocommit reads
                # could see a concurrent write half-applied and wrongly
                # conclude there is nothing to write.
                self._database.execute("BEGIN")
        elif self.began_at is None:
            # The marker lives only as long as this BEGIN: lib.db records the
            # transaction it opens as an import's, and that record ends with the
            # transaction on every exit. Nothing is left on the thread after a
            # failed BEGIN, a rollback or an error (RM-N7).
            note_import_holds_lock(True)
            try:
                self._database.execute("BEGIN IMMEDIATE")
            finally:
                note_import_holds_lock(False)
            self.began_at = time.monotonic()
            self.began_seq = self.seq
            if self._awaiting_reacquire:
                # Same actual connection, sampled before releasing and after
                # reacquiring BEGIN IMMEDIATE. This can ONLY reject reuse.
                if self._released_connection is not self._database:
                    self._publication_refusals.add("connection_changed_yield")
                version = self._fence_version()
                if (self._released_connection is self._database
                        and version is not None and self._released_version is not None
                        and version != self._released_version):
                    self._publication_refusals.add("foreign_commit_yield")
                self._awaiting_reacquire = False
        return self._database.execute(sql, parameters)

    def due(self) -> bool:
        """Whether the open batch has reached its row count or time budget."""
        if self.began_at is None:
            return False
        every = _facade().IMPORT_COMMIT_EVERY
        return (every > 0 and self.seq - self.began_seq >= every) or \
            time.monotonic() - self.began_at >= _facade().IMPORT_WRITE_BUDGET_SECONDS

    def commit(self) -> None:
        """End the batch; the next statement opens the following one."""
        if self.began_at is not None:
            if not self._finalizing:
                self._released_connection = self._database
                self._released_version = self._fence_version()
            self._database.execute("COMMIT")
            self.held_since_fair_yield += time.monotonic() - self.began_at
            self.began_at = None

    def finish(self) -> None:
        """Final release has no yield/reacquire window or post-publication sample."""
        self._finalizing = True
        self.commit()

    def yield_writer(self) -> None:
        """Between batches: the short yield, or once per second of held lock
        the long one (RM-N7)."""
        self._awaiting_reacquire = True
        if self._released_connection is None:
            self._publication_refusals.add("yield_without_release_sample")
        if self.held_since_fair_yield >= _facade().IMPORT_FAIR_YIELD_EVERY_SECONDS:
            time.sleep(_facade().IMPORT_FAIR_YIELD_SECONDS)
            self.held_since_fair_yield = 0.0
        else:
            time.sleep(_facade().IMPORT_WRITER_YIELD_SECONDS)

    def __getattr__(self, name):
        return getattr(self._database, name)


class _PlanCursor:
    def __init__(self, rows=(), rowcount=0):
        self._rows = list(rows)
        self.rowcount = rowcount

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class _PlanWriter:
    """Runs the import against one read snapshot and writes nothing.

    Every write is recorded instead: revisions come from a private counter
    above the current clock, deletes report the rows they would remove. Like
    the probe it is unlocked, so the recording helpers (team messages,
    Dreaming, heartbeats) stay untouched."""

    locked = False

    def __init__(self, database):
        self._database = database
        self.seq = 0
        self.began_at = None
        self.deleted: set[str] = set()
        self.updated: set[str] = set()
        if not database.in_transaction:
            database.execute("BEGIN")
        row = database.execute(
            "SELECT revision FROM message_clock WHERE singleton = 0").fetchone()
        self.revision_floor = int(row["revision"]) if row else 0
        self._revision = self.revision_floor

    def execute(self, sql, parameters=()):
        text = str(sql).lstrip()
        verb = text.split(None, 1)[0].upper()
        if verb not in _WRITE_VERBS:
            return self._database.execute(sql, parameters)
        if "message_clock" in text:
            self._revision += 1
            return _PlanCursor([{"revision": self._revision}], 1)
        if verb == "DELETE" and text.upper().startswith("DELETE FROM MESSAGES"):
            ids = [row[0] for row in self._database.execute(
                "SELECT message_id " + text[len("DELETE "):], parameters).fetchall()]
            self.deleted.update(ids)
            return _PlanCursor([], len(ids))
        if verb == "UPDATE" and text.upper().startswith("UPDATE MESSAGES"):
            self.updated.add(str(parameters[-1]))
        return _PlanCursor([], 1)

    def due(self) -> bool:
        return False

    def commit(self) -> None:
        pass

    def __getattr__(self, name):
        return getattr(self._database, name)
