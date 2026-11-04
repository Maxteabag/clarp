"""Retention deletes that never hold the write lock for long (RM-N8).

A DELETE whose condition no index covers scans its whole table, and it takes
the database's write lock when it starts: the hourly state_log sweep held it
for 10-47 s while every other writer waited out its 5 s deadline. Here the
scan is a series of short reads by rowid range, which in WAL mode block no
writer, and the delete is a series of small statements by rowid that re-check
the condition, with a pause between them so a waiting writer gets in.

The rows deleted are the rows the single statement deletes, provided nothing
writes the table during the sweep. A row that stops matching meanwhile is
kept (the re-check); a row that starts matching is left to the next sweep.

Callers pass the single statement itself, ``DELETE FROM <table> WHERE ...``,
as a literal: it states what is deleted, and the table-writers guard
(tests/unit/test_table_writers.py) keeps seeing who writes the table.
"""
from __future__ import annotations

import json
import re
import time

READ_SPAN = 20_000      # rowids per read
DELETE_BATCH = 500      # rows per write statement
PAUSE_S = 0.05          # between write statements

_DELETE = re.compile(r"\s*DELETE\s+FROM\s+([a-z_][a-z0-9_]*)\s+WHERE\s+(.+)", re.I | re.S)


def split(statement: str) -> tuple[str, str]:
    """``DELETE FROM <table> WHERE <condition>`` as (table, condition)."""
    match = _DELETE.fullmatch(statement)
    if match is None:
        raise ValueError("expected DELETE FROM <table> WHERE <condition>")
    return match.group(1), match.group(2).strip()


def matching_rowids(c, table: str, where: str, params=()) -> list[int]:
    """Rowids of ``table`` matching ``where``, read in short rowid ranges,
    oldest first: the sweep deletes in that order, so a reader mid-sweep sees
    a state between before and after."""
    lo, hi = c.execute(f"SELECT MIN(rowid), MAX(rowid) FROM {table}").fetchone()
    if lo is None:
        return []
    found: list[int] = []
    for start in range(int(lo), int(hi) + 1, READ_SPAN):
        rows = c.execute(
            f"SELECT rowid FROM {table} WHERE rowid BETWEEN ? AND ? AND ({where}) ORDER BY rowid",
            (start, min(start + READ_SPAN - 1, int(hi)), *params)).fetchall()
        found.extend(row[0] for row in rows)
    return found


def delete_rowids(c, table: str, rowids: list[int], where: str, params=(), *,
                  batch: int | None = None, pause_s: float | None = None) -> int:
    """Delete ``rowids`` that still match ``where``, ``batch`` per statement."""
    batch = max(1, int(DELETE_BATCH if batch is None else batch))
    pause_s = PAUSE_S if pause_s is None else pause_s
    total = 0
    for index in range(0, len(rowids), batch):
        if index and pause_s:
            time.sleep(pause_s)
        total += c.execute(
            f"DELETE FROM {table} WHERE rowid IN (SELECT value FROM json_each(?)) AND ({where})",
            (json.dumps(rowids[index:index + batch]), *params)).rowcount
    return total


def prune(c, statement: str, params=(), *, rowids: list[int] | None = None,
          batch: int | None = None, pause_s: float | None = None) -> int:
    """Run ``statement`` (one DELETE) as bounded batches by rowid. The rows
    are read outside the write lock unless ``rowids`` names them; either way
    each batch deletes only rows that still match."""
    table, where = split(statement)
    if rowids is None:
        rowids = matching_rowids(c, table, where, params)
    return delete_rowids(c, table, rowids, where, params, batch=batch, pause_s=pause_s)
