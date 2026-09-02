"""Integration test for the PostToolUse hook.

The hook is state-only now: it records that a tool call returned, so the
PWA's live activity line advances instead of sticking on the last-started
tool. It must not produce audio.
"""
from __future__ import annotations

import pathlib
import sqlite3

from .fake_claude import FakeClaude


REPO = pathlib.Path(__file__).resolve().parents[2]
HOOK = REPO / "plugin" / "hooks" / "tool_finished.py"


def test_tool_finished_records_state_for_injected_session(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    with FakeClaude(home=home, session="claude",
                    persona="Mike", voice_id="v") as agent:
        agent.user_prompt("hello", source="pwa")
        agent.assistant_text("in-progress chunk")

        result = agent._run(HOOK, stdin_payload={
            "session_id": agent.backend_session_id,
            "transcript_path": str(agent.transcript),
            "tool_name": "Bash",
            "tool_input": {"command": "ls"},
        })
        assert result.returncode == 0, result.stderr

        rows = sqlite3.connect(
            str(home / ".local/share/clarp/state.sqlite")
        ).execute(
            "SELECT kind, detail FROM state_log ORDER BY state_id DESC"
        ).fetchall()
        assert rows, "hook recorded no state"
        assert rows[0][0] == "tool", rows[0]
        assert "tool_finished" in (rows[0][1] or ""), rows[0]

        # State only — no audio from hooks any more.
        assert list((home / ".cache/clarp/audio").glob("*.mp3")) == []
