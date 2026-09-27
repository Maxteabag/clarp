"""When a work label has outlived the work behind it.

The apps draw an agent as still working while it has a running background
job, a running helper, or a status line of its own. Each of those can be left
behind when the work ends without saying so: a worker that stops
heartbeating where its PID cannot be checked, a helper whose session went
quiet without reporting, a status nobody cleared. These three rules decide
when to retire them. The IO code (``lib.stale_work`` and
``background_jobs.reconcile_stale``) gathers the facts and acts on the answer.

A threshold of 0 turns its rule off. Nothing here touches work that can be
shown to be alive: a verified worker PID, a running or queued turn, an active
job or a running helper.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

MINUTE_MS = 60 * 1000
HOUR_MS = 60 * MINUTE_MS

# Terminal reason for a job failed by the heartbeat rule, and the timeline
# note left when it first goes stale.
HEARTBEAT_LOST = "heartbeat_lost"
STALE_NOTE = "reconcile: heartbeat stale; worker PID cannot be verified"
LOST_NOTE = "reconcile: heartbeat lost; failed after the grace period"


@dataclass(frozen=True)
class Thresholds:
    job_stale_after_ms: int = 15 * MINUTE_MS
    job_grace_ms: int = 15 * MINUTE_MS
    helper_idle_after_ms: int = 30 * MINUTE_MS
    custom_status_ttl_ms: int = 2 * HOUR_MS


def from_config(cfg) -> Thresholds:
    """Thresholds from a loaded ``config.Config`` (its ``[agents]`` values).

    A stand-in config without these fields gets the defaults."""
    default = Thresholds()
    return Thresholds(
        job_stale_after_ms=int(getattr(cfg, "job_stale_after_minutes",
                                       default.job_stale_after_ms / MINUTE_MS) * MINUTE_MS),
        job_grace_ms=int(getattr(cfg, "job_heartbeat_grace_minutes",
                                 default.job_grace_ms / MINUTE_MS) * MINUTE_MS),
        helper_idle_after_ms=int(getattr(cfg, "helper_idle_after_minutes",
                                         default.helper_idle_after_ms / MINUTE_MS) * MINUTE_MS),
        custom_status_ttl_ms=int(getattr(cfg, "custom_status_ttl_hours",
                                         default.custom_status_ttl_ms / HOUR_MS) * HOUR_MS),
    )


class JobVerdict(StrEnum):
    KEEP = "keep"
    STALE = "stale"    # note it on the timeline, do not end it yet
    LOST = "lost"      # fail it with HEARTBEAT_LOST


def job_heartbeat(*, heartbeat_age_ms: int, worker_verified: bool,
                  thresholds: Thresholds) -> JobVerdict:
    """What to do with a running job whose heartbeat is ``heartbeat_age_ms`` old.

    A worker whose PID is verified alive is never touched here; the existing
    ``heartbeat_timeout_ms`` rule owns a live but wedged worker.
    """
    stale = int(thresholds.job_stale_after_ms)
    if worker_verified or stale <= 0:
        return JobVerdict.KEEP
    if heartbeat_age_ms > stale + max(0, int(thresholds.job_grace_ms)):
        return JobVerdict.LOST
    if heartbeat_age_ms > stale:
        return JobVerdict.STALE
    return JobVerdict.KEEP


@dataclass(frozen=True)
class Activity:
    """What an agent is doing right now, as the dashboard counts it."""
    busy: bool                # a turn is running or queued
    active_jobs: int          # background processes, not helper mirrors
    running_children: int     # helpers still running
    idle_since_ms: int        # last state change, turn end or job change

    @property
    def idle(self) -> bool:
        return not self.busy and not self.active_jobs and not self.running_children


def helper_idle_due(*, helper_state: str | None, activity: Activity,
                    now_ms: int, thresholds: Thresholds) -> bool:
    """A running helper whose own session has sat idle past the threshold.

    It moves to ``reported``, not done or failed: the parent still decides.
    """
    limit = int(thresholds.helper_idle_after_ms)
    return (limit > 0 and helper_state == "running" and activity.idle
            and now_ms - int(activity.idle_since_ms or 0) > limit)


def custom_status_due(*, status: str, status_at: int | None, janitor_owned: bool,
                      activity: Activity, now_ms: int, thresholds: Thresholds) -> bool:
    """An idle agent's own status line older than the TTL.

    A Janitor-maintained label has its own validity window and is left to it.
    A status with no recorded time is not guessed at.
    """
    ttl = int(thresholds.custom_status_ttl_ms)
    return (ttl > 0 and bool(status.strip()) and not janitor_owned
            and status_at is not None and activity.idle
            and now_ms - int(status_at) > ttl)
