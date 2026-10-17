"""Integration test for TranscriptStreamer.

Drives the real reconcile loop + inotify dispatcher against synthetic
DB state + synthetic transcript files. Verifies that when a transcript
gains a new assistant text block, a row lands in tts_queue with the
right agent_id and the right text.
"""
from __future__ import annotations

import json
import pathlib
import sys
import time

import pytest

_SERVER_DIR = pathlib.Path(__file__).resolve().parents[2] / "server"
sys.path.insert(0, str(_SERVER_DIR))

from lib import agents as agents_db                  # noqa: E402
from lib import tts_queue                            # noqa: E402
from lib.protocol import TurnSource                  # noqa: E402
from lib.transcript_streamer import TranscriptStreamer  # noqa: E402


# ---- helpers -----------------------------------------------------------


def _assistant_text(text: str) -> dict:
    return {"type": "assistant",
            "message": {"content": [{"type": "text", "text": f"<speak>{text}</speak>"}]}}


def _append(path: pathlib.Path, entry: dict) -> None:
    with path.open("a") as f:
        f.write(json.dumps(entry) + "\n"); f.flush()


def _wait_for(pred, *, timeout: float = 2.0, interval: float = 0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        v = pred()
        if v:
            return v
        time.sleep(interval)
    return pred()


def _build_agent_with_transcript(tmp_path: pathlib.Path, *,
                                 persona="Rachel",
                                 session="rachel",
                                 voice="V_R",
                                 backend_session_id="cs-1") -> tuple[str, pathlib.Path]:
    """Seed a DB agent with a runtime row pointing at a real JSONL file
    under tmp_path/.claude/projects/. Returns (agent_id, transcript_path)."""
    agent_id = agents_db.create_agent(
        persona=persona, voice_id=voice, cwd=str(tmp_path),
        session=session,
    )
    agents_db.start_runtime(agent_id, session)
    agents_db.bind_backend_session(agent_id, backend_session_id)
    # Transcript dir + file, matching find_latest_jsonl's lookup pattern.
    projects = tmp_path / ".claude" / "projects" / "test-project"
    projects.mkdir(parents=True, exist_ok=True)
    tx = projects / f"{backend_session_id}.jsonl"
    tx.touch()
    # Mark the latest turn as 'pwa' so the streamer voices it.
    agents_db.open_turn(agent_id=agent_id, source=TurnSource.PWA,
                        trace_id="trace-1")
    agents_db.set_trace(agent_id, "trace-1")
    return agent_id, tx


@pytest.fixture
def streamer_env(tmp_path, monkeypatch):
    """Streamer with the standard transcript-lookup root pointed at tmp_path."""
    # find_latest_jsonl uses pathlib.Path.home() / ".claude" / "projects".
    # Easiest test isolation: monkeypatch HOME so home() returns tmp_path.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CLAUDE_PWA_DB", str(tmp_path / "state.sqlite"))
    from lib import db
    db.reset_for_tests(tmp_path / "state.sqlite")
    streamer = TranscriptStreamer(reconcile_interval_sec=0.1)
    yield streamer, tmp_path
    streamer.stop(timeout=2.0)


# ---- Tests --------------------------------------------------------------


def test_streamer_enqueues_text_block_when_transcript_grows(streamer_env):
    streamer, tmp_path = streamer_env
    agent_id, tx = _build_agent_with_transcript(tmp_path)

    streamer.start()
    # Wait for the reconcile loop + inotify watch setup.
    _wait_for(lambda: streamer.pool.has(agent_id), timeout=2.0)
    assert streamer.pool.has(agent_id), "streamer should have subscribed"

    # Now write a text block to the transcript.
    _append(tx, _assistant_text("Hello from the streamer test"))

    # tts_queue should gain one row carrying this agent + this text.
    def matched():
        rows = tts_queue.recent(limit=10)
        for r in rows:
            if r["agent_id"] == agent_id and r["status"] in (
                    tts_queue.QUEUED, tts_queue.SYNTHESIZING, tts_queue.DONE):
                return True
        return False
    assert _wait_for(matched, timeout=3.0), (
        f"streamer never enqueued; queue rows = {tts_queue.recent(limit=5)}"
    )

    # And the row should carry the text we wrote.
    from lib import db
    row = db.conn().execute(
        "SELECT text, agent_id FROM tts_queue WHERE agent_id = ? "
        "ORDER BY queue_id DESC LIMIT 1", (agent_id,),
    ).fetchone()
    assert row is not None
    # Off-focus agents get prefixed ("Rachel here. ...") — same rule as the
    # Stop hook. The original text must still be in the body.
    assert "Hello from the streamer test" in row["text"]


def test_streamer_skips_local_source_turns(streamer_env):
    """Same agent, but the turn was tagged local (laptop) not pwa (phone).
    The streamer must respect the source check the hooks already enforce."""
    streamer, tmp_path = streamer_env
    agent_id, tx = _build_agent_with_transcript(tmp_path,
                                                backend_session_id="cs-local")
    # Override the most recent turn to be 'local'.
    agents_db.open_turn(agent_id=agent_id, source=TurnSource.LOCAL,
                        trace_id="trace-local")
    streamer.start()
    _wait_for(lambda: streamer.pool.has(agent_id), timeout=2.0)
    _append(tx, _assistant_text("Should not be voiced"))
    time.sleep(0.5)        # let the streamer process if it would
    matching = [r for r in tts_queue.recent(limit=10)
                if r["agent_id"] == agent_id]
    assert matching == [], (
        f"streamer voiced a local turn; rows = {matching}"
    )


def test_streamer_fires_per_text_block(streamer_env):
    streamer, tmp_path = streamer_env
    agent_id, tx = _build_agent_with_transcript(tmp_path,
                                                backend_session_id="cs-many")
    streamer.start()
    _wait_for(lambda: streamer.pool.has(agent_id), timeout=2.0)

    _append(tx, _assistant_text("first"))
    _wait_for(lambda: len([r for r in tts_queue.recent(limit=10)
                            if r["agent_id"] == agent_id]) >= 1,
              timeout=2.0)

    _append(tx, {"type": "assistant",
                 "message": {"content": [{"type": "tool_use",
                                          "name": "Bash"}]}})
    time.sleep(0.2)        # tool use must NOT add a row

    _append(tx, _assistant_text("second"))
    _wait_for(lambda: len([r for r in tts_queue.recent(limit=10)
                            if r["agent_id"] == agent_id]) >= 2,
              timeout=2.0)

    matching = [r for r in tts_queue.recent(limit=10)
                if r["agent_id"] == agent_id]
    assert len(matching) == 2, (
        f"expected 2 enqueued rows (one per text block), got {len(matching)}"
    )


def test_streamer_unbinds_when_agent_is_deleted(streamer_env):
    streamer, tmp_path = streamer_env
    agent_id, _tx = _build_agent_with_transcript(tmp_path,
                                                 backend_session_id="cs-del")
    streamer.start()
    _wait_for(lambda: streamer.pool.has(agent_id), timeout=2.0)
    assert streamer.pool.has(agent_id)
    agents_db.soft_delete(agent_id)
    _wait_for(lambda: not streamer.pool.has(agent_id), timeout=2.0)
    assert not streamer.pool.has(agent_id), (
        "streamer should have unsubscribed when the agent was deleted"
    )


class _FakeStream:
    """Captures broadcast() calls in-memory for assertion."""
    def __init__(self):
        self.events: list[dict] = []
    def broadcast(self, event):
        self.events.append(event)


def test_streamer_broadcasts_transcript_updated_for_text_blocks(tmp_path,
                                                                 monkeypatch):
    """Each text block that lands fires a TRANSCRIPT_UPDATED SSE event so
    the client can refetch /log and refresh the history pane live."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CLAUDE_PWA_DB", str(tmp_path / "state.sqlite"))
    from lib import db
    db.reset_for_tests(tmp_path / "state.sqlite")

    from lib.protocol import SSEType
    fake_stream = _FakeStream()

    from lib.transcript_streamer import TranscriptStreamer
    streamer = TranscriptStreamer(reconcile_interval_sec=0.05,
                                  stream=fake_stream)

    agent_id, tx = _build_agent_with_transcript(tmp_path,
                                                backend_session_id="cs-sse")
    streamer.start()
    try:
        _wait_for(lambda: streamer.pool.has(agent_id), timeout=2.0)
        _append(tx, _assistant_text("first block"))
        _wait_for(lambda: any(e.get("type") == SSEType.TRANSCRIPT_UPDATED
                                for e in fake_stream.events), timeout=2.0)
        evs = [e for e in fake_stream.events
                if e.get("type") == SSEType.TRANSCRIPT_UPDATED]
        assert evs, "no TRANSCRIPT_UPDATED event broadcast"
        e = evs[-1]
        assert e["agent_id"] == agent_id
        assert e["session"]    # the session is included so the
                                     # client can match against currentSession
        assert e["backend_session_id"] == "cs-sse"
    finally:
        streamer.stop(timeout=2.0)


def test_streamer_broadcasts_for_tool_use_lines_too(tmp_path, monkeypatch):
    """Tool calls don't fire on_text (no audio for them) but they SHOULD
    fire on_change so the history pane updates live as Claude works."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CLAUDE_PWA_DB", str(tmp_path / "state.sqlite"))
    from lib import db
    db.reset_for_tests(tmp_path / "state.sqlite")

    from lib.protocol import SSEType
    fake_stream = _FakeStream()
    from lib.transcript_streamer import TranscriptStreamer
    streamer = TranscriptStreamer(reconcile_interval_sec=0.05,
                                  stream=fake_stream)
    agent_id, tx = _build_agent_with_transcript(tmp_path,
                                                backend_session_id="cs-tool")
    streamer.start()
    try:
        _wait_for(lambda: streamer.pool.has(agent_id), timeout=2.0)
        # Write ONLY a tool_use entry (no assistant text). The streamer
        # must still broadcast — the history pane should update so the
        # user sees the tool invocation appear.
        _append(tx, {"type": "assistant",
                     "message": {"content": [{"type": "tool_use",
                                              "name": "Bash"}]}})
        ok = _wait_for(
            lambda: any(e.get("type") == SSEType.TRANSCRIPT_UPDATED
                          for e in fake_stream.events),
            timeout=2.0)
        assert ok, (
            "tool_use line should broadcast TRANSCRIPT_UPDATED even though "
            "it produces no TTS — the history pane needs to update"
        )
    finally:
        streamer.stop(timeout=2.0)


def test_streamer_does_not_walk_projects_for_an_unbound_agent_every_tick(streamer_env, monkeypatch):
    """An agent whose session is bound but whose transcript has not been
    written yet is retried every second; that retry must be a dict lookup,
    and the bind must still land on the first tick after the file appears."""
    from lib import claude_transcript
    streamer, tmp_path = streamer_env
    claude_transcript.reset_transcript_index()
    agent_id = agents_db.create_agent(persona="Late", voice_id="V", cwd=str(tmp_path), session="late")
    agents_db.start_runtime(agent_id, "late")
    agents_db.bind_backend_session(agent_id, "cs-late")
    projects = tmp_path / ".claude" / "projects" / "test-project"
    projects.mkdir(parents=True, exist_ok=True)
    rebuilds = []
    original = claude_transcript._TranscriptIndex._rebuild
    monkeypatch.setattr(claude_transcript._TranscriptIndex, "_rebuild",
                        lambda self: (rebuilds.append(1), original(self))[1])
    monkeypatch.setattr(claude_transcript, "_glob_latest_jsonl",
                        lambda *a: pytest.fail("unbound agent must not glob the projects tree"))
    for _ in range(10):
        streamer.reconcile_once()
    assert not streamer.pool.has(agent_id)
    assert len(rebuilds) == 1

    (projects / "cs-late.jsonl").touch()
    started = time.monotonic()
    streamer.reconcile_once()
    assert streamer.pool.has(agent_id)
    assert time.monotonic() - started < 2.0
    claude_transcript.reset_transcript_index()


def test_a_held_import_for_one_agent_does_not_delay_another_agents_speech(streamer_env, monkeypatch):
    """One inotify thread serves every Claude agent. A slow transcript import
    for agent A (a large, busy transcript) must not hold agent B's <speak>
    block out of the TTS queue: Jonas's replies waited ~96 s on 17.10."""
    import threading
    from lib import transcript_incremental

    streamer, tmp_path = streamer_env
    agent_a, tx_a = _build_agent_with_transcript(
        tmp_path, persona="Arnold", session="arnold", backend_session_id="cs-a")
    agent_b, tx_b = _build_agent_with_transcript(
        tmp_path, persona="Jonas", session="jonas", backend_session_id="cs-b")

    entered = threading.Event()
    release = threading.Event()
    original = transcript_incremental.import_transcript

    def import_transcript(*args, **kwargs):
        if kwargs.get("agent_id") == agent_a:
            entered.set()
            release.wait(timeout=10.0)
        return original(*args, **kwargs)

    monkeypatch.setattr(transcript_incremental, "import_transcript", import_transcript)

    def b_rows():
        from lib import db
        return db.conn().execute(
            "SELECT queue_id FROM tts_queue WHERE agent_id = ?", (agent_b,)).fetchall()

    streamer.start()
    try:
        assert _wait_for(lambda: streamer.pool.has(agent_a) and streamer.pool.has(agent_b),
                         timeout=2.0)
        # A tool line: no speech, but its tick runs agent A's import.
        _append(tx_a, {"type": "assistant",
                       "message": {"content": [{"type": "tool_use", "name": "Bash"}]}})
        assert entered.wait(timeout=2.0), "agent A's import never started"

        _append(tx_b, _assistant_text("Ja, vent litt"))
        assert _wait_for(b_rows, timeout=2.0), (
            "agent B's speech waited for agent A's import")
        assert not release.is_set()
    finally:
        release.set()


@pytest.mark.parametrize("line_ts, status", [
    ("{recent}", "ok"),
    ("{future}", "future"),
    ("yesterday-ish", "invalid"),
    (None, "absent"),
])
def test_enqueue_event_reports_read_lag_and_the_trace_it_chose(streamer_env, monkeypatch,
                                                                line_ts, status):
    """P3: the enqueue event says when the block was written, how late it was
    read and which trace the read-time marker gave it. A future, invalid or
    missing timestamp is an unknown clock, never a lag; no text in the event."""
    import datetime
    from lib import transcript_streamer

    streamer, tmp_path = streamer_env
    agent_id, _tx = _build_agent_with_transcript(tmp_path)
    events: list[tuple] = []
    monkeypatch.setattr(transcript_streamer, "_emit", lambda *a, **kw: events.append((a, kw)))

    now = datetime.datetime.now(datetime.timezone.utc)
    stamp = {
        "{recent}": (now - datetime.timedelta(seconds=2)).isoformat().replace("+00:00", "Z"),
        "{future}": (now + datetime.timedelta(seconds=60)).isoformat().replace("+00:00", "Z"),
    }.get(line_ts, line_ts)
    streamer._on_text(agents_db.get_by_agent_id(agent_id), "<speak>Ja, vent litt</speak>",
                      line_ts=stamp)

    enqueues = [kw["detail"] for a, kw in events if a[:2] == ("transcript_streamer", "enqueue")]
    assert len(enqueues) == 1
    detail = enqueues[0]
    assert detail["ts_status"] == status
    assert detail["selected_trace"] == "trace-1"
    assert detail["trace_source"] == "read_time_marker"
    if status == "ok":
        assert detail["line_ts"] == stamp
        assert 2000 <= detail["read_lag_ms"] < 60_000
    else:
        assert detail["read_lag_ms"] is None
    assert "Ja, vent litt" not in json.dumps(detail)


def test_a_queued_reclassify_for_a_rebound_or_stopped_owner_does_nothing(streamer_env, monkeypatch):
    """The reclassify runs later on the import worker. By then the agent may
    have been rebound (to another backend session OR the same one, under a new
    generation) or the streamer stopped; a stale callback must not reclassify."""
    from lib import user_notifications

    streamer, tmp_path = streamer_env
    agent_id, _tx = _build_agent_with_transcript(tmp_path, backend_session_id="cs-old")
    streamer._stream = _FakeStream()
    calls: list[tuple] = []

    def recorder(**kw):
        if kw["admit"]():
            calls.append((kw["agent_id"], kw["backend_session_id"]))
        return []

    monkeypatch.setattr(user_notifications, "reclassify_recent_suppressed", recorder)

    streamer.reconcile_once()
    assert streamer.pool.has(agent_id)
    current = streamer._bind_generation[agent_id]

    streamer._after_import(agent_id, "cs-old", current, 1)
    assert calls == [(agent_id, "cs-old")]

    streamer._after_import(agent_id, "cs-old", current - 1, 1)   # an older binding's callback
    streamer._after_import(agent_id, "cs-other", current, 1)     # another backend session
    assert calls == [(agent_id, "cs-old")]

    streamer._unbind(agent_id)                                    # same bsid, new generation
    streamer.reconcile_once()
    assert streamer._bind_generation[agent_id] > current
    streamer._after_import(agent_id, "cs-old", current, 1)
    assert calls == [(agent_id, "cs-old")]

    streamer.stop(timeout=2.0)
    streamer._after_import(agent_id, "cs-old", streamer._bind_generation[agent_id], 1)
    assert calls == [(agent_id, "cs-old")]


def test_bind_generations_only_increase_across_unbind_stop_and_new_streamers(streamer_env):
    """G-a: one process-wide counter, never popped or reset. G-b: stop() unbinds all."""
    streamer, tmp_path = streamer_env
    agent_id, _tx = _build_agent_with_transcript(tmp_path, backend_session_id="cs-same")
    streamer.reconcile_once()
    first = streamer._bind_generation[agent_id]
    streamer._unbind(agent_id)
    assert streamer._bind_generation[agent_id] == first          # not popped
    streamer.reconcile_once()
    second = streamer._bind_generation[agent_id]
    assert second > first
    streamer.stop(timeout=2.0)
    assert streamer._bound == {} and streamer._dirty == {}
    other = TranscriptStreamer(reconcile_interval_sec=0.1)
    try:
        other.reconcile_once()
        assert other._bind_generation[agent_id] > second
    finally:
        other.stop(timeout=2.0)


def test_a_newer_change_during_a_running_callback_keeps_its_pending_intent(streamer_env, monkeypatch):
    """E2-b: the callback clears only its own (bsid, gen, seq). A newer change
    noted while the callback runs must stay pending."""
    import threading
    from lib import user_notifications

    streamer, tmp_path = streamer_env
    agent_id, _tx = _build_agent_with_transcript(tmp_path, backend_session_id="cs-race")
    streamer._stream = _FakeStream()
    streamer.reconcile_once()
    agent = agents_db.get_by_agent_id(agent_id)
    inside, release = threading.Event(), threading.Event()

    def blocking(**kw):
        inside.set()
        assert release.wait(5)
        return []

    monkeypatch.setattr(user_notifications, "reclassify_recent_suppressed", blocking)
    old = streamer._note_change(agent, "cs-race")
    runner = threading.Thread(target=lambda: streamer._after_import(agent_id, old.bsid, old.gen, old.seq))
    runner.start()
    try:
        assert inside.wait(2)
        newer = streamer._note_change(agent, "cs-race")
        assert newer.seq > old.seq
    finally:
        release.set()
        runner.join(5)
    assert streamer._dirty[agent_id].seq == newer.seq


def test_a_callback_effect_failure_clears_only_its_own_intent(streamer_env, monkeypatch):
    """§4: an exception in the callback's effects is logged; its own intent is
    cleared (no effect retry loop); the import is not retried."""
    from lib import user_notifications

    streamer, tmp_path = streamer_env
    agent_id, _tx = _build_agent_with_transcript(tmp_path, backend_session_id="cs-fail")
    streamer._stream = _FakeStream()
    streamer.reconcile_once()
    agent = agents_db.get_by_agent_id(agent_id)

    def boom(**kw):
        raise RuntimeError("effect failed")

    monkeypatch.setattr(user_notifications, "reclassify_recent_suppressed", boom)
    intent = streamer._note_change(agent, "cs-fail")
    streamer._after_import(agent_id, intent.bsid, intent.gen, intent.seq)
    assert agent_id not in streamer._dirty


def test_an_unresolved_intent_is_resubmitted_on_the_backoff_schedule(streamer_env, monkeypatch):
    """§3: a refused (or failed) submission leaves the intent dirty; the existing
    reconcile resubmits it at about 1, 3, 7, 15, 31, 63 s, then every 60 s."""
    from lib import transcript_import_cache, transcript_streamer

    streamer, tmp_path = streamer_env
    agent_id, _tx = _build_agent_with_transcript(tmp_path, backend_session_id="cs-cap")
    streamer.reconcile_once()
    agent = agents_db.get_by_agent_id(agent_id)
    clock = {"now": 1000.0}
    monkeypatch.setattr(transcript_streamer.time, "monotonic", lambda: clock["now"])
    submissions: list[float] = []

    def refused(*args, **kwargs):
        submissions.append(clock["now"])
        return False

    monkeypatch.setattr(transcript_import_cache, "schedule_import", refused)
    monkeypatch.setattr(transcript_import_cache, "is_active", lambda *a, **kw: False)

    streamer._on_change(agent, "cs-cap")                          # first submission, refused
    assert len(submissions) == 1 and agent_id in streamer._dirty
    seen = []
    for t in (0.5, 1.0, 2.9, 3.0, 6.9, 7.0, 15.0, 31.0, 63.0, 122.9, 123.0):
        clock["now"] = 1000.0 + t
        before = len(submissions)
        streamer.reconcile_once()
        if len(submissions) > before:
            seen.append(t)
    assert seen == [1.0, 3.0, 7.0, 15.0, 31.0, 63.0, 123.0]
    assert agent_id in streamer._dirty                            # never dropped while bound
