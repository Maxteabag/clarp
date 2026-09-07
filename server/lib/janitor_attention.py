"""Quiet, durable diagnostic alerts for repeated Janitor failures.

Call reconcile after the runner tick or a configuration change, outside a store
transaction. This module never dispatches a turn, speaks, pushes, or marks chat
unread. Existing artifact archive/discard guards are the dismissal mechanism.
"""
from __future__ import annotations

import hashlib
import json
import re

from . import artifacts, db, janitor_builtins, janitors
from .janitor_context import redact
from .voice_markup import strip_hidden_blocks


KIND = "janitor_failure"
PREFIX = "janitor-failure:"
_TERMINAL = "status IN ('completed','failed','cancelled')"
_FAILED = "(status='failed' OR outcome='error')"


def _valid_demand_receipt(config: dict, run) -> bool:
    """A provider response is not recovery until the demand store accepts it."""
    if config["template_id"] not in janitor_builtins.ROLES:
        return False
    try:
        frozen = json.loads(run["configuration_json"])
        result = janitor_builtins._metadata(json.loads(run["result_json"]))
    except (ValueError, TypeError):
        return False
    return bool(
        isinstance(frozen, dict) and frozen.get("executor") == "ephemeral"
        and frozen.get("template_id") == config["template_id"]
        and isinstance(result.get("summary"), str) and result["summary"].strip()
    )


def _episode(config: dict) -> tuple[str, dict | None, int]:
    """A verified success closes an episode, including dismissed ones."""
    con = db.conn()
    identity = (config["agent_id"], config["generation"])
    candidates = con.execute("""SELECT r.rowid AS sequence,r.*,d.result_json FROM janitor_runs r
        LEFT JOIN janitor_demand_results d ON d.run_id=r.run_id
        WHERE r.agent_id=? AND r.generation=? AND r.status='completed'
          AND ((r.outcome IN ('changed','same_task')
          AND EXISTS (SELECT 1 FROM janitor_effects e WHERE e.run_id=r.run_id
                      AND e.outcome IN ('changed','same_task')))
            OR (r.outcome='completed' AND r.finished_at IS NOT NULL AND d.result_json IS NOT NULL))
        ORDER BY r.finished_at DESC,r.rowid DESC""", identity)
    success = next((run for run in candidates if run["outcome"] in {"changed", "same_task"}
                    or _valid_demand_receipt(config, run)), None)
    boundary = success["run_id"] if success else "initial"
    reference = f"{PREFIX}{config['agent_id']}:{config['generation']}:{boundary}"
    latest = con.execute(f"""SELECT rowid AS sequence,* FROM janitor_runs
        WHERE agent_id=? AND generation=? AND {_TERMINAL}
        ORDER BY finished_at DESC,rowid DESC LIMIT 1""", identity).fetchone()
    if not latest or not (latest["status"] == "failed" or latest["outcome"] == "error"):
        return reference, None, 0
    # Keep the streak boundary separate from the episode boundary: a harmless
    # skip ends consecutive failures but does not claim successful recovery.
    previous = con.execute(f"""SELECT rowid AS sequence,finished_at FROM janitor_runs
        WHERE agent_id=? AND generation=? AND {_TERMINAL} AND NOT {_FAILED}
        ORDER BY finished_at DESC,rowid DESC LIMIT 1""", identity).fetchone()
    condition, values = "", []
    if previous:
        condition = " AND (finished_at>? OR (finished_at=? AND rowid>?))"
        values = [previous["finished_at"], previous["finished_at"], previous["sequence"]]
    count = con.execute(f"""SELECT COUNT(*) FROM janitor_runs
        WHERE agent_id=? AND generation=? AND {_TERMINAL} AND {_FAILED}{condition}""",
        (*identity, *values)).fetchone()[0]
    return reference, dict(latest), count


def _description(config, latest, count):
    name = strip_hidden_blocks(str(config["persona"] or config["session"]))[:80]
    labels = config["template_id"] == "task-labels"
    missing = ("Maintenance ended without all review receipts." if labels
               else "Maintenance ended without a valid completion receipt.")
    diagnostic = strip_hidden_blocks(redact(str(latest.get("error") or
        missing)))
    diagnostic = re.sub(r"<(think|analysis)\b[^>]*>.*?(?:</\1>|$)", "", diagnostic,
                        flags=re.IGNORECASE | re.DOTALL)
    diagnostic = " ".join(diagnostic.split())[:300]
    if labels:
        summary = f"Task labels need attention after {count} failed reviews. Review the configuration or pause maintenance."
    else:
        try:
            job = janitors.template(config["template_id"])["name"]
        except janitors.JanitorError:
            job = "Maintenance"
        summary = f"{job} needs attention after {count} failed runs. Review the configuration or pause maintenance."
    return {
        "title": f"{name} needs attention",
        "summary": summary,
        "status": "failed",
        "payload": {"attention_kind": KIND,
                    "configuration_generation": config["generation"],
                    "latest_run_id": latest["run_id"], "failure_count": count,
                    "content": f"{summary}\n\nLatest error: {diagnostic}",
                    "diagnostic": diagnostic,
                    "suggested_action": "review_configuration"},
    }


def reconcile() -> int:
    """Return changed artifact count. Repeated polls do not rewrite stable rows."""
    con = db.conn()
    con.execute("BEGIN IMMEDIATE")
    changed = 0
    try:
        configs = [dict(r) for r in con.execute("""SELECT j.*,a.session,a.persona,a.is_janitor,
            a.archived_at AS agent_archived_at,a.deleted_at AS agent_deleted_at
            FROM janitor_configs j JOIN agents a ON a.agent_id=j.agent_id""")]
        for config in configs:
            active = bool(config["enabled"] and config["is_janitor"] and
                          not config["agent_archived_at"] and not config["agent_deleted_at"])
            reference, latest, count = _episode(config) if active else ("", None, 0)
            open_rows = list(con.execute("""SELECT artifact_id,reference_id FROM artifacts
                WHERE agent_id=? AND type='document' AND reference_id LIKE ?
                  AND status='failed' AND deleted_at IS NULL AND json_valid(payload_json)
                  AND json_extract(payload_json,'$.attention_kind')=?""",
                (config["agent_id"], PREFIX + "%", KIND)))
            for row in open_rows:
                if active and row["reference_id"] == reference:
                    continue
                artifacts.update(row["artifact_id"], {
                    "status": "completed",
                    "summary": "Maintenance recovered or its configuration changed." if active
                               else "Maintenance is paused or removed.",
                })
                changed += 1
            if not active or count < 2 or latest is None:
                continue
            artifact_id = "janitor-alert-" + hashlib.sha256(reference.encode()).hexdigest()[:40]
            # Query raw rows: get() hides discarded records, but their tombstones
            # must continue suppressing recreation of this same failure episode.
            existing = con.execute("SELECT * FROM artifacts WHERE artifact_id=?", (artifact_id,)).fetchone()
            if existing and (existing["archived_at"] is not None or existing["deleted_at"] is not None
                             or existing["status"] in {"completed", "cancelled", "expired"}):
                continue
            desired = _description(config, latest, count)
            if existing:
                # Do not repurpose a user artifact in the unlikely event of an ID collision.
                if existing["agent_id"] != config["agent_id"] or existing["reference_id"] != reference \
                        or existing["type"] != "document":
                    continue
                current = artifacts.get(artifact_id)
                if current["payload"].get("attention_kind") != KIND:
                    continue
                if all(current[key] == value for key, value in desired.items()):
                    continue
                artifacts.update(artifact_id, desired)
            else:
                artifacts.create(session=config["session"], type="document", reference_id=reference,
                                 artifact_id=artifact_id, **desired)
            changed += 1
        con.execute("COMMIT")
    except BaseException:
        con.execute("ROLLBACK")
        raise
    return changed


def pending(*, include_archived: bool = False) -> list[dict]:
    """Attention projection; timestamps are artifact archive/discard revisions."""
    rows = db.conn().execute("""SELECT a.*,g.persona AS agent_name,j.generation
        FROM artifacts a JOIN agents g ON g.agent_id=a.agent_id
        JOIN janitor_configs j ON j.agent_id=a.agent_id
        WHERE a.type='document' AND a.status='failed' AND a.reference_id LIKE ?
          AND a.deleted_at IS NULL AND g.deleted_at IS NULL AND g.archived_at IS NULL
          AND g.is_janitor=1 AND j.enabled=1 AND (? OR a.archived_at IS NULL)
        ORDER BY a.updated_at DESC,a.artifact_id""", (PREFIX + "%", include_archived)).fetchall()
    result = []
    for row in rows:
        try:
            payload = json.loads(row["payload_json"])
        except (ValueError, TypeError):
            continue
        if not isinstance(payload, dict) or payload.get("attention_kind") != KIND \
                or payload.get("configuration_generation") != row["generation"] \
                or not all(key in payload for key in ("latest_run_id", "failure_count")):
            continue
        item = {key: row[key] for key in ("artifact_id", "agent_id", "session", "agent_name", "type",
                "status", "title", "summary", "created_at", "updated_at", "archived_at", "reference_id")}
        item.update({key: payload[key] for key in ("attention_kind", "configuration_generation",
                     "latest_run_id", "failure_count")})
        result.append(item)
    return result
