"""Persistence for transcription context packs.

Thin CRUD over the v64 tables plus the audit write. Deliberately free of
ranking or budget logic - that lives in `vocab_budget` and stays testable
without a database.
"""

from __future__ import annotations

import json
import uuid

from .db import conn, now_ms
from .vocab_budget import CompileResult, Pack, Term

# Ordinary speech has no biasing value, and a term nobody said is not free -
# it costs budget that a rarer term could have used.
DEFAULT_PACK_PRIORITY = 1.0


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


# --- packs -----------------------------------------------------------------

def create_pack(name: str, *, kind: str = "static", generator: str = "",
                priority: float = DEFAULT_PACK_PRIORITY, floor: int = 0,
                enabled: bool = True) -> str:
    pack_id = _new_id("pk")
    ts = now_ms()
    conn().execute(
        "INSERT INTO vocab_packs"
        " (pack_id, name, kind, generator, priority, floor, enabled,"
        "  created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (pack_id, name.strip(), kind, generator, float(priority), int(floor),
         1 if enabled else 0, ts, ts))
    conn().commit()
    return pack_id


def list_packs() -> list[dict]:
    rows = conn().execute(
        "SELECT pack_id, name, kind, generator, priority, floor, enabled"
        " FROM vocab_packs ORDER BY name").fetchall()
    return [
        {"pack_id": r[0], "name": r[1], "kind": r[2], "generator": r[3],
         "priority": r[4], "floor": r[5], "enabled": bool(r[6])}
        for r in rows
    ]


def set_pack_enabled(pack_id: str, enabled: bool) -> None:
    conn().execute(
        "UPDATE vocab_packs SET enabled=?, updated_at=? WHERE pack_id=?",
        (1 if enabled else 0, now_ms(), pack_id))
    conn().commit()


def delete_pack(pack_id: str) -> None:
    conn().execute("DELETE FROM vocab_terms WHERE pack_id=?", (pack_id,))
    conn().execute("DELETE FROM vocab_packs WHERE pack_id=?", (pack_id,))
    conn().commit()


# --- terms -----------------------------------------------------------------

def add_term(pack_id: str, text: str, *, rarity: float = 0.5,
             say_as: str = "", often_heard_as: str = "",
             source: str = "manual") -> bool:
    """Insert a term. Returns False when the pack already holds it.

    Case-insensitive uniqueness is enforced by the schema: 'Clarp' and 'clarp'
    are the same biasing term and storing both would waste budget twice.
    """
    value = str(text or "").strip()
    if not value:
        return False
    cur = conn().execute(
        "INSERT OR IGNORE INTO vocab_terms"
        " (pack_id, text, say_as, often_heard_as, rarity, source, created_at)"
        " VALUES (?,?,?,?,?,?,?)",
        (pack_id, value, say_as.strip(), often_heard_as.strip(),
         float(rarity), source, now_ms()))
    conn().commit()
    return cur.rowcount > 0


def pack_terms(pack_id: str, pack_name: str) -> list[Term]:
    rows = conn().execute(
        "SELECT text, rarity, say_as, often_heard_as FROM vocab_terms"
        " WHERE pack_id=? ORDER BY rarity DESC", (pack_id,)).fetchall()
    return [
        Term(text=r[0], pack=pack_name, rarity=float(r[1]),
             say_as=(r[2] or None), confusable=bool((r[3] or "").strip()))
        for r in rows
    ]


def delete_term(term_id: int) -> None:
    conn().execute("DELETE FROM vocab_terms WHERE term_id=?", (term_id,))
    conn().commit()


# --- profiles --------------------------------------------------------------

def create_profile(name: str) -> str:
    profile_id = _new_id("pf")
    ts = now_ms()
    conn().execute(
        "INSERT INTO vocab_profiles (profile_id, name, created_at, updated_at)"
        " VALUES (?,?,?,?)", (profile_id, name.strip(), ts, ts))
    conn().commit()
    return profile_id


def add_pack_to_profile(profile_id: str, pack_id: str, position: int = 0) -> None:
    conn().execute(
        "INSERT OR REPLACE INTO vocab_profile_packs"
        " (profile_id, pack_id, position) VALUES (?,?,?)",
        (profile_id, pack_id, int(position)))
    conn().commit()


def assign_profile(profile_id: str, *, agent_id: str = "",
                   team_id: str = "") -> str:
    """Bind a profile to exactly one owner, replacing any previous binding."""
    agent = agent_id.strip() or None
    team = team_id.strip() or None
    if (agent is None) == (team is None):
        raise ValueError("assign to exactly one of agent_id or team_id")
    if agent:
        conn().execute("DELETE FROM vocab_assignments WHERE agent_id=?", (agent,))
    else:
        conn().execute("DELETE FROM vocab_assignments WHERE team_id=?", (team,))
    assignment_id = _new_id("as")
    conn().execute(
        "INSERT INTO vocab_assignments"
        " (assignment_id, profile_id, agent_id, team_id, created_at)"
        " VALUES (?,?,?,?,?)",
        (assignment_id, profile_id, agent, team, now_ms()))
    conn().commit()
    return assignment_id


def profile_for_agent(agent_id: str) -> str | None:
    row = conn().execute(
        "SELECT profile_id FROM vocab_assignments WHERE agent_id=?",
        (agent_id,)).fetchone()
    return row[0] if row else None


def profile_packs(profile_id: str) -> list[Pack]:
    """Load a profile's static packs, ordered, with their terms attached.

    Dynamic packs carry no stored terms - they are filled by generators at
    compile time - so they come back empty here by design.
    """
    rows = conn().execute(
        "SELECT p.pack_id, p.name, p.kind, p.priority, p.floor, p.enabled"
        " FROM vocab_profile_packs pp"
        " JOIN vocab_packs p ON p.pack_id = pp.pack_id"
        " WHERE pp.profile_id=? ORDER BY pp.position, p.name",
        (profile_id,)).fetchall()
    packs: list[Pack] = []
    for pack_id, name, kind, priority, floor, enabled in rows:
        terms = tuple(pack_terms(pack_id, name)) if kind == "static" else ()
        packs.append(Pack(
            name=name, terms=terms, priority=float(priority),
            floor=int(floor), enabled=bool(enabled)))
    return packs


# --- audit -----------------------------------------------------------------

def record_run(result: CompileResult, *, provider: str, model: str,
               agent_id: str = "", session: str = "", trace_id: str = "",
               profile_id: str | None = None, transcript: str = "",
               latency_ms: int = 0) -> int:
    """Persist one compile so a transcript can be traced back to its prompt.

    Never raises into the transcription path: losing an audit row is bad, but
    failing a user's turn because of one is worse.
    """
    audit = result.audit()
    cur = conn().execute(
        "INSERT INTO vocab_runs"
        " (agent_id, session, trace_id, profile_id, provider, model, unit,"
        "  capacity, used, form, rarity_floor, payload, included_json,"
        "  dropped_json, transcript, latency_ms, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (agent_id or None, session, trace_id, profile_id, provider, model,
         audit["unit"], audit["capacity"], audit["used"], audit["form"],
         audit["rarity_floor"], audit["payload"],
         json.dumps(audit["included"]), json.dumps(audit["dropped"]),
         transcript, int(latency_ms), now_ms()))
    conn().commit()
    return int(cur.lastrowid)


def recent_runs(limit: int = 20, *, session: str = "") -> list[dict]:
    sql = ("SELECT run_id, session, provider, model, unit, capacity, used,"
           " form, payload, included_json, dropped_json, transcript, created_at"
           " FROM vocab_runs")
    params: tuple = ()
    if session:
        sql += " WHERE session=?"
        params = (session,)
    sql += " ORDER BY run_id DESC LIMIT ?"
    rows = conn().execute(sql, params + (int(limit),)).fetchall()
    return [
        {"run_id": r[0], "session": r[1], "provider": r[2], "model": r[3],
         "unit": r[4], "capacity": r[5], "used": r[6], "form": r[7],
         "payload": r[8], "included": json.loads(r[9] or "[]"),
         "dropped": json.loads(r[10] or "[]"), "transcript": r[11],
         "created_at": r[12]}
        for r in rows
    ]


def run_for_trace(trace_id: str) -> dict | None:
    """The 'what was sent' lookup behind a single transcript."""
    if not trace_id:
        return None
    row = conn().execute(
        "SELECT run_id, session, provider, model, unit, capacity, used,"
        " form, payload, included_json, dropped_json, transcript, created_at"
        " FROM vocab_runs WHERE trace_id=? ORDER BY run_id DESC LIMIT 1",
        (trace_id,)).fetchone()
    if not row:
        return None
    return {
        "run_id": row[0], "session": row[1], "provider": row[2],
        "model": row[3], "unit": row[4], "capacity": row[5], "used": row[6],
        "form": row[7], "payload": row[8],
        "included": json.loads(row[9] or "[]"),
        "dropped": json.loads(row[10] or "[]"),
        "transcript": row[11], "created_at": row[12],
    }
