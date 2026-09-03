"""Durable agent-bound scheduled jobs for recurring autonomous session turns."""
from __future__ import annotations

import calendar
from datetime import datetime, timezone
import json
import logging
import threading
import time
import uuid
from typing import Any, Callable

from . import agents as agents_db
from .db import conn, now_ms
from .log import log, log_exception

logger = logging.getLogger(__name__)

CRON_SHORTCUTS = {
    "@hourly": "0 * * * *",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@weekly": "0 0 * * 0",
    "@monthly": "0 0 1 * *",
}


def _parse_cron_field(field_str: str, min_val: int, max_val: int) -> set[int]:
    """Parse a single cron field into a set of matching integers."""
    result: set[int] = set()
    parts = field_str.strip().split(",")
    for part in parts:
        part = part.strip()
        if not part:
            continue
        if "/" in part:
            subparts = part.split("/", 1)
            step = int(subparts[1])
            if step <= 0:
                raise ValueError(f"Step must be > 0 in {part}")
            if subparts[0] == "*":
                start_val, end_val = min_val, max_val
            elif "-" in subparts[0]:
                r_start, r_end = subparts[0].split("-", 1)
                start_val, end_val = int(r_start), int(r_end)
            else:
                start_val = int(subparts[0])
                end_val = max_val
            for v in range(start_val, end_val + 1, step):
                if min_val <= v <= max_val:
                    result.add(v)
        elif "-" in part:
            r_start, r_end = part.split("-", 1)
            s_val, e_val = int(r_start), int(r_end)
            for v in range(s_val, e_val + 1):
                if min_val <= v <= max_val:
                    result.add(v)
        elif part == "*":
            result.update(range(min_val, max_val + 1))
        else:
            val = int(part)
            if min_val <= val <= max_val:
                result.add(val)
            else:
                raise ValueError(f"Value {val} out of range ({min_val}-{max_val})")
    if not result:
        raise ValueError(f"Empty or invalid field expression: {field_str}")
    return result


def parse_cron(expression: str) -> tuple[set[int], set[int], set[int], set[int], set[int]]:
    """Parse a 5-field cron expression into allowed values for (min, hour, dom, month, dow)."""
    expr = expression.strip()
    if expr in CRON_SHORTCUTS:
        expr = CRON_SHORTCUTS[expr]

    parts = expr.split()
    if len(parts) != 5:
        raise ValueError(f"Cron expression must have 5 fields, got {len(parts)}: {expression}")

    mins = _parse_cron_field(parts[0], 0, 59)
    hours = _parse_cron_field(parts[1], 0, 23)
    doms = _parse_cron_field(parts[2], 1, 31)
    months = _parse_cron_field(parts[3], 1, 12)
    # Day of week: 0-6 (0=Sun) or 7 (also Sun)
    raw_dows = _parse_cron_field(parts[4], 0, 7)
    dows = {0 if d == 7 else d for d in raw_dows}

    return mins, hours, doms, months, dows


def compute_next_run(expression: str, from_ms: int) -> int | None:
    """Compute the next run time in epoch milliseconds from from_ms."""
    mins, hours, doms, months, dows = parse_cron(expression)

    # Start at the beginning of the next minute
    from_sec = from_ms / 1000.0
    dt = datetime.fromtimestamp(from_sec, tz=timezone.utc)
    # Truncate seconds/microseconds and add 1 minute
    dt = dt.replace(second=0, microsecond=0)
    current_ts = int(dt.timestamp()) + 60

    # Search forward up to 366 days
    limit_ts = current_ts + 366 * 86400
    while current_ts < limit_ts:
        cand = datetime.fromtimestamp(current_ts, tz=timezone.utc)

        # Month check
        if cand.month not in months:
            # Advance to next month
            days_in_month = calendar.monthrange(cand.year, cand.month)[1]
            days_to_add = days_in_month - cand.day + 1
            current_ts += days_to_add * 86400 - (cand.hour * 3600 + cand.minute * 60)
            continue

        # Day of month & day of week check
        # Python weekday: Mon=0 ... Sun=6 -> standard cron dow: Sun=0, Mon=1...Sat=6
        cron_dow = (cand.weekday() + 1) % 7
        if cand.day not in doms or cron_dow not in dows:
            # Advance to next day at 00:00
            current_ts += 86400 - (cand.hour * 3600 + cand.minute * 60)
            continue

        # Hour check
        if cand.hour not in hours:
            # Advance to next hour at :00
            current_ts += 3600 - (cand.minute * 60)
            continue

        # Minute check
        if cand.minute in mins:
            return int(cand.timestamp() * 1000)

        current_ts += 60

    return None


def create_schedule(
    session: str,
    name: str,
    cron_expression: str,
    prompt: str,
    *,
    enabled: bool = True,
    schedule_id: str | None = None,
) -> dict[str, Any]:
    """Register a new scheduled job for an agent session."""
    agent = agents_db.get_by_session(session)
    if not agent:
        agent = agents_db.get_by_agent_id(session)
    if not agent:
        raise ValueError(f"Agent session {session!r} not found")

    # Validate cron
    parse_cron(cron_expression)

    sid = schedule_id or f"sched_{uuid.uuid4().hex[:12]}"
    now = now_ms()
    next_run = compute_next_run(cron_expression, now) if enabled else None

    with conn() as c:
        c.execute(
            """INSERT INTO agent_schedules (
                schedule_id, agent_id, session, name, cron_expression,
                prompt, enabled, last_run_at, next_run_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)""",
            (
                sid,
                agent["agent_id"],
                agent["session"],
                name.strip(),
                cron_expression.strip(),
                prompt.strip(),
                1 if enabled else 0,
                next_run,
                now,
                now,
            ),
        )

    return get_schedule(sid) or {}


def get_schedule(schedule_id: str) -> dict[str, Any] | None:
    with conn() as c:
        row = c.execute(
            """SELECT schedule_id, agent_id, session, name, cron_expression,
                      prompt, enabled, last_run_at, next_run_at, created_at, updated_at
                 FROM agent_schedules
                WHERE schedule_id = ?""",
            (schedule_id,),
        ).fetchone()
        if not row:
            return None
        return _row_to_dict(row)


def list_schedules(
    session: str | None = None, agent_id: str | None = None
) -> list[dict[str, Any]]:
    with conn() as c:
        if agent_id:
            rows = c.execute(
                """SELECT schedule_id, agent_id, session, name, cron_expression,
                          prompt, enabled, last_run_at, next_run_at, created_at, updated_at
                     FROM agent_schedules
                    WHERE agent_id = ?
                    ORDER BY created_at ASC""",
                (agent_id,),
            ).fetchall()
        elif session:
            rows = c.execute(
                """SELECT schedule_id, agent_id, session, name, cron_expression,
                          prompt, enabled, last_run_at, next_run_at, created_at, updated_at
                     FROM agent_schedules
                    WHERE session = ?
                    ORDER BY created_at ASC""",
                (session,),
            ).fetchall()
        else:
            rows = c.execute(
                """SELECT schedule_id, agent_id, session, name, cron_expression,
                          prompt, enabled, last_run_at, next_run_at, created_at, updated_at
                     FROM agent_schedules
                    ORDER BY session, created_at ASC"""
            ).fetchall()
        return [_row_to_dict(r) for r in rows]


def update_schedule(
    schedule_id: str,
    *,
    enabled: bool | None = None,
    name: str | None = None,
    cron_expression: str | None = None,
    prompt: str | None = None,
) -> dict[str, Any] | None:
    current = get_schedule(schedule_id)
    if not current:
        return None

    new_cron = cron_expression.strip() if cron_expression is not None else current["cron_expression"]
    new_enabled = enabled if enabled is not None else current["enabled"]
    new_name = name.strip() if name is not None else current["name"]
    new_prompt = prompt.strip() if prompt is not None else current["prompt"]

    if cron_expression is not None:
        parse_cron(new_cron)

    now = now_ms()
    next_run = current["next_run_at"]
    if new_enabled and (not current["enabled"] or cron_expression is not None):
        next_run = compute_next_run(new_cron, now)
    elif not new_enabled:
        next_run = None

    with conn() as c:
        c.execute(
            """UPDATE agent_schedules
                  SET name = ?, cron_expression = ?, prompt = ?, enabled = ?,
                      next_run_at = ?, updated_at = ?
                WHERE schedule_id = ?""",
            (new_name, new_cron, new_prompt, 1 if new_enabled else 0, next_run, now, schedule_id),
        )

    return get_schedule(schedule_id)


def delete_schedule(schedule_id: str) -> bool:
    with conn() as c:
        res = c.execute("DELETE FROM agent_schedules WHERE schedule_id = ?", (schedule_id,))
        return res.rowcount > 0


def due_schedules(current_ms: int | None = None) -> list[dict[str, Any]]:
    now = current_ms if current_ms is not None else now_ms()
    with conn() as c:
        rows = c.execute(
            """SELECT schedule_id, agent_id, session, name, cron_expression,
                      prompt, enabled, last_run_at, next_run_at, created_at, updated_at
                 FROM agent_schedules
                WHERE enabled = 1 AND next_run_at IS NOT NULL AND next_run_at <= ?
                ORDER BY next_run_at ASC""",
            (now,),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]


def advance_schedule(schedule_id: str, run_at_ms: int) -> None:
    sched = get_schedule(schedule_id)
    if not sched:
        return
    next_run = compute_next_run(sched["cron_expression"], run_at_ms)
    now = now_ms()
    with conn() as c:
        c.execute(
            """UPDATE agent_schedules
                  SET last_run_at = ?, next_run_at = ?, updated_at = ?
                WHERE schedule_id = ?""",
            (run_at_ms, next_run, now, schedule_id),
        )


def _row_to_dict(row: Any) -> dict[str, Any]:
    return {
        "schedule_id": row[0],
        "agent_id": row[1],
        "session": row[2],
        "name": row[3],
        "cron_expression": row[4],
        "prompt": row[5],
        "enabled": bool(row[6]),
        "last_run_at": row[7],
        "next_run_at": row[8],
        "created_at": row[9],
        "updated_at": row[10],
    }


class AgentScheduleRunner:
    """Periodic scheduler that evaluates and dispatches due agent tasks."""

    def __init__(
        self,
        dispatch_turn: Callable[[str, str], None],
        *,
        check_interval_sec: float = 15.0,
    ):
        self._dispatch_turn = dispatch_turn
        self._check_interval = check_interval_sec
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="agent-scheduler")
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)

    def _loop(self) -> None:
        while not self._stop_event.wait(self._check_interval):
            try:
                self.tick()
            except Exception as exc:
                log_exception("agentScheduleRunnerTickFail", exc)

    def tick(self) -> int:
        now = now_ms()
        due = due_schedules(now)
        dispatched_count = 0
        for item in due:
            sid = item["schedule_id"]
            session = item["session"]
            prompt = item["prompt"]
            # Mark schedule advanced so it will not double-fire
            advance_schedule(sid, now)
            try:
                self._dispatch_turn(session, prompt)
                dispatched_count += 1
            except Exception as exc:
                log_exception(f"scheduleDispatchFail:{sid}", exc)
        return dispatched_count
