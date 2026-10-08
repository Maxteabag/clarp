"""Pure adaptive-interval policy for interval-driven Janitors; no I/O.

Every interval-driven Janitor run ends with an activity: ``worked`` (it found
and did something), ``idle`` (nothing needed doing) or ``failed``. ``outcome``
reads it from a finished run; ``advance`` turns it into the next interval:

* idle doubles the interval, from the Janitor's configured base, up to its
  ``max_interval_seconds`` (24 h by default);
* worked resets it to the base;
* failed leaves it as it is (failures keep their own handling and backoff).

A run that does not report counts as worked, so a Janitor that forgets the
contract keeps its configured cadence instead of drifting to a day; callers
log it. The caller persists ``state`` (``JanitorRunner`` progress for
task-label attachments, settings for the autonomy Janitors), so a restart
keeps the current interval. See docs/janitor-runner.md, "Adaptive intervals".
"""
from __future__ import annotations

ACTIVITIES = ("worked", "idle", "failed")
DEFAULT_MAX_INTERVAL_SECONDS = 86_400
MAX_INTERVAL_LIMIT_SECONDS = 7 * 86_400
SUMMARY_LIMIT = 300
# The run statuses/outcomes that are failures whatever the Janitor reported.
FAILED_RUN = frozenset({"failed", "error"})


def outcome(run: dict) -> tuple[str, str, bool]:
    """``(activity, summary, reported)`` for one finished run.

    A failed run is ``failed``; otherwise the reported activity; a run that
    reported nothing is ``worked`` with ``reported`` False."""
    summary = str(run.get("activity_summary") or "")[:SUMMARY_LIMIT]
    reported = run.get("activity") in ACTIVITIES
    if run.get("status") in FAILED_RUN or run.get("outcome") in FAILED_RUN:
        return "failed", summary or str(run.get("error") or "")[:SUMMARY_LIMIT], reported
    if reported:
        return run["activity"], summary, True
    return "worked", summary, False


def bounds(base_seconds: int, max_seconds: int | None) -> tuple[int, int]:
    """The base and the cap; a cap of 0 or below the base keeps it fixed."""
    base = max(1, int(base_seconds))
    cap = DEFAULT_MAX_INTERVAL_SECONDS if max_seconds is None else int(max_seconds)
    return base, max(base, min(cap, MAX_INTERVAL_LIMIT_SECONDS))


def current(state: dict, *, base_seconds: int, max_seconds: int | None) -> int:
    """The interval to wait now, in seconds, without recording a run."""
    base, cap = bounds(base_seconds, max_seconds)
    if state.get("base_seconds") != base or state.get("max_seconds") != cap:
        return base
    return min(max(int(state.get("interval_seconds") or base), base), cap)


def advance(state: dict, *, activity: str, summary: str = "", base_seconds: int,
            max_seconds: int | None, now: int | None = None) -> int:
    """Record one finished run and return the next interval in seconds.

    A changed base or cap (the Janitor was reconfigured) starts from the base."""
    if activity not in ACTIVITIES:
        raise ValueError(f"unknown Janitor activity: {activity!r}")
    base, cap = bounds(base_seconds, max_seconds)
    interval = current(state, base_seconds=base, max_seconds=cap)
    if state.get("base_seconds") != base or state.get("max_seconds") != cap:
        state["idle_streak"] = 0
    if activity == "idle":
        interval = min(interval * 2, cap)
        state["idle_streak"] = int(state.get("idle_streak") or 0) + 1
    elif activity == "worked":
        interval = base
        state["idle_streak"] = 0
    state.update(base_seconds=base, max_seconds=cap, interval_seconds=interval,
                 last_activity=activity, last_summary=str(summary or "")[:SUMMARY_LIMIT])
    if now is not None:
        state["last_run_at"] = int(now)
    return interval


def cadence(state: dict, *, base_seconds: int, max_seconds: int | None,
            next_run_at: int | None, lane: str = "") -> dict:
    """What `clarp-admin janitor list` shows for one schedule lane."""
    base, cap = bounds(base_seconds, max_seconds)
    return {"lane": lane, "base_interval_seconds": base,
            "current_interval_seconds": current(state, base_seconds=base, max_seconds=cap),
            "max_interval_seconds": cap, "adaptive": cap > base,
            "idle_streak": int(state.get("idle_streak") or 0) if state.get("base_seconds") == base else 0,
            "last_activity": state.get("last_activity"), "last_summary": state.get("last_summary") or "",
            "next_run_at": next_run_at}
