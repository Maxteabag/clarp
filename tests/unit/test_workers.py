"""lib.workers: the ordered background-worker registry behind build_server."""
import pytest

from lib import workers as workers_module
from lib.workers import BOOT, SERVING, Worker, WorkerSet, started


class FakeServer:
    def __init__(self):
        self.close_callbacks = []

    def on_close(self, callback):
        self.close_callbacks.append(callback)

    def server_close(self):
        for callback in self.close_callbacks:
            callback()


class Handle:
    def __init__(self, name, journal):
        self.name = name
        self.journal = journal

    def start(self):
        self.journal.append(("start", self.name))

    def stop(self):
        self.journal.append(("stop", self.name))


def _worker(name, journal, **kw):
    return Worker(name, lambda: started(Handle(name, journal)), **kw)


def test_workers_start_in_registry_order_and_stop_in_the_same_order():
    journal = []
    srv = FakeServer()
    workers = WorkerSet([_worker(name, journal) for name in ("a", "b", "c")])

    assert workers.start(srv) == ["a", "b", "c"]
    srv.server_close()

    assert journal == [("start", "a"), ("start", "b"), ("start", "c"),
                       ("stop", "a"), ("stop", "b"), ("stop", "c")]
    assert workers.names() == ["a", "b", "c"]
    assert list(workers.running) == ["a", "b", "c"]
    assert isinstance(workers.get("b"), Handle)


def test_disabled_and_declined_workers_register_no_stop():
    journal = []
    srv = FakeServer()
    workers = WorkerSet([
        _worker("on", journal),
        _worker("off", journal, enabled=lambda: False),
        Worker("declined", lambda: None),
    ])

    assert workers.start(srv) == ["on"]

    assert workers.skipped == ["off", "declined"]
    assert workers.get("off") is None and workers.get("declined") is None
    assert [callback.worker.name for callback in srv.close_callbacks] == ["on"]
    assert workers.diagnostics() == {
        "running": ["on"], "skipped": ["off", "declined"],
        "order": ["on", "off", "declined"]}


def test_enabled_predicate_is_read_at_start_time_not_registration_time():
    flag = {"on": False}
    journal = []
    workers = WorkerSet([_worker("late", journal, enabled=lambda: flag["on"])])
    flag["on"] = True

    assert workers.start(FakeServer()) == ["late"]


def test_custom_stop_receives_the_handle_returned_by_start():
    stopped = []
    srv = FakeServer()
    workers = WorkerSet([Worker("relay", lambda: "relay-handle",
                                stop=lambda handle: stopped.append(handle))])
    workers.start(srv)
    srv.server_close()

    assert stopped == ["relay-handle"]


def test_stages_start_separately_and_only_once():
    journal = []
    srv = FakeServer()
    workers = WorkerSet([
        _worker("boot-1", journal),
        _worker("listener", journal, stage=SERVING),
        _worker("boot-2", journal),
    ])

    assert workers.start(srv, BOOT) == ["boot-1", "boot-2"]
    assert journal == [("start", "boot-1"), ("start", "boot-2")]
    assert workers.names(SERVING) == ["listener"]
    assert workers.start(srv, SERVING) == ["listener"]
    with pytest.raises(RuntimeError):
        workers.start(srv, BOOT)


def test_start_failure_propagates_after_earlier_workers_registered_stops():
    journal = []
    srv = FakeServer()

    def explode():
        raise OSError("port in use")

    workers = WorkerSet([_worker("first", journal), Worker("broken", explode),
                         _worker("never", journal)])
    with pytest.raises(OSError):
        workers.start(srv)
    srv.server_close()

    assert journal == [("start", "first"), ("stop", "first")]


def test_registry_rejects_duplicate_names_and_unknown_stages():
    with pytest.raises(ValueError):
        WorkerSet([Worker("dup", lambda: 1), Worker("dup", lambda: 2)])
    with pytest.raises(ValueError):
        Worker("x", lambda: 1, stage="later")


def test_each_start_logs_one_worker_start_line(monkeypatch):
    lines = []
    monkeypatch.setattr(workers_module, "log", lambda event, detail="": lines.append((event, detail)))
    WorkerSet([Worker("one", lambda: object()), Worker("two", lambda: object())]).start(FakeServer())

    assert lines == [("workerStart", "name=one"), ("workerStart", "name=two")]
