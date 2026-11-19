"""A hook that reached its agent only through the inherited CLAUDE_PWA_SESSION, while its own session
id is not that agent's live session, acts for nobody (R96). A claude started with Clarp's plugin
inside a dispatched turn's shell inherits the variable: its prompt hook re-bound the agent's live
session to its own id, so the agent's next turn resumed the helper's conversation, and its other
hooks moved the agent's visible state while the agent's real turn ran. The rule is applied once,
in agents.resolve_for_hook; every hook already does nothing for no agent."""
from __future__ import annotations

import io
import json
import pathlib
import sys
import time

import pytest

from lib import agents as agents_db
from lib import db, turn_lifecycle
from lib.protocol import AgentState, TurnSource
from lib.turn_lifecycle import TurnEvent

_HOOKS = pathlib.Path(__file__).resolve().parents[2] / "plugin" / "hooks"
sys.path.insert(0, str(_HOOKS))
import pre_compact  # noqa: E402
import pwa_source_flag  # noqa: E402
import stop_state  # noqa: E402
import tool_activity  # noqa: E402


@pytest.fixture
def events(monkeypatch):
    seen = []
    from lib import eventlog
    real = eventlog.emit

    def emit(*args, **kwargs):
        seen.append((args, kwargs))
        return real(*args, **kwargs)
    monkeypatch.setattr(eventlog, "emit", emit)
    return seen


def _foreign(events):
    return [kwargs["detail"] for args, kwargs in events if args[:2] == ("hook", "foreignSession")]


def _run(monkeypatch, module, *, session_id, env_session="mike", payload=None):
    if env_session:
        monkeypatch.setenv("CLAUDE_PWA_SESSION", env_session)
    else:
        monkeypatch.delenv("CLAUDE_PWA_SESSION", raising=False)
    body = {"session_id": session_id, "prompt": "hi", "tool_name": "Bash", "tool_input": {"command": "ls"},
            **(payload or {})}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(body)))
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    assert module.main() == 0                                   # the child CLI is never disturbed
    return out.getvalue()


def _agent(tmp_path, *, backend="claude", live="live-uuid"):
    agent_id = agents_db.create_agent(persona="Mike", voice_id="V", cwd=str(tmp_path), session="mike",
                                      backend=backend)
    agents_db.start_runtime(agent_id, "mike")
    if live:
        agents_db.bind_backend_session(agent_id, live)
    return agent_id


def _dispatched_turn(agent_id, trace="turn-1"):
    """Admission's own row and transition, as the dispatcher writes them."""
    turn_lifecycle.open_turn(agent_id=agent_id, source="pwa", trace_id=trace)
    turn_lifecycle.transition(agent_id, TurnEvent.PROMPT_ADMITTED,
                              {"source": "pwa", "dispatch": "claude", "trace_id": trace})
    agents_db.set_trace(agent_id, trace)


def _snapshot(agent_id):
    state = agents_db.latest_state(agent_id)
    rows = db.conn().execute("SELECT trace_id, settled_at FROM turns WHERE agent_id=? ORDER BY turn_id",
                             (agent_id,)).fetchall()
    return (agents_db.live_backend_session(agent_id), state["kind"], state["ts"],
            [tuple(row) for row in rows])


# (6) the child claude with the plugin and the inherited variable, during a dispatched turn

@pytest.mark.parametrize("hook", ["prompt", "tool", "compact", "stop"])
def test_a_child_claudes_hooks_change_nothing_and_log_one_line(tmp_path, monkeypatch, events, hook):
    agent_id = _agent(tmp_path)
    _dispatched_turn(agent_id)
    before = _snapshot(agent_id)
    marker = pwa_source_flag.PATHS.source_marker("mike")
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(f"{TurnSource.PWA_VOICE_MARKER} mike {time.time():.3f} turn-1\n")   # the agent's flag
    module = {"prompt": pwa_source_flag, "tool": tool_activity, "compact": pre_compact,
              "stop": stop_state}[hook]
    out = _run(monkeypatch, module, session_id="child-uuid-1234")
    assert _snapshot(agent_id) == before                         # live session, state, rows: unchanged
    assert marker.exists() and out == ""                         # its flag untouched, no context given
    lines = _foreign(events)
    assert len(lines) == 1 and lines[0]["agent"] == agent_id[:8] and lines[0]["session_id"] == "child-uu"


# (1) a dispatched turn that resumes X and whose CLI reports X

def test_the_agents_own_cli_resolves_by_its_session_id_as_before(tmp_path, monkeypatch, events):
    agent_id = _agent(tmp_path)
    _dispatched_turn(agent_id)
    _run(monkeypatch, tool_activity, session_id="live-uuid")
    assert agents_db.latest_state(agent_id)["kind"] == AgentState.TOOL and _foreign(events) == []


# the one exception: the agent has no live session yet

def test_an_agent_with_no_live_session_yet_is_bound_by_its_first_prompt(tmp_path, monkeypatch, events):
    agent_id = _agent(tmp_path, live="")
    _run(monkeypatch, pwa_source_flag, session_id="first-uuid")
    assert agents_db.live_backend_session(agent_id) == "first-uuid" and _foreign(events) == []


# (2) a CLI that reports a new id on resume (only --fork-session does): foreign for that instant

def test_a_new_id_on_resume_acts_for_nobody_until_on_init_binds_it(tmp_path, monkeypatch, events):
    """The dispatcher's admission already wrote the turn's 'thinking' and its row; the hook's own
    would add nothing to them. on_init (turn_launcher.py) binds the new id, after which every hook
    resolves by it. What the prompt hook does not give that turn: its added context."""
    agent_id = _agent(tmp_path)
    _dispatched_turn(agent_id)
    out = _run(monkeypatch, pwa_source_flag, session_id="forked-uuid")
    assert agents_db.latest_state(agent_id)["kind"] == AgentState.THINKING      # admission's
    assert agents_db.live_backend_session(agent_id) == "live-uuid" and out == ""
    agents_db.bind_backend_session(agent_id, "forked-uuid")                     # on_init
    _run(monkeypatch, tool_activity, session_id="forked-uuid")
    assert agents_db.latest_state(agent_id)["kind"] == AgentState.TOOL
    assert len(_foreign(events)) == 1


# (3) and (4): the terminal carries no inherited variable

@pytest.mark.parametrize("session_id, resolves", [("live-uuid", True), ("cleared-uuid", False)],
                         ids=["resumed or compacted: the same id", "after /clear: a new id"])
def test_the_terminal_resolves_by_its_own_id_as_before(tmp_path, monkeypatch, events, session_id, resolves):
    """The terminal (terminal_ws) runs claude --resume <the agent's live id> under the Host's own
    environment, with no CLAUDE_PWA_SESSION. Its hooks resolve by the session id alone, before and
    after this change; a /clear's new conversation resolves to nobody, as before."""
    agent_id = _agent(tmp_path)
    _dispatched_turn(agent_id)                                   # also while a dispatched turn runs
    _run(monkeypatch, tool_activity, session_id=session_id, env_session="")
    assert (agents_db.latest_state(agent_id)["kind"] == AgentState.TOOL) is resolves
    assert _foreign(events) == []


# (5) a fork's or an adopted session's first turn: the agent is created bound to that session

def test_a_forked_or_adopted_agents_first_turn_resolves_by_its_bound_id(tmp_path, monkeypatch, events):
    agent_id = _agent(tmp_path, live="adopted-uuid")
    _dispatched_turn(agent_id)
    _run(monkeypatch, tool_activity, session_id="adopted-uuid")
    assert agents_db.latest_state(agent_id)["kind"] == AgentState.TOOL and _foreign(events) == []


# a claude started in a Codex agent's shell (Codex sets CLAUDE_PWA_SESSION per thread)

def test_a_claude_started_in_a_codex_agents_shell_acts_for_nobody(tmp_path, monkeypatch, events):
    """No Claude turn runs for a Codex agent: per-agent fallback models are gone since schema v101
    ('an agent never switches provider'), and model fallbacks run Claude without the plugin
    (routing_cmd). So a Claude hook that reaches a Codex agent through the variable is a child's."""
    agent_id = _agent(tmp_path, backend="codex", live="codex-thread")
    _dispatched_turn(agent_id)
    _run(monkeypatch, tool_activity, session_id="child-of-codex-uuid")
    assert agents_db.latest_state(agent_id)["kind"] == AgentState.THINKING
    assert agents_db.live_backend_session(agent_id) == "codex-thread" and len(_foreign(events)) == 1
