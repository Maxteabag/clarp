"""RM-N8b: the telemetry rollup and prune never hold telemetry.sqlite's write
lock for a scan, so an emit waits at most one short batch.

The hourly rollup re-aggregated every retained hour in one INSERT ... SELECT
under the write lock (p50 7 s, max 35 s), and every emit waited up to 5 s on
it. The aggregation is now a read, its results are upserted in short
transactions, and the deletes run in bounded batches.
"""
from __future__ import annotations

import random
import sqlite3
import types

from lib import batched_delete, telemetry

HOUR = 60 * 60 * 1000
NOW = 100 * HOUR + 17 * 60 * 1000

# The statements rollup_and_prune ran before RM-N8b, verbatim: the oracle.
_BUCKET = """CASE
    WHEN duration_ms<=1 THEN 1 WHEN duration_ms<=5 THEN 5
    WHEN duration_ms<=10 THEN 10 WHEN duration_ms<=25 THEN 25
    WHEN duration_ms<=50 THEN 50 WHEN duration_ms<=100 THEN 100
    WHEN duration_ms<=250 THEN 250 WHEN duration_ms<=500 THEN 500
    WHEN duration_ms<=1000 THEN 1000
    WHEN duration_ms<=2500 THEN 2500
    WHEN duration_ms<=5000 THEN 5000
    WHEN duration_ms<=10000 THEN 10000 ELSE 60000 END"""
OLD = [
    ("""INSERT INTO hourly_metrics (
           bucket_ms,source,event,path,status,samples,total_duration_ms,max_duration_ms)
       SELECT ts - (ts % ?),source,event,COALESCE(path,''),COALESCE(status,0),count(*),
              COALESCE(sum(duration_ms),0),COALESCE(max(duration_ms),0)
         FROM diagnostic_events WHERE ts < ?
        GROUP BY ts - (ts % ?),source,event,COALESCE(path,''),COALESCE(status,0)
       ON CONFLICT(bucket_ms,source,event,path,status) DO UPDATE SET
           samples=excluded.samples, total_duration_ms=excluded.total_duration_ms,
           max_duration_ms=excluded.max_duration_ms""",
     (HOUR, NOW - NOW % HOUR, HOUR)),
    (f"""INSERT INTO hourly_latency_buckets (bucket_ms,source,event,path,status,upper_ms,samples)
       SELECT ts - (ts % ?),source,event,COALESCE(path,''),COALESCE(status,0),{_BUCKET},count(*)
         FROM diagnostic_events WHERE ts < ? AND duration_ms IS NOT NULL
        GROUP BY ts - (ts % ?),source,event,COALESCE(path,''),COALESCE(status,0),{_BUCKET}
       ON CONFLICT(bucket_ms,source,event,path,status,upper_ms) DO UPDATE SET samples=excluded.samples""",
     (HOUR, NOW - NOW % HOUR, HOUR)),
    ("DELETE FROM diagnostic_events WHERE ts < ?", (NOW - telemetry.DETAIL_RETENTION_MS,)),
    ("DELETE FROM hourly_metrics WHERE bucket_ms < ?", (NOW - telemetry.ROLLUP_RETENTION_MS,)),
    ("DELETE FROM hourly_latency_buckets WHERE bucket_ms < ?", (NOW - telemetry.ROLLUP_RETENTION_MS,)),
]
TABLES = {"diagnostic_events": "ts", "hourly_metrics": "bucket_ms", "hourly_latency_buckets": "bucket_ms"}


def _seed(rnd: random.Random) -> None:
    """Detail inside and outside the 24 h window (whole hours only: RM-N8c's
    partly deleted hour is its own case), and rollups inside and outside the
    30-day window, some of them about to be overwritten."""
    c = telemetry.conn()
    for _ in range(1500):
        hour_start = NOW - NOW % HOUR - rnd.choice([*range(1, 24), *range(26, 31)]) * HOUR
        c.execute(
            """INSERT INTO diagnostic_events (ts,ts_iso,source,event,level,path,status,duration_ms)
               VALUES (?,?,?,?,?,?,?,?)""",
            (hour_start + rnd.randrange(HOUR), "t", rnd.choice(["server", "client"]),
             rnd.choice(["httpRequest", "send", "clip"]), "info", rnd.choice(["/log", "/send", None]),
             rnd.choice([200, 500, None]), rnd.choice([None, 0.5, 7, 40, 300, 4000, 70000])))
    for day in (1, 29, 31, 45):
        bucket = NOW - NOW % HOUR - day * 24 * HOUR
        c.execute("""INSERT INTO hourly_metrics VALUES (?, 'server', 'httpRequest', '/log', 200, 9, 9, 9)""",
                  (bucket,))
        c.execute("""INSERT INTO hourly_latency_buckets VALUES (?, 'server', 'httpRequest', '/log', 200, 10, 9)""",
                  (bucket,))


def _snapshot(c) -> dict[str, list[tuple]]:
    return {t: [tuple(r) for r in c.execute(f"SELECT * FROM {t} ORDER BY {k}, rowid")] for t, k in TABLES.items()}


def test_the_batched_rollup_and_prune_equal_the_single_statements(monkeypatch):
    monkeypatch.setattr(telemetry, "UPSERT_BATCH", 13)
    monkeypatch.setattr(telemetry, "UPSERT_PAUSE_S", 0)
    monkeypatch.setattr(batched_delete, "DELETE_BATCH", 17)
    monkeypatch.setattr(batched_delete, "PAUSE_S", 0)
    _seed(random.Random(5))
    c = telemetry.conn()
    oracle = sqlite3.connect(":memory:")
    c.backup(oracle)
    for sql, params in OLD:
        oracle.execute(sql, params)
    oracle.commit()

    counts = telemetry.rollup_and_prune(now_ms=NOW, checkpoint=False)

    assert _snapshot(c) == _snapshot(oracle)
    assert counts["telemetry_rollups"] == 2   # the 31- and 45-day-old metric rows
    assert counts["telemetry_details"] > 17 * 3, "the fixture must delete detail in several batches"
    assert len(_snapshot(oracle)["hourly_metrics"]) > 13 * 3, "the fixture must need several batches"


def test_every_write_is_one_bounded_batch(monkeypatch):
    monkeypatch.setattr(telemetry, "UPSERT_BATCH", 13)
    monkeypatch.setattr(telemetry, "UPSERT_PAUSE_S", 0)
    monkeypatch.setattr(batched_delete, "DELETE_BATCH", 17)
    monkeypatch.setattr(batched_delete, "PAUSE_S", 0)
    _seed(random.Random(6))
    c = telemetry.conn()
    statements: list[str] = []
    c.set_trace_callback(statements.append)
    try:
        telemetry.rollup_and_prune(now_ms=NOW, checkpoint=False)
    finally:
        c.set_trace_callback(None)
    flat = [" ".join(s.split()) for s in statements]
    assert not any(s.upper().startswith("INSERT") and " SELECT " in s.upper() for s in flat), \
        "no INSERT ... SELECT: the aggregation is a read"
    # Upserts: at most one batch of rows between BEGIN and COMMIT.
    in_txn, rows, worst = False, 0, 0
    for s in flat:
        if s == "BEGIN IMMEDIATE":
            in_txn, rows = True, 0
        elif s == "COMMIT":
            in_txn, worst = False, max(worst, rows)
        elif in_txn and s.upper().startswith("INSERT INTO HOURLY_"):
            rows += 1
    assert 0 < worst <= 13
    deletes = [s for s in flat if s.upper().startswith("DELETE FROM")]
    assert deletes and all("rowid IN (SELECT value FROM json_each(" in s for s in deletes), deletes[:2]


def test_an_emit_gets_the_lock_between_batches(monkeypatch):
    monkeypatch.setattr(telemetry, "UPSERT_BATCH", 13)
    monkeypatch.setattr(batched_delete, "DELETE_BATCH", 17)
    _seed(random.Random(7))
    path = telemetry.conn().execute("PRAGMA database_list").fetchone()[2]
    other = sqlite3.connect(path, timeout=0, isolation_level=None)
    admitted = []

    def pause(seconds):
        # An emit with no busy wait at all gets in only if no lock is held.
        other.execute("""INSERT INTO diagnostic_events (ts,ts_iso,source,event,level)
                         VALUES (?, 't', 'test', 'between', 'info')""", (NOW,))
        admitted.append(seconds)

    monkeypatch.setattr(telemetry, "time", types.SimpleNamespace(sleep=pause, time=lambda: NOW / 1000))
    monkeypatch.setattr(batched_delete, "time", types.SimpleNamespace(sleep=pause))
    try:
        telemetry.rollup_and_prune(now_ms=NOW, checkpoint=False)
    finally:
        other.close()
    assert len(admitted) >= 4, admitted


def _hour_counts(c) -> dict[int, int]:
    return {r[0]: r[1] for r in c.execute(
        "SELECT bucket_ms, sum(samples) FROM hourly_metrics GROUP BY bucket_ms")}


def _seed_minutes(start: int, minutes: int) -> None:
    for minute in range(minutes):
        telemetry.conn().execute(
            """INSERT INTO diagnostic_events (ts,ts_iso,source,event,level,duration_ms)
               VALUES (?, 't', 'server', 'httpRequest', 'info', 5)""", (start + minute * 60_000,))


def test_a_rollup_keeps_every_hour_whole_across_runs_boot_passes_and_downtime():
    """RM-N8c: the detail delete cut inside an hour; the next rollup
    re-aggregated that hour from what was left and overwrote its full count.
    Detail now expires by whole hours, so every hour's count stays whole."""
    base = 200 * HOUR
    _seed_minutes(base, 60 * 6)                         # six full hours, one row a minute
    first = base + 24 * HOUR + 17 * 60_000              # cutoff 17 min into the first hour
    for at in (first, first + 45_000, first + HOUR, first + 2 * HOUR + 5 * 60_000):
        telemetry.rollup_and_prune(now_ms=at, checkpoint=False)
        assert set(_hour_counts(telemetry.conn()).values()) == {60}, at
    # A Host down for two days rolls up its last hours at the first run back.
    telemetry.conn().execute("DELETE FROM hourly_metrics")
    _seed_minutes(base + 10 * HOUR, 60 * 2)
    telemetry.rollup_and_prune(now_ms=base + 60 * HOUR, checkpoint=False)
    counts = _hour_counts(telemetry.conn())
    assert counts[base + 10 * HOUR] == counts[base + 11 * HOUR] == 60
    assert set(counts.values()) == {60}
