"""One turn's account failure must not take the backend's other turns hostage.

laptopstudio, 2026-11-11 17:49 to 21:20: two helpers ran with model ``gpt-6``,
which no saved Codex account offers. Every turn of theirs ended "The 'gpt-6'
model is not supported when using Codex with a ChatGPT account", which is an
account-plan failure and so went into the pool's account recovery. That
recovery killed every running Codex turn, parked them, and asked the selector.
The selector switched to an account that serves the other models and answered
``{"available": true, "models": [...]}``; the coordinator ignored ``models``
and resumed all five. The ``gpt-6`` turn failed again within three seconds:
kill, park, resume, about 146 times in three and a half hours. Three agents
that had done nothing wrong got an interrupted turn and one more
``<clarp-account-recovery>`` message in their conversation each cycle.

These tests drive that through the real dispatcher and coordinator with a
selector that is a small script (the real one is never run).
"""
from __future__ import annotations

import heapq
import json
import random
import sys
import threading
import uuid
from dataclasses import replace
from types import SimpleNamespace

import pytest

from lib import account_failover
from lib import agents as agents_db
from lib.account_failover import (
    MAX_PARK_SECONDS, MAX_RELAPSES, QUIET_SECONDS, RECHECK_SECONDS, AccountFailover, Attempt)
from lib.protocol import AgentState
import lib.turn_dispatch as _td
from lib.turn_dispatch import TurnDispatchService
from tests.unit.test_turn_dispatch import _Backends, _Stream

NOT_OFFERED = ("The 'gpt-6' model is not supported when using Codex with a ChatGPT account.")
USAGE_LIMIT = "You've hit your usage limit. Upgrade to Pro or try again at 5:08 PM."
GOOD = ["gpt-6.1-sol", "gpt-6-sol", "gpt-6.1-sol", "gpt-6-astra"]
BAD = "gpt-6"

# The selector's protocol: models on stdin, one JSON object on stdout. This one
# serves every model but the ones listed in a file the test rewrites, and logs
# each call.
SELECTOR = """
import json, sys
log, unserved = sys.argv[1], set(open(sys.argv[2]).read().split())
asked = json.load(sys.stdin)["models"]
served = [m for m in asked if m not in unserved]
with open(log, "a") as out:
    out.write(json.dumps(asked) + "\\n")
print(json.dumps({"available": bool(served), "profile": "work", "models": served}
                 if served else {"available": False}))
"""


@pytest.fixture(autouse=True)
def _local_dispatch_runtime(monkeypatch):
    monkeypatch.setattr(_td, "_RUNTIME_CLIENT", None)
    _td.reset_for_tests()
    yield
    _td.reset_for_tests()


class Fleet:
    """Agents of one backend, each with one running turn, and the pool's real
    coordinator on a clock the test moves."""

    def __init__(self, tmp_path, monkeypatch, backend, models, *, unserved=(BAD,)):
        self.backend, self.tmp_path = backend, tmp_path
        self.log = tmp_path / "selector.log"
        script = tmp_path / "selector.py"
        script.write_text(SELECTOR)
        self.unserved_file = tmp_path / "selector.unserved"
        self.no_account_offers(*unserved)
        command = (sys.executable, str(script), str(self.log), str(self.unserved_file))
        field = f"{backend}_account_switch_command"
        cfg = replace(_td.config.load(), **{field: command})
        monkeypatch.setattr(_td.config, "load", lambda *args, **kwargs: cfg)
        self.clock = [1000.0]
        self.scheduled = []           # (when it is due, callback)
        self.selector_seconds = 0.0   # how long the selector takes to answer
        self.asked_at = []            # when each selector call began
        self.coordinator = AccountFailover(
            _td.OwnershipLock(), switch=self._switch,
            schedule=lambda delay, cb: self.scheduled.append((self.clock[0] + delay, cb)),
            now=lambda: self.clock[0])
        monkeypatch.setitem(_td._FAILOVERS, backend, self.coordinator)
        # Every turn has a conversation to resume, as on a live Host.
        monkeypatch.setattr(_td.backends.by_id(backend), "resume_target",
                            lambda *args, **kwargs: tmp_path / "native.jsonl")
        self.backends = _Backends()
        self.backends.live = False
        self.started_at = {}          # trace -> when each of its processes started
        spawn = self.backends.spawn_turn

        def spawn_turn(backend, **call):
            self.started_at.setdefault(call["trace_id"], []).append(self.clock[0])
            return spawn(backend, **call)
        self.backends.spawn_turn = spawn_turn
        ctx = SimpleNamespace(default_session="agent-0", agents_path=tmp_path / "unused.json",
                              stream=_Stream())
        self.service = TurnDispatchService(
            ctx, backend_registry=self.backends, home=tmp_path,
            uuid_factory=lambda: str(uuid.uuid4()), now=lambda: self.clock[0])
        self.agents = []
        for index, model in enumerate(models):
            session = f"agent-{index}"
            agent_id = agents_db.create_agent(
                persona=session, voice_id="V", cwd=str(tmp_path), session=session,
                backend=backend, model=model)
            agents_db.start_runtime(agent_id, session)
            self.agents.append(agent_id)
            self.service.dispatch(text=f"work of {session}", requested_session=session,
                                  forced_session=session, trace_id=f"trace-{index}",
                                  synthesize_audio=False)
            first = self.spawns(index)[-1]
            # Codex reports its thread id at init; Claude's is minted before the spawn.
            assert first["on_session_init"](first["backend_session_id"] or f"native-{index}")
        assert len(self.backends.spawned) == len(models)
        assert [_td._INFLIGHT.get(agent) for agent in self.agents] == [
            f"trace-{index}" for index in range(len(models))]

    def no_account_offers(self, *models):
        """The models the selector's answers leave out from now on."""
        self.unserved_file.write_text("\n".join(models))

    def key(self, index):
        return (self.agents[index], f"trace-{index}")

    def spawns(self, index):
        return [call for _, call in self.backends.spawned if call["trace_id"] == f"trace-{index}"]

    def selector_calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] \
            if self.log.exists() else []

    def _switch(self, command, models):
        self.asked_at.append(self.clock[0])
        self.clock[0] += self.selector_seconds
        return account_failover.switch_account(command, models)

    def run_scheduled(self, *, advance=0.0):
        """Fire every timer the coordinator armed, due or not."""
        self.clock[0] += advance
        pending, self.scheduled[:] = list(self.scheduled), []
        for _due, callback in pending:
            callback()
        return len(pending)

    def run_until(self, at):
        """Let time pass until ``at``, firing each timer when it is due, as
        the coordinator's thread would."""
        while True:
            due = [entry for entry in self.scheduled if entry[0] <= at]
            if not due:
                break
            entry = min(due, key=lambda item: item[0])
            self.scheduled.remove(entry)
            self.clock[0] = max(self.clock[0], entry[0])
            entry[1]()
        self.clock[0] = max(self.clock[0], at)

    def limit(self, index, message=USAGE_LIMIT):
        """The turn's running process started its session and then met this."""
        call = self.spawns(index)[-1]
        call["on_session_init"](call["backend_session_id"] or f"native-{index}")
        call["on_error"](message)

    def starts(self, index):
        return self.started_at[f"trace-{index}"]

    def sessions_started(self):
        """Every running process got as far as starting its session, which is
        when its prompt is in the conversation."""
        for index in range(len(self.agents)):
            call = self.spawns(index)[-1]
            call["on_session_init"](call["backend_session_id"] or f"native-{index}")

    def waiting(self):
        return [self.state(index)["detail"].get("account_recovery")
                for index in range(len(self.agents))]

    def kinds(self):
        return [self.state(index)["kind"] for index in range(len(self.agents))]

    def state(self, index):
        return agents_db.latest_state(self.agents[index])

    def recovery_messages(self, index):
        return sum("Clarp recovered from" in call["text"] for call in self.spawns(index))


def _visible_failure(state, *, category, says):
    assert state["kind"] == AgentState.INTERRUPTED, state
    assert state["detail"]["reason"] == category
    shown = " ".join(str(state["detail"].get(key) or "")
                     for key in ("message", "error", "provider_message"))
    assert says in shown, shown


BACKENDS = ["codex", "claude"]


def _evening(tmp_path, monkeypatch, backend):
    """The real loop. Five turns are parked by a genuine usage limit; one of
    them wants a model the account the selector picks does not serve; the
    selector answers available with a models list that lacks it."""
    fleet = Fleet(tmp_path, monkeypatch, backend, [*GOOD, BAD])
    start = fleet.clock[0]
    fleet.limit(0)                                    # the account ran out
    assert fleet.coordinator.recovering
    fleet.run_until(start)                            # stop the others, ask the selector

    # The four the answer covers are resumed. The fifth is neither launched
    # again only to fail nor ended: it waits, and its state says for what.
    assert fleet.selector_calls() == [sorted({*GOOD, BAD})]
    assert [len(fleet.spawns(index)) for index in range(5)] == [2, 2, 2, 2, 1]
    assert fleet.kinds() == [AgentState.THINKING] * 5
    assert fleet.waiting() == [None] * 4 + ["waiting"]
    assert fleet.state(4)["detail"]["message"] == (
        f"Waiting for a {backend} account that serves {BAD}; the active one does not right now")
    assert not fleet.coordinator.recovering
    fleet.sessions_started()
    return fleet, start


@pytest.mark.parametrize("backend", BACKENDS)
def test_the_evenings_loop_cannot_happen_while_the_others_work(tmp_path, monkeypatch, backend):
    """While the four work, nothing is done on the fifth's behalf: they are
    not stopped and the selector, which switches the account, is not run. At
    its half hour the fifth runs once and fails with the provider's sentence."""
    fleet, start = _evening(tmp_path, monkeypatch, backend)

    moment = start
    while moment + 30 < start + MAX_PARK_SECONDS:
        moment += 30
        fleet.run_until(moment)
        assert len(fleet.selector_calls()) == 1
        assert [len(fleet.spawns(index)) for index in range(5)] == [2, 2, 2, 2, 1]
        assert fleet.waiting() == [None] * 4 + ["waiting"]
    fleet.run_until(start + MAX_PARK_SECONDS + RECHECK_SECONDS)

    assert fleet.starts(4)[1:] and fleet.starts(4)[-1] <= start + MAX_PARK_SECONDS + RECHECK_SECONDS
    assert [len(fleet.spawns(index)) for index in range(5)] == [2, 2, 2, 2, 2]
    fleet.spawns(4)[-1]["on_error"](NOT_OFFERED)
    _visible_failure(fleet.state(4), category="account_plan", says="'gpt-6' model is not supported")
    # One round in all: one selector call, one recovery message each, no timer left.
    fleet.run_until(start + 2 * MAX_PARK_SECONDS)
    assert len(fleet.selector_calls()) == 1 and fleet.scheduled == []
    assert not fleet.coordinator.recovering
    assert [len(fleet.spawns(index)) for index in range(4)] == [2, 2, 2, 2]
    assert [fleet.recovery_messages(index) for index in range(4)] == [1, 1, 1, 1]
    assert fleet.kinds()[:4] == [AgentState.THINKING] * 4 and fleet.waiting()[:4] == [None] * 4


@pytest.mark.parametrize("backend", BACKENDS)
def test_the_evenings_loop_cannot_happen_once_the_others_are_done(tmp_path, monkeypatch, backend):
    """With no turn of the pool running the selector may be asked for the
    fifth: once a minute, never sooner, and to its half hour at the latest."""
    fleet, start = _evening(tmp_path, monkeypatch, backend)
    fleet.run_until(start + 100)
    for index in range(4):
        fleet.spawns(index)[-1]["on_result"]({"duration_ms": 5})
    assert fleet.kinds()[:4] == [AgentState.DONE] * 4

    fleet.run_until(start + MAX_PARK_SECONDS - 1)
    asked = fleet.asked_at[1:]
    assert asked and all(models == [BAD] for models in fleet.selector_calls()[1:])
    assert asked[0] - start >= 100
    assert min(later - earlier for earlier, later in zip(fleet.asked_at, fleet.asked_at[1:])) >= (
        RECHECK_SECONDS)
    assert len(fleet.spawns(4)) == 1 and fleet.waiting()[4] == "waiting"      # not ended early

    fleet.run_until(start + MAX_PARK_SECONDS + RECHECK_SECONDS)
    assert len(fleet.spawns(4)) == 2
    fleet.spawns(4)[-1]["on_error"](NOT_OFFERED)
    _visible_failure(fleet.state(4), category="account_plan", says="'gpt-6' model is not supported")
    fleet.run_until(start + 2 * MAX_PARK_SECONDS)
    assert fleet.scheduled == [] and not fleet.coordinator.recovering
    assert len(fleet.spawns(4)) == 2


PROBE = {"codex": ["gpt-6.1-sol", "gpt-6.1-sol", "gpt-6-sol", "gpt-6-astra", "gpt-6-astra"],
         "claude": ["sonnet", "sonnet", "opus", "fable", "fable"]}


def _probe(tmp_path, monkeypatch, backend):
    """The reviewer's probe. Five turns working on three models; one meets a
    genuine usage limit; the selector answers available for two of the three.
    Nobody is ended: the three it covers work on, the two it does not wait."""
    models = PROBE[backend]
    fleet = Fleet(tmp_path, monkeypatch, backend, models, unserved=(models[-1],))
    start = fleet.clock[0]
    fleet.limit(0)
    fleet.run_until(start)

    assert fleet.kinds() == [AgentState.THINKING] * 5, "a turn was ended"
    assert [len(fleet.spawns(index)) for index in range(5)] == [2, 2, 2, 1, 1]
    assert fleet.waiting() == [None, None, None, "waiting", "waiting"]
    for index in (3, 4):
        assert fleet.state(index)["detail"]["message"] == (
            f"Waiting for a {backend} account that serves {models[-1]}; "
            "the active one does not right now")
        assert fleet.coordinator.first_parked[fleet.key(index)] == start
    fleet.sessions_started()
    return fleet, start


@pytest.mark.parametrize("backend", BACKENDS)
def test_turns_a_partial_answer_leaves_out_are_resumed_when_a_later_answer_covers_them(
        tmp_path, monkeypatch, backend):
    fleet, start = _probe(tmp_path, monkeypatch, backend)
    model = PROBE[backend][-1]

    fleet.run_until(start + 600)                        # ten minutes of work by the three
    assert len(fleet.selector_calls()) == 1, "the selector ran under working turns"
    assert [len(fleet.spawns(index)) for index in range(5)] == [2, 2, 2, 1, 1]
    for index in range(3):
        fleet.spawns(index)[-1]["on_result"]({"duration_ms": 5})

    fleet.run_until(start + 720)                        # nothing runs: asked, still not served
    assert fleet.selector_calls()[1:] and set(map(tuple, fleet.selector_calls()[1:])) == {(model,)}
    assert fleet.waiting()[3:] == ["waiting"] * 2 and fleet.kinds()[3:] == [AgentState.THINKING] * 2

    fleet.no_account_offers()                           # an account serves it again
    fleet.run_until(start + 800)
    assert [len(fleet.spawns(index)) for index in (3, 4)] == [2, 2]
    assert fleet.waiting()[3:] == [None, None] and not fleet.coordinator.recovering
    assert fleet.scheduled == []
    assert [fleet.recovery_messages(index) for index in (3, 4)] == [1, 1]
    for index in (3, 4):
        fleet.spawns(index)[-1]["on_result"]({"duration_ms": 5})
    assert fleet.kinds() == [AgentState.DONE] * 5


@pytest.mark.parametrize("backend", BACKENDS)
def test_turns_a_partial_answer_leaves_out_are_released_at_their_own_cap(
        tmp_path, monkeypatch, backend):
    """The three keep working for longer than half an hour. The two are then
    released to run and fail with the provider's own message, as the cap does."""
    fleet, start = _probe(tmp_path, monkeypatch, backend)

    fleet.run_until(start + MAX_PARK_SECONDS - 1)
    assert [len(fleet.spawns(index)) for index in range(5)] == [2, 2, 2, 1, 1]
    fleet.run_until(start + MAX_PARK_SECONDS + RECHECK_SECONDS)

    assert [len(fleet.spawns(index)) for index in range(5)] == [2, 2, 2, 2, 2]
    assert len(fleet.selector_calls()) == 1
    for index in (3, 4):
        fleet.spawns(index)[-1]["on_error"](USAGE_LIMIT)
        _visible_failure(fleet.state(index), category="usage_limit", says="usage limit")
    # The three were never stopped for it, and nothing is left to start a round.
    assert fleet.kinds()[:3] == [AgentState.THINKING] * 3 and fleet.waiting()[:3] == [None] * 3
    assert [len(fleet.spawns(index)) for index in range(3)] == [2, 2, 2]
    assert not fleet.coordinator.recovering
    fleet.run_until(start + 2 * MAX_PARK_SECONDS)
    assert fleet.scheduled == [] and len(fleet.selector_calls()) == 1


@pytest.mark.parametrize("backend", BACKENDS)
def test_a_working_turns_own_limit_asks_for_the_parked_ones_too_and_keeps_their_clock(
        tmp_path, monkeypatch, backend):
    """A covered turn that later meets its own limit starts a recovery as
    usual. That one stops the working turns (their account is out) and asks
    for every model, the parked turns' included. Their park is not begun
    again: the half hour still counts from their first park."""
    fleet, start = _probe(tmp_path, monkeypatch, backend)
    models = PROBE[backend]

    fleet.run_until(start + 600)
    fleet.limit(0)
    fleet.run_until(start + 600)

    assert fleet.selector_calls()[-1] == sorted(set(models)) and len(fleet.selector_calls()) == 2
    assert [len(fleet.spawns(index)) for index in range(5)] == [3, 3, 3, 1, 1]
    assert fleet.waiting() == [None, None, None, "waiting", "waiting"]
    assert [fleet.coordinator.first_parked[fleet.key(index)] for index in (3, 4)] == [start] * 2
    assert f"serves {models[-1]}" in fleet.state(3)["detail"]["message"]
    fleet.sessions_started()

    fleet.run_until(start + MAX_PARK_SECONDS + RECHECK_SECONDS)
    assert [len(fleet.spawns(index)) for index in range(5)] == [3, 3, 3, 2, 2]


def _dispatch(fleet, session, model):
    agent = agents_db.create_agent(persona=session, voice_id="V", cwd=str(fleet.tmp_path),
                                   session=session, backend=fleet.backend, model=model)
    agents_db.start_runtime(agent, session)
    fleet.service.dispatch(text=f"work of {session}", requested_session=session,
                           forced_session=session, trace_id=f"trace-{session}",
                           synthesize_audio=False)
    return agent, [call for _, call in fleet.backends.spawned
                   if call["trace_id"] == f"trace-{session}"]


def test_a_turn_that_joined_the_parked_ones_later_keeps_its_own_half_hour(tmp_path, monkeypatch):
    """Each parked turn's cap counts from its own first park, also once a
    recovery has begun for them all."""
    fleet = Fleet(tmp_path, monkeypatch, "claude", ["fable", "sonnet"], unserved=("fable",))
    start = fleet.clock[0]
    fleet.limit(0)
    fleet.run_until(start)
    fleet.sessions_started()
    fleet.run_until(start + 600)
    late, _spawns = _dispatch(fleet, "late-fable", "fable")
    fleet.spawns(1)[-1]["on_result"]({"duration_ms": 5})           # now nothing runs

    def late_spawns():
        return [call for _, call in fleet.backends.spawned if call["trace_id"] == "trace-late-fable"]

    fleet.run_until(start + MAX_PARK_SECONDS + RECHECK_SECONDS)
    assert len(fleet.spawns(0)) == 2                                # the first one's cap
    assert late_spawns() == []
    assert agents_db.latest_state(late)["detail"]["account_recovery"] == "waiting"
    fleet.run_until(start + 600 + MAX_PARK_SECONDS - 1)
    assert late_spawns() == []
    fleet.run_until(start + 600 + MAX_PARK_SECONDS + 2 * RECHECK_SECONDS)
    assert len(late_spawns()) == 1                                  # and its own, ten minutes on


def test_fables_weekly_limit_parks_the_fable_turns_and_leaves_sonnet_working(
        tmp_path, monkeypatch):
    """Claude's limits are per model. Fable's week is spent on every account
    and Sonnet is not: the selector answers available for Sonnet. The Fable
    turns wait for the reset; they are not ended, and the Sonnet turns are not
    stopped again for them."""
    weekly = "You've hit your weekly limit for Fable · resets 3pm"
    fleet = Fleet(tmp_path, monkeypatch, "claude",
                  ["fable", "fable", "fable", "sonnet", "sonnet"], unserved=("fable",))
    start = fleet.clock[0]
    fleet.limit(0, weekly)
    fleet.run_until(start)

    assert fleet.kinds() == [AgentState.THINKING] * 5
    assert fleet.waiting() == ["waiting"] * 3 + [None, None]
    assert [len(fleet.spawns(index)) for index in range(5)] == [1, 1, 1, 2, 2]
    fleet.sessions_started()

    # A Fable turn that arrives now waits with them instead of running into
    # the limit and stopping the pool; a Sonnet turn that arrives now runs.
    fleet.run_until(start + 200)
    late, late_spawns = _dispatch(fleet, "late-fable", "fable")
    assert late_spawns == []
    assert agents_db.latest_state(late)["detail"]["account_recovery"] == "waiting"
    assert agents_db.latest_state(late)["detail"]["message"] == (
        "Waiting for a claude account that serves fable; the active one does not right now")
    assert fleet.coordinator.first_parked[(late, "trace-late-fable")] == start + 200
    _other, other_spawns = _dispatch(fleet, "late-sonnet", "sonnet")
    assert len(other_spawns) == 1

    fleet.run_until(start + 900)
    assert len(fleet.selector_calls()) == 1
    assert [len(fleet.spawns(index)) for index in range(5)] == [1, 1, 1, 2, 2]
    for index in (3, 4):
        fleet.spawns(index)[-1]["on_result"]({"duration_ms": 5})
    other_spawns[-1]["on_result"]({"duration_ms": 5})

    fleet.run_until(start + 1100)                       # nothing runs: asked, Fable still spent
    assert set(map(tuple, fleet.selector_calls()[1:])) == {("fable",)}
    fleet.no_account_offers()                           # the reset
    fleet.run_until(start + 1200)

    assert [len(fleet.spawns(index)) for index in range(3)] == [2, 2, 2]
    assert fleet.kinds()[:3] == [AgentState.THINKING] * 3 and fleet.waiting()[:3] == [None] * 3
    assert len([call for _, call in fleet.backends.spawned
                if call["trace_id"] == "trace-late-fable"]) == 1
    assert agents_db.latest_state(late)["detail"].get("account_recovery") is None
    assert not fleet.coordinator.recovering and fleet.scheduled == []


@pytest.mark.parametrize("backend", BACKENDS)
def test_a_plan_failure_does_not_stop_the_turns_that_run_fine(tmp_path, monkeypatch, backend):
    """How the evening began: four turns working, a fifth whose model the
    account does not offer."""
    fleet = Fleet(tmp_path, monkeypatch, backend, [*GOOD, BAD])
    before = [fleet.state(index) for index in range(4)]

    for _ in range(5):                                # the helper kept being told to try
        fleet.spawns(4)[-1]["on_error"](NOT_OFFERED)
        fleet.run_scheduled(advance=90)

    # The four were never paused, stopped, resumed or told to recover.
    assert [fleet.state(index) for index in range(4)] == before
    assert [len(fleet.spawns(index)) for index in range(4)] == [1, 1, 1, 1]
    assert [fleet.recovery_messages(index) for index in range(4)] == [0, 0, 0, 0]
    assert all(_td._INFLIGHT.get(fleet.agents[index]) == f"trace-{index}" for index in range(4))
    # The selector switches accounts while it probes; it must not run under them.
    assert fleet.selector_calls() == []
    assert not fleet.coordinator.recovering and fleet.scheduled == []
    # The fifth ended at once, with the provider's sentence.
    assert len(fleet.spawns(4)) == 1
    _visible_failure(fleet.state(4), category="account_plan", says="'gpt-6' model is not supported")


@pytest.mark.parametrize("backend", BACKENDS)
def test_a_plan_failure_alone_ends_after_three_checks_find_no_account(
        tmp_path, monkeypatch, backend):
    """With nothing else running the selector may look for an account that
    offers the model. None does. The turn waits for three such answers (a
    plan can be put right) and then ends; it does not get the half hour a
    usage window gets."""
    fleet = Fleet(tmp_path, monkeypatch, backend, [BAD])

    fleet.spawns(0)[-1]["on_error"](NOT_OFFERED)
    fleet.run_scheduled()
    assert fleet.selector_calls() == [[BAD]]
    assert fleet.state(0)["detail"]["account_recovery"] == "waiting"   # paused, not failed
    fleet.run_scheduled(advance=RECHECK_SECONDS + 1)
    assert fleet.state(0)["detail"]["account_recovery"] == "waiting"
    fleet.run_scheduled(advance=RECHECK_SECONDS + 1)

    assert fleet.selector_calls() == [[BAD]] * account_failover.PLAN_CHECKS
    assert len(fleet.spawns(0)) == 1
    # With the provider's own sentence, from its own failure.
    _visible_failure(fleet.state(0), category="account_plan", says="'gpt-6' model is not supported")
    assert "No saved" not in json.dumps(fleet.state(0)["detail"])
    assert not fleet.coordinator.recovering
    assert fleet.run_scheduled(advance=MAX_PARK_SECONDS) == 0


@pytest.mark.parametrize("backend", BACKENDS)
def test_a_usage_window_is_still_waited_out_when_no_account_has_usage(
        tmp_path, monkeypatch, backend):
    """Only a plan failure ends after three answers. A turn parked for lack of
    usage keeps waiting for a reset, to the park cap, as before."""
    fleet = Fleet(tmp_path, monkeypatch, backend, [BAD])

    fleet.spawns(0)[-1]["on_error"](USAGE_LIMIT)
    for _ in range(account_failover.PLAN_CHECKS + 3):
        fleet.run_scheduled(advance=RECHECK_SECONDS + 1)

    assert len(fleet.selector_calls()) > account_failover.PLAN_CHECKS
    assert fleet.state(0)["detail"]["account_recovery"] == "waiting"
    assert fleet.coordinator.recovering


@pytest.mark.parametrize("backend", BACKENDS)
def test_a_plan_failure_alone_moves_to_an_account_that_offers_the_model(
        tmp_path, monkeypatch, backend):
    """What account recovery is for: the active account lost its plan, another
    saved one has it."""
    fleet = Fleet(tmp_path, monkeypatch, backend, ["gpt-6-astra"], unserved=())

    fleet.spawns(0)[-1]["on_error"](
        "The 'gpt-6-astra' model is not supported when using Codex with a ChatGPT account.")
    fleet.run_scheduled()

    assert fleet.selector_calls() == [["gpt-6-astra"]]
    assert len(fleet.spawns(0)) == 2 and fleet.recovery_messages(0) == 1
    assert fleet.state(0)["kind"] == AgentState.THINKING


@pytest.mark.parametrize("backend", BACKENDS)
def test_a_plan_failure_right_after_a_recovery_ends_the_turn(tmp_path, monkeypatch, backend):
    """The selector said an account offers the model, the turn was resumed,
    and the account does not offer it. Waiting never helps a plan failure and
    asking again is the loop: the turn ends with the provider's sentence, and
    the other turns are left alone."""
    fleet = Fleet(tmp_path, monkeypatch, backend, [*GOOD, "gpt-6-astra"], unserved=())
    fleet.spawns(0)[-1]["on_error"](USAGE_LIMIT)
    fleet.run_scheduled()
    assert [len(fleet.spawns(index)) for index in range(5)] == [2, 2, 2, 2, 2]

    fleet.clock[0] += 3
    fleet.spawns(4)[-1]["on_error"](NOT_OFFERED)
    for _ in range(5):
        fleet.run_scheduled(advance=90)

    assert len(fleet.selector_calls()) == 1
    assert [len(fleet.spawns(index)) for index in range(5)] == [2, 2, 2, 2, 2]
    assert [fleet.recovery_messages(index) for index in range(5)] == [1, 1, 1, 1, 1]
    assert [fleet.state(index)["kind"] for index in range(4)] == [AgentState.THINKING] * 4
    _visible_failure(fleet.state(4), category="account_plan", says=NOT_OFFERED[:40])


@pytest.mark.parametrize("backend", BACKENDS)
def test_a_plan_failure_alone_right_after_its_recovery_ends_instead_of_looping(
        tmp_path, monkeypatch, backend):
    """The same with nothing else running, where a recovery would be allowed
    to start: a selector that keeps saying an account offers the model must
    not get to say it twice."""
    fleet = Fleet(tmp_path, monkeypatch, backend, [BAD], unserved=())
    fleet.spawns(0)[-1]["on_error"](NOT_OFFERED)
    fleet.run_scheduled()
    assert len(fleet.spawns(0)) == 2

    for _ in range(5):
        fleet.clock[0] += 3
        fleet.spawns(0)[-1]["on_error"](NOT_OFFERED)
        fleet.run_scheduled(advance=90)

    assert len(fleet.selector_calls()) == 1 and len(fleet.spawns(0)) == 2
    assert not fleet.coordinator.recovering
    _visible_failure(fleet.state(0), category="account_plan", says=NOT_OFFERED[:40])


AFTERNOON = ["fable", "fable", "sonnet"]


SIGN_IN = "OAuth session expired; please run /login"


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("failure", [USAGE_LIMIT, SIGN_IN], ids=["usage-limit", "sign-in"])
def test_a_relapse_is_waited_out_and_the_turns_end_working(
        tmp_path, monkeypatch, backend, failure):
    """A usage limit or a failed sign-in soon after a resume is not the
    evening's loop: waiting does help. The turns are parked again and work
    when an account has usage."""
    fleet = Fleet(tmp_path, monkeypatch, backend, AFTERNOON, unserved=())
    start = fleet.clock[0]
    fleet.limit(0)
    fleet.run_until(start)
    fleet.run_until(start + 90)
    fleet.no_account_offers("fable", "sonnet")
    fleet.limit(0, failure)
    assert fleet.coordinator.relapses == 1

    moment = start + 90
    while moment < start + 90 + 20 * 60:                # twenty minutes without an account
        moment += 30
        fleet.run_until(moment)
        assert fleet.kinds() == [AgentState.THINKING] * 3 and fleet.waiting() == ["waiting"] * 3
    assert [len(fleet.spawns(index)) for index in range(3)] == [2, 2, 2]

    fleet.no_account_offers()                           # the windows reset
    fleet.run_until(moment + RECHECK_SECONDS)

    assert not fleet.coordinator.recovering and fleet.scheduled == []
    assert [len(fleet.spawns(index)) for index in range(3)] == [3, 3, 3]
    assert fleet.kinds() == [AgentState.THINKING] * 3 and fleet.waiting() == [None] * 3
    for index in range(3):
        fleet.spawns(index)[-1]["on_result"]({"duration_ms": 5})
    assert fleet.kinds() == [AgentState.DONE] * 3


def _journal(clock):
    hours, minutes, seconds = (int(part) for part in clock.split(":"))
    return float(hours * 3600 + minutes * 60 + seconds)


@pytest.mark.parametrize("relapse, backend, failure", [
    *[(relapse, "claude", USAGE_LIMIT) for relapse in (3, 60, 119, 121, 145, 400)],
    (25, "codex", USAGE_LIMIT), (25, "claude", SIGN_IN), (25, "codex", SIGN_IN),
], ids=lambda value: {USAGE_LIMIT: "usage-limit", SIGN_IN: "sign-in"}.get(value, str(value)))
def test_a_turn_that_keeps_meeting_the_limit_ends_after_five_rounds(
        tmp_path, monkeypatch, relapse, backend, failure):
    """The reviewer's table. The selector answers available every time (its
    small probe passes) and one turn meets the limit again ``relapse`` seconds
    after every resume. Whatever that gap, the fifth relapse ends the turn,
    visibly; the selector's next run waits 60, 120, 240, then 480 seconds; and
    the three other turns are stopped and resumed five times, not for ever."""
    fleet = Fleet(tmp_path, monkeypatch, backend, GOOD, unserved=())
    start = fleet.clock[0]
    fleet.selector_seconds = 30                         # the probes take about this long
    fleet.limit(0, failure)
    fleet.run_until(start + 30)
    relapses = []
    while fleet.state(0)["kind"] != AgentState.INTERRUPTED:
        assert len(relapses) < 5, "it never gives up"
        assert fleet.waiting() == [None] * 4            # resumed, all four
        fleet.sessions_started()
        fleet.run_until(fleet.clock[0] + relapse)
        relapses.append(fleet.clock[0])
        fleet.spawns(0)[-1]["on_error"](failure)
        running = len(fleet.spawns(1))
        while len(fleet.spawns(1)) == running and fleet.coordinator.recovering:
            fleet.run_until(fleet.clock[0] + 1)

    assert len(relapses) == MAX_RELAPSES == 5
    assert fleet.state(0)["detail"]["reason"] == ("usage_limit" if failure == USAGE_LIMIT else "auth")
    # Told once: the resumes after a relapse carried one line, not the message.
    texts = [call["text"] for call in fleet.spawns(0)]
    assert [text.count("Clarp recovered from") for text in texts] == [0, 1, 0, 0, 0, 0]
    assert len(set(texts[2:])) == 1 and "work of agent-0" not in texts[2] and len(texts[2]) < 260
    assert texts[2].count("<clarp-account-recovery>") == 1
    # How long the selector waited after each relapse before it ran again.
    assert [asked - relapsed for asked, relapsed in zip(fleet.asked_at[1:], relapses)] == [
        60, 120, 240, 480]
    assert [len(fleet.spawns(index)) for index in range(4)] == [6, 6, 6, 6]
    # The others work on, and nothing is left that could start another round.
    assert fleet.kinds()[1:] == [AgentState.THINKING] * 3 and fleet.waiting()[1:] == [None] * 3
    assert not fleet.coordinator.recovering and fleet.scheduled == []
    assert fleet.coordinator.park_expired == set()
    assert fleet.clock[0] - start == 30 + 5 * relapse + 900 + 4 * 30


@pytest.mark.parametrize("first, back", [(145, "15:01:02"), (100, "15:01:02"), (145, "15:02:06")],
                         ids=["as-it-happened", "first-relapse-after-100s", "usage-back-a-check-later"])
def test_the_afternoon_replayed_from_the_journal_is_held_until_usage_is_back(
        tmp_path, monkeypatch, first, back):
    """laptopstudio, 11 Nov, with the journal's times: limit 14:27:10,
    recovered 14:28:46, the limit again after 145 s, recovered 14:32:58, the
    limit again after 60 s, then "no account" from 14:35 until usage was back
    at 15:01:02. The turns are held until then and end working, also when the
    first relapse comes after 100 s: each park has its own half hour."""
    fleet = Fleet(tmp_path, monkeypatch, "claude", ["fable", "sonnet", "fable", "sonnet"],
                  unserved=())
    fleet.clock[0] = _journal("14:27:10")
    fleet.selector_seconds = 96
    fleet.limit(0)
    fleet.run_until(_journal("14:28:46"))
    assert [len(fleet.spawns(index)) for index in range(4)] == [2, 2, 2, 2]
    fleet.sessions_started()

    fleet.run_until(_journal("14:28:46") + first)
    fleet.selector_seconds = 47
    fleet.limit(0)                                      # one relapse: the selector waits 60 s
    fleet.run_until(_journal("14:28:46") + first + 60 + 47)
    assert [len(fleet.spawns(index)) for index in range(4)] == [3, 3, 3, 3]
    if first == 145:
        assert fleet.clock[0] == _journal("14:32:58")   # when the journal says "recovered"
    fleet.sessions_started()

    second = fleet.clock[0] + 60
    fleet.run_until(second)
    fleet.no_account_offers("fable", "sonnet")          # nothing has usage any more
    fleet.selector_seconds = 4
    fleet.limit(0)                                      # two relapses: it waits 120 s
    moment = second
    while moment < _journal(back):
        moment = min(moment + 16, _journal(back))
        fleet.run_until(moment)
        assert fleet.kinds() == [AgentState.THINKING] * 4, f"ended early at {moment}"
        assert fleet.waiting() == ["waiting"] * 4
        assert fleet.coordinator.park_expired == set()
    checks = fleet.asked_at[2:]
    assert checks[0] == second + 120
    assert {later - earlier for earlier, later in zip(checks, checks[1:])} == {RECHECK_SECONDS + 4}

    fleet.no_account_offers()                           # usage is back
    fleet.run_until(_journal(back) + RECHECK_SECONDS + 8)

    assert [len(fleet.spawns(index)) for index in range(4)] == [4, 4, 4, 4]
    assert _journal(back) <= fleet.starts(0)[-1] <= _journal(back) + RECHECK_SECONDS + 8
    for index in range(4):
        # One recovery message; the two resumes after a relapse carry one line.
        assert [call["text"].count("Clarp recovered from") for call in fleet.spawns(index)] == [
            0, 1, 0, 0]
    assert not fleet.coordinator.recovering and fleet.coordinator.park_expired == set()
    assert fleet.kinds() == [AgentState.THINKING] * 4 and fleet.waiting() == [None] * 4
    fleet.sessions_started()
    for index in range(4):
        fleet.spawns(index)[-1]["on_result"]({"duration_ms": 5})
    assert fleet.kinds() == [AgentState.DONE] * 4


@pytest.mark.parametrize("backend", BACKENDS)
def test_a_process_that_never_started_its_session_is_told_again(tmp_path, monkeypatch, backend):
    """The message is left out only where it is known to be in the
    conversation. A resumed process stopped before it started its session
    never delivered it."""
    fleet = Fleet(tmp_path, monkeypatch, backend, ["sonnet", "sonnet"], unserved=())
    fleet.limit(0)
    fleet.run_until(fleet.clock[0])
    fleet.spawns(0)[-1]["on_session_init"](
        fleet.spawns(0)[-1]["backend_session_id"] or "native-0")     # only the first got that far
    fleet.run_until(fleet.clock[0] + 5)
    fleet.spawns(0)[-1]["on_error"](USAGE_LIMIT)
    fleet.run_until(fleet.clock[0] + RECHECK_SECONDS)

    assert [len(fleet.spawns(index)) for index in range(2)] == [3, 3]
    assert [call["text"].count("Clarp recovered from") for call in fleet.spawns(0)] == [0, 1, 0]
    assert [call["text"].count("Clarp recovered from") for call in fleet.spawns(1)] == [0, 1, 1]

    # Once the message is known to have arrived, a later process of the same
    # park that is stopped before its session starts does not bring it back.
    fleet.spawns(1)[-1]["on_session_init"](fleet.spawns(1)[-1]["backend_session_id"] or "native-1")
    fleet.run_until(fleet.clock[0] + 5)
    fleet.spawns(1)[-1]["on_error"](USAGE_LIMIT)        # the first one's third process never began
    fleet.run_until(fleet.clock[0] + 2 * RECHECK_SECONDS)             # a second relapse: 120 s
    assert [call["text"].count("Clarp recovered from") for call in fleet.spawns(0)] == [0, 1, 0, 0]
    assert [call["text"].count("Clarp recovered from") for call in fleet.spawns(1)] == [0, 1, 1, 0]


class _Pool:
    """The coordinator alone, with turns that fail as a script says and a
    selector that answers as a script says, on a simulated clock."""

    def __init__(self, seed, *, answers, failures, selector_seconds, turns=4):
        self.rng = random.Random(seed)
        self.answers, self.failures, self.selector_seconds = answers, failures, selector_seconds
        self.clock, self.timers, self.sequence = 0.0, [], 0
        self.asked, self.restarts, self.broken, self.requesting = [], [], [], False
        self.coordinator = AccountFailover(
            threading.RLock(), switch=self.switch, now=lambda: self.clock,
            schedule=lambda delay, callback: self.at(self.clock + delay, callback))
        self.turns = [_Turn(self, f"agent-{index}", self.rng.choice(["fable", "sonnet", "opus"]))
                      for index in range(turns)]
        for turn in self.turns:
            self.at(float(self.rng.randrange(31)), turn.start)   # whole seconds: exact sums

    def at(self, due, callback):
        self.sequence += 1
        heapq.heappush(self.timers, (due, self.sequence, callback))

    def switch(self, command, models):
        self.asked.append(self.clock)
        expired = self.coordinator.park_expired
        self.broken += [f"the selector ran under {turn.agent} at {self.clock}"
                        for turn in self.turns
                        if turn.running and (turn.agent, "trace") not in expired]
        self.clock += self.rng.choice(self.selector_seconds)
        answer = self.rng.choice(self.answers)
        if answer == "some":
            return frozenset(self.rng.sample(models, self.rng.randint(0, len(models))))
        if answer == "raise":
            raise RuntimeError("selector broke")
        return answer

    def run(self, seconds):
        while self.timers and self.timers[0][0] <= seconds:
            due, _, callback = heapq.heappop(self.timers)
            self.clock = max(self.clock, due)
            callback()


class _Turn:
    def __init__(self, pool, agent, model):
        self.pool, self.agent, self.model = pool, agent, model
        self.starts, self.parks, self.ended, self.process = [], [], None, 0
        self.running, self.plan_failed = False, False

    def paused(self):
        pool = self.pool
        self.parks.append(pool.clock)
        if self.running and not pool.requesting:
            pool.broken.append(f"{self.agent} was stopped at {pool.clock} though no turn failed")
        self.running = False

    def ended_by_the_coordinator(self):
        if not self.plan_failed:
            self.pool.broken.append(f"{self.agent} was ended though its plan never failed")
        self.end()

    def start(self):
        pool = self.pool
        self.process += 1
        process = self.process
        self.attempt = Attempt(
            self.agent, "trace", self.model, {}, owned=lambda: self.ended is None,
            pause=self.paused, resume=self.start, fail=self.ended_by_the_coordinator)
        self.attempt.spawned.set()
        if pool.coordinator.register(self.attempt):
            return                                      # parked before it ran
        self.running = True
        self.starts.append(pool.clock)
        if len(self.starts) > 1:
            pool.restarts.append(pool.clock)
        after, kind = pool.rng.choice(pool.failures)
        if self is pool.turns[0] and len(self.starts) == 1:
            after, kind = 5, "usage"                    # something always happens
        if kind:
            pool.at(pool.clock + after, lambda: self.fails(process, kind))

    def fails(self, process, kind):
        if (self.ended is not None or process != self.process
                or self.attempt.state.get("account_recovery")):
            return                                      # that process was stopped
        self.running, self.plan_failed = False, self.plan_failed or kind == "plan"
        self.pool.requesting = True
        try:
            parked = self.pool.coordinator.request(self.agent, "trace", ("selector",),
                                                   plan=kind == "plan")
        finally:
            self.pool.requesting = False
        if not parked:
            self.end()

    def end(self):
        self.running = False
        self.ended = self.pool.clock
        self.pool.coordinator.discard(self.agent, "trace")


ANY_ANSWER = [True, True, False, None, "some", "some", "some", "raise"]
ANY_FAILURE = [(after, kind) for after in (1, 3, 25, 59, 60, 61, 90, 119, 120, 121, 145, 400)
               for kind in ("usage", "auth", "plan")] + [(0, "")] * 6


@pytest.mark.parametrize("seed", range(300))
def test_no_selector_answers_restart_turns_more_often_than_the_recheck(seed):
    """Whatever the selector answers (yes, no, some models or none of them,
    nothing, a crash), however long it takes, and whenever and however the
    turns fail: no turn is restarted twice within the recheck interval, and
    neither is the pool. And a working turn is stopped only when a turn of
    the pool failed on its account, the selector never runs under one, and
    the coordinator ends no turn whose plan did not fail."""
    pool = _Pool(seed, answers=ANY_ANSWER, failures=ANY_FAILURE, selector_seconds=[0, 0, 5, 40])
    pool.run(3 * 3600)

    assert pool.broken == []

    assert pool.asked, "nothing happened"
    for turn in pool.turns:
        restarts = turn.starts[1:]
        assert all(later - earlier >= RECHECK_SECONDS
                   for earlier, later in zip(restarts, restarts[1:])), (turn.agent, restarts)
    moments = sorted(set(pool.restarts))
    assert all(later - earlier >= RECHECK_SECONDS
               for earlier, later in zip(moments, moments[1:])), moments


@pytest.mark.parametrize("seed", range(100))
def test_an_account_that_always_fails_ends_every_turn_within_five_rounds(seed):
    """The selector always says available and every resumed turn meets its
    limit again, after anything from a second to nearly seven minutes. The
    pool asks the selector five times in all, and every turn ends."""
    slowest, latest = 40, 400
    pool = _Pool(seed, answers=[True], selector_seconds=[0, 5, slowest],
                 failures=[(after, kind) for after in (1, 25, 59, 100, 119, 121, 145, latest)
                           for kind in ("usage", "auth")])
    pool.run(3 * 3600)

    assert not pool.timers and not pool.coordinator.recovering and pool.broken == []
    assert len(pool.asked) == MAX_RELAPSES
    waits = sum(RECHECK_SECONDS * 2 ** round_ for round_ in range(MAX_RELAPSES - 1))
    for turn in pool.turns:
        assert turn.ended is not None, turn.agent
        assert len(turn.starts) - 1 <= MAX_RELAPSES
        assert turn.ended - pool.asked[0] <= (
            MAX_RELAPSES * (slowest + latest) + waits + latest), turn.agent


@pytest.mark.parametrize("backend", BACKENDS)
def test_the_recovery_message_is_the_same_one_each_time_never_one_inside_another(
        tmp_path, monkeypatch, backend):
    """Processes stopped before they started their session never delivered
    the message, so the next resume carries it again: the same one, built
    from the request, not one wrapped inside the other."""
    fleet = Fleet(tmp_path, monkeypatch, backend, AFTERNOON, unserved=())
    start = fleet.clock[0]
    fleet.spawns(0)[-1]["on_error"](USAGE_LIMIT)
    fleet.run_until(start)
    fleet.run_until(start + 90)
    fleet.spawns(0)[-1]["on_error"](USAGE_LIMIT)
    fleet.run_until(start + 90 + RECHECK_SECONDS)

    for index in range(3):
        first, second, third = [call["text"] for call in fleet.spawns(index)]
        assert "Clarp recovered from" not in first
        assert second.count("Clarp recovered from") == 1
        assert second.count("<clarp-account-recovery>") == 1
        assert third == second, "the message grew or was wrapped again"
        assert second.count(f"work of agent-{index}") == 1


@pytest.mark.parametrize("backend", BACKENDS)
def test_a_relapse_begins_a_new_half_hour_as_it_always_did(tmp_path, monkeypatch, backend):
    """The wait is bounded per park. A relapse is a new park: half an hour
    without an account counts from there, not from the first limit."""
    fleet = Fleet(tmp_path, monkeypatch, backend, AFTERNOON, unserved=())
    start = fleet.clock[0]
    fleet.limit(0)
    fleet.run_until(start)
    fleet.run_until(start + 100)
    fleet.no_account_offers("fable", "sonnet")
    fleet.limit(0)
    assert [fleet.coordinator.first_parked[fleet.key(index)] for index in range(3)] == [
        start + 100] * 3

    fleet.run_until(start + 100 + MAX_PARK_SECONDS - 1)
    # Past half an hour from the first limit, short of it from the relapse.
    assert [len(fleet.spawns(index)) for index in range(3)] == [2, 2, 2]
    assert fleet.waiting() == ["waiting"] * 3 and fleet.coordinator.park_expired == set()

    fleet.run_until(start + 100 + MAX_PARK_SECONDS + RECHECK_SECONDS)
    assert [len(fleet.spawns(index)) for index in range(3)] == [3, 3, 3]
    assert not fleet.coordinator.recovering
    for index in range(3):
        fleet.spawns(index)[-1]["on_error"](USAGE_LIMIT)
    assert fleet.kinds() == [AgentState.INTERRUPTED] * 3
    assert not fleet.coordinator.recovering and fleet.scheduled == []


def test_a_turn_that_arrives_during_a_recovery_starts_its_own_park_clock(tmp_path, monkeypatch):
    fleet = Fleet(tmp_path, monkeypatch, "claude", ["sonnet"], unserved=("sonnet",))
    fleet.spawns(0)[-1]["on_error"](USAGE_LIMIT)
    fleet.run_scheduled()
    assert fleet.coordinator.recovering

    fleet.clock[0] += 300
    late = agents_db.create_agent(persona="late", voice_id="V", cwd=str(tmp_path),
                                  session="late", backend="claude", model="sonnet")
    agents_db.start_runtime(late, "late")
    fleet.service.dispatch(text="arrives during the wait", requested_session="late",
                           forced_session="late", trace_id="trace-late", synthesize_audio=False)

    # Parked before it ever spawned, and its half hour runs from now.
    assert [call for _, call in fleet.backends.spawned if call["trace_id"] == "trace-late"] == []
    assert agents_db.latest_state(late)["detail"]["account_recovery"] == "waiting"
    assert fleet.coordinator.first_parked[(late, "trace-late")] == fleet.clock[0]
    assert fleet.coordinator.first_parked[fleet.key(0)] == fleet.clock[0] - 300


@pytest.mark.parametrize("backend", BACKENDS)
def test_a_turn_that_worked_after_its_recovery_may_be_recovered_again(
        tmp_path, monkeypatch, backend):
    """The relapse rules are for a failure soon after a resume. A turn that
    worked for twenty minutes on the new account and then met its limit is a
    new recovery: asked at once, not counted, and told with the full message,
    since what it was told twenty minutes ago is long behind it."""
    fleet = Fleet(tmp_path, monkeypatch, backend, ["gpt-6.1-sol"], unserved=())
    start = fleet.clock[0]
    fleet.limit(0)
    fleet.run_until(start)
    assert len(fleet.spawns(0)) == 2
    fleet.sessions_started()

    fleet.run_until(start + 20 * 60)                  # twenty minutes of work
    fleet.spawns(0)[-1]["on_error"](USAGE_LIMIT)
    fleet.run_until(start + 20 * 60)

    assert fleet.asked_at == [start, start + 20 * 60] and fleet.coordinator.relapses == 0
    assert len(fleet.spawns(0)) == 3
    assert fleet.state(0)["kind"] == AgentState.THINKING
    # One message per recovery, never one wrapped inside the other.
    texts = [call["text"] for call in fleet.spawns(0)]
    assert [text.count("Clarp recovered from") for text in texts] == [0, 1, 1]
    assert texts[2] == texts[1]


def test_the_park_cap_counts_from_a_turns_first_park(tmp_path, monkeypatch):
    """A selector that says "no account" keeps a turn parked up to half an
    hour. A recovery that starts later for another turn must not give the
    first one a new half hour."""
    fleet = Fleet(tmp_path, monkeypatch, "claude", ["sonnet", "opus"],
                  unserved=("sonnet", "opus"))
    fleet.spawns(0)[-1]["on_error"](USAGE_LIMIT)
    fleet.run_scheduled()
    assert len(fleet.selector_calls()) == 1 and fleet.coordinator.recovering
    for _ in range(int(MAX_PARK_SECONDS / RECHECK_SECONDS) + 2):
        fleet.run_scheduled(advance=RECHECK_SECONDS + 1)

    # Both were released at the cap to run and fail visibly.
    assert not fleet.coordinator.recovering
    assert [len(fleet.spawns(index)) for index in range(2)] == [2, 2]
    fleet.spawns(0)[-1]["on_error"](USAGE_LIMIT)
    fleet.spawns(1)[-1]["on_error"](USAGE_LIMIT)
    assert [fleet.state(index)["kind"] for index in range(2)] == [AgentState.INTERRUPTED] * 2
    assert not fleet.coordinator.recovering and fleet.run_scheduled(advance=90) == 0


@pytest.mark.parametrize("payload,requested,expected", [
    ('{"available": true}', ["a", "b"], True),                       # an older selector
    ('{"available": true, "models": ["a", "b"]}', ["a", "b"], True),
    ('{"available": true, "models": ["b", "a", "c"]}', ["a", "b"], True),
    ('{"available": true, "models": ["a"]}', ["a", "b"], frozenset({"a"})),
    ('{"available": true, "models": []}', ["a", "b"], frozenset()),  # serves none of them now
    ('{"available": true, "models": ["z"]}', ["a", "b"], frozenset()),
    ('{"available": true, "models": "a"}', ["a", "b"], None),        # not a list: no verdict
    ('{"available": true, "models": ["a", 7]}', ["a", "b"], None),
    ('{"available": false, "models": ["a"]}', ["a", "b"], False),
])
def test_the_selectors_models_answer_is_read(payload, requested, expected):
    code = "import sys; print(sys.argv[1])"
    verdict = account_failover.switch_account((sys.executable, "-c", code, payload), requested)
    assert verdict == expected and type(verdict) is type(expected)


# --- a model id the backend's catalogue does not know ----------------------------
#
# The helpers were started with ``gpt-6``. Codex's own list had gpt-6.1-sol,
# gpt-6-sol and others, and the Host had read that list; nothing compared.


def test_a_new_turn_of_the_agent_forgets_the_old_ones_resume_time():
    """The bound is per turn. What a recovery did to an agent's earlier turn
    says nothing about its next one, and is not kept."""
    import threading
    from unittest.mock import Mock
    from lib.account_failover import Attempt

    def attempt(trace):
        item = Attempt("agent", trace, "sonnet", {}, Mock(return_value=True), Mock(), Mock(),
                       handle=Mock(drain_thread=None))
        item.spawned.set()
        return item

    scheduled = []
    coordinator = AccountFailover(threading.RLock(), switch=Mock(return_value=True),
                                  schedule=lambda delay, cb: scheduled.append(cb),
                                  now=lambda: 100.0)
    coordinator.register(attempt("first"))
    assert coordinator.request("agent", "first", ("selector",))
    scheduled.pop()()
    assert coordinator.resumed_at == {("agent", "first"): 100.0}

    coordinator.register(attempt("first"))            # its own respawn keeps it
    assert coordinator.resumed_at == {("agent", "first"): 100.0}
    coordinator.register(attempt("second"))           # a new turn does not
    assert coordinator.resumed_at == {} and coordinator.first_parked == {}
    assert coordinator.request("agent", "second", ("selector",))


def test_a_plan_parked_turn_counts_every_answer_without_its_model():
    """Three answers that do not serve its model end a turn parked for its
    plan, whether the selector says "no account" or names other models."""
    from unittest.mock import Mock

    answers = [frozenset(), False, frozenset({"other"})]
    clock, scheduled = [0.0], []
    coordinator = AccountFailover(
        threading.RLock(), switch=lambda command, models: answers.pop(0),
        schedule=lambda delay, cb: scheduled.append(cb), now=lambda: clock[0])
    item = Attempt("agent", "trace", "gpt-6", {}, Mock(return_value=True), Mock(), Mock(),
                   handle=Mock(drain_thread=None), fail=Mock())
    item.spawned.set()
    coordinator.register(item)
    assert coordinator.request("agent", "trace", ("selector",), plan=True)

    for expected in (0, 0, 1):
        scheduled.pop()()
        clock[0] += RECHECK_SECONDS
        assert item.fail.call_count == expected
    assert item.resume.call_count == 0 and not coordinator.recovering and scheduled == []


def _parked_beside_a_working_turn(answers):
    """The coordinator alone: ``a`` works on, ``b`` and ``c`` (one model)
    stay parked after an answer that serves only a's model."""
    from unittest.mock import Mock

    def attempt(agent, model):
        # Only a's process ever ran; the other two were parked before theirs.
        item = Attempt(agent, "trace", model, {}, Mock(return_value=True), Mock(), Mock(),
                       handle=Mock(drain_thread=None) if agent == "a" else None)
        item.spawned.set()
        return item

    clock, scheduled, asked = [0.0], [], []

    def switch(command, models):
        asked.append(list(models))
        return answers.pop(0)

    coordinator = AccountFailover(threading.RLock(), switch=switch, now=lambda: clock[0],
                                  schedule=lambda delay, cb: scheduled.append(cb))
    for agent, model in (("a", "sonnet"), ("b", "fable"), ("c", "fable")):
        coordinator.register(attempt(agent, model))
    parked = [coordinator.attempts["b"], coordinator.attempts["c"]]
    assert coordinator.request("a", "trace", ("selector",))
    scheduled.pop()()
    assert asked == [["fable", "sonnet"]] and not coordinator.recovering
    assert coordinator.register(attempt("a", "sonnet")) is False     # resumed: its new process
    assert len(scheduled) == 1                                      # the parked ones' timer
    return coordinator, clock, scheduled, asked, parked, attempt


def test_the_parked_turns_have_one_timer_however_often_it_was_armed():
    coordinator, clock, scheduled, asked, _parked, _attempt = _parked_beside_a_working_turn(
        [frozenset({"sonnet"}), frozenset({"sonnet"})])
    clock[0] += QUIET_SECONDS
    assert coordinator.request("a", "trace", ("selector",))          # a's own limit, much later
    scheduled.pop()()                                                # its pass: the same answer
    assert coordinator.register(_attempt("a", "sonnet")) is False
    older, newer = scheduled
    scheduled.clear()

    clock[0] += RECHECK_SECONDS
    older()
    assert scheduled == []                                           # superseded: it does nothing
    newer()
    assert len(scheduled) == 1 and len(asked) == 2                   # a works: looked, not asked


def test_a_timer_for_parked_turns_leaves_a_recovery_to_its_own_passes():
    coordinator, clock, scheduled, asked, _parked, _attempt = _parked_beside_a_working_turn(
        [frozenset({"sonnet"})])
    timer = scheduled.pop()
    clock[0] += QUIET_SECONDS
    assert coordinator.request("a", "trace", ("selector",)) and coordinator.recovering

    timer()

    assert len(asked) == 1 and len(scheduled) == 1                   # only the recovery's pass


def test_a_failed_hand_over_puts_a_turn_back_among_the_parked_ones():
    """The release drain took a parked turn and could not hand it over. It
    goes back to waiting with the others for its model; run now, it would
    meet the limit and stop the working turns."""
    coordinator, _clock, _scheduled, _asked, (first, second), _attempt = (
        _parked_beside_a_working_turn([frozenset({"sonnet"})]))
    taken = coordinator.take_parked(lambda item: item is first)
    assert taken == [first] and "b" not in coordinator.attempts

    assert coordinator.restore(taken) == [first]
    assert coordinator.attempts["b"] is first and first.resume.call_count == 0
    assert second.resume.call_count == 0


# --- the relapse count -----------------------------------------------------------------

def _pool_of(models, answers):
    """The coordinator alone with one running attempt for each model."""
    from unittest.mock import Mock

    def attempt(agent, model, *, owned=True, fail=None):
        item = Attempt(agent, "trace", model, {}, Mock(return_value=owned), Mock(), Mock(),
                       handle=Mock(drain_thread=None), fail=fail)
        item.spawned.set()
        return item

    clock, scheduled, asked = [0.0], [], []

    def switch(command, models):
        asked.append(clock[0])
        return answers.pop(0) if answers else True

    coordinator = AccountFailover(threading.RLock(), switch=switch, now=lambda: clock[0],
                                  schedule=lambda delay, cb: scheduled.append((clock[0] + delay, cb)))
    for index, model in enumerate(models):
        coordinator.register(attempt(f"agent-{index}", model))

    def run(until):
        while scheduled and min(scheduled, key=lambda item: item[0])[0] <= until:
            entry = min(scheduled, key=lambda item: item[0])
            scheduled.remove(entry)
            clock[0] = max(clock[0], entry[0])
            entry[1]()
        clock[0] = max(clock[0], until)

    def respawn(*agents):
        for agent in agents:
            old = coordinator.attempts.get(agent)
            assert old is None, f"{agent} is still parked"
            assert coordinator.register(attempt(agent, "sonnet")) is False

    return coordinator, clock, asked, run, respawn, attempt


def _relapse(coordinator, clock, run, respawn, agent="agent-0", after=30.0):
    """Resumed, then the limit again ``after`` seconds later. Returns whether
    the turn was parked for another recovery."""
    respawn(agent)
    run(max(clock[0], coordinator.last_resumed + after))
    return coordinator.request(agent, "trace", ("selector",))


def test_the_count_starts_again_after_ten_quiet_minutes():
    """Four relapses, then the pool works for ten minutes. The next limit is a
    new one: asked at once, counted from nothing."""
    coordinator, clock, asked, run, respawn, _attempt = _pool_of(["sonnet"], [])
    assert coordinator.request("agent-0", "trace", ("selector",))
    run(0)
    for expected in (1, 2, 3, 4):
        assert _relapse(coordinator, clock, run, respawn)
        assert coordinator.relapses == expected
        run(clock[0] + 480)
    assert len(asked) == 5

    respawn("agent-0")
    run(clock[0] + QUIET_SECONDS)
    assert coordinator.request("agent-0", "trace", ("selector",))
    assert coordinator.relapses == 0
    run(clock[0])
    assert asked[-1] == clock[0] and len(asked) == 6                 # asked at once

    # A second short of ten minutes would have been the fifth relapse: the end.
    assert _relapse(coordinator, clock, run, respawn, after=QUIET_SECONDS - 1)
    assert coordinator.relapses == 1


def test_just_short_of_ten_minutes_the_fifth_relapse_ends_the_turn():
    coordinator, clock, _asked, run, respawn, _attempt = _pool_of(["sonnet"], [])
    assert coordinator.request("agent-0", "trace", ("selector",))
    run(0)
    for _ in range(4):
        assert _relapse(coordinator, clock, run, respawn, after=QUIET_SECONDS - 1)
        run(clock[0] + 480)

    assert _relapse(coordinator, clock, run, respawn, after=QUIET_SECONDS - 1) is False
    assert coordinator.relapses == 5 and not coordinator.recovering


def test_a_turn_new_to_a_relapsing_pool_gets_its_recovery_and_ends_at_its_own_relapse():
    """The count is the pool's; the end is the relapsing turn's. A turn that
    was never resumed starts a recovery even after five rounds, eight minutes
    later, and ends when it relapses itself."""
    coordinator, clock, asked, run, respawn, attempt = _pool_of(["sonnet"], [])
    assert coordinator.request("agent-0", "trace", ("selector",))
    run(0)
    for _ in range(4):
        assert _relapse(coordinator, clock, run, respawn)
        run(clock[0] + 480)
    assert _relapse(coordinator, clock, run, respawn) is False       # agent-0 ends
    coordinator.discard("agent-0", "trace")
    assert len(asked) == 5

    coordinator.register(attempt("new", "sonnet"))
    failed = clock[0]
    assert coordinator.request("new", "trace", ("selector",))        # not ended: never resumed
    assert coordinator.relapses == 6
    run(failed + 479)
    assert len(asked) == 5
    run(failed + 480)
    assert asked[-1] == failed + 480 and len(asked) == 6
    assert _relapse(coordinator, clock, run, respawn, agent="new") is False


def test_a_plan_failure_is_not_a_relapse_of_the_pool():
    """It is about one turn's model. It neither counts nor slows the selector
    down for the turns that meet a real limit."""
    coordinator, clock, asked, run, respawn, attempt = _pool_of(["sonnet"], [])
    assert coordinator.request("agent-0", "trace", ("selector",))
    run(0)
    respawn("agent-0")
    coordinator.discard("agent-0", "trace")                          # it finished
    run(30)
    coordinator.register(attempt("planless", "gpt-6"))
    assert coordinator.request("planless", "trace", ("selector",), plan=True)
    assert coordinator.relapses == 0


# --- the reviewer's four mutants -------------------------------------------------------

def test_a_plan_failure_recovers_when_the_only_other_turns_are_parked(tmp_path, monkeypatch):
    """Rule 1 asks whether another turn of the pool is running. A turn that
    waits for an account is not: nothing would be switched away under it."""
    fleet = Fleet(tmp_path, monkeypatch, "claude", ["fable", "sonnet"], unserved=("fable",))
    start = fleet.clock[0]
    fleet.limit(0)
    fleet.run_until(start)
    assert fleet.waiting() == ["waiting", None]                      # fable waits, sonnet works
    fleet.sessions_started()
    fleet.no_account_offers()

    fleet.run_until(start + QUIET_SECONDS)
    fleet.spawns(1)[-1]["on_error"]("Your current plan does not include this model")
    fleet.run_until(start + QUIET_SECONDS)

    # A recovery, not the end of the turn: the selector found an account for both.
    assert fleet.selector_calls()[-1] == ["fable", "sonnet"] and len(fleet.selector_calls()) == 2
    assert fleet.kinds() == [AgentState.THINKING] * 2 and fleet.waiting() == [None, None]
    assert [len(fleet.spawns(index)) for index in range(2)] == [2, 3]


def test_a_plan_failure_recovers_when_the_only_other_turn_was_stopped():
    """A turn the user stopped stays registered until the next pass looks. It
    is not running either."""
    coordinator, _clock, asked, run, _respawn, attempt = _pool_of([], [])
    coordinator.register(attempt("stopped", "sonnet", owned=False))
    coordinator.register(attempt("planless", "gpt-6"))

    assert coordinator.request("planless", "trace", ("selector",), plan=True)
    run(0)
    assert len(asked) == 1


def test_a_plan_parked_turn_that_cannot_be_ended_stays_parked():
    """Work registered without a way to end it is not dropped after three
    answers: it waits like any other, to the cap."""
    coordinator, clock, asked, run, _respawn, attempt = _pool_of([], [False] * 40)
    item = attempt("planless", "gpt-6")
    coordinator.register(item)
    assert coordinator.request("planless", "trace", ("selector",), plan=True)

    run(5 * RECHECK_SECONDS)
    assert len(asked) == 6 and item.unserved_checks == 6
    assert coordinator.attempts["planless"] is item and coordinator.recovering
    assert item.resume.call_count == 0

    run(MAX_PARK_SECONDS)                                            # released at the cap
    assert item.resume.call_count == 1 and not coordinator.recovering


@pytest.mark.parametrize("backend", BACKENDS)
def test_an_account_found_at_the_cap_resumes_the_turn_to_work_not_to_fail(
        tmp_path, monkeypatch, backend):
    """The check that falls on the half hour finds an account. The turn is
    resumed like any other: it is not marked as released, so a limit it meets
    much later is recovered again."""
    fleet = Fleet(tmp_path, monkeypatch, backend, ["sonnet"], unserved=("sonnet",))
    start = fleet.clock[0]
    fleet.limit(0)
    fleet.run_until(start + MAX_PARK_SECONDS - 1)
    assert len(fleet.spawns(0)) == 1 and fleet.waiting() == ["waiting"]
    fleet.no_account_offers()
    fleet.run_until(start + MAX_PARK_SECONDS)

    assert fleet.asked_at[-1] == start + MAX_PARK_SECONDS
    assert len(fleet.spawns(0)) == 2 and fleet.recovery_messages(0) == 1
    assert fleet.coordinator.park_expired == set() and not fleet.coordinator.recovering
    fleet.sessions_started()

    fleet.run_until(start + MAX_PARK_SECONDS + 2 * QUIET_SECONDS)
    fleet.spawns(0)[-1]["on_error"](USAGE_LIMIT)
    fleet.run_until(fleet.clock[0])
    assert len(fleet.spawns(0)) == 3 and fleet.kinds() == [AgentState.THINKING]

