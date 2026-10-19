"""DIAGNOSTIC ONLY (not part of any feature): run one command unchanged and record, alongside it, fixed numeric
metadata about the temp filesystem and the run's own pytest temp. The final LF source must decide explicitly, after
review, whether this stays; it must never ship by accident.

    python scripts/ci_storage_observe.py [--tmp /tmp] [--out test-results/storage-observe.jsonl]
        [--cadence 60] [--max 20] [--budget 5] [--grace 5] -- <command> [args...]

Runs inside scripts/ci_contained.sh (no network, fresh tmpfs /tmp, no privilege).

Bounds. Every sample runs in its own short-lived measurement worker (this file with --measure-worker, in its own
process group). The observer never blocks on it: it polls in 0.2 s slices, and a worker that is not done within
--budget seconds, or any worker when a signal arrives, gets SIGKILL to its group and a bounded reap (<= 1 s). That
sample is then UNKNOWN (all fields null, qualified=false). A worker stuck in an uninterruptible call may outlive its
reap wait; that is recorded (worker_reaped=false), never hidden. If the worker's exit cannot be collected here
(wait4 ECHILD: reaped elsewhere), its exit, output and ownership are UNKNOWN (worker_reaped=null): it is neither
read nor signalled, since its pid may already name another process.

Timing (honest, not a guarantee). Noticing the command's exit can take one 0.2 s slice, plus <= 1 s to reap an
interrupted worker. One sample costs <= budget + 0.2 s + <= 1 s reap (<= 6.2 s at --budget 5). The "before" sample
runs before the command starts and the "after" sample after it ended: both are SERIAL overhead (<= 6.2 s each); only
the "during" samples overlap the command. These bounds hold on a normal, interruptible OS; the CI job timeout stays
the final bound.

Records (JSON lines, fixed keys; integers, booleans, null and fixed enum strings only; no paths, names, content,
headers, environment or exception text): one "before", at most --max "during" (every --cadence s), one "after"
(attempted once, only after a normal command exit), and a "summary" with the command's exit or signal, whether it
was started at all, the sampler, the cleanup of the command's own process group, the observed sample maxima (NOT
the true peak), the measurement workers' own CPU time and the bytes this diagnostic wrote.

Diagnostics never change the command: output open/write/close failures are best effort and only counted; the
command still runs and its exit status is returned unchanged (128+n when it died of signal n).

The command's status is read ONLY through this observer's own wait4 (never Popen.wait/poll, which turn ECHILD into
a fabricated 0). When it cannot be collected here (ECHILD, e.g. an inherited SIGCHLD=SIG_IGN let the kernel reap
it), the result is UNKNOWN: child_status "lost", child_exit null, exit STATUS_LOST_EXIT (never 0), no "after"
sample, cleanup "unconfirmed", and neither the pid nor its group is signalled again. An inherited SIGCHLD
disposition is left as it is.

A TERM/INT/HUP that
arrives before the command starts means the command is NOT started (exit 128+n). Signals are forwarded to the
command's own process group only while this observer still owns it.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time

SCHEMA = 4
SLICE_S = 0.2
REAP_S = 1.0
STATUS_LOST_EXIT = 125                  # the command's status could not be collected: UNKNOWN, never success
FIELDS = ("tmp_bytes", "tmp_avail_bytes", "tmp_used_bytes", "inodes", "inodes_avail", "inodes_used",
          "pytest_tmp_apparent_bytes", "pytest_tmp_allocated_bytes", "pytest_tmp_files")


def measure_worker(tmp: str) -> None:
    """One measurement; prints one JSON object. A failed part is null; the parent bounds the whole run."""
    fs = py = None
    try:
        v = os.statvfs(tmp)
        fs = {"tmp_bytes": v.f_blocks * v.f_frsize, "tmp_avail_bytes": v.f_bavail * v.f_frsize,
              "tmp_used_bytes": (v.f_blocks - v.f_bfree) * v.f_frsize, "inodes": v.f_files,
              "inodes_avail": v.f_favail, "inodes_used": v.f_files - v.f_ffree}
    except OSError:
        pass
    try:
        apparent = allocated = files = 0

        def fail(err):
            raise err
        for root in [os.path.join(tmp, n) for n in os.listdir(tmp) if n.startswith("pytest-of-")]:
            for top, _dirs, names in os.walk(root, onerror=fail):
                for name in names:
                    st = os.lstat(os.path.join(top, name))
                    apparent += st.st_size
                    allocated += st.st_blocks * 512
                    files += 1
        py = {"pytest_tmp_apparent_bytes": apparent, "pytest_tmp_allocated_bytes": allocated,
              "pytest_tmp_files": files}
    except OSError:
        pass
    sys.stdout.write(json.dumps({"fs": fs, "pytest": py}))


def _valid_part(part, keys) -> bool:
    return isinstance(part, dict) and set(part) == set(keys) and all(
        isinstance(part[k], int) and not isinstance(part[k], bool) and part[k] >= 0 for k in keys)


class Observer:
    def __init__(self, tmp, out, cadence, max_samples, budget, grace, command):
        self.tmp, self.out, self.cadence, self.max = tmp, out, cadence, max_samples
        self.budget, self.grace, self.command = budget, grace, command
        self.sig = None                 # the first forwarded signal
        self.child = None               # owned only while running; cleared once ownership ends
        self.owned = True               # False once the command's status was lost: nothing is signalled after
        self.status = None              # ("exit", n) | ("signal", n) | ("lost", None): own wait4 evidence only
        self.stream = None
        self.output = "ok"              # ok | open_failed | write_failed | close_failed
        self.write_errors = 0
        self.bytes_written = 0
        self.worker_cpu_ms = 0
        self.samples = {"qualified": 0, "unknown": 0, "timeout": 0, "interrupted": 0, "unreaped": 0,
                        "reap_unknown": 0, "kill_denied": 0}
        self.maxima = {"tmp_used_bytes": None, "pytest_tmp_allocated_bytes": None}
        self.min_avail = None

    # -- best-effort diagnostic output ------------------------------------------------------------
    def open(self):
        try:
            os.makedirs(os.path.dirname(self.out) or ".", exist_ok=True)
            self.stream = open(self.out, "a", encoding="utf-8")
        except OSError:
            self.output, self.stream = "open_failed", None
            self._note()

    def write(self, rec):
        if self.stream is None:
            return
        line = json.dumps({"schema": SCHEMA, **rec}, sort_keys=True) + "\n"
        try:
            self.stream.write(line)
            self.stream.flush()
            self.bytes_written += len(line.encode())
        except (OSError, ValueError):
            self.write_errors += 1
            if self.output == "ok":
                self.output = "write_failed"
                self._note()

    def close(self):
        if self.stream is None:
            return
        try:
            self.stream.close()
        except OSError:
            self.output = "close_failed"
            self._note()

    def _note(self):
        try:
            sys.stderr.write(f"ci_storage_observe: diagnostic output {self.output}\n")
        except Exception:  # noqa: BLE001 - nothing left to report through; the command is unaffected
            pass

    # -- signals ------------------------------------------------------------------------------------
    def on_signal(self, signum, _frame):
        if self.sig is None:
            self.sig = signum
        child = self.child
        if child is not None and self.owned:
            try:
                os.killpg(child.pid, signum)
            except (ProcessLookupError, PermissionError):
                pass

    # -- one bounded sample -----------------------------------------------------------------------
    def sample(self, phase):
        rec = {"phase": phase, "t": int(time.time()), "status": "unknown", "worker_reaped": True,
               "fs_qualified": False, "pytest_qualified": False, "qualified": False, **{k: None for k in FIELDS}}
        started = time.monotonic()
        try:
            worker = subprocess.Popen([sys.executable, os.path.abspath(__file__), "--measure-worker", self.tmp],
                                      stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError:
            rec["sample_ms"] = 0
            self._count(rec)
            return rec
        status, code, lost = None, None, False
        while status is None:                    # never blocks: 0.2 s slices, the signal flag and the budget win
            try:
                pid, st, usage = os.wait4(worker.pid, os.WNOHANG)
            except ChildProcessError:            # reaped elsewhere: exit, output and ownership are unknown
                status, lost = "unknown", True
                break
            if pid:
                code, status = os.waitstatus_to_exitcode(st), "done"
                self._cpu(usage)
            elif self.sig is not None or (self.child is not None and self.child_status() is not None):
                status = "interrupted"           # a signal or the command's exit is never delayed by a sample
            elif time.monotonic() - started > self.budget:
                status = "timeout"
            else:
                time.sleep(SLICE_S)
        out = b""
        if lost:
            rec["worker_reaped"] = None          # never read, never signalled: the pid may name another process
        elif status == "done":
            try:
                out = worker.stdout.read(65536)
            except OSError:
                out = b""
        else:                                    # kill the worker's own group, then a bounded reap
            try:                                 # still our unreaped child here, so its pid is still its own
                os.killpg(worker.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except PermissionError:
                self.samples["kill_denied"] += 1
            deadline, reaped = time.monotonic() + REAP_S, False
            while time.monotonic() < deadline:
                try:
                    pid, _st, usage = os.wait4(worker.pid, os.WNOHANG)
                except ChildProcessError:        # reaped elsewhere: whether it is gone is unknown here
                    reaped = None
                    break
                if pid:
                    reaped = True
                    self._cpu(usage)
                    break
                time.sleep(0.05)
            rec["worker_reaped"] = reaped
        worker.returncode = code if code is not None else -signal.SIGKILL   # never waited by Popen; not reported
        try:
            worker.stdout.close()
        except OSError:
            pass
        rec["sample_ms"] = int((time.monotonic() - started) * 1000)
        if status == "done" and code == 0:
            try:
                data = json.loads(out.decode())
            except (ValueError, UnicodeDecodeError):
                data = None
            if isinstance(data, dict):
                fs, py = data.get("fs"), data.get("pytest")
                if _valid_part(fs, FIELDS[:6]):
                    rec.update(fs, fs_qualified=True)
                if _valid_part(py, FIELDS[6:]):
                    rec.update(py, pytest_qualified=True)
            rec["qualified"] = rec["fs_qualified"] and rec["pytest_qualified"]
            rec["status"] = "qualified" if rec["qualified"] else "partial"
        else:
            rec["status"] = status if status in ("timeout", "interrupted") else "unknown"
        self._count(rec)
        return rec

    def child_status(self):
        """The command's status from this observer's own wait4 only; cached once observed. None while it runs."""
        if self.status is None:
            try:
                pid, st, _usage = os.wait4(self.child.pid, os.WNOHANG)
            except ChildProcessError:            # reaped elsewhere: exit and ownership are unknown
                pid, st, self.status = 0, 0, ("lost", None)
            if pid:
                if os.WIFEXITED(st):
                    self.status = ("exit", os.WEXITSTATUS(st))
                elif os.WIFSIGNALED(st):
                    self.status = ("signal", os.WTERMSIG(st))
                else:                            # not reported without WUNTRACED; never read as success
                    self.status = ("lost", None)
            if self.status is not None:
                self.child.returncode = -1       # never read; keeps Popen from ever waiting on this pid again
                if self.status[0] == "lost":
                    self.owned = False
        return self.status

    def _cpu(self, usage):
        if usage is not None:
            self.worker_cpu_ms += int((usage.ru_utime + usage.ru_stime) * 1000)

    def _count(self, rec):
        key = {"qualified": "qualified", "timeout": "timeout", "interrupted": "interrupted"}.get(rec["status"], "unknown")
        self.samples[key] += 1
        if rec["worker_reaped"] is False:
            self.samples["unreaped"] += 1
        elif rec["worker_reaped"] is None:
            self.samples["reap_unknown"] += 1
        if rec["fs_qualified"]:
            if self.maxima["tmp_used_bytes"] is None or rec["tmp_used_bytes"] > self.maxima["tmp_used_bytes"]:
                self.maxima["tmp_used_bytes"] = rec["tmp_used_bytes"]
            if self.min_avail is None or rec["tmp_avail_bytes"] < self.min_avail:
                self.min_avail = rec["tmp_avail_bytes"]
        if rec["pytest_qualified"] and (self.maxima["pytest_tmp_allocated_bytes"] is None
                                        or rec["pytest_tmp_allocated_bytes"] > self.maxima["pytest_tmp_allocated_bytes"]):
            self.maxima["pytest_tmp_allocated_bytes"] = rec["pytest_tmp_allocated_bytes"]
        self.write(rec)

    # -- the run --------------------------------------------------------------------------------------
    def run(self) -> int:
        started_wall, started = int(time.time()), time.monotonic()
        for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):    # before anything starts: nothing is orphaned
            signal.signal(s, self.on_signal)
        self.open()
        self.sample("before")
        child_started, rc, during, cleanup = False, None, 0, "not_started"
        if self.sig is None:
            try:
                self.child = subprocess.Popen(self.command, start_new_session=True)
                child_started = True
            except OSError:
                rc, cleanup = 127, "not_started"          # the command could not be executed (as a shell would)
        if child_started:
            if self.sig is not None:                      # arrived between the check and the start
                self.on_signal(self.sig, None)
            next_sample = time.monotonic() + self.cadence
            sig_seen_at = None
            while self.child_status() is None:
                if self.sig is not None:
                    sig_seen_at = sig_seen_at or time.monotonic()
                    if time.monotonic() - sig_seen_at > self.grace:
                        break
                elif time.monotonic() >= next_sample:
                    if during < self.max:
                        self.sample("during")
                        during += 1
                    next_sample = time.monotonic() + self.cadence
                    continue
                time.sleep(SLICE_S)
            pgid = self.child.pid
            status = self.child_status()
            if status is None:                            # still running after the grace: kill the owned group
                try:
                    os.killpg(pgid, signal.SIGKILL)
                    cleanup = "group_killed"
                except ProcessLookupError:
                    cleanup = "group_gone"
                except PermissionError:
                    cleanup = "group_check_failed"        # denied is not gone: the group's fate is unknown
                deadline = time.monotonic() + self.grace
                while self.child_status() is None and time.monotonic() < deadline:
                    time.sleep(0.05)
                status = self.child_status()
                if (status is None or status[0] == "lost") and cleanup != "group_check_failed":
                    cleanup = "group_kill_unconfirmed"
            elif status[0] == "lost":
                cleanup = "unconfirmed"                   # ownership lost: neither the pid nor the group is signalled
            else:
                cleanup = "group_exited"
                try:                                      # remnants left in the owned group (e.g. stray workers)
                    os.killpg(pgid, 0)
                    os.killpg(pgid, signal.SIGKILL)
                    cleanup = "group_remnant_killed"
                except ProcessLookupError:
                    pass
                except PermissionError:
                    cleanup = "group_check_failed"
            self.child = None                             # ownership ended: no further forwarding
        status = self.status
        if child_started and self.sig is None and status is not None and status[0] != "lost":
            self.sample("after")
            after = "taken"
        else:
            after = "skipped"
        if not child_started and self.sig is not None:
            exit_status, child_exit, child_signal, child_status = 128 + self.sig, None, None, "not_started"
        elif not child_started:
            exit_status, child_exit, child_signal, child_status = rc, rc, None, "not_started"
        elif status is None:
            exit_status, child_exit, child_signal, child_status = 128 + signal.SIGKILL, None, None, "unconfirmed"
        elif status[0] == "lost":
            exit_status, child_exit, child_signal, child_status = STATUS_LOST_EXIT, None, None, "lost"
        elif status[0] == "signal":
            exit_status, child_exit, child_signal, child_status = 128 + status[1], None, status[1], "signaled"
        else:
            exit_status, child_exit, child_signal, child_status = status[1], status[1], None, "exited"
        self.write({"phase": "summary", "started_t": started_wall, "ended_t": int(time.time()),
                    "duration_s": round(time.monotonic() - started, 3), "child_started": child_started,
                    "cancelled_before_start": (not child_started) and self.sig is not None,
                    "child_status": child_status, "child_exit": child_exit, "child_signal": child_signal,
                    "forwarded_signal": self.sig,
                    "exit_status": exit_status, "cleanup": cleanup, "after": after,
                    "samples_during": during, "cap_reached": during >= self.max,
                    "samples_qualified": self.samples["qualified"], "samples_unknown": self.samples["unknown"],
                    "samples_timeout": self.samples["timeout"], "samples_interrupted": self.samples["interrupted"],
                    "workers_unreaped": self.samples["unreaped"], "workers_reap_unknown": self.samples["reap_unknown"],
                    "worker_kill_denied": self.samples["kill_denied"],
                    "observed_sample_max_tmp_used_bytes": self.maxima["tmp_used_bytes"],
                    "observed_sample_max_pytest_tmp_allocated_bytes": self.maxima["pytest_tmp_allocated_bytes"],
                    "observed_sample_min_tmp_avail_bytes": self.min_avail,
                    "measurement_workers_cpu_ms": self.worker_cpu_ms,
                    "diag_bytes_written_before_summary": self.bytes_written, "diag_write_errors": self.write_errors,
                    "diag_output": self.output})
        self.close()
        return exit_status


def _args(argv):
    opts = {"--tmp": "/tmp", "--out": "test-results/storage-observe.jsonl", "--cadence": "60", "--max": "20",
            "--budget": "5", "--grace": "5"}
    if "--" not in argv:
        raise SystemExit("usage: ci_storage_observe.py [options] -- <command> [args...]")
    split = argv.index("--")
    head, command = argv[:split], argv[split + 1:]
    if not command or len(head) % 2:
        raise SystemExit("usage: ci_storage_observe.py [options] -- <command> [args...]")
    for key, value in zip(head[::2], head[1::2]):
        if key not in opts:
            raise SystemExit("usage: unknown option")
        opts[key] = value
    return (opts["--tmp"], opts["--out"], float(opts["--cadence"]), int(opts["--max"]), float(opts["--budget"]),
            float(opts["--grace"]), command)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--measure-worker":
        measure_worker(sys.argv[2])
        sys.exit(0)
    sys.exit(Observer(*_args(sys.argv[1:])).run())
