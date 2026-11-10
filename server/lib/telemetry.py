"""Isolated short-lived telemetry store.

Authoritative product state stays in state.sqlite. Developer diagnostics use a
separate WAL database so telemetry volume or lock contention cannot delay chat,
queue, agent, or message transactions.
"""
from __future__ import annotations

import os
import pathlib
import sqlite3
import threading
import time

from . import batched_delete, xdg
from .timing import SQLITE_BUSY_TIMEOUT_MS, SQLITE_CONNECT_TIMEOUT_SEC


TELEMETRY_PATH = pathlib.Path(os.environ.get(
    "CLARP_TELEMETRY_DB", str(xdg.data_dir() / "telemetry.sqlite")))
HOUR_MS = 60 * 60 * 1000
DETAIL_RETENTION_MS = 24 * HOUR_MS
ROLLUP_RETENTION_MS = 30 * DETAIL_RETENTION_MS
# The size budget on top of the age bounds: when the live data (pages in use,
# see live_bytes) is over this at a maintenance pass, the oldest detail goes
# first, however young. Age alone let one noisy code path write 16 GB inside
# the 24 hours. A budget restored at each pass, not a ceiling on the file.
DETAIL_BUDGET_BYTES = 1024 ** 3
# One delete statement on diagnostic_events frees at most this much detail,
# as well as at most batched_delete.DELETE_BATCH rows. A statement holds the
# write lock while it frees every page of its rows: 500 rows are nothing at
# 300 bytes each and 1.6 GB at 3 MB each, which held the lock for seconds.
DELETE_STATEMENT_BYTES = 8 * 1024 * 1024
# A row's detail size from its record header, without reading the detail
# (octet_length, SQLite 3.43). An older SQLite reads the value to measure it.
_DETAIL_BYTES = ("octet_length(detail)" if sqlite3.sqlite_version_info >= (3, 43)
                 else "length(CAST(detail AS BLOB))")
_LOCAL = threading.local()
_SCHEMA_LOCK = threading.Lock()
# The files the schema has been made in (TEL-N1): a connection to one file never
# marks another ready, so a thread still holding a previous path cannot leave the
# current one without tables.
_SCHEMA_READY_PATHS: set[str] = set()


SCHEMA = """
CREATE TABLE IF NOT EXISTS diagnostic_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL, ts_iso TEXT NOT NULL, source TEXT NOT NULL,
    event TEXT NOT NULL, level TEXT NOT NULL DEFAULT 'info', trace_id TEXT,
    request_id TEXT, client_id TEXT, session TEXT, agent_id TEXT,
    backend_session_id TEXT, persona TEXT, clip_id INTEGER, clip_url TEXT,
    sse_event_id INTEGER, path TEXT, status INTEGER, duration_ms REAL,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_telemetry_ts ON diagnostic_events(ts);
CREATE INDEX IF NOT EXISTS idx_telemetry_trace_ts
    ON diagnostic_events(trace_id, ts);
CREATE INDEX IF NOT EXISTS idx_telemetry_source_event_ts
    ON diagnostic_events(source, event, ts);
CREATE TABLE IF NOT EXISTS hourly_metrics (
    bucket_ms INTEGER NOT NULL, source TEXT NOT NULL, event TEXT NOT NULL,
    path TEXT NOT NULL DEFAULT '', status INTEGER NOT NULL DEFAULT 0,
    samples INTEGER NOT NULL, total_duration_ms REAL NOT NULL,
    max_duration_ms REAL NOT NULL,
    PRIMARY KEY(bucket_ms, source, event, path, status)
);
CREATE INDEX IF NOT EXISTS idx_hourly_metrics_bucket ON hourly_metrics(bucket_ms);
CREATE TABLE IF NOT EXISTS hourly_latency_buckets (
    bucket_ms INTEGER NOT NULL, source TEXT NOT NULL, event TEXT NOT NULL,
    path TEXT NOT NULL DEFAULT '', status INTEGER NOT NULL DEFAULT 0,
    upper_ms INTEGER NOT NULL, samples INTEGER NOT NULL,
    PRIMARY KEY(bucket_ms,source,event,path,status,upper_ms)
);
CREATE VIEW IF NOT EXISTS requests AS
SELECT ts_iso AS ts,request_id,trace_id,json_extract(detail,'$.method') AS method,
       path,status,duration_ms,json_extract(detail,'$.client') AS client,detail
  FROM diagnostic_events WHERE source='server' AND event='httpRequest';
CREATE VIEW IF NOT EXISTS errors AS
SELECT ts_iso AS ts,source,event,trace_id,request_id,session,path,status,
       json_extract(detail,'$.error_type') AS error_type,
       json_extract(detail,'$.error') AS error_message,
       json_extract(detail,'$.traceback') AS traceback,detail
  FROM diagnostic_events WHERE level='error';
CREATE VIEW IF NOT EXISTS trace_paths AS
SELECT d.trace_id,min(d.ts_iso) AS first_at,max(d.ts_iso) AS last_at,
       count(*) AS events,
       (SELECT group_concat(step,' -> ') FROM (
          SELECT d2.source || ':' || d2.event AS step
            FROM diagnostic_events d2 WHERE d2.trace_id=d.trace_id
           ORDER BY d2.ts,d2.event_id)) AS path
  FROM diagnostic_events d
 WHERE d.trace_id IS NOT NULL AND d.trace_id!='' GROUP BY d.trace_id;
CREATE VIEW IF NOT EXISTS voice_latency AS
WITH sent AS (
    SELECT trace_id,min(ts) AS sent_ms,json_extract(detail,'$.text') AS prompt
      FROM diagnostic_events WHERE event='send' AND trace_id IS NOT NULL
     GROUP BY trace_id
), first_synth AS (
    SELECT trace_id,min(ts) AS ts FROM diagnostic_events
     WHERE source='tts_worker' AND event='synthOk' GROUP BY trace_id
), first_play AS (
    SELECT trace_id,min(ts) AS ts FROM diagnostic_events
     WHERE event='clipAck' AND json_extract(detail,'$.status')='play-start'
     GROUP BY trace_id
), done AS (
    SELECT trace_id,min(ts) AS ts FROM diagnostic_events
     WHERE source='stop_hook' AND event='done' GROUP BY trace_id
)
SELECT s.trace_id,s.sent_ms,
       datetime(s.sent_ms/1000,'unixepoch','localtime') AS sent_at,s.prompt,
       round((fs.ts-s.sent_ms)/1000.0,2) AS first_speak_s,
       round((fp.ts-s.sent_ms)/1000.0,2) AS first_play_s,
       round((dn.ts-s.sent_ms)/1000.0,2) AS done_s
  FROM sent s LEFT JOIN first_synth fs ON fs.trace_id=s.trace_id
  LEFT JOIN first_play fp ON fp.trace_id=s.trace_id
  LEFT JOIN done dn ON dn.trace_id=s.trace_id ORDER BY s.sent_ms DESC;
CREATE VIEW IF NOT EXISTS audio_faults AS
SELECT ts_iso AS ts,ts,trace_id,clip_id,clip_url,session,
       json_extract(detail,'$.kind') AS kind,
       json_extract(detail,'$.delivery') AS delivery,
       json_extract(detail,'$.stall_ms') AS stall_ms,
       json_extract(detail,'$.element.current_s') AS at_s,
       json_extract(detail,'$.element.duration_s') AS duration_s,
       json_extract(detail,'$.element.position_pct') AS position_pct,
       json_extract(detail,'$.element.buffered_ahead_ms') AS buffered_ahead_ms,
       json_extract(detail,'$.element.ready_state') AS ready_state,
       json_extract(detail,'$.element.network_state') AS network_state,
       json_extract(detail,'$.element.rate') AS rate,
       json_extract(detail,'$.element.volume') AS volume,
       json_extract(detail,'$.element.error_name') AS error_name,
       json_extract(detail,'$.latency.broadcast_to_queued_ms') AS broadcast_to_queued_ms,
       json_extract(detail,'$.latency.queued_to_play_start_ms') AS queued_to_play_start_ms,
       json_extract(detail,'$.latency.play_start_to_sound_ms') AS play_start_to_sound_ms,
       json_extract(detail,'$.conditions.online') AS online,
       json_extract(detail,'$.conditions.net_type') AS net_type,
       json_extract(detail,'$.conditions.net_rtt_ms') AS net_rtt_ms,
       json_extract(detail,'$.conditions.visibility') AS visibility,
       json_extract(detail,'$.conditions.sse_open') AS sse_open,
       json_extract(detail,'$.conditions.mic_recording') AS mic_recording,
       json_extract(detail,'$.conditions.mic_capturing') AS mic_capturing,
       json_extract(detail,'$.conditions.mic_level') AS mic_level,
       json_extract(detail,'$.conditions.queue_len') AS queue_len,
       json_extract(detail,'$.conditions.machine_state') AS machine_state,
       json_extract(detail,'$.conditions.battery_level') AS battery_level,
       detail
  FROM diagnostic_events WHERE source='client' AND event='audioFault';
CREATE VIEW IF NOT EXISTS audio_clip_health AS
SELECT ts_iso AS ts,ts,trace_id,clip_id,clip_url,session,
       json_extract(detail,'$.delivery') AS delivery,
       json_extract(detail,'$.ok') AS ok,
       json_extract(detail,'$.faults') AS faults,
       json_extract(detail,'$.stall_count') AS stall_count,
       json_extract(detail,'$.stall_total_ms') AS stall_total_ms,
       json_extract(detail,'$.played_ms') AS played_ms,
       json_extract(detail,'$.reached_sound') AS reached_sound,
       json_extract(detail,'$.latency.broadcast_to_queued_ms') AS broadcast_to_queued_ms,
       json_extract(detail,'$.latency.queued_to_play_start_ms') AS queued_to_play_start_ms,
       json_extract(detail,'$.latency.play_start_to_sound_ms') AS play_start_to_sound_ms,
       json_extract(detail,'$.conditions.net_type') AS net_type,
       json_extract(detail,'$.conditions.visibility') AS visibility,
       json_extract(detail,'$.conditions.mic_level') AS mic_level,
       detail
  FROM diagnostic_events WHERE source='client' AND event='audioClipSummary';
"""


def _open(path: pathlib.Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=SQLITE_CONNECT_TIMEOUT_SEC,
                          isolation_level=None, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
    con.execute("PRAGMA synchronous=NORMAL")
    # Match the measured SQLite default (KiB), never enlarge each thread's
    # private cache while reducing the state-store connection budget.
    con.execute("PRAGMA cache_size=-2000")
    return con


def conn() -> sqlite3.Connection:
    current = getattr(_LOCAL, "conn", None)
    if current is not None:
        return current
    path = TELEMETRY_PATH
    current = _open(path)
    with _SCHEMA_LOCK:
        if str(path) not in _SCHEMA_READY_PATHS:
            current.execute("PRAGMA journal_mode=WAL")
            current.executescript(SCHEMA)
            _SCHEMA_READY_PATHS.add(str(path))
    _LOCAL.conn = current
    return current


def close_local() -> None:
    current = getattr(_LOCAL, "conn", None)
    if current is not None:
        current.close()
        _LOCAL.conn = None


def reset_for_tests(path: pathlib.Path | None = None) -> None:
    global TELEMETRY_PATH
    close_local()
    with _SCHEMA_LOCK:
        if path is not None:
            TELEMETRY_PATH = pathlib.Path(path)
        _SCHEMA_READY_PATHS.clear()


UPSERT_BATCH = 500        # rollup rows per write transaction
UPSERT_PAUSE_S = 0.05     # between them, so a waiting emit gets in

_LATENCY_BUCKET = """CASE
                    WHEN duration_ms<=1 THEN 1 WHEN duration_ms<=5 THEN 5
                    WHEN duration_ms<=10 THEN 10 WHEN duration_ms<=25 THEN 25
                    WHEN duration_ms<=50 THEN 50 WHEN duration_ms<=100 THEN 100
                    WHEN duration_ms<=250 THEN 250 WHEN duration_ms<=500 THEN 500
                    WHEN duration_ms<=1000 THEN 1000
                    WHEN duration_ms<=2500 THEN 2500
                    WHEN duration_ms<=5000 THEN 5000
                    WHEN duration_ms<=10000 THEN 10000 ELSE 60000 END"""


def _upsert_in_batches(c, statement: str, rows: list) -> None:
    """Write ``rows`` with ``statement``, ``UPSERT_BATCH`` per transaction."""
    for index in range(0, len(rows), UPSERT_BATCH):
        if index and UPSERT_PAUSE_S:
            time.sleep(UPSERT_PAUSE_S)
        c.execute("BEGIN IMMEDIATE")
        try:
            c.executemany(statement, [tuple(row) for row in rows[index:index + UPSERT_BATCH]])
            c.execute("COMMIT")
        except BaseException:
            c.execute("ROLLBACK")
            raise


def live_bytes(c: sqlite3.Connection | None = None) -> int:
    """Bytes of telemetry.sqlite that hold data: its pages in use.

    Not the size on disk. SQLite keeps the pages a delete frees and fills
    them again before it extends the file, so a file that was large stays
    large and nothing here shrinks it; the WAL is a separate file that grows
    until a checkpoint. The count includes the schema and the rollup tables,
    which deleting detail does not free.
    """
    c = c if c is not None else conn()
    page_size = c.execute("PRAGMA page_size").fetchone()[0]
    pages = c.execute("PRAGMA page_count").fetchone()[0]
    free = c.execute("PRAGMA freelist_count").fetchone()[0]
    return (pages - free) * page_size


def over_budget(budget_bytes: int = DETAIL_BUDGET_BYTES) -> bool:
    """Three pragmas, no scan: cheap enough to ask every few minutes."""
    return live_bytes() > budget_bytes


def _bounded_chunks(rows):
    """``(rowid, detail bytes)`` rows as rowid lists for one statement each."""
    chunk: list[int] = []
    size = 0
    for rowid, detail_bytes in rows:
        if chunk and (len(chunk) >= batched_delete.DELETE_BATCH
                      or size + detail_bytes > DELETE_STATEMENT_BYTES):
            yield chunk
            chunk, size = [], 0
        chunk.append(rowid)
        size += detail_bytes
    if chunk:
        yield chunk


def _prune_detail(c, statement: str, params: tuple) -> int:
    """Run one retention DELETE on diagnostic_events as statements bounded by
    rows (batched_delete.DELETE_BATCH) and by bytes (DELETE_STATEMENT_BYTES).

    The rows and their sizes are read outside the write lock, oldest first.
    Each statement deletes only the rows that still match, with the usual
    pause before the next, so a waiting emit gets in. A row larger than the
    byte bound goes in a statement of its own.
    """
    table, where = batched_delete.split(statement)
    rows = c.execute(
        f"SELECT rowid, COALESCE({_DETAIL_BYTES}, 0) FROM {table} WHERE ({where}) ORDER BY rowid",
        tuple(params)).fetchall()
    removed = 0
    for index, chunk in enumerate(_bounded_chunks(rows)):
        if index and batched_delete.PAUSE_S:
            time.sleep(batched_delete.PAUSE_S)
        removed += batched_delete.prune(c, statement, params, rowids=chunk)
    return removed


def _drop_whole_hours(c, current_hour: int, budget_bytes: int) -> int:
    """Delete the oldest whole hours until the live data fits the budget.

    Only hours before the current one: the caller has just rolled those up.
    A late row can bring such an hour back into the detail; the rollup it
    already has is then kept (_retained_keys), not recomputed from that row.
    """
    removed = 0
    while live_bytes(c) > budget_bytes:
        oldest = c.execute("SELECT MIN(ts) FROM diagnostic_events").fetchone()[0]
        if oldest is None or oldest >= current_hour:
            break
        removed += _prune_detail(
            c, "DELETE FROM diagnostic_events WHERE ts < ?",
            (oldest - oldest % HOUR_MS + HOUR_MS,))
    return removed


def _cut_current_hour(c, keep_bytes: int) -> int:
    """The current hour alone is over the budget: delete its oldest rows
    until ``keep_bytes`` are left.

    This is the one case that deletes part of an hour. That hour's rollup,
    made after the hour ends, then counts only the rows that survived; the
    number deleted is returned so the caller can log it. A disk that fills
    costs more than one hour's exact counts.
    """
    removed = 0
    for _ in range(8):
        live = live_bytes(c)
        if live <= keep_bytes:
            break
        first, last = c.execute(
            "SELECT MIN(event_id), MAX(event_id) FROM diagnostic_events").fetchone()
        if first is None:
            break
        # Rows differ in size: delete the share that would fit if they did
        # not, oldest first, then measure again.
        through = first + int((last - first + 1) * (1 - keep_bytes / live))
        removed += _prune_detail(
            c, "DELETE FROM diagnostic_events WHERE event_id <= ?", (through,))
    return removed


def _retained_keys(c, metrics: list) -> set[tuple]:
    """The rollup rows that must stay as they are: those that already count
    more rows than the detail now holds for the same hour and kind.

    Detail rows are never changed, so a kind's count in an hour only falls
    when detail was deleted: the hour was pruned (by age or by the budget)
    and a late row has arrived for it, or a prune was interrupted. Replacing
    the rollup then would overwrite the hour's full count with the count of
    what is left (100 becomes 1).
    """
    if not metrics:
        return set()
    counted = {tuple(row[:5]): row[5] for row in c.execute(
        """SELECT bucket_ms,source,event,path,status,samples
             FROM hourly_metrics WHERE bucket_ms >= ?""",
        (min(row[0] for row in metrics),))}
    return {tuple(row[:5]) for row in metrics if counted.get(tuple(row[:5]), 0) > row[5]}


def rollup_and_prune(*, now_ms: int | None = None, checkpoint: bool = True,
                      budget_bytes: int = DETAIL_BUDGET_BYTES) -> dict[str, int]:
    """Roll every retained hour up, then expire detail and old rollups, then
    trim the detail back to the size budget.

    The aggregation is a plain read, which in WAL mode blocks no writer; only
    its results are written, in short transactions, and the deletes run in
    bounded batches (RM-N8b). An emit waits at most one batch, never the scan.
    A batch of detail is bounded by its bytes as well as its rows
    (_prune_detail).

    The budget (``budget_bytes`` of live data, see live_bytes) drops whole
    hours, oldest first, all of them rolled up above. If the current hour
    alone is still over it, its oldest rows go too (_cut_current_hour).

    A rollup row is replaced only by one made from at least as many detail
    rows (_retained_keys). So a row that arrives late for an hour whose detail
    is already gone (a deferred emit that commits after the hour turned, or a
    supplied older timestamp) leaves that hour's rollup as it was. This is a
    declared loss, not exact accounting: such a late row is not added to a
    rollup row of its own kind, and only a kind the hour did not have yet
    gets a row. If late rows of a kind are at least as many as the hour
    counted, they replace it.

    It is best-effort reclamation at each pass, not a hard ceiling: what
    arrives between two passes can overshoot it, the cut of the current hour
    stops after a few rounds whether or not it got there, and it bounds
    neither the file on disk nor the WAL (live_bytes).
    """
    now = int(now_ms if now_ms is not None else time.time() * 1000)
    hour = HOUR_MS
    current_hour = now - (now % hour)
    c = conn()
    metrics = c.execute(
        """SELECT ts - (ts % ?),source,event,COALESCE(path,''),
                  COALESCE(status,0),count(*),
                  COALESCE(sum(duration_ms),0),COALESCE(max(duration_ms),0)
             FROM diagnostic_events
            WHERE ts < ?
            GROUP BY ts - (ts % ?),source,event,COALESCE(path,''),
                     COALESCE(status,0)""",
        (hour, current_hour, hour),
    ).fetchall()
    # Both tables follow one decision per hour and kind, so they stay in step.
    retained = _retained_keys(c, metrics)
    metrics = [row for row in metrics if tuple(row[:5]) not in retained]
    _upsert_in_batches(c,
        """INSERT INTO hourly_metrics (
               bucket_ms,source,event,path,status,samples,
               total_duration_ms,max_duration_ms)
           VALUES (?,?,?,?,?,?,?,?)
           ON CONFLICT(bucket_ms,source,event,path,status) DO UPDATE SET
               samples=excluded.samples,
               total_duration_ms=excluded.total_duration_ms,
               max_duration_ms=excluded.max_duration_ms""", metrics)
    latency = c.execute(
        f"""SELECT ts - (ts % ?),source,event,COALESCE(path,''),
                  COALESCE(status,0),{_LATENCY_BUCKET},count(*)
             FROM diagnostic_events
            WHERE ts < ? AND duration_ms IS NOT NULL
            GROUP BY ts - (ts % ?),source,event,COALESCE(path,''),
                     COALESCE(status,0),{_LATENCY_BUCKET}""",
        (hour, current_hour, hour),
    ).fetchall()
    latency = [row for row in latency if tuple(row[:5]) not in retained]
    _upsert_in_batches(c,
        """INSERT INTO hourly_latency_buckets (
               bucket_ms,source,event,path,status,upper_ms,samples)
           VALUES (?,?,?,?,?,?,?)
           ON CONFLICT(bucket_ms,source,event,path,status,upper_ms) DO UPDATE SET
               samples=excluded.samples""", latency)
    # Detail expires by whole hours (RM-N8c): a delete inside an hour left a
    # partial bucket that the next rollup re-aggregated, overwriting its full
    # count with the remainder. Rows now live 24 to 25 h.
    detail_cutoff = (now - DETAIL_RETENTION_MS) - (now - DETAIL_RETENTION_MS) % hour
    details = _prune_detail(
        c, "DELETE FROM diagnostic_events WHERE ts < ?", (detail_cutoff,))
    rollups = batched_delete.prune(
        c, "DELETE FROM hourly_metrics WHERE bucket_ms < ?",
        (now - ROLLUP_RETENTION_MS,), indexed=True)
    batched_delete.prune(
        c, "DELETE FROM hourly_latency_buckets WHERE bucket_ms < ?",
        (now - ROLLUP_RETENTION_MS,))
    budget_details = _drop_whole_hours(c, current_hour, budget_bytes)
    current_hour_cut = 0
    if live_bytes(c) > budget_bytes:
        # Down to half, so the cut is not repeated at every check.
        current_hour_cut = _cut_current_hour(c, budget_bytes // 2)
    # Exclusive and can block concurrent event writers for its duration;
    # the boot sweep runs with checkpoint=False for the same reason
    # MaintenanceWorker defers state.sqlite's own truncate (see
    # maintenance.py), so it doesn't collide with startup write volume.
    if checkpoint:
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return {"telemetry_details": details, "telemetry_rollups": rollups,
            "telemetry_budget_details": budget_details,
            "telemetry_current_hour_cut": current_hour_cut}
