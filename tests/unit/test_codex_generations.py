"""BACKENDS-06b (stage 2): a credential change reaches the next Codex turn
while other agents are busy, through a second app-server generation.

The old generation drains: its idle threads are released (thread/unsubscribe,
then thread/closed) and a running turn is never interrupted. A thread is never
open in two processes. Runs the real pool against tests/qa/fake_codex.py,
which keeps a real per-thread writer flock under CODEX_HOME."""
import json
import threading
import time

import pytest

from lib import agents as agents_db, codex_app_server as cas
from tests.unit.test_codex_app_server import _NullStream, _install_fake_codex


@pytest.fixture
def fake(tmp_path, monkeypatch):
    home = _install_fake_codex(tmp_path, monkeypatch)
    monkeypatch.setenv("CLARP_QA_RECORD_AUTH", "1")
    monkeypatch.setenv("CLARP_QA_UNLOAD_DELAY", "0.4")
    (home / "auth.json").write_text("account-one")
    yield home
    cas.recycle_clients()
    for client in list(getattr(cas, "_DRAINING", {}).values()):
        client.shutdown()


def _agent(tmp_path, name):
    agent_id = agents_db.create_agent(persona=name.title(), voice_id="v", cwd=str(tmp_path),
                                      session=name, backend="codex")
    agents_db.start_runtime(agent_id, name)
    return agent_id


def _turn(tmp_path, agent_id, session, text, *, thread=""):
    done, outcome, bound = threading.Event(), [], []

    def bind(thread_id):
        bound.append(thread_id)
        if agents_db.live_backend_session(agent_id) != thread_id:
            agents_db.bind_backend_session(agent_id, thread_id)
        return True

    cas.spawn_turn(
        text=text, cwd=tmp_path, backend_session_id=thread, is_new_session=not thread,
        session=session, agent_id=agent_id, on_session_init=bind,
        on_result=lambda _p: (outcome.append("completed"), done.set()),
        on_error=lambda m: (outcome.append(str(m)), done.set()),
        trace_id=f"tr-{session}-{time.monotonic_ns()}", stream=_NullStream(),
        enqueue=lambda **_k: 0, voice_preamble=False)
    return done, outcome, bound


def _records(home, kind):
    path = home / "turn-requests.jsonl"
    if not path.exists():
        return []
    return [row for row in map(json.loads, path.read_text().splitlines()) if row.get("method") == kind]


def _wait(predicate, timeout=8.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def _auth_of(home, thread_id):
    tags = [row["authTag"] for row in _records(home, "turn/start.auth") if row["threadId"] == thread_id]
    return tags[-1] if tags else None


def test_a_switch_while_another_agent_is_busy_reaches_the_next_turn(fake, tmp_path):
    busy, nxt = _agent(tmp_path, "busy"), _agent(tmp_path, "next")
    busy_done, busy_outcome, busy_thread = _turn(tmp_path, busy, "busy", "[qa-slow] long work")
    assert _wait(lambda: busy_thread and _auth_of(fake, busy_thread[0]))
    (fake / "auth.json").write_text("account-two")

    next_done, next_outcome, next_thread = _turn(tmp_path, nxt, "next", "hello")
    assert next_done.wait(8) and next_outcome == ["completed"]
    assert _auth_of(fake, next_thread[0]) == "account-two", \
        "the next turn still ran on the old credentials"
    assert busy_done.wait(8) and busy_outcome == ["completed"], \
        "the busy agent's turn was interrupted by the switch"
    assert _auth_of(fake, busy_thread[0]) == "account-one"


def test_a_thread_moves_only_after_the_old_process_closed_it(fake, tmp_path, monkeypatch):
    monkeypatch.setenv("CLARP_QA_UNLOAD_DELAY", "2")   # a release that takes a while
    mover, busy, other = (_agent(tmp_path, name) for name in ("mover", "busy", "other"))
    done, outcome, thread = _turn(tmp_path, mover, "mover", "hello")
    assert done.wait(8) and outcome == ["completed"]
    busy_done, _, _ = _turn(tmp_path, busy, "busy", "[qa-slow] keeps the old process busy")
    (fake / "auth.json").write_text("account-two")

    other_done, _, _ = _turn(tmp_path, other, "other", "starts the new generation")
    # The mover's thread is still open in the old process: its turn waits.
    assert cas.turn_holders(mover), "the moving thread is not held"
    with pytest.raises(cas.CodexSessionStuck):
        _turn(tmp_path, mover, "mover", "too early", thread=thread[0])
    assert other_done.wait(8)
    assert _wait(lambda: not cas.turn_holders(mover)), "the old process never released the thread"

    done, outcome, _ = _turn(tmp_path, mover, "mover", "after the move", thread=thread[0])
    assert done.wait(8) and outcome == ["completed"], f"resume after the move failed: {outcome}"
    assert _auth_of(fake, thread[0]) == "account-two"
    assert busy_done.wait(8)


def _goal_turns(home, thread_id, kind="goal_turn"):
    return [row["authTag"] for row in _records(home, kind) if row["threadId"] == thread_id]


def _goal_agent(fake, tmp_path, monkeypatch, name="goal"):
    monkeypatch.setenv("CLARP_QA_GOAL_TURN_HOLD", "1.5")
    agent = _agent(tmp_path, name)
    done, _, thread = _turn(tmp_path, agent, name, "hello")
    assert done.wait(8)
    cas.goal(agent, "start", objective="keep going", stream=_NullStream())
    assert _wait(lambda: _goal_turns(fake, thread[0]) == ["account-one"]), "no continuation on gen1"
    return agent, thread[0]


def test_a_goal_moves_paused_and_continues_on_the_new_generation(fake, tmp_path, monkeypatch):
    agent, thread = _goal_agent(fake, tmp_path, monkeypatch)
    (fake / "auth.json").write_text("account-two")
    _turn(tmp_path, _agent(tmp_path, "other"), "other", "starts the new generation")
    assert _wait(lambda: cas._move_get(thread) is not None), "no durable move record before the pause"

    assert _wait(lambda: _goal_turns(fake, thread) == ["account-one", "account-two"], 15), \
        "the goal did not continue on the new generation"
    assert _goal_turns(fake, thread, "goal_turn_end")[:1] == ["account-one"]
    assert not _goal_turns(fake, thread, "goal_turn_interrupted"), "the move interrupted a running turn"
    assert _wait(lambda: cas._move_get(thread) is None), "the move record was left behind"


def test_a_users_pause_during_the_move_wins(fake, tmp_path, monkeypatch):
    agent, thread = _goal_agent(fake, tmp_path, monkeypatch)
    (fake / "auth.json").write_text("account-two")
    _turn(tmp_path, _agent(tmp_path, "other"), "other", "starts the new generation")
    assert _wait(lambda: cas._move_get(thread) is not None)
    cas.goal(agent, "pause", stream=_NullStream())           # the user, mid-move
    assert cas._move_get(thread) is None, "a user's pause must end the move's claim"
    assert _wait(lambda: not cas.turn_holders(agent), 15)
    time.sleep(1.0)
    assert _goal_turns(fake, thread) == ["account-one"], "the move re-activated a goal the user paused"


def _leftover(fake, tmp_path, monkeypatch, name):
    """A thread whose goal is paused, as a crash or rollback mid-move leaves it."""
    agent = _agent(tmp_path, name)
    done, _, thread = _turn(tmp_path, agent, name, "hello")
    assert done.wait(8)
    cas.goal(agent, "start", objective="keep going", stream=_NullStream())
    cas.goal(agent, "pause", stream=_NullStream())
    client = cas._client(agent, name)
    goal = client.request("thread/goal/get", {"threadId": thread[0]})["goal"]
    return agent, thread[0], goal


def test_recovery_restores_a_moving_goal_only_when_it_is_unchanged(fake, tmp_path, monkeypatch):
    monkeypatch.setenv("CLARP_QA_GOAL_TURN_HOLD", "0")
    moved, moved_thread, goal = _leftover(fake, tmp_path, monkeypatch, "moved")
    before = len(_goal_turns(fake, moved_thread))
    cas._move_put(moved_thread, {"agent_id": moved, "thread": moved_thread, "phase": "moving",
                                 "generation": "old", "updated_at": goal["updatedAt"]})
    changed, changed_thread, goal2 = _leftover(fake, tmp_path, monkeypatch, "changed")
    cas._move_put(changed_thread, {"agent_id": changed, "thread": changed_thread, "phase": "moving",
                                   "generation": "old", "updated_at": goal2["updatedAt"] - 5})
    pausing, pausing_thread, _ = _leftover(fake, tmp_path, monkeypatch, "pausing")
    cas._move_put(pausing_thread, {"agent_id": pausing, "thread": pausing_thread, "phase": "pausing",
                                   "generation": "old"})
    cas._move_put("gone-thread", {"agent_id": "gone", "thread": "gone-thread", "phase": "moving",
                                  "generation": "old", "updated_at": 1})

    cas.recover_goal_moves()

    assert _wait(lambda: len(_goal_turns(fake, moved_thread)) == before + 1), "an unchanged moved goal was not restored"
    for thread in (moved_thread, changed_thread, pausing_thread, "gone-thread"):
        assert cas._move_get(thread) is None, f"a leftover record stayed for {thread}"
    assert agents_db.get_by_agent_id(changed) and cas.goal(changed, "get")["status"] == "paused"
    assert cas.goal(pausing, "get")["status"] == "paused"


def test_a_turn_interrupted_without_a_stop_is_logged(fake, tmp_path, capsys):
    agent = _agent(tmp_path, "unasked")
    done, outcome, _ = _turn(tmp_path, agent, "unasked", "[qa-self-interrupt]")
    assert done.wait(8)
    assert "codexTurnInterruptedUnasked" in capsys.readouterr().err
