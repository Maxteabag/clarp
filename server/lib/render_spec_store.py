"""Transaction and projection boundaries for render_spec artifacts."""
from __future__ import annotations

import json

from . import db, render_spec as contract
from .voice_markup import strip_hidden_blocks


class PublicRow(dict):
    """Safe to serialize as-is. Only the negotiated projection may add a marker."""
    def __init__(self, item, meta):
        super().__init__(item)
        self.meta = meta


def public_row(item):
    stored = json.loads(item.pop("payload_json"))
    meta = {k: stored[k] for k in ("version", "catalog", "catalog_version", "catalog_sha256",
                                    "content_identity", "element_count")}
    return PublicRow(item, meta)


def drawable(row, capable):
    """Whether a client of render ``capable`` (the highest catalog version it draws, 0 for none;
    True is version 1) draws this public row. Below it the client gets the plain card."""
    meta = getattr(row, "meta", None) or {}
    return bool(capable) and int(capable) >= meta.get("catalog_version", 1)


def summary(row, capable):
    result = dict(row)
    if drawable(row, capable):
        meta = row.meta
        result.update({k: meta[k] for k in ("version", "catalog", "catalog_version", "catalog_sha256",
                                            "element_count")})
        result["content_omitted"] = {**meta["content_identity"], "updated_at": row["updated_at"]}
    return result


def open_current(ident):
    from . import artifacts
    con = db.conn()
    owned = not con.in_transaction
    if owned:
        con.execute("BEGIN")
    try:
        row = artifacts.get(ident)
        if not isinstance(row, PublicRow):
            raise contract.Refusal("not_open", "")
        body = con.execute("SELECT payload_json FROM artifacts WHERE artifact_id=?", (ident,)).fetchone()
        item = dict(row)
        item.update(json.loads(body["payload_json"]))
        if row["status"] == "completed":
            previous = con.execute(
                "SELECT submission_id,version,answers_json,created_at,status FROM form_submissions "
                "WHERE artifact_id=? LIMIT 1", (ident,)).fetchone()
            if previous:
                item["submission"] = dict(submission_id=previous["submission_id"], version=previous["version"],
                                          answers=json.loads(previous["answers_json"]),
                                          accepted_at=previous["created_at"], delivery_status=previous["status"])
        if owned:
            con.execute("COMMIT")
        return item
    except BaseException:
        if owned and con.in_transaction:
            con.execute("ROLLBACK")
        raise


class NotExportable(ValueError):
    """An artifact of a kind the export does not carry yet."""


def export(ident):
    """What moving a render_spec to another Host needs (R137), independent of any client's display
    negotiation: the public row, the stored payload as kept (the spec, its catalog and schema
    identity, its content identity), and the accepted submission or None, with its answers, its
    audio preference as accepted, its payload hash, its delivery status and when it was delivered.
    None for no such artifact; NotExportable for another kind. One read snapshot. Nothing here is
    logged: the answers are the user's."""
    from . import artifacts
    con = db.conn()
    owned = not con.in_transaction
    if owned:
        con.execute("BEGIN")
    try:
        row = artifacts.get(ident)
        if row is None:
            return None
        if row["type"] != "render_spec":
            raise NotExportable(row["type"])
        stored = json.loads(con.execute("SELECT payload_json FROM artifacts WHERE artifact_id=?",
                                        (ident,)).fetchone()["payload_json"])
        found = con.execute(
            "SELECT submission_id,version,answers_json,payload_hash,synthesize_audio,status,created_at,delivered_at "
            "FROM form_submissions WHERE artifact_id=? ORDER BY created_at, submission_id LIMIT 1", (ident,)).fetchone()
        submission = None if found is None else {
            "submission_id": found["submission_id"], "version": found["version"],
            "answers": json.loads(found["answers_json"]), "accepted_at": found["created_at"],
            "payload_hash": found["payload_hash"], "synthesize_audio": bool(found["synthesize_audio"]),
            "delivery_status": found["status"], "delivered_at": found["delivered_at"]}
        if owned:
            con.execute("COMMIT")
        return {"kind": "render_spec", "render_spec": {"artifact": dict(row), "stored": stored, "submission": submission}}
    except BaseException:
        if owned and con.in_transaction:
            con.execute("ROLLBACK")
        raise


def publish(agent, ident, title, summary_text, status, reference, payload, expected, *, validate_only=False,
            source_trace_id=""):
    from . import artifacts
    contract.validate(payload)
    identity = contract.content_identity(payload["spec"], payload["version"])
    stored = {**payload, "content_identity": identity, "element_count": len(payload["spec"]["elements"]),
              "catalog_sha256": contract.catalog(payload["catalog_version"]).sha256}
    con = db.conn()
    con.execute("BEGIN" if validate_only else "BEGIN IMMEDIATE")
    try:
        old = con.execute("SELECT type,status,payload_json,agent_id,updated_at,deleted_at FROM artifacts "
                          "WHERE artifact_id=?", (ident,)).fetchone() if ident else None
        if old:
            if old["type"] != "render_spec" or old["agent_id"] != agent["agent_id"] or old["deleted_at"]:
                raise contract.Refusal("not_open", "")
            if old["status"] == "completed":
                raise contract.Refusal("already_answered", "")
            current = json.loads(old["payload_json"])
            if expected != current["version"] or payload["version"] == expected:
                raise contract.Refusal("version_mismatch", "/expected_version")
        elif expected is not None:
            raise contract.Refusal("version_mismatch", "/expected_version")
        if status not in ("ready", "active", "draft"):
            raise contract.Refusal("not_open", "/status")
        if validate_only:
            result = {"valid": True}
        elif old:
            now = max(old["updated_at"] + 1, db.now_ms())
            artifacts.replace_render_spec_in(
                con, ident, title=title[:300], summary=strip_hidden_blocks(summary_text).strip()[:4000],
                status=status, reference=strip_hidden_blocks(reference).strip()[:500],
                payload_json=json.dumps(stored), now=now)
            result = artifacts.get(ident)
        else:
            result = artifacts._insert_artifact(agent, ident or artifacts._new("artifact"), "render_spec",
                                                title, summary_text, status, reference, stored,
                                                source_trace_id=source_trace_id)
        con.execute("COMMIT")
        return result
    except BaseException:
        con.execute("ROLLBACK")
        raise


def submit(ident, data, *, capable, on_completed=None):
    from . import artifacts, html_forms
    if not capable:
        raise contract.Refusal("client_contract", "")
    con = db.conn()
    con.execute("BEGIN IMMEDIATE")
    try:
        row = con.execute("SELECT * FROM artifacts WHERE artifact_id=? AND type='render_spec' "
                          "AND deleted_at IS NULL", (ident,)).fetchone()
        if not row:
            raise contract.Refusal("not_open", "")
        sid = data.get("submission_id") if isinstance(data, dict) else None
        # Only an id the contract takes is looked up: anything else (a lone half of a UTF-16
        # pair included) is refused by admit() with its own triple, before SQLite sees it (R71).
        previous = (con.execute("SELECT * FROM form_submissions WHERE submission_id=?", (sid,)).fetchone()
                    if contract.submission_id_ok(sid) else None)
        payload = json.loads(row["payload_json"])
        if int(capable) < contract.version_of(payload):
            raise contract.Refusal("client_contract", "")
        result = contract.admit(ident, payload, row["status"], data, previous)
        if not result["repeat"]:
            now = db.now_ms()
            html_forms.record_submission_in(
                con, submission_id=sid, artifact_id=ident, version=data["version"], session=row["session"],
                answers_json=result["answers_json"], payload_hash=result["payload_hash"],
                prompt=contract.message_text(row, data, payload), created_at=now,
                synthesize_audio=int(data.get("synthesize_audio", True)))
            artifacts.complete_in(con, ident, now=now, updated_at=max(now, row["updated_at"] + 1))
            previous = con.execute("SELECT * FROM form_submissions WHERE submission_id=?", (sid,)).fetchone()
        receipt = html_forms.receipt(previous)
        con.execute("COMMIT")
    except BaseException:
        con.execute("ROLLBACK")
        raise
    if not result["repeat"] and on_completed is not None:
        on_completed({key: row[key] for key in ("artifact_id", "session", "agent_id")})
    return receipt
