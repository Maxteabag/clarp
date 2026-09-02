"""Coordination test: TranscriptStreamer + Stop hook share the cursor.

The risk: if the streamer enqueues each text block AND the Stop hook
also reads the transcript at end-of-turn, the same text would land in
tts_queue twice — audible double-speak.

The fix that's already in place: both use `cursor_positions.position`
keyed by backend_session_id. The streamer writes its byte_offset BEFORE
emitting callbacks, so by the time the Stop hook fires the cursor is
already advanced past whatever the streamer consumed. Hook's
cursor.advance() returns no texts, hook returns 0.

This test pins that contract: with the streamer running, the Stop
hook sees nothing new.
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
from lib.transcript_cursor import TranscriptCursor   # noqa: E402
from lib.transcript_streamer import TranscriptStreamer  # noqa: E402


def _wait_for(pred, timeout=2.0, interval=0.02):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = pred()
        if v:
            return v
        time.sleep(interval)
    return pred()


def _assistant_text(t: str) -> dict:
    return {"type": "assistant",
            "message": {"content": [{"type": "text", "text": f"<speak>{t}</speak>"}]}}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CLAUDE_PWA_DB", str(tmp_path / "state.sqlite"))
    from lib import db
    db.reset_for_tests(tmp_path / "state.sqlite")
    yield tmp_path


def test_streamer_advances_cursor_so_hook_sees_no_new_text(env):
    """Streamer consumes a text block → cursor_positions.position is at
    end-of-file. A fresh TranscriptCursor (which is what the Stop hook
    uses) sees no new text on advance()."""
    tmp_path = env
    backend_session_id = "cs-coord-1"

    agent_id = agents_db.create_agent(
        persona="Mike", voice_id="V", cwd=str(tmp_path), session="claude")
    agents_db.start_runtime(agent_id, "claude")
    agents_db.bind_backend_session(agent_id, backend_session_id)
    agents_db.open_turn(agent_id=agent_id, source=TurnSource.PWA,
                        trace_id="t1")
    agents_db.set_trace(agent_id, "t1")

    projects = tmp_path / ".claude" / "projects" / "p"
    projects.mkdir(parents=True)
    tx = projects / f"{backend_session_id}.jsonl"
    tx.touch()

    streamer = TranscriptStreamer(reconcile_interval_sec=0.05)
    streamer.start()
    try:
        _wait_for(lambda: streamer.pool.has(agent_id), timeout=2.0)

        # Now write a text block. Streamer should consume it + enqueue it +
        # advance cursor_positions.position.
        with tx.open("a") as f:
            f.write(json.dumps(_assistant_text("the only text")) + "\n")

        _wait_for(lambda: bool([r for r in tts_queue.recent()
                                  if r["agent_id"] == agent_id]),
                  timeout=3.0)

        # Snapshot how many rows the streamer enqueued.
        streamer_rows = [r for r in tts_queue.recent()
                          if r["agent_id"] == agent_id]
        assert len(streamer_rows) == 1, (
            f"streamer should have enqueued exactly one row; "
            f"got {len(streamer_rows)}"
        )

        # NOW simulate the Stop hook's read pattern: open a fresh cursor
        # for the same backend_session_id, call advance(). It should see no
        # new texts because the streamer already advanced the position.
        positions_dir = tmp_path / ".cache" / "clarp" / "positions"
        positions_dir.mkdir(parents=True, exist_ok=True)
        cursor = TranscriptCursor(positions_dir, backend_session_id)
        with cursor.locked():
            advance = cursor.advance(tx)
        assert advance.texts == [], (
            f"Stop hook would have double-spoken — its cursor.advance() "
            f"returned {advance.texts!r} when the streamer had already "
            f"consumed everything. Cursor position vs file size: "
            f"{cursor.read_position()} vs {tx.stat().st_size}"
        )
    finally:
        streamer.stop(timeout=2.0)

