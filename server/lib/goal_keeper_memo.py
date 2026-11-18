"""Process-local inputs to the goal keeper's decision pass."""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass

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


@dataclass(frozen=True)
class MessageFacts:
    """Unlabelled facts for one agent at one exact cursor; no message text."""
    any_messages: bool
    newest_ms: int
    finals: tuple[int, ...]
    sends: tuple[tuple[int, str], ...]


def message_facts(rows):
    newest, present, finals, sends = 0, False, [], []
    for row in rows:
        at = int(row["updated_at"])
        newest, present = max(newest, at), True
        if row["role"] == "assistant" and row["phase"] == "final":
            finals.append(at)
        elif row["role"] == "user" and row["sender_agent_id"]:
            sends.append((at, row["sender_agent_id"]))
    return MessageFacts(present, newest, tuple(finals), tuple(sends))


def _head_token(con, agent):
    heads = tuple(tuple(row) for row in con.execute(
        "SELECT backend_session_id,revision,replace_revision FROM conversation_heads "
        "WHERE agent_id=? ORDER BY backend_session_id", (agent,)))
    if not heads:
        return None
    # A partial/old database can have both headed and headless sessions.
    # Seek to the next distinct session in the covering index. SELECT DISTINCT
    # walks every entry of a busy conversation merely to certify an idle hit.
    sessions = {s for s, _, _ in heads}
    if con.execute("SELECT 1 FROM messages WHERE agent_id=? AND backend_session_id IS NULL LIMIT 1",
                   (agent,)).fetchone():
        return None
    row = con.execute("SELECT MIN(backend_session_id) FROM messages WHERE agent_id=? "
                      "AND backend_session_id IS NOT NULL", (agent,)).fetchone()
    while row[0] is not None:
        if row[0] not in sessions:
            return None
        row = con.execute("SELECT MIN(backend_session_id) FROM messages WHERE agent_id=? "
                          "AND backend_session_id>?", (agent, row[0])).fetchone()
    return heads


class KeeperMemo:
    """One keeper configuration's decisions, with a fresh read transaction per pass."""

    def __init__(self):
        self.plans = {}
        self.passes = 0
        self.messages = {}
        self.members = {}
        self.binding = None

    def clear(self):
        self.plans.clear()
        self.messages.clear()
        self.members.clear()

    def evaluate(self, config, progress, now, decide):
        from .log import log

        con = db.conn()
        if con.in_transaction:
            return decide(config, progress, now, None)
        if self.binding != (db.DB_PATH, con):
            self.clear()
            self.binding = (db.DB_PATH, con)
        self.heads = {}
        self.live_members = {}
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
            agents = {agent for members in self.live_members.values() for agent in members}
            self.messages = {a: entry for a, entry in self.messages.items() if a in agents}
            self.members = self.live_members
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

    def facts(self, con, pid, agents, since, full, read):
        old = self.members.get(pid, ())
        self.live_members[pid] = tuple(agents)
        actual = rows = None
        if self.check:
            # Use the original batched query once, sharing its result between
            # validation, misses and authoritative decisions on this pass.
            grouped = {a: [] for a in agents}
            rows = list(read(agents))
            for row in rows:
                grouped[row["agent_id"]].append(row)
            actual = {a: message_facts(grouped[a]) for a in agents}
        predicted = {}
        for a in agents:
            if a not in self.heads:
                self.heads[a] = _head_token(con, a)
            token = self.heads[a]
            key = (since, token)
            entry = self.messages.get(a)
            if not full and token and a in old and entry and key == entry[0]:
                facts = entry[1]
            elif actual is not None:
                facts = actual[a]
            else:
                facts = message_facts(read([a]))
            predicted[a] = facts
            if token and not full and len(facts.finals) + len(facts.sends) <= 2048:
                self.messages[a] = (key, facts)
            else:
                self.messages.pop(a, None)
        if actual is not None and predicted != actual:
            self.differences.setdefault(pid, set()).add("messages")
        return predicted, rows

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
