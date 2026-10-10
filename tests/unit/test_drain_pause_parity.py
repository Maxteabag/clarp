"""The release drain persists pause rights; it never grants new ones.

Each scenario runs the same message flow two ways. ``baseline`` is the code
before the drain (no fence), observed in process and after a crash. The drain
modes hold the same arrivals behind the fence and then either lift it
(``revert``) or hand over (``handoff``: memory gone, the next runtime recovers
the durable queue; a crash mid-drain recovers the same way). The asserted
tables were measured on origin/main before b540547a with this file's
``baseline`` mode (see ``test_baseline_table``).
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from lib import agents as agents_db
from lib import turn_queue
from lib import turn_dispatch as td
from lib.turn_dispatch import TurnDispatchService

HAS_FENCE = hasattr(TurnDispatchService, "begin_admission_fence")


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setattr(td, "_RUNTIME_CLIENT", None)
    td.reset_for_tests()
    yield
    td.reset_for_tests()


class Provider:
    CLAUDE = "claude"

    def __init__(self):
        self.spawned: list[dict] = []
        self.live: set[str] = set()

    def normalize(self, backend):
        return backend or self.CLAUDE

    def interrupt(self, backend, agent_id):
        self.live.discard(agent_id)
        return 1

    def interrupt_any(self, agent_id):
        return self.interrupt("", agent_id)

    def active_handles(self, backend, agent_id):
        return ["handle"] if agent_id in self.live else []

    def spawn_turn(self, backend, **kwargs):
        self.spawned.append(kwargs)
        self.live.add(kwargs["agent_id"])


_SESSIONS = iter(range(10**6))


class World:
    def __init__(self, tmp_path):
        self.tmp_path = tmp_path
        self.agent = agents_db.create_agent(
            persona="Mike", voice_id="V", cwd=str(tmp_path), session="mike",
            backend="claude")
        agents_db.start_runtime(self.agent, "mike")
        self.started: list[str] = []
        self._new_process()

    def _new_process(self):
        self.provider = Provider()
        ctx = SimpleNamespace(default_session="mike",
                              agents_path=self.tmp_path / "unused.json",
                              stream=SimpleNamespace(broadcast=lambda _e: None))
        self.service = TurnDispatchService(
            ctx, backend_registry=self.provider, home=self.tmp_path,
            uuid_factory=lambda: f"bs-{next(_SESSIONS)}",
            retry_scheduler=lambda _delay, _fn: None)
        self._seen = 0
        # Boot: the runtime's first recovery rehydrates (parked rows become
        # queued) before any new work arrives, as recover_runtime does.
        self.service.recover_queued()

    def send(self, cid, *, origin="user", queue=False):
        self.service.dispatch(
            text=cid, requested_session="mike", trace_id=f"t-{cid}",
            client_msg_id=cid, synthesize_audio=False, queue_if_busy=queue,
            origin=origin,
            sender_agent_id="other-agent" if origin == "agent" else "")

    def stop(self):
        """The runtime's Stop (RPC begin_stop / finish_stop, nothing cancelled)."""
        snapshot, _dropped, _was_paused = td.begin_stop(self.agent)
        self.provider.interrupt("claude", self.agent)
        td.complete_stop(self.service.ctx, self.agent, snapshot, set(),
                         backend_registry=self.provider)

    def _collect(self):
        for call in self.provider.spawned[self._seen:]:
            self.started.append(call["text"])
        self._seen = len(self.provider.spawned)

    def settle(self, *, recover=True):
        """Finish every turn that starts, as a quiet fleet would, running the
        recovery pass the scheduler would run, until nothing new starts."""
        for _ in range(50):
            if recover:
                self.service.recover_queued()
            self._collect()
            running = [c for c in self.provider.spawned
                       if c["agent_id"] in self.provider.live]
            if not running:
                return
            call = running[-1]
            self.provider.live.discard(call["agent_id"])
            call["on_result"]({"result": "done"})
        raise AssertionError("did not settle")

    def restart(self):
        """Handoff or crash: memory is gone, the database is not."""
        self._collect()
        td.reset_for_tests()
        self._new_process()
        self.settle()

    def outcome(self):
        rows = {row["queue_id"]: row["status"] for row in agents_db.conn().execute(
            "SELECT queue_id, status FROM queued_turns")}
        return {"started": list(self.started), "paused": turn_queue.is_paused(self.agent),
                "waiting": sorted(q for q, s in rows.items() if s in ("queued", "parked"))}


def _fence(world):
    world.service.begin_admission_fence()


def _lift(world):
    world.service.lower_admission_fence()
    world.service.start_held_work()


# --- scenarios -------------------------------------------------------------------
#
# A: a Stop pause already exists when work arrives (post-Stop arrivals), with
#    pre-Stop work queued behind the stopped turn (grandfathered).
# C: work is queued (held by the drain, or queued behind a turn), then the user
#    Stops: later Stop must fence exactly as today.

PRE = [("a_pre", "automation", True), ("p_pre", "agent", False),
       ("uq_pre", "user", True)]
POST = [("u_post", "user", False), ("a_post", "automation", True),
        ("p_post", "agent", False), ("uq_post", "user", True),
        ("h_post", "automation", False)]


def scenario_a(world, mode):
    world.send("t0")
    if mode not in ("baseline", "baseline-crash"):
        _fence(world)
    for cid, origin, queue in PRE:
        world.send(cid, origin=origin, queue=queue)
    world.stop()
    for cid, origin, queue in POST:
        world.send(cid, origin=origin, queue=queue)
    if mode == "baseline":
        world.settle()
    elif mode == "baseline-crash" or mode == "handoff":
        world.restart()
    elif mode == "revert":
        _lift(world)
        world.settle()
    return world.outcome()


HELD = [("a1", "automation", True), ("p1", "agent", False),
        ("u1q", "user", True), ("h1", "automation", False)]


def scenario_c(world, mode):
    """Work queued behind a running turn (held by the drain in drain modes),
    then a NEW user Stop."""
    world.send("t0")
    if mode not in ("baseline", "baseline-crash"):
        _fence(world)
    for cid, origin, queue in HELD:
        world.send(cid, origin=origin, queue=queue)
    world.stop()
    if mode == "baseline":
        world.settle()
    elif mode in ("baseline-crash", "handoff"):
        world.restart()
    elif mode == "revert":
        _lift(world)
        world.settle()
    return world.outcome()


def scenario_c2(world, mode):
    """A Stop pause exists; the user carries on (a fresh send runs past it);
    work arrives behind that turn (held by the drain in drain modes), then the
    user Stops AGAIN."""
    world.send("t0")
    world.stop()
    world.settle(recover=False)
    world.send("u0")
    if mode not in ("baseline", "baseline-crash"):
        _fence(world)
    for cid, origin, queue in HELD + [("u1", "user", False)]:
        world.send(cid, origin=origin, queue=queue)
    world.stop()
    if mode == "baseline":
        world.settle()
    elif mode in ("baseline-crash", "handoff"):
        world.restart()
    elif mode == "revert":
        _lift(world)
        world.settle()
    return world.outcome()


SCENARIOS = {"a": scenario_a, "c": scenario_c, "c2": scenario_c2}


@pytest.mark.parametrize("name", sorted(SCENARIOS))
@pytest.mark.parametrize("mode", ["baseline", "baseline-crash"])
def test_baseline_table(tmp_path, name, mode, capsys):
    result = SCENARIOS[name](World(tmp_path), mode)
    with capsys.disabled():
        print(f"\nTABLE {name} {mode}: {result}")


@pytest.mark.skipif(not HAS_FENCE, reason="no drain before b540547a")
@pytest.mark.parametrize("name", sorted(SCENARIOS))
@pytest.mark.parametrize("mode", ["revert", "handoff"])
def test_drain_table(tmp_path, name, mode, capsys):
    result = SCENARIOS[name](World(tmp_path), mode)
    with capsys.disabled():
        print(f"\nTABLE {name} {mode}: {result}")


# --- measured baseline (origin/main before b540547a, `test_baseline_table`) --------
#
# In process, the runtime's Stop restores the work queued behind the stopped
# turn and starts its head (finish_stop): grandfathered work runs. After a
# crash, recovery leaves every row of a paused agent waiting.
BASELINE = {
    "a": {"started": {"t0", "a_pre", "u_post", "h_post", "p_pre", "uq_pre",
                      "p_post", "uq_post"}, "waiting": {"a_post"}},
    "c": {"started": {"t0", "h1", "a1", "p1", "u1q"}, "waiting": set()},
    "c2": {"started": {"t0", "u0", "h1", "u1", "p1", "u1q"}, "waiting": {"a1"}},
}
BASELINE_CRASH = {
    "a": {"started": {"t0", "a_pre", "u_post", "h_post"},
          "waiting": {"a_post", "p_post", "p_pre", "uq_post", "uq_pre"}},
    "c": {"started": {"t0", "h1", "a1"}, "waiting": {"p1", "u1q"}},
    "c2": {"started": {"t0", "u0", "h1", "u1", "p1"}, "waiting": {"a1", "u1q"}},
}
# Work that arrived before the scenario's last Stop: a Stop fences it.
BEFORE_LAST_STOP = {
    "a": {"t0", "a_pre", "p_pre", "uq_pre"},
    "c": {"t0", "a1", "p1", "u1q", "h1"},
    "c2": {"t0", "u0", "a1", "p1", "u1q", "h1", "u1"},
}
# The branch, through the drain's handoff (= a crash mid-drain): post-Stop
# arrivals keep the right they had on arrival; nothing older runs past a Stop.
HANDOFF = {
    "a": {"t0", "u_post", "p_post", "uq_post", "h_post"},
    "c": {"t0"},
    "c2": {"t0", "u0"},
}


def _ids(outcome):
    return {q.removeprefix("drain-park-t-").removeprefix("stop-park-t-")
            for q in outcome["waiting"]}


@pytest.mark.skipif(not HAS_FENCE, reason="no drain before b540547a")
@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_lifting_the_fence_matches_the_baseline_exactly(tmp_path, name):
    outcome = SCENARIOS[name](World(tmp_path), "revert")
    assert set(outcome["started"]) == BASELINE[name]["started"]
    assert len(outcome["started"]) == len(set(outcome["started"]))  # each once
    assert _ids(outcome) == BASELINE[name]["waiting"]


@pytest.mark.skipif(not HAS_FENCE, reason="no drain before b540547a")
@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_the_handoff_never_grants_more_than_the_old_code(tmp_path, name):
    outcome = SCENARIOS[name](World(tmp_path), "handoff")
    started = set(outcome["started"])
    assert len(outcome["started"]) == len(started)
    # Never more than the old code ran in process (the in-memory right) ...
    assert started <= BASELINE[name]["started"]
    # ... and nothing from before a Stop runs past it that a restart would not
    # have run: a later Stop fences held peer and automation work as today.
    assert started & BEFORE_LAST_STOP[name] <= BASELINE_CRASH[name]["started"]
    assert started == HANDOFF[name]
    assert outcome["paused"] is True
    assert _ids(outcome) | started == BASELINE[name]["started"] | BASELINE[name]["waiting"]



@pytest.mark.skipif(not HAS_FENCE, reason="no drain before b540547a")
@pytest.mark.parametrize("origin,queue,paused,send_now,cid,exempt", [
    ("user", False, True, False, "u", True),         # normal sends are never paused
    ("automation", False, True, False, "h", True),
    ("agent", False, True, False, "p", True),        # peer message waits, unpaused
    ("user", True, True, False, "uq", True),         # fresh user intent bypasses
    ("user", True, False, False, "uq", False),       # queue send, no pause on arrival
    ("automation", True, True, False, "a", False),   # automation queue: paused
    ("automation", True, False, False, "a", False),  # ... or simply queued
    ("agent", True, True, False, "pq", False),       # explicit peer queue: paused
    ("user", True, True, True, "q", True),           # explicit send-now
    ("automation", True, True, True, "task-goal-x", False),  # goal wake: re-checked
])
def test_only_rights_the_admission_already_granted_are_persisted(
        origin, queue, paused, send_now, cid, exempt):
    from lib.policies import admission
    from lib.turn_dispatch import DispatchCommand, _pause_exempt_on_arrival
    command = DispatchCommand(
        text="x", requested_session="mike", trace_id="t", client_msg_id=cid,
        origin=origin, sender_agent_id="other" if origin == "agent" else "",
        queue_if_busy=queue, allow_paused_queue=send_now)
    decision = admission.admission(
        origin, {"agent_id": "a", "can_chat": True}, [], admission.HostSettings(),
        admission.LiveWork(client_msg_id=cid, queue_if_busy=queue,
                           sender_agent_id=command.sender_agent_id,
                           allow_paused_queue=send_now),
        admission.QueueState(paused=paused))
    # Exempt exactly when the old admission let it past an existing pause.
    if paused and exempt:
        assert not isinstance(decision, admission.Queue)
    if paused and queue and not exempt and not cid.startswith("task-goal-"):
        assert isinstance(decision, admission.Queue)
    effective = decision.effective
    assert _pause_exempt_on_arrival(command, effective) is exempt


@pytest.mark.skipif(not HAS_FENCE, reason="no drain before b540547a")
def test_every_stop_takes_back_persisted_rights_even_when_already_paused(tmp_path):
    world = World(tmp_path)
    turn_queue.enqueue(queue_id="p", agent_id=world.agent, session="mike", text="p",
                       trace_id="t-p", client_msg_id="p", synthesize_audio=False,
                       origin="agent", sender_agent_id="other")
    turn_queue.set_paused(world.agent, True)       # an earlier Stop
    turn_queue.allow_paused("p")                   # held by a drain after it
    assert agents_db.conn().execute(
        "SELECT allow_paused FROM queued_turns WHERE queue_id='p'").fetchone()[0] == 1
    turn_queue.set_paused(world.agent, True)       # a later Stop, queue still paused
    assert agents_db.conn().execute(
        "SELECT allow_paused FROM queued_turns WHERE queue_id='p'").fetchone()[0] == 0
