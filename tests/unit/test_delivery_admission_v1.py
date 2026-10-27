"""F4 delivery admission, first source child (ROOT 1710a835 on design 67922402): the runtime's
A2 self-report, ONE fresh bounded manager read (B) per /log bracket behind a 1-in-flight /
2-per-second (burst 2) gate with a bounded kill + reap reserve, the reconcile transaction-entry
guard, the hint-only SSE event with its replay strip, and the health-only /status block.
Private fixtures only: fake manager output, fake /proc and clocks, and small local fixture
child processes for the probe runner; no unit, systemctl, production /proc or network."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from lib import db, events, launch_receipts as lr, maintenance, runtime_attestation as ra, settings_store, sse_store
from lib import turn_queue

BOOT = "0b8a1f9e-2c3d-4e5f-8a9b-0c1d2e3f4a5b"
INV = "3932aee90792440bae6f9c8c5a14c973"
CG = "/user.slice/user-1000.slice/user@1000.service/app.slice/clarp-runtime.service"
A2 = {"pid": 4242, "start_ticks": 777, "boot_id": BOOT, "cgroup": CG, "invocation": INV}
SERVER = Path(__file__).resolve().parents[2] / "server"


def _obs(instance="i1", a2=None):
    return {"protocol_version": 1, "capabilities": {"send_delivery_state": True}, "draining": False,
            "release_id": "r-A", "instance": instance, "attestation": dict(A2 if a2 is None else a2)}


def _show(main=4242, inv=INV, cg=CG, active="active", extra=""):
    return f"MainPID={main}\nInvocationID={inv}\nControlGroup={cg}\nActiveState={active}\n{extra}".encode()


class _Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def fake(monkeypatch):
    """Fake boot / proc readers and a frozen clock; run_probe returns `probe.result`."""
    clock = _Clock()
    probe = SimpleNamespace(result=(0, _show(), "", None, True), calls=0, in_tx=[], gate=None, timeouts=[], advance=0.0)

    def run_probe(timeout=None):
        probe.calls += 1
        probe.timeouts.append(timeout)
        clock.t += probe.advance
        if threading.current_thread() is threading.main_thread():
            probe.in_tx.append(db.conn().in_transaction)
        if probe.gate is not None:
            probe.gate.wait(10)
        return probe.result
    proc_reads = []

    def proc(pid):
        proc_reads.append(pid)
        return 777 if pid == 4242 else None
    monkeypatch.setattr(ra, "CLOCK", clock)
    monkeypatch.setattr(ra, "run_probe", run_probe)
    monkeypatch.setattr(ra, "BOOT_READER", lambda: BOOT)
    monkeypatch.setattr(ra, "PROC_READER", proc)
    return SimpleNamespace(clock=clock, probe=probe, proc_reads=proc_reads)


# --- A2 self-report ------------------------------------------------------------------------

STAT = "4242 (python3 (x) y) S " + " ".join(["1"] * 18) + " 777 1 1"


def _reader(cgroup=f"0::{CG}\n", stat=STAT, boot=BOOT + "\n"):
    files = {"/proc/self/cgroup": cgroup, "/proc/self/stat": stat, "/proc/sys/kernel/random/boot_id": boot}
    return lambda path: files[path]


def test_a2_is_a_membership_self_report_and_malformed_parts_are_absent():
    assert ra.self_report(read=_reader(), environ={"INVOCATION_ID": INV}, pid=4242) == A2
    for kw, env in (({}, {}), ({"cgroup": "0::/app.slice/other.service\n"}, {"INVOCATION_ID": INV}),
                    ({"stat": "garbage"}, {"INVOCATION_ID": INV}), ({"boot": "x"}, {"INVOCATION_ID": INV})):
        assert ra.self_report(read=_reader(**kw), environ=env, pid=4242) is None
    for bad in (dict(A2, pid=True), dict(A2, invocation=INV.upper()), dict(A2, extra=1), None):
        assert ra.attestation_of({"attestation": bad}) is None


def test_the_runtime_status_carries_its_attestation_field(monkeypatch):
    import tempfile
    from lib.runtime_bridge import RuntimeRPCServer
    monkeypatch.setattr(ra, "self_report", lambda: dict(A2))
    sock = Path(tempfile.mkdtemp(prefix="f4v", dir="/var/tmp")) / "a.sock"
    server = RuntimeRPCServer(sock, dispatch_service=SimpleNamespace(ctx=SimpleNamespace(), backends=None),
                              release_id="r-A", status_provider=lambda: {})
    try:
        status = server.dispatch_request("status", {})["result"]
    finally:
        server.server_close()
    assert status["attestation"] == server.attestation == A2


# --- B: manager identity -------------------------------------------------------------------

def test_b_binds_only_on_every_field_and_reads_proc_only_after_the_mainpid_match(fake):
    assert ra.bind(A2, 0, _show()) == "bound" and fake.proc_reads == [4242]
    fake.proc_reads.clear()
    cases = [((3, _show()), "nonzero"), ((0, _show()[:-1]), "bad_bytes"),
             ((0, _show() + "é\n".encode()), "bad_bytes"), ((0, _show(extra="MainPID=4242\n")), "bad_bytes"),
             ((0, _show(extra="Foo=1\n")), "bad_bytes"), ((0, b"MainPID=4242\n"), "bad_bytes"),
             ((0, _show(active="activating")), "inactive"), ((0, _show(main=5151)), "mainpid_mismatch"),
             ((0, _show(main=0)), "mainpid_mismatch"), ((0, _show(inv="0" * 32)), "invocation_mismatch"),
             ((0, _show(cg="/x/other.service")), "cgroup_mismatch")]
    for (rc, raw), want in cases:
        assert ra.bind(A2, rc, raw) == want, want
    assert fake.proc_reads == [], "no /proc read of an attested pid the manager did not name"
    assert ra.bind(dict(A2, boot_id="1" * 8 + BOOT[8:]), 0, _show()) == "boot_mismatch"
    assert ra.bind(dict(A2, start_ticks=778), 0, _show()) == "start_mismatch"


def _fixture(tmp_path, body):
    script = tmp_path / "fake_show.py"
    script.write_text(body)
    return [sys.executable, str(script)]


@pytest.mark.parametrize("body,rc,reason", [
    ("import sys; sys.stdout.write('MainPID=1\\n')", 0, ""),
    ("import sys; sys.exit(3)", 3, ""),
    ("import sys; sys.stdout.write('x' * 5000); sys.stdout.flush()", None, "bad_bytes"),
], ids=["ok", "nonzero", "oversize"])
def test_the_probe_runner_returns_bytes_rc_or_a_closed_reason(tmp_path, monkeypatch, body, rc, reason):
    monkeypatch.setattr(ra, "SYSTEMCTL", sys.executable)
    monkeypatch.setattr(ra, "SHOW_ARGV", tuple(_fixture(tmp_path, body)))
    got_rc, out, got_reason, unreaped, spawned = ra.run_probe()
    assert got_reason == reason and unreaped is None and spawned
    if not reason:
        assert got_rc == rc


def test_a_slow_probe_is_killed_and_reaped_within_the_recorded_reserve(tmp_path, monkeypatch):
    assert (ra.SHOW_TIMEOUT_S, ra.REAP_RESERVE_S, ra.OUT_CAP) == (1.0, 0.25, 4096)
    monkeypatch.setattr(ra, "SYSTEMCTL", sys.executable)
    monkeypatch.setattr(ra, "SHOW_ARGV", tuple(_fixture(tmp_path, "import time; time.sleep(30)")))
    monkeypatch.setattr(ra, "SHOW_TIMEOUT_S", 0.3)
    t0 = time.monotonic()
    rc, out, reason, unreaped, spawned = ra.run_probe()
    elapsed = time.monotonic() - t0
    assert reason == "timeout" and unreaped is None and spawned
    assert elapsed < 0.3 + ra.REAP_RESERVE_S + 0.5, elapsed


def test_no_systemctl_or_a_spawn_error_never_starts_a_child(monkeypatch):
    monkeypatch.setattr(ra, "SYSTEMCTL", "/nonexistent/systemctl")
    assert ra.run_probe()[2:] == ("non_systemd", None, False)
    monkeypatch.setattr(ra, "SYSTEMCTL", sys.executable)

    def boom(*a, **k):
        raise OSError("no exec")
    monkeypatch.setattr(ra, "SPAWN", boom)
    assert ra.run_probe()[2:] == ("exec_error", None, False)


class _Stuck:
    """A child that cannot be reaped until `released`."""

    def __init__(self):
        r, w = os.pipe()
        self._w = w
        self.stdout = os.fdopen(r, "rb", buffering=0)
        self.released, self.killed, self.returncode = False, 0, None

    def wait(self, timeout=None):
        if self.released:
            self.returncode = -9
            return -9
        raise subprocess.TimeoutExpired("x", timeout)

    def poll(self):
        if self.released:
            self.returncode = -9
        return self.returncode

    def kill(self):
        self.killed += 1
        if self._w is not None:
            os.close(self._w)
            self._w = None


def test_an_unreaped_child_keeps_the_slot_and_later_brackets_omit_without_a_second_child(monkeypatch):
    spawned = []

    def spawn(*a, **k):
        child = _Stuck()
        spawned.append(child)
        return child
    monkeypatch.setattr(ra, "SYSTEMCTL", sys.executable)
    monkeypatch.setattr(ra, "SPAWN", spawn)
    monkeypatch.setattr(ra, "SHOW_TIMEOUT_S", 0.2)
    t0 = time.monotonic()
    assert ra.admit(_obs(), in_transaction=False) is False
    assert time.monotonic() - t0 < 0.2 + ra.REAP_RESERVE_S + 0.5
    assert len(spawned) == 1 and spawned[0].killed == 1
    for _ in range(3):
        assert ra.admit(_obs(), in_transaction=False) is False
    assert len(spawned) == 1, "never a second child while the slot is held"
    block = ra.status_block()
    assert block["omitted"]["timeout"] == 1 and block["omitted"]["unreaped"] == 3
    assert block["gate"]["inflight_now"] == 1
    spawned[0].released = True
    ra.admit(_obs(), in_transaction=False)
    assert len(spawned) == 2, "the slot frees once that child is reaped"


# --- per-bracket freshness and the gate ------------------------------------------------------

def test_every_bracket_is_a_fresh_probe_and_a_failure_does_not_stick(fake):
    assert ra.admit(_obs(), in_transaction=False)
    fake.clock.t += 1
    assert ra.admit(_obs(), in_transaction=False)
    assert fake.probe.calls == 2, "no reuse across brackets"
    fake.clock.t += 1
    fake.probe.result = (0, _show(main=5151), "", None, True)
    assert ra.admit(_obs(), in_transaction=False) is False
    fake.clock.t += 1
    fake.probe.result = (0, _show(), "", None, True)
    assert ra.admit(_obs(), in_transaction=False) is True


def test_refusals_before_the_probe_never_start_one(fake):
    assert ra.admit(dict(_obs(), attestation=None), in_transaction=False) is False
    assert ra.admit(_obs(), in_transaction=True) is False
    assert ra.admit(_obs(), in_transaction=False, disconnected=True) is False
    assert fake.probe.calls == 0
    omitted = ra.status_block()["omitted"]
    assert (omitted["no_attestation"], omitted["in_transaction"], omitted["disconnected"]) == (1, 1, 1)


def test_the_rate_is_two_per_second_with_a_burst_of_two(fake):
    assert ra.admit(_obs(), in_transaction=False) and ra.admit(_obs(), in_transaction=False)
    assert ra.admit(_obs(), in_transaction=False) is False and fake.probe.calls == 2
    fake.clock.t += 0.5
    assert ra.admit(_obs(), in_transaction=False) is True
    assert ra.admit(_obs(), in_transaction=False) is False
    assert ra.status_block()["omitted"]["gate_rate"] == 2


def test_one_probe_in_flight_and_a_concurrent_bracket_omits_without_waiting(fake):
    fake.probe.gate = threading.Event()
    results = []
    t = threading.Thread(target=lambda: results.append(ra.admit(_obs(), in_transaction=False)))
    t.start()
    for _ in range(500):
        if fake.probe.calls:
            break
        time.sleep(0.005)
    t0 = time.monotonic()
    assert ra.admit(_obs(), in_transaction=False) is False
    assert time.monotonic() - t0 < 0.5
    fake.probe.gate.set()
    t.join(5)
    assert results == [True] and fake.probe.calls == 1
    assert ra.status_block()["omitted"]["gate_inflight"] == 1


# --- F1: optional enrichment only -------------------------------------------------------------

def _server():
    import server as server_module
    return server_module


def test_an_enrichment_exception_omits_and_is_counted_but_baseexception_propagates(monkeypatch):
    server_module = _server()
    monkeypatch.setattr(server_module.agents_db, "get_by_session", lambda session: {"agent_id": "a"})
    body = {"turns": [{"role": "user", "id": "m1"}]}

    def raises(exc):
        def fields(*a, **k):
            raise exc
        return SimpleNamespace(_delivery_fields=fields)
    assert server_module.Handler._add_delivery_state(raises(RuntimeError("x")), body, "s", {}) == ""
    assert body == {"turns": [{"role": "user", "id": "m1"}]}, "no partial field on omit"
    assert ra.status_block()["unexpected_by_type"] == {"RuntimeError": 1}
    assert server_module.Handler._add_delivery_state(
        raises(RuntimeError("x")), body, "s", {"delivery_since": ["x"]}) == "bad delivery_since", \
        "the parameter 400 is unchanged"
    with pytest.raises(KeyboardInterrupt):
        server_module.Handler._add_delivery_state(raises(KeyboardInterrupt()), body, "s", {})


# --- ROOT 17e71bf1: ONE 2.0 s enrichment deadline (mock clock) -------------------------------

class _Runtime:
    """A runtime client on the test's clock. A stall above the call's cap times out after
    exactly the cap (an honest client); ("over", s) takes s and still answers (a residual
    blocking client), so the deadline checks after it are exercised too."""

    def __init__(self, clock, *stalls, observation=None):
        self.clock, self.stalls, self.caps = clock, list(stalls), []
        self.observation = observation or _obs()

    def status_within(self, total_s):
        from lib.runtime_bridge import RuntimeUnavailable
        self.caps.append(total_s)
        stall = self.stalls.pop(0) if self.stalls else 0.0
        if isinstance(stall, tuple):
            self.clock.t += stall[1]
        elif stall > total_s:
            self.clock.t += total_s
            raise RuntimeUnavailable("timed out")
        else:
            self.clock.t += stall
        return dict(self.observation)

    def status(self, **kwargs):
        raise AssertionError("a /log bracket never uses the shared default status()")


def _handler(runtime):
    server_module = _server()
    handler = server_module.Handler.__new__(server_module.Handler)
    handler.ctx = SimpleNamespace(runtime_client=runtime)
    handler._client_gone = lambda: False
    return handler


def _bracket(runtime, body=None):
    body = {"turns": []} if body is None else body
    return _handler(runtime)._delivery_fields(body, "ag", since=0, until=None, generation=None,
                                              bootstrap=False, carried=("", ""))


def _snapshot_takes(monkeypatch, clock, seconds, calls=None):
    from lib import launch_receipts
    real = launch_receipts.annotate_rows

    def annotate(agent_id, users):
        if calls is not None:
            calls.append(agent_id)
        clock.t += seconds
        return real(agent_id, users)
    monkeypatch.setattr(launch_receipts, "annotate_rows", annotate)


def test_a_normal_bracket_fits_the_deadline_and_reports_its_phases(fake, monkeypatch):
    runtime = _Runtime(fake.clock, 0.125, 0.125)
    fake.probe.advance = 0.25
    _snapshot_takes(monkeypatch, fake.clock, 0.125)
    assert _bracket(runtime) is not None
    assert runtime.caps == [ra.O1_CAP_S, ra.O2_CAP_S] == [0.5, 0.5] and fake.probe.timeouts == [1.0]
    budget = ra.status_block()["budget"]
    assert budget["last_ms"] == {"o1": 125, "b": 250, "snapshot": 125, "o2": 125, "total": 625}
    assert budget["deadline_ms"] == 2000 and budget["caps_ms"] == {"o1": 500, "b": 1000, "b_reap_reserve": 250,
                                                                   "o2": 500}


def test_an_o1_miss_that_stalls_times_out_at_its_cap_and_omits_before_b(fake):
    runtime = _Runtime(fake.clock, 5.0)
    assert _bracket(runtime) is None
    assert runtime.caps == [0.5] and fake.probe.calls == 0
    assert ra.status_block()["omitted"]["o1_unavailable"] == 1
    assert ra.status_block()["budget"]["last_ms"]["total"] == 500


def test_an_o2_that_stalls_omits_and_no_partial_field_or_cursor_escapes(fake, monkeypatch):
    server_module = _server()
    monkeypatch.setattr(server_module.agents_db, "get_by_session", lambda session: {"agent_id": "ag"})
    runtime = _Runtime(fake.clock, 0.0, 5.0)
    handler = _handler(runtime)
    body = {"turns": [{"role": "user", "id": "m1"}]}
    assert server_module.Handler._add_delivery_state(handler, body, "s", {"delivery_since": ["0"]}) == ""
    assert body == {"turns": [{"role": "user", "id": "m1"}]}, "nothing of the snapshot leaves"
    assert runtime.caps == [0.5, 0.5] and fake.probe.calls == 1
    assert ra.status_block()["omitted"]["o2_unavailable"] == 1


@pytest.mark.parametrize("o1,b,snapshot,calls_o2,calls_b,snapshots", [
    (("over", 1.875), 0.0, 0.0, 0, 0, 0),         # nothing left for B and its reap reserve
    (0.125, 1.875, 0.0, 0, 1, 0),                 # nothing left for the snapshot
    (0.125, 0.25, 1.625, 0, 1, 1),                # nothing left for O2
], ids=["before-b", "before-snapshot", "before-o2"])
def test_an_exhausted_budget_omits_and_never_starts_the_next_phase(fake, monkeypatch, o1, b, snapshot,
                                                                    calls_o2, calls_b, snapshots):
    runtime = _Runtime(fake.clock, o1)
    fake.probe.advance = b
    seen = []
    _snapshot_takes(monkeypatch, fake.clock, snapshot, seen)
    assert _bracket(runtime) is None
    assert (len(runtime.caps) - 1, fake.probe.calls, len(seen)) == (calls_o2, calls_b, snapshots)
    assert ra.status_block()["omitted"]["deadline_exhausted"] == 1


def test_each_phase_cap_is_clamped_to_what_is_left(fake):
    runtime = _Runtime(fake.clock, ("over", 1.0), 0.0)
    fake.probe.advance = 0.625
    assert _bracket(runtime) is not None
    assert fake.probe.timeouts == [0.75], "B: min(1.0, 1.0 left - 0.25 reap reserve)"
    assert runtime.caps == [0.5, 0.375], "O2: min(0.5, 0.375 left)"


def test_sql_past_the_deadline_is_interrupted_and_the_snapshot_still_closes(fake, monkeypatch):
    from lib import launch_receipts
    heavy = "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c WHERE x < 200000) SELECT count(*) FROM c"

    def annotate(agent_id, users):
        fake.clock.t += 2.5
        return db.conn().execute(heavy).fetchone()[0]
    monkeypatch.setattr(launch_receipts, "annotate_rows", annotate)
    assert _bracket(_Runtime(fake.clock)) is None
    assert ra.status_block()["omitted"]["deadline_exhausted"] == 1
    assert db.conn().in_transaction is False
    assert db.conn().execute(heavy).fetchone()[0] == 200000, "the interrupt handler is cleared"


def test_o1_reads_a_fresh_shared_status_without_waiting_and_o2_is_always_fresh(fake, monkeypatch):
    from lib import backends
    runtime = _Runtime(fake.clock)
    monkeypatch.setattr(backends, "_RUNTIME_CLIENT", runtime)
    monkeypatch.setattr(backends, "_clock", fake.clock)
    monkeypatch.setattr(backends, "_STATUS_CACHE", (runtime, fake.clock.t, _obs(), None))
    assert _bracket(runtime) is not None and runtime.caps == [0.5], "O1 from the cache, O2 fresh"
    monkeypatch.setattr(backends, "_STATUS_CACHE", (runtime, fake.clock.t - 1.0, _obs(), None))
    fake.clock.t += 1
    assert _bracket(runtime) is not None and runtime.caps == [0.5, 0.5, 0.5], "a 1 s old status is a miss"
    with backends._STATUS_LOCK:                   # another caller's shared RPC in flight: never waited for
        monkeypatch.setattr(backends, "_STATUS_CACHE", (runtime, fake.clock.t, _obs(), None))
        fake.clock.t += 1
        assert _bracket(runtime) is not None and runtime.caps == [0.5] * 5


class _Socket:
    def __init__(self, log, replies, clock=None):
        self.log, self.replies, self.clock = log, list(replies), clock

    def __call__(self, *args):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def settimeout(self, value):
        self.log.append(value)

    def connect(self, path):
        pass

    def sendall(self, data):
        pass

    def recv(self, n):
        if self.clock is not None:
            self.clock[0] += 0.2
        return self.replies.pop(0) if self.replies else b"x"

    def close(self):
        pass


def test_the_shared_10s_default_stays_for_every_other_caller(monkeypatch):
    import socket as real_socket
    from lib import runtime_bridge
    from lib.runtime_bridge import RuntimeClient, RuntimeUnavailable

    def fake_socket(sock):
        return SimpleNamespace(socket=sock, AF_UNIX=real_socket.AF_UNIX, SOCK_STREAM=real_socket.SOCK_STREAM,
                               timeout=real_socket.timeout)
    log = []
    monkeypatch.setattr(runtime_bridge, "socket", fake_socket(_Socket(log, [b'{"ok":true,"result":{"a":1}}\n'])))
    client = RuntimeClient("/nonexistent.sock")
    assert client.timeout == 10.0 and client.status() == {"a": 1} and log == [10.0]
    log.clear()
    now = [0.0]
    monkeypatch.setattr(runtime_bridge, "time", SimpleNamespace(monotonic=lambda: now[0], sleep=lambda s: None))
    monkeypatch.setattr(runtime_bridge, "socket", fake_socket(_Socket(log, [], clock=now)))
    with pytest.raises(RuntimeUnavailable):
        client.status_within(0.5)                 # a reply that drips forever
    assert now[0] == pytest.approx(0.6) and len(log) == 5 and max(log) <= 0.5, log
    assert client.timeout == 10.0


def test_cleanup_stays_inside_the_budget_and_an_unreaped_child_keeps_the_slot(tmp_path, monkeypatch):
    monkeypatch.setattr(ra, "SYSTEMCTL", sys.executable)
    monkeypatch.setattr(ra, "SHOW_ARGV", tuple(_fixture(tmp_path, "import time; time.sleep(30)")))
    budget = ra.Budget(total_s=0.6)
    t0 = time.monotonic()
    assert ra.admit(_obs(), in_transaction=False, budget=budget) is False
    assert time.monotonic() - t0 < 0.6 + 0.15 and ra.status_block()["probes"]["reaped"] == 1
    spawned = []

    def spawn(*a, **k):
        spawned.append(_Stuck())
        return spawned[-1]
    monkeypatch.setattr(ra, "SPAWN", spawn)
    monkeypatch.setattr(ra, "CLOCK", lambda: time.monotonic() + 10)    # the gate has refilled
    budget = ra.Budget(total_s=0.6)
    t0 = time.monotonic()
    assert ra.admit(_obs(), in_transaction=False, budget=budget) is False
    assert time.monotonic() - t0 < 0.6 + 0.15 and len(spawned) == 1
    assert ra.admit(_obs(), in_transaction=False, budget=ra.Budget(total_s=0.6)) is False and len(spawned) == 1
    assert ra.status_block()["omitted"]["unreaped"] == 1 and ra.status_block()["gate"]["inflight_now"] == 1


# --- health block -------------------------------------------------------------------------

def test_the_status_block_is_closed_bounded_and_side_effect_free(fake, monkeypatch):
    for i in range(20):
        ra.note_unexpected(type(f"Err{i}", (Exception,), {})())
    ra.note_unexpected(type("bad name!", (Exception,), {})())
    with monkeypatch.context() as m:
        for module, attr in ((ra, "run_probe"), (ra, "SPAWN"), (ra, "BOOT_READER"), (ra, "PROC_READER"),
                             (db, "conn"), (settings_store, "get_text")):
            m.setattr(module, attr, lambda *a, **k: (_ for _ in ()).throw(AssertionError("touched")))
        before = json.dumps([ra._GATE["tokens"], ra._GATE["stamp"], ra._HEALTH], sort_keys=True, default=str)
        block = ra.status_block()
        assert ra.status_block() == block
        assert json.dumps([ra._GATE["tokens"], ra._GATE["stamp"], ra._HEALTH], sort_keys=True,
                          default=str) == before, "reading the block changes nothing"
    assert set(block) == {"scope", "reset_on", "gate", "probes", "omitted", "unexpected_by_type", "budget",
                          "reconcile"}
    assert block["gate"]["inflight_limit"] == 1 and block["gate"]["rate_per_s"] == 2
    assert block["gate"]["burst"] == 2 and block["gate"]["probes_per_log_response"] == 1
    assert list(block["omitted"]) == list(ra.OMIT_REASONS) and len(ra.OMIT_REASONS) == 22
    names = block["unexpected_by_type"]
    assert len([n for n in names if n != "_other"]) == 16 and names["_other"] == 5
    assert set(block["reconcile"]["findings"]) == set(lr.FINDING_KINDS)
    assert len(json.dumps(block, separators=(",", ":"))) <= ra.STATUS_MAX_BYTES
    monkeypatch.setattr(ra, "STATUS_MAX_BYTES", 100)
    over = ra.status_block()
    assert over["overflow"] is True and over["limit"] == 100 and over["scope"] == "process"


def test_counters_saturate_and_logs_coalesce_while_counting_everything(fake):
    ra._HEALTH["omitted"]["timeout"] = ra._COUNTER_MAX
    ra.note("timeout")
    assert ra.status_block()["omitted"]["timeout"] == ra._COUNTER_MAX
    logs = [ra.note_unexpected(ValueError()) for _ in range(3)]
    fake.clock.t += ra.LOG_COALESCE_S
    logs.append(ra.note_unexpected(ValueError()))
    assert logs == [True, False, False, True]
    assert ra.status_block()["unexpected_by_type"]["ValueError"] == 4


# --- reconcile entry guard, rollback, run_once ----------------------------------------------

class _ClockMs:
    def __init__(self):
        self.t = 1_800_000_000_000

    def __call__(self):
        self.t += 1
        return self.t


@pytest.fixture
def ms(monkeypatch):
    c = _ClockMs()
    for module in (db, settings_store, lr):
        monkeypatch.setattr(module, "now_ms", c)
    return c


def _pending(trace):
    lr.record_pending(agent_id="ag", trace_id=trace, client_msg_id="c-" + trace, origin="user",
                      user_stop_generation=turn_queue.user_stop_generation("ag"))


def _legacy_rewrite(trace, ms):
    settings_store.set_text(lr._key(trace), json.dumps(
        {"state": "pending", "agent_id": "ag", "trace_id": trace, "client_msg_id": "c-" + trace,
         "origin": "user", "sender_agent_id": "", "admitted_at": ms.t, "user_stop_gen": 0}), updated_at=ms())


def _state():
    return [tuple(r) for r in db.conn().execute(
        "SELECT key, value, updated_at FROM settings WHERE key LIKE 'launch-receipt%' ORDER BY key").fetchall()]


def test_reconcile_refuses_inside_a_transaction_and_rolls_back_whole_on_failure(ms, monkeypatch):
    _pending("t1")
    _pending("t2")
    _legacy_rewrite("t1", ms)
    before = _state()
    with settings_store.transaction():
        assert lr.reconcile()["refused"] is True
    assert _state() == before
    real = settings_store.set_int
    monkeypatch.setattr(settings_store, "set_int", lambda key, value: (_ for _ in ()).throw(RuntimeError("x"))
                        if key == lr.PRUNE_SEQ_KEY else real(key, value))
    with pytest.raises(RuntimeError):
        lr.reconcile()
    assert _state() == before and db.conn().in_transaction is False


def test_the_real_run_once_enters_reconcile_outside_a_transaction(ms, tmp_path, monkeypatch):
    _pending("t1")
    _legacy_rewrite("t1", ms)
    seen, real = [], lr.reconcile
    monkeypatch.setattr(lr, "reconcile", lambda: seen.append(db.conn().in_transaction) or real())
    counts = maintenance.MaintenanceWorker(audio_dir=tmp_path).run_once(checkpoint=False)
    assert seen == [False] and counts["launch_receipt_pointers"] == 1
    assert settings_store.get_int(lr.PRUNE_SEQ_KEY) == settings_store.get_int(lr.SEQ_KEY) == 2, \
        "the proven global reset is unchanged"
    health = lr.health_snapshot()
    assert health["passes"] == 1 and health["findings"]["orphan_legacy"] == 1


# --- SSE: hint-only, eligibility exact, replay stripped -------------------------------------

def test_the_hint_has_exactly_the_identity_and_keeps_the_eligibility(tmp_path, monkeypatch):
    from lib import agents as agents_db, message_writes
    import lib.turn_dispatch as td
    sent = []
    monkeypatch.setattr(td.events, "broadcast", lambda stream, ev: sent.append(dict(ev)))
    monkeypatch.setattr(agents_db, "get_by_agent_id", lambda a: {"agent_id": a, "session": "s"})
    _pending("t1")
    td._announce_delivery_locked(object(), "t1")
    assert sent == [{"type": "delivery-updated", "session": "s", "agent_id": "ag",
                     "message_id": message_writes._client_message_id("c-t1")}]
    settings_store.set_text(lr._key("t2"), json.dumps({"state": "pending", "agent_id": "ag", "trace_id": "t2",
                                                       "client_msg_id": "c-t2", "origin": "user"}))
    td._announce_delivery_locked(object(), "t2")
    assert len(sent) == 1, "a receipt that would not project emits nothing, as before"


def test_replay_strips_a_stored_delivery_without_rewriting_the_row():
    legacy = {"type": "delivery-updated", "session": "s", "agent_id": "a", "message_id": "m",
              "delivery": {"state": "handed_over", "rev": 2, "seq": 3}}
    eid = sse_store.record_sse_event(legacy)
    broken = sse_store.record_sse_event({"type": "delivery-updated", "session": "s", "agent_id": "a",
                                         "delivery": {"state": "stopped"}})
    other = sse_store.record_sse_event({"type": "agent-focus", "session": "s", "agent_id": "a"})
    out = {e["event_id"]: e for e in sse_store.events_after(0)}
    assert "delivery" not in out[eid] and out[eid]["message_id"] == "m"
    assert broken not in out and other in out
    assert all("delivery" not in e for e in sse_store.recent_events(10 ** 9) if e["type"] == "delivery-updated")
    stored = db.conn().execute("SELECT payload FROM sse_events WHERE event_id=?", (eid,)).fetchone()[0]
    assert '"delivery"' in stored, "the stored row is never rewritten"
    assert events.FIELDS[events.SSEType.DELIVERY_UPDATED] == ("session", "agent_id", "message_id")


def test_a_malformed_identity_is_dropped_from_replay_only_and_replay_moves_past_it():
    hint = {"type": "delivery-updated", "session": "s", "agent_id": "a", "message_id": "m"}
    start = int(db.conn().execute("SELECT COALESCE(MAX(event_id), 0) FROM sse_events").fetchone()[0])
    first = sse_store.record_sse_event(dict(hint, delivery={"state": "pending_launch"}, extra="x"))
    malformed = [dict(hint, session=""), {k: v for k, v in hint.items() if k != "agent_id"},
                 dict(hint, message_id=7), dict(hint, agent_id=None)] * 3          # 12 consecutive bad rows
    bad = [sse_store.record_sse_event(event) for event in malformed]
    last = sse_store.record_sse_event(dict(hint, message_id="m2"))
    def rows():
        return [tuple(r) for r in db.conn().execute("SELECT event_id, payload FROM sse_events ORDER BY event_id")]
    stored = rows()
    out = sse_store.events_after(start, limit=2)
    assert [e["event_id"] for e in out] == [first, last], "a page of dropped rows never stalls the cursor"
    assert all(set(e) == {"type", "session", "agent_id", "message_id", "event_id", "ts"} for e in out)
    assert sse_store.events_after(first, limit=1) == [out[1]] and sse_store.events_after(last) == []
    assert not {e["event_id"] for e in sse_store.recent_events(10 ** 9)} & set(bad)
    assert rows() == stored, "replay rewrites no stored row and makes up no id"
    tail = sse_store.record_sse_event(dict(hint, session=None))           # a dropped row at the tail of the page
    assert sse_store.events_after(last, limit=1) == [], "only the table's end shortens a page"
    later = sse_store.record_sse_event(dict(hint, message_id="m3"))
    assert [e["event_id"] for e in sse_store.events_after(last, limit=1)] == [later], \
        "the next cursor reads past the dropped tail: nothing skipped, nothing duplicated"
    assert tail not in {e["event_id"] for e in sse_store.events_after(start)}


def test_the_runtime_relay_reads_past_dropped_hints_and_relays_valid_ones_identity_only():
    from lib import agents as agents_db
    from lib.runtime_events import _RUNTIME_MARKER, RuntimeEventWatcher
    assert _RUNTIME_MARKER == sse_store._RUNTIME_MARKER

    class Recording:
        def __init__(self):
            self.events = []

        def broadcast_ephemeral(self, event):
            self.events.append(dict(event))
    relay = Recording()
    watcher = RuntimeEventWatcher(relay)
    hint = {"type": "delivery-updated", "session": "s", "agent_id": "a", "message_id": "m", _RUNTIME_MARKER: True}
    page = [agents_db.record_sse_event(dict(hint, message_id="")) for _ in range(501)]  # more than a whole page
    valid = agents_db.record_sse_event(dict(hint, delivery={"state": "handed_over"}))   # a legacy stored hint
    watcher._poll_once()
    assert relay.events == [{"type": "delivery-updated", "session": "s", "agent_id": "a", "message_id": "m",
                             "ts": relay.events[0]["ts"], "event_id": valid}], relay.events[:1]
    assert watcher._last_id == valid > page[-1]
    tail = [agents_db.record_sse_event(dict(hint, session=None)) for _ in range(3)]       # dropped rows at the tail
    watcher._poll_once()
    assert relay.events[1:] == [] and watcher._last_id == tail[-1], "the relay moves past them, never re-reads"
    later = agents_db.record_sse_event(dict(hint, message_id="m2"))
    watcher._poll_once()
    assert [e["event_id"] for e in relay.events] == [valid, later], "nothing skipped, nothing duplicated"
    client = sse_store.events_after(page[0] - 1)
    assert [e["event_id"] for e in client] == [valid, later] and all(_RUNTIME_MARKER not in e for e in client), \
        "a client's since-cursor reads the same valid rows; the internal marker never reaches it"


def test_a_hint_the_runtime_emits_is_relayed_live_as_the_identity_only():
    from lib.runtime_events import _RUNTIME_MARKER, RuntimeEventStream, RuntimeEventWatcher

    class Recording:
        def __init__(self):
            self.events = []

        def broadcast_ephemeral(self, event):
            self.events.append(dict(event))
    relay = Recording()
    watcher = RuntimeEventWatcher(relay)
    events.broadcast(RuntimeEventStream(), events.delivery_updated(session="s", agent_id="a", message_id="m"))
    watcher._poll_once()
    [sent] = relay.events
    assert set(sent) == {"type", "session", "agent_id", "message_id", "event_id", "ts"}
    assert (sent["type"], sent["session"], sent["agent_id"], sent["message_id"]) == ("delivery-updated", "s", "a", "m")
    [replayed] = sse_store.events_after(sent["event_id"] - 1)
    assert replayed == sent and _RUNTIME_MARKER not in replayed, "a client replays the same hint without the marker"


def test_the_replay_strip_is_scoped_to_delivery_updated_only():
    others = [{"type": "agent-status", "session": "s", "agent_id": "a", "delivery": {"kept": True}, "n": 1},
              {"type": "agent-focus", "session": "s", "agent_id": "a", "message_id": 5},
              {"type": "runtime-x", "session": "", "agent_id": None, "extra": [1, 2]}]
    ids = [sse_store.record_sse_event(event) for event in others]
    stored = {}
    for eid, ts, kind, payload in db.conn().execute(
            "SELECT event_id, ts, type, payload FROM sse_events WHERE event_id IN (?, ?, ?)", ids):
        expected = json.loads(payload)                  # the decode as before F4: column fill-ins only
        expected.setdefault("event_id", eid)
        expected.setdefault("ts", ts)
        expected.setdefault("type", kind)
        stored[eid] = expected
    after = {e["event_id"]: e for e in sse_store.events_after(0)}
    recent = {e["event_id"]: e for e in sse_store.recent_events(10 ** 9)}
    assert all(after[eid] == stored[eid] and recent[eid] == stored[eid] for eid in ids), \
        "every other type replays exactly as stored, also to the Host's runtime-event relay"


# --- egress gate (source) -------------------------------------------------------------------

def test_no_f4_projection_leaves_the_host_outside_the_log_bracket():
    server_src = (SERVER / "server.py").read_text()
    assert "result.delivery" not in server_src, "the Host forwards no dispatch-result delivery"
    assert len(re.findall(r"launch_receipts\.annotate_rows\(", server_src)) == 1
    assert len(re.findall(r"launch_receipts\.delivery_page\(", server_src)) == 1
    body = server_src[server_src.index("def _delivery_fields"):server_src.index("def _client_gone")]
    assert "annotate_rows(" in body and "delivery_page(" in body
    lib = SERVER / "lib"
    users = sorted(p.name for p in lib.glob("*.py")
                   if "delivery_updated(" in p.read_text() and p.name != "events.py")
    assert users == ["turn_dispatch.py"]
    assert "delivery=" not in (lib / "turn_dispatch.py").read_text().split(
        "def _announce_delivery_locked")[1].split("\ndef ")[0]
