"""Regression: relaunching an agent must not bleed the previous
conversation's turns into the history pane.

On relaunch, start_runtime() opens a fresh runtimes row whose
backend_session_id is still NULL until the agent's first hook fires and
calls bind_backend_session. In that window load_conversation must report
an empty pane — not fall back to "every message this agent ever had",
which is exactly the stale-history bug this guards against.
"""
from lib import agents as agents_db
from lib.conversation import load_conversation
from lib.transcript_log import find_latest_jsonl, parse_turns


def _load(session):
    return load_conversation(
        session=session,
        claude_finder=find_latest_jsonl,
        claude_parser=parse_turns,
    )


def test_relaunch_before_first_response_shows_empty_not_previous(tmp_path):
    agent_id = agents_db.create_agent(
        persona="Mike", voice_id="v", cwd=str(tmp_path), session="claude",
    )
    # First conversation: bound UUID with real turns.
    agents_db.start_runtime(agent_id, "claude")
    agents_db.bind_backend_session(agent_id, "uuid-old")
    agents_db.store_transcript_turns(
        agent_id=agent_id,
        backend_session_id="uuid-old",
        source_file="/tmp/old.jsonl",
        turns=[
            {"role": "user", "text": "old question", "timestamp": "1"},
            {"role": "assistant", "text": "old answer", "timestamp": "2"},
        ],
    )
    assert [t["text"] for t in _load("claude")["turns"]] == ["old question", "old answer"]

    # Relaunch: new runtime row, backend_session_id NULL until first hook.
    agents_db.start_runtime(agent_id, "claude")
    assert agents_db.live_backend_session(agent_id) == ""

    res = _load("claude")
    assert res["turns"] == [], "previous conversation bled into a fresh relaunch"
    assert res["missing"] is True
    assert res["latest_revision"] == 0

    # First response stamps the new UUID; only the new turns should show.
    agents_db.bind_backend_session(agent_id, "uuid-new")
    agents_db.store_transcript_turns(
        agent_id=agent_id,
        backend_session_id="uuid-new",
        source_file="/tmp/new.jsonl",
        turns=[{"role": "assistant", "text": "fresh start", "timestamp": "3"}],
    )
    assert [t["text"] for t in _load("claude")["turns"]] == ["fresh start"]
