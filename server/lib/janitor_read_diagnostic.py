"""Opt-in attribution of read syscalls on the janitor-autonomy thread."""
from __future__ import annotations

from collections import Counter
from pathlib import Path

from .log import log

LANES = ("setup", "heartbeat", "quota", "hotseat", "labels", "keeper")
FAMILIES = ("plan_headers", "plan_points", "plain_plans", "heads", "helpers", "messages",
            "stall_max", "jobs", "events", "owner_state", "other")


def thread_rchar():
    raw = Path("/proc/thread-self/io").read_bytes()
    return int(raw.splitlines()[0].split()[1]), len(raw)


def _family(sql):
    sql = sql.lower()
    if "idx_task_plans_recovery" in sql:
        return "plan_headers"
    if "from task_plans" in sql:
        return "plain_plans" if "p.*" in sql else "plan_points"
    if "from conversation_heads" in sql or "min(backend_session_id)" in sql or "backend_session_id is null" in sql:
        return "heads"
    if "from messages" in sql:
        return "stall_max" if "max(updated_at)" in sql else "messages"
    if "from agents" in sql:
        return "helpers"
    if "from background_jobs" in sql:
        return "jobs"
    if "from goal_events" in sql:
        return "events"
    if "from state_log" in sql:
        return "owner_state"
    return "other"


class _Cursor:
    def __init__(self, cursor, measure):
        self.cursor, self.measure = cursor, measure

    def fetchone(self):
        return self.measure(self.cursor.fetchone)

    def fetchall(self):
        return self.measure(self.cursor.fetchall)

    def __iter__(self):
        while batch := self.measure(lambda: self.cursor.fetchmany(128)):
            yield from batch


class ReadConnection:
    """Only the decision reader's SELECTs; never replaces the store's connection."""
    def __init__(self, connection, meter):
        self.connection, self.meter = connection, meter

    def execute(self, sql, args=()):
        family = _family(sql)
        def measure(fn):
            return self.meter.measure(self.meter.families, family, fn)
        return _Cursor(measure(lambda: self.connection.execute(sql, args)), measure)


class ReadMeter:
    def __init__(self):
        self.passes = self.observer_bytes = 0
        self.lanes, self.families = Counter(), Counter()
        self.worked = set()
        self.current = None
        self.sweep = self.self_check = self.unavailable = False

    def _sample(self):
        try:
            observed, size = thread_rchar()
        except (OSError, ValueError, IndexError):
            self.unavailable = True
            return None
        adjusted = observed - self.observer_bytes
        self.observer_bytes += size
        return adjusted

    def measure(self, counter, key, fn):
        start = self._sample()
        try:
            return fn()
        finally:
            end = self._sample()
            if start is not None and end is not None:
                counter[key] += max(0, end - start)

    def work(self):
        if self.current is not None:
            self.worked.add(self.current)

    def lane(self, name, fn):
        self.current = name
        try:
            return self.measure(self.lanes, name, fn)
        finally:
            self.current = None

    def finish_pass(self):
        self.passes += 1
        if self.passes < 20:
            return
        def mb(counter, names):
            return {k: round(counter[k] / 1_000_000, 4) for k in names}
        remainder = max(0, self.lanes["keeper"] - sum(self.families.values()))
        detail = {"keeper_other_mb": round(remainder / 1_000_000, 4), "passes": self.passes, "lane_mb": mb(self.lanes, LANES),
                  "keeper_family_mb": mb(self.families, FAMILIES), "worked": sorted(self.worked),
                  "sweep": self.sweep, "self_check": self.self_check,
                  "sample_unavailable": self.unavailable}
        self.__init__()
        log("janitorAutonomyReads", detail)
