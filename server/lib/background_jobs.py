"""Durable, cancellable registry for detached/background work."""
from __future__ import annotations

import hashlib
import ctypes
import ctypes.util
import json
import os
import pathlib
import signal
import stat
import subprocess
import sys
import time
import uuid
from typing import Any, Callable

from . import agents, db


ACTIVE_STATUSES = frozenset({"queued", "running"})
TERMINAL_STATUSES = frozenset({"succeeded", "failed", "cancelled"})
DEFAULT_HEARTBEAT_TIMEOUT_MS = 600_000
TERMINAL_VISIBILITY_MS = 24 * 60 * 60 * 1000
WORKER_TERM_TIMEOUT_SEC = 2.0
WORKER_KILL_TIMEOUT_SEC = 2.0
PROGRESS_MAX = 500
LOG_TAIL_BYTES = 64 * 1024
TIMELINE_LIMIT = 200
# Besides the worker's own directory and its owner's cwd, logs may live here.
SHARED_LOG_ROOTS = ("/var/tmp",)


def active_by_agent() -> dict[str, list[dict[str, Any]]]:
    """Running or queued jobs per agent, oldest first, for the dashboard.

    One indexed read per snapshot. Stale jobs are reconciled by the watcher,
    so a crashed worker drops out within a heartbeat timeout.
    """
    marks = ",".join("?" for _ in ACTIVE_STATUSES)
    out: dict[str, list[dict[str, Any]]] = {}
    for row in db.conn().execute(
            f"SELECT job_id, agent_id, kind, title, detail, metadata_json "
            f"FROM background_jobs WHERE status IN ({marks}) "
            "AND COALESCE(agent_id, '') != '' "
            "ORDER BY started_at", tuple(sorted(ACTIVE_STATUSES))):
        try:
            metadata = json.loads(row["metadata_json"] or "{}")
        except json.JSONDecodeError:
            metadata = {}
        out.setdefault(row["agent_id"], []).append({
            "job_id": str(row["job_id"]),
            "kind": str(row["kind"] or ""), "title": str(row["title"] or ""),
            "detail": str(row["detail"] or ""),
            "metadata": metadata if isinstance(metadata, dict) else {},
        })
    return out


def mirrored_helper_session(job: dict, helper_sessions: set[str] | frozenset[str]) -> str:
    """The helper session a job only mirrors, or "" when it is a real process.

    `clarp-sub-agent --clarp-agent` registers a `sub-agent` job whose detail is
    the helper's session so the parent looked busy before helpers existed.
    The helper itself is the sub-agent; its watcher job is not a second one.
    `helper_sessions` are the sessions of this job owner's helper children.
    """
    if str(job.get("kind") or "") != "sub-agent":
        return ""
    metadata = job.get("metadata") if isinstance(job.get("metadata"), dict) else {}
    for candidate in (metadata.get("helper_session"), job.get("detail")):
        candidate = str(candidate or "").strip()
        if candidate and candidate in helper_sessions:
            return candidate
    return ""


def background_processes(jobs: list[dict], helper_sessions: set[str] | frozenset[str]) -> list[dict]:
    """Active jobs that are background processes, not helper mirrors."""
    return [job for job in jobs if not mirrored_helper_session(job, helper_sessions)]


def job_handle(job: dict) -> str:
    return f"bg1:{int(job.get('generation') or 1)}:{job['job_id']}"


def parse_job_handle(raw: str) -> tuple[str, int]:
    if raw.startswith("bg1:"):
        parts = raw.split(":", 2)
        if len(parts) == 3 and parts[1].isdigit() and parts[2]:
            return parts[2], int(parts[1])
    return raw, 1


def process_start_token(pid: int) -> str:
    """Stable process identity: boot session plus process start time."""
    if pid <= 0:
        return ""
    if sys.platform == "darwin":
        try:
            boot = subprocess.check_output(
                ["/usr/sbin/sysctl", "-n", "kern.boottime"], text=True,
                stderr=subprocess.DEVNULL).strip()
            started = subprocess.check_output(
                ["/bin/ps", "-p", str(pid), "-o", "lstart="], text=True,
                stderr=subprocess.DEVNULL).strip()
            return hashlib.sha256(
                f"{boot}\0{pid}\0{started}".encode()).hexdigest()
        except (OSError, subprocess.SubprocessError):
            return ""
    try:
        boot_id = pathlib.Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        raw = pathlib.Path(f"/proc/{pid}/stat").read_text()
        tail = raw.rsplit(")", 1)[1].split()
        return f"{boot_id}:{tail[19]}"
    except (OSError, IndexError):
        return ""


def current_worker_identity() -> tuple[int, str]:
    explicit = os.environ.get("CLARP_BACKGROUND_WORKER_PID", "").strip()
    pid = int(explicit) if explicit.isdigit() else os.getppid()
    command = process_command(pid)
    # Existing message-watch is the first adopted detached workflow. Other
    # callers use explicit heartbeats unless they opt in with the env override.
    if not explicit and "watch_messages.py" not in command:
        return 0, ""
    return pid, process_start_token(pid)


def process_command(pid: int) -> str:
    return " ".join(process_argv(pid))


def process_argv(pid: int) -> list[str]:
    if sys.platform == "darwin":
        try:
            return _parse_macos_procargs(_macos_procargs(pid))
        except (OSError, ValueError):
            return []
    try:
        return [
            part.decode(errors="replace")
            for part in pathlib.Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
            if part
        ]
    except OSError:
        return []


def _parse_macos_procargs(raw: bytes) -> list[str]:
    """Parse the kern.procargs2 binary payload without losing spaces."""
    if len(raw) < 5:
        return []
    argc = int.from_bytes(raw[:4], byteorder=sys.byteorder, signed=True)
    if argc <= 0 or argc > 100_000:
        return []
    offset = raw.find(b"\0", 4)
    if offset < 0:
        return []
    offset += 1
    while offset < len(raw) and raw[offset] == 0:
        offset += 1
    argv = []
    for _ in range(argc):
        end = raw.find(b"\0", offset)
        if end < 0:
            return []
        argv.append(raw[offset:end].decode(errors="replace"))
        offset = end + 1
    return argv


def _macos_procargs(pid: int) -> bytes:
    """Read another process's argv through the Darwin KERN_PROCARGS2 MIB."""
    if pid <= 0:
        return b""
    library = ctypes.util.find_library("c")
    if not library:
        raise OSError("Darwin libc not found")
    libc = ctypes.CDLL(library, use_errno=True)
    mib = (ctypes.c_int * 3)(1, 49, pid)  # CTL_KERN, KERN_PROCARGS2, pid
    size = ctypes.c_size_t()
    if libc.sysctl(mib, 3, None, ctypes.byref(size), None, 0) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    buffer = ctypes.create_string_buffer(size.value)
    if libc.sysctl(mib, 3, buffer, ctypes.byref(size), None, 0) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    return buffer.raw[:size.value]


def adopted_worker_job_ids(pid: int, session: str) -> list[str]:
    """Exact message-watch targets encoded in the adopted worker argv."""
    argv = process_argv(pid)
    script_index = next(
        (index for index, arg in enumerate(argv) if "watch_messages.py" in arg),
        -1,
    )
    if script_index < 0 or script_index + 1 >= len(argv):
        return []
    subcommand = argv[script_index + 1]
    provider = {"whatsapp": "whatsapp", "himalaya": "email"}.get(
        subcommand, "")
    if not provider:
        return []
    labels: set[str] = set()
    mapping_flags = {"--watch", "--watch-name", "--from"}
    for index, arg in enumerate(argv):
        if arg in mapping_flags:
            if index + 1 < len(argv):
                raw = argv[index + 1]
                labels.add((raw.split("=", 1)[1] if "=" in raw else raw).strip())
        elif any(arg.startswith(f"{flag}=") for flag in mapping_flags):
            raw = arg.split("=", 1)[1]
            labels.add((raw.split("=", 1)[1] if "=" in raw else raw).strip())
        elif arg == "--subject-keyword" and index + 1 < len(argv):
            labels.add(argv[index + 1].strip())
        elif arg.startswith("--subject-keyword="):
            labels.add(arg.split("=", 1)[1].strip())
        if arg == "--reply-watch-json" and index + 1 < len(argv):
            try:
                data = json.loads(pathlib.Path(argv[index + 1]).read_text())
                if isinstance(data, dict):
                    labels.update(str(value).strip() for value in data.values())
            except (OSError, json.JSONDecodeError):
                pass
        elif arg.startswith("--reply-watch-json="):
            try:
                data = json.loads(pathlib.Path(arg.split("=", 1)[1]).read_text())
                if isinstance(data, dict):
                    labels.update(str(value).strip() for value in data.values())
            except (OSError, json.JSONDecodeError):
                pass
    result = []
    for label in sorted(label for label in labels if label):
        digest = hashlib.sha256(
            f"{session}\0{provider}\0{label}".encode()).hexdigest()[:16]
        result.append(f"message-watch-{provider}-{digest}")
    return result


def worker_is_alive(pid: int, expected_token: str) -> bool:
    if not expected_token or process_start_token(pid) != expected_token:
        return False
    try:
        if sys.platform == "darwin":
            state = subprocess.check_output(
                ["/bin/ps", "-p", str(pid), "-o", "stat="], text=True,
                stderr=subprocess.DEVNULL).strip()
        else:
            raw = pathlib.Path(f"/proc/{pid}/stat").read_text()
            state = raw.rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError, subprocess.SubprocessError):
        return False
    return bool(state) and state[0] not in {"Z", "X"}


def terminate_worker(
    pid: int, expected_token: str, *,
    term_timeout: float = WORKER_TERM_TIMEOUT_SEC,
    kill_timeout: float = WORKER_KILL_TIMEOUT_SEC,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """Stop one exact PID/start-token identity, escalating only while it matches."""
    if not worker_is_alive(pid, expected_token):
        return True

    def wait_until_gone(timeout: float) -> bool:
        deadline = monotonic() + max(0.0, timeout)
        while worker_is_alive(pid, expected_token):
            if monotonic() >= deadline:
                return False
            sleep(0.05)
        return True

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    if wait_until_gone(term_timeout):
        return True
    if not worker_is_alive(pid, expected_token):
        return True
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    return wait_until_gone(kill_timeout)


def upsert(
    *, session: str, job_id: str = "", kind: str = "other",
    title: str, detail: str = "", metadata: dict[str, Any] | None = None,
    status: str = "running", heartbeat_timeout_ms: int = DEFAULT_HEARTBEAT_TIMEOUT_MS,
    worker_pid: int | None = None, worker_start_token: str = "",
    restart_cancelled: bool = False,
) -> dict:
    agent = agents.get_by_session(session)
    if not agent:
        raise ValueError(f"unknown session: {session}")
    from . import identity
    return _upsert_owned(
        owner_kind="agent", agent_id=agent["agent_id"], session=session,
        computer_id="", job_id=job_id, kind=kind, title=title, detail=detail,
        metadata=metadata, status=status,
        heartbeat_timeout_ms=heartbeat_timeout_ms, worker_pid=worker_pid,
        worker_start_token=worker_start_token,
        restart_cancelled=restart_cancelled,
        started_trace_id=identity.open_trace_id(agent),
    )


def upsert_computer(
    *, computer_id: str, job_id: str = "", kind: str = "other",
    title: str, detail: str = "", metadata: dict[str, Any] | None = None,
    status: str = "running", heartbeat_timeout_ms: int = DEFAULT_HEARTBEAT_TIMEOUT_MS,
    worker_pid: int | None = None, worker_start_token: str = "",
    restart_cancelled: bool = False,
) -> dict:
    computer_id = computer_id.strip()
    if not computer_id:
        raise ValueError("computer-owned background job requires computer_id")
    return _upsert_owned(
        owner_kind="computer", agent_id="", session="", computer_id=computer_id,
        job_id=job_id, kind=kind, title=title, detail=detail, metadata=metadata,
        status=status, heartbeat_timeout_ms=heartbeat_timeout_ms,
        worker_pid=worker_pid, worker_start_token=worker_start_token,
        restart_cancelled=restart_cancelled,
    )


def _upsert_owned(
    *, owner_kind: str, agent_id: str, session: str, computer_id: str,
    job_id: str, kind: str, title: str, detail: str,
    metadata: dict[str, Any] | None, status: str, heartbeat_timeout_ms: int,
    worker_pid: int | None, worker_start_token: str,
    restart_cancelled: bool, started_trace_id: str = "",
) -> dict:
    if status not in ACTIVE_STATUSES:
        raise ValueError(f"background job must start queued or running: {status}")
    now = db.now_ms()
    job_id = job_id.strip() or uuid.uuid4().hex
    timeout = max(30_000, min(int(heartbeat_timeout_ms), 24 * 60 * 60 * 1000))
    pid = int(worker_pid or 0) or None
    token = worker_start_token.strip()
    c = db.conn()
    c.execute("BEGIN IMMEDIATE")
    try:
        existing = c.execute(
            "SELECT * FROM background_jobs WHERE job_id=?", (job_id,)
        ).fetchone()
        if existing and (
            str(existing["owner_kind"] or "agent") != owner_kind
            or str(existing["agent_id"] or "") != agent_id
            or str(existing["session"] or "") != session
            or str(existing["computer_id"] or "") != computer_id
        ):
            label = (
                "agent" if str(existing["owner_kind"] or "agent") == "agent"
                and owner_kind == "agent" else "owner"
            )
            raise ValueError(f"job id already belongs to another {label}: {job_id}")
        if existing and existing["status"] == "cancelled" and not restart_cancelled:
            c.execute("COMMIT")
            result = get(job_id, reconcile=False)
            assert result is not None
            return result
        old_token = str(existing["worker_start_token"] or "") if existing else ""
        worker_changed = bool(existing and token and old_token and token != old_token)
        new_generation = bool(
            existing and (
                existing["status"] in TERMINAL_STATUSES or worker_changed
            )
        )
        generation = (
            int(existing["generation"] or 1) + 1 if new_generation
            else int(existing["generation"] or 1) if existing else 1
        )
        next_status = status if new_generation or not existing else existing["status"]
        next_started_at = now if new_generation or not existing else existing["started_at"]
        if new_generation or not existing:
            next_pid = pid
            next_token = token
            next_heartbeat_source = "worker_registration"
            # A new run starts a clean inspection record: its own trace, and
            # no progress line or log left over from the previous generation.
            next_trace = started_trace_id
            next_progress, next_progress_at = "", None
            next_log, next_cwd = "", ""
        else:
            next_trace = str(existing["started_trace_id"] or "")
            next_progress = str(existing["progress_text"] or "")
            next_progress_at = existing["progress_at"]
            next_log = str(existing["log_path"] or "")
            next_cwd = str(existing["worker_cwd"] or "")
            next_pid = pid if token else existing["worker_pid"]
            next_token = token or old_token
            # Idempotent callers must not erase a launch claim or a worker's
            # ownership source for the same active generation.
            next_heartbeat_source = str(
                existing["heartbeat_source"] or "worker_registration")
        c.execute(
            """INSERT INTO background_jobs
               (job_id, agent_id, session, owner_kind, computer_id,
                kind, title, detail, status,
                started_at, updated_at, metadata_json, heartbeat_at,
                heartbeat_timeout_ms, heartbeat_source, worker_pid,
                worker_start_token, terminal_at, terminal_reason, revision,
                generation, started_trace_id, progress_text, progress_at,
                log_path, worker_cwd)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, '', 0, ?,
                       ?, ?, ?, ?, ?)
               ON CONFLICT(job_id) DO UPDATE SET
                 owner_kind=excluded.owner_kind,
                 computer_id=excluded.computer_id,
                 kind=excluded.kind, title=excluded.title, detail=excluded.detail,
                 status=excluded.status,
                 started_at=excluded.started_at,
                 updated_at=excluded.updated_at,
                 heartbeat_at=excluded.heartbeat_at,
                 heartbeat_timeout_ms=excluded.heartbeat_timeout_ms,
                 heartbeat_source=excluded.heartbeat_source,
                 worker_pid=excluded.worker_pid,
                 worker_start_token=excluded.worker_start_token,
                 terminal_at=NULL, terminal_reason='', cancelled_at=NULL,
                 metadata_json=excluded.metadata_json,
                 generation=excluded.generation,
                 started_trace_id=excluded.started_trace_id,
                 progress_text=excluded.progress_text,
                 progress_at=excluded.progress_at,
                 log_path=excluded.log_path,
                 worker_cwd=excluded.worker_cwd""",
            (
                job_id, agent_id, session, owner_kind, computer_id or None,
                kind[:40], title[:120], detail[:1000], next_status,
                next_started_at, now,
                json.dumps(metadata or {}, separators=(",", ":")), now, timeout,
                next_heartbeat_source, next_pid, next_token, generation,
                next_trace[:200], next_progress, next_progress_at,
                next_log, next_cwd,
            ),
        )
        _record_event(c, job_id, now)
        c.execute("COMMIT")
    except BaseException:
        c.execute("ROLLBACK")
        raise
    result = get(job_id, reconcile=False)
    assert result is not None
    return result


def get(
    job_id: str, *, reconcile: bool = True, observed_at: int | None = None,
) -> dict | None:
    if reconcile:
        reconcile_stale(job_id=job_id)
    row = db.conn().execute(
        """SELECT j.*, a.persona FROM background_jobs j
           LEFT JOIN agents a ON a.agent_id=j.agent_id
           WHERE j.job_id=?""",
        (job_id,),
    ).fetchone()
    return _public(row, observed_at=observed_at) if row else None


def claim_queued_launch(job_id: str, *, generation: int) -> bool:
    """Let exactly one request launch a queued generation's detached worker."""
    changed = db.conn().execute(
        """UPDATE background_jobs SET heartbeat_source='launch_claimed'
             WHERE job_id=? AND generation=? AND status='queued'
               AND worker_pid IS NULL
               AND heartbeat_source='worker_registration'""",
        (job_id, int(generation)),
    ).rowcount
    return changed == 1


def snapshot(*, include_terminal: bool = True, now_ms: int | None = None) -> dict:
    now = int(now_ms if now_ms is not None else db.now_ms())
    reconcile_stale(now_ms=now)
    c = db.conn()
    c.execute("BEGIN")
    try:
        active_rows = c.execute(
            """SELECT j.*, a.persona FROM background_jobs j
                  LEFT JOIN agents a ON a.agent_id=j.agent_id
                 WHERE j.status IN ('queued','running')
                 ORDER BY j.updated_at DESC""",
        ).fetchall()
        terminal_rows = []
        if include_terminal:
            terminal_rows = c.execute(
                """SELECT j.*, a.persona FROM background_jobs j
                      LEFT JOIN agents a ON a.agent_id=j.agent_id
                     WHERE j.status IN ('succeeded','failed','cancelled')
                       AND j.updated_at>=?
                     ORDER BY j.updated_at DESC LIMIT 100""",
                (now - TERMINAL_VISIBILITY_MS,),
            ).fetchall()
        revision_row = c.execute(
            "SELECT COALESCE(MAX(event_id), 0) AS event_id FROM background_job_events"
        ).fetchone()
        revision = int(revision_row["event_id"] if revision_row else 0)
        c.execute("COMMIT")
    except BaseException:
        c.execute("ROLLBACK")
        raise
    return {
        "jobs": [
            _public(row, observed_at=now)
            for row in [*active_rows, *terminal_rows]
        ],
        "snapshot_revision": revision,
        "observed_at": now,
    }


def list_active() -> list[dict]:
    return snapshot(include_terminal=False)["jobs"]


def restart(
    *, session: str, job_id: str, kind: str, title: str, detail: str = "",
    metadata: dict[str, Any] | None = None, worker_pid: int | None = None,
    worker_start_token: str = "",
) -> dict:
    return upsert(
        session=session, job_id=job_id, kind=kind, title=title, detail=detail,
        metadata=metadata, status="running", worker_pid=worker_pid,
        worker_start_token=worker_start_token, restart_cancelled=True)


def cancel(job_id: str) -> dict | None:
    return cancel_with_result(job_id)[0]


def cancel_with_result(
    job_id: str, *, reason: str = "user_cancelled",
) -> tuple[dict | None, bool]:
    now = db.now_ms()
    c = db.conn()
    c.execute("BEGIN IMMEDIATE")
    try:
        row = c.execute(
            "SELECT status FROM background_jobs WHERE job_id=?", (job_id,)
        ).fetchone()
        if row is None:
            c.execute("COMMIT")
            return None, False
        if row["status"] in TERMINAL_STATUSES:
            c.execute("COMMIT")
            return get(job_id, reconcile=False), False
        c.execute(
            """UPDATE background_jobs
                  SET status='cancelled', cancelled_at=?, terminal_at=?,
                      terminal_reason=?, updated_at=?
                WHERE job_id=? AND status IN ('queued','running')""",
            (now, now, reason[:1000], now, job_id),
        )
        _record_event(c, job_id, now)
        c.execute("COMMIT")
    except BaseException:
        c.execute("ROLLBACK")
        raise
    return get(job_id, reconcile=False), True


def start(
    job_id: str, *, generation: int | None = None,
    worker_pid: int | None = None, worker_start_token: str = "",
) -> dict | None:
    return _active_update(
        job_id, status="running", source="worker_start",
        generation=generation, worker_pid=worker_pid,
        worker_start_token=worker_start_token)


def heartbeat(
    job_id: str, *, source: str = "worker_heartbeat",
    worker_pid: int | None = None, worker_start_token: str = "",
    generation: int | None = None,
) -> dict | None:
    return _active_update(
        job_id, source=source, worker_pid=worker_pid,
        worker_start_token=worker_start_token, generation=generation)


def heartbeat_jobs(
    job_ids: list[str], *, worker_pid: int, worker_start_token: str,
) -> list[str]:
    if not worker_pid or not worker_start_token:
        return []
    updated: list[str] = []
    for job_id in dict.fromkeys(job_ids):
        job = _active_update(
            job_id, source="worker_status",
            worker_pid=worker_pid, worker_start_token=worker_start_token)
        if job and job["status"] in ACTIVE_STATUSES:
            updated.append(job_id)
    return updated


def finish(
    job_id: str, *, generation: int, status: str = "succeeded", reason: str = "",
    worker_pid: int | None = None, worker_start_token: str = "",
) -> dict | None:
    if status not in {"succeeded", "failed"}:
        raise ValueError(f"invalid terminal background-job status: {status}")
    now = db.now_ms()
    c = db.conn()
    c.execute("BEGIN IMMEDIATE")
    try:
        row = c.execute(
            "SELECT * FROM background_jobs WHERE job_id=?", (job_id,)
        ).fetchone()
        if row is None:
            c.execute("COMMIT")
            return None
        expected_pid = int(row["worker_pid"] or 0)
        expected_token = str(row["worker_start_token"] or "")
        if int(row["generation"] or 1) != int(generation):
            c.execute("COMMIT")
            return None
        if expected_pid and (not worker_pid or int(worker_pid) != expected_pid):
            c.execute("COMMIT")
            return None
        if expected_token and worker_start_token != expected_token:
            c.execute("COMMIT")
            return None
        changed = c.execute(
            """UPDATE background_jobs
                  SET status=?, terminal_at=?, terminal_reason=?, updated_at=?
                WHERE job_id=? AND status IN ('queued','running')""",
            (status, now, reason[:1000], now, job_id),
        ).rowcount
        if changed:
            _record_event(c, job_id, now)
        c.execute("COMMIT")
    except BaseException:
        c.execute("ROLLBACK")
        raise
    return get(job_id, reconcile=False)


def _owned_active_row(c, job_id: str, *, session: str, generation: int):
    """The job row when `session` owns this active generation, else None."""
    row = c.execute(
        "SELECT * FROM background_jobs WHERE job_id=?", (job_id,)).fetchone()
    if (row is None or str(row["owner_kind"] or "agent") != "agent"
            or str(row["session"] or "") != session
            or int(row["generation"] or 1) != int(generation)
            or row["status"] not in ACTIVE_STATUSES):
        return None
    return row


def cancel_owned(job_id: str, *, session: str, generation: int) -> dict | None:
    """Let the owning agent session close its own job.

    Finish and fail stay fenced to the registered worker PID so one process
    cannot report another's outcome. Cancelling is different: the owner
    must be able to close a job whose worker is gone or unreachable, and a
    cancel reports no outcome. Returns None when `session` does not own
    this active generation.
    """
    c = db.conn()
    c.execute("BEGIN IMMEDIATE")
    try:
        owned = _owned_active_row(c, job_id, session=session, generation=generation)
        c.execute("COMMIT")
    except BaseException:
        c.execute("ROLLBACK")
        raise
    if owned is None:
        return None
    job, _changed = cancel_with_result(job_id, reason="owner_cancelled")
    return job if job and int(job.get("generation") or 1) == int(generation) else None


def set_progress(
    job_id: str, *, session: str, generation: int, text: str,
) -> dict | None:
    """Store the worker's latest progress line and publish it."""
    text = " ".join(str(text or "").split())[:PROGRESS_MAX]
    now = db.now_ms()
    c = db.conn()
    c.execute("BEGIN IMMEDIATE")
    try:
        if _owned_active_row(c, job_id, session=session, generation=generation) is None:
            c.execute("COMMIT")
            return None
        c.execute(
            """UPDATE background_jobs SET progress_text=?, progress_at=?, updated_at=?
                WHERE job_id=?""",
            (text, now, now, job_id))
        _record_event(c, job_id, now, note=text)
        c.execute("COMMIT")
    except BaseException:
        c.execute("ROLLBACK")
        raise
    return get(job_id, reconcile=False)


def set_log(
    job_id: str, *, session: str, generation: int, path: str, worker_cwd: str = "",
) -> dict | None:
    """Register the log file a worker writes. The path must be absolute."""
    path = str(path or "").strip()
    if not os.path.isabs(path):
        raise ValueError(f"log path must be absolute: {path!r}")
    worker_cwd = str(worker_cwd or "").strip()
    if worker_cwd and not os.path.isabs(worker_cwd):
        worker_cwd = ""
    c = db.conn()
    c.execute("BEGIN IMMEDIATE")
    try:
        if _owned_active_row(c, job_id, session=session, generation=generation) is None:
            c.execute("COMMIT")
            return None
        c.execute(
            "UPDATE background_jobs SET log_path=?, worker_cwd=? WHERE job_id=?",
            (path[:4096], worker_cwd[:4096], job_id))
        c.execute("COMMIT")
    except BaseException:
        c.execute("ROLLBACK")
        raise
    return get(job_id, reconcile=False)


def timeline(job_id: str, *, limit: int = TIMELINE_LIMIT) -> list[dict]:
    """The job's change feed, oldest first: status changes and progress."""
    rows = db.conn().execute(
        """SELECT event_id, observed_at, status, note FROM background_job_events
            WHERE job_id=? ORDER BY event_id DESC LIMIT ?""",
        (job_id, max(1, min(int(limit), 1000))),
    ).fetchall()
    out: list[dict] = []
    previous = ""
    for row in reversed(rows):
        status = str(row["status"] or "")
        note = str(row["note"] or "")
        if status and status != previous:
            change = "status"
        elif note:
            change = "progress"
        else:
            change = "update"
        out.append({
            "event_id": int(row["event_id"]), "observed_at": int(row["observed_at"]),
            "status": status, "note": note, "change": change,
        })
        previous = status or previous
    return out


def _inside(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


def log_roots(job: dict, *, owner_cwd: str = "") -> list[str]:
    """Directories a job's log may resolve into, symlinks resolved."""
    roots = []
    for raw in (job.get("worker_cwd"), owner_cwd, *SHARED_LOG_ROOTS):
        raw = str(raw or "").strip()
        if not raw or not os.path.isabs(raw):
            continue
        real = os.path.realpath(raw)
        # A worker started in / (or an agent whose cwd is /) must not turn
        # the whole filesystem into a readable log directory.
        if real != "/" and real not in roots:
            roots.append(real)
    return roots


def read_log_tail(
    path: str, roots: list[str], *, limit: int = LOG_TAIL_BYTES,
) -> dict:
    """The last `limit` bytes of a registered log, or why it is withheld.

    Only a regular file whose fully resolved path lies under one of `roots`
    is read, so a symlink anywhere in the path cannot point the Host at a
    file outside them. The final component is opened without following a
    symlink, so swapping one in after the check is refused too.
    """
    out: dict[str, Any] = {"path": path, "available": False, "reason": "",
                           "text": "", "size": 0, "truncated": False}
    if not path:
        out["reason"] = "no_log"
        return out
    if not os.path.isabs(path):
        out["reason"] = "not_absolute"
        return out
    real = os.path.realpath(path)
    if not any(_inside(real, root) for root in roots):
        out["reason"] = "outside_allowed_roots"
        return out
    try:
        fd = os.open(real, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                     | getattr(os, "O_NONBLOCK", 0))
    except FileNotFoundError:
        out["reason"] = "missing"
        return out
    except OSError:
        out["reason"] = "unreadable"
        return out
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            out["reason"] = "not_a_regular_file"
            return out
        size = int(info.st_size)
        start = max(0, size - max(1, int(limit)))
        os.lseek(fd, start, os.SEEK_SET)
        chunks, remaining = [], size - start
        while remaining > 0:
            chunk = os.read(fd, min(remaining, 65536))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    finally:
        os.close(fd)
    data = b"".join(chunks)
    if start > 0:
        # Drop the partial first line so the tail starts on a line boundary.
        newline = data.find(b"\n")
        if 0 <= newline < len(data) - 1:
            data = data[newline + 1:]
    out.update(available=True, text=data.decode("utf-8", errors="replace"),
               size=size, truncated=start > 0)
    return out


def detail(job_id: str, *, include_log: bool = True) -> dict | None:
    """One background process with its timeline, progress, log and owners."""
    job = get(job_id)
    if job is None:
        return None
    owner = agents.get_by_agent_id(job["agent_id"]) if job.get("agent_id") else None
    helper_sessions = frozenset(
        str(child["session"]) for child in agents.children(job["agent_id"])
        if str(child.get("role") or "") == "helper"
    ) if owner else frozenset()
    helper = mirrored_helper_session(job, helper_sessions)
    log = read_log_tail(
        str(job.get("log_path") or ""),
        log_roots(job, owner_cwd=str(owner.get("cwd") or "") if owner else ""),
    ) if include_log else {
        "path": "", "available": False, "reason": "forbidden", "text": "",
        "size": 0, "truncated": False,
    }
    return {
        "job": job,
        "handle": job_handle(job),
        "is_process": not helper,
        "owner_session": str(job.get("session") or ""),
        "owner_agent_id": str(job.get("agent_id") or ""),
        "started_trace_id": str(job.get("started_trace_id") or ""),
        "helper_session": helper,
        "progress": {"text": str(job.get("progress_text") or ""),
                     "at": job.get("progress_at")},
        "timeline": timeline(job_id),
        "log": log,
    }


def is_active(job_id: str, *, generation: int | None = None) -> bool:
    reconcile_stale(job_id=job_id)
    row = db.conn().execute(
        "SELECT status,generation FROM background_jobs WHERE job_id=?", (job_id,)
    ).fetchone()
    return bool(
        row and row["status"] in ACTIVE_STATUSES
        and (generation is None or int(row["generation"] or 1) == generation)
    )


def reassign_terminal_owner(job_id: str, *, session: str) -> dict | None:
    """Move a logical terminal singleton to a new live owner before restart."""
    agent = agents.get_by_session(session)
    if not agent:
        raise ValueError(f"unknown session: {session}")
    now = db.now_ms()
    c = db.conn()
    c.execute("BEGIN IMMEDIATE")
    try:
        changed = c.execute(
            """UPDATE background_jobs
                  SET agent_id=?,session=?,owner_kind='agent',computer_id=NULL,
                      updated_at=?
                WHERE job_id=? AND status IN ('succeeded','failed','cancelled')""",
            (agent["agent_id"], session, now, job_id),
        ).rowcount
        if changed:
            _record_event(c, job_id, now)
        c.execute("COMMIT")
    except BaseException:
        c.execute("ROLLBACK")
        raise
    return get(job_id, reconcile=False) if changed else None


def reassign_terminal_computer_owner(
    job_id: str, *, computer_id: str,
) -> dict | None:
    """Move a terminal singleton to its authoritative Computer owner."""
    computer_id = computer_id.strip()
    if not computer_id:
        raise ValueError("computer-owned background job requires computer_id")
    now = db.now_ms()
    c = db.conn()
    c.execute("BEGIN IMMEDIATE")
    try:
        changed = c.execute(
            """UPDATE background_jobs
                  SET agent_id='',session='',owner_kind='computer',computer_id=?,
                      updated_at=?
                WHERE job_id=? AND status IN ('succeeded','failed','cancelled')""",
            (computer_id, now, job_id),
        ).rowcount
        if changed:
            _record_event(c, job_id, now)
        c.execute("COMMIT")
    except BaseException:
        c.execute("ROLLBACK")
        raise
    return get(job_id, reconcile=False) if changed else None


def is_active_for_worker(
    job_id: str, *, generation: int, worker_pid: int,
    worker_start_token: str,
) -> bool:
    row = db.conn().execute(
        """SELECT status,generation,worker_pid,worker_start_token
             FROM background_jobs WHERE job_id=?""",
        (job_id,),
    ).fetchone()
    return bool(
        row and row["status"] in ACTIVE_STATUSES
        and int(row["generation"] or 1) == int(generation)
        and int(row["worker_pid"] or 0) == int(worker_pid)
        and str(row["worker_start_token"] or "") == worker_start_token
    )


def is_cancelled(job_id: str, *, generation: int) -> bool:
    row = db.conn().execute(
        "SELECT status,generation FROM background_jobs WHERE job_id=?", (job_id,)
    ).fetchone()
    return bool(
        row and row["status"] == "cancelled"
        and int(row["generation"] or 1) == int(generation)
    )


def run_terminal_generation_cleanup(
    job_id: str, *, generation: int, cleanup: Callable[[], Any],
    statuses: frozenset[str] = frozenset({"cancelled"}),
) -> tuple[bool, Any]:
    """Run short terminal cleanup preparation while restart is fenced."""
    if not statuses or not statuses <= TERMINAL_STATUSES:
        raise ValueError("cleanup statuses must be terminal background-job states")
    c = db.conn()
    c.execute("BEGIN IMMEDIATE")
    try:
        row = c.execute(
            "SELECT status,generation FROM background_jobs WHERE job_id=?",
            (job_id,),
        ).fetchone()
        if not row or row["status"] not in statuses or int(
            row["generation"] or 1
        ) != int(generation):
            c.execute("COMMIT")
            return False, None
        result = cleanup()
        c.execute("COMMIT")
        return True, result
    except BaseException:
        c.execute("ROLLBACK")
        raise


def reconcile_stale(
    *, job_id: str | None = None, now_ms: int | None = None,
    process_probe: Callable[[int, str], bool] | None = None,
) -> list[str]:
    """Fail active jobs nobody is running any more.

    Two independent reasons end a running job: its heartbeat is older than
    `heartbeat_timeout_ms` (whether or not a worker PID is recorded, since a
    live but wedged worker is no better than a dead one), or its recorded
    worker PID no longer matches the start token it registered with. The
    second is checked on every pass, so a worker killed with `systemctl stop`
    drops out at once instead of a heartbeat timeout later.
    """
    now = int(now_ms if now_ms is not None else db.now_ms())
    probe = process_probe or worker_is_alive
    params: list[Any] = [now]
    job_clause = ""
    if job_id:
        job_clause = "AND job_id=?"
        params.append(job_id)
    rows = db.conn().execute(
        f"""SELECT * FROM background_jobs
             WHERE status IN ('queued','running')
               AND ((? - COALESCE(heartbeat_at, updated_at)) > heartbeat_timeout_ms
                    OR (status='running' AND COALESCE(worker_pid, 0) > 0
                        AND worker_start_token != ''))
               {job_clause}""",
        tuple(params),
    ).fetchall()
    changed: list[str] = []
    for row in rows:
        expired = now - int(row["heartbeat_at"] or row["updated_at"]) > int(
            row["heartbeat_timeout_ms"])
        if row["status"] == "queued":
            try:
                metadata = json.loads(row["metadata_json"] or "{}")
            except json.JSONDecodeError:
                metadata = {}
            if not expired or metadata.get("expire_queued") is not True:
                continue
            if _fail_stale_observation(
                row, now=now, reason="worker_never_started"):
                changed.append(row["job_id"])
            continue
        pid = int(row["worker_pid"] or 0)
        token = str(row["worker_start_token"] or "")
        alive = bool(pid and token and probe(pid, token))
        if not expired and (alive or not (pid and token)):
            continue
        terminal_reason = (
            "heartbeat_expired" if alive or not (pid or token)
            else "worker_vanished")
        if _fail_stale_observation(
                row, now=now, reason=terminal_reason, require_expired=expired):
            changed.append(row["job_id"])
    return changed


def _fail_stale_observation(
    row: Any, *, now: int, reason: str, require_expired: bool = True,
) -> bool:
    """Fail `row` only if nothing touched it since it was read."""
    observed_heartbeat = int(row["heartbeat_at"] or row["updated_at"])
    expiry_clause = (
        "AND (? - COALESCE(heartbeat_at,updated_at)) > heartbeat_timeout_ms"
        if require_expired else "AND ? > 0")
    c = db.conn()
    c.execute("BEGIN IMMEDIATE")
    try:
        changed = c.execute(
            f"""UPDATE background_jobs
                  SET status='failed', terminal_at=?, terminal_reason=?, updated_at=?
                WHERE job_id=? AND status=? AND revision=?
                  AND COALESCE(heartbeat_at,updated_at)=?
                  {expiry_clause}""",
            (
                now, reason, now, row["job_id"], row["status"],
                int(row["revision"]),
                observed_heartbeat, now,
            ),
        ).rowcount
        if changed:
            _record_event(c, row["job_id"], now)
        c.execute("COMMIT")
    except BaseException:
        c.execute("ROLLBACK")
        raise
    return bool(changed)


def events_after(event_id: int, *, limit: int = 100) -> list[dict]:
    rows = db.conn().execute(
        """SELECT event_id, job_id, observed_at FROM background_job_events
            WHERE event_id>? ORDER BY event_id LIMIT ?""",
        (max(0, int(event_id)), max(1, min(int(limit), 500))),
    ).fetchall()
    return [dict(row) for row in rows]


def latest_event_id() -> int:
    row = db.conn().execute(
        "SELECT COALESCE(MAX(event_id), 0) AS event_id FROM background_job_events"
    ).fetchone()
    return int(row["event_id"] if row else 0)


def _active_update(
    job_id: str, *, source: str, status: str | None = None,
    worker_pid: int | None = None, worker_start_token: str = "",
    generation: int | None = None, now_ms: int | None = None,
) -> dict | None:
    now = int(now_ms if now_ms is not None else db.now_ms())
    c = db.conn()
    c.execute("BEGIN IMMEDIATE")
    try:
        row = c.execute(
            "SELECT * FROM background_jobs WHERE job_id=?", (job_id,)
        ).fetchone()
        if row is None or row["status"] not in ACTIVE_STATUSES:
            c.execute("COMMIT")
            return get(job_id, reconcile=False)
        expected_pid = int(row["worker_pid"] or 0)
        expected_token = str(row["worker_start_token"] or "")
        now = max(now, int(row["heartbeat_at"] or row["updated_at"]) + 1)
        if generation is not None and int(row["generation"] or 1) != int(generation):
            c.execute("COMMIT")
            return None
        if expected_pid and (
            not worker_pid or int(worker_pid) != expected_pid
        ):
            c.execute("COMMIT")
            return None
        if expected_token and worker_start_token != expected_token:
            c.execute("COMMIT")
            return None
        next_status = status or row["status"]
        c.execute(
            """UPDATE background_jobs
                  SET status=?, heartbeat_at=?, heartbeat_source=?, updated_at=?,
                      worker_pid=COALESCE(?, worker_pid),
                      worker_start_token=CASE WHEN ?!='' THEN ? ELSE worker_start_token END
                WHERE job_id=? AND status IN ('queued','running')""",
            (
                next_status, now, source[:40], now, worker_pid,
                worker_start_token, worker_start_token, job_id,
            ),
        )
        if next_status != row["status"]:
            _record_event(c, job_id, now)
        c.execute("COMMIT")
    except BaseException:
        c.execute("ROLLBACK")
        raise
    return get(job_id, reconcile=False)


def _record_event(c, job_id: str, observed_at: int, *, note: str = "") -> int:
    row = c.execute(
        "SELECT status FROM background_jobs WHERE job_id=?", (job_id,)).fetchone()
    cur = c.execute(
        "INSERT INTO background_job_events(job_id, observed_at, status, note) "
        "VALUES (?, ?, ?, ?)",
        (job_id, observed_at, str(row["status"] if row else ""), note[:PROGRESS_MAX]),
    )
    revision = int(cur.lastrowid)
    c.execute(
        "UPDATE background_jobs SET revision=? WHERE job_id=?",
        (revision, job_id),
    )
    return revision


def _public(row: Any, *, observed_at: int | None = None) -> dict:
    now = int(observed_at if observed_at is not None else db.now_ms())
    out = dict(row)
    try:
        out["metadata"] = json.loads(out.pop("metadata_json", "{}") or "{}")
    except json.JSONDecodeError:
        out["metadata"] = {}
    owner_kind = str(out.get("owner_kind") or "agent")
    out["owner_kind"] = owner_kind
    out["agent_name"] = (
        "Computer" if owner_kind == "computer"
        else out.pop("persona", "") or out.get("session", "")
    )
    out.pop("persona", None)
    heartbeat_at = out.get("heartbeat_at")
    age = max(0, now - int(heartbeat_at)) if heartbeat_at is not None else None
    out["heartbeat_age_ms"] = age
    monitor_lost = out.get("terminal_reason") in {
        "heartbeat_expired", "worker_vanished",
    }
    if monitor_lost:
        freshness = "stale"
    elif heartbeat_at is None:
        freshness = "unknown"
    elif age is not None and age <= int(out.get("heartbeat_timeout_ms") or 0):
        freshness = "fresh"
    else:
        freshness = "stale"
    out["worker_freshness"] = freshness
    out["is_terminal"] = out.get("status") in TERMINAL_STATUSES
    out["can_cancel"] = out.get("status") in ACTIVE_STATUSES
    # `status=failed` remains the durable worker-lifecycle terminal state. It
    # must not be presented as proof that the external operation failed when
    # all we know is that Clarp stopped hearing from its monitor.
    if monitor_lost:
        outcome_state = "unknown"
    elif out.get("status") in ACTIVE_STATUSES:
        outcome_state = "pending"
    else:
        outcome_state = str(out.get("status") or "unknown")
    out["outcome_state"] = outcome_state
    return out
