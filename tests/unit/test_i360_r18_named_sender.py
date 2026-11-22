"""Issue 360, R18: a /send that names a sender (another agent) is never the user's
word, whatever origin it claims, also without a turn token; the user's own
sends name no sender."""
from __future__ import annotations

import pytest

from lib import agents as agents_db
from lib.send_request import SendRequest


@pytest.fixture
def two_agents(tmp_path):
    mike = agents_db.create_agent(persona="Mike", voice_id="V", cwd=str(tmp_path), session="mike")
    rachel = agents_db.create_agent(persona="Rachel", voice_id="V", cwd=str(tmp_path), session="rachel")
    return mike, rachel


def _origin(data):
    return SendRequest.from_payload(
        {"text": "hello", "session": "mike", **data}, default_session="mike",
        trace_id_factory=lambda: "t-r18", authenticated=True).origin


@pytest.mark.parametrize("claimed", ["user", "oracle"])
def test_a_named_sender_cannot_claim_the_users_word(two_agents, claimed):
    assert _origin({"origin": claimed, "sender": "rachel"}) == "agent"


def test_an_agent_naming_itself_as_sender_wakes_itself_not_as_the_user(two_agents):
    assert _origin({"origin": "user", "sender": "mike"}) == "automation"


def test_the_users_own_send_names_no_sender_and_stays_the_users(two_agents):
    assert _origin({"origin": "user"}) == "user"
    assert _origin({}) == "user"
    assert _origin({"origin": "oracle"}) == "oracle"


def test_other_origins_with_a_sender_are_kept(two_agents):
    assert _origin({"origin": "agent", "sender": "rachel"}) == "agent"
    assert _origin({"origin": "schedule", "sender": "rachel"}) == "schedule"
