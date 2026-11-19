"""One turn row per trace (R92). Claude's prompt hook opened a turn row of its own for every
dispatched Claude turn, a second about a second after admission's. Its Stop hook's done and the
runner's end each settled one of the two, and every ending but a clean one left admission's row
unsettled for ever. Now the hook opens a row only for a trace that has none; a dispatched turn's
row is settled by the Host's side alone (the runner's end, which comes after the CLI exited, a
Stop, a janitor cancel, boot recovery), once; the Stop hook settles only a terminal turn."""
from __future__ import annotations

import io
import json
import sys

import pytest

from lib import agents as agents_db
from lib import db, identity, turn_lifecycle
from lib.protocol import AgentState
from lib.turn_lifecycle import TurnEvent
from tests.unit.test_pwa_source_marker import pwa_source_flag
from tests.unit.test_turn_dispatch import _local_dispatch_runtime, _make_service, _run_now  # noqa: F401


def _rows(agent_id):
    return [tuple(row) for row in db.conn().execute(
        "SELECT trace_id, outcome, settled_at IS NOT NULL FROM turns WHERE agent_id = ? ORDER BY turn_id",
        (agent_id,))]


def _prompt_hook(monkeypatch, *, session="mike", clarp_process=True, uuid="claude-uuid"):
    """Claude's UserPromptSubmit hook, as the CLI runs it: inside a process Clarp started (the
    session in its environment), or in a terminal (none)."""
    if clarp_process:
        monkeypatch.setenv("CLAUDE_PWA_SESSION", session)
    else:
        monkeypatch.delenv("CLAUDE_PWA_SESSION", raising=False)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"session_id": uuid, "prompt": "hi"})))
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    pwa_source_flag.main()


def _stop_hook(agent_id, *, dispatched=True):
    """Claude's Stop hook as plugin/hooks/stop_state.py records it: in a process the dispatcher
    started, or (dispatched=False) in a terminal. dispatched=None: a hook loaded before R92."""
    detail = {"backend_session_id": "claude-uuid", "source": "stop_hook"}
    if dispatched is not None:
        detail["dispatched"] = dispatched
    turn_lifecycle.hook_transition(agent_id, TurnEvent.HOOK_STOP, detail)


_SPAWNED: dict = {}


def _spawned(agent_id):
    return [call for _, call in _SPAWNED[agent_id].spawned]


def _dispatched(tmp_path, monkeypatch):
    service, backends, agent_id = _make_service(tmp_path, retry_scheduler=_run_now)
    _SPAWNED[agent_id] = backends
    service.dispatch(text="hi", requested_session="mike", trace_id="tr-1", synthesize_audio=False)
    _prompt_hook(monkeypatch)                     # the CLI takes the prompt
    _, call = backends.spawned[0]
    return call, agent_id


# ---- the start: one row ----

def test_a_dispatched_claude_turn_has_one_turn_row(tmp_path, monkeypatch):
    _call, agent_id = _dispatched(tmp_path, monkeypatch)
    assert _rows(agent_id) == [("tr-1", None, 0)]                  # two rows on main


def test_a_prompt_arriving_inside_the_running_turn_opens_no_row(tmp_path, monkeypatch):
    """A background-task notification or a retried attempt: the same process's trace, its row."""
    _call, agent_id = _dispatched(tmp_path, monkeypatch)
    _prompt_hook(monkeypatch)
    assert _rows(agent_id) == [("tr-1", None, 0)]


def test_a_late_hook_in_a_clarp_process_after_its_turn_settled_opens_no_row(tmp_path, monkeypatch):
    call, agent_id = _dispatched(tmp_path, monkeypatch)
    _stop_hook(agent_id)
    call["on_result"]({"type": "result", "subtype": "success", "is_error": False, "result": "ok"})
    _prompt_hook(monkeypatch)
    assert [row[0] for row in _rows(agent_id)] == ["tr-1"]


def test_a_terminal_prompt_with_no_trace_gets_a_row_of_its_own(tmp_path, monkeypatch):
    agent_id = agents_db.create_agent(persona="Mike", voice_id="V", cwd=str(tmp_path), session="mike")
    agents_db.bind_backend_session(agent_id, "claude-uuid")
    _prompt_hook(monkeypatch, clarp_process=False)
    (trace, outcome, settled), = _rows(agent_id)
    assert trace.startswith("local-") and not settled


def test_the_next_terminal_prompt_after_a_settled_turn_is_a_turn_of_its_own(tmp_path, monkeypatch):
    """Inside the trace's hour its trace was reused, and a second row opened under it: two turns
    sharing a trace (main). The settled turn's trace is not reused now."""
    agent_id = agents_db.create_agent(persona="Mike", voice_id="V", cwd=str(tmp_path), session="mike")
    agents_db.bind_backend_session(agent_id, "claude-uuid")
    _prompt_hook(monkeypatch, clarp_process=False)
    _stop_hook(agent_id, dispatched=False)
    db.conn().execute("UPDATE turns SET started_at = started_at - 5000, settled_at = settled_at - 4000")
    _prompt_hook(monkeypatch, clarp_process=False)
    rows = _rows(agent_id)
    assert len(rows) == 2 and rows[0][0] != rows[1][0]
    assert rows[0][1:] == ("completed", 1) and rows[1][1:] == (None, 0)


def test_a_terminal_prompt_inside_a_running_turn_is_part_of_it(tmp_path, monkeypatch):
    agent_id = agents_db.create_agent(persona="Mike", voice_id="V", cwd=str(tmp_path), session="mike")
    agents_db.bind_backend_session(agent_id, "claude-uuid")
    _prompt_hook(monkeypatch, clarp_process=False)
    _prompt_hook(monkeypatch, clarp_process=False)
    assert len(_rows(agent_id)) == 1


def test_two_openers_of_one_trace_open_one_row(tmp_path):
    """The check and the insert are one statement, so two hooks at once cannot both insert."""
    agent_id = agents_db.create_agent(persona="Mike", voice_id="V", cwd=str(tmp_path), session="mike")
    first = turn_lifecycle.open_turn_once(agent_id=agent_id, source="local", trace_id="tr-x")
    second = turn_lifecycle.open_turn_once(agent_id=agent_id, source="local", trace_id="tr-x")
    assert first and second is None and len(_rows(agent_id)) == 1


# ---- the end: a dispatched turn is settled by the Host's side, once ----

OK = {"type": "result", "subtype": "success", "is_error": False, "result": "ok"}


def test_ok_the_hooks_done_writes_no_outcome_and_the_runner_settles(tmp_path, monkeypatch):
    call, agent_id = _dispatched(tmp_path, monkeypatch)
    _stop_hook(agent_id)
    assert _rows(agent_id) == [("tr-1", None, 0)]                  # the visible state only
    assert agents_db.latest_state(agent_id)["kind"] == AgentState.DONE
    call["on_result"](OK)
    assert _rows(agent_id) == [("tr-1", "completed", 1)]


def test_a_failed_turn_is_never_stored_as_completed(tmp_path, monkeypatch):
    """The Stop hook's done comes first (it runs inside the CLI, before the process exits), then
    the runner judges the turn failed: the row goes from unsettled to failed, never via completed."""
    call, agent_id = _dispatched(tmp_path, monkeypatch)
    _stop_hook(agent_id)
    call["on_error"]("codex exited rc=1")
    assert _rows(agent_id) == [("tr-1", "failed", 1)]
    state = agents_db.latest_state(agent_id)
    assert state["kind"] == AgentState.INTERRUPTED and state["detail"]["trace_id"] == "tr-1"


@pytest.mark.parametrize("error", ["codex exited rc=1", "some unexpected parser crash"])
def test_a_failed_turn_with_no_stop_hook_settles_its_row_as_failed(tmp_path, monkeypatch, error):
    call, agent_id = _dispatched(tmp_path, monkeypatch)
    call["on_error"](error)
    assert _rows(agent_id) == [("tr-1", "failed", 1)]


def test_a_retried_attempt_keeps_the_traces_one_row_until_the_last_attempt_ends(tmp_path, monkeypatch):
    call, agent_id = _dispatched(tmp_path, monkeypatch)
    call["on_error"]("read ECONNRESET")                             # a connection drop: retried
    assert len(_spawned(agent_id)) == 2
    _prompt_hook(monkeypatch)                                       # the retried CLI takes the prompt
    assert _rows(agent_id) == [("tr-1", None, 0)]
    retried = _spawned(agent_id)[-1]
    _stop_hook(agent_id)
    retried["on_result"](OK)
    assert _rows(agent_id) == [("tr-1", "completed", 1)]


def test_a_claude_started_by_a_dispatched_turns_tool_owns_no_row_and_settles_nothing(tmp_path, monkeypatch):
    """A helper `claude` run from the dispatched turn's shell inherits CLAUDE_PWA_SESSION, so its
    hooks resolve to the dispatching agent and say 'dispatched'. Its prompt hook finds the agent's
    running trace's row and opens none; its Stop hook writes no outcome. The dispatched turn's row
    is settled by its own runner. Bound to the session id instead, the helper's Stop would settle
    the agent's row early."""
    call, agent_id = _dispatched(tmp_path, monkeypatch)
    _prompt_hook(monkeypatch, uuid="helper-uuid")
    turn_lifecycle.hook_transition(agent_id, TurnEvent.HOOK_STOP,
                                   {"backend_session_id": "helper-uuid", "source": "stop_hook",
                                    "dispatched": True})
    assert _rows(agent_id) == [("tr-1", None, 0)]
    call["on_result"](OK)
    assert _rows(agent_id) == [("tr-1", "completed", 1)]


def _real_stop_hook(monkeypatch, *, clarp_process):
    """plugin/hooks/stop_state.py's main(), as the CLI runs it; returns the flag it sent."""
    import pathlib
    hooks = str(pathlib.Path(__file__).resolve().parents[2] / "plugin" / "hooks")
    if hooks not in sys.path:
        sys.path.insert(0, hooks)
    import stop_state
    if clarp_process:
        monkeypatch.setenv("CLAUDE_PWA_SESSION", "mike")
    else:
        monkeypatch.delenv("CLAUDE_PWA_SESSION", raising=False)
    sent = []
    real = turn_lifecycle.hook_transition

    def hook_transition(agent_id, event, detail=None, *args, **kwargs):
        sent.append(dict(detail or {}).get("dispatched", "absent"))
        return real(agent_id, event, detail, *args, **kwargs)
    monkeypatch.setattr(turn_lifecycle, "hook_transition", hook_transition)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"session_id": "claude-uuid"})))
    assert stop_state.main() == 0
    return sent


@pytest.mark.parametrize("clarp_process", [True, False], ids=["the dispatcher's process", "a terminal"])
def test_the_real_stop_hook_says_dispatched_only_in_a_process_the_dispatcher_started(
        tmp_path, monkeypatch, clarp_process):
    """The hook itself sends the flag (the tests above pass it to the transition by hand)."""
    call, agent_id = _dispatched(tmp_path, monkeypatch)
    assert _real_stop_hook(monkeypatch, clarp_process=clarp_process) == [clarp_process]
    if clarp_process:
        assert _rows(agent_id) == [("tr-1", None, 0)]               # the runner settles it
        call["on_result"](OK)
    assert _rows(agent_id) == [("tr-1", "completed", 1)]


@pytest.mark.parametrize("flag", ["true", 1, None], ids=["the string true", "the number 1", "no flag"])
def test_only_the_boolean_true_says_dispatched(tmp_path, monkeypatch, flag):
    """Read strictly: anything but True is a terminal turn, settled by its hook as before."""
    _call, agent_id = _dispatched(tmp_path, monkeypatch)
    _stop_hook(agent_id, dispatched=flag)
    assert _rows(agent_id) == [("tr-1", "completed", 1)]


def test_a_terminal_turn_is_settled_by_its_stop_hook(tmp_path, monkeypatch):
    agent_id = agents_db.create_agent(persona="Mike", voice_id="V", cwd=str(tmp_path), session="mike")
    agents_db.bind_backend_session(agent_id, "claude-uuid")
    _prompt_hook(monkeypatch, clarp_process=False)
    _stop_hook(agent_id, dispatched=False)
    (_trace, outcome, settled), = _rows(agent_id)
    assert (outcome, settled) == ("completed", 1)


def test_a_users_stop_settles_the_turn_it_stopped_not_a_newer_row(tmp_path):
    """The Stop names the trace it took from the slot; a second turn of another trace already has a
    row (admitted behind it): only the stopped turn settles."""
    agent_id = agents_db.create_agent(persona="Mike", voice_id="V", cwd=str(tmp_path), session="mike")
    turn_lifecycle.open_turn(agent_id=agent_id, source="pwa", trace_id="tr-1")
    turn_lifecycle.transition(agent_id, TurnEvent.PROMPT_ADMITTED, {"source": "pwa", "trace_id": "tr-1"})
    db.conn().execute("UPDATE turns SET started_at = started_at - 1000")
    turn_lifecycle.open_turn(agent_id=agent_id, source="pwa", trace_id="tr-2")
    turn_lifecycle.try_transition(agent_id, TurnEvent.STOP_REQUESTED,
                                  {"source": "user_stop", "message": "Turn stopped", "trace_id": "tr-1"})
    assert _rows(agent_id) == [("tr-1", "interrupted", 1), ("tr-2", None, 0)]


def test_two_ends_arriving_together_settle_once_and_the_second_is_logged(tmp_path, monkeypatch, capfd):
    call, agent_id = _dispatched(tmp_path, monkeypatch)
    turn_lifecycle.try_transition(agent_id, TurnEvent.STOP_REQUESTED,
                                  {"source": "user_stop", "message": "Turn stopped", "trace_id": "tr-1"})
    turn_lifecycle.try_transition(agent_id, TurnEvent.PROCESS_EXITED_FAILED,
                                  {"trace_id": "tr-1", "dispatch": "claude", "reason": "runner_exit"})
    assert _rows(agent_id) == [("tr-1", "interrupted", 1)]
    err = capfd.readouterr().err
    assert "turnSettledAlready" in err and "settled_by=interrupted:user_stop" in err
    assert "then=process_exited_failed" in err


def test_a_restart_mid_turn_settles_the_row_as_interrupted(tmp_path, monkeypatch):
    _call, agent_id = _dispatched(tmp_path, monkeypatch)
    turn_lifecycle.transition(agent_id, TurnEvent.RESTART_INTERRUPTED, {"source": "restart", "trace_id": "tr-1"})
    assert _rows(agent_id) == [("tr-1", "interrupted", 1)]


def test_a_traced_end_whose_row_is_settled_never_settles_another_traces_row(tmp_path):
    """On main it fell back to the agent's newest row started by then, here another trace's."""
    agent_id = agents_db.create_agent(persona="Mike", voice_id="V", cwd=str(tmp_path), session="mike")
    turn_lifecycle.open_turn(agent_id=agent_id, source="pwa", trace_id="tr-old")
    turn_lifecycle.transition(agent_id, TurnEvent.PROMPT_ADMITTED, {"source": "pwa", "trace_id": "tr-old"})
    turn_lifecycle.transition(agent_id, TurnEvent.PROCESS_EXITED_FAILED, {"trace_id": "tr-old"})
    turn_lifecycle.open_turn(agent_id=agent_id, source="pwa", trace_id="tr-new")
    turn_lifecycle.transition(agent_id, TurnEvent.PROMPT_ADMITTED, {"source": "pwa", "trace_id": "tr-new"})
    turn_lifecycle.transition(agent_id, TurnEvent.RESTART_INTERRUPTED, {"trace_id": "tr-old"})
    assert _rows(agent_id) == [("tr-old", "failed", 1), ("tr-new", None, 0)]


# ---- mixed versions: a runtime on old code (its failure untraced) and hooks loaded before R92 ----

def _twins(tmp_path, monkeypatch):
    """A dispatched turn whose CLI runs a hook loaded before R92: it opened a second row."""
    call, agent_id = _dispatched(tmp_path, monkeypatch)
    db.conn().execute("UPDATE turns SET started_at = started_at - 1000")
    turn_lifecycle.open_turn(agent_id=agent_id, source="pwa", trace_id="tr-1")
    return call, agent_id


def _old_runtime_fails(agent_id):
    turn_lifecycle.try_transition(agent_id, TurnEvent.PROCESS_EXITED_FAILED,
                                  {"dispatch": "claude", "reason": "runner_exit"})


def test_old_runtime_and_old_hook_behave_as_main(tmp_path, monkeypatch):
    _call, agent_id = _twins(tmp_path, monkeypatch)
    _stop_hook(agent_id, dispatched=None)
    _old_runtime_fails(agent_id)
    assert _rows(agent_id) == [("tr-1", None, 0), ("tr-1", "completed", 1)]


def test_old_runtime_and_new_hook_settle_the_one_row_from_the_untraced_failure(tmp_path, monkeypatch):
    _call, agent_id = _dispatched(tmp_path, monkeypatch)
    _stop_hook(agent_id)
    _old_runtime_fails(agent_id)
    assert _rows(agent_id) == [("tr-1", "failed", 1)]


def test_new_runtime_and_old_hook_settle_admissions_row_with_the_runners_end(tmp_path, monkeypatch):
    call, agent_id = _twins(tmp_path, monkeypatch)
    _stop_hook(agent_id, dispatched=None)
    call["on_error"]("codex exited rc=1")
    assert _rows(agent_id) == [("tr-1", "failed", 1), ("tr-1", "completed", 1)]


# ---- the running turn's trace: a background job's started_trace_id ----

def _job_trace(agent_id, job_id):
    from lib import background_jobs
    background_jobs.upsert(session="mike", job_id=job_id, kind="worker", title="Run")
    return background_jobs.detail(job_id)["started_trace_id"]


def test_a_job_registered_inside_a_turn_names_it_and_between_turns_none(tmp_path, monkeypatch):
    call, agent_id = _dispatched(tmp_path, monkeypatch)
    assert _job_trace(agent_id, "inside") == "tr-1"
    _stop_hook(agent_id)
    assert _job_trace(agent_id, "after-the-hook") == "tr-1"        # the runner has not ended it yet
    call["on_result"](OK)
    assert _job_trace(agent_id, "between") == ""                    # 'tr-1' on main


def test_a_cut_off_turn_and_an_old_unsettled_twin_name_no_running_turn(tmp_path):
    agent_id = agents_db.create_agent(persona="Mike", voice_id="V", cwd=str(tmp_path), session="mike")
    turn_lifecycle.open_turn(agent_id=agent_id, source="pwa", trace_id="tr-twin")       # never settled
    turn_lifecycle.open_turn(agent_id=agent_id, source="pwa", trace_id="tr-twin")
    db.conn().execute("UPDATE turns SET settled_at = 1, outcome = 'failed' WHERE turn_id = "
                      "(SELECT MAX(turn_id) FROM turns)")
    assert identity.open_trace_id(agent_id) == ""
    newer = turn_lifecycle.open_turn(agent_id=agent_id, source="pwa", trace_id="tr-cut")
    turn_lifecycle.close_turn(newer)
    assert identity.open_trace_id(agent_id) == ""
