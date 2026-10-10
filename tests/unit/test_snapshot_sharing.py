"""Concurrent /agents/snapshot requests share one build without going stale."""
import threading
import time
import types

from lib import snapshot


def test_requests_waiting_on_a_build_share_the_next_one(monkeypatch):
    first_started = threading.Event()
    release_first = threading.Event()
    builds = []

    def build(ctx):
        builds.append(ctx)
        if len(builds) == 1:
            first_started.set()
            release_first.wait(5)
        return {"build": len(builds)}

    monkeypatch.setattr(snapshot, "build_agent_snapshot", build)
    clock_reads = threading.Semaphore(0)

    def monotonic():
        clock_reads.release()
        return time.monotonic()

    monkeypatch.setattr(snapshot, "time", types.SimpleNamespace(monotonic=monotonic))
    shared = snapshot.SharedSnapshot()
    ctx = object()
    bodies = []
    first = threading.Thread(target=lambda: bodies.append(shared.body(ctx)))
    first.start()
    assert first_started.wait(5)
    # Ten clients arrive while the first build runs. The running build may
    # predate their writes, so they share exactly one build that starts later.
    waiters = [threading.Thread(target=lambda: bodies.append(shared.body(ctx))) for _ in range(10)]
    for waiter in waiters:
        waiter.start()
    # Every request reads the clock on arrival; the first also when it starts
    # building. Release only once all ten have arrived.
    for _ in range(2 + len(waiters)):
        assert clock_reads.acquire(timeout=5)
    release_first.set()
    for thread in [first, *waiters]:
        thread.join(5)
    assert len(builds) == 2
    assert sorted(bodies) == [b'{"build": 1}'] + [b'{"build": 2}'] * 10


def test_a_request_after_a_finished_build_gets_a_fresh_one(monkeypatch):
    builds = []
    monkeypatch.setattr(snapshot, "build_agent_snapshot",
                        lambda ctx: builds.append(ctx) or {"build": len(builds)})
    shared = snapshot.SharedSnapshot()
    ctx = object()
    assert shared.body(ctx) == b'{"build": 1}'
    assert shared.body(ctx) == b'{"build": 2}'
