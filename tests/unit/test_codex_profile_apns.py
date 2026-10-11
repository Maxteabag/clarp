"""B1: nothing an EU (provider-profile) agent says reaches Apple.

Every APNs payload the Host builds for a profile agent (alerts, decisions,
background sync, Live Activity state) carries ``provider_profile`` and
``privacy_restricted: true`` ahead of any content field, and generic text in
place of the reply preview, decision question/title or tool label. An ordinary
agent's payloads are unchanged (the control).
"""
from __future__ import annotations

import json

import pytest

from lib import agents as agents_db
from lib import apns, live_activity

pytestmark = pytest.mark.usefixtures("fake_codex_profile")

SENTINEL = "SENTINEL-EU-APNS-41c9"


@pytest.fixture
def sent(monkeypatch):
    """APNs enabled, one device, and a transport that records payloads."""
    from lib import config, desktop_presence, quiet_agents
    real = config.load()

    class Cfg:
        def __getattr__(self, name):
            return getattr(real, name)

        def apns_enabled(self):
            return True

        apns_environment = "sandbox"
        apns_background_sync = True

    payloads: list[dict] = []

    class Transport:
        name = "test"

        def send(self, token, environment, payload, **_kw):
            payloads.append(payload)
            return 200, "", "id"

        def reset(self):
            pass

    monkeypatch.setattr(config, "load", lambda *a, **k: Cfg())
    monkeypatch.setattr(apns, "_transport", lambda _cfg: Transport())
    monkeypatch.setattr(apns, "active_tokens", lambda: [
        {"token": "tok", "environment": "sandbox", "base_url": ""}])
    monkeypatch.setattr(apns, "_avatar_details", lambda *a, **k: ("", False))
    monkeypatch.setattr(apns, "_mark_pushed", lambda _t: None)
    monkeypatch.setattr(apns, "_background_budget_allows", lambda *_a, **_k: True)
    monkeypatch.setattr(desktop_presence, "active", lambda: False)
    monkeypatch.setattr(quiet_agents, "is_quiet", lambda _a: False)
    return payloads


@pytest.fixture
def fleet():
    eu = agents_db.create_agent(persona="Eunice", voice_id="V", cwd="/tmp",
                                session="eunice", backend="codex",
                                provider_profile="example-eu")
    plain = agents_db.create_agent(persona="Plain", voice_id="V", cwd="/tmp",
                                   session="plain", backend="codex")
    return eu, plain


def _marked_first(payload: dict) -> bool:
    keys = list(payload)
    return keys[:2] == ["provider_profile", "privacy_restricted"] \
        and payload["provider_profile"] == "example-eu" \
        and payload["privacy_restricted"] is True


def _notify(agent_id, session, persona, text):
    apns.send_user_notification({
        "push": True, "preview": text, "persona": persona, "session": session,
        "agent_id": agent_id, "notification_id": f"n-{session}",
        "source_message_id": "m", "needs_response": True})


def test_a_reply_alert_for_an_eu_agent_is_generic_and_marked(fleet, sent):
    eu, plain = fleet
    _notify(eu, "eunice", "Eunice", f"Invoice 4711 {SENTINEL}")
    _notify(plain, "plain", "Plain", "plainword reply")

    eu_payload, plain_payload = sent
    assert SENTINEL not in json.dumps(eu_payload)
    assert _marked_first(eu_payload)
    assert eu_payload["aps"]["alert"]["body"] and "subtitle" not in eu_payload["aps"]["alert"]
    assert plain_payload["aps"]["alert"]["body"] == "plainword reply"   # control
    assert "privacy_restricted" not in plain_payload


def test_a_decision_alert_for_an_eu_agent_carries_no_question_or_title(fleet, sent):
    eu, plain = fleet
    for agent_id, session, word in ((eu, "eunice", SENTINEL), (plain, "plain", "plainword")):
        apns.send_decision_created({
            "artifact_id": f"a-{session}", "session": session, "agent_id": agent_id,
            "agent_name": session.title(), "title": f"Approve {word}?",
            "decision": {"decision_id": f"d-{session}", "status": "pending",
                         "question": f"Pay {word} now?", "response_type": "text_input"}})

    eu_payload, plain_payload = sent
    assert SENTINEL not in json.dumps(eu_payload)
    assert _marked_first(eu_payload)
    assert eu_payload["decision_id"] == "d-eunice"           # still deep-links
    assert "plainword" in json.dumps(plain_payload)          # control


def test_a_background_sync_for_an_eu_agent_is_marked(fleet, sent):
    eu, plain = fleet
    apns.send_background_sync("eunice", eu)
    apns.send_background_sync("plain", plain)

    eu_payload, plain_payload = sent
    assert _marked_first(eu_payload)
    assert "privacy_restricted" not in plain_payload


def test_live_activity_state_for_an_eu_agent_is_phase_only(fleet):
    eu, plain = fleet
    activity = {"state": "tool", "headline": f"Reading {SENTINEL}",
                "tool": {"name": "Read", "label": f"/srv/{SENTINEL}.pdf"},
                "turn_started_ms": 2}
    state = live_activity.content_state({
        eu: {"session": "eunice", "persona": "Eunice", "activity": activity},
        plain: {"session": "plain", "persona": "Plain", "activity": {
            **activity, "headline": "", "tool": {"name": "Read", "label": "/srv/plainword.pdf"},
            "turn_started_ms": 1}},
    }, now_ms=10)

    pushed = json.dumps(live_activity.payload(state, 10.0))
    assert SENTINEL not in pushed
    rows = {row["agent_id"]: row for row in state["agents"]}
    assert rows[eu]["privacy_restricted"] is True
    assert rows[eu]["provider_profile"] == "example-eu"
    assert rows[eu]["tool"] is None and rows[eu]["state"] == "tool"
    assert rows[plain]["headline"] == "Reading plainword.pdf"   # control
