"""Bounded retention for ephemeral server data and generated audio."""
from __future__ import annotations

import datetime
import errno
import fcntl
import os
import pathlib
import shutil
import threading
import time
from dataclasses import dataclass

from . import batched_delete, db
from .log import log, log_exception
from .protocol import ClipProducerStatus
from .timing import MAINTENANCE_STARTUP_DELAY_SEC

HOUR_MS = 60 * 60 * 1000
# state_log retention deletes at most this many rows per statement and pauses
# between statements, so other writers get the lock in between (PT-01).
STATE_LOG_DELETE_BATCH = 500
STATE_LOG_BATCH_PAUSE_S = 0.05
DAY_MS = 24 * HOUR_MS
MIB = 1024 * 1024


@dataclass(frozen=True)
class Policy:
    sse_max_age_ms: int = DAY_MS
    tts_max_age_ms: int = 7 * DAY_MS
    state_max_age_ms: int = 30 * DAY_MS
    clip_row_max_age_ms: int = 30 * DAY_MS
    hls_artifact_max_age_ms: int = DAY_MS
    # The daily event-log mirror (lib.eventlog) is diagnostic; SQLite holds the
    # live record. Nothing else ever deleted these files.
    event_log_max_age_ms: int = 14 * DAY_MS
    # Size bounds on the diagnostics, on top of the age bounds: on 10 Nov 2026
    # they held 34 GB, all of it younger than its age limit. The telemetry
    # database's budget is telemetry.DETAIL_BUDGET_BYTES.
    # The event-log directory: the oldest days go first. Today's file is never
    # deleted; when it alone is over the bound its newest part is kept.
    event_log_max_bytes: int = 2048 * MIB
    event_log_today_keep_bytes: int = 512 * MIB
    # hook.log (hooks and the client log append to it; nothing else trims it).
    hook_log_max_bytes: int = 100 * MIB
    hook_log_keep_bytes: int = 20 * MIB
    # The size bounds are also checked between the hourly passes, so a flood
    # overshoots them by minutes of writes, not by an hour of them.
    size_check_interval_sec: float = 5 * 60
    # Must outlive the longest window lib.turn_usage reports on (7 days).
    turn_usage_max_age_ms: int = 30 * DAY_MS
    background_job_events_max_age_ms: int = 30 * DAY_MS
    judgment_decisions_max_age_ms: int = 30 * DAY_MS
    # None reads [agents] helper_archive_grace_hours on every sweep.
    helper_archive_grace_ms: int | None = None


class _Phases:
    """Diagnostic metadata for one maintenance pass: per fixed phase, a monotonic EXECUTION duration, a row count and the
    outcome. The duration is wall time around the call: it includes waits (busy_timeout, I/O, GIL) and is NOT lock-held
    time. Phase names are fixed strings; no SQL, parameters or content is recorded. Recording never raises and never
    changes a phase's result or exception."""

    MAX_PHASES = 32
    _NO_CLOCK = object()      # sentinel: the initial clock read failed

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.records: list[tuple[str, int, int | None, str]] = []

    def run(self, name: str, fn):
        start = self._NO_CLOCK
        try:
            start = self.clock()
        except Exception:  # noqa: BLE001 - no timing for this phase only
            start = self._NO_CLOCK
        if start is self._NO_CLOCK:
            # Called AFTER the except block has been left, so a work exception is raised fresh: the clock failure is
            # never attached as its __context__. fn runs exactly once; its result or original exception is unchanged.
            return fn()
        outcome, result = "ok", None
        try:
            result = fn()
            return result
        except BaseException as exc:
            outcome = type(exc).__name__[:64]
            raise
        finally:
            try:
                if len(self.records) < self.MAX_PHASES:
                    elapsed_ms = max(0, int((self.clock() - start) * 1000))
                    self.records.append((name, elapsed_ms, _row_count(result) if outcome == "ok" else None, outcome))
            except Exception:  # noqa: BLE001 - diagnostics are best effort
                pass

    def line(self) -> str:
        parts = [f"{name}:{ms}ms:rows={'-' if rows is None else rows}:{outcome}"
                 for name, ms, rows, outcome in self.records]
        return "execution_ms includes waits; NOT lock-held | " + " ".join(parts)


def _row_count(result) -> int | None:
    if isinstance(result, bool):
        return None
    if isinstance(result, int):
        return result
    if isinstance(result, dict):
        return sum(v for v in result.values() if isinstance(v, int) and not isinstance(v, bool))
    return None


def _prune_keeping_newest(c, statement: str, params, *, key: str, group: str,
                          batch: int, pause_s: float) -> int:
    """Delete expired rows, keeping each group's newest, one bounded batch per
    autocommit statement. The newest rows are read once per sweep, not once
    per batch, in one statement with the highest key; a row written during the
    sweep is above it and left alone, even back-dated. The expired rows are
    found by short reads outside the write lock (RM-N8)."""
    table, where = batched_delete.split(statement)
    high, keep = c.execute(
        f"""SELECT (SELECT MAX({key}) FROM {table}),
                   (SELECT json_group_array(newest) FROM (
                        SELECT MAX({key}) AS newest FROM {table} GROUP BY {group}))"""
    ).fetchone()
    if high is None:
        return 0
    # Every read and every delete carries the whole condition, so a row read
    # outside the lock is deleted only if it still qualifies when written.
    return batched_delete.prune(
        c, f"{statement} AND {key} <= ? AND {key} NOT IN (SELECT value FROM json_each(?))",
        (*params, high, keep), batch=batch, pause_s=pause_s)


def _prune_state_log(c, cutoff_ms: int) -> int:
    """Expired state rows, keeping each agent's newest (PT-01, RM-N8)."""
    return _prune_keeping_newest(
        c, "DELETE FROM state_log WHERE ts < ?", (cutoff_ms,), key="state_id", group="agent_id",
        batch=STATE_LOG_DELETE_BATCH, pause_s=STATE_LOG_BATCH_PAUSE_S)


def _timed(phases: _Phases | None, name: str, fn):
    return fn() if phases is None else phases.run(name, fn)


def prune_database(*, now_ms: int | None = None,
                   policy: Policy = Policy(), phases: _Phases | None = None) -> dict[str, int]:
    now_ms = db.now_ms() if now_ms is None else int(now_ms)
    c = db.conn()
    counts: dict[str, int] = {}
    # Every retention delete runs in bounded batches found by reads outside
    # the write lock (RM-N8): one statement over an unindexed column held the
    # lock for its whole table scan.
    counts["sse_events"] = _timed(phases, "db.sse_events", lambda: batched_delete.prune(
        c, "DELETE FROM sse_events WHERE ts < ?",
        (now_ms - policy.sse_max_age_ms,)))
    counts["tts_queue"] = _timed(phases, "db.tts_queue", lambda: batched_delete.prune(
        c, """DELETE FROM tts_queue
            WHERE status IN ('done', 'failed')
              AND completed_at < ?""",
        (now_ms - policy.tts_max_age_ms,)))
    counts["state_log"] = _timed(phases, "db.state_log", lambda: _prune_state_log(
        c, now_ms - policy.state_max_age_ms))
    counts["background_job_events"] = _timed(phases, "db.background_job_events", lambda: _prune_keeping_newest(
        c, "DELETE FROM background_job_events WHERE observed_at < ?",
        (now_ms - policy.background_job_events_max_age_ms,), key="event_id", group="job_id",
        batch=batched_delete.DELETE_BATCH, pause_s=batched_delete.PAUSE_S))
    counts["clips"] = _timed(phases, "db.clips", lambda: batched_delete.prune(
        c, """DELETE FROM clips
            WHERE created_at < ?
              AND producer_status IN (?, ?)""",
        (now_ms - policy.clip_row_max_age_ms,
         ClipProducerStatus.COMPLETE, ClipProducerStatus.FAILED)))
    counts["turn_usage"] = _timed(phases, "db.turn_usage", lambda: batched_delete.prune(
        c, "DELETE FROM turn_usage WHERE at < ?",
        (now_ms - policy.turn_usage_max_age_ms,)))
    counts["judgment_decisions"] = _timed(phases, "db.judgment_decisions", lambda: batched_delete.prune(
        c, "DELETE FROM judgment_decisions WHERE created_at < ?",
        (now_ms - policy.judgment_decisions_max_age_ms,)))
    # VP-04: audio bookkeeping observations and their janitor runs, with the
    # clip retention, in short transactions. Logged, never fatal.
    try:
        from . import audio_bookkeeper
        pruned = _timed(phases, "db.audio_bookkeeping", lambda: audio_bookkeeper.prune(
            now_ms, max_age_ms=policy.clip_row_max_age_ms))
        counts["audio_bookkeeping_events"] = pruned["events"]
        counts["audio_bookkeeping_runs"] = pruned["runs"]
    except Exception as e:  # noqa: BLE001
        from .log import log_exception
        log_exception("audioBookkeepingPruneFail", e)
        counts["audio_bookkeeping_events"] = counts["audio_bookkeeping_runs"] = 0
    from . import tool_explanation_learning, tool_explanation_templates
    from .tool_explanations import PROMPT_VERSION
    counts.update(_timed(phases, "db.tool_explanation_learning", lambda: tool_explanation_learning.prune(
        c, now_ms, PROMPT_VERSION, tool_explanation_templates.VERSION)))
    from .tool_explanations import recover_programs
    counts["tool_explanation_programs"] = _timed(phases, "db.tool_explanation_programs", lambda: recover_programs(c))
    # F4 N7: launch receipts and their delivery pointers, at most PRUNE_LIMIT
    # per pass in their own short transaction (no catch-up loop). With this
    # pass on schedule and no backlog a receipt lives about 7 days + up to one
    # pass interval; that is not a guarantee. A failure is logged and never
    # touches an admission or the other phases.
    try:
        from . import launch_receipts
        counts["launch_receipts"] = _timed(
            phases, "db.launch_receipts", lambda: launch_receipts.prune(now_ms)["receipts"])
    except Exception as e:  # noqa: BLE001
        from .log import log_exception
        log_exception("launchReceiptPruneFail", e)
        counts["launch_receipts"] = 0
    # Pointers an older runtime left inconsistent (rollback residue): at most
    # RECONCILE_LIMIT per pass in their own transaction; any finding expires
    # every delivery cursor. Never touches a receipt. Logged, never fatal.
    from . import launch_receipts

    def reconcile_pointers() -> int:
        result = launch_receipts.reconcile()
        launch_receipts.record_reconcile_pass(result)       # process-local, health only
        return result["orphans"] + result["stale"]
    try:
        counts["launch_receipt_pointers"] = _timed(
            phases, "db.launch_receipt_pointers", reconcile_pointers)
    except Exception as e:  # noqa: BLE001
        launch_receipts.record_reconcile_failure(e)          # no database write on failure
        from .log import log_exception
        log_exception("launchReceiptReconcileFail", e)
        counts["launch_receipt_pointers"] = 0
    return counts


def archive_done_helpers(*, now_ms: int | None = None, policy: Policy = Policy(),
                         stream=None) -> int:
    """Archive helper agents that have been done for longer than the grace."""
    from . import helper_agents
    grace = policy.helper_archive_grace_ms
    if grace is None:
        from . import config
        grace = int(config.load().helper_archive_grace_hours * HOUR_MS)
    now_ms = db.now_ms() if now_ms is None else int(now_ms)
    return len(helper_agents.archive_finished(now_ms=now_ms, grace_ms=grace, stream=stream))


def prune_hls_artifacts(audio_dir: pathlib.Path, *, now_ms: int | None = None,
                        max_age_ms: int = Policy().hls_artifact_max_age_ms) -> int:
    now_ms = db.now_ms() if now_ms is None else int(now_ms)
    cutoff = now_ms - max_age_ms
    root = pathlib.Path(audio_dir) / "hls"
    if not root.is_dir():
        return 0
    rows = db.conn().execute(
        "SELECT clip_id, created_at, producer_status FROM clips"
    ).fetchall()
    clips = {int(row["clip_id"]): row for row in rows}
    removed = 0
    for target in root.iterdir():
        if not target.is_dir() or not target.name.isdigit():
            continue
        row = clips.get(int(target.name))
        expired_known = bool(
            row
            and int(row["created_at"]) < cutoff
            and row["producer_status"] in {
                ClipProducerStatus.COMPLETE, ClipProducerStatus.FAILED,
            }
        )
        expired_orphan = row is None and int(target.stat().st_mtime * 1000) < cutoff
        if expired_known or expired_orphan:
            shutil.rmtree(target, ignore_errors=True)
            removed += 1
    return removed


_COPY_CHUNK = MIB


def _copy_within(fd: int, src: int, dst: int, length: int) -> int:
    """Copy ``length`` bytes of one file from ``src`` down to ``dst``; the end
    of what was written."""
    while length > 0:
        chunk = os.pread(fd, min(_COPY_CHUNK, length), src)
        if not chunk:
            break
        view = memoryview(chunk)
        while view:
            written = os.pwrite(fd, view, dst)
            dst += written
            view = view[written:]
        src += len(chunk)
        length -= len(chunk)
    return dst


def _line_start(fd: int, offset: int, size: int) -> int:
    """The first line start at or after ``offset``; ``size`` when there is none."""
    at = max(0, offset - 1)
    while at < size:
        chunk = os.pread(fd, min(65536, size - at), at)
        if not chunk:
            break
        newline = chunk.find(b"\n")
        if newline >= 0:
            return at + newline + 1
        at += len(chunk)
    return size


def keep_tail(path: pathlib.Path, keep_bytes: int) -> int:
    """Cut a log file down, in place, to its newest ``keep_bytes`` (at most
    half of it), starting at a line; the number of bytes removed.

    Safe against the processes that append to it. Every writer opens the file
    in append mode for each write, so it only ever adds after the end measured
    here. The kept part is first copied over the head of the same file under
    no lock: it is at most half the file, so it never reaches what appenders
    write. Only the short finish (copy what was appended meanwhile, truncate)
    holds the flock the event-log writers take, so an emit waits for that and
    not for the copy. A writer that takes no lock (hook.log) can lose a line it
    appends during the finish.

    In place, never a new file moved over the old one: a writer waiting for
    the lock already has this file open and would write into the discarded
    one. After a crash midway the newest part is still intact at the end of
    the file and the next pass repeats the cut. On a disk too full even for
    the copy (a copy-on-write filesystem writes new blocks), the file is
    emptied instead: freeing the space is what matters then.
    """
    fd = os.open(path, os.O_RDWR)   # not O_APPEND: pwrite would ignore its offset
    try:
        size = os.fstat(fd).st_size
        start = _line_start(fd, size - min(max(0, keep_bytes), size // 2), size)
        if size == 0 or start == 0:
            return 0
        try:
            end = _copy_within(fd, start, 0, size - start)
        except OSError as e:
            if e.errno != errno.ENOSPC:
                raise
            end = start = size       # keep nothing of what was measured
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            grown = os.fstat(fd).st_size
            if grown < size:
                return 0    # something else cut the file meanwhile: leave it
            if start == size:
                os.ftruncate(fd, 0)
                return grown
            end = _copy_within(fd, size, end, grown - size)
            os.ftruncate(fd, end)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
        return grown - end
    finally:
        os.close(fd)


def prune_event_logs(log_dir: pathlib.Path, *, max_age_ms: int,
                     today: datetime.date | None = None,
                     max_total_bytes: int | None = None,
                     today_keep_bytes: int | None = None) -> int:
    """Delete daily ``YYYY-MM-DD.jsonl`` event logs older than ``max_age_ms``,
    then the oldest remaining days while together they exceed
    ``max_total_bytes``; the number of files deleted.

    Age comes from the date in the file name, so a touched or copied file
    cannot dodge retention, and any other file in the directory is left alone.

    Today's file is never deleted. If it alone is over ``max_total_bytes`` it
    is cut down to its newest ``today_keep_bytes`` (keep_tail) and one
    ``eventLogCut`` record says so.
    """
    today = today or datetime.datetime.now(datetime.timezone.utc).date()
    cutoff = today - datetime.timedelta(milliseconds=max_age_ms)
    removed = 0
    kept: list[tuple[datetime.date, pathlib.Path]] = []
    for path in pathlib.Path(log_dir).glob("*.jsonl"):
        try:
            day = datetime.date.fromisoformat(path.stem)
        except ValueError:
            continue
        if day >= cutoff:
            kept.append((day, path))
            continue
        try:
            path.unlink()
            removed += 1
        except OSError as e:
            log_exception("eventLogPruneFail", e, detail=path.name)
    if max_total_bytes is None:
        return removed
    sizes: dict[pathlib.Path, int] = {}
    for _, path in kept:
        try:
            sizes[path] = path.stat().st_size
        except OSError:
            continue    # gone meanwhile
    for day, path in sorted(kept):
        if sum(sizes.values()) <= max_total_bytes:
            break
        if path not in sizes:
            continue
        try:
            if day < today:
                path.unlink()
                removed += 1
                del sizes[path]
                continue
            keep = max_total_bytes // 4 if today_keep_bytes is None else today_keep_bytes
            cut = keep_tail(path, min(keep, max_total_bytes))
            if cut:
                sizes[path] -= cut
                log("eventLogCut", f"file={path.name} removed_bytes={cut} "
                    f"kept_bytes={sizes[path]} max_bytes={max_total_bytes}")
        except OSError as e:
            log_exception("eventLogPruneFail", e, detail=path.name)
    return removed


def prune_hook_log(path: pathlib.Path, *, max_bytes: int, keep_bytes: int) -> int:
    """Keep hook.log bounded: over ``max_bytes`` it is cut down to its newest
    ``keep_bytes`` (keep_tail) and a line in it says so; the bytes removed."""
    try:
        if path.stat().st_size <= max_bytes:
            return 0
    except OSError:
        return 0    # no hook log yet
    cut = keep_tail(path, keep_bytes)
    if cut:
        with path.open("a") as f:
            f.write(f"{time.strftime('%H:%M:%S')} maintenance {os.getpid()} "
                    f"hookLogCut removed_bytes={cut} max_bytes={max_bytes}\n")
        log("hookLogCut", f"removed_bytes={cut} max_bytes={max_bytes}")
    return cut


def _checkpoint_database() -> None:
    # A blocking checkpoint can hold the writer while waiting for readers.
    # Keep TRUNCATE opportunistic, without raising a caller's shorter timeout.
    with db.busy_timeout_at_most(100) as connection:
        result = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    _log_checkpoint_result("TRUNCATE", result)
    if result[0]:
        # PASSIVE copies what it can without waiting for readers or writers.
        # Retry truncation on the next ordinary hourly pass, as before.
        result = db.conn().execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
        _log_checkpoint_result("PASSIVE", result)


def _log_checkpoint_result(mode: str, result) -> None:
    log("maintenanceCheckpoint",
        f"mode={mode} busy={result[0]} log_frames={result[1]} checkpointed_frames={result[2]}")


class MaintenanceWorker:
    """Run conservative retention cleanup at startup and then hourly, and
    check the diagnostics' size bounds every few minutes in between."""

    def __init__(self, *, audio_dir: pathlib.Path, policy: Policy = Policy(),
                 interval_sec: float = 60 * 60,
                 startup_delay_sec: float | None = None, stream=None,
                 hook_log: pathlib.Path | None = None):
        self.audio_dir = pathlib.Path(audio_dir)
        # None: this worker does not bound a hook log (tests, embedded use).
        self.hook_log = pathlib.Path(hook_log) if hook_log is not None else None
        self.stream = stream
        self.policy = policy
        self.interval_sec = interval_sec
        self.startup_delay_sec = (
            MAINTENANCE_STARTUP_DELAY_SEC if startup_delay_sec is None
            else max(0.0, float(startup_delay_sec))
        )
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="maintenance-worker",
        )
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)

    def run_once(self, *, checkpoint: bool = True) -> dict[str, int]:
        # Diagnostic phase metadata (lane-1 DB-lock attribution): logged once in `finally`, so phases already timed
        # survive a later phase that raises; results, exceptions and their handling below are unchanged.
        phases = _Phases()
        try:
            counts = {"hls_artifacts": phases.run("hls_artifacts", lambda: prune_hls_artifacts(
                self.audio_dir, max_age_ms=self.policy.hls_artifact_max_age_ms,
            ))}
            counts.update(prune_database(policy=self.policy, phases=phases))
            counts["event_logs"] = phases.run("event_logs", self._prune_event_logs)
            if self.hook_log is not None:
                try:
                    counts["hook_log_bytes"] = phases.run("hook_log", self._prune_hook_log)
                except Exception as e:  # noqa: BLE001
                    log_exception("hookLogPruneFail", e)
            try:
                counts["helpers_archived"] = phases.run("helpers_archived", lambda: archive_done_helpers(
                    policy=self.policy, stream=self.stream))
            except Exception as e:  # noqa: BLE001
                log_exception("helperArchiveFail", e)
            try:
                from . import telemetry
                counts.update(phases.run("telemetry", lambda: telemetry.rollup_and_prune(checkpoint=checkpoint)))
            except Exception as e:  # noqa: BLE001
                log_exception("telemetryMaintenanceFail", e)
            self._report_current_hour_cut(counts)
            if any(counts.values()):
                log("maintenancePruned", str(counts))
            # Keep trying to truncate hourly so a quiet pass can cap the WAL.
            # A reader prevents truncation only for the short busy budget;
            # PASSIVE then makes progress without holding up new writers.
            if checkpoint:
                try:
                    phases.run("wal_checkpoint", _checkpoint_database)
                except Exception as e:  # noqa: BLE001
                    log_exception("maintenanceCheckpointFail", e)
            return counts
        finally:
            try:
                log("maintenancePhases", phases.line())
            except Exception:  # noqa: BLE001 - diagnostics are best effort
                pass

    def _prune_event_logs(self) -> int:
        from . import eventlog
        return prune_event_logs(
            eventlog.LOG_DIR, max_age_ms=self.policy.event_log_max_age_ms,
            max_total_bytes=self.policy.event_log_max_bytes,
            today_keep_bytes=self.policy.event_log_today_keep_bytes)

    def _prune_hook_log(self) -> int:
        return prune_hook_log(
            self.hook_log, max_bytes=self.policy.hook_log_max_bytes,
            keep_bytes=self.policy.hook_log_keep_bytes)

    @staticmethod
    def _report_current_hour_cut(counts: dict[str, int]) -> None:
        # The one budget cut that costs accuracy: say so on its own line.
        cut = counts.get("telemetry_current_hour_cut")
        if cut:
            log("telemetryCurrentHourCut",
                f"rows={cut} the current hour alone was over the telemetry budget; "
                "its hourly rollup will count only the rows that are left")

    def check_sizes(self) -> dict[str, int]:
        """The size bounds alone, between the hourly passes. With nothing over
        its bound this is a few file sizes and three pragmas."""
        counts: dict[str, int] = {}
        try:
            counts["event_logs"] = self._prune_event_logs()
        except Exception as e:  # noqa: BLE001
            log_exception("eventLogPruneFail", e)
        if self.hook_log is not None:
            try:
                counts["hook_log_bytes"] = self._prune_hook_log()
            except Exception as e:  # noqa: BLE001
                log_exception("hookLogPruneFail", e)
        try:
            from . import telemetry
            if telemetry.over_budget():
                # The whole pass, so every hour the budget deletes is rolled
                # up first. No exclusive checkpoint outside the hourly pass.
                counts.update(telemetry.rollup_and_prune(checkpoint=False))
        except Exception as e:  # noqa: BLE001
            log_exception("telemetryMaintenanceFail", e)
        self._report_current_hour_cut(counts)
        if any(counts.values()):
            log("maintenancePruned", str(counts))
        return counts

    def _wait_for_next_pass(self) -> bool:
        """Wait out the interval to the next full pass, checking the size
        bounds on the way. True when the worker was stopped."""
        step = self.policy.size_check_interval_sec
        deadline = time.monotonic() + self.interval_sec
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return self._stop.is_set()
            if step <= 0 or remaining <= step:
                return self._stop.wait(remaining)
            if self._stop.wait(step):
                return True
            try:
                self.check_sizes()
            except Exception as e:  # noqa: BLE001
                log_exception("maintenanceFail", e)

    def _loop(self) -> None:
        # Retention and the telemetry rollup are ordinary short writes, so they
        # still run at boot; deferring the whole sweep left telemetry.sqlite
        # missing until the first hourly pass. Only the exclusive WAL
        # truncation races runtime interrupt recovery (SQLITE_BUSY → skipped
        # INTERRUPTED marks → silent "thinking"), so only it waits.
        try:
            self.run_once(checkpoint=False)
        except Exception as e:  # noqa: BLE001
            log_exception("maintenanceFail", e)
        if self.startup_delay_sec and self._stop.wait(self.startup_delay_sec):
            return
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception as e:  # noqa: BLE001
                log_exception("maintenanceFail", e)
            if self._wait_for_next_pass():
                break
