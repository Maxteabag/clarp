"""Paths outside an agent's own turn that would send its content to another
model provider skip agents on a Codex provider profile; every other agent
keeps today's behaviour."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from lib import agents as agents_db
from lib import codex_profiles

pytestmark = pytest.mark.usefixtures("fake_codex_profile")


@pytest.fixture
def agents():
    plain = agents_db.create_agent(persona="Plain", voice_id="V", cwd="/tmp",
                                   session="plain", backend="codex")
    eu = agents_db.create_agent(persona="Eu", voice_id="V", cwd="/tmp",
                                session="eu", backend="codex", provider_profile="example-eu")
    return SimpleNamespace(plain=plain, eu=eu)


def _row(agent_id):
    return agents_db.get_by_agent_id(agent_id)


def test_janitors_never_take_a_profile_agent_in_scope(agents):
    from lib import janitor_runner, janitors
    attachment = {"agent_id": "janitor", "scope": {}}

    assert janitors._in_scope({}, _row(agents.plain))
    assert not janitors._in_scope({}, _row(agents.eu))
    assert janitor_runner.in_scope(_row(agents.plain), attachment)
    assert not janitor_runner.in_scope(_row(agents.eu), attachment)


def test_a_profile_agent_never_dreams(agents, monkeypatch):
    from lib import dreaming
    agents_db.update_agent(agents.eu, dreaming_enabled=1)
    agents_db.update_agent(agents.plain, dreaming_enabled=1)
    monkeypatch.setattr(dreaming.backends, "spawn_turn",
                        lambda *a, **k: pytest.fail("a dream was dispatched"))

    assert dreaming.dreaming_enabled(_row(agents.plain))
    assert not dreaming.dreaming_enabled(_row(agents.eu))
    assert dreaming.dispatch_isolated_dream(_row(agents.eu), "dream") is False


def test_the_routing_model_sees_a_profile_agents_name_but_not_its_messages(agents):
    from lib.orchestrator import build_context_packet, get_settings, record_routing_message
    record_routing_message(session="plain", role="user", text="check the logs")
    record_routing_message(session="eu", role="user", text="customer invoice 4711")

    packet = build_context_packet(utterance="hi", requested_session="eu",
                                  trace_id="t", hands_free=True, settings=get_settings())

    shown = {agent["session"]: agent for agent in packet["agents"]}
    assert shown["plain"]["recent_user_messages"][-1]["text"] == "check the logs"
    assert shown["eu"]["recent_user_messages"] == []
    assert shown["eu"]["state"] is None or "4711" not in json.dumps(shown["eu"]["state"])
    assert "4711" not in json.dumps(packet)


def test_oracle_cannot_reach_or_read_a_profile_agent(agents, monkeypatch):
    from lib import oracle_attention, oracle_calls, oracle_calls_stable, oracle_contact
    for module in (oracle_calls, oracle_calls_stable):
        tools = module.AgentTools(SimpleNamespace(), "user", "", lambda: None)
        assert [a["session"] for a in tools.roster()] == ["plain"]
    assert [a["session"] for a in oracle_contact.roster()] == ["plain"]
    monkeypatch.setattr(oracle_attention.artifacts, "attention", lambda **_kw: [
        {"status": "pending", "session": session, "decision_id": f"d-{session}",
         "title": "Approve?", "options": []} for session in ("plain", "eu")])

    assert [d["session"] for d in oracle_attention.pending_decisions()] == ["plain"]


def test_the_fleet_map_never_offers_a_profile_agents_commands_to_its_designer(agents):
    from lib import db, viz_normalize
    for agent_id in (agents.plain, agents.eu):
        db.conn().execute(
            "INSERT INTO state_log(agent_id,ts,kind,detail) VALUES (?,1,'tool',?)",
            (agent_id, json.dumps({"tool": "Bash", "input": {
                "command": f"frobnicate-{agent_id[:4]} --customer 4711"}})))

    clusters = viz_normalize.build_fleet_map(0)["_unknown_clusters"]

    assert [c["hint"] for c in clusters] == [f"frobnicate-{agents.plain[:4]}"]


def test_the_live_explainer_never_admits_a_profile_agents_tools(agents):
    from lib import live_explain
    explainer = object.__new__(live_explain.LiveExplainer)
    explainer._current_setting = lambda: pytest.fail("the explainer looked at it")

    explainer._admit(agents.eu, [{"op": "upsert", "kind": "tool", "id": "x",
                                  "item": {"tool": {"command": "cat invoice"}}}])


def test_the_label_checker_skips_a_profile_agent(agents, monkeypatch):
    from lib import label_audit
    idle = SimpleNamespace(busy=False, idle_since_ms=0)
    monkeypatch.setattr(label_audit.stale_work, "activities",
                        lambda rows: {r["agent_id"]: idle for r in rows})
    monkeypatch.setattr(label_audit.snapshot, "shown_status",
                        lambda *_a, **_kw: "Reconciling invoices")
    monkeypatch.setattr(label_audit, "_evidence",
                        lambda item, _now: item["agent"]["agent_id"])

    assert label_audit.candidates(10**13) == [agents.plain]


def test_compaction_and_the_terminal_never_open_a_profile_agent_on_the_default_account(
        agents, monkeypatch):
    from lib import compaction, terminal_ws
    agents_db.bind_backend_session(agents.eu, "eu-thread")
    monkeypatch.setattr(compaction, "_runtime_client", None)
    monkeypatch.setattr(compaction.threading, "Thread",
                        lambda *a, **k: pytest.fail("compaction started"))
    refused = []
    monkeypatch.setattr(terminal_ws, "_send_http_error",
                        lambda _h, status, message: refused.append(status))
    monkeypatch.setattr(terminal_ws, "_serve_pty",
                        lambda *a, **k: pytest.fail("a terminal was opened"))
    handler = SimpleNamespace(headers={"Upgrade": "websocket", "Connection": "Upgrade",
                                       "Sec-WebSocket-Key": "a2V5", "Sec-WebSocket-Version": "13"})

    assert compaction.compact_session("eu")["ok"] is False
    terminal_ws.serve_terminal(handler, "eu")

    assert refused == [501]
