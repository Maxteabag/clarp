"""Bounded retention for ephemeral server data and generated audio."""
from __future__ import annotations

import datetime
import pathlib
import shutil
import threading
import time
from dataclasses import dataclass

from . import db
from .log import log, log_exception
from .protocol import ClipProducerStatus
from .timing import MAINTENANCE_STARTUP_DELAY_SEC

HOUR_MS = 60 * 60 * 1000
DAY_MS = 24 * HOUR_MS


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


def _timed(phases: _Phases | None, name: str, fn):
    return fn() if phases is None else phases.run(name, fn)


def prune_database(*, now_ms: int | None = None,
                   policy: Policy = Policy(), phases: _Phases | None = None) -> dict[str, int]:
    now_ms = db.now_ms() if now_ms is None else int(now_ms)
    c = db.conn()
    counts: dict[str, int] = {}
    counts["sse_events"] = _timed(phases, "db.sse_events", lambda: c.execute(
        "DELETE FROM sse_events WHERE ts < ?",
        (now_ms - policy.sse_max_age_ms,),
    ).rowcount)
    counts["tts_queue"] = _timed(phases, "db.tts_queue", lambda: c.execute(
        """DELETE FROM tts_queue
            WHERE status IN ('done', 'failed')
              AND completed_at < ?""",
        (now_ms - policy.tts_max_age_ms,),
    ).rowcount)
    counts["state_log"] = _timed(phases, "db.state_log", lambda: c.execute(
        """DELETE FROM state_log
            WHERE ts < ?
              AND state_id NOT IN (
                  SELECT MAX(state_id) FROM state_log GROUP BY agent_id
              )""",
        (now_ms - policy.state_max_age_ms,),
    ).rowcount)
    counts["background_job_events"] = _timed(phases, "db.background_job_events", lambda: c.execute(
        """DELETE FROM background_job_events
            WHERE observed_at < ?
              AND event_id NOT IN (
                  SELECT MAX(event_id) FROM background_job_events GROUP BY job_id
              )""",
        (now_ms - policy.background_job_events_max_age_ms,),
    ).rowcount)
    counts["clips"] = _timed(phases, "db.clips", lambda: c.execute(
        """DELETE FROM clips
            WHERE created_at < ?
              AND producer_status IN (?, ?)""",
        (now_ms - policy.clip_row_max_age_ms,
         ClipProducerStatus.COMPLETE, ClipProducerStatus.FAILED),
    ).rowcount)
    counts["turn_usage"] = _timed(phases, "db.turn_usage", lambda: c.execute(
        "DELETE FROM turn_usage WHERE at < ?",
        (now_ms - policy.turn_usage_max_age_ms,),
    ).rowcount)
    counts["judgment_decisions"] = _timed(phases, "db.judgment_decisions", lambda: c.execute(
        "DELETE FROM judgment_decisions WHERE created_at < ?",
        (now_ms - policy.judgment_decisions_max_age_ms,),
    ).rowcount)
    from . import tool_explanation_learning, tool_explanation_templates
    from .tool_explanations import PROMPT_VERSION
    counts.update(_timed(phases, "db.tool_explanation_learning", lambda: tool_explanation_learning.prune(
        c, now_ms, PROMPT_VERSION, tool_explanation_templates.VERSION)))
    from .tool_explanations import recover_programs
    counts["tool_explanation_programs"] = _timed(phases, "db.tool_explanation_programs", lambda: recover_programs(c))
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


def prune_event_logs(log_dir: pathlib.Path, *, max_age_ms: int,
                     today: datetime.date | None = None) -> int:
    """Delete daily ``YYYY-MM-DD.jsonl`` event logs older than ``max_age_ms``.

    Age comes from the date in the file name, so a touched or copied file
    cannot dodge retention, and any other file in the directory is left alone.
    """
    today = today or datetime.datetime.now(datetime.timezone.utc).date()
    cutoff = today - datetime.timedelta(milliseconds=max_age_ms)
    removed = 0
    for path in pathlib.Path(log_dir).glob("*.jsonl"):
        try:
            day = datetime.date.fromisoformat(path.stem)
        except ValueError:
            continue
        if day >= cutoff:
            continue
        try:
            path.unlink()
            removed += 1
        except OSError as e:
            log_exception("eventLogPruneFail", e, detail=path.name)
    return removed


class MaintenanceWorker:
    """Run conservative retention cleanup at startup and then hourly."""

    def __init__(self, *, audio_dir: pathlib.Path, policy: Policy = Policy(),
                 interval_sec: float = 60 * 60,
                 startup_delay_sec: float | None = None, stream=None):
        self.audio_dir = pathlib.Path(audio_dir)
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
            from . import eventlog
            counts["event_logs"] = phases.run("event_logs", lambda: prune_event_logs(
                eventlog.LOG_DIR, max_age_ms=self.policy.event_log_max_age_ms,
            ))
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
            if any(counts.values()):
                log("maintenancePruned", str(counts))
            # Passive checkpoints never truncate under constant readers, so the
            # WAL grows without bound (it once reached 3 GB and stalled writes
            # for minutes). TRUNCATE here keeps it capped; busy_timeout bounds
            # how long it may wait, and a busy result just defers to next hour.
            if checkpoint:
                try:
                    # Its outcome "ok" means only that execute() returned: it does NOT prove the TRUNCATE completed or
                    # that the checkpoint was not busy (the PRAGMA reports busy in its result row, which is not read).
                    phases.run("wal_checkpoint", lambda: db.conn().execute("PRAGMA wal_checkpoint(TRUNCATE)"))
                except Exception as e:  # noqa: BLE001
                    log_exception("maintenanceCheckpointFail", e)
            return counts
        finally:
            try:
                log("maintenancePhases", phases.line())
            except Exception:  # noqa: BLE001 - diagnostics are best effort
                pass

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
            if self._stop.wait(self.interval_sec):
                break
