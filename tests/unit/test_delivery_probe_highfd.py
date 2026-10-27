"""ROOT 9b2c02cb R-a / R-c: the manager-read probe with its stdout pipe on a REAL owned fd >= 1024 (beyond
select()'s FD_SETSIZE), and the runtime status attestation that a status provider cannot replace.

Each high-fd case runs in its OWN child Python (so only that child raises its RLIMIT_NOFILE soft limit, and
only if needed): it spawns a real local fixture process, moves its stdout pipe to an fd >= 1024 with
fcntl(F_DUPFD), proves select.select() would reject that fd, then runs the REAL run_probe. A host that cannot
give such an fd fails with an explicit BLOCKER, never a skip."""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import tempfile
from types import SimpleNamespace

import pytest

from lib import runtime_attestation as ra

SERVER = pathlib.Path(__file__).resolve().parents[2] / "server"
HIGH = 1024

_CHILD = r'''
import fcntl, json, os, resource, select, subprocess, sys, time
case, server, high = sys.argv[1], sys.argv[2], int(sys.argv[3])
soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
want = high + 64
if soft < want:
    if hard != resource.RLIM_INFINITY and hard < want:
        print(json.dumps({"blocker": f"RLIMIT_NOFILE hard {hard} < {want}: no fd >= {high} possible"}))
        sys.exit(3)
    resource.setrlimit(resource.RLIMIT_NOFILE, (want, hard))
sys.path.insert(0, server)
from lib import runtime_attestation as ra

def to_high(fd):
    new = fcntl.fcntl(fd, fcntl.F_DUPFD, high)
    os.close(fd)
    return new

def select_rejects(fd):
    try:
        select.select([fd], [], [], 0)
    except ValueError:
        return True
    return False

made = []

class HighFdChild:
    """A REAL child process whose stdout pipe this test moved to an owned fd >= 1024."""
    def __init__(self, argv, **kwargs):
        self.p = subprocess.Popen(argv, **kwargs)
        fd = to_high(os.dup(self.p.stdout.fileno()))
        self.p.stdout.close()
        self.stdout = os.fdopen(fd, "rb", buffering=0)
        self.fd, self.rejects = fd, select_rejects(fd)
        made.append(self)
    def kill(self): self.p.kill()
    def wait(self, timeout=None): return self.p.wait(timeout)
    def poll(self): return self.p.poll()
    @property
    def returncode(self): return self.p.returncode

class StuckChild:
    """Cannot be reaped: a pipe (read end on an owned fd >= 1024) nobody writes to."""
    def __init__(self, argv, **kwargs):
        r, w = os.pipe()
        r = to_high(r)
        self.stdout = os.fdopen(r, "rb", buffering=0)
        self.fd, self.rejects, self._w, self.killed = r, select_rejects(r), w, 0
        made.append(self)
    def kill(self):
        self.killed += 1
    def wait(self, timeout=None):
        raise subprocess.TimeoutExpired("stuck", timeout)
    def poll(self): return None
    returncode = None

scripts = {
    "ready": "import sys; sys.stdout.write('MainPID=1\\n')",
    "eof": "pass",
    "timeout": "import time; time.sleep(30)",
    "cap": "import sys; sys.stdout.write('x' * 5000); sys.stdout.flush()",
}
ra.SYSTEMCTL = sys.executable
res = {"case": case}
if case in scripts:
    ra.SHOW_ARGV = (sys.executable, "-c", scripts[case])
    ra.SPAWN = HighFdChild
    t0 = time.monotonic()
    rc, out, reason, unreaped, spawned = ra.run_probe(0.3 if case == "timeout" else 1.0)
    res.update(rc=rc, out_len=len(out), out=out[:64].decode("latin-1"), reason=reason,
               unreaped=unreaped is not None, spawned=spawned, elapsed=time.monotonic() - t0,
               reaped=made[0].p.returncode is not None)
else:  # "unreaped": the slot is kept and a later bracket omits without a second child
    ra.SPAWN = StuckChild
    a2 = {"pid": 4242, "start_ticks": 777, "boot_id": "0b8a1f9e-2c3d-4e5f-8a9b-0c1d2e3f4a5b",
          "cgroup": "/user.slice/user-1000.slice/user@1000.service/app.slice/clarp-runtime.service",
          "invocation": "3932aee90792440bae6f9c8c5a14c973"}
    obs = {"attestation": a2}
    t0 = time.monotonic()
    first = ra.admit(obs, in_transaction=False, budget=ra.Budget(total_s=0.6))
    elapsed = time.monotonic() - t0
    second = ra.admit(obs, in_transaction=False, budget=ra.Budget(total_s=0.6))
    block = ra.status_block()
    res.update(first=first, second=second, elapsed=elapsed, children=len(made), killed=made[0].killed,
               omitted_timeout=block["omitted"]["timeout"], omitted_unreaped=block["omitted"]["unreaped"],
               inflight_now=block["gate"]["inflight_now"])
res.update(fds=[m.fd for m in made], select_rejects=[m.rejects for m in made])
print(json.dumps(res))
'''


def _child(case):
    proc = subprocess.run([sys.executable, "-B", "-c", _CHILD, case, str(SERVER), str(HIGH)],
                          capture_output=True, text=True, timeout=30)
    lines = [x for x in proc.stdout.splitlines() if x.startswith("{")]
    assert lines, f"no result (rc {proc.returncode}): {proc.stderr[-2000:]}"
    res = json.loads(lines[-1])
    assert "blocker" not in res, f"BLOCKER: {res['blocker']}"
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert res["fds"] and all(fd >= HIGH for fd in res["fds"]), res
    assert all(res["select_rejects"]), "the fd must be beyond select()'s FD_SETSIZE"
    return res


@pytest.mark.parametrize("case", ["ready", "eof", "timeout", "cap"])
def test_the_probe_reads_a_pipe_on_an_fd_above_1024(case):
    res = _child(case)
    assert res["spawned"] is True and res["unreaped"] is False and res["reaped"] is True
    if case == "ready":
        assert (res["rc"], res["reason"], res["out"]) == (0, "", "MainPID=1\n")
    elif case == "eof":
        assert (res["rc"], res["reason"], res["out_len"]) == (0, "", 0)
    elif case == "timeout":
        assert res["reason"] == "timeout" and res["elapsed"] < 0.3 + ra.REAP_RESERVE_S + 0.5, res
    else:
        assert res["reason"] == "bad_bytes" and res["out_len"] == ra.OUT_CAP + 1, res


def test_an_unreaped_child_on_a_high_fd_keeps_the_slot_without_a_second_child():
    res = _child("unreaped")
    assert (res["first"], res["second"]) == (False, False)
    assert res["children"] == 1 and res["killed"] == 1, "never a second child while the slot is held"
    assert (res["omitted_timeout"], res["omitted_unreaped"], res["inflight_now"]) == (1, 1, 1), res
    assert res["elapsed"] < 0.6 + 0.5, "bounded inside the budget + slack"


def test_a_status_provider_cannot_replace_the_computed_attestation(monkeypatch):
    from lib.runtime_bridge import RuntimeRPCServer
    computed = {"pid": 4242, "start_ticks": 777, "boot_id": "0b8a1f9e-2c3d-4e5f-8a9b-0c1d2e3f4a5b",
                "cgroup": "/user.slice/user-1000.slice/user@1000.service/app.slice/clarp-runtime.service",
                "invocation": "3932aee90792440bae6f9c8c5a14c973"}
    forged = dict(computed, pid=9999, invocation="f" * 32)
    monkeypatch.setattr(ra, "self_report", lambda: dict(computed))
    sock = pathlib.Path(tempfile.mkdtemp(prefix="rarc", dir="/var/tmp")) / "a.sock"
    server = RuntimeRPCServer(sock, dispatch_service=SimpleNamespace(ctx=SimpleNamespace(), backends=None),
                              release_id="r-A", status_provider=lambda: {"attestation": forged, "active": {}})
    try:
        status = server.dispatch_request("status", {})["result"]
    finally:
        server.server_close()
    assert status["attestation"] == computed and status["attestation"] != forged
    assert status["active"] == {}, "the provider's other fields still pass through"
    assert set(status) == {"protocol_version", "capabilities", "draining", "release_id", "instance", "drain",
                           "attestation", "active"}, "no other RPC shape change"
