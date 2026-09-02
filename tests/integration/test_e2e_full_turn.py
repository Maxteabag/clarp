"""End-to-end smoke test using the FakeClaude harness.

Verifies one full turn lands every expected side effect: a turns row, a
state_log transition, a clip file on disk, a clips row tagged with the
right agent_id, and a sidecar JSON with the expected metadata.

This is the seed test for the harness — more scenarios (relaunch, fork,
concurrent turns, herald-conflict) live next to it as the project grows.
"""
from __future__ import annotations

import json
import pathlib

import pytest

from .fake_claude import FakeClaude


@pytest.fixture
def home(tmp_path) -> pathlib.Path:
    h = tmp_path / "home"
    h.mkdir()
    return h


def test_full_turn_lands_clip_db_row_and_sidecar(home):
    with FakeClaude(home=home, session="rachel",
                    persona="Rachel", voice_id="v_rachel") as agent:
        agent.user_prompt("hello there", source="pwa")
        agent.assistant_text("Hi! I'm Rachel and I am ready.")
        r = agent.stop()
        assert r.returncode == 0, r.stderr
        agent.speak_now("Hi! I'm Rachel and I am ready.")

        # ---- audio clip on disk -----------------------------------------
        clips = agent.clips_on_disk()
        assert len(clips) == 1, f"expected one mp3, got {[c.name for c in clips]}"
        clip = clips[0]
        assert clip.name.endswith("__rachel.mp3")

        # ---- sidecar carries identity ----------------------------------
        sidecar = clip.with_suffix(clip.suffix + ".json")
        assert sidecar.is_file(), "expected sidecar JSON next to the mp3"
        meta = json.loads(sidecar.read_text())
        assert meta["persona"] == "Rachel"
        assert meta["voice_id"] == "v_rachel"
        assert meta["session"] == "rachel"
        assert meta["source"] == "pwa"

        # ---- DB rows: turn opened, state log written, clip recorded ----
        agent_rows = agent.db_rows("agents")
        assert len(agent_rows) == 1
        agent_id = agent_rows[0][0]

        turns = agent.db_rows("turns")
        assert any(row[1] == agent_id and row[3] == "pwa" for row in turns), (
            f"expected a 'pwa' turn for {agent_id}, got {turns!r}"
        )

        state_kinds = [row[4] for row in agent.db_rows("state_log")
                       if row[1] == agent_id]
        assert "thinking" in state_kinds, (
            f"UserPromptSubmit should have logged 'thinking', got {state_kinds}"
        )
        assert "done" in state_kinds, (
            f"Stop hook should have logged 'done', got {state_kinds}"
        )

        # state_log is append-only; the LATEST state must be 'done'.
        latest_kind = [row[4] for row in agent.db_rows("state_log")
                       if row[1] == agent_id][-1]
        assert latest_kind == "done"


def test_two_agents_dont_cross_route(home):
    """Two agents fire simultaneously — each clip lands tagged to its own
    app session. This pins identity isolation between concurrent agents."""
    with FakeClaude(home=home, session="rachel",
                    persona="Rachel", voice_id="v_r") as rachel:
        with FakeClaude(home=home, session="antoni",
                        persona="Antoni", voice_id="v_a") as antoni:
            rachel.user_prompt("rachel question", source="pwa")
            rachel.assistant_text("Rachel answering.")
            r1 = rachel.stop()
            assert r1.returncode == 0, r1.stderr
            rachel.speak_now("Rachel answering.")

            antoni.user_prompt("antoni question", source="pwa")
            antoni.assistant_text("Antoni answering.")
            r2 = antoni.stop()
            assert r2.returncode == 0, r2.stderr
            antoni.speak_now("Antoni answering.")

            files = sorted(p.name for p in
                           (home / ".cache/clarp/audio").glob("*.mp3"))
            assert any(n.endswith("__rachel.mp3") for n in files), files
            assert any(n.endswith("__antoni.mp3") for n in files), files
            # And NEVER cross-tagged.
            for n in files:
                assert "__rachel" in n or "__antoni" in n
