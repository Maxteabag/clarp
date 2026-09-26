"""TurnSlots and its OwnershipLock, without the dispatcher."""
import threading
from types import SimpleNamespace

import pytest

from lib import turn_slots
from lib.turn_slots import OwnershipLock, SlotsExhausted, TurnSlots


def test_deferred_work_runs_after_the_outermost_release():
    lock = OwnershipLock()
    seen = []
    with lock:
        with lock:
            lock.defer(lambda: seen.append(lock.held()))
        assert seen == []
        assert lock.held()
    assert seen == [False]
    lock.defer(lambda: seen.append("now"))
    assert seen == [False, "now"]


def test_deferred_work_runs_on_the_releasing_thread_only():
    lock = OwnershipLock()
    ran = []
    with lock:
        lock.defer(lambda: ran.append(threading.current_thread().name))
        other = threading.Thread(target=lambda: lock.defer(lambda: ran.append("other")),
                                 name="other")
        # The other thread does not hold the lock, so its work runs at once
        # once it can proceed; it never joins this thread's queue.
        other.start()
        other.join(timeout=5)
    assert ran == ["other", threading.current_thread().name]


def test_a_failing_deferred_call_does_not_skip_the_rest():
    lock = OwnershipLock()
    ran = []
    with pytest.raises(RuntimeError):
        with lock:
            lock.defer(lambda: (_ for _ in ()).throw(RuntimeError("boom")))
            lock.defer(lambda: ran.append("second"))
    assert ran == ["second"]
    assert not lock.held()


def test_slots_are_bounded():
    slots = TurnSlots()
    slots.MAX_QUEUE_PER_AGENT = 2
    slots.MAX_AGENTS = 1
    slots.claim("a", "t-a")
    with pytest.raises(SlotsExhausted):
        slots.claim("b", "t-b")
    slots.enqueue("a", SimpleNamespace(trace_id="q1"))
    slots.enqueue("a", SimpleNamespace(trace_id="q2"))
    with pytest.raises(SlotsExhausted):
        slots.enqueue("a", SimpleNamespace(trace_id="q3"))


def test_handover_release_and_stop_barrier():
    slots = TurnSlots()
    slots.claim("a", "t-1")
    assert slots.is_spawning("a") and slots.mark_spawned("a", "t-1")
    slots.enqueue("a", SimpleNamespace(trace_id="t-2"))
    assert slots.pop_next("a", expected="other") is None
    assert slots.pop_next("a", expected="t-1").trace_id == "t-2"
    assert not slots.release("a", "t-1") and slots.release("a", "t-2")
    slots.claim("a", "t-3")
    snapshot, dropped = slots.begin_stop("a")
    assert (snapshot.trace_id, dropped) == ("t-3", 0)
    assert slots.get("a") == turn_slots.STOPPING_SENTINEL
    assert slots.restore_stop("a", snapshot.as_dict())
    assert slots.get("a") == "t-3"


def test_live_work_folds_in_terminal_and_compaction(monkeypatch):
    slots = TurnSlots()
    monkeypatch.setattr(turn_slots, "_terminal_live", lambda agent_id: agent_id == "a")
    monkeypatch.setattr(turn_slots, "_compacting", lambda session: session == "s")
    work = slots.live_work("a", session="s")
    assert work.busy and work.terminal and work.compacting and not work.inflight
    assert not slots.live_work("b").busy
    slots.claim("b", "t")
    assert slots.live_work("b").active_trace == "t"


def test_reset_for_tests_empties_every_view():
    slots = TurnSlots()
    slots.claim("a", "t")
    slots.enqueue("a", SimpleNamespace(trace_id="q"))
    slots.reset_for_tests()
    assert slots.snapshot() == {"active": {}, "terminals": [], "spawning": [], "queued": {}}
