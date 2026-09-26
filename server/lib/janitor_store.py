"""The janitor_* tables' only writer (rule 5 of state-and-boundaries.md).

`janitors.py` and `janitor_builtins.py` hold the maintenance logic: what a
run may admit, when a configuration is stale, which label is protected. This
module holds every statement those decisions end in. Functions take an
optional connection `c` so a caller inside `write()` reads and writes through
the same BEGIN IMMEDIATE transaction; without one they use this thread's
autocommit connection.

Writes to neighbouring tables that must commit in the janitor transaction
(the agent's janitor flag and status, the built-in identity row, cancelled
queued turns) live in their owners, `agents.py` and `turn_queue.py`, as
functions that take the same connection. The few reads of those tables that
remain here are lookups inside that transaction.
"""
from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from contextlib import contextmanager

from . import db

MAX_PAYLOAD_BYTES = 1024 * 1024
ACTIVE_STATUSES = ("queued", "running")
_ACTIVE = "status IN ('queued','running')"


class JanitorError(ValueError):
    """Raised by the store for payload limits and by janitors.py for policy."""

    def __init__(self, message: str, status: int = 400, code: str = "invalid_janitor"):
        super().__init__(message)
        self.status = status
        self.code = code


def encode(value) -> str:
    """Canonical JSON for stored payloads; refuses anything over 1 MiB."""
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if len(encoded.encode()) > MAX_PAYLOAD_BYTES:
        raise JanitorError("Maintenance configuration is too large")
    return encoded


def decode(value, fallback):
    return json.loads(value) if value else fallback


@contextmanager
def write() -> Iterator:
    """One BEGIN IMMEDIATE transaction on this thread's connection."""
    c = db.conn()
    c.execute("BEGIN IMMEDIATE")
    try:
        yield c
        c.execute("COMMIT")
    except BaseException:
        c.execute("ROLLBACK")
        raise


def _c(c):
    return c if c is not None else db.conn()


# ---- trigger definitions ----------------------------------------------------

def trigger_definition_rows(c=None) -> list:
    return list(_c(c).execute("SELECT * FROM janitor_trigger_definitions ORDER BY trigger_id, version"))


def ensure_trigger_definition(c, trigger_id: str, name: str, *, kind: str = "demand",
                              version: int = 1, defaults: str = "{}") -> None:
    c.execute("INSERT OR IGNORE INTO janitor_trigger_definitions(trigger_id,version,name,kind,defaults_json) VALUES (?,?,?,?,?)",
              (trigger_id, version, name, kind, defaults))


# ---- builtins ----------------------------------------------------------------

def builtin_role(agent_id: str, c=None) -> str | None:
    row = _c(c).execute("SELECT role FROM janitor_builtins WHERE agent_id=?", (agent_id,)).fetchone()
    return row[0] if row else None


def builtin_agent_id(role: str, c=None) -> str | None:
    row = _c(c).execute("SELECT agent_id FROM janitor_builtins WHERE role=?", (role,)).fetchone()
    return row[0] if row else None


def register_builtin(c, role: str, agent_id: str, seed_version: int, now: int) -> None:
    c.execute("INSERT INTO janitor_builtins(role,agent_id,seed_version,created_at) VALUES (?,?,?,?)",
              (role, agent_id, seed_version, now))


# ---- configs -----------------------------------------------------------------

def config_row(agent_id: str, c=None):
    return _c(c).execute("SELECT * FROM janitor_configs WHERE agent_id=?", (agent_id,)).fetchone()


def has_config(agent_id: str, c=None) -> bool:
    return bool(_c(c).execute("SELECT 1 FROM janitor_configs WHERE agent_id=?", (agent_id,)).fetchone())


def insert_config(c, agent_id: str, template_id: str, *, scope, execution, options, now: int,
                  enabled: bool | None = None) -> None:
    if enabled is None:
        c.execute("INSERT INTO janitor_configs(agent_id,template_id,scope_json,execution_json,options_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                  (agent_id, template_id, encode(scope), encode(execution), encode(options), now, now))
    else:
        c.execute("""INSERT INTO janitor_configs(agent_id,template_id,enabled,scope_json,execution_json,options_json,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?)""", (agent_id, template_id, int(enabled), encode(scope), encode(execution), encode(options), now, now))


def replace_config_settings(c, agent_id: str, *, template_id: str, scope, execution, options, now: int) -> None:
    """A new generation and revision, paused, with the error cleared."""
    c.execute("UPDATE janitor_configs SET enabled=0,generation=generation+1,revision=revision+1,template_id=?,scope_json=?,execution_json=?,options_json=?,last_error='',updated_at=? WHERE agent_id=?",
              (template_id, encode(scope), encode(execution), encode(options), now, agent_id))


def replace_config_options(c, agent_id: str, options, now: int) -> None:
    c.execute("UPDATE janitor_configs SET options_json=?,generation=generation+1,revision=revision+1,updated_at=? WHERE agent_id=?",
              (encode(options), now, agent_id))


def set_config_enabled(c, agent_id: str, enabled: bool, now: int) -> None:
    c.execute("UPDATE janitor_configs SET enabled=?,generation=generation+1,revision=revision+1,last_error='',updated_at=? WHERE agent_id=?",
              (int(enabled), now, agent_id))


def bump_config(c, agent_id: str, now: int, *, disable: bool = False) -> None:
    """Advance generation and revision; optionally pause in the same statement."""
    if disable:
        c.execute("UPDATE janitor_configs SET enabled=0,generation=generation+1,revision=revision+1,updated_at=? WHERE agent_id=?", (now, agent_id))
    else:
        c.execute("UPDATE janitor_configs SET generation=generation+1,revision=revision+1,updated_at=? WHERE agent_id=?", (now, agent_id))


def set_config_generation(c, agent_id: str, generation: int, now: int) -> None:
    c.execute("UPDATE janitor_configs SET generation=?,revision=revision+1,updated_at=? WHERE agent_id=?", (generation, now, agent_id))


def record_config_run(c, agent_id: str, now: int, error: str = "", *, touch_updated: bool = True) -> None:
    """Last run time and error; deterministic bookkeeping leaves updated_at alone."""
    if touch_updated:
        c.execute("UPDATE janitor_configs SET last_run_at=?,last_error=?,updated_at=? WHERE agent_id=?", (now, error, now, agent_id))
    else:
        c.execute("UPDATE janitor_configs SET last_run_at=?,last_error=? WHERE agent_id=?", (now, error, agent_id))


def record_config_change(c, agent_id: str, now: int) -> None:
    c.execute("UPDATE janitor_configs SET last_change_at=? WHERE agent_id=?", (now, agent_id))


def other_enabled_configs(agent_id: str, c=None) -> list:
    return list(_c(c).execute("SELECT j.* FROM janitor_configs j JOIN agents a ON a.agent_id=j.agent_id WHERE j.enabled=1 AND j.agent_id!=? AND a.deleted_at IS NULL AND a.archived_at IS NULL", (agent_id,)))


def janitor_agent_ids(c=None) -> list[str]:
    return [r[0] for r in _c(c).execute("SELECT a.agent_id FROM agents a JOIN janitor_configs j ON j.agent_id=a.agent_id WHERE a.is_janitor=1 AND a.deleted_at IS NULL AND a.archived_at IS NULL ORDER BY a.persona")]


def agent_config_row(agent_id: str, c=None):
    """The agent's backend/model/effort next to its configuration, one row."""
    return _c(c).execute("""SELECT a.backend,a.model,a.effort,j.template_id,j.scope_json,j.execution_json,j.options_json
        FROM agents a JOIN janitor_configs j ON j.agent_id=a.agent_id WHERE a.agent_id=?""", (agent_id,)).fetchone()


# ---- attachments -------------------------------------------------------------

def attachment_row(attachment_id: str, c=None):
    return _c(c).execute("SELECT * FROM janitor_attachments WHERE attachment_id=?", (attachment_id,)).fetchone()


def active_attachment_rows(agent_id: str, c=None, *, trigger_id: str | None = None) -> list:
    sql = "SELECT * FROM janitor_attachments WHERE agent_id=? AND retired_at IS NULL"
    params: list = [agent_id]
    if trigger_id is not None:
        sql += " AND trigger_id=?"
        params.append(trigger_id)
    return list(_c(c).execute(sql + " ORDER BY created_at,attachment_id", tuple(params)))


def has_enabled_attachment(agent_id: str, c=None) -> bool:
    return bool(_c(c).execute("SELECT 1 FROM janitor_attachments WHERE agent_id=? AND enabled=1 AND retired_at IS NULL", (agent_id,)).fetchone())


_ATTACHMENT_WITH_CONFIG = """SELECT t.*,j.generation,j.template_id,j.scope_json,j.enabled AS janitor_enabled,
        a.session FROM janitor_attachments t JOIN janitor_configs j ON j.agent_id=t.agent_id
        JOIN agents a ON a.agent_id=t.agent_id"""


def attachment_with_config(attachment_id: str, c=None):
    """One attachment joined to its configuration and a live janitor agent, else None."""
    return _c(c).execute(_ATTACHMENT_WITH_CONFIG + """ WHERE t.attachment_id=?
        AND a.is_janitor=1 AND a.deleted_at IS NULL AND a.archived_at IS NULL""", (attachment_id,)).fetchone()


def attachments_with_configs(*, enabled_only: bool = False, c=None) -> list:
    query = _ATTACHMENT_WITH_CONFIG + """ WHERE t.retired_at IS NULL
        AND a.is_janitor=1 AND a.deleted_at IS NULL AND a.archived_at IS NULL"""
    if enabled_only:
        query += " AND t.enabled=1 AND j.enabled=1"
    return list(_c(c).execute(query))


def save_attachments(c, agent_id: str, values: list[dict], now: int) -> None:
    """Retire attachments not in `values`, upsert the rest, reset progress on
    an incompatible change (trigger, version or config differs)."""
    ids = {v["attachment_id"] for v in values}
    for row in c.execute("SELECT * FROM janitor_attachments WHERE agent_id=? AND retired_at IS NULL", (agent_id,)).fetchall():
        if row["attachment_id"] not in ids:
            c.execute("UPDATE janitor_attachments SET enabled=0,retired_at=?,next_run_at=NULL WHERE attachment_id=?", (now, row["attachment_id"]))
    for v in values:
        old = c.execute("SELECT * FROM janitor_attachments WHERE attachment_id=?", (v["attachment_id"],)).fetchone()
        compatible = old and old["trigger_id"] == v["trigger_id"] and old["trigger_version"] == v["trigger_version"] and old["config_json"] == encode(v["config"])
        c.execute("""INSERT INTO janitor_attachments
            (attachment_id,agent_id,trigger_id,trigger_version,enabled,config_json,created_at)
            VALUES (?,?,?,?,?,?,?) ON CONFLICT(attachment_id) DO UPDATE SET
            trigger_id=excluded.trigger_id,trigger_version=excluded.trigger_version,
            enabled=excluded.enabled,config_json=excluded.config_json,next_run_at=NULL""",
            (v["attachment_id"], agent_id, v["trigger_id"], v["trigger_version"], int(v["enabled"]), encode(v["config"]), now))
        if not compatible:
            c.execute("DELETE FROM janitor_progress WHERE attachment_id=?", (v["attachment_id"],))


def clear_next_runs(c, agent_id: str) -> None:
    c.execute("UPDATE janitor_attachments SET next_run_at=NULL WHERE agent_id=?", (agent_id,))


def set_next_run(c, attachment_id: str, next_run_at) -> None:
    c.execute("UPDATE janitor_attachments SET next_run_at=? WHERE attachment_id=?", (next_run_at, attachment_id))


# ---- progress ----------------------------------------------------------------

def progress_row(attachment_id: str, c=None):
    return _c(c).execute("SELECT * FROM janitor_progress WHERE attachment_id=?", (attachment_id,)).fetchone()


def save_progress(c, attachment_id: str, generation: int, state: dict, now: int) -> None:
    c.execute("""INSERT INTO janitor_progress(attachment_id,generation,state_json,updated_at)
        VALUES (?,?,?,?) ON CONFLICT(attachment_id) DO UPDATE SET generation=excluded.generation,
        state_json=excluded.state_json,updated_at=excluded.updated_at""", (attachment_id, generation, encode(state), now))


def clear_progress_for_agent(c, agent_id: str) -> None:
    c.execute("DELETE FROM janitor_progress WHERE attachment_id IN (SELECT attachment_id FROM janitor_attachments WHERE agent_id=?)", (agent_id,))


# ---- runs --------------------------------------------------------------------

def run_row(run_id: str, c=None):
    return _c(c).execute("SELECT * FROM janitor_runs WHERE run_id=?", (run_id,)).fetchone()


def run_row_for_trace(trace_id: str, c=None):
    return _c(c).execute("SELECT * FROM janitor_runs WHERE trace_id=?", (trace_id,)).fetchone()


def run_rows_for_agent(agent_id: str, limit: int, c=None) -> list:
    return list(_c(c).execute("SELECT * FROM janitor_runs WHERE agent_id=? ORDER BY created_at DESC,run_id DESC LIMIT ?", (agent_id, limit)))


def active_run_rows(agent_id: str = "", c=None) -> list:
    return list(_c(c).execute(f"SELECT * FROM janitor_runs WHERE {_ACTIVE}" + (" AND agent_id=?" if agent_id else ""),
                              (agent_id,) if agent_id else ()))


def active_run_id(agent_id: str, c=None) -> str | None:
    row = _c(c).execute(f"SELECT run_id FROM janitor_runs WHERE agent_id=? AND {_ACTIVE}", (agent_id,)).fetchone()
    return row[0] if row else None


def active_trace_ids(agent_id: str, c=None) -> list[str]:
    return [r[0] for r in _c(c).execute(f"SELECT trace_id FROM janitor_runs WHERE agent_id=? AND {_ACTIVE}", (agent_id,))]


def has_active_run(agent_id: str, c=None) -> bool:
    return active_run_id(agent_id, c) is not None


def pending_demand_claim(agent_id: str, now: int, c=None) -> bool:
    return bool(_c(c).execute("""SELECT 1 FROM janitor_demand_claims d JOIN janitor_runs r ON r.run_id=d.run_id
        WHERE r.agent_id=? AND d.finished_at IS NULL
        AND json_extract(r.configuration_json,'$.expires_at')>? LIMIT 1""", (agent_id, now)).fetchone())


def expired_demand_runs(c, ttl_ms: int, now: int) -> list[dict]:
    return [dict(row) for row in c.execute(f"""SELECT * FROM janitor_runs
        WHERE {_ACTIVE} AND json_extract(configuration_json,'$.executor')='ephemeral'
        AND COALESCE(json_extract(configuration_json,'$.expires_at'),created_at+?)<=?""", (ttl_ms, now))]


def insert_run(c, *, run_id: str, agent_id: str, session: str, attachment_id: str, generation: int,
               trace_id: str, candidates, configuration=None, status: str = "queued", outcome: str = "",
               created_at: int, started_at: int | None = None, finished_at: int | None = None,
               ignore_existing: bool = False) -> None:
    """Insert a run; `configuration=None` stores the schema default '{}'."""
    c.execute(f"""INSERT {'OR IGNORE ' if ignore_existing else ''}INTO janitor_runs
        (run_id,agent_id,session,attachment_id,generation,trace_id,status,outcome,candidates_json,configuration_json,created_at,started_at,finished_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (run_id, agent_id, session, attachment_id, generation, trace_id, status, outcome, encode(candidates),
         "{}" if configuration is None else encode(configuration), created_at, started_at, finished_at))


def set_run_configuration(c, run_id: str, configuration) -> None:
    c.execute("UPDATE janitor_runs SET configuration_json=? WHERE run_id=?", (encode(configuration), run_id))


def mark_run_started(c, run_id: str, now: int) -> None:
    c.execute("UPDATE janitor_runs SET status='running',started_at=COALESCE(started_at,?) WHERE run_id=?", (now, run_id))


def finish_run_row(c, run_id: str, *, status: str, outcome: str, now: int, error: str = "") -> None:
    c.execute("UPDATE janitor_runs SET status=?,outcome=?,finished_at=?,error=? WHERE run_id=?", (status, outcome, now, error, run_id))


def cancel_run(c, run_id: str, now: int, *, error: str | None = None,
               statuses: Iterable[str] | None = None) -> None:
    """Mark one run cancelled; `statuses` restricts which current states qualify."""
    sql = "UPDATE janitor_runs SET status='cancelled',outcome='cancelled',finished_at=?"
    params: list = [now]
    if error is not None:
        sql += ",error=?"
        params.append(error)
    sql += " WHERE run_id=?"
    params.append(run_id)
    if statuses is not None:
        wanted = tuple(statuses)
        sql += " AND status IN (" + ",".join("?" for _ in wanted) + ")"
        params.extend(wanted)
    c.execute(sql, tuple(params))


def cancel_active_runs(c, agent_id: str, now: int) -> None:
    c.execute(f"UPDATE janitor_runs SET status='cancelled',outcome='cancelled',finished_at=? WHERE agent_id=? AND {_ACTIVE}", (now, agent_id))


# ---- demand claims and results ----------------------------------------------

def claim_run(c, run_id: str, now: int) -> bool:
    return bool(c.execute("INSERT OR IGNORE INTO janitor_demand_claims(run_id,claimed_at) VALUES (?,?)", (run_id, now)).rowcount)


def finish_claim(c, run_id: str, now: int) -> None:
    c.execute("UPDATE janitor_demand_claims SET finished_at=COALESCE(finished_at,?) WHERE run_id=?", (now, run_id))


def demand_result_json(run_id: str, c=None) -> str | None:
    row = _c(c).execute("SELECT result_json FROM janitor_demand_results WHERE run_id=?", (run_id,)).fetchone()
    return row[0] if row else None


def insert_demand_result(c, run_id: str, result, now: int, *, ignore_existing: bool = False) -> None:
    c.execute(f"INSERT {'OR IGNORE ' if ignore_existing else ''}INTO janitor_demand_results(run_id,result_json,created_at) VALUES (?,?,?)",
              (run_id, encode(result), now))


# ---- effects -----------------------------------------------------------------

def effect_rows(run_id: str, c=None) -> list:
    return list(_c(c).execute("SELECT * FROM janitor_effects WHERE run_id=? ORDER BY created_at,target_agent_id", (run_id,)))


def effect_row(run_id: str, target_agent_id: str, c=None):
    return _c(c).execute("SELECT * FROM janitor_effects WHERE run_id=? AND target_agent_id=?", (run_id, target_agent_id)).fetchone()


def effect_outcomes(run_id: str, c=None) -> list[str]:
    return [r[0] for r in _c(c).execute("SELECT outcome FROM janitor_effects WHERE run_id=?", (run_id,))]


def insert_effect(c, *, run_id: str, target_agent_id: str, target_session: str, observed_state_id: int,
                  outcome: str, before_label: str, after_label: str, reason: str, now: int) -> None:
    c.execute("INSERT INTO janitor_effects(run_id,target_agent_id,target_session,observed_state_id,outcome,before_label,after_label,reason,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
              (run_id, target_agent_id, target_session, observed_state_id, outcome, before_label, after_label, reason, now))


def receipt_export_rows(agent_id: str, c=None) -> list:
    return list(_c(c).execute("""SELECT e.* FROM janitor_effects e JOIN janitor_runs r ON r.run_id=e.run_id
        WHERE r.agent_id=? ORDER BY e.created_at DESC LIMIT 1000""", (agent_id,)))


# ---- label ownership ---------------------------------------------------------

def label_ownership_row(target_agent_id: str, c=None):
    return _c(c).execute("SELECT * FROM janitor_label_ownership WHERE target_agent_id=?", (target_agent_id,)).fetchone()


def label_ownership_with_status(c=None) -> list:
    return list(_c(c).execute("SELECT o.*,a.custom_status FROM janitor_label_ownership o JOIN agents a ON a.agent_id=o.target_agent_id"))


def owned_label_row(owner_agent_id: str, target_session: str, c=None):
    return _c(c).execute("SELECT o.label,a.custom_status FROM janitor_label_ownership o JOIN agents a ON a.agent_id=o.target_agent_id WHERE o.owner_agent_id=? AND a.session=?",
                         (owner_agent_id, target_session)).fetchone()


def owned_target_rows(owner_agent_id: str, c=None) -> list:
    """Agents whose current custom_status is the label this owner wrote."""
    return list(_c(c).execute("""SELECT a.* FROM agents a
        JOIN janitor_label_ownership o ON o.target_agent_id=a.agent_id
        WHERE o.owner_agent_id=? AND a.custom_status=o.label""", (owner_agent_id,)))


def ownership_export_rows(owner_agent_id: str, c=None) -> list:
    return list(_c(c).execute("""SELECT o.*,a.session,e.observed_state_id
        FROM janitor_label_ownership o JOIN agents a ON a.agent_id=o.target_agent_id
        LEFT JOIN janitor_effects e ON e.run_id=o.run_id AND e.target_agent_id=o.target_agent_id
        WHERE o.owner_agent_id=?""", (owner_agent_id,)))


def upsert_label_ownership(c, *, target_agent_id: str, owner_agent_id: str, run_id: str, label: str,
                           task_signature: str, valid_until: int, now: int) -> None:
    c.execute("""INSERT INTO janitor_label_ownership(target_agent_id,owner_agent_id,run_id,label,task_signature,valid_until,updated_at)
        VALUES (?,?,?,?,?,?,?) ON CONFLICT(target_agent_id) DO UPDATE SET owner_agent_id=excluded.owner_agent_id,
        run_id=excluded.run_id,label=excluded.label,revision=janitor_label_ownership.revision+1,
        task_signature=excluded.task_signature,valid_until=excluded.valid_until,updated_at=excluded.updated_at""",
        (target_agent_id, owner_agent_id, run_id, label, task_signature, valid_until, now))


def refresh_label_ownership(c, target_agent_id: str, *, task_signature: str, valid_until: int, now: int) -> None:
    c.execute("UPDATE janitor_label_ownership SET task_signature=?,valid_until=?,updated_at=? WHERE target_agent_id=?",
              (task_signature, valid_until, now, target_agent_id))


def forget_label_ownership(c, target_agent_id: str) -> None:
    """A manual status write owns the field again. TODO(integration): agents.
    set_custom_status runs this DELETE inline today; call this instead."""
    c.execute("DELETE FROM janitor_label_ownership WHERE target_agent_id=?", (target_agent_id,))


def transfer_label_ownership(c, successor_agent_id: str, target_agent_ids: list[str], now: int) -> None:
    c.executemany("UPDATE janitor_label_ownership SET owner_agent_id=?,revision=revision+1,updated_at=? WHERE target_agent_id=?",
                  [(successor_agent_id, now, target_id) for target_id in target_agent_ids])


# ---- pilot imports and creation requests ------------------------------------

def pilot_import_row(import_id: str, c=None):
    return _c(c).execute("SELECT * FROM janitor_pilot_imports WHERE import_id=?", (import_id,)).fetchone()


def record_pilot_import(c, import_id: str, agent_id: str, now: int, payload_sha256: str) -> None:
    c.execute("INSERT INTO janitor_pilot_imports(import_id,agent_id,imported_at,payload_sha256) VALUES (?,?,?,?)",
              (import_id, agent_id, now, payload_sha256))


def creation_request_row(request_id: str, c=None):
    return _c(c).execute("SELECT * FROM janitor_creation_requests WHERE request_id=?", (request_id,)).fetchone()


def creation_session_reserved(session: str, c=None) -> bool:
    return bool(_c(c).execute("SELECT 1 FROM janitor_creation_requests WHERE session=?", (session,)).fetchone())


def insert_creation_request(c, *, request_id: str, payload_json: str, payload_sha256: str, identity,
                            session: str, now: int) -> None:
    c.execute("INSERT INTO janitor_creation_requests(request_id,payload_json,payload_sha256,identity_json,session,created_at) VALUES (?,?,?,?,?,?)",
              (request_id, payload_json, payload_sha256, encode(identity), session, now))


def link_creation_agent(c, request_id: str, agent_id: str) -> None:
    c.execute("UPDATE janitor_creation_requests SET agent_id=? WHERE request_id=? AND agent_id IS NULL", (agent_id, request_id))


def complete_creation_request(c, request_id: str, response, now: int) -> None:
    c.execute("UPDATE janitor_creation_requests SET response_json=?,completed_at=? WHERE request_id=?", (encode(response), now, request_id))


# ---- autonomy continuity and quota receipts ---------------------------------
#
# janitor_autonomy.py creates these two tables (its SCHEMA); the statements
# against them live here like every other janitor_* table.

def continuity_row(target_id: str, c=None):
    return _c(c).execute("SELECT * FROM janitor_continuity WHERE target_id=?", (target_id,)).fetchone()


def continuity_row_for_run(run_id: str, c=None):
    return _c(c).execute("SELECT * FROM janitor_continuity WHERE run_id=?", (run_id,)).fetchone()


def pending_continuity_rows(c=None) -> list:
    return _c(c).execute("SELECT * FROM janitor_continuity WHERE status='pending'").fetchall()


def save_continuity(target_id: str, snapshot: str, run_id: str, decision_json: str, due_at: int,
                    status: str, c=None) -> None:
    _c(c).execute("INSERT OR REPLACE INTO janitor_continuity VALUES (?,?,?,?,?,?)",
                  (target_id, snapshot, run_id, decision_json, due_at, status))


def set_continuity_status(target_id: str, status: str, c=None) -> None:
    _c(c).execute("UPDATE janitor_continuity SET status=? WHERE target_id=?", (status, target_id))


def insert_quota_receipt(receipt_id: str, run_id: str, payload_json: str, now: int, c=None) -> bool:
    """True when this receipt is new; a repeat of the same identity is ignored."""
    return bool(_c(c).execute("INSERT OR IGNORE INTO janitor_quota_receipts VALUES (?,?,?,NULL,?)",
                              (receipt_id, run_id, payload_json, now)).rowcount)


def set_quota_receipt_payload(receipt_id: str, payload_json: str, c=None) -> None:
    _c(c).execute("UPDATE janitor_quota_receipts SET payload_json=? WHERE receipt_id=?", (payload_json, receipt_id))


def undelivered_quota_receipts(c=None) -> list:
    return _c(c).execute("SELECT * FROM janitor_quota_receipts WHERE delivery_json IS NULL").fetchall()


def set_quota_receipt_delivery(receipt_id: str, delivery_json: str, c=None) -> None:
    _c(c).execute("UPDATE janitor_quota_receipts SET delivery_json=? WHERE receipt_id=?", (delivery_json, receipt_id))


# ---- reads of neighbouring tables inside the janitor transaction ------------

def agent_row(agent_id: str, c=None):
    return _c(c).execute("SELECT * FROM agents WHERE agent_id=?", (agent_id,)).fetchone()


def agent_id_exists(agent_id: str, c=None) -> bool:
    return bool(_c(c).execute("SELECT 1 FROM agents WHERE agent_id=?", (agent_id,)).fetchone())


def agent_session_exists(session: str, c=None) -> bool:
    return bool(_c(c).execute("SELECT 1 FROM agents WHERE session=?", (session,)).fetchone())


def session_occupant(session: str, c=None):
    return _c(c).execute("SELECT agent_id,deleted_at FROM agents WHERE session=?", (session,)).fetchone()


def latest_state_id(agent_id: str, c=None):
    return _c(c).execute("SELECT MAX(state_id) FROM state_log WHERE agent_id=?", (agent_id,)).fetchone()[0]


def has_queued_turn(agent_id: str, c=None) -> bool:
    return bool(_c(c).execute("SELECT 1 FROM queued_turns WHERE agent_id=? AND status IN ('queued','claimed') LIMIT 1", (agent_id,)).fetchone())


def task_signature_rows(c, agent_ids: list[str]) -> list:
    """The runtime, request revision and plan of each agent, for signatures."""
    marks = ",".join("?" for _ in agent_ids)
    return list(c.execute(f"""SELECT a.agent_id,r.runtime_id,r.backend_session_id,
            (SELECT COALESCE(MAX(m.revision),0) FROM messages m WHERE m.agent_id=a.agent_id
              AND m.backend_session_id=COALESCE(r.backend_session_id,'') AND m.role='user'
              AND (m.origin='user' OR m.origin IS NULL)) AS request_revision,
            p.plan_id,p.title,p.status,p.updated_at
            FROM agents a LEFT JOIN runtimes r ON r.runtime_id=(
                SELECT runtime_id FROM runtimes WHERE agent_id=a.agent_id
                ORDER BY started_at DESC,runtime_id DESC LIMIT 1)
            LEFT JOIN task_plans p ON p.plan_id=(
                SELECT plan_id FROM task_plans WHERE agent_id=a.agent_id
                ORDER BY updated_at DESC,plan_id LIMIT 1)
            WHERE a.agent_id IN ({marks})""", agent_ids))
