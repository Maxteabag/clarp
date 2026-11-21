"""The user's choice whether coordination between agents is shown (issue 478, piece 3).

A Host setting (show | hide, default show) with a per-chat override (show | hide | default).
A change is written, then an agent-roster event (kind coordination-changed) carries the new
effective value, and every agent row of the next snapshot carries it: one step, never a later
poll (ruling V). A change writes nothing to any conversation (ruling S)."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from lib import agents, backends, coordination, db, settings_store, snapshot
from lib.routes.agents import AgentRoutes
from lib.routes.settings import SettingsRoutes


@pytest.fixture(autouse=True)
def _no_live_work(monkeypatch):
    monkeypatch.setattr(backends, "active_handles", lambda *a: [])


class _Stream:
    def __init__(self):
        self.events = []

    def broadcast(self, event):
        self.events.append(event)


class _Request(AgentRoutes, SettingsRoutes):
    def __init__(self, body):
        self.body = body
        self.headers = {}
        self.ctx = SimpleNamespace(stream=_Stream())
        self.answer = None

    def _read_json(self):
        return self.body

    def _json_ok(self, payload):
        self.answer = (200, payload)

    def _json_error(self, code, message):
        self.answer = (code, {"error": message})

    def _json(self, code, payload):
        self.answer = (code, payload)


@pytest.fixture
def chats(tmp_path):
    ids = {}
    for session in ("ivy", "rex"):
        ids[session] = agents.create_agent(persona=session.title(), voice_id="v", cwd=str(tmp_path),
                                           session=session, backend="claude")
        agents.start_runtime(ids[session], session)
    return ids


def _rows():
    return {row["session"]: (row["coordination_visibility"], row["coordination_override"])
            for row in snapshot.build_agent_snapshot(None)["agents"]}


def _heads():
    return [tuple(row) for row in db.conn().execute(
        "SELECT * FROM conversation_heads ORDER BY agent_id, backend_session_id")]


def test_the_default_is_hide_and_every_chat_follows_the_host(chats):
    """Nothing stored: coordination is out of the way unless the user asks for it (issue 478)."""
    assert coordination.global_visibility() == "hide"
    assert _rows() == {"ivy": ("hide", None), "rex": ("hide", None)}
    request = _Request(None)
    request._handle_coordination_settings_get()
    assert request.answer == (200, {"coordination_visibility": "hide"})


def test_the_hosts_setting_changes_every_chats_row_in_the_same_step(chats):
    request = _Request({"coordination_visibility": "show"})
    request._handle_coordination_settings_post()
    assert request.answer == (200, {"coordination_visibility": "show"})
    assert [(e["type"], e["kind"], e["coordination_visibility"], e.get("session"))
            for e in request.ctx.stream.events] == [("agent-roster", "coordination-changed", "show", None)]
    assert _rows() == {"ivy": ("show", None), "rex": ("show", None)}


def test_a_chats_own_choice_wins_and_default_follows_the_host_again(chats):
    request = _Request({"session": "ivy", "visibility": "show"})
    request._handle_agent_coordination()
    assert request.answer == (200, {"ok": True, "session": "ivy", "coordination_visibility": "show",
                                    "coordination_override": "show"})
    assert [(e["kind"], e["session"], e["coordination_visibility"]) for e in request.ctx.stream.events] == [
        ("coordination-changed", "ivy", "show")]
    assert _rows() == {"ivy": ("show", "show"), "rex": ("hide", None)}
    coordination.set_global("hide")
    coordination.set_chat(chats["ivy"], "show")
    assert _rows() == {"ivy": ("show", "show"), "rex": ("hide", None)}
    back = _Request({"session": "ivy", "visibility": "default"})
    back._handle_agent_coordination()
    assert back.answer[1]["coordination_visibility"] == "hide" and back.answer[1]["coordination_override"] is None
    assert _rows() == {"ivy": ("hide", None), "rex": ("hide", None)}


@pytest.mark.parametrize("body, handler", [
    ({"coordination_visibility": "sometimes"}, "_handle_coordination_settings_post"),
    ({}, "_handle_coordination_settings_post"),
    ({"session": "ivy", "visibility": "sometimes"}, "_handle_agent_coordination"),
    ({"session": "ivy"}, "_handle_agent_coordination"),
])
def test_an_unknown_value_is_refused_and_nothing_changes(chats, body, handler):
    request = _Request(body)
    getattr(request, handler)()
    assert request.answer[0] == 400 and request.ctx.stream.events == []
    assert _rows() == {"ivy": ("hide", None), "rex": ("hide", None)}


def test_an_unknown_chat_is_404(chats):
    request = _Request({"session": "nobody", "visibility": "hide"})
    request._handle_agent_coordination()
    assert request.answer[0] == 404


def test_a_setting_change_writes_nothing_to_any_conversation(chats):
    """Ruling S: conversation_heads is byte-identical across a change, global or per chat."""
    agents.record_user_message(agent_id=chats["ivy"], backend_session_id="native", client_msg_id="c1",
                               text="hello", trace_id="t1")
    before = _heads()
    _Request({"coordination_visibility": "hide"})._handle_coordination_settings_post()
    _Request({"session": "ivy", "visibility": "show"})._handle_agent_coordination()
    _Request({"session": "rex", "visibility": "default"})._handle_agent_coordination()
    assert _heads() == before and before


@pytest.mark.parametrize("header", ["X-Clarp-Turn", "X-Clarp-Peer"])
@pytest.mark.parametrize("body, handler", [
    ({"coordination_visibility": "hide"}, "_handle_coordination_settings_post"),
    ({"session": "ivy", "visibility": "hide"}, "_handle_agent_coordination"),
])
def test_an_agent_may_not_change_what_the_user_sees_of_coordination(chats, header, body, handler):
    """Z1: a request with a turn token, or from a paired peer Host, would let an agent hide its own
    coordination from the user (or the whole Host's): 403 agents_may_not, nothing written, no event."""
    coordination.set_global("show")                     # the user chose to see it
    request = _Request(body)
    request.headers = {header: "tok-or-peer"}
    getattr(request, handler)()
    assert request.answer == (403, {"error": "agents_may_not"})
    assert request.ctx.stream.events == []
    assert _rows() == {"ivy": ("show", None), "rex": ("show", None)}
    assert coordination.global_visibility() == "show"


@pytest.mark.parametrize("body, handler", [
    ({"coordination_visibility": "hide"}, "_handle_coordination_settings_post"),
    ({"session": "ivy", "visibility": "hide"}, "_handle_agent_coordination"),
])
def test_a_device_still_changes_it(chats, body, handler):
    request = _Request(body)
    request.headers = {"X-Clarp-Turn": "  "}        # an empty header names no turn
    getattr(request, handler)()
    assert request.answer[0] == 200 and len(request.ctx.stream.events) == 1


def test_the_roster_view_says_what_the_separate_reads_said(chats):
    """The snapshot takes the Host's setting and each chat's own choice from the one statement
    that decides which chats hide (roster_view), not from two reads of its own."""
    coordination.set_chat(chats["rex"], "hide")
    view = coordination.roster_view()
    assert view.host == coordination.global_visibility() == "hide"
    assert view.chosen == {chats["rex"]: "hide"}
    assert view.hiding == frozenset(coordination.hiding_agents())
    settings_store.set_text(coordination.GLOBAL_KEY, "hide")
    view = coordination.roster_view()
    assert view.host == "hide" and view.chosen == {chats["rex"]: "hide"}
    for agent_id in chats.values():
        agents.soft_delete(agent_id)
    assert coordination.roster_view() == (frozenset(), "hide", {})
