"""A batch that loses its queue lease before publication (F1-product, design r2 + addenda 1-3).

Real worker boundary: the translate stub is held while the synthetic clock moves past the 60 s queue lease
but not the run's 180 s lifetime. Assertions read final durable state (run row, claim, demand result,
cache, learned answers, Janitor last_error), never intermediate calls. Ledger bookkeeping is not asserted:
it is current behaviour (decision (i)), not a requirement.
"""
from __future__ import annotations

import threading
import time

import pytest

from lib import backends, db, janitor_store, janitors, tool_explanations

ITEM = {"id": "row", "demand_id": "view", "activity": {"command": "ls"}}


@pytest.fixture(autouse=True)
def no_agent_processes(monkeypatch):
    monkeypatch.setattr(backends, "active_handles", lambda *args: [])


def wait_for(check, timeout=10.0, what="condition"):
    deadline = time.monotonic() + timeout
    while True:
        result = check()
        if result:
            return result
        if time.monotonic() >= deadline:
            pytest.fail(f"timed out waiting for {what}")
        time.sleep(.01)


def _one(sql, *args):
    return db.conn().execute(sql, args).fetchone()


def _seed():
    return tool_explanations.janitor_builtins.ensure_builtins(cwd="/tmp", initial={"tool-explainer": {}})["tool-explainer"]


def _frozen_clock(monkeypatch):
    clock = [db.now_ms()]
    monkeypatch.setattr(db, "now_ms", lambda: clock[0])
    monkeypatch.setattr(tool_explanations.durable_queue.cache, "now_ms", lambda: clock[0])
    return clock


def _held_translate():
    """The first model call blocks until released; later calls are held on their own Events."""
    gates = [(threading.Event(), threading.Event()), (threading.Event(), threading.Event())]
    calls = []

    def translate(level, items):
        entered, release = gates[min(len(calls), 1)]
        calls.append(1)
        entered.set()
        assert release.wait(5), "the test never released the model call"
        return {item["id"]: "List the files." for item in items}
    return translate, gates, calls


def _past_publication_attempt():
    # After complete() the batch's job stays 'running' until the worker's next claim prunes it, so
    # "no running job" means the worker has finished the publication attempt (and any closure).
    return _one("SELECT count(*) FROM tool_explanation_jobs WHERE status='running'")[0] == 0


def test_a_batch_that_loses_its_queue_lease_closes_its_own_run_and_frees_admission(monkeypatch):
    config = _seed()
    clock = _frozen_clock(monkeypatch)
    translate, gates, calls = _held_translate()
    with tool_explanations.ToolExplanations(translate=translate, debounce=0) as service:
        assert service.request(3, [ITEM])["items"][0]["status"] == "pending"
        assert gates[0][0].wait(5), "the batch was never admitted to the model"
        # Positives: exactly one admitted run, and the worker owns the job.
        [run] = [r for r in janitors.list_runs(config["session"]) if r["status"] == "running"]
        assert _one("SELECT status FROM tool_explanation_jobs")[0] == "running"
        clock[0] += 60001          # the queue lease is lost; the run's 180 s lifetime is not reached
        gates[0][1].set()
        wait_for(_past_publication_attempt, what="the worker to finish the publication attempt")

        closed = janitors.get_run(run["run_id"])
        assert (closed["status"], closed["outcome"], closed["error"]) == ("cancelled", "cancelled", "lease_lost"), (
            f"the admitted run was left {closed['status']!r}: it would block admission until its lifetime ends")
        assert closed["demand_result"] == {"summary": "Explanation lost its queue lease before publication"}
        assert _one("SELECT finished_at FROM janitor_demand_claims WHERE run_id=?", run["run_id"])[0] is not None
        assert _one("SELECT count(*) FROM tool_explanation_cache")[0] == 0
        assert _one("SELECT count(*) FROM tool_explanation_learned")[0] == 0
        assert _one("SELECT last_error FROM janitor_configs WHERE agent_id=?", config["agent_id"])[0] == "lease_lost"

        # The successor is admitted with no wait for the old run's lifetime (the clock does not move).
        service.request(3, [ITEM])
        assert gates[1][0].wait(5), "no successor was admitted"
        successor = [r for r in janitors.list_runs(config["session"]) if r["status"] == "running"]
        assert [r["run_id"] for r in successor] != [run["run_id"]] and len(successor) == 1
        gates[1][1].set()
        wait_for(lambda: _one("SELECT count(*) FROM tool_explanation_cache")[0] == 1, what="the successor to publish")
        assert janitors.get_run(successor[0]["run_id"])["status"] == "completed"
    assert len(calls) == 2


def test_the_closure_never_rewrites_a_terminal_run_or_touches_a_successor(monkeypatch):
    config = _seed()
    builtin = tool_explanations.janitor_builtins
    clock = _frozen_clock(monkeypatch)
    translate, gates, _calls = _held_translate()
    with tool_explanations.ToolExplanations(translate=translate, debounce=0) as service:
        service.request(3, [ITEM])
        assert gates[0][0].wait(5), "the batch was never admitted to the model"
        [run] = [r for r in janitors.list_runs(config["session"]) if r["status"] == "running"]
        # The run's lifetime ends while the model call is held: recovery makes it terminal, and a
        # successor (another request) is admitted and claimed before the held batch returns.
        clock[0] += builtin.DEMAND_RUN_TTL_MS + 1
        assert builtin.recover_expired_runs() == 1
        successor = builtin.begin_run("tool-explainer", "successor-request")
        assert successor is not None and builtin.claim_run(successor["run_id"])
        terminal = dict(janitor_store.run_row(run["run_id"]))
        terminal_result = janitor_store.demand_result_json(run["run_id"])
        successor_row = dict(janitor_store.run_row(successor["run_id"]))
        last_error = _one("SELECT last_error FROM janitor_configs WHERE agent_id=?", config["agent_id"])[0]
        assert terminal["status"] == "cancelled" and successor_row["status"] == "running"

        gates[0][1].set()
        wait_for(_past_publication_attempt, what="the worker to finish the publication attempt")

        assert dict(janitor_store.run_row(run["run_id"])) == terminal
        assert janitor_store.demand_result_json(run["run_id"]) == terminal_result
        assert dict(janitor_store.run_row(successor["run_id"])) == successor_row
        assert janitor_store.demand_result_json(successor["run_id"]) is None
        assert _one("SELECT last_error FROM janitor_configs WHERE agent_id=?", config["agent_id"])[0] == last_error
        assert _one("SELECT count(*) FROM tool_explanation_cache")[0] == 0
