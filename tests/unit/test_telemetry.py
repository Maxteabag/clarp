from lib import db, telemetry


def _insert(ts: int, *, duration: float = 10) -> None:
    telemetry.conn().execute(
        """INSERT INTO diagnostic_events
           (ts,ts_iso,source,event,level,path,status,duration_ms)
           VALUES (?,?,?,?,?,?,?,?)""",
        (ts, "t", "server", "httpRequest", "info", "/log", 200, duration),
    )


def test_telemetry_is_physically_separate_from_business_state():
    _insert(1000)
    assert telemetry.TELEMETRY_PATH != db.DB_PATH
    assert telemetry.conn().execute(
        "SELECT count(*) FROM diagnostic_events").fetchone()[0] == 1
    assert db.conn().execute(
        "SELECT count(*) FROM sqlite_master WHERE name = 'diagnostic_events'"
    ).fetchone()[0] == 0


def test_rollup_is_idempotent_and_detail_expires_after_24_hours():
    hour = 60 * 60 * 1000
    now = 40 * hour
    _insert(now - 25 * hour, duration=10)
    _insert(now - 25 * hour + 1, duration=30)
    _insert(now - hour, duration=50)
    first = telemetry.rollup_and_prune(now_ms=now)
    second = telemetry.rollup_and_prune(now_ms=now)
    assert first["telemetry_details"] == 2
    assert second["telemetry_details"] == 0
    rows = telemetry.conn().execute(
        "SELECT samples,total_duration_ms,max_duration_ms FROM hourly_metrics"
    ).fetchall()
    assert [tuple(row) for row in rows] == [(2, 40, 30), (1, 50, 50)]
    buckets = telemetry.conn().execute(
        "SELECT upper_ms,sum(samples) FROM hourly_latency_buckets "
        "GROUP BY upper_ms ORDER BY upper_ms"
    ).fetchall()
    assert [tuple(row) for row in buckets] == [(10, 1), (50, 2)]
