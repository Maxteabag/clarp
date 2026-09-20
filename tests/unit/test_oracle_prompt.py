"""The Oracle voice prompt is one text, written for a model that holds no tools.

Recorded 2026-09-20: asked to talk to an agent, Oracle said "I can't reach
people outside this chat", answered "Checking." to every request, and told the
user she had no contact. Each of those came straight from prompt text written
for v1, where the voice model called tools itself, reused verbatim on v2.
"""
from __future__ import annotations

from lib import agents as agents_db
from lib import oracle_calls, oracle_calls_stable, oracle_live, oracle_live_stable
from lib.oracle_prompt import PROMPT


def test_both_engines_share_one_prompt_object():
    assert oracle_calls.PROMPT is PROMPT
    assert oracle_calls_stable.PROMPT is PROMPT
    assert oracle_live.PROMPT is PROMPT
    assert oracle_live_stable.PROMPT is PROMPT


def test_prompt_tells_oracle_the_roster_is_who_she_can_reach_and_to_say_what_she_does():
    text = PROMPT.lower()
    assert "roster" in text and "contact" in text
    assert "never say you cannot reach an agent" in text
    assert "say what you asked and who is doing it" in text
    for banned in ("never mention", "let me check", "stay silent", "do not narrate", "list_agents"):
        assert banned not in text, banned


def test_tool_notes_no_longer_order_silence():
    cfg = oracle_calls_stable.session_config(model="m", voice="v", fallback="mike-cb43")
    for engine in (oracle_live_stable, oracle_live):
        names = [t["name"] for t in engine.router_tools()]
        assert "get_agent_status" in names, "status was stripped since Init; the router needs it back"
    describe = next(t["description"] for t in cfg["tools"] if t["name"] == "investigate_with_oracle")
    assert "contact" in describe.lower() and "never mention" not in describe.lower()
    for module in (oracle_calls, oracle_calls_stable):
        import inspect
        source = inspect.getsource(module.AgentTools.execute)
        assert "Stay silent" not in source and "Do not say let me check" not in source


def test_stable_session_instructions_carry_the_roster_and_contact():
    roster = {"agents": [{"name": "Mike", "session": "mike-cb43"}, {"name": "Omar", "session": "omar-5c9d"}],
              "oracle_contact": "mike-cb43"}
    session = oracle_live_stable.live_config(roster=roster)
    assert "Mike (mike-cb43)" in session["instructions"]
    assert "Omar (omar-5c9d)" in session["instructions"]
    assert "Your contact: mike-cb43" in session["instructions"]
    assert session["instructions"].startswith(PROMPT)
    # Without a contact she is told to ask, not to pretend.
    bare = oracle_live_stable.live_config(roster={"agents": [], "oracle_contact": None})
    assert "No contact is configured" in bare["instructions"]
    assert oracle_live_stable.live_config()["instructions"] == PROMPT


def test_quiet_timer_is_shared_and_longer_than_a_breath():
    # 0.5 s ended one in ten of her natural pauses (measured 2026-09-20).
    assert oracle_live_stable.QUIET_AFTER_SECONDS >= 1.0
    assert oracle_live.QUIET_AFTER_SECONDS is oracle_live_stable.QUIET_AFTER_SECONDS


def test_age_text_reads_naturally_and_never_raises():
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    assert oracle_calls._age_text((now - timedelta(days=3)).isoformat()) == "3 days ago"
    assert oracle_calls._age_text((now - timedelta(hours=1, minutes=5)).isoformat()) == "1 hour ago"
    assert oracle_calls._age_text((now - timedelta(seconds=20)).isoformat()) == "just now"
    assert oracle_calls._age_text((now - timedelta(minutes=2)).isoformat().replace("+00:00", "Z")) == "2 minutes ago"
    assert oracle_calls._age_text("") == "" and oracle_calls._age_text(None) == ""


def test_agent_status_answers_what_an_agent_is_doing(tmp_path):
    agent_id = agents_db.create_agent(persona="Omar", voice_id="V", cwd=str(tmp_path), session="omar-5c9d")
    agents_db.record_state(agent_id, "tool", {"summary": "Reading the Oracle transcript", "tool": "Bash"})
    tools = oracle_calls_stable.AgentTools(None, "owner", "omar-5c9d", lambda _s: None)
    status = tools.execute("get_agent_status", {"agent": "Omar"}, "c1")
    assert status["agent"] == "Omar" and status["session"] == "omar-5c9d"
    assert status["state"] == "working"
    assert status["current_step"] == "Reading the Oracle transcript"
    assert status["since"] == "just now"
    assert "silent" not in status
    assert "one sentence" in status["note"]
