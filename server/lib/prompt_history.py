"""Bounded read model over prospective authenticated prompt admissions."""
from __future__ import annotations

import base64
import binascii
import datetime as dt
import hashlib
import json
import sqlite3
import uuid
from typing import Any

from . import db
from .server_identity import get_server_info


SCHEMA_VERSION = 3
CONTRACT = "user-prompt-history.v3"
DEFAULT_LIMIT = 50
MAX_LIMIT = 200
ITEM_TEXT_BYTE_LIMIT = 4 * 1024
PREVIEW_BYTE_LIMIT = 320
PAGE_RESPONSE_BYTE_LIMIT = 64 * 1024
IDENTITY_FIELD_BYTE_LIMIT = 1024


def _session_id(computer_id: str, session_slug: str) -> str:
    return f"{computer_id}:{session_slug}"


def _route_contact_id(computer_id: str, agent_id: str) -> str:
    opaque = uuid.uuid5(uuid.UUID(computer_id), f"legacy-agent-contact:{agent_id}")
    return f"{computer_id}:route-contact:{opaque}"


def _public_id(computer_id: str, kind: str, local_id: str) -> str:
    opaque = uuid.uuid5(uuid.UUID(computer_id), f"{kind}:{local_id}")
    return f"{computer_id}:{kind}:{opaque}"


def _iso_from_ms(value: int) -> str:
    stamp = dt.datetime.fromtimestamp(value / 1000, tz=dt.timezone.utc)
    return stamp.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _bounded_utf8(value: str, byte_limit: int) -> str:
    raw = value.encode("utf-8")
    if len(raw) <= byte_limit:
        return value
    return raw[:byte_limit].decode("utf-8", errors="ignore").rstrip() + "…"


def _preview(value: str) -> str:
    return _bounded_utf8(" ".join(value.split()), PREVIEW_BYTE_LIMIT)


def _client_admission_summary(value: str) -> dict[str, str]:
    raw = value.encode("utf-8")
    if len(raw) <= 256:
        return {
            "identity_kind": "client_message_id",
            "id": value,
            "presentation": "full",
        }
    return {
        "identity_kind": "client_message_id_sha256",
        "id": "sha256:" + hashlib.sha256(raw).hexdigest(),
        "presentation": "digested_for_response_bound",
    }


def _revision(payload: dict[str, Any]) -> str:
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode()
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def _build_payload(
    *,
    computer_id: str,
    computer_name: str,
    session_slug: str,
    agent_id: str,
    addressing_mode: str,
    prompts: list[dict[str, Any]],
    limit: int,
    has_more: bool,
    next_before: str | None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "contract": CONTRACT,
        "computer": {
            "computer_id": computer_id,
            "name": _bounded_utf8(computer_name, 256),
            "name_status": (
                "available"
                if len(computer_name.encode()) <= 256
                else "truncated"
            ),
        },
        "session": {
            "session_id": _session_id(computer_id, session_slug),
            "compatibility_session_slug": session_slug,
            "agent_id": agent_id,
            "addressing_mode": addressing_mode,
            "contact": {
                "route_contact_id": _route_contact_id(computer_id, agent_id),
                "identity_kind": "synthesized_route_contact",
                "contact_id": None,
            },
        },
        "prompts": prompts,
        "page": {
            "limit": limit,
            "has_more": has_more,
            "next_before": next_before,
            "response_byte_limit": PAGE_RESPONSE_BYTE_LIMIT,
            "item_text_byte_limit": ITEM_TEXT_BYTE_LIMIT,
        },
        "privacy": {
            "read_only": True,
            "prospective_only": True,
            "unknown_authorship_excluded": True,
            "historical_presentation_snapshot_available": False,
            "editable": False,
        },
    }
    payload["ordering_revision"] = _revision(payload)
    payload["ordering_revision_kind"] = "content_hash_equality"
    return payload


def _encode_cursor(
    *, computer_id: str, agent_id: str, admission_id: str,
) -> str:
    payload = json.dumps(
        {"v": 1, "computer_id": computer_id, "agent_id": agent_id,
         "admission_id": admission_id},
        sort_keys=True, separators=(",", ":"),
    ).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def _decode_cursor(value: str) -> dict[str, Any] | None:
    try:
        padding = "=" * (-len(value) % 4)
        decoded = base64.b64decode(
            value + padding, altchars=b"-_", validate=True,
        )
        payload = json.loads(decoded)
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError, binascii.Error):
        return None
    return payload if isinstance(payload, dict) else None


def _resolve_session_slug(
    *, computer_id: str, session_id: str, compatibility_session_slug: str,
) -> tuple[str, str]:
    if session_id:
        prefix = computer_id + ":"
        if not session_id.startswith(prefix) or not session_id[len(prefix):]:
            raise ValueError("invalid session_id")
        slug = session_id[len(prefix):]
        if compatibility_session_slug and compatibility_session_slug != slug:
            raise ValueError("session identifiers conflict")
        return slug, "session_id"
    if compatibility_session_slug:
        return compatibility_session_slug, "compatibility_session_slug"
    raise ValueError("session_id required")


def _cursor_position(
    con: sqlite3.Connection,
    *,
    computer_id: str,
    agent_id: str,
    cursor: str,
) -> tuple[int, str] | None:
    if not cursor:
        return None
    payload = _decode_cursor(cursor)
    if (
        payload is None
        or payload.get("v") != 1
        or payload.get("computer_id") != computer_id
        or payload.get("agent_id") != agent_id
        or not isinstance(payload.get("admission_id"), str)
    ):
        return None
    admission_id = str(payload["admission_id"])
    row = con.execute(
        """SELECT p.observed_at,p.admission_id
             FROM prompt_admissions p
            WHERE p.admission_id = ? AND p.agent_id = ?
              AND p.admission_version = 1
              AND p.authenticated_at_admission = 1
              AND p.cooperative_principal = 'user'
              AND p.principal_id = 'user'
              AND EXISTS (
                    SELECT 1 FROM messages m
                     WHERE m.prompt_admission_id = p.admission_id
                       AND m.role = 'user'
              )""",
        (admission_id, agent_id),
    ).fetchone()
    if row is None:
        return None
    return int(row["observed_at"]), admission_id


def _metadata_rows(
    con: sqlite3.Connection,
    *,
    agent_id: str,
    cursor_position: tuple[int, str] | None,
    limit: int,
) -> list[dict[str, Any]]:
    params: list[Any] = [agent_id]
    cursor_sql = ""
    if cursor_position:
        cursor_sql = """AND (
            p.observed_at < ?
            OR (p.observed_at = ? AND p.admission_id < ?)
        )"""
        params.extend([
            cursor_position[0], cursor_position[0], cursor_position[1],
        ])
    params.append(limit)
    rows = con.execute(
        f"""SELECT p.admission_id,p.admission_version,
                   p.authenticated_at_admission,p.cooperative_principal,
                   p.principal_id,p.channel,p.observed_at,p.client_admission_id,
                   p.trace_id,p.agent_id,p.session,p.message_id,
                   length(CAST(p.original_text AS BLOB)) AS text_bytes
              FROM prompt_admissions p
             WHERE p.agent_id = ?
               AND p.admission_version = 1
               AND p.authenticated_at_admission = 1
               AND p.cooperative_principal = 'user'
               AND p.principal_id = 'user'
               AND EXISTS (
                    SELECT 1 FROM messages m
                     WHERE m.prompt_admission_id = p.admission_id
                       AND m.role = 'user'
               )
               {cursor_sql}
             ORDER BY p.observed_at DESC,p.admission_id DESC
             LIMIT ?""",
        tuple(params),
    ).fetchall()
    return [dict(row) for row in rows]


def _bounded_text(
    con: sqlite3.Connection, *, admission_id: str, text_bytes: int,
) -> tuple[str | None, str, str]:
    if text_bytes <= ITEM_TEXT_BYTE_LIMIT:
        row = con.execute(
            "SELECT original_text FROM prompt_admissions WHERE admission_id = ?",
            (admission_id,),
        ).fetchone()
        text = str(row["original_text"] or "") if row else ""
        return text, _preview(text), "available"
    row = con.execute(
        """SELECT substr(original_text, 1, 1024) AS prefix
             FROM prompt_admissions WHERE admission_id = ?""",
        (admission_id,),
    ).fetchone()
    prefix = str(row["prefix"] or "") if row else ""
    return None, _preview(prefix), "truncated"


def build_prompt_history(
    *,
    session_id: str = "",
    compatibility_session_slug: str = "",
    limit: int = DEFAULT_LIMIT,
    before: str = "",
    server_info: dict[str, str] | None = None,
) -> dict[str, Any] | None:
    """Return only prospectively evidenced user prompt admissions."""
    info = dict(server_info or get_server_info())
    computer_id = str(info["server_id"])
    session_slug, addressing_mode = _resolve_session_slug(
        computer_id=computer_id,
        session_id=session_id,
        compatibility_session_slug=compatibility_session_slug,
    )
    for identity in (computer_id, session_slug):
        if len(identity.encode()) > IDENTITY_FIELD_BYTE_LIMIT:
            raise ValueError("identity exceeds response byte limit")
    bounded_limit = max(1, min(int(limit), MAX_LIMIT))
    con = db.conn()
    con.execute("SAVEPOINT prompt_history_read")
    try:
        agent = con.execute(
            """SELECT agent_id,session
                 FROM agents
                WHERE session = ? AND deleted_at IS NULL""",
            (session_slug,),
        ).fetchone()
        if agent is None:
            con.execute("RELEASE prompt_history_read")
            return None
        agent_id = str(agent["agent_id"])
        if len(agent_id.encode()) > IDENTITY_FIELD_BYTE_LIMIT:
            raise ValueError("identity exceeds response byte limit")
        position = _cursor_position(
            con, computer_id=computer_id, agent_id=agent_id, cursor=before,
        )
        if before and position is None:
            raise ValueError("invalid before cursor")
        rows = _metadata_rows(
            con,
            agent_id=agent_id,
            cursor_position=position,
            limit=bounded_limit + 1,
        )

        prompts: list[dict[str, Any]] = []
        consumed = 0
        for row in rows[:bounded_limit]:
            text, preview, content_status = _bounded_text(
                con,
                admission_id=str(row["admission_id"]),
                text_bytes=int(row["text_bytes"] or 0),
            )
            observed_at = int(row["observed_at"])
            prompt = {
                "message_id": _public_id(
                    computer_id, "message", str(row["message_id"]),
                ),
                "turn_id": _public_id(
                    computer_id, "prompt-turn", str(row["admission_id"]),
                ),
                "turn_id_kind": "prompt_admission",
                "created_at": _iso_from_ms(observed_at),
                "created_at_ms": observed_at,
                "text": text,
                "preview": preview,
                "content_status": content_status,
                "original_bytes": int(row["text_bytes"] or 0),
                "presentation_snapshot": {"status": "unavailable"},
                "prompt_origin": {
                    "version": 1,
                    "kind": "user",
                    "principal_id": str(row["principal_id"]),
                    "channel": str(row["channel"]),
                    "client_admission": _client_admission_summary(
                        str(row["client_admission_id"]),
                    ),
                    "observed_at": _iso_from_ms(observed_at),
                    "evidence": {
                        "admission_version": int(row["admission_version"]),
                        "authenticated_at_admission": bool(
                            row["authenticated_at_admission"]
                        ),
                        "authority": "clarp_server",
                        "trust_boundary": (
                            "cooperative_shared_server_principal"
                        ),
                    },
                },
            }
            candidate_prompts = [*prompts, prompt]
            candidate_cursor = _encode_cursor(
                computer_id=computer_id,
                agent_id=agent_id,
                admission_id=str(row["admission_id"]),
            )
            candidate = _build_payload(
                computer_id=computer_id,
                computer_name=str(info.get("name") or ""),
                session_slug=session_slug,
                agent_id=agent_id,
                addressing_mode=addressing_mode,
                prompts=candidate_prompts,
                limit=bounded_limit,
                has_more=True,
                next_before=candidate_cursor,
            )
            if len(json.dumps(candidate, ensure_ascii=False).encode()) > (
                PAGE_RESPONSE_BYTE_LIMIT
            ):
                if not prompts:
                    raise ValueError("prompt metadata exceeds response byte limit")
                break
            prompts.append(prompt)
            consumed += 1
    except BaseException:
        con.execute("ROLLBACK TO prompt_history_read")
        con.execute("RELEASE prompt_history_read")
        raise
    else:
        con.execute("RELEASE prompt_history_read")

    has_more = len(rows) > consumed
    next_before = (
        _encode_cursor(
            computer_id=computer_id,
            agent_id=agent_id,
            admission_id=str(rows[consumed - 1]["admission_id"]),
        )
        if has_more and consumed else None
    )
    return _build_payload(
        computer_id=computer_id,
        computer_name=str(info.get("name") or ""),
        session_slug=session_slug,
        agent_id=agent_id,
        addressing_mode=addressing_mode,
        prompts=prompts,
        limit=bounded_limit,
        has_more=has_more,
        next_before=next_before,
    )
