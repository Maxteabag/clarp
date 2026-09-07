"""Tests for AudioStream — broadcast, replay, queue overflow handling."""
import queue
import sys
import pathlib
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "server"))
from lib.audio_stream import AudioStream  # noqa: E402


def test_subscribe_receives_broadcast(tmp_path):
    s = AudioStream(tmp_path)
    q = s.subscribe()
    s.broadcast({"type": "test", "n": 1})
    payload = q.get(timeout=1)
    assert "test" in payload


def test_recent_replays_in_order(tmp_path):
    s = AudioStream(tmp_path)
    s.broadcast({"type": "a"})
    s.broadcast({"type": "b"})
    types = [ev["type"] for ev in s.recent()]
    assert types == ["a", "b"]


def test_ephemeral_broadcast_reaches_live_subscriber_but_never_replays(tmp_path):
    s = AudioStream(tmp_path)
    q = s.subscribe()

    s.broadcast_ephemeral({"type": "remote-action", "action": "record-toggle"})

    assert "remote-action" in q.get(timeout=1)
    assert not any(ev["type"] == "remote-action" for ev in s.recent())


def test_recent_filters_remote_actions_persisted_by_an_older_release(tmp_path):
    from lib import agents as agents_db

    legacy_id = agents_db.record_sse_event({
        "type": "remote-action",
        "action": "record-toggle",
    })
    agents_db.record_sse_event({"type": "server-version", "version": "new"})

    s = AudioStream(tmp_path)
    replay = s.recent(since_event_id=legacy_id - 1)

    assert not any(ev["type"] == "remote-action" for ev in replay)
    assert any(ev["type"] == "server-version" for ev in replay)


def test_transcript_update_bursts_are_throttled_per_session(tmp_path):
    now = [10.0]
    s = AudioStream(tmp_path, transcript_event_min_interval_sec=0.25,
                    monotonic=lambda: now[0])
    q = s.subscribe()

    for _ in range(100):
        s.broadcast({"type": "transcript-updated", "session": "arnold"})
    s.broadcast({"type": "transcript-updated", "session": "yuki"})

    assert q.qsize() == 2
    now[0] += 0.25
    s.broadcast({"type": "transcript-updated", "session": "arnold"})
    assert q.qsize() == 3


def test_recent_purges_old_events(tmp_path):
    s = AudioStream(tmp_path)
    # Inject an old timestamp directly.
    s._recent.append((time.time() - s.RECENT_WINDOW_SEC - 1, {"type": "old"}))
    s.broadcast({"type": "fresh"})
    types = [ev["type"] for ev in s.recent()]
    assert types == ["fresh"]


def test_full_subscriber_is_evicted_with_log(tmp_path, capsys):
    s = AudioStream(tmp_path)
    q = s.subscribe(maxsize=1)
    q.put_nowait("dummy")          # now full
    s.broadcast({"type": "x"})
    err = capsys.readouterr().err
    assert "sseSubFull" in err
    # Subscriber should have been removed; another broadcast must not raise.
    s.broadcast({"type": "y"})


def test_unsubscribe_silent_when_absent(tmp_path):
    s = AudioStream(tmp_path)
    q: queue.Queue = queue.Queue()
    # Not subscribed — should be a no-op.
    s.unsubscribe(q)


def test_start_is_idempotent_and_stop_joins_threads(tmp_path):
    s = AudioStream(tmp_path)
    s.JANITOR_INTERVAL_SEC = 10

    s.start()
    first_threads = tuple(s._threads)
    s.start()

    assert tuple(s._threads) == first_threads
    s.stop()
    assert all(not t.is_alive() for t in s._threads)


def test_audio_broadcast_marks_clip_broadcast(tmp_path):
    from lib import agents as agents_db

    agent_id = agents_db.create_agent(
        persona="Mike", voice_id="V", cwd=str(tmp_path), session="claude")
    clip = tmp_path / "123__claude.mp3"
    clip.write_bytes(b"mp3")
    clip_id = agents_db.record_clip(agent_id=agent_id, path=str(clip), trace_id="t1")

    s = AudioStream(tmp_path)
    s.broadcast({"type": "audio", "url": "/audio/123__claude.mp3", "clip_id": clip_id})

    row = agents_db.conn().execute(
        "SELECT status, broadcast_at FROM clips WHERE clip_id = ?",
        (clip_id,),
    ).fetchone()
    assert row["status"] == "broadcast"
    assert row["broadcast_at"] is not None
