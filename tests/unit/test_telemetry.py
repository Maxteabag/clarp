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


def test_audio_fault_views_flatten_client_records():
    import json
    fault = {
        "kind": "stall", "delivery": "hls", "stall_ms": 812,
        "element": {"current_s": 3.2, "duration_s": 10, "position_pct": 32,
                    "buffered_ahead_ms": 0, "ready_state": 2, "network_state": 2,
                    "rate": 1.2, "volume": 1, "error_name": None},
        "latency": {"broadcast_to_queued_ms": 140, "queued_to_play_start_ms": 20,
                    "play_start_to_sound_ms": 310},
        "conditions": {"online": True, "net_type": "4g", "net_rtt_ms": 150,
                       "visibility": "visible", "sse_open": True,
                       "mic_recording": True, "mic_capturing": False,
                       "mic_level": 14, "queue_len": 1,
                       "machine_state": "idle", "battery_level": 63},
    }
    summary = {"delivery": "hls", "ok": False, "faults": ["stall"],
               "stall_count": 1, "stall_total_ms": 812, "played_ms": 9100,
               "reached_sound": True,
               "latency": {"broadcast_to_queued_ms": 140},
               "conditions": {"net_type": "4g", "visibility": "visible",
                              "mic_level": 14}}
    con = telemetry.conn()
    for event, detail in (("audioFault", fault), ("audioClipSummary", summary)):
        con.execute(
            """INSERT INTO diagnostic_events
               (ts,ts_iso,source,event,level,trace_id,clip_id,clip_url,session,detail)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (5000, "t", "client", event, "info", "trace1", 7,
             "/clips/7/playlist.m3u8", "rachel", json.dumps(detail)),
        )
    row = con.execute("SELECT * FROM audio_faults").fetchone()
    assert row["kind"] == "stall"
    assert row["clip_id"] == 7
    assert row["stall_ms"] == 812
    assert row["at_s"] == 3.2
    assert row["position_pct"] == 32
    assert row["buffered_ahead_ms"] == 0
    assert row["play_start_to_sound_ms"] == 310
    assert row["net_type"] == "4g"
    assert row["mic_recording"] == 1
    assert row["mic_level"] == 14
    assert row["battery_level"] == 63
    health = con.execute("SELECT * FROM audio_clip_health").fetchone()
    assert health["ok"] == 0
    assert health["stall_count"] == 1
    assert health["faults"] == '["stall"]'
    assert health["reached_sound"] == 1
    assert con.execute("SELECT count(*) FROM audio_faults").fetchone()[0] == 1
