"""Who serves delivery state: the runtime's self-report, bound by the manager ONCE per /log bracket.

A runtime process reports its own identity (A2) in its ``status``: pid, the kernel
start time of that pid (``/proc/self/stat`` field 22), boot id, its cgroup and the
``INVOCATION_ID`` systemd put in its environment. That is only a self-report of
cgroup MEMBERSHIP: children in the unit share the cgroup and the environment, and
any process can claim the same text. It never says "I am the unit's main".

For every /log delivery bracket the Host makes ONE fresh manager read (B):
``systemctl --user show -p MainPID -p InvocationID -p ControlGroup -p ActiveState
clarp-runtime.service``, at most 1 s and 4 KiB, never inside a database
transaction. Only when the manager's MainPID equals the reported pid does it read
``/proc/<MainPID>/stat``; the bracket is admitted only if pid, start time, boot,
invocation and cgroup all match. Nothing is cached or reused between brackets.

Manager probes are budgeted per Host process: 1 in flight, 2 starts per second
(burst 2), at most one per /log response. A probe that times out is killed and
waited for at most REAP_RESERVE_S; a child still not reaped keeps its slot, and
later brackets omit instead of starting another. Every refusal or failure omits
the delivery fields of that response (no wait, queue or retry). The gate only
withholds work; it never establishes exclusivity.

Counters here are process-local health (reset on restart), never an input to
admission, currentness or proof; reading them touches nothing but memory.
Supported topology only: one manager-owned runtime unit per data root."""
from __future__ import annotations

import json
import os
import re
import select
import subprocess
import sys
import threading
import time
from typing import Any

UNIT = "clarp-runtime.service"
SYSTEMCTL = "/usr/bin/systemctl"
SHOW_PROPS = ("MainPID", "InvocationID", "ControlGroup", "ActiveState")
SHOW_ARGV = (SYSTEMCTL, "--user", "show", *(x for p in SHOW_PROPS for x in ("-p", p)), UNIT)
SHOW_TIMEOUT_S = 1.0
REAP_RESERVE_S = 0.25
OUT_CAP = 4096
GATE_INFLIGHT, GATE_RATE_PER_S, GATE_BURST, PROBES_PER_LOG_RESPONSE = 1, 2.0, 2, 1
STATUS_MAX_BYTES = 8192
TYPE_NAMES_MAX = 16
_COUNTER_MAX = (1 << 53) - 1
OMIT_REASONS = (
    "no_attestation", "gate_inflight", "gate_rate", "unreaped", "in_transaction", "disconnected",
    "timeout", "nonzero", "exec_error", "bad_bytes", "inactive", "mainpid_mismatch", "start_mismatch",
    "invocation_mismatch", "cgroup_mismatch", "boot_mismatch", "non_systemd", "o2_mismatch", "unexpected")
_BOOT = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_INVOCATION = re.compile(r"[0-9a-f]{32}")
_TYPE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
_KEYS = frozenset({"pid", "start_ticks", "boot_id", "cgroup", "invocation"})

# Injection points (tests replace them); production values below.
CLOCK = time.monotonic
SPAWN = subprocess.Popen

_LOCK = threading.Lock()
# The one probe slot and the start-rate bucket of this Host process.
_GATE: dict[str, Any] = {"busy": False, "child": None, "tokens": float(GATE_BURST), "stamp": None}
# Process-local health counters (closed keys; see status_block).
_HEALTH: dict[str, Any] = {}


def reset_for_tests() -> None:
    with _LOCK:
        _GATE.clear()
        _GATE.update({"busy": False, "child": None, "tokens": float(GATE_BURST), "stamp": None})
        _HEALTH.clear()
        _HEALTH.update({"started": 0, "reaped": 0, "bound": 0, "last_duration_ms": None, "unexpected": 0,
                        "omitted": {reason: 0 for reason in OMIT_REASONS}, "unexpected_by_type": {},
                        "log_at": {}})


reset_for_tests()


def _bump(table: dict, key: str) -> None:
    table[key] = min(_COUNTER_MAX, int(table.get(key) or 0) + 1)


def note(reason: str) -> None:
    """Count one omission under a CLOSED reason."""
    with _LOCK:
        _bump(_HEALTH["omitted"], reason if reason in OMIT_REASONS else "unexpected")


LOG_COALESCE_S = 10.0


def type_label(exc: BaseException) -> str:
    """The exception's TYPE NAME if it is a plain identifier, else "_other"."""
    name = type(exc).__name__
    return name if _TYPE_NAME.fullmatch(name) else "_other"


def note_unexpected(exc: BaseException) -> bool:
    """An unexpected enrichment exception: counted EVERY time, by sanitized TYPE NAME only
    (at most TYPE_NAMES_MAX names, then "_other"). Returns whether to write a log line:
    at most one per type per LOG_COALESCE_S (the counters keep every occurrence)."""
    name = type_label(exc)
    with _LOCK:
        _HEALTH["unexpected"] = min(_COUNTER_MAX, _HEALTH["unexpected"] + 1)
        _bump(_HEALTH["omitted"], "unexpected")
        names = _HEALTH["unexpected_by_type"]
        if name not in names and len([n for n in names if n != "_other"]) >= TYPE_NAMES_MAX:
            name = "_other"
        _bump(names, name)
        now, last = CLOCK(), _HEALTH["log_at"].get(name)
        if last is not None and now - last < LOG_COALESCE_S:
            return False
        _HEALTH["log_at"][name] = now
        return True


# --- A2: the runtime's own self-report ---------------------------------------------------

def start_ticks_of(stat_text: str) -> int | None:
    """Field 22 (starttime) of a /proc/<pid>/stat text, split after the LAST ')'."""
    try:
        value = int(stat_text[stat_text.rindex(")") + 2:].split()[19])
    except (ValueError, IndexError, AttributeError):
        return None
    return value if value > 0 else None


def _read(path: str) -> str:
    with open(path) as f:
        return f.read()


def _valid(a2: Any) -> bool:
    return (isinstance(a2, dict) and set(a2) == _KEYS
            and isinstance(a2["pid"], int) and not isinstance(a2["pid"], bool) and a2["pid"] > 1
            and isinstance(a2["start_ticks"], int) and not isinstance(a2["start_ticks"], bool)
            and a2["start_ticks"] > 0
            and isinstance(a2["boot_id"], str) and bool(_BOOT.fullmatch(a2["boot_id"]))
            and isinstance(a2["cgroup"], str) and a2["cgroup"].startswith("/")
            and a2["cgroup"].endswith("/" + UNIT)
            and isinstance(a2["invocation"], str) and bool(_INVOCATION.fullmatch(a2["invocation"])))


def self_report(*, read=_read, environ: dict | None = None, pid: int | None = None) -> dict | None:
    """This process's A2 (membership only), or None when any part is missing or malformed."""
    env = os.environ if environ is None else environ
    try:
        lines = [x for x in read("/proc/self/cgroup").splitlines() if x.startswith("0::")]
        a2 = {"pid": os.getpid() if pid is None else int(pid),
              "start_ticks": start_ticks_of(read("/proc/self/stat")),
              "boot_id": read("/proc/sys/kernel/random/boot_id").strip(),
              "cgroup": lines[0][3:] if len(lines) == 1 else None,
              "invocation": env.get("INVOCATION_ID")}
    except (OSError, ValueError):
        return None
    return a2 if _valid(a2) else None


def attestation_of(observation: Any) -> dict | None:
    if not isinstance(observation, dict):
        return None
    a2 = observation.get("attestation")
    return dict(a2) if _valid(a2) else None


def attestation_key(observation: Any) -> tuple | None:
    a2 = attestation_of(observation)
    return None if a2 is None else tuple(a2[k] for k in sorted(_KEYS))


# --- B: one bounded manager read ----------------------------------------------------------

def read_boot_id() -> str | None:
    try:
        return _read("/proc/sys/kernel/random/boot_id").strip()
    except OSError:
        return None


def read_proc_start(pid: int) -> int | None:
    """Start ticks of /proc/<pid>/stat, or None when it is gone or unreadable."""
    try:
        return start_ticks_of(_read(f"/proc/{int(pid)}/stat"))
    except OSError:
        return None


BOOT_READER, PROC_READER = read_boot_id, read_proc_start


def run_probe() -> tuple[int | None, bytes, str, Any, bool]:
    """ONE bounded `systemctl show`: (rc, stdout, failure reason or "", unreaped child or None,
    whether a child was spawned).
    At most SHOW_TIMEOUT_S of reading and OUT_CAP bytes; on timeout / oversize the child is
    killed and waited for at most REAP_RESERVE_S; a child still alive is returned, never waited on."""
    if not sys.platform.startswith("linux") or not os.path.exists(SYSTEMCTL):
        return None, b"", "non_systemd", None, False
    try:
        child = SPAWN(list(SHOW_ARGV), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                      stderr=subprocess.DEVNULL, close_fds=True)
    except OSError:
        return None, b"", "exec_error", None, False
    deadline, out, reason = CLOCK() + SHOW_TIMEOUT_S, b"", ""
    try:
        fd = child.stdout.fileno()
        os.set_blocking(fd, False)
        while True:
            left = deadline - CLOCK()
            if left <= 0:
                reason = "timeout"
                break
            ready, _, _ = select.select([fd], [], [], left)
            if not ready:
                continue
            chunk = os.read(fd, OUT_CAP + 1 - len(out))
            if not chunk:
                break
            out += chunk
            if len(out) > OUT_CAP:
                reason = "bad_bytes"
                break
        if not reason:
            try:
                child.wait(timeout=max(0.0, deadline - CLOCK()))
            except subprocess.TimeoutExpired:
                reason = "timeout"
    except Exception:  # noqa: BLE001 - any read failure kills and reaps the child below
        reason = reason or "exec_error"
    finally:
        try:
            child.stdout.close()
        except OSError:
            pass
    if reason:
        child.kill()
        try:
            child.wait(timeout=REAP_RESERVE_S)
        except subprocess.TimeoutExpired:
            return None, out, reason, child, True
        return child.returncode, out, reason, None, True
    return child.returncode, out, "", None, True


def _parse_show(raw: bytes) -> dict | None:
    if not raw.endswith(b"\n"):
        return None
    try:
        lines = raw.decode("ascii").splitlines()
    except UnicodeDecodeError:
        return None
    props: dict[str, str] = {}
    for line in lines:
        key, sep, value = line.partition("=")
        if not sep or key not in SHOW_PROPS or key in props:
            return None
        props[key] = value
    return props if set(props) == set(SHOW_PROPS) else None


def bind(a2: dict, rc: int | None, raw: bytes) -> str:
    """"bound" or the closed reason why not. MainPID is compared BEFORE any /proc read."""
    if rc != 0:
        return "nonzero"
    props = _parse_show(raw)
    if props is None:
        return "bad_bytes"
    if props["ActiveState"] != "active":
        return "inactive"
    if props["MainPID"] != str(a2["pid"]):
        return "mainpid_mismatch"
    if props["InvocationID"] != a2["invocation"]:
        return "invocation_mismatch"
    if props["ControlGroup"] != a2["cgroup"]:
        return "cgroup_mismatch"
    if BOOT_READER() != a2["boot_id"]:
        return "boot_mismatch"
    if PROC_READER(int(props["MainPID"])) != a2["start_ticks"]:
        return "start_mismatch"                     # different, gone or unreadable: not confirmed
    return "bound"


# --- the gate and the bracket's admission ---------------------------------------------------

def _try_acquire() -> str:
    """"" when this bracket may start ONE probe, else the closed refusal reason. Never waits."""
    with _LOCK:
        held = _GATE["child"]
        if held is not None:
            if held.poll() is None:
                return "unreaped"
            _GATE["child"] = None
            _HEALTH["reaped"] = min(_COUNTER_MAX, _HEALTH["reaped"] + 1)
        if _GATE["busy"]:
            return "gate_inflight"
        now = CLOCK()
        if _GATE["stamp"] is not None:
            _GATE["tokens"] = min(float(GATE_BURST),
                                  _GATE["tokens"] + max(0.0, now - _GATE["stamp"]) * GATE_RATE_PER_S)
        _GATE["stamp"] = now
        if _GATE["tokens"] < 1.0:
            return "gate_rate"
        _GATE["tokens"] -= 1.0
        _GATE["busy"] = True
        _HEALTH["started"] = min(_COUNTER_MAX, _HEALTH["started"] + 1)
        return ""


def _release(unreaped, spawned: bool) -> None:
    with _LOCK:
        _GATE["busy"] = False
        if unreaped is not None:
            _GATE["child"] = unreaped               # the slot stays held until that child is reaped
        elif spawned:
            _HEALTH["reaped"] = min(_COUNTER_MAX, _HEALTH["reaped"] + 1)


def admit(observation: Any, *, in_transaction: bool, disconnected: bool = False) -> bool:
    """One /log bracket's B. True only when THIS fresh manager read binds the observation's A2.
    The caller passes whether a DB transaction is open and whether the client has gone; both
    refuse without a probe. Every refusal is counted under its closed reason."""
    a2 = attestation_of(observation)
    if a2 is None:
        note("no_attestation")
        return False
    if disconnected:
        note("disconnected")
        return False
    if in_transaction:
        note("in_transaction")
        return False
    refused = _try_acquire()
    if refused:
        note(refused)
        return False
    unreaped, spawned, started = None, False, CLOCK()
    try:
        rc, raw, reason, unreaped, spawned = run_probe()
        if not reason:
            reason = bind(a2, rc, raw)
    finally:
        _release(unreaped, spawned)
        with _LOCK:
            _HEALTH["last_duration_ms"] = max(0, min(_COUNTER_MAX, int((CLOCK() - started) * 1000)))
    if reason != "bound":
        note(reason)
        return False
    with _LOCK:
        _HEALTH["bound"] = min(_COUNTER_MAX, _HEALTH["bound"] + 1)
    return True


# --- the read-only health block (GET /status) -----------------------------------------------

def status_block() -> dict:
    """A snapshot of in-memory counters; no DB, probe, RPC, spawn, file or network access, and
    no waiting beyond copying under the counters lock. At most STATUS_MAX_BYTES serialized."""
    from . import launch_receipts
    with _LOCK:
        held = _GATE["child"] is not None
        block = {
            "scope": "process", "reset_on": "process_restart",
            "gate": {"inflight_limit": GATE_INFLIGHT, "rate_per_s": GATE_RATE_PER_S, "burst": GATE_BURST,
                     "probes_per_log_response": PROBES_PER_LOG_RESPONSE,
                     "inflight_now": 1 if (_GATE["busy"] or held) else 0,
                     "tokens_now": round(max(0.0, min(float(GATE_BURST), float(_GATE["tokens"]))), 3)},
            "probes": {"started": _HEALTH["started"], "reaped": _HEALTH["reaped"], "bound": _HEALTH["bound"],
                       "last_duration_ms": _HEALTH["last_duration_ms"]},
            "omitted": {reason: _HEALTH["omitted"].get(reason, 0) for reason in OMIT_REASONS},
            "unexpected_by_type": dict(_HEALTH["unexpected_by_type"]),
        }
    block["reconcile"] = launch_receipts.health_snapshot()
    size = len(json.dumps(block, separators=(",", ":")))
    if size > STATUS_MAX_BYTES:
        return {"overflow": True, "serialized_bytes": size, "limit": STATUS_MAX_BYTES, "scope": "process"}
    return block
