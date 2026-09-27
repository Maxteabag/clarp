"""Permanent learned explanations and the decision ledger. Owns both tables.

`tool_explanation_learned` keeps one explanation per call shape (see
`tool_explanation_shapes`) and audience, with no expiry. A row is either a
parameterised sentence whose `{slot}` placeholders are filled from the next
call with the same shape, an exact-only sentence keyed by the hash of one
identical part (`parameterised = 0`), or a Jev pick of a scripted template
(`template_id`, `level = 0`: it renders at every audience). A row applies only
while its `prompt_version` and `templates_version` are current; otherwise it is
ignored and eventually pruned. `revoke()` removes one signature. Raw tool input
is never stored here: signatures are keyed hashes and the text has its dynamic
values replaced by placeholders, except in an exact-only row, which holds the
same explanation text a client was shown.

`tool_explanation_decisions` records how every lookup was answered, one row per
explained call: `template`, `learned`, `exact_cache`, `jev`, `llm`, `failed`,
`miss` (not admitted: queue full or too many views) or `disabled`. Rows are
buffered in memory by `Ledger` and written in the explanation worker's batch
transaction, or at most once a second when idle, never per request. Maintenance
prunes them after 90 days and above 500,000 rows.
"""
from __future__ import annotations

# Defined before the local imports: db_schema imports SCHEMA while `db` is
# still loading, so it must exist before this module imports `db`.
SCHEMA = """
CREATE TABLE IF NOT EXISTS tool_explanation_learned (
    signature TEXT NOT NULL,
    level INTEGER NOT NULL,
    template_text TEXT NOT NULL DEFAULT '',
    template_id TEXT NOT NULL DEFAULT '',
    argument_index INTEGER,
    slot_names TEXT NOT NULL DEFAULT '[]',
    program TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL DEFAULT '',
    producer TEXT NOT NULL,
    parameterised INTEGER NOT NULL DEFAULT 1,
    confidence REAL,
    prompt_version INTEGER NOT NULL,
    templates_version INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    last_hit_at INTEGER,
    hits INTEGER NOT NULL DEFAULT 0,
    verified INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (signature, level)
);
CREATE INDEX IF NOT EXISTS tool_explanation_learned_program ON tool_explanation_learned(program, level);
CREATE TABLE IF NOT EXISTS tool_explanation_decisions (
    id INTEGER PRIMARY KEY,
    at INTEGER NOT NULL,
    agent_id TEXT NOT NULL DEFAULT '',
    signature TEXT NOT NULL DEFAULT '',
    tier TEXT NOT NULL,
    level INTEGER NOT NULL DEFAULT 0,
    parts INTEGER NOT NULL DEFAULT 1,
    jev_confidence REAL,
    latency_ms INTEGER NOT NULL DEFAULT 0,
    model TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS tool_explanation_decisions_at ON tool_explanation_decisions(at);
"""

import collections
import json
import threading

from . import db as _db

DECISION_MAX_AGE_MS = 90 * 24 * 60 * 60 * 1000
DECISION_MAX_ROWS = 500_000
# Exact-only rows can accumulate one per unique opaque command; parameterised
# rows are the point of learning and are never capped.
EXACT_MAX_ROWS = 100_000
STALE_VERSION_GRACE_MS = 30 * 24 * 60 * 60 * 1000
TIERS = ("template", "learned", "exact_cache", "jev", "llm", "failed", "miss", "disabled")
HIT_TIERS = ("template", "learned", "exact_cache")
# Tiers that are a lookup that wanted an explanation; `disabled` is not.
COUNTED_TIERS = tuple(t for t in TIERS if t != "disabled")
WINDOWS = {"24h": 24 * 60 * 60 * 1000, "7d": 7 * 24 * 60 * 60 * 1000, "30d": 30 * 24 * 60 * 60 * 1000}
BUCKETS = {"hour": 60 * 60 * 1000, "day": 24 * 60 * 60 * 1000}


_COLUMNS = ("signature", "level", "template_text", "template_id", "argument_index", "slot_names", "program",
            "action", "producer", "parameterised", "confidence", "prompt_version", "templates_version",
            "created_at", "hits", "verified")


def lookup(signatures, level, prompt_version, templates_version, connection=None):
    """Current rows for these signatures at this audience (or any audience).

    Returns {signature: row}; a row for the exact audience wins over level 0.
    """
    signatures = sorted({s for s in signatures if s})
    if not signatures:
        return {}
    marks = ",".join("?" * len(signatures))
    rows = (connection or _db.conn()).execute(
        f"SELECT signature, level, template_text, template_id, argument_index, slot_names, producer, parameterised, "
        f"confidence, action FROM tool_explanation_learned WHERE signature IN ({marks}) AND level IN (?, 0) "
        f"AND prompt_version=? AND templates_version=?",
        (*signatures, level, prompt_version, templates_version)).fetchall()
    found = {}
    for row in sorted(rows, key=lambda r: r[1] == level):
        found[row[0]] = {"signature": row[0], "level": row[1], "template_text": row[2], "template_id": row[3],
                         "argument_index": row[4], "slot_names": json.loads(row[5] or "[]"), "producer": row[6],
                         "parameterised": bool(row[7]), "confidence": row[8], "action": row[9]}
    return found


def candidates(program, level, prompt_version, templates_version, limit=4):
    """Parameterised model explanations of the same program, most used first."""
    if not program or program == "?":
        return []
    rows = _db.conn().execute(
        "SELECT signature, template_text, slot_names FROM tool_explanation_learned WHERE program=? AND level=? "
        "AND producer='llm' AND parameterised=1 AND prompt_version=? AND templates_version=? "
        "ORDER BY hits DESC, created_at DESC LIMIT ?",
        (program, level, prompt_version, templates_version, limit)).fetchall()
    return [{"signature": r[0], "template_text": r[1], "slot_names": json.loads(r[2] or "[]")} for r in rows]


def store(connection, rows, now):
    """Insert or refresh learned rows inside the caller's transaction."""
    for row in rows:
        values = {"template_text": "", "template_id": "", "argument_index": None, "slot_names": "[]", "program": "",
                  "action": "", "parameterised": 1, "confidence": None, "hits": 0, "verified": 0,
                  **row, "created_at": now}
        connection.execute(
            f"INSERT INTO tool_explanation_learned({','.join(_COLUMNS)}) VALUES({','.join('?' * len(_COLUMNS))}) "
            "ON CONFLICT(signature, level) DO UPDATE SET template_text=excluded.template_text,"
            "template_id=excluded.template_id,argument_index=excluded.argument_index,slot_names=excluded.slot_names,"
            "program=excluded.program,action=excluded.action,producer=excluded.producer,"
            "parameterised=excluded.parameterised,confidence=excluded.confidence,"
            "prompt_version=excluded.prompt_version,templates_version=excluded.templates_version,"
            "created_at=excluded.created_at,verified=0",
            tuple(values[c] for c in _COLUMNS))


def write(connection, decisions, hits, now):
    """Buffered decisions and learned-row hit counts, in the caller's transaction."""
    if decisions:
        connection.executemany(
            "INSERT INTO tool_explanation_decisions(at,agent_id,signature,tier,level,parts,jev_confidence,"
            "latency_ms,model,reason) VALUES(?,?,?,?,?,?,?,?,?,?)",
            [(d["at"], d.get("agent_id") or "", d.get("signature", ""), d["tier"], d.get("level", 0),
              d.get("parts", 1), d.get("jev_confidence"), int(d.get("latency_ms", 0)), d.get("model", ""),
              d.get("reason", "")[:200]) for d in decisions])
    if hits:
        connection.executemany(
            "UPDATE tool_explanation_learned SET hits=hits+?, last_hit_at=? WHERE signature=? AND level=?",
            [(count, now, signature, level) for (signature, level), count in hits.items()])


def forget(connection, signature):
    """Delete one signature's rows inside the caller's transaction."""
    return connection.execute("DELETE FROM tool_explanation_learned WHERE signature=?", (signature,)).rowcount


def revoke(signature):
    """Forget every audience's learned explanation for one signature."""
    connection = _db.conn()
    return _db.retry_locked(lambda: forget(connection, signature))


def listing(limit=50):
    rows = _db.conn().execute(
        "SELECT signature, level, producer, parameterised, template_id, hits, created_at, last_hit_at, verified, "
        "prompt_version, templates_version FROM tool_explanation_learned ORDER BY hits DESC, created_at DESC LIMIT ?",
        (limit,)).fetchall()
    return [dict(zip(("signature", "level", "producer", "parameterised", "template_id", "hits", "created_at",
                      "last_hit_at", "verified", "prompt_version", "templates_version"), row)) for row in rows]


def prune(connection, now, prompt_version, templates_version):
    """Retention: old or excess decisions, dead-version and excess exact-only rows."""
    counts = {}
    counts["tool_explanation_decisions"] = connection.execute(
        "DELETE FROM tool_explanation_decisions WHERE at < ?", (now - DECISION_MAX_AGE_MS,)).rowcount
    counts["tool_explanation_decisions"] += connection.execute(
        "DELETE FROM tool_explanation_decisions WHERE id <= (SELECT id FROM tool_explanation_decisions "
        "ORDER BY id DESC LIMIT 1 OFFSET ?)", (DECISION_MAX_ROWS,)).rowcount
    counts["tool_explanation_learned"] = connection.execute(
        "DELETE FROM tool_explanation_learned WHERE (prompt_version<>? OR templates_version<>?) "
        "AND COALESCE(last_hit_at, created_at) < ?",
        (prompt_version, templates_version, now - STALE_VERSION_GRACE_MS)).rowcount
    counts["tool_explanation_learned"] += connection.execute(
        "DELETE FROM tool_explanation_learned WHERE parameterised=0 AND rowid IN (SELECT rowid FROM "
        "tool_explanation_learned WHERE parameterised=0 ORDER BY COALESCE(last_hit_at, created_at) DESC "
        "LIMIT -1 OFFSET ?)", (EXACT_MAX_ROWS,)).rowcount
    return counts


# ---- metrics -----------------------------------------------------------------

def stats(window="24h", bucket="hour", *, now=None, prompt_version, templates_version):
    """Per-bucket counts by tier and the hit rate, plus learning totals.

    Buckets are aligned to UTC epoch multiples of their size. `hit_rate` is
    (template + learned + exact_cache) / lookups, where lookups exclude
    `disabled`; it is null for a bucket without lookups.
    """
    if window not in WINDOWS:
        raise ValueError("window must be one of " + ", ".join(WINDOWS))
    if bucket not in BUCKETS:
        raise ValueError("bucket must be one of " + ", ".join(BUCKETS))
    now = _db.now_ms() if now is None else int(now)
    size = BUCKETS[bucket]
    first = (now - WINDOWS[window]) // size * size + size
    connection = _db.conn()
    rows = connection.execute(
        "SELECT (at / ?) * ? AS bucket, tier, count(*), COALESCE(sum(latency_ms), 0) FROM tool_explanation_decisions "
        "WHERE at >= ? AND at <= ? GROUP BY bucket, tier", (size, size, first, now)).fetchall()
    counts = collections.defaultdict(lambda: dict.fromkeys(TIERS, 0))
    latency = collections.defaultdict(int)
    for start, tier, count, total_latency in rows:
        if tier in TIERS:
            counts[start][tier] += count
            latency[start] += total_latency
    buckets = []
    total = dict.fromkeys(TIERS, 0)
    for start in range(first, now // size * size + 1, size):
        entry = counts.get(start, dict.fromkeys(TIERS, 0))
        buckets.append({"start": start, "counts": entry, **_rate(entry),
                        "mean_latency_ms": _mean(latency.get(start, 0), entry)})
        for tier, count in entry.items():
            total[tier] += count
    learned = connection.execute(
        "SELECT producer, parameterised, count(*), COALESCE(sum(hits), 0) FROM tool_explanation_learned "
        "WHERE prompt_version=? AND templates_version=? GROUP BY producer, parameterised",
        (prompt_version, templates_version)).fetchall()
    by_producer = collections.Counter()
    parameterised = exact = learned_hits = 0
    for producer, is_parameterised, count, hits in learned:
        by_producer[producer] += count
        learned_hits += hits
        if is_parameterised:
            parameterised += count
        else:
            exact += count
    return {"window": window, "bucket": bucket, "now": now, "tiers": list(TIERS), "buckets": buckets,
            "totals": {"counts": total, **_rate(total),
                       "learned_entries": parameterised + exact, "learned_parameterised": parameterised,
                       "learned_exact_only": exact, "learned_by_producer": dict(by_producer),
                       "learned_hits": learned_hits, "jev_picks": total["jev"], "llm_calls": total["llm"]}}


def _rate(counts):
    lookups = sum(counts[t] for t in COUNTED_TIERS)
    hits = sum(counts[t] for t in HIT_TIERS)
    return {"lookups": lookups, "hits": hits, "hit_rate": round(hits / lookups, 4) if lookups else None}


def _mean(total_latency, counts):
    answered = sum(counts.values())
    return round(total_latency / answered) if answered else None


# ---- the in-memory buffer ----------------------------------------------------

class Ledger:
    """Decisions, learned hits and Jev evidence waiting for the next batch write.

    Owned by one ToolExplanations service. Bounded: past `capacity` pending
    decisions the oldest are dropped and counted, so a stuck database cannot
    grow memory. A crash loses at most the unflushed second.
    """

    def __init__(self, capacity=20_000):
        self._lock = threading.Lock()
        self._capacity = capacity
        self._decisions = collections.deque()
        self._hits = collections.Counter()
        self._evidence = collections.deque(maxlen=256)
        self._quiet = collections.OrderedDict()
        self.dropped = 0

    def record(self, tier, **fields):
        with self._lock:
            if len(self._decisions) >= self._capacity:
                self._decisions.popleft()
                self.dropped += 1
            self._decisions.append({"at": _db.now_ms(), "tier": tier, **fields})

    def hit(self, signature, level):
        with self._lock:
            self._hits[(signature, level)] += 1

    def evidence(self, route, template_id, confidence, activity):
        with self._lock:
            self._evidence.append((route, template_id, confidence, activity))

    def quiet(self, key, seconds, now):
        """Suppress repeat decisions for `key` (a delivered or busy item) for a while."""
        with self._lock:
            self._quiet[key] = now + seconds * 1000
            self._quiet.move_to_end(key)
            while len(self._quiet) > 4096:
                self._quiet.popitem(last=False)

    def quieted(self, key, now, *, consume=False):
        with self._lock:
            until = self._quiet.get(key)
            if until is None:
                return False
            if until <= now or consume:
                del self._quiet[key]
            return until > now

    def pending(self):
        with self._lock:
            return bool(self._decisions or self._hits or self._evidence)

    def drain(self):
        with self._lock:
            decisions, hits, evidence = list(self._decisions), self._hits, list(self._evidence)
            self._decisions.clear()
            self._hits = collections.Counter()
            self._evidence.clear()
        return decisions, hits, evidence

    def restore(self, decisions, hits):
        """Put back what a failed write drained, oldest first, within capacity."""
        with self._lock:
            self._decisions.extendleft(reversed(decisions))
            while len(self._decisions) > self._capacity:
                self._decisions.popleft()
                self.dropped += 1
            self._hits.update(hits)
