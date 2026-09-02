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
