"""Exact fake-Codex resource ownership; never a production pool reset.

The registry is deliberately sticky on failure. Pytest continues other fixture
finalizers after an error, so both sink resets and MonkeyPatch.undo consult it.
"""
from __future__ import annotations

from contextlib import contextmanager
import subprocess
import sys
import threading
from time import monotonic as _real_monotonic, sleep as _real_sleep


class QuiescenceError(RuntimeError):
    pass


class OwnerGate:
    def __init__(self):
        self.owners = set()

    def check(self, cas=None):
        if self.owners:
            raise QuiescenceError("Codex fixture owner not positively quiescent")
        cas = cas if cas is not None else sys.modules.get("lib.codex_app_server")
        if cas is not None:
            # These are exact published references, not a global thread scan.
            # Even an unregistered/foreign client must not cross a sink reset.
            for pool in (cas._CLIENTS, cas._DRAINING, cas._RETIRING):
                for client in list(pool.values()):
                    # poll()/reader exit alone cannot prove an unowned
                    # client's unretained callback/timer queue is empty.
                    raise QuiescenceError("unowned Codex pool client lacks quiescence provenance")


GATE = OwnerGate()


def _stopped(client):
    proc = getattr(client, "proc", None)
    if proc is None or type(proc.poll()) is not int:
        return False
    return all(actor.is_alive() is False for actor in
               (getattr(client, "reader", None), getattr(client, "stderr_reader", None))
               if actor is not None)


def assert_quiescent():
    GATE.check()


def _pools(cas):
    return (cas._CLIENTS, cas._DRAINING, cas._RETIRING)


def _is_process(client) -> bool:
    return isinstance(getattr(client, "proc", None), subprocess.Popen)


def drop_hand_built_clients(cas=None) -> int:
    """Take out of the pools every client a test built by hand: an object
    with a stand-in for its process, put into a pool the test patched or
    moved into another pool by the code under test. It is not a process, so
    there is nothing to retire; left in a pool it fails the quiescence gate
    of this test (the gate looks before the test's patches are undone) and,
    in a pool the test did not patch, of every later test of the worker.
    A client that IS a process is left where it is: the gate still sees it.
    Run for every test by the conftest fixture of the same name."""
    cas = cas if cas is not None else sys.modules.get("lib.codex_app_server")
    if cas is None:
        return 0
    dropped = 0
    with cas._LOCK:
        for pool in _pools(cas):
            for key, client in list(pool.items()):
                if not _is_process(client):
                    del pool[key]
                    dropped += 1
    return dropped


def retire_left_clients(cas=None, limit=15.0) -> None:
    """Retire the processes a test left, until the pools are empty. For a
    test that starts clients through the code under test, in its own
    teardown. An idle client is retired at once; one a held start has just
    been released on is idle a moment later. A process recycled as stuck has
    exited but stays listed as retiring until the next send prunes it
    (``cas._client``), and no send follows a test: pruned here the same way,
    once its readers are done. Left in a pool, it fails the next test's
    quiescence gate, and with it every later test of the worker."""
    cas = cas if cas is not None else sys.modules["lib.codex_app_server"]
    deadline = _real_monotonic() + limit
    while True:
        cas.recycle_clients()
        with cas._LOCK:
            for key, retired in list(cas._RETIRING.items()):
                readers = (getattr(retired, "reader", None), getattr(retired, "stderr_reader", None))
                if (_is_process(retired) and retired.proc.poll() is not None and not any(
                        reader is not None and reader.is_alive() for reader in readers)):
                    del cas._RETIRING[key]
            left = [client for pool in _pools(cas) for client in pool.values() if _is_process(client)]
        if not left or _real_monotonic() >= deadline:
            return
        _real_sleep(0.05)


class PoolOwner:
    def __init__(self, cas, monkeypatch, *, gate=GATE, timeout=15.0):
        gate.check(cas)
        self.cas, self.monkeypatch, self.gate = cas, monkeypatch, gate
        self.timeout = timeout
        self.lock = threading.RLock()
        self.changed = threading.Condition(self.lock)
        self.bodies = 0
        self.context = threading.local()
        self.creator = threading.current_thread()
        self.clients = {}
        self.actors = []
        self.errors = []
        self.state = "OPEN"
        self.original_undo = monkeypatch.undo
        self.undo_attribute = vars(monkeypatch).get("undo")
        self.had_undo_attribute = "undo" in vars(monkeypatch)
        # Do NOT put the guard in its own undo stack: nothing may restore it
        # before the original operation has proved quiescence.
        monkeypatch.undo = self.undo
        gate.owners.add(self)
        owner = self
        original_client = cas._Client

        class Client(original_client):
            def __new__(cls, *args, **kwargs):
                client = super().__new__(cls)
                owner.register(client)
                return client

            def __init__(self, *args, **kwargs):
                # Also cover explicit object.__new__(Client) followed by init.
                if owner.clients.get(id(self)) is not self:
                    owner.register(self)
                initialize = super().__init__
                owner.run(self, lambda: initialize(*args, **kwargs))

            def _callback(self, callback, value):
                # A retained bound dispatch may be invoked after CAS's module
                # proxy was undone. Its own original owner still gates entry.
                dispatch = super()._callback
                return owner.run(self, lambda: dispatch(callback, value))

        self.client_class = Client
        monkeypatch.setattr(cas, "_Client", Client)
        monkeypatch.setattr(cas, "threading", _Threading(owner))

    def register(self, client):
        with self.lock:
            if self.state != "OPEN":
                self.errors.append("client construction after close admission")
                raise QuiescenceError(self.errors[-1])
            # Only this fixture's root and its exact owned descendants may
            # allocate clients; an unrelated producer is never adopted.
            if threading.current_thread() is not self.creator and not self.is_producer():
                self.errors.append("foreign client construction during fixture")
                raise QuiescenceError(self.errors[-1])
            self.clients[id(client)] = client

    def is_producer(self):
        return getattr(self.context, "active", False)

    @contextmanager
    def producing(self):
        previous = self.is_producer()
        self.context.active = True
        try:
            yield
        finally:
            self.context.active = previous

    def owns_target(self, target):
        bound = getattr(target, "__self__", None)
        values = [bound, getattr(bound, "client", None)]
        values.extend(cell.cell_contents for cell in (getattr(target, "__closure__", None) or ()))
        # An explicit foreign client wins over inherited producer context.
        clients = [value for value in values if isinstance(value, self.client_class.__bases__[0])]
        if clients:
            return all(self.clients.get(id(client)) is client for client in clients)
        return self.is_producer()

    def retain(self, actor):
        with self.lock:
            if self.state != "OPEN" and not (self.state == "CLOSING" and self.is_producer()):
                raise QuiescenceError("actor construction after owner admission closed")
            self.actors.append(actor)

    def start(self, actor, start):
        with self.lock:
            if self.state not in ("OPEN", "CLOSING"):
                raise QuiescenceError("actor start after owner admission closed")
            if self.state == "CLOSING":
                if isinstance(actor, threading.Timer):
                    actor.cancel()  # real Timer.run observes finished; no recurring body
                elif not self.is_producer():
                    raise QuiescenceError("late external actor start during close")
            actor._owner_start_attempted = True
            try:
                start()
            except BaseException:
                # Thread.start can have launched before raising. Retain the
                # object and sticky uncertainty; never treat this as unstarted.
                self.errors.append("owned actor start failed or partially started")
                raise

    def run(self, actor, run):
        with self.lock:
            if self.state not in ("OPEN", "CLOSING"):
                raise QuiescenceError("owned actor body after owner closed/failed")
            self.bodies += 1
        try:
            with self.producing():
                return run()
        finally:
            with self.changed:
                self.bodies -= 1
                self.changed.notify_all()

    def undo(self):
        with self.lock:
            if self.state != "CLOSED":
                raise QuiescenceError("refusing monkeypatch undo before Codex quiescence")
        self.original_undo()  # native restoration/errors, no filtering or retry
        if self.had_undo_attribute:
            self.monkeypatch.undo = self.undo_attribute
        else:
            del self.monkeypatch.undo
        # A test may have patched a pool away while it still held one of this
        # owner's clients (taken from the pool, then the pool replaced). The
        # close above saw the patched pools; the ones just restored can hold
        # that client, stopped and proven quiescent, and the next test's gate
        # would refuse it and every test after it.
        with self.cas._LOCK:
            self._forget_owned()

    def _forget_owned(self):
        """Under cas._LOCK: take this owner's clients out of the pools."""
        for pool in (self.cas._CLIENTS, self.cas._DRAINING, self.cas._RETIRING):
            for key, client in list(pool.items()):
                if self.clients.get(id(client)) is client:
                    del pool[key]

    def close(self):
        deadline = _real_monotonic() + self.timeout
        try:
            self._close(deadline)
        except BaseException:
            with self.lock:
                self.state = "FAILED"
            raise

    def _close(self, deadline):
        with self.lock:
            if self.state == "CLOSED":
                return
            if self.state != "OPEN":
                raise QuiescenceError("Codex owner already failed/closing; no implicit retry")
            self.state = "CLOSING"
            for actor in self.actors:
                if isinstance(actor, threading.Timer):
                    actor.cancel()
        # Stop can block too. It belongs to the same deadline as every join.
        def retire(client):
            try:
                try:
                    client.retire()
                finally:
                    # An initializer may have published proc but not yet its
                    # locks/pending maps. Even a failed retire must attempt
                    # process stop, within this same tracked worker/deadline.
                    if client.proc.poll() is None:
                        client._stop_process()
            except BaseException as exc:
                with self.lock:
                    self.errors.append(f"owned retire failed: {type(exc).__name__}")
                raise

        with self.producing():
            for client in list(self.clients.values()):
                if hasattr(client, "proc"):
                    worker = _OwnedThread(self, target=retire, args=(client,), daemon=True)
                    worker.start()
        while True:
            with self.lock:
                actors = list(self.actors)
            for actor in actors:
                remaining = deadline - _real_monotonic()
                if remaining <= 0:
                    break
                # Never join a never-started object. Late ordinary starts
                # and the final CLOSED transition share the support lock.
                if actor.ident is not None:
                    actor.join(remaining)
            with self.lock:
                alive = [actor for actor in self.actors if actor.is_alive()]
                if not alive and not self.bodies:
                    stopped = all(not hasattr(c, "proc") or _stopped(c)
                                  for c in self.clients.values())
                    if stopped and not self.errors:
                        # No own actor can now spawn more work. The per-object
                        # start/body gates survive namespace/fixture undo.
                        self.state = "QUIESCENT"
                        break
                    self.errors.append("client exit or actor start not positively confirmed")
                if self.errors or _real_monotonic() >= deadline:
                    self.state = "FAILED"
                    raise QuiescenceError("Codex fixture cleanup failed: " +
                                          "; ".join(self.errors or ["total deadline expired"]))
                if not alive:
                    self.changed.wait(max(0, deadline - _real_monotonic()))
        remaining = deadline - _real_monotonic()
        if remaining <= 0 or not self.cas._LOCK.acquire(timeout=remaining):
            raise QuiescenceError("Codex pool identity cleanup exceeded total deadline")
        try:
            self._forget_owned()
        finally:
            self.cas._LOCK.release()
        with self.lock:
            self.state = "CLOSED"
        self.gate.owners.remove(self)


class _OwnedThread(threading.Thread):
    def __init__(self, owner, *args, **kwargs):
        self.owner = owner
        super().__init__(*args, **kwargs)
        target = self._target
        if target is not None:
            self._target = lambda *a, **kw: owner.run(self, lambda: target(*a, **kw))
        owner.retain(self)

    def start(self):
        return self.owner.start(self, super().start)

    def run(self):
        return self.owner.run(self, super().run)


class _OwnedTimer(threading.Timer):
    def __init__(self, owner, *args, **kwargs):
        self.owner = owner
        super().__init__(*args, **kwargs)
        function = self.function
        self.function = lambda *a, **kw: owner.run(self, lambda: function(*a, **kw))
        owner.retain(self)

    def start(self):
        return self.owner.start(self, super().start)

    def run(self):
        return self.owner.run(self, super().run)


class _Threading:
    def __init__(self, owner):
        self.owner = owner

    def __getattr__(self, name):
        return getattr(threading, name)

    def Thread(self, *args, **kwargs):
        target = kwargs.get("target", args[1] if len(args) > 1 else None)
        if self.owner.owns_target(target):
            return _OwnedThread(self.owner, *args, **kwargs)
        return threading.Thread(*args, **kwargs)

    def Timer(self, interval, function, args=None, kwargs=None):
        if self.owner.owns_target(function):
            return _OwnedTimer(self.owner, interval, function, args=args, kwargs=kwargs)
        return threading.Timer(interval, function, args=args, kwargs=kwargs)
