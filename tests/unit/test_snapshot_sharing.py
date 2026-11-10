"""Concurrent snapshots share a running build; completed requests stay fresh."""
import threading
import time
import types
from concurrent.futures import Future

import pytest

from lib import snapshot


def _selected_waiters(monkeypatch, shared, names):
    """Observe admission, without supplying a result or changing a lock.

    The baseline blocks immediately after its arrival clock; the new flight
    blocks in Future.result after selecting its generation. These are only
    scheduling probes. The oracles below assert returned bytes and work.
    """
    selected = threading.Semaphore(0)
    seen = set()
    guard = threading.Lock()
    real_clock, real_result, real_wait = time.monotonic, Future.result, threading.Condition.wait

    def observe():
        name = threading.current_thread().name
        with guard:
            if name in names and name not in seen:
                seen.add(name)
                selected.release()

    def monotonic():
        now = real_clock()
        if hasattr(shared, "_last"):
            observe()
        return now

    def result(future, *args, **kwargs):
        observe()
        return real_result(future, *args, **kwargs)

    def wait(condition, *args, **kwargs):
        observe()
        return real_wait(condition, *args, **kwargs)

    monkeypatch.setattr(snapshot, "time", types.SimpleNamespace(monotonic=monotonic))
    monkeypatch.setattr(Future, "result", result)
    monkeypatch.setattr(threading.Condition, "wait", wait)
    return selected


def test_requests_waiting_on_a_build_share_the_running_one(monkeypatch):
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
    shared = snapshot.SharedSnapshot()
    names = {f"snapshot-waiter-{i}" for i in range(10)}
    selected = _selected_waiters(monkeypatch, shared, names)
    ctx = object()
    bodies = []
    first = threading.Thread(target=lambda: bodies.append(shared.body(ctx)), daemon=True)
    first.start()
    waiters = []
    try:
        assert first_started.wait(5)
        # Lead66(a): arrivals during this build join it, even though it began
        # before their arrival. No completed-body TTL is introduced.
        waiters = [threading.Thread(name=name, daemon=True,
                    target=lambda: bodies.append(shared.body(ctx))) for name in names]
        for waiter in waiters:
            waiter.start()
        for _ in waiters:
            assert selected.acquire(timeout=5)
    finally:
        release_first.set()
        for thread in [first, *waiters]:
            thread.join(5)
    assert not any(t.is_alive() for t in [first, *waiters])
    assert len(builds) == 1
    assert bodies == [b'{"build": 1}'] * 11


def test_a_request_after_a_finished_build_gets_a_fresh_one(monkeypatch):
    builds = []
    monkeypatch.setattr(snapshot, "build_agent_snapshot",
                        lambda ctx: builds.append(ctx) or {"build": len(builds)})
    shared = snapshot.SharedSnapshot()
    ctx = object()
    assert shared.body(ctx) == b'{"build": 1}'
    assert shared.body(ctx) == b'{"build": 2}'


@pytest.mark.parametrize("failure", ["builder", "encoding", "base-exception"])
def test_joined_failure_reaches_every_waiter_and_next_request_retries(monkeypatch, failure):
    started, release = threading.Event(), threading.Event()
    calls, errors = [], []
    marker = RuntimeError("snapshot build failed")
    if failure == "base-exception":
        marker = BaseException("snapshot producer exited")

    def build(ctx):
        calls.append(ctx)
        if len(calls) == 1:
            started.set()
            assert release.wait(5)
            if failure == "encoding":
                return {"unencodable": object()}
            raise marker
        return {"retry": len(calls)}

    monkeypatch.setattr(snapshot, "build_agent_snapshot", build)
    shared, ctx = snapshot.SharedSnapshot(), object()
    names = {f"failure-waiter-{i}" for i in range(3)}
    selected = _selected_waiters(monkeypatch, shared, names)

    def call():
        try:
            shared.body(ctx)
        except BaseException as exc:
            errors.append(exc)

    leader = threading.Thread(target=call, daemon=True)
    waiters = []
    leader.start()
    try:
        assert started.wait(5)
        waiters = [threading.Thread(name=name, target=call, daemon=True) for name in names]
        for waiter in waiters:
            waiter.start()
        for _ in waiters:
            assert selected.acquire(timeout=5)
    finally:
        release.set()
        for thread in [leader, *waiters]:
            thread.join(5)
    assert not any(t.is_alive() for t in [leader, *waiters])
    assert len(calls) == 1
    assert len(errors) == 4
    assert all(exc is errors[0] for exc in errors)
    if failure == "encoding":
        assert isinstance(errors[0], TypeError)
    else:
        assert errors[0] is marker
    assert shared.body(ctx) == b'{"retry": 2}'


def test_different_context_never_receives_another_contexts_result_or_error(monkeypatch):
    first_ctx, second_ctx = object(), object()
    started, release = threading.Event(), threading.Event()
    seen, values, errors = [], [], []

    def build(ctx):
        seen.append(ctx)
        if ctx is first_ctx:
            started.set()
            assert release.wait(5)
            raise ValueError("first context only")
        return {"context": "second"}

    monkeypatch.setattr(snapshot, "build_agent_snapshot", build)
    shared = snapshot.SharedSnapshot()
    selected = _selected_waiters(monkeypatch, shared, {"other-context"})

    def first():
        try:
            shared.body(first_ctx)
        except ValueError as exc:
            errors.append(str(exc))

    leader = threading.Thread(target=first, daemon=True)
    other = threading.Thread(name="other-context", daemon=True,
                             target=lambda: values.append(shared.body(second_ctx)))
    leader.start()
    try:
        assert started.wait(5)
        other.start()
        assert selected.acquire(timeout=5)
        assert seen == [first_ctx]
    finally:
        release.set()
        leader.join(5)
        if other.ident is not None:
            other.join(5)
    assert not leader.is_alive() and not other.is_alive()
    assert seen == [first_ctx, second_ctx]
    assert errors == ["first context only"]
    assert values == [b'{"context": "second"}']
