from __future__ import annotations

import json

import pytest

from lib import agents as agents_db
from lib import message_store, prompt_admissions
from lib.prompt_history import (
    ITEM_TEXT_BYTE_LIMIT,
    PAGE_RESPONSE_BYTE_LIMIT,
    build_prompt_history,
)


COMPUTER_ID = "01234567-89ab-4def-8123-456789abcdef"


def _server_info() -> dict[str, str]:
    return {"server_id": COMPUTER_ID, "name": "Test Computer"}


def _agent(tmp_path, *, session: str = "mike") -> str:
    return agents_db.create_agent(
        persona="Mike", voice_id="voice", cwd=str(tmp_path), session=session,
    )


def _admit(
    agent_id: str,
    *,
    session: str = "mike",
    client_id: str,
    text: str,
    observed_at: int,
    authenticated: bool = True,
    origin: str = "user",
    sender_agent_id: str = "",
    channel: str = "chat",
) -> str:
    admission = prompt_admissions.create(
        authenticated_at_admission=authenticated,
        origin=origin,
        sender_agent_id=sender_agent_id,
        channel=channel,
        observed_at=observed_at,
        client_admission_id=client_id,
        trace_id=f"trace-{client_id}",
        original_text=text,
    )
    admission_id = prompt_admissions.record(
        admission, agent_id=agent_id, session=session,
    )
    assert admission_id
    message_store.record_user_message(
        agent_id=agent_id,
        backend_session_id="conversation",
        client_msg_id=client_id,
        text="provider-visible transformed text",
        origin=origin,
        sender_agent_id=sender_agent_id or None,
        prompt_admission_id=admission_id,
    )
    return admission_id


def _history(session: str = "mike", **kwargs):
    return build_prompt_history(
        session_id=f"{COMPUTER_ID}:{session}",
        server_info=_server_info(),
        **kwargs,
    )


def test_history_exposes_only_prospective_authenticated_user_admissions(tmp_path):
    agent_id = _agent(tmp_path)
    _admit(
        agent_id, client_id="u-user", text="the user's original prompt",
        observed_at=1000, channel="voice",
    )
    _admit(
        agent_id, client_id="u-unauth", text="unauthenticated",
        observed_at=1100, authenticated=False,
    )
    _admit(
        agent_id, client_id="u-agent", text="agent prompt", observed_at=1200,
        origin="agent", sender_agent_id="sender-agent",
    )
    for index, origin in enumerate(
        ("automation", "heartbeat", "dreaming", "schedule", "watcher"),
        start=1,
    ):
        _admit(
            agent_id,
            client_id=f"u-{origin}",
            text=f"{origin} prompt",
            observed_at=1200 + index,
            origin=origin,
        )
    message_store.record_user_message(
        agent_id=agent_id,
        backend_session_id="conversation",
        client_msg_id="u-spoofed-legacy",
        text="legacy u-* row without admission",
        origin="user",
    )

    history = _history()

    assert history is not None
    assert history["schema_version"] == 3
    assert history["contract"] == "user-prompt-history.v3"
    assert [row["text"] for row in history["prompts"]] == [
        "the user's original prompt",
    ]
    prompt = history["prompts"][0]
    assert prompt["message_id"].startswith(f"{COMPUTER_ID}:message:")
    assert prompt["turn_id"].startswith(f"{COMPUTER_ID}:prompt-turn:")
    assert history["session"]["session_id"] == f"{COMPUTER_ID}:mike"
    assert history["session"]["contact"]["identity_kind"] == (
        "synthesized_route_contact"
    )
    assert prompt["presentation_snapshot"] == {"status": "unavailable"}
    assert prompt["prompt_origin"] == {
        "version": 1,
        "kind": "user",
        "principal_id": "user",
        "channel": "voice",
        "client_admission": {
            "identity_kind": "client_message_id",
            "id": "u-user",
            "presentation": "full",
        },
        "observed_at": "1970-01-01T00:00:01.000Z",
        "evidence": {
            "admission_version": 1,
            "authenticated_at_admission": True,
            "authority": "clarp_server",
            "trust_boundary": "cooperative_shared_server_principal",
        },
    }
    assert history["privacy"] == {
        "read_only": True,
        "prospective_only": True,
        "unknown_authorship_excluded": True,
        "historical_presentation_snapshot_available": False,
        "editable": False,
    }


def test_history_preserves_literal_tags_and_uses_original_admitted_text(tmp_path):
    agent_id = _agent(tmp_path)
    literal = (
        "Please explain `<team>literal</team>` and <vox>this</vox> "
        "<speak>code</speak>\n"
        "<oai-mem-citation>user-authored example</oai-mem-citation>"
    )
    _admit(
        agent_id, client_id="u-literal", text=literal, observed_at=2000,
    )

    history = _history()

    assert history is not None
    prompt = history["prompts"][0]
    assert prompt["text"] == literal
    assert "<team>literal</team>" in prompt["preview"]
    assert "user-authored example" in prompt["preview"]
    assert "provider-visible transformed text" not in json.dumps(history)


def test_history_excludes_admission_until_durable_message_exists(tmp_path):
    agent_id = _agent(tmp_path)
    admission = prompt_admissions.create(
        authenticated_at_admission=True,
        origin="user",
        sender_agent_id="",
        channel="chat",
        observed_at=2500,
        client_admission_id="u-queued",
        trace_id="trace-queued",
        original_text="queued but not started",
    )
    admission_id = prompt_admissions.record(
        admission, agent_id=agent_id, session="mike",
    )
    assert admission_id
    assert _history()["prompts"] == []

    message_store.record_user_message(
        agent_id=agent_id,
        backend_session_id="conversation",
        client_msg_id="u-queued",
        text="queued but not started",
        origin="user",
        prompt_admission_id=admission_id,
    )
    assert [row["text"] for row in _history()["prompts"]] == [
        "queued but not started",
    ]


def test_history_bounds_oversized_items_and_total_response(tmp_path):
    agent_id = _agent(tmp_path)
    oversized = "å" * (ITEM_TEXT_BYTE_LIMIT * 2)
    _admit(
        agent_id,
        client_id="u-" + ("x" * 10_000),
        text=oversized,
        observed_at=20_000,
    )
    for index in range(120):
        _admit(
            agent_id,
            client_id=f"u-large-{index}",
            text=oversized,
            observed_at=10_000 + index,
        )

    history = _history(limit=200)

    assert history is not None
    assert history["prompts"]
    assert history["prompts"][0]["prompt_origin"]["client_admission"][
        "identity_kind"
    ] == "client_message_id_sha256"
    assert all(row["text"] is None for row in history["prompts"])
    assert all(row["content_status"] == "truncated" for row in history["prompts"])
    assert all(
        len(row["preview"].encode()) <= 323 for row in history["prompts"]
    )
    assert len(json.dumps(history, ensure_ascii=False).encode()) <= (
        PAGE_RESPONSE_BYTE_LIMIT
    )
    assert history["page"]["has_more"] is True
    assert history["page"]["next_before"]


def test_history_cursor_is_scoped_and_revision_is_stable(tmp_path):
    agent_id = _agent(tmp_path)
    for index in range(3):
        _admit(
            agent_id,
            client_id=f"u-{index}",
            text=f"Prompt {index}",
            observed_at=1000,
        )

    first = _history(limit=2)
    assert first is not None
    repeat = _history(limit=2)
    assert repeat == first
    second = _history(limit=2, before=first["page"]["next_before"])
    assert second is not None
    assert len(second["prompts"]) == 1
    assert second["page"]["has_more"] is False
    with pytest.raises(ValueError, match="invalid before cursor"):
        _history(before="not-a-cursor")


def test_session_id_is_preferred_and_slug_is_compatibility_only(tmp_path):
    _agent(tmp_path)

    by_id = _history()
    by_slug = build_prompt_history(
        compatibility_session_slug="mike", server_info=_server_info(),
    )

    assert by_id is not None and by_slug is not None
    assert by_id["session"]["addressing_mode"] == "session_id"
    assert by_slug["session"]["addressing_mode"] == (
        "compatibility_session_slug"
    )
    with pytest.raises(ValueError, match="session identifiers conflict"):
        build_prompt_history(
            session_id=f"{COMPUTER_ID}:mike",
            compatibility_session_slug="other",
            server_info=_server_info(),
        )


def test_history_rejects_identity_that_cannot_fit_bounded_response(tmp_path):
    oversized_session = "s" * 1025
    _agent(tmp_path, session=oversized_session)

    with pytest.raises(ValueError, match="identity exceeds response byte limit"):
        build_prompt_history(
            session_id=f"{COMPUTER_ID}:{oversized_session}",
            server_info=_server_info(),
        )
