from __future__ import annotations

import datetime
import threading

from lib import agents as agents_db
from lib import db
from lib import maintenance
from lib.protocol import ClipProducerStatus


def test_prune_database_preserves_latest_state_and_removes_old_ephemera(tmp_path):
    agent_id = agents_db.create_agent(
        persona="Mike", voice_id="V", cwd=str(tmp_path), session="mike"
    )
    c = db.conn()
    c.execute("UPDATE state_log SET ts = 1")
    agents_db.record_state(agent_id, "idle")
    c.execute("INSERT INTO sse_events (ts, type, payload) VALUES (1, 'x', '{}')")
    c.execute(
        """INSERT INTO tts_queue
           (agent_id, text, voice_id, session, source, status,
            enqueued_at, completed_at)
           VALUES (?, 'x', 'V', 'mike', 'pwa', 'done', 1, 1)""",
        (agent_id,),
    )

    result = maintenance.prune_database(now_ms=10_000, policy=maintenance.Policy(
        sse_max_age_ms=100, tts_max_age_ms=100, state_max_age_ms=100,
        clip_row_max_age_ms=100,
    ))

    assert result["sse_events"] == 1
    assert result["tts_queue"] == 1
    states = c.execute("SELECT kind FROM state_log ORDER BY state_id").fetchall()
    assert [row["kind"] for row in states] == ["idle"]


def test_prune_background_job_events_keeps_latest_per_job():
    c = db.conn()
    c.executemany(
        "INSERT INTO background_job_events(job_id,observed_at) VALUES (?,?)",
        [("a", 1), ("a", 2), ("b", 1), ("c", 9_990)],
    )

    result = maintenance.prune_database(
        now_ms=10_000,
        policy=maintenance.Policy(background_job_events_max_age_ms=100),
    )

    assert result["background_job_events"] == 1
    kept = c.execute(
        "SELECT job_id,observed_at FROM background_job_events ORDER BY event_id"
    ).fetchall()
    assert [tuple(row) for row in kept] == [
        ("a", 2), ("b", 1), ("c", 9_990)]


def test_prune_hls_artifacts_removes_only_expired_complete_clips(tmp_path):
    audio = tmp_path / "audio"
    old_dir = audio / "hls" / "41"
    live_dir = audio / "hls" / "42"
    old_dir.mkdir(parents=True)
    live_dir.mkdir(parents=True)
    (old_dir / "playlist.m3u8").write_text("#EXTM3U")
    (live_dir / "playlist.m3u8").write_text("#EXTM3U")

    c = db.conn()
    c.execute(
        """INSERT INTO clips
           (clip_id, agent_id, path, created_at, producer_status)
           VALUES (41, 'a', ?, 1, ?), (42, 'a', ?, 9999, ?)""",
        (str(old_dir / "playlist.m3u8"), ClipProducerStatus.COMPLETE,
         str(live_dir / "playlist.m3u8"), ClipProducerStatus.COMPLETE),
    )

    removed = maintenance.prune_hls_artifacts(
        audio, now_ms=10_000, max_age_ms=100,
    )

    assert removed == 1
    assert not old_dir.exists()
    assert live_dir.exists()


def test_maintenance_worker_defers_only_the_exclusive_checkpoint(tmp_path):
    """Retention runs at boot; the WAL truncation waits out interrupt recovery.

    Deferring the entire sweep also deferred the telemetry rollup, so
    telemetry.sqlite did not exist until the first hourly pass.
    """
    calls: list[bool] = []
    first = threading.Event()
    checkpointed = threading.Event()
    worker = maintenance.MaintenanceWorker(
        audio_dir=tmp_path, interval_sec=60, startup_delay_sec=0.2,
    )

    def record(*, checkpoint: bool = True):
        calls.append(checkpoint)
        (checkpointed if checkpoint else first).set()
        return {}

    worker.run_once = record
    worker.start()
    try:
        # The boot sweep happens immediately, without the exclusive lock.
        assert first.wait(1.0)
        assert calls[0] is False
        assert not checkpointed.wait(0.05)
        # The checkpointing sweep only follows the startup delay.
        assert checkpointed.wait(2.0)
        assert calls[1] is True
    finally:
        worker.stop(timeout=1.0)


def test_prune_database_drops_old_judgment_decisions_only(tmp_path):
    from lib import judgments
    c = db.conn()
    for created in (1, 9_990):
        c.execute(
            """INSERT INTO judgment_decisions (site, question_ids, created_at)
               VALUES ('junk', 'junk', ?)""", (created,))
    result = maintenance.prune_database(now_ms=10_000, policy=maintenance.Policy(
        judgment_decisions_max_age_ms=100))
    assert result["judgment_decisions"] == 1
    remaining = judgments.recent(limit=10, site="junk")
    assert [row["created_at"] for row in remaining] == [9_990]


def test_run_once_forwards_checkpoint_flag_to_telemetry_rollup(tmp_path):
    """run_once(checkpoint=False) must also spare telemetry.sqlite's own
    exclusive truncate, not just state.sqlite's — that gap let the boot
    sweep collide with startup write volume and throw "database is locked".
    """
    from lib import telemetry

    executed: list[str] = []
    con = telemetry.conn()
    con.set_trace_callback(executed.append)
    worker = maintenance.MaintenanceWorker(audio_dir=tmp_path)
    try:
        worker.run_once(checkpoint=False)
        assert not any("wal_checkpoint" in sql for sql in executed)
        executed.clear()
        worker.run_once(checkpoint=True)
        assert any("wal_checkpoint" in sql for sql in executed)
    finally:
        con.set_trace_callback(None)


def test_prune_event_logs_removes_only_expired_daily_logs(tmp_path):
    today = datetime.date(2026, 9, 29)
    for name in ("2026-09-01.jsonl", "2026-09-14.jsonl", "2026-09-15.jsonl",
                 "2026-09-29.jsonl", "notes.jsonl", "2026-09-01.txt"):
        (tmp_path / name).write_text("{}\n")

    removed = maintenance.prune_event_logs(
        tmp_path, max_age_ms=14 * maintenance.DAY_MS, today=today,
    )

    assert removed == 2
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "2026-09-01.txt", "2026-09-15.jsonl", "2026-09-29.jsonl", "notes.jsonl",
    ]
