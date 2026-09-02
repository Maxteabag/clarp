from __future__ import annotations

import pathlib

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
