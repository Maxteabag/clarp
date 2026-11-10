"""Five real HTTP failure owners; no sockets, workers or real fd2 writes.

Synthetic primitive faults prove ordinary-error ownership and privacy, not
availability under a blocked fd2 or independent diagnostic delivery.
"""
import builtins
import contextlib
import queue
import sqlite3
from types import SimpleNamespace

import pytest

from lib import http_diagnostics, telemetry
from tests.integration.test_server_di import server_module as root


SECRET = "synthetic-private-token-body-transcript"


class OpaqueError(RuntimeError):
    def __str__(self):
        raise AssertionError("exception message evaluated")

    def __repr__(self):
        raise AssertionError("exception repr evaluated")


class Interruption(BaseException):
    pass


@contextlib.contextmanager
def sink(monkeypatch, result="full", order=None):
    """Scope the native primitive replacement to the owning invocation only."""
    calls = []

    def write(fd, payload):
        calls.append((fd, payload))
        if order is not None:
            order.append("fallback")
        if isinstance(result, BaseException):
            raise result
        if type(result) is str and result == "full":
            return len(payload)
        if type(result) is str and result == "short":
            return len(payload) - 1
        return result

    with monkeypatch.context() as patch:
        patch.setattr(http_diagnostics.os, "write", write)
        yield calls


def assert_record(calls, site, category="RUNTIME"):
    assert calls == [(2, f"clarpHttpDiagnostic site={site} error={category}\n".encode())]
    assert len(calls[0][1]) <= 96
    assert SECRET.encode() not in calls[0][1]


@pytest.mark.parametrize("site", ["H01", "H02", "H03", "H04", "H05"])
@pytest.mark.parametrize("error, category", [
    (OSError(SECRET), "IO"), (sqlite3.OperationalError(SECRET), "DB"),
    (ValueError(SECRET), "VALUE"), (ImportError(SECRET), "IMPORT"),
    (OpaqueError(SECRET), "RUNTIME"), (LookupError(SECRET), "OTHER"),
])
def test_fixed_codes_are_the_only_new_bytes(monkeypatch, site, error, category):
    with sink(monkeypatch) as calls:
        result = http_diagnostics._note_http_failure(site, error)
    assert result == "ATTEMPTED"  # a count, never a delivery receipt
    assert_record(calls, site, category)


class UnsafeValue:
    def __eq__(self, other):
        pytest.fail("arbitrary value comparison invoked")

    def __str__(self):
        pytest.fail("arbitrary value formatting invoked")


class UnexpectedCount(int):
    def __eq__(self, other):
        pytest.fail("int subclass comparison invoked")


@pytest.mark.parametrize("site, error", [
    ("unknown", RuntimeError(SECRET)), (UnsafeValue(), RuntimeError(SECRET)),
    ("H01", UnsafeValue()), ("H01", Interruption()),
])
def test_invalid_internal_inputs_do_not_write(monkeypatch, site, error):
    with sink(monkeypatch) as calls:
        result = http_diagnostics._note_http_failure(site, error)
    assert result == "DROPPED"
    assert calls == []


@pytest.mark.parametrize("result", [0, -1, "short", True, None, UnsafeValue(), UnexpectedCount(43)])
def test_partial_or_invalid_result_is_dropped_without_completion(monkeypatch, result):
    with sink(monkeypatch, result) as calls:
        outcome = http_diagnostics._note_http_failure("H03", OpaqueError(SECRET))
    assert outcome == "DROPPED"  # short writes may already have emitted bytes
    assert_record(calls, "H03")


def test_ordinary_classification_fault_is_dropped_before_any_write(monkeypatch):
    class BrokenClassification(Exception):
        @property
        def __class__(self):
            raise OpaqueError(SECRET)

    with sink(monkeypatch) as calls:
        outcome = http_diagnostics._note_http_failure("H03", BrokenClassification(SECRET))
    assert outcome == "DROPPED"
    assert calls == []


@pytest.mark.parametrize("failure", [OSError(SECRET), InterruptedError(SECRET),
                                     ValueError(SECRET), OpaqueError(SECRET)])
def test_ordinary_primitive_fault_drops_once_without_recursive_sink(monkeypatch, failure):
    with sink(monkeypatch, failure) as calls:
        outcome = http_diagnostics._note_http_failure("H03", OpaqueError(SECRET))
    assert outcome == "DROPPED"
    assert_record(calls, "H03")


def test_primitive_interruption_is_not_translated_or_retried(monkeypatch):
    failure = Interruption()
    with sink(monkeypatch, failure) as calls:
        with pytest.raises(Interruption) as caught:
            http_diagnostics._note_http_failure("H03", RuntimeError(SECRET))
    assert caught.value is failure
    assert_record(calls, "H03")


@pytest.mark.parametrize("primary", [BrokenPipeError, ConnectionResetError, TimeoutError])
@pytest.mark.parametrize("log_failed", [False, True])
@pytest.mark.parametrize("sink_result", ["full", OpaqueError(SECRET)])
def test_routine_disconnect_stays_suppressed_after_log_failure(
        monkeypatch, primary, log_failed, sink_result):
    order = []

    def log(*args):
        order.append("log")
        assert args[0] == "clientGone"
        if log_failed:
            raise OpaqueError(SECRET)

    monkeypatch.setattr(root, "log", log)
    server = object.__new__(root.ContextHTTPServer)
    with sink(monkeypatch, sink_result, order) as calls:
        try:
            raise primary(SECRET)
        except primary:
            server.handle_error(None, (SECRET, 1))
    assert order == (["log", "fallback"] if log_failed else ["log"])
    if log_failed:
        assert_record(calls, "H01")
    else:
        assert calls == []


def test_unexpected_server_error_retains_stdlib_owner(monkeypatch):
    seen = []
    primary = RuntimeError(SECRET)

    def stdlib(server, request, address):
        seen.append((request, address, root.sys.exc_info()[1]))

    monkeypatch.setattr(root.ThreadingHTTPServer, "handle_error", stdlib)
    server = object.__new__(root.ContextHTTPServer)
    with sink(monkeypatch) as calls:
        try:
            raise primary
        except RuntimeError:
            server.handle_error("request", (SECRET, 1))
    assert seen == [("request", (SECRET, 1), primary)]
    assert calls == []


@pytest.mark.parametrize("primary_failed", [False, True])
@pytest.mark.parametrize("telemetry_failure", [None, "close", "import"])
@pytest.mark.parametrize("sink_result", ["full", OpaqueError(SECRET)])
def test_finish_preserves_primary_and_cleanup_order(
        monkeypatch, primary_failed, telemetry_failure, sink_result):
    order = []
    primary = RuntimeError("finish-primary")

    def finish(handler):
        order.append("finish")
        if primary_failed:
            raise primary

    def close_db():
        order.append("db")

    def close_telemetry():
        order.append("telemetry")
        if telemetry_failure == "close":
            raise OpaqueError(SECRET)

    original_import = builtins.__import__

    def import_module(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "lib" and "telemetry" in fromlist and telemetry_failure == "import":
            order.append("import-fail")
            raise ImportError(SECRET)
        return original_import(name, globals, locals, fromlist, level)

    with monkeypatch.context() as owner_patch:
        owner_patch.setattr(root.BaseHTTPRequestHandler, "finish", finish)
        owner_patch.setattr(root.db, "close_local", close_db)
        owner_patch.setattr(telemetry, "close_local", close_telemetry)
        handler = object.__new__(root.Handler)
        with sink(monkeypatch, sink_result, order) as calls:
            with monkeypatch.context() as patch:
                patch.setattr(builtins, "__import__", import_module)
                if primary_failed:
                    with pytest.raises(RuntimeError) as caught:
                        handler.finish()
                    assert caught.value is primary
                else:
                    handler.finish()
        expected = ["finish", "db", "import-fail" if telemetry_failure == "import" else "telemetry"]
        assert order == expected + (["fallback"] if telemetry_failure else [])
        if telemetry_failure:
            assert_record(calls, "H02", "IMPORT" if telemetry_failure == "import" else "RUNTIME")
        else:
            assert calls == []


@pytest.mark.parametrize("primary_failed", [False, True])
def test_finish_db_failure_still_replaces_primary_and_skips_telemetry(monkeypatch, primary_failed):
    order = []
    primary, cleanup = RuntimeError("finish-primary"), RuntimeError("db-cleanup")

    def finish(handler):
        order.append("finish")
        if primary_failed:
            raise primary

    def close_db():
        order.append("db")
        raise cleanup

    with monkeypatch.context() as owner_patch:
        owner_patch.setattr(root.BaseHTTPRequestHandler, "finish", finish)
        owner_patch.setattr(root.db, "close_local", close_db)
        owner_patch.setattr(telemetry, "close_local", lambda: order.append("telemetry"))
        with sink(monkeypatch) as calls:
            with pytest.raises(RuntimeError) as caught:
                object.__new__(root.Handler).finish()
        assert caught.value is cleanup
        assert order == ["finish", "db"]
        assert calls == []  # adjacent DB-close policy is not repaired in this packet


@pytest.mark.parametrize("failed_stage", [None, "metrics", "phases", "eventlog"])
@pytest.mark.parametrize("sink_result", ["full", OpaqueError(SECRET)])
def test_http_observation_cannot_change_response_or_recurse(monkeypatch, failed_stage, sink_result):
    order = []
    handler = object.__new__(root.Handler)
    handler.path = "/synthetic?token=" + SECRET
    handler.command = "POST"
    handler.close_connection = False
    handler._status_code = 503
    handler._headers_buffer = [b"synthetic-existing-header\r\n"]
    handler.wfile = SimpleNamespace(write=lambda *_: pytest.fail("diagnostic wrote HTTP bytes"))
    handler.send_response = lambda *_: pytest.fail("diagnostic emitted a response")
    handler.send_header = lambda *_: pytest.fail("diagnostic emitted a header")
    handler.end_headers = lambda: pytest.fail("diagnostic closed response headers")
    handler._response_phases = {"unchanged": 1}
    handler._interaction_id = lambda: "synthetic-interaction"
    handler.address_string = lambda: SECRET

    class BadPhases:
        def __iter__(self):
            raise OpaqueError(SECRET)

    if failed_stage == "phases":
        handler._response_phases = BadPhases()
    original_phases = handler._response_phases

    def metrics():
        order.append("metrics")
        if failed_stage == "metrics":
            raise OpaqueError(SECRET)
        return {}

    def emit(*args, **kwargs):
        order.append("eventlog")
        assert args == ("server", "httpRequest")
        if failed_stage == "eventlog":
            raise OpaqueError(SECRET)

    monkeypatch.setattr(root.db, "finish_request_metrics", metrics)
    monkeypatch.setattr(root.eventlog, "emit", emit)
    with sink(monkeypatch, sink_result, order) as calls:
        handler._log_http(503, root.time.time())
    assert handler.close_connection is False
    assert handler._status_code == 503
    assert handler._headers_buffer == [b"synthetic-existing-header\r\n"]
    assert handler._response_phases is original_phases
    assert order == (["metrics", "eventlog"] if failed_stage is None else
                     ["metrics", "eventlog", "fallback"] if failed_stage == "eventlog" else
                     ["metrics", "fallback"])
    if failed_stage:
        assert_record(calls, "H03")
    else:
        assert calls == []


def sse_handler(monkeypatch, mode, log_failed, cleanup_failure=None):
    """Finite real _sse owner invocation: evict, or disconnect on live payload."""
    from lib import http_utils

    order, wire, attempts = [], [], []
    handler = object.__new__(root.Handler)
    handler.headers = {"Authorization": SECRET}
    handler._query = lambda: {}
    handler._live_subscription = lambda: False
    handler.send_response = lambda code: order.append(("status", code))
    handler.send_header = lambda name, value: order.append(("header", name, value))
    handler.end_headers = lambda: order.append("headers-end")

    class Subscription:
        evicted = True

        def get(self, timeout):
            order.append("get")
            if mode == "evict":
                raise queue.Empty()
            return '{"kind":"synthetic"}'

    subscription = Subscription()

    def subscribe(**kwargs):
        order.append("subscribe")
        return subscription

    def unsubscribe(value):
        assert value is subscription
        order.append("unsubscribe")
        if cleanup_failure is not None:
            raise cleanup_failure

    def write(data):
        attempts.append(data)
        if mode == "disconnect" and len(attempts) == 3:
            raise BrokenPipeError(SECRET)
        wire.append(data)

    def emit(*args, **kwargs):
        order.append(args[1])
        if log_failed:
            raise OpaqueError(SECRET)

    handler.wfile = SimpleNamespace(write=write, flush=lambda: order.append("flush"))
    handler.server = SimpleNamespace(ctx=SimpleNamespace(stream=SimpleNamespace(
        subscribe=subscribe, unsubscribe=unsubscribe, recent=lambda cursor: [])))
    monkeypatch.setattr(http_utils, "principal_of", lambda _: SimpleNamespace(principal=None))
    monkeypatch.setattr(root.events, "agent_roster", lambda: {"kind": "roster"})
    monkeypatch.setattr(root.eventlog, "emit", emit)
    return handler, order, wire, attempts


@pytest.mark.parametrize("mode", ["evict", "disconnect"])
@pytest.mark.parametrize("log_failed", [False, True])
@pytest.mark.parametrize("cleanup_failed", [False, True])
@pytest.mark.parametrize("sink_result", ["full", OpaqueError(SECRET)])
def test_sse_close_keeps_wire_and_exact_cleanup_owner(
        monkeypatch, mode, log_failed, cleanup_failed, sink_result):
    cleanup = RuntimeError("unsubscribe-owner") if cleanup_failed else None
    handler, order, wire, attempts = sse_handler(monkeypatch, mode, log_failed, cleanup)
    with sink(monkeypatch, sink_result, order) as calls:
        if cleanup_failed:
            with pytest.raises(RuntimeError) as caught:
                handler._sse()
            assert caught.value is cleanup
        else:
            handler._sse()
    assert handler.close_connection is True
    assert order.count(("status", 200)) == 1
    assert order.count("subscribe") == order.count("unsubscribe") == 1
    assert order.count("get") == 1
    event = "sseEvictedClose" if mode == "evict" else "sseClientGone"
    assert order.count(event) == 1
    tail = [event, "fallback", "unsubscribe"] if log_failed else [event, "unsubscribe"]
    assert order[-len(tail):] == tail
    assert wire == [b": connected\n\n", b'data: {"kind": "roster"}\n\n']
    assert len(attempts) == (2 if mode == "evict" else 3)
    if log_failed:
        assert_record(calls, "H04" if mode == "evict" else "H05")
    else:
        assert calls == []


@pytest.mark.parametrize("mode", ["evict", "disconnect"])
def test_sse_diagnostic_interruption_propagates_through_unsubscribe(monkeypatch, mode):
    handler, order, _, _ = sse_handler(monkeypatch, mode, log_failed=True)
    interruption = Interruption()
    with sink(monkeypatch, interruption, order) as calls:
        with pytest.raises(Interruption) as caught:
            handler._sse()
    assert caught.value is interruption
    assert order[-2:] == ["fallback", "unsubscribe"]
    assert order.count("unsubscribe") == 1
    assert_record(calls, "H04" if mode == "evict" else "H05")
