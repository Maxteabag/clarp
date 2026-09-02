"""Prospective, server-owned provenance for admitted prompt requests."""
from __future__ import annotations

import dataclasses
import json
import uuid

from . import db


ADMISSION_VERSION = 1


@dataclasses.dataclass(frozen=True)
class PromptAdmission:
    admission_id: str
    admission_version: int
    authenticated_at_admission: bool
    cooperative_principal: str
    principal_id: str
    origin: str
    sender_agent_id: str
    channel: str
    observed_at: int
    client_admission_id: str
    trace_id: str
    original_text: str

    def to_json(self) -> str:
        return json.dumps(dataclasses.asdict(self), separators=(",", ":"))

    @classmethod
    def from_json(cls, raw: str | None) -> PromptAdmission | None:
        try:
            value = json.loads(raw or "")
            if not isinstance(value, dict):
                return None
            return cls(
                admission_id=str(value["admission_id"]),
                admission_version=int(value["admission_version"]),
                authenticated_at_admission=bool(
                    value["authenticated_at_admission"]
                ),
                cooperative_principal=str(value["cooperative_principal"]),
                principal_id=str(value.get("principal_id") or ""),
                origin=str(value["origin"]),
                sender_agent_id=str(value.get("sender_agent_id") or ""),
                channel=str(value["channel"]),
                observed_at=int(value["observed_at"]),
                client_admission_id=str(value["client_admission_id"]),
                trace_id=str(value["trace_id"]),
                original_text=str(value["original_text"]),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None


def create(
    *,
    authenticated_at_admission: bool,
    origin: str,
    sender_agent_id: str,
    channel: str,
    observed_at: int,
    client_admission_id: str,
    trace_id: str,
    original_text: str,
) -> PromptAdmission:
    normalized_origin = (origin or "").strip()
    sender = (sender_agent_id or "").strip()
    if (authenticated_at_admission
            and normalized_origin in {"user", "oracle"} and not sender):
        principal, principal_id = "user", "user"
    elif sender:
        principal, principal_id = "agent", sender
    elif normalized_origin and normalized_origin != "user":
        principal, principal_id = "automation", normalized_origin
    else:
        principal, principal_id = "unknown", ""
    return PromptAdmission(
        admission_id=f"padm-{uuid.uuid4()}",
        admission_version=ADMISSION_VERSION,
        authenticated_at_admission=authenticated_at_admission,
        cooperative_principal=principal,
        principal_id=principal_id,
        origin=normalized_origin,
        sender_agent_id=sender,
        channel=channel,
        observed_at=observed_at,
        client_admission_id=client_admission_id,
        trace_id=trace_id,
        original_text=original_text,
    )


def message_id(client_admission_id: str) -> str:
    value = client_admission_id.strip()
    return value if value.startswith("u-") else f"u-{value}"


def record(
    admission: PromptAdmission, *, agent_id: str, session: str,
) -> str:
    """Persist once after target resolution; never bless a legacy message."""
    if admission.admission_version != ADMISSION_VERSION:
        return ""
    con = db.conn()
    expected_message_id = message_id(admission.client_admission_id)
    existing_message = con.execute(
        "SELECT prompt_admission_id FROM messages WHERE message_id = ?",
        (expected_message_id,),
    ).fetchone()
    if existing_message is not None:
        return str(existing_message["prompt_admission_id"] or "")
    existing = con.execute(
        """SELECT admission_id FROM prompt_admissions
            WHERE agent_id = ? AND client_admission_id = ?""",
        (agent_id, admission.client_admission_id),
    ).fetchone()
    if existing is not None:
        return str(existing["admission_id"])
    con.execute(
        """INSERT INTO prompt_admissions (
               admission_id,admission_version,authenticated_at_admission,
               cooperative_principal,principal_id,origin,sender_agent_id,
               channel,observed_at,
               client_admission_id,trace_id,agent_id,session,message_id,
               original_text
           ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            admission.admission_id,
            admission.admission_version,
            int(admission.authenticated_at_admission),
            admission.cooperative_principal,
            admission.principal_id,
            admission.origin,
            admission.sender_agent_id,
            admission.channel,
            admission.observed_at,
            admission.client_admission_id,
            admission.trace_id,
            agent_id,
            session,
            expected_message_id,
            admission.original_text,
        ),
    )
    return admission.admission_id


def find_for_client(*, agent_id: str, client_admission_id: str) -> str:
    if not client_admission_id:
        return ""
    row = db.conn().execute(
        """SELECT admission_id FROM prompt_admissions
            WHERE agent_id = ? AND client_admission_id = ?""",
        (agent_id, client_admission_id),
    ).fetchone()
    return str(row["admission_id"] or "") if row else ""


def update_for_queued_edit(admission_id: str, text: str) -> None:
    if not admission_id:
        return
    con = db.conn()
    con.execute(
        "UPDATE prompt_admissions SET original_text = ? WHERE admission_id = ?",
        (text, admission_id),
    )
    message = con.execute(
        """SELECT agent_id,backend_session_id FROM messages
            WHERE prompt_admission_id = ? AND role = 'user'""",
        (admission_id,),
    ).fetchone()
    if message is None:
        return
    revision_row = con.execute(
        """UPDATE message_clock SET revision = revision + 1
            WHERE singleton = 0 RETURNING revision"""
    ).fetchone()
    revision = int(revision_row["revision"])
    con.execute(
        """UPDATE messages SET text = ?,updated_at = ?,revision = ?
            WHERE prompt_admission_id = ? AND role = 'user'""",
        (text, db.now_ms(), revision, admission_id),
    )
    con.execute(
        """INSERT INTO conversation_heads (
               agent_id,backend_session_id,revision,replace_revision
           ) VALUES (?,?,?,0)
           ON CONFLICT(agent_id,backend_session_id) DO UPDATE SET
               revision = MAX(conversation_heads.revision,excluded.revision)""",
        (
            str(message["agent_id"]),
            str(message["backend_session_id"] or ""),
            revision,
        ),
    )


def delete_unmaterialized(admission_id: str) -> None:
    if not admission_id:
        return
    db.conn().execute(
        """DELETE FROM prompt_admissions
            WHERE admission_id = ?
              AND NOT EXISTS (
                    SELECT 1 FROM messages
                     WHERE prompt_admission_id = prompt_admissions.admission_id
              )""",
        (admission_id,),
    )
