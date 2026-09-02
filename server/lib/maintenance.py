"""Bounded retention for ephemeral server data and generated audio."""
from __future__ import annotations

import pathlib
import shutil
import threading
from dataclasses import dataclass

from . import db
from .log import log, log_exception
from .protocol import ClipProducerStatus

HOUR_MS = 60 * 60 * 1000
DAY_MS = 24 * HOUR_MS


@dataclass(frozen=True)
class Policy:
    sse_max_age_ms: int = DAY_MS
    tts_max_age_ms: int = 7 * DAY_MS
    state_max_age_ms: int = 30 * DAY_MS
    clip_row_max_age_ms: int = 30 * DAY_MS
    hls_artifact_max_age_ms: int = DAY_MS
    # Must outlive the longest window lib.turn_usage reports on (7 days).
    turn_usage_max_age_ms: int = 30 * DAY_MS
    background_job_events_max_age_ms: int = 30 * DAY_MS


def prune_database(*, now_ms: int | None = None,
                   policy: Policy = Policy()) -> dict[str, int]:
    now_ms = db.now_ms() if now_ms is None else int(now_ms)
    c = db.conn()
    counts: dict[str, int] = {}
    counts["sse_events"] = c.execute(
        "DELETE FROM sse_events WHERE ts < ?",
        (now_ms - policy.sse_max_age_ms,),
    ).rowcount
    counts["tts_queue"] = c.execute(
        """DELETE FROM tts_queue
            WHERE status IN ('done', 'failed')
              AND completed_at < ?""",
        (now_ms - policy.tts_max_age_ms,),
    ).rowcount
    counts["state_log"] = c.execute(
        """DELETE FROM state_log
            WHERE ts < ?
              AND state_id NOT IN (
                  SELECT MAX(state_id) FROM state_log GROUP BY agent_id
              )""",
        (now_ms - policy.state_max_age_ms,),
    ).rowcount
    counts["background_job_events"] = c.execute(
        """DELETE FROM background_job_events
            WHERE observed_at < ?
              AND event_id NOT IN (
                  SELECT MAX(event_id) FROM background_job_events GROUP BY job_id
              )""",
        (now_ms - policy.background_job_events_max_age_ms,),
    ).rowcount
    counts["clips"] = c.execute(
        """DELETE FROM clips
            WHERE created_at < ?
              AND producer_status IN (?, ?)""",
        (now_ms - policy.clip_row_max_age_ms,
         ClipProducerStatus.COMPLETE, ClipProducerStatus.FAILED),
    ).rowcount
    counts["turn_usage"] = c.execute(
        "DELETE FROM turn_usage WHERE at < ?",
        (now_ms - policy.turn_usage_max_age_ms,),
    ).rowcount
    return counts


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


class MaintenanceWorker:
    """Run conservative retention cleanup at startup and then hourly."""

    def __init__(self, *, audio_dir: pathlib.Path, policy: Policy = Policy(),
                 interval_sec: float = 60 * 60):
        self.audio_dir = pathlib.Path(audio_dir)
        self.policy = policy
        self.interval_sec = interval_sec
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

    def run_once(self) -> dict[str, int]:
        counts = {"hls_artifacts": prune_hls_artifacts(
            self.audio_dir, max_age_ms=self.policy.hls_artifact_max_age_ms,
        )}
        counts.update(prune_database(policy=self.policy))
        try:
            from . import telemetry
            counts.update(telemetry.rollup_and_prune())
        except Exception as e:  # noqa: BLE001
            log_exception("telemetryMaintenanceFail", e)
        if any(counts.values()):
            log("maintenancePruned", str(counts))
        # Passive checkpoints never truncate under constant readers, so the
        # WAL grows without bound (it once reached 3 GB and stalled writes
        # for minutes). TRUNCATE here keeps it capped; busy_timeout bounds
        # how long it may wait, and a busy result just defers to next hour.
        try:
            db.conn().execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception as e:  # noqa: BLE001
            log_exception("maintenanceCheckpointFail", e)
        return counts

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception as e:  # noqa: BLE001
                log_exception("maintenanceFail", e)
            if self._stop.wait(self.interval_sec):
                break
