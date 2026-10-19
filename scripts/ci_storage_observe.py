"""DIAGNOSTIC ONLY (not part of any feature): run one command unchanged and record, alongside it, fixed numeric
metadata about the temp filesystem and the run's own pytest temp. The final LF source must decide explicitly, after
review, whether this stays; it must never ship by accident.

    python scripts/ci_storage_observe.py [--tmp /tmp] [--out test-results/storage-observe.jsonl]
        [--cadence 60] [--max 20] [--budget 5] [--grace 5] -- <command> [args...]

Runs inside scripts/ci_contained.sh (no network, fresh tmpfs /tmp, no privilege). Records, as JSON lines:
- one "before" sample, at most --max "during" samples (every --cadence seconds), and one "after" sample attempted
  once on a normal child exit (never after a signal);
- per sample: filesystem capacity/free/used bytes and inodes (os.statvfs), and the retained apparent/allocated bytes
  and file count under <tmp>/pytest-of-* (os.walk). The whole sample has ONE time budget (--budget seconds);
  any failed, partial or over-budget read gives null and qualified=false, never 0;
- a final "summary": actual start/end/duration, samples taken, whether the cap was reached, the OBSERVED sample maxima
  (not the true peak), the child's exit status or signal, the sampler status and the cleanup result.
No names, paths, content, headers or environment are written. The command's exit status is returned unchanged
(128+n when it died of signal n). TERM/INT/HUP are forwarded to the command's whole process group (the command
runs in its own session, so xdist workers are included); after --grace seconds the group is killed. Only that group
is ever signalled. Measurement happens in this process: there are no measurement child processes.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time

SCHEMA = 1


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
            raise SystemExit(f"unknown option {key}")
        opts[key] = value
    return (opts["--tmp"], opts["--out"], float(opts["--cadence"]), int(opts["--max"]), float(opts["--budget"]),
            float(opts["--grace"]), command)


class _OverBudget(Exception):
    pass


def sample(tmp: str, phase: str, budget: float) -> dict:
    """One bounded sample. Every field is a number or None; qualified is True only if every field was read."""
    started = time.monotonic()
    deadline = started + budget
    rec = {"phase": phase, "t": int(time.time()), "tmp_bytes": None, "tmp_avail_bytes": None, "tmp_used_bytes": None,
           "inodes": None, "inodes_avail": None, "inodes_used": None, "pytest_tmp_apparent_bytes": None,
           "pytest_tmp_allocated_bytes": None, "pytest_tmp_files": None, "fs_qualified": False,
           "pytest_qualified": False, "qualified": False}
    try:
        vfs = os.statvfs(tmp)
        rec.update(tmp_bytes=vfs.f_blocks * vfs.f_frsize, tmp_avail_bytes=vfs.f_bavail * vfs.f_frsize,
                   tmp_used_bytes=(vfs.f_blocks - vfs.f_bfree) * vfs.f_frsize, inodes=vfs.f_files,
                   inodes_avail=vfs.f_favail, inodes_used=vfs.f_files - vfs.f_ffree, fs_qualified=True)
    except OSError:
        pass
    try:
        apparent = allocated = files = 0
        roots = [os.path.join(tmp, n) for n in os.listdir(tmp) if n.startswith("pytest-of-")]

        def fail(err):
            raise err
        for root in roots:
            for top, _dirs, names in os.walk(root, onerror=fail):
                if time.monotonic() > deadline:
                    raise _OverBudget()
                for name in names:
                    st = os.lstat(os.path.join(top, name))
                    apparent += st.st_size
                    allocated += st.st_blocks * 512
                    files += 1
        if time.monotonic() > deadline:
            raise _OverBudget()
        rec.update(pytest_tmp_apparent_bytes=apparent, pytest_tmp_allocated_bytes=allocated, pytest_tmp_files=files,
                   pytest_qualified=True)
    except (OSError, _OverBudget):
        pass                       # partial or over budget: the pytest fields stay null, never 0
    rec["sample_ms"] = int((time.monotonic() - started) * 1000)
    rec["qualified"] = rec["fs_qualified"] and rec["pytest_qualified"]
    return rec


def main(argv) -> int:
    tmp, out, cadence, max_samples, budget, grace, command = _args(argv)
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    stream = open(out, "a", encoding="utf-8")

    def write(rec):
        try:
            stream.write(json.dumps({"schema": SCHEMA, **rec}, sort_keys=True) + "\n")
            stream.flush()
        except OSError:
            pass                       # diagnostics never change the command's outcome

    started_wall, started = int(time.time()), time.monotonic()
    maxima = {"tmp_used_bytes": None, "pytest_tmp_allocated_bytes": None}
    min_avail, qualified = None, 0

    def record(rec):
        nonlocal min_avail, qualified
        write(rec)
        if rec["qualified"]:
            qualified += 1
        for key, part in (("tmp_used_bytes", "fs_qualified"), ("pytest_tmp_allocated_bytes", "pytest_qualified")):
            if rec[part] and (maxima[key] is None or rec[key] > maxima[key]):
                maxima[key] = rec[key]
        if rec["fs_qualified"] and (min_avail is None or rec["tmp_avail_bytes"] < min_avail):
            min_avail = rec["tmp_avail_bytes"]

    signalled, owned = {"sig": None}, {"child": None}

    def forward(signum, _frame):
        signalled["sig"] = signum
        if owned["child"] is not None:
            try:
                os.killpg(owned["child"].pid, signum)
            except ProcessLookupError:
                pass

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):   # before the child exists: nothing is orphaned
        signal.signal(sig, forward)
    record(sample(tmp, "before", budget))
    child = owned["child"] = subprocess.Popen(command, start_new_session=True)
    if signalled["sig"] is not None:                             # a signal that arrived before the start
        try:
            os.killpg(child.pid, signalled["sig"])
        except ProcessLookupError:
            pass

    during, sampler = 0, "ok"
    rc = None
    next_sample = time.monotonic() + cadence
    while rc is None:
        if signalled["sig"] is not None:       # forwarded at once; wait at most the grace, never longer
            sampler = "stopped_by_signal"
            try:
                rc = child.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                rc = None
            break
        try:
            rc = child.wait(timeout=max(0.0, min(0.5, next_sample - time.monotonic())))
        except subprocess.TimeoutExpired:
            if time.monotonic() >= next_sample:
                if during < max_samples:
                    record(sample(tmp, "during", budget))
                    during += 1
                next_sample = time.monotonic() + cadence

    cleanup = "group_exited"
    if rc is None:                                     # still running after the grace: kill the owned group only
        try:
            os.killpg(child.pid, signal.SIGKILL)
            cleanup = "group_killed"
        except ProcessLookupError:
            cleanup = "group_exited"
        try:
            rc = child.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            cleanup, rc = "group_kill_unconfirmed", None
    else:
        try:                                           # descendants left in the owned group (e.g. stray workers)
            os.killpg(child.pid, 0)
            os.killpg(child.pid, signal.SIGKILL)
            cleanup = "group_remnant_killed"
        except ProcessLookupError:
            pass
        except PermissionError:
            cleanup = "group_check_failed"

    if signalled["sig"] is not None:
        sampler = "stopped_by_signal"
    if signalled["sig"] is None and rc is not None:
        record(sample(tmp, "after", budget))
        after = "taken"
    else:
        after = "skipped_signal"
    if rc is None:
        exit_status, child_signal = 128 + signal.SIGKILL, None
    elif rc < 0:
        exit_status, child_signal = 128 - rc, -rc
    else:
        exit_status, child_signal = rc, None
    write({"phase": "summary", "started_t": started_wall, "ended_t": int(time.time()),
           "duration_s": round(time.monotonic() - started, 3), "samples_during": during,
           "cap_reached": during >= max_samples, "after": after,
           "observed_sample_max_tmp_used_bytes": maxima["tmp_used_bytes"],
           "observed_sample_max_pytest_tmp_allocated_bytes": maxima["pytest_tmp_allocated_bytes"],
           "observed_sample_min_tmp_avail_bytes": min_avail,
           "qualified_samples": qualified, "child_exit": None if child_signal else rc, "child_signal": child_signal,
           "forwarded_signal": signalled["sig"], "sampler": sampler, "cleanup": cleanup})
    stream.close()
    return exit_status


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
