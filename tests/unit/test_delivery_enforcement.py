"""F4 enforcement (R3 61c4caac; ROOT 96f502f5): runtime identity and observation
bracket (E1-E4), S1 bound to the dispatch result's runtime identity, the
post-handoff hint with a short busy override (N4 A+B), and the receipt prune in
the Host maintenance pass (N7 C). Isolated temp DB per test (conftest), fake
backends and a fake runtime client; no timing."""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import tempfile
from types import SimpleNamespace

import pytest

from lib import agents as agents_db
from lib import db, launch_receipts as lr, maintenance, settings_store, turn_queue
import lib.turn_dispatch as _td
from lib.turn_dispatch import DispatchCommand, DispatchResult, TurnDispatchService

SERVER_DIR = pathlib.Path(__file__).resolve().parents[2] / "server"


def _sock(name):
    """A short UNIX-socket path (the 108-byte sun_path limit)."""
    return pathlib.Path(tempfile.mkdtemp(prefix="f4e", dir="/var/tmp")) / name


@pytest.fixture(autouse=True)
def _local(monkeypatch):
    monkeypatch.setattr(_td, "_RUNTIME_CLIENT", None)
    _td.reset_for_tests()
    yield
    _td.reset_for_tests()


def _status(instance="i1", release="r-A", *, capable=True, draining=False, **extra):
    caps = {"janitor_runs": True}
    if capable:
        caps["send_delivery_state"] = True
    return {"protocol_version": 1, "capabilities": caps, "draining": draining,
            "release_id": release, "instance": instance, **extra}


# --- E1: identity validation -------------------------------------------------------------

def test_runtime_identity_accepts_only_a_capable_undrained_named_runtime():
    assert lr.runtime_identity(_status()) == ("r-A", "i1")
    for bad in (None, "x", _status(capable=False), _status(draining=True),
                _status(instance=""), _status(release=""), dict(_status(), capabilities=["x"]),
                {k: v for k, v in _status().items() if k != "draining"},
                dict(_status(), capabilities={"send_delivery_state": "yes"})):
        assert lr.runtime_identity(bad) is None, bad


def test_the_runtime_rpc_status_names_its_instance_and_capability(tmp_path):
    from lib.runtime_bridge import RuntimeRPCServer
    service = SimpleNamespace(ctx=SimpleNamespace(), backends=None)
    one = RuntimeRPCServer(_sock("a.sock"), dispatch_service=service, release_id="r-A",
                           status_provider=lambda: {})
    two = RuntimeRPCServer(_sock("b.sock"), dispatch_service=service, release_id="r-A",
                           status_provider=lambda: {})
    try:
        s1 = one.dispatch_request("status", {})["result"]
        s2 = two.dispatch_request("status", {})["result"]
    finally:
        one.server_close()
        two.server_close()
    assert lr.runtime_identity(s1) == ("r-A", s1["instance"])
    assert s1["instance"] != s2["instance"], "a same-release restart is a new instance"


def test_s1_carries_the_dispatching_runtime_identity_in_the_same_result(tmp_path):
    from lib.runtime_bridge import RuntimeClient, RuntimeRPCServer

    class Owner:
        ctx = SimpleNamespace()
        backends = None

        def submit(self, command):
            return DispatchResult(session="mike", backend="claude",
                                  delivery={"state": "handed_over", "rev": 2, "seq": 3,
                                            "for": {"client_msg_id": "u-1", "trace_id": "t"}})
    server = RuntimeRPCServer(_sock("s.sock"), dispatch_service=Owner(), release_id="r-A")
    try:
        raw = server.dispatch_request("dispatch", DispatchCommand(
            text="x", requested_session="mike", trace_id="t", client_msg_id="u-1",
            synthesize_audio=False, origin="user").to_wire())
    finally:
        server.server_close()
    result = RuntimeClient._dispatch_result(raw)
    assert lr.runtime_identity(result.delivery_runtime) == ("r-A", server.instance)


# --- N4: the hint follows the handoff, under a restored short busy budget ------------------

class _Backends:
    CLAUDE = "claude"

    def __init__(self, stream):
        self.stream = stream
        self.hints_at_spawn = None

    def normalize(self, backend):
        return backend or self.CLAUDE

    def interrupt(self, backend, agent_id):
        return 0

    def active_handles(self, backend, agent_id):
        return []

    def spawn_turn(self, backend, **kwargs):
        self.hints_at_spawn = sum(1 for e in self.stream.events if "delivery-updated" in str(e))


def _service(tmp_path):
    agent = agents_db.create_agent(persona="Mike", voice_id="V", cwd=str(tmp_path),
                                   session="mike", backend="claude")
    agents_db.start_runtime(agent, "mike")
    stream = SimpleNamespace(events=[])
    stream.broadcast = stream.events.append
    backends = _Backends(stream)
    ctx = SimpleNamespace(default_session="mike", agents_path=tmp_path / "u.json", stream=stream)
    return TurnDispatchService(ctx, backend_registry=backends, home=tmp_path,
                               uuid_factory=lambda: "bs", retry_scheduler=lambda *_: None), backends


def test_n4a_the_hint_is_written_after_the_handoff_never_before(tmp_path, monkeypatch):
    from lib import server_identity
    monkeypatch.setattr(server_identity, "feature_advertised", lambda name: True)
    service, backends = _service(tmp_path)
    service.submit(DispatchCommand(text="hi", requested_session="mike", trace_id="t1",
                                   client_msg_id="u-1", synthesize_audio=False, origin="user"))
    assert backends.hints_at_spawn == 0, "no hint existed when the provider got the prompt"
    hints = [e for e in backends.stream.events if "delivery-updated" in str(e)]
    assert len(hints) == 1 and "delivery" not in hints[0] \
        and set(hints[0]) - {"type", "ts", "event_id"} == {"session", "agent_id", "message_id"}


def _busy():
    return int(db.conn().execute("PRAGMA busy_timeout").fetchone()[0])


def test_n4b_the_busy_override_is_short_and_restored_on_success_and_exception(monkeypatch):
    from lib import events, server_identity
    monkeypatch.setattr(server_identity, "feature_advertised", lambda name: True)
    lr.record_pending(agent_id="ag", trace_id="t1", client_msg_id="c1", origin="user")
    agents_db_get = agents_db.get_by_agent_id
    monkeypatch.setattr(agents_db, "get_by_agent_id",
                        lambda a: {"agent_id": a, "session": "s"} if a == "ag" else agents_db_get(a))
    seen = []
    stream = SimpleNamespace(broadcast=lambda e: seen.append(_busy()))
    monkeypatch.setattr(_td, "_DELIVERY_STREAM", stream)
    before = _busy()
    _td._announce_delivery("t1")
    assert seen == [min(before, _td.DELIVERY_HINT_BUSY_MS)] and _busy() == before

    def failing(_stream, _event):
        seen.append(_busy())
        raise RuntimeError("injected: database is locked")
    monkeypatch.setattr(events, "broadcast", failing)
    _td._announce_delivery("t1")                      # swallowed, logged
    assert seen[-1] == min(before, _td.DELIVERY_HINT_BUSY_MS) and _busy() == before


# --- N7: the prune lives in the maintenance pass ------------------------------------------------

def _age(key):
    settings_store.conn().execute("UPDATE settings SET updated_at = 0 WHERE key = ?", (key,))


def test_n7_admission_no_longer_prunes_and_a_prune_failure_never_touches_it(tmp_path, monkeypatch):
    monkeypatch.setattr(lr, "prune", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("injected")))
    service, _backends = _service(tmp_path)
    result = service.submit(DispatchCommand(text="hi", requested_session="mike", trace_id="t1",
                                            client_msg_id="u-1", synthesize_audio=False,
                                            origin="user"))
    assert result.session == "mike" and lr.get("t1")["state"] == "attempted"
    counts = maintenance.prune_database(now_ms=lr.RETENTION_MS + 10)
    assert counts["launch_receipts"] == 0 and "sse_events" in counts, "other phases still ran"


def test_n7_one_maintenance_pass_deletes_at_most_the_limit_and_is_timed(monkeypatch):
    monkeypatch.setattr(lr, "PRUNE_LIMIT", 3)
    for i in range(5):
        lr.record_pending(agent_id="ag", trace_id=f"t{i}", client_msg_id=f"c{i}", origin="user")
        _age(lr.PREFIX + f"t{i}")
    phases = maintenance._Phases()
    first = maintenance.prune_database(now_ms=lr.RETENTION_MS + 10, phases=phases)
    assert first["launch_receipts"] == 3, "no catch-up loop inside one pass"
    assert any(r[0] == "db.launch_receipts" for r in phases.records)
    assert maintenance.prune_database(now_ms=lr.RETENTION_MS + 10)["launch_receipts"] == 2


# --- independent-process interleavings ---------------------------------------------------------

_CHILD = r"""
import sys
sys.path.insert(0, {server!r})
from lib import launch_receipts as lr, db
with db.busy_timeout(30000):
    for i in range(60):
        lr.record_pending(agent_id="child", trace_id=f"c{{i}}", client_msg_id=f"cc{{i}}", origin="user")
        if i % 2:
            lr.mark_attempted(f"c{{i}}")
print("child-done")
"""


def test_independent_process_transitions_and_prunes_keep_receipt_pointer_watermark(tmp_path):
    for i in range(40):
        lr.record_pending(agent_id="parent", trace_id=f"p{i}", client_msg_id=f"pc{i}", origin="user")
        _age(lr.PREFIX + f"p{i}")
    child = subprocess.Popen([sys.executable, "-B", "-c", _CHILD.format(server=str(SERVER_DIR))],
                             env=dict(os.environ), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True)
    deleted_max = 0
    with db.busy_timeout(30000):
        for _round in range(20):
            out = lr.prune(lr.RETENTION_MS + 10)
            deleted_max = max(deleted_max, out["watermark"])
            lr.record_pending(agent_id="parent", trace_id=f"q{_round}", client_msg_id=f"qc{_round}",
                              origin="user")
    out, err = child.communicate(timeout=120)
    assert child.returncode == 0 and "child-done" in out, err
    # Invariants over the whole table, both writers done.
    receipts = {k[len(lr.PREFIX):]: json.loads(v) for k, v in settings_store.items_in_range(
        lr.PREFIX, lr.PREFIX[:-1] + ";", limit=100_000)}
    pointers = settings_store.items_in_range(lr.POINTER_PREFIX, lr.POINTER_PREFIX[:-1] + ";",
                                             limit=100_000)
    by_trace = {}
    for key, trace in pointers:
        by_trace.setdefault(trace, []).append(int(key[-20:]))
    for trace, r in receipts.items():
        assert by_trace.get(trace) == [r["seq"]], (trace, r, by_trace.get(trace))
    assert set(by_trace) <= set(receipts), "no pointer outlives its receipt"
    seqs = [r["seq"] for r in receipts.values()]
    assert len(seqs) == len(set(seqs))
    watermark = settings_store.get_int(lr.PRUNE_SEQ_KEY)
    assert watermark == deleted_max and watermark <= settings_store.get_int(lr.SEQ_KEY)
    assert sum(1 for t in receipts if t.startswith("c")) == 60, "the child's work all survived"
