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

from . import xdg
from .timing import SQLITE_BUSY_TIMEOUT_MS, SQLITE_CONNECT_TIMEOUT_SEC


TELEMETRY_PATH = pathlib.Path(os.environ.get(
    "CLARP_TELEMETRY_DB", str(xdg.data_dir() / "telemetry.sqlite")))
DETAIL_RETENTION_MS = 24 * 60 * 60 * 1000
ROLLUP_RETENTION_MS = 30 * DETAIL_RETENTION_MS
_LOCAL = threading.local()
_SCHEMA_LOCK = threading.Lock()
_SCHEMA_READY = False


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
"""


def _open(path: pathlib.Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=SQLITE_CONNECT_TIMEOUT_SEC,
                          isolation_level=None, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
    con.execute("PRAGMA synchronous=NORMAL")
    return con


def conn() -> sqlite3.Connection:
    global _SCHEMA_READY
    current = getattr(_LOCAL, "conn", None)
    if current is not None:
        return current
    current = _open(TELEMETRY_PATH)
    with _SCHEMA_LOCK:
        if not _SCHEMA_READY:
            current.execute("PRAGMA journal_mode=WAL")
            current.executescript(SCHEMA)
            _SCHEMA_READY = True
    _LOCAL.conn = current
    return current


def close_local() -> None:
    current = getattr(_LOCAL, "conn", None)
    if current is not None:
        current.close()
        _LOCAL.conn = None


def reset_for_tests(path: pathlib.Path | None = None) -> None:
    global TELEMETRY_PATH, _SCHEMA_READY
    close_local()
    if path is not None:
        TELEMETRY_PATH = pathlib.Path(path)
    _SCHEMA_READY = False


def rollup_and_prune(*, now_ms: int | None = None) -> dict[str, int]:
    now = int(now_ms if now_ms is not None else time.time() * 1000)
    hour = 60 * 60 * 1000
    current_hour = now - (now % hour)
    c = conn()
    c.execute(
        """INSERT INTO hourly_metrics (
               bucket_ms,source,event,path,status,samples,
               total_duration_ms,max_duration_ms)
           SELECT ts - (ts % ?),source,event,COALESCE(path,''),
                  COALESCE(status,0),count(*),
                  COALESCE(sum(duration_ms),0),COALESCE(max(duration_ms),0)
             FROM diagnostic_events
            WHERE ts < ?
            GROUP BY ts - (ts % ?),source,event,COALESCE(path,''),
                     COALESCE(status,0)
           ON CONFLICT(bucket_ms,source,event,path,status) DO UPDATE SET
               samples=excluded.samples,
               total_duration_ms=excluded.total_duration_ms,
               max_duration_ms=excluded.max_duration_ms""",
        (hour, current_hour, hour),
    )
    c.execute(
        """INSERT INTO hourly_latency_buckets (
               bucket_ms,source,event,path,status,upper_ms,samples)
           SELECT ts - (ts % ?),source,event,COALESCE(path,''),
                  COALESCE(status,0),
                  CASE
                    WHEN duration_ms<=1 THEN 1 WHEN duration_ms<=5 THEN 5
                    WHEN duration_ms<=10 THEN 10 WHEN duration_ms<=25 THEN 25
                    WHEN duration_ms<=50 THEN 50 WHEN duration_ms<=100 THEN 100
                    WHEN duration_ms<=250 THEN 250 WHEN duration_ms<=500 THEN 500
                    WHEN duration_ms<=1000 THEN 1000
                    WHEN duration_ms<=2500 THEN 2500
                    WHEN duration_ms<=5000 THEN 5000
                    WHEN duration_ms<=10000 THEN 10000 ELSE 60000 END,
                  count(*)
             FROM diagnostic_events
            WHERE ts < ? AND duration_ms IS NOT NULL
            GROUP BY ts - (ts % ?),source,event,COALESCE(path,''),
                     COALESCE(status,0),
                     CASE
                       WHEN duration_ms<=1 THEN 1 WHEN duration_ms<=5 THEN 5
                       WHEN duration_ms<=10 THEN 10 WHEN duration_ms<=25 THEN 25
                       WHEN duration_ms<=50 THEN 50 WHEN duration_ms<=100 THEN 100
                       WHEN duration_ms<=250 THEN 250 WHEN duration_ms<=500 THEN 500
                       WHEN duration_ms<=1000 THEN 1000
                       WHEN duration_ms<=2500 THEN 2500
                       WHEN duration_ms<=5000 THEN 5000
                       WHEN duration_ms<=10000 THEN 10000 ELSE 60000 END
           ON CONFLICT(bucket_ms,source,event,path,status,upper_ms) DO UPDATE SET
               samples=excluded.samples""",
        (hour, current_hour, hour),
    )
    details = c.execute(
        "DELETE FROM diagnostic_events WHERE ts < ?",
        (now - DETAIL_RETENTION_MS,),
    ).rowcount
    rollups = c.execute(
        "DELETE FROM hourly_metrics WHERE bucket_ms < ?",
        (now - ROLLUP_RETENTION_MS,),
    ).rowcount
    c.execute(
        "DELETE FROM hourly_latency_buckets WHERE bucket_ms < ?",
        (now - ROLLUP_RETENTION_MS,),
    )
    c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return {"telemetry_details": details, "telemetry_rollups": rollups}
