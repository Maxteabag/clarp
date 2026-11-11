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

    def no_account_offers(self, *models):
        """What the selector finds from now on."""
        self.unserved_file.write_text("\n".join(models))

    def key(self, index):
        return (self.agents[index], f"trace-{index}")

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


def _into_the_same_park(fleet):
    """laptopstudio, 11 Nov 14:28 to 14:35: the account runs out, the selector
    picks one with a sliver left, the resumed turns meet the limit again 90
    seconds later, and now no account has usage."""
    start = fleet.clock[0]
    fleet.spawns(0)[-1]["on_error"](USAGE_LIMIT)
    fleet.run_scheduled()
    assert [len(fleet.spawns(index)) for index in range(3)] == [2, 2, 2]
    fleet.clock[0] += 90
    fleet.no_account_offers("fable", "sonnet")
    fleet.spawns(0)[-1]["on_error"](USAGE_LIMIT)
    fleet.run_scheduled()
    assert fleet.coordinator.recovering
    # Parked again, not ended: and it is the same park, begun at the start.
    assert [fleet.state(index)["detail"].get("account_recovery") for index in range(3)] == [
        "waiting"] * 3
    assert [fleet.coordinator.first_parked[fleet.key(index)] for index in range(3)] == [start] * 3
    return start


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("failure", [USAGE_LIMIT, "OAuth session expired; please run /login"],
                         ids=["usage-limit", "sign-in"])
def test_the_afternoon_park_resume_park_resume_ends_with_the_turns_working(
        tmp_path, monkeypatch, backend, failure):
    """A usage limit or a failed sign-in right after a resume is not the
    evening's loop: waiting does help. The turns go back into their park and
    work again when an account has usage."""
    fleet = Fleet(tmp_path, monkeypatch, backend, AFTERNOON, unserved=())
    start = fleet.clock[0]
    fleet.spawns(0)[-1]["on_error"](USAGE_LIMIT)
    fleet.run_scheduled()
    fleet.clock[0] += 90
    fleet.no_account_offers("fable", "sonnet")
    fleet.spawns(0)[-1]["on_error"](failure)            # within two minutes of its resume
    fleet.run_scheduled()
    assert fleet.coordinator.first_parked[fleet.key(0)] == start

    for _ in range(20):                                 # twenty minutes without an account
        fleet.run_scheduled(advance=RECHECK_SECONDS + 1)
        assert [fleet.state(index)["kind"] for index in range(3)] == [AgentState.THINKING] * 3
    assert [len(fleet.spawns(index)) for index in range(3)] == [2, 2, 2]

    fleet.no_account_offers()                           # the windows reset
    fleet.run_scheduled(advance=RECHECK_SECONDS + 1)

    assert not fleet.coordinator.recovering
    assert [len(fleet.spawns(index)) for index in range(3)] == [3, 3, 3]
    assert [fleet.state(index)["kind"] for index in range(3)] == [AgentState.THINKING] * 3
    assert [fleet.state(index)["detail"].get("account_recovery") for index in range(3)] == [
        None] * 3
    for index in range(3):
        fleet.spawns(index)[-1]["on_result"]({"duration_ms": 5})
    assert [fleet.state(index)["kind"] for index in range(3)] == [AgentState.DONE] * 3


@pytest.mark.parametrize("backend", BACKENDS)
def test_the_recovery_message_is_the_same_one_each_time_never_one_inside_another(
        tmp_path, monkeypatch, backend):
    fleet = Fleet(tmp_path, monkeypatch, backend, AFTERNOON, unserved=())
    _into_the_same_park(fleet)
    fleet.no_account_offers()
    fleet.run_scheduled(advance=RECHECK_SECONDS + 1)

    for index in range(3):
        first, second, third = [call["text"] for call in fleet.spawns(index)]
        assert "Clarp recovered from" not in first
        assert second.count("Clarp recovered from") == 1
        assert second.count("<clarp-account-recovery>") == 1
        assert third == second, "the message grew or was wrapped again"
        assert second.count(f"work of agent-{index}") == 1


@pytest.mark.parametrize("backend", BACKENDS)
def test_the_park_cap_counts_from_the_turns_first_park_not_from_the_latest(
        tmp_path, monkeypatch, backend):
    """Half an hour without an account is where a usage wait gives up. A turn
    that was resumed for 90 seconds in between does not get another half
    hour: its park began at the first limit."""
    fleet = Fleet(tmp_path, monkeypatch, backend, AFTERNOON, unserved=())
    start = _into_the_same_park(fleet)

    while fleet.clock[0] + RECHECK_SECONDS + 1 < start + MAX_PARK_SECONDS:
        fleet.run_scheduled(advance=RECHECK_SECONDS + 1)
    # Just short of thirty minutes from the first park: still waiting.
    assert [len(fleet.spawns(index)) for index in range(3)] == [2, 2, 2]
    assert fleet.coordinator.recovering

    fleet.run_scheduled(advance=RECHECK_SECONDS + 1)
    # Thirty minutes from the FIRST park, and a minute and a half before
    # thirty from the second: released to run and fail visibly.
    assert start + MAX_PARK_SECONDS <= fleet.clock[0] < start + 90 + MAX_PARK_SECONDS
    assert [len(fleet.spawns(index)) for index in range(3)] == [3, 3, 3]
    assert not fleet.coordinator.recovering
    for index in range(3):
        fleet.spawns(index)[-1]["on_error"](USAGE_LIMIT)
    assert [fleet.state(index)["kind"] for index in range(3)] == [AgentState.INTERRUPTED] * 3
    assert not fleet.coordinator.recovering and fleet.run_scheduled(advance=90) == 0


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
def test_an_available_answer_that_keeps_being_wrong_gives_up_at_the_same_cap(
        tmp_path, monkeypatch, backend):
    """The evening's shape with a usage limit instead of a plan: the selector
    keeps saying an account has usage and the turn keeps meeting the limit
    seconds after each resume. Nothing ends early, and it does not go on for
    ever: thirty minutes after the turn's first park it fails visibly."""
    fleet = Fleet(tmp_path, monkeypatch, backend, ["sonnet"], unserved=())
    start = fleet.clock[0]

    rounds = 0
    while fleet.state(0)["kind"] != AgentState.INTERRUPTED:
        fleet.spawns(0)[-1]["on_error"](USAGE_LIMIT)
        fleet.run_scheduled()
        fleet.clock[0] += 90
        rounds += 1
        assert rounds < 40, "it never gives up"

    assert fleet.coordinator.first_parked == {} and not fleet.coordinator.recovering
    assert start + MAX_PARK_SECONDS <= fleet.clock[0] <= start + MAX_PARK_SECONDS + 180
    _visible_failure(fleet.state(0), category="usage_limit", says="usage limit")
    assert fleet.run_scheduled(advance=90) == 0


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
    # It had left its park: the second limit began a new one, with its own cap.
    assert fleet.coordinator.first_parked[fleet.key(0)] == fleet.clock[0]
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


# --- a model id the backend's catalogue does not know ----------------------------
#
# The helpers were started with ``gpt-6``. Codex's own list had gpt-6.1-sol,
# gpt-6-sol and others, and the Host had read that list; nothing compared.


def _observed_codex_models(monkeypatch, ids, *, kind="cli_probe"):
    """The Host's cached model catalogue, as its last look at Codex left it."""
    import time
    from lib import provider_capabilities
    source = {"kind": kind, "detail": "codex debug models", "freshness": "fresh"}
    catalogue = {"providers": {"codex": {
        "id": "codex", "source": source,
        "models": [{"id": model, "label": model} for model in ids]}}}
    monkeypatch.setattr(provider_capabilities, "_cache",
                        (time.monotonic(), catalogue, ()))


def test_codex_refuses_a_model_id_its_catalogue_does_not_list(monkeypatch):
    from lib import backends
    _observed_codex_models(monkeypatch, ["gpt-6.1-sol", "gpt-6-sol", "gpt-6-astra"])

    assert backends.is_valid_model("codex", "gpt-6") is False
    assert backends.is_valid_model("codex", "gpt-6-sol") is True
    assert backends.is_valid_model("codex", " gpt-6.1-sol ") is True
    assert backends.is_valid_model("codex", "") is True          # the CLI's default
    assert backends.is_valid_model("codex", None) is True
    assert backends.known_models("codex") == ["gpt-6-astra", "gpt-6-sol", "gpt-6.1-sol"]
    # Another backend is not judged by Codex's list.
    assert backends.is_valid_model("claude", "gpt-6") is True


@pytest.mark.parametrize("case", ["never-observed", "probe-failed", "empty-list"])
def test_without_an_observed_catalogue_codex_accepts_the_id_as_before(monkeypatch, case):
    """The check never guesses and never probes: with no list read from the
    CLI itself there is nothing to compare against, and the id is accepted."""
    from lib import backends, provider_capabilities
    if case == "never-observed":
        monkeypatch.setattr(provider_capabilities, "_cache", None)
    elif case == "probe-failed":      # the bundled fallback list is not the CLI's word
        _observed_codex_models(monkeypatch, ["gpt-6-sol"], kind="static_fallback")
    else:
        _observed_codex_models(monkeypatch, [])

    assert backends.is_valid_model("codex", "gpt-6") is True
    assert backends.known_models("codex") == []


def test_starting_an_agent_with_an_unknown_model_is_refused_and_says_what_exists(monkeypatch):
    """The launch request is where a helper gets its model."""
    from lib import backends
    from lib.policies import agent_spec
    from tests.unit.test_policies_agent_spec import PERSONAS, VIEW, FakeBackends

    class RealModels(FakeBackends):
        is_valid_model = staticmethod(backends.is_valid_model)
        known_models = staticmethod(backends.known_models)

    _observed_codex_models(monkeypatch, ["gpt-6.1-sol", "gpt-6-sol"])

    refused = agent_spec.AgentSpec.parse(
        {"name": "Helper", "backend": "codex", "model": "gpt-6"},
        backends=RealModels(), roster=VIEW, personas=PERSONAS)
    accepted = agent_spec.AgentSpec.parse(
        {"name": "Helper", "backend": "codex", "model": "gpt-6-sol"},
        backends=RealModels(), roster=VIEW, personas=PERSONAS)

    assert isinstance(refused, agent_spec.SpecError)
    assert (refused.status, refused.message) == (400, "invalid model for backend")
    assert refused.detail == "codex has no model 'gpt-6'; it offers gpt-6-sol, gpt-6.1-sol"
    assert isinstance(accepted, agent_spec.AgentSpec) and accepted.model == "gpt-6-sol"


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
