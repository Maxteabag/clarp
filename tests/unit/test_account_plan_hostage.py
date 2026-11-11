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

import json
import sys
import uuid
from dataclasses import replace
from types import SimpleNamespace

import pytest

from lib import account_failover
from lib import agents as agents_db
from lib.account_failover import MAX_PARK_SECONDS, RECHECK_SECONDS, AccountFailover
from lib.protocol import AgentState
import lib.turn_dispatch as _td
from lib.turn_dispatch import TurnDispatchService
from tests.unit.test_turn_dispatch import _Backends, _Stream

NOT_OFFERED = ("The 'gpt-6' model is not supported when using Codex with a ChatGPT account.")
USAGE_LIMIT = "You've hit your usage limit. Upgrade to Pro or try again at 5:08 PM."
GOOD = ["gpt-6.1-sol", "gpt-6-sol", "gpt-6.1-sol", "gpt-6-astra"]
BAD = "gpt-6"

# The selector's protocol: models on stdin, one JSON object on stdout. This one
# serves every model but the ones named on its command line, and logs each call.
SELECTOR = """
import json, sys
log, unserved = sys.argv[1], set(sys.argv[2:])
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
        command = (sys.executable, str(script), str(self.log), *unserved)
        field = f"{backend}_account_switch_command"
        cfg = replace(_td.config.load(), **{field: command})
        monkeypatch.setattr(_td.config, "load", lambda *args, **kwargs: cfg)
        self.clock = [1000.0]
        self.scheduled = []
        self.coordinator = AccountFailover(
            _td.OwnershipLock(), schedule=lambda delay, cb: self.scheduled.append((delay, cb)),
            now=lambda: self.clock[0])
        monkeypatch.setitem(_td._FAILOVERS, backend, self.coordinator)
        # Every turn has a conversation to resume, as on a live Host.
        monkeypatch.setattr(_td.backends.by_id(backend), "resume_target",
                            lambda *args, **kwargs: tmp_path / "native.jsonl")
        self.backends = _Backends()
        self.backends.live = False
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

    def spawns(self, index):
        return [call for _, call in self.backends.spawned if call["trace_id"] == f"trace-{index}"]

    def selector_calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] \
            if self.log.exists() else []

    def run_scheduled(self, *, advance=0.0):
        """Fire every timer the coordinator armed, as its thread would."""
        self.clock[0] += advance
        pending, self.scheduled[:] = list(self.scheduled), []
        for _delay, callback in pending:
            callback()
        return len(pending)

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


@pytest.mark.parametrize("backend", BACKENDS)
def test_five_parked_turns_and_a_model_no_account_serves_do_not_loop(
        tmp_path, monkeypatch, backend):
    """The real loop. Five turns are parked by a genuine usage limit; one of
    them wants a model no saved account offers; the selector answers available
    with a models list that lacks it."""
    fleet = Fleet(tmp_path, monkeypatch, backend, [*GOOD, BAD])
    fleet.spawns(0)[-1]["on_error"](USAGE_LIMIT)      # the account ran out
    assert fleet.coordinator.recovering
    fleet.run_scheduled()                             # stop the others, ask the selector

    # The four the verdict covers are resumed; the fifth is not launched again
    # only to fail: it ends, and says why.
    assert fleet.selector_calls() == [sorted({*GOOD, BAD})]
    assert [len(fleet.spawns(index)) for index in range(5)] == [2, 2, 2, 2, 1]
    _visible_failure(fleet.state(4), category="account_plan",
                     says=f"No saved {backend.capitalize()} account offers model {BAD}")
    assert not fleet.coordinator.recovering

    # Nothing is left that could start the next round: no timer, no parked
    # work, and the selector is not asked again.
    for _ in range(10):
        fleet.run_scheduled(advance=90)
    assert len(fleet.selector_calls()) == 1
    assert [len(fleet.spawns(index)) for index in range(5)] == [2, 2, 2, 2, 1]
    assert [fleet.recovery_messages(index) for index in range(4)] == [1, 1, 1, 1]
    assert [fleet.state(index)["kind"] for index in range(4)] == [AgentState.THINKING] * 4


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
    _visible_failure(fleet.state(0), category="account_plan",
                     says=f"No saved {backend.capitalize()} account offers model {BAD}")
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
@pytest.mark.parametrize("failure,category", [
    (NOT_OFFERED, "account_plan"), (USAGE_LIMIT, "usage_limit")])
def test_a_turn_that_fails_again_right_after_its_recovery_ends_instead_of_restarting_it(
        tmp_path, monkeypatch, backend, failure, category):
    """The selector said an account serves the turn, the turn was resumed, and
    it failed the same way within seconds. Asking again is the loop: the turn
    ends with the provider's sentence, and the other turns are left alone."""
    fleet = Fleet(tmp_path, monkeypatch, backend, [*GOOD, "gpt-6-astra"], unserved=())
    fleet.spawns(0)[-1]["on_error"](USAGE_LIMIT)
    fleet.run_scheduled()
    assert [len(fleet.spawns(index)) for index in range(5)] == [2, 2, 2, 2, 2]

    fleet.clock[0] += 3
    fleet.spawns(4)[-1]["on_error"](failure)
    for _ in range(5):
        fleet.run_scheduled(advance=90)

    assert len(fleet.selector_calls()) == 1
    assert [len(fleet.spawns(index)) for index in range(5)] == [2, 2, 2, 2, 2]
    assert [fleet.recovery_messages(index) for index in range(5)] == [1, 1, 1, 1, 1]
    assert [fleet.state(index)["kind"] for index in range(4)] == [AgentState.THINKING] * 4
    _visible_failure(fleet.state(4), category=category, says=failure[:40])


@pytest.mark.parametrize("backend", BACKENDS)
def test_a_turn_that_worked_after_its_recovery_may_be_recovered_again(
        tmp_path, monkeypatch, backend):
    """The bound above is for a failure right after the resume. A turn that
    ran for a while on the new account and then met its limit is a new
    recovery, with its one recovery message."""
    fleet = Fleet(tmp_path, monkeypatch, backend, ["gpt-6.1-sol"], unserved=())
    fleet.spawns(0)[-1]["on_error"](USAGE_LIMIT)
    fleet.run_scheduled()
    assert len(fleet.spawns(0)) == 2

    fleet.clock[0] += 20 * 60                         # twenty minutes of work
    fleet.spawns(0)[-1]["on_error"](USAGE_LIMIT)
    fleet.run_scheduled()

    assert len(fleet.selector_calls()) == 2
    assert len(fleet.spawns(0)) == 3
    assert fleet.state(0)["kind"] == AgentState.THINKING
    # One message per recovery, never one wrapped inside the other.
    assert [call["text"].count("Clarp recovered from") for call in fleet.spawns(0)] == [0, 1, 1]


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
    ('{"available": true, "models": []}', ["a", "b"], None),         # available for nothing
    ('{"available": true, "models": ["z"]}', ["a", "b"], None),
    ('{"available": true, "models": "a"}', ["a", "b"], None),        # not a list: no verdict
    ('{"available": true, "models": ["a", 7]}', ["a", "b"], None),
    ('{"available": false, "models": ["a"]}', ["a", "b"], False),
])
def test_the_selectors_models_answer_is_read(payload, requested, expected):
    code = "import sys; print(sys.argv[1])"
    verdict = account_failover.switch_account((sys.executable, "-c", code, payload), requested)
    assert verdict == expected and type(verdict) is type(expected)


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
    assert coordinator.resumed_at == {}
    assert coordinator.request("agent", "second", ("selector",))
