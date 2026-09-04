"""Hook subprocesses must not overwrite a newer turn's state."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

from lib import agents
from lib.protocol import AgentState


ROOT = Path(__file__).resolve().parents[2]


def test_stale_stop_hook_cannot_mark_a_newer_turn_done(tmp_path):
    agent_id = agents.create_agent(
        persona="Hook owner",
        voice_id="voice",
        cwd=str(tmp_path),
        session="hook-owner",
        backend="claude",
    )
    agents.start_runtime(agent_id, "hook-owner")
    agents.bind_backend_session(agent_id, "shared-backend-session")
    agents.set_trace(agent_id, "newer-turn")
    agents.record_state(
        agent_id,
        AgentState.THINKING,
        {"trace_id": "newer-turn", "backend_session_id": "shared-backend-session"},
    )
    env = os.environ.copy()
    env.update({
        "CLAUDE_PWA_SESSION": "hook-owner",
        # The runner needs to make this a real hook contract; current hooks
        # ignore it and therefore cannot fence a stale process.
        "CLARP_TURN_TRACE_ID": "older-preempted-turn",
    })

    subprocess.run(
        [sys.executable, str(ROOT / "plugin/hooks/stop_state.py")],
        input=json.dumps({"session_id": "shared-backend-session"}),
        text=True,
        env=env,
        check=True,
        timeout=10,
    )

    latest = agents.latest_state(agent_id)
    assert latest["kind"] == AgentState.THINKING
    assert latest["detail"]["trace_id"] == "newer-turn"
