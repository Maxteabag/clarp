"""A heartbeat decision that could not be saved is tried again at the review interval (R106).

One fault while saving the decision (a locked database at that instant) used to cost the agent its
heartbeat until its snapshot changed: the next pass computed the same request id and found that
run closed (or, on main before R100, still running). Now the decider backs off as for a model
failure (a 'failed' continuity naming the run, due at the review interval), and when even that
write fails, the next pass finds the closed run and asks again once a review interval has passed:
at most once an interval, never two dispatches for one decision."""
from __future__ import annotations

import sqlite3

import pytest

from lib import agents, db, janitors, janitor_builtins, janitor_store, task_plans
from lib import janitor_autonomy as service

T0 = 1_795_082_400_000


@pytest.fixture
def env(monkeypatch):
    now = [T0]
    monkeypatch.setattr(db, "now_ms", lambda: now[0])
    monkeypatch.setattr(service.backends, "active_handles", lambda *a: [])
    owners = janitor_builtins.ensure_builtins(initial={"heartbeat-decider": {"enabled": True}})
    aid = agents.create_agent(persona="Task agent", voice_id="v", cwd="/tmp", session="task", backend="codex")
    agents.update_agent(aid, heartbeat_enabled=True)
    agents.record_state(aid, "done", {"source": "user"})
    task_plans.create(session="task", title="Finish current task", items=[{"id": "do", "title": "Implement result"}])
    service.setup()
    return now, owners["heartbeat-decider"]["options"]["review_interval_seconds"] * 1000


class Saves:
    """janitor_store.save_continuity, locked for its first `locked` calls (None: for good)."""

    def __init__(self, monkeypatch, locked):
        self.locked, self.calls = locked, 0
        real = janitor_store.save_continuity

        def save(*args, **kwargs):
            self.calls += 1
            if self.locked is None or self.calls <= self.locked:
                raise sqlite3.OperationalError("database is locked")
            return real(*args, **kwargs)
        monkeypatch.setattr(janitor_store, "save_continuity", save)


def _worker(now, calls, sent):
    def decide(snapshot, run):
        calls.append(now[0] - T0)
        return {"action": "wake", "delay_seconds": 86_400, "message": "Continue the task now.", "reason": "pending"}
    return service.AutonomyJanitors(lambda *a: sent.append(now[0] - T0) or True, lambda _: None, decide)


def _passes(worker, now, offsets):
    for offset in offsets:
        now[0] = T0 + offset
        worker.heartbeat_once()                              # never raises out of the pass


def test_a_save_locked_once_is_decided_again_at_the_next_due_pass_and_dispatched_once(env, monkeypatch):
    now, review = env
    Saves(monkeypatch, locked=1)                             # the decision's save; the back-off saves
    calls, sent = [], []
    worker = _worker(now, calls, sent)
    _passes(worker, now, [0, 60_000, review - 1, review, review + 60_000, 2 * review, 3 * review])
    assert calls == [0, review]                              # one model call more, at the next due pass
    assert sent == [review]                                  # one dispatch, never two


def test_the_back_off_locked_too_is_decided_again_a_review_interval_after_the_run_ended(env, monkeypatch):
    now, review = env
    Saves(monkeypatch, locked=2)                             # the decision's save and the back-off's
    calls, sent = [], []
    worker = _worker(now, calls, sent)
    _passes(worker, now, [0, 60_000, review - 1, review, review + 60_000, 2 * review, 3 * review])
    assert calls == [0, review] and sent == [review]
    assert janitors.list_runs("clarp-heartbeat-decider")[-1]["status"] == "failed"


def test_a_save_locked_for_good_asks_once_a_review_interval_and_never_dispatches(env, monkeypatch):
    now, review = env
    Saves(monkeypatch, locked=None)
    calls, sent = [], []
    worker = _worker(now, calls, sent)
    offsets = sorted({k * 60_000 for k in range(0, 3 * review // 60_000 + 1)} | {review, 2 * review, 3 * review})
    _passes(worker, now, offsets)
    assert calls == [0, review, 2 * review, 3 * review]      # no spin: once an interval
    assert sent == []
    assert all(run["status"] == "failed" for run in janitors.list_runs("clarp-heartbeat-decider"))


def test_a_model_failure_whose_back_off_cannot_be_saved_does_not_end_the_pass(env, monkeypatch):
    """The model-failure path's own back-off write, failing, used to raise out of the whole pass."""
    now, review = env
    Saves(monkeypatch, locked=1)
    answers = iter([RuntimeError("model down")])
    calls, sent = [], []

    def decide(snapshot, run):
        calls.append(now[0] - T0)
        answer = next(answers, None)
        if isinstance(answer, Exception):
            raise answer
        return {"action": "wake", "delay_seconds": 86_400, "message": "Continue the task now.", "reason": "pending"}
    worker = service.AutonomyJanitors(lambda *a: sent.append(now[0] - T0) or True, lambda _: None, decide)
    _passes(worker, now, [0, 60_000, review, 2 * review])
    assert calls == [0, review] and sent == [review]
