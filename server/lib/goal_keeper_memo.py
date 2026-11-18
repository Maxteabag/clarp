"""Process-local inputs to the goal keeper's decision pass."""
from __future__ import annotations

import copy
import json

from . import db, janitors


def project(row):
    """Only fields used by the decision pass. Drop history, documents and prompt text."""
    raw = row["goal_json"]
    if raw == "{}":
        return None
    goal = json.loads(raw)
    updater = goal.get("updater") or {}
    checkpoint = goal.get("checkpoint") or {}
    continuation = goal.get("continuation") or {}
    return {
        "checkpoint": {"at": checkpoint.get("at")},
        "updater": {key: updater.get(key) for key in ("through_ms", "through_event", "progress")}
                   if updater else {},
        "continuation": {key: continuation.get(key) for key in ("state", "due_at")},
    }


class KeeperMemo:
    """One keeper configuration's decisions, with a fresh read transaction per pass."""

    def __init__(self):
        self.plans = {}
        self.passes = 0

    def clear(self):
        self.plans.clear()

    def evaluate(self, config, progress, now, decide):
        from .log import log

        con = db.conn()
        self.passes += 1
        self.check = self.passes % 20 == 0
        self.differences = {}
        attempt = copy.deepcopy(progress)
        owned = not con.in_transaction
        try:
            if owned:
                con.execute("BEGIN")
            ready = decide(config, attempt, now, self)
            if owned:
                con.execute("COMMIT")
        except Exception as exc:  # noqa: BLE001 - discard the partial pass and use the original reader
            if owned and con.in_transaction:
                con.execute("ROLLBACK")
            self.clear()
            log("goalKeeperMemoFallback", f"error_type={type(exc).__name__}")
            return decide(config, progress, now, None)
        if self.differences:
            fields = sorted(set().union(*self.differences.values()))
            log("goalKeeperMemoMismatch", f"goals={len(self.differences)} fields={','.join(fields)}")
            self.clear()
        progress.clear()
        progress.update(attempt)
        return ready

    def rows(self, config, sweeping, plain):
        con = db.conn()
        headers = con.execute(
            """SELECT p.plan_id,p.agent_id,p.status,p.created_at,p.revision,p.updated_at,
                      (SELECT MAX(event_id) FROM goal_events e WHERE e.plan_id=p.plan_id) AS event_id,
                      a.is_janitor,a.archived_at,a.deleted_at
                 FROM task_plans p INDEXED BY idx_task_plans_recovery
                 JOIN agents a ON a.agent_id=p.agent_id
                WHERE p.status IN ('active','paused') AND a.deleted_at IS NULL
                  AND a.archived_at IS NULL AND a.is_janitor=0 ORDER BY p.rowid""")
        # Materialize within the snapshot; narrow headers preserve capped batch order.
        scoped = [r for r in headers if janitors._in_scope(config.get("scope") or {}, dict(r))]
        truth = {r["plan_id"]: (r, project(r)) for r in plain(config)} if self.check else {}
        next_plans, result = {}, []
        for header in scoped:
            pid = header["plan_id"]
            token = (header["revision"], header["updated_at"], header["event_id"])
            entry = self.plans.get(pid)
            hit = entry is not None and entry[0] == token
            if self.check:
                value = truth[pid][1] if pid in truth else None
                if hit and entry[1] != value:
                    self.differences[pid] = self._field_classes(entry[1], value)
            elif hit and not sweeping:
                value = entry[1]
            else:
                value = project(con.execute("SELECT goal_json FROM task_plans WHERE plan_id=?", (pid,)).fetchone())
            next_plans[pid] = token, value
            if value is not None:
                result.append({k: header[k] for k in ("plan_id", "agent_id", "created_at", "status")}
                              | {"goal_json": json.dumps(value)})
        self.plans = next_plans
        if self.check:
            expected = list(truth)
            found = [r["plan_id"] for r in result]
            if found != expected:
                for pid in set(found) ^ set(expected) or set(found):
                    self.differences.setdefault(pid, set()).add("membership")
            return [row for row, _ in truth.values()]
        return result

    @staticmethod
    def _field_classes(old, new):
        if old is None or new is None:
            return {"membership"}
        classes = set()
        if (old["checkpoint"] != new["checkpoint"] or bool(old["updater"]) != bool(new["updater"])
                or any(old["updater"].get(k) != new["updater"].get(k) for k in ("through_ms", "through_event"))):
            classes.add("cursor")
        if (old["continuation"] != new["continuation"]
                or old["updater"].get("progress") != new["updater"].get("progress")):
            classes.add("stall")
        return classes
