"""Pure activity-gated schedule policy; progress is persisted by JanitorRunner."""

DEFAULTS = {"interval_seconds": 900, "idle_timeout_seconds": 300,
            "run_on_resume": True, "max_targets": 3, "coalesce_seconds": 0}


def advance(state: dict, config: dict, *, now: int, active: bool) -> bool:
    """Return one due occurrence; never accumulate missed/inactive ticks."""
    interval = config.get("interval_seconds", 900) * 1000
    was_active = state.get("application_active", False)
    state["application_active"] = active
    if not active:
        state["next_run_at"] = None
        state["pending"] = {}
        return False
    if not was_active:
        earliest = state.get("last_check_at", now - interval) + interval
        state["next_run_at"] = max(now, earliest) if config.get("run_on_resume", True) else now + interval
    due = state.get("next_run_at")
    if due is None:
        state["next_run_at"] = now + interval
        return False
    if now < due:
        return False
    state["next_run_at"] = now + interval
    state["last_check_at"] = now
    return True
