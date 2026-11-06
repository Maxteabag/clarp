"""Step0 additive contract, not a conversion of S02 or any S42 owner.

Authoring audit: legacy SQL tests already own transactions/CAS/Outbox. These
cases own NEW declaration and orchestration risks (missing/frozen vocabulary,
attempt confusion, falsey outcomes and diagnostic error precedence). The tiny
memory owner is a protocol witness with a real lock/guard/write boundary; its
fencing is NOT proof that a production owner has been converted correctly.
"""
from __future__ import annotations

from dataclasses import dataclass
from threading import Lock

import pytest

from lib import lifecycle as lk
from tests.lifecycle_cases import registered_cases


def policy(**updates):
    data = dict(family="work", owner="tests.memory_owner", states={"ready", "busy", "done"},
                events={"start", "finish", "renew"}, initial="ready", final={"done"},
                edges=(lk.Edge("start", frozenset({"ready"}), "busy"),
                       lk.Edge("finish", frozenset({"busy"}), "done"),
                       lk.Edge("renew", frozenset({"busy"}), "busy")),
                waits={"ready": lk.NamedWait(name="admit", exits=(lk.WaitExit("event", "start"),)),
                       "busy": lk.NamedWait(name="result", exits=(lk.WaitExit("event", "finish"),))},
                refusal_codes={"illegal", "generation", "token", "closed"})
    data.update(updates)
    return lk.Rules(**data)


def test_independent_policy_pairs_and_structural_registration():
    rules = policy()
    expected = {(state, event): None for state in (None, "ready", "busy", "done")
                for event in ("start", "finish", "renew")}
    expected.update({("ready", "start"): "busy", ("busy", "finish"): "done",
                     ("busy", "renew"): "busy"})
    for (state, event), target in registered_cases(rules, expected):
        if target is None:
            with pytest.raises(lk.IllegalTransition):
                rules.decide(state, event)
        else:
            assert rules.decide(state, event) == target
    with pytest.raises(AssertionError, match="missing"):
        registered_cases(rules, {key: value for key, value in expected.items() if key != ("ready", "start")})
    with pytest.raises(AssertionError, match="extra"):
        registered_cases(rules, expected | {("new", "start"): "busy"})
    with pytest.raises(lk.IllegalTransition):
        rules.decide("ready", "unknown")


@pytest.mark.parametrize("change", [
    {"states": {"ready", "done"}}, {"events": {"start", "finish"}},
    {"initial": "missing"}, {"final": {"missing"}}, {"final": set()},
    {"waits": {}}, {"waits": {"ready": "eventually"}},
    {"edges": (lk.Edge("start", frozenset({"ready"}), "busy"),
                lk.Edge("start", frozenset({"ready"}), "done"))},
    {"edges": (lk.Edge("finish", frozenset({"done"}), "ready"),)},
])
def test_invalid_new_declarations_refuse(change):
    with pytest.raises(lk.LifecycleError):
        policy(**change)


@pytest.mark.parametrize("exit", [lk for lk in ("unbounded", "timer")])
def test_unknown_wait_exit_kind_refuses(exit):
    with pytest.raises(lk.LifecycleError):
        policy(waits={"ready": lk.NamedWait(name="bad", exits=(lk.WaitExit(exit, "start"),))})


def test_wait_exit_requires_real_event_and_existing_deadline_source():
    for exit in (lk.WaitExit("event", "typo"), lk.WaitExit("deadline", "owner_timeout")):
        with pytest.raises(lk.LifecycleError):
            policy(waits={"ready": lk.NamedWait(name="admit", exits=(exit,)),
                          "busy": lk.NamedWait(name="result", exits=(lk.WaitExit("event", "finish"),))})


def test_new_declaration_copies_small_metadata_without_copying_payload():
    states = {"ready", "busy", "done"}
    waits = {"ready": lk.NamedWait(name="admit", exits=(lk.WaitExit("event", "start"),)),
             "busy": lk.NamedWait(name="result", exits=(lk.WaitExit("event", "finish"),))}
    rules = policy(states=states, waits=waits)
    states.clear(); waits.clear()
    assert rules.decide("ready", "start") == "busy"
    assert rules.states == frozenset({"ready", "busy", "done"})
    with pytest.raises(TypeError):
        rules.waits["ready"] = rules.waits["busy"]
    payload = {"borrowed": []}
    event = lk.OwnedEvent("start", "work1", (1, object()), payload)
    assert event.payload is payload


def sink_for(rules, events, *, report=None):
    return lk.RefusalSink(family=rules.family, events=rules.events, states=rules.states,
                          refusal_codes=rules.refusal_codes,
                          report=report or (lambda notice: events.append(("notice", notice.reason))),
                          recent_limit=2)


@dataclass(frozen=True)
class Fence:
    generation: int
    token: object


class MemoryOwner:
    """Minimal owning authority; mutation is performed inside its real lock."""
    def __init__(self):
        self.lock = Lock()
        self.state = "ready"
        self.generation = 1
        self.token = object()
        self.trace = []
        self.writes = 0

    def attempt(self, *, failure=None, completion="normal", nested=None):
        owner = self
        class Port(lk.OwnerWritePort):
            def write(self, rules, event, reject):
                with owner.lock:
                    owner.trace.append("enter")
                    try:
                        if nested:
                            nested()
                        if failure:
                            raise failure
                        if event.expected.generation != owner.generation:
                            return reject("generation", owner.state, lk.SafeIds(entity_id=event.entity_id))
                        if event.expected.token is not owner.token:
                            return reject("token", owner.state, lk.SafeIds(entity_id=event.entity_id))
                        try:
                            target = rules.decide(owner.state, event.name)
                        except lk.IllegalTransition as primary:
                            reject("illegal", owner.state, lk.SafeIds(), primary=primary)
                            raise
                        owner.state = target
                        owner.writes += 1
                        owner.trace.append("settled")
                        return lk.Applied(event.payload)
                    finally:
                        owner.trace.append("release")
            def after_boundary(self, report):
                assert not owner.lock.locked()
                if completion == "omit":
                    return
                report()
                if completion == "twice":
                    report()
        return Port()

    def event(self, name="start", payload=None):
        return lk.OwnedEvent(name, "work1", Fence(self.generation, self.token), payload)


@pytest.mark.parametrize("value", [None, False, 0])
def test_settle_then_act_preserves_falsey_applied_value(value):
    owner, rules = MemoryOwner(), policy()
    event = owner.event(payload=value)
    result = lk.apply_owned(rules, event, owner.attempt(), sink_for(rules, owner.trace))
    assert isinstance(result, lk.Applied) and result.value is value
    # External continuation consumes a settled result only after release.
    assert not owner.lock.locked()
    owner.trace.append(("effect", owner.state))
    assert owner.trace == ["enter", "settled", "release", ("effect", "busy")]
    assert owner.writes == 1


@pytest.mark.parametrize("replace", ["generation", "token", "aba"])
def test_at_write_owner_fence_refuses_without_success(replace):
    owner, rules = MemoryOwner(), policy()
    event = owner.event()
    if replace in {"generation", "aba"}:
        owner.generation += 1
    if replace == "token":
        owner.token = object()
    if replace == "aba":
        owner.state = "busy"
        owner.state = "ready"
    sink = sink_for(rules, owner.trace)
    result = lk.apply_owned(rules, event, owner.attempt(), sink)
    reason = "token" if replace == "token" else "generation"
    assert isinstance(result, lk.Refused) and result.code == reason
    assert owner.writes == 0 and owner.state == "ready"
    assert owner.trace == ["enter", "release", ("notice", reason)]
    assert sum(sink.counts.values()) == 1


def test_exceptional_illegal_pair_counts_and_emits_after_release():
    owner, rules = MemoryOwner(), policy()
    sink = sink_for(rules, owner.trace)
    with pytest.raises(lk.IllegalTransition):
        lk.apply_owned(rules, owner.event("finish"), owner.attempt(), sink)
    assert owner.writes == 0
    assert owner.trace == ["enter", "release", ("notice", "illegal")]
    assert sum(sink.counts.values()) == 1


def test_storage_failure_is_same_exception_without_invented_refusal():
    owner, rules = MemoryOwner(), policy()
    error = OSError("test storage failure")
    sink = sink_for(rules, owner.trace)
    with pytest.raises(OSError) as caught:
        lk.apply_owned(rules, owner.event(), owner.attempt(failure=error), sink)
    assert caught.value is error and owner.writes == 0
    assert owner.trace == ["enter", "release"] and not sink.counts


def test_reporter_failure_keeps_primary_and_explicit_secondary_cause():
    owner, rules = MemoryOwner(), policy()
    reporter_error = RuntimeError("diagnostic test failure")
    def report(notice):
        assert not owner.lock.locked()
        raise reporter_error
    with pytest.raises(lk.IllegalTransition) as caught:
        lk.apply_owned(rules, owner.event("finish"), owner.attempt(), sink_for(rules, [], report=report))
    assert caught.value.__cause__ is reporter_error
    assert owner.writes == 0 and owner.trace == ["enter", "release"]


@pytest.mark.parametrize("completion", ["omit", "twice"])
def test_bad_completion_never_succeeds_or_reemits(completion):
    owner, rules = MemoryOwner(), policy()
    owner.generation += 1
    stale = lk.OwnedEvent("start", "work1", Fence(1, owner.token), None)
    with pytest.raises(lk.LifecycleError):
        lk.apply_owned(rules, stale, owner.attempt(completion=completion), sink_for(rules, owner.trace))
    assert owner.writes == 0
    assert owner.trace.count(("notice", "generation")) <= 1


def test_reusing_same_facade_never_runs_a_second_write():
    owner, rules = MemoryOwner(), policy()
    port = owner.attempt()
    sink = sink_for(rules, [])
    lk.apply_owned(rules, owner.event(), port, sink)
    with pytest.raises(lk.LifecycleError):
        lk.apply_owned(rules, owner.event("finish"), port, sink)
    assert owner.writes == 1 and owner.state == "busy"


def test_diagnostics_inventory_is_immutable_bounded_and_has_no_caller_family():
    rules = policy()
    events, states, codes = set(rules.events), set(rules.states), set(rules.refusal_codes)
    sink = lk.RefusalSink(family="work", events=events, states=states, refusal_codes=codes,
                          report=lambda notice: None, recent_limit=2)
    events.clear(); states.clear(); codes.clear()
    for i in range(10):
        sink.capture(f"unknown{i}", f"unknown{i}", "illegal", lk.SafeIds(entity_id="x" * 200))
    assert sum(sink.counts.values()) == 10 and len(sink.counts) == 1
    assert len(sink.recent) == 2 and len(sink.recent[0].ids.entity_id) <= 128
    with pytest.raises(TypeError):
        sink.capture("start", "ready", "illegal", lk.SafeIds(), family="other")
    with pytest.raises(lk.LifecycleError):
        sink.capture("start", "ready", "unbounded-new-reason", lk.SafeIds())
    assert sum(sink.counts.values()) == 10
