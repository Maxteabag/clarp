"""Conversation and session views of a chat (issue 702): the row's view, the
/log filter, the re-tag when a turn ends, the agent setting and the turn brief."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from lib import agents, backends, chat_view, db, message_store, message_writes, snapshot
from lib.routes.agents import AgentRoutes


@pytest.fixture(autouse=True)
def _no_live_work(monkeypatch):
    monkeypatch.setattr(backends, "active_handles", lambda *a: [])


SESSION = "c1"
_SEQ = [0]


def _agent(session="ivy", tmp="/tmp"):
    aid = agents.create_agent(persona=session.title(), voice_id="v", cwd=tmp, session=session,
                              backend="claude")
    agents.start_runtime(aid, session)
    return aid


def _row(aid, mid, role, text, *, trace="t1", tool_name=None, kind=None, origin="user", phase=None):
    _SEQ[0] += 1
    database = db.conn()
    database.execute(
        """INSERT INTO messages (message_id, agent_id, backend_session_id, source_file, seq, role,
               timestamp, text, kind, tool_name, tools_json, display_cells_json, updated_at,
               revision, origin, trace_id, phase)
           VALUES (?, ?, ?, 'x.jsonl', ?, ?, ?, ?, ?, ?, '[]', '[]', ?, ?, ?, ?, ?)""",
        (mid, aid, SESSION, _SEQ[0], role, f"2026-11-22T10:00:{_SEQ[0]:02d}.000Z", text, kind,
         tool_name, db.now_ms(), message_writes._next_revision(database), origin, trace, phase))


def _turn(aid, trace="t1", *, settled=False, outcome="completed"):
    db.conn().execute(
        "INSERT INTO turns (agent_id, source, trace_id, started_at, ended_at, settled_at, outcome)"
        " VALUES (?, 'pwa', ?, 1, ?, ?, ?)",
        (aid, trace, 2 if settled else None, 2 if settled else None, outcome if settled else None))


def _settle(aid, trace="t1"):
    db.conn().execute("UPDATE turns SET ended_at = 2, settled_at = 2, outcome = 'completed'"
                      " WHERE agent_id = ? AND trace_id = ?", (aid, trace))
    message_writes.restamp_turn(aid, trace)


def _a_turn(aid):
    """The user asks; the agent narrates, works, notes, and answers."""
    _row(aid, "u1", "user", "fix it", origin="user")
    _row(aid, "a1", "assistant", "Looking at the logs.")
    _row(aid, "tool1", "assistant", "", tool_name="Bash")
    _row(aid, "n1", "assistant", "Found it", kind="note")
    _row(aid, "a2", "assistant", "Fixed: the cache key was wrong.")


def _views(aid, **kw):
    return {r["id"]: r["view"] for r in message_store.list_messages(
        agent_id=aid, backend_session_id=SESSION, **kw)}


def test_a_running_turn_has_no_final_reply_and_its_end_retags_it_by_revision(tmp_path):
    aid = _agent(tmp=str(tmp_path))
    _turn(aid)
    _a_turn(aid)
    assert _views(aid) == {"u1": "conversation", "a1": "session", "tool1": "session",
                           "n1": "conversation", "a2": "session"}
    before = message_store.latest_revision(agent_id=aid, backend_session_id=SESSION)
    _settle(aid)
    delta = {r["id"]: r for r in message_store.list_messages(
        agent_id=aid, backend_session_id=SESSION, after_revision=before)}
    assert delta["a2"]["view"] == "conversation"
    assert delta["a2"]["session_rows"] == 2          # a1 and the tool row
    assert _views(aid)["a1"] == "session"


def test_a_later_reply_moves_the_anchor_and_both_rows_come_again(tmp_path):
    aid = _agent(tmp=str(tmp_path))
    _turn(aid)
    _a_turn(aid)
    _settle(aid)
    before = message_store.latest_revision(agent_id=aid, backend_session_id=SESSION)
    _row(aid, "a3", "assistant", "One more thing.")
    message_writes.restamp_turn(aid, "t1")
    delta = {r["id"]: r for r in message_store.list_messages(
        agent_id=aid, backend_session_id=SESSION, after_revision=before)}
    assert delta["a2"]["view"] == "session" and "session_rows" not in delta["a2"]
    assert delta["a3"]["view"] == "conversation" and delta["a3"]["session_rows"] == 3


def test_a_turn_with_no_text_anchors_on_its_last_note_or_prompt(tmp_path):
    aid = _agent(tmp=str(tmp_path))
    _turn(aid)
    _row(aid, "u1", "user", "run it", origin="user")
    _row(aid, "tool1", "assistant", "", tool_name="Bash")
    _settle(aid)
    rows = {r["id"]: r for r in message_store.list_messages(agent_id=aid, backend_session_id=SESSION)}
    assert rows["u1"]["session_rows"] == 1 and rows["u1"]["view"] == "conversation"
    assert rows["tool1"]["view"] == "session"


def test_other_prompts_untraced_rows_and_markers(tmp_path):
    aid = _agent(tmp=str(tmp_path))
    _row(aid, "s1", "user", "scheduled check", origin="schedule", trace="")
    _row(aid, "x1", "assistant", "old final", trace="", phase="final")
    _row(aid, "x2", "assistant", "old commentary", trace="", phase="commentary")
    _row(aid, "m1", "assistant", "Turn interrupted", origin="system", trace="")
    assert _views(aid) == {"s1": "session", "x1": "conversation", "x2": "session",
                           "m1": "conversation"}


def test_view_conversation_filters_before_the_limit(tmp_path):
    aid = _agent(tmp=str(tmp_path))
    for n in range(3):
        trace = f"t{n}"
        _turn(aid, trace)
        _row(aid, f"u{n}", "user", "go", origin="user", trace=trace)
        for k in range(5):
            _row(aid, f"w{n}{k}", "assistant", "", tool_name="Read", trace=trace)
        _row(aid, f"r{n}", "assistant", "done", trace=trace)
        _settle(aid, trace)
    page = message_store.list_messages(agent_id=aid, backend_session_id=SESSION, limit=4,
                                       view="conversation")
    assert [r["id"] for r in page] == ["u1", "r1", "u2", "r2"]
    assert all(r["view"] == "conversation" for r in page)
    older = message_store.list_messages(agent_id=aid, backend_session_id=SESSION, limit=4,
                                        view="conversation", before_message_id="u1")
    assert [r["id"] for r in older] == ["u0", "r0"]
    assert len(message_store.list_messages(agent_id=aid, backend_session_id=SESSION, limit=100)) == 21


def test_the_log_read_passes_the_view_and_echoes_it(tmp_path, monkeypatch):
    from lib import conversation
    aid = _agent(tmp=str(tmp_path))
    _turn(aid)
    _a_turn(aid)
    monkeypatch.setattr(conversation, "_live_backend_session", lambda agent_id: SESSION)
    monkeypatch.setattr(conversation.backends, "find_session_jsonl", lambda backend, sid: None)
    body = conversation.load_conversation(session="ivy", view="conversation")
    assert body["view"] == "conversation"
    assert [t["id"] for t in body["turns"]] == ["u1", "n1"]
    assert "view" not in conversation.load_conversation(session="ivy")


class _Stream:
    def __init__(self):
        self.events = []

    def broadcast(self, event):
        self.events.append(event)


class _Request(AgentRoutes):
    def __init__(self, body, headers=None):
        self.body, self.headers = body, headers or {}
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


def _snapshot_views():
    return {row["session"]: row["chat_view"] for row in snapshot.build_agent_snapshot(None)["agents"]}


def test_the_setting_is_stored_announced_and_in_the_snapshot(tmp_path):
    _agent("ivy", str(tmp_path))
    _agent("rex", str(tmp_path))
    assert _snapshot_views() == {"ivy": "session", "rex": "session"}
    request = _Request({"session": "ivy", "chat_view": "conversation"})
    request._handle_agent_chat_view()
    assert request.answer == (200, {"ok": True, "session": "ivy", "chat_view": "conversation"})
    assert [(e["type"], e["kind"], e["session"], e["chat_view"]) for e in request.ctx.stream.events] == [
        ("agent-roster", "chat_view", "ivy", "conversation")]
    assert _snapshot_views() == {"ivy": "conversation", "rex": "session"}


@pytest.mark.parametrize("body, headers, code", [
    ({"session": "ivy", "chat_view": "compact"}, None, 400),
    ({"session": "ivy"}, None, 400),
    ({"session": "nobody", "chat_view": "session"}, None, 404),
    ({"session": "ivy", "chat_view": "conversation"}, {"X-Clarp-Turn-Token": "t"}, 403),
])
def test_a_bad_request_changes_nothing(tmp_path, body, headers, code, monkeypatch):
    from lib import http_utils
    _agent("ivy", str(tmp_path))
    if headers:
        monkeypatch.setattr(http_utils, "from_an_agent", lambda h: True)
    request = _Request(body, headers)
    request._handle_agent_chat_view()
    assert request.answer[0] == code
    assert _snapshot_views() == {"ivy": "session"}


def test_the_turn_brief_says_it_only_in_conversation_view(tmp_path):
    from lib import turn_command
    aid = _agent("ivy", str(tmp_path))
    assert "Conversation view" not in turn_command._turn_protocol(aid, origin="user")
    chat_view.set_agent_view(aid, "conversation")
    brief = turn_command._turn_protocol(aid, origin="user")
    assert "Conversation view" in brief and "clarp-admin tell-user --from ivy" in brief
    chat_view.set_agent_view(aid, "session")
    assert "Conversation view" not in turn_command._turn_protocol(aid, origin="user")


def test_the_upgrade_anchors_turns_that_had_settled(tmp_path):
    from lib import db_migrations
    aid = _agent(tmp=str(tmp_path))
    _turn(aid, settled=True)
    _a_turn(aid)
    database = db.conn()
    database.execute("UPDATE messages SET session_rows = NULL")
    columns = [r[1] for r in database.execute("PRAGMA table_info(messages)")]
    assert "session_rows" in columns
    db_migrations._backfill_chat_view_anchors(database, chat_view.anchor)
    anchored = [tuple(r) for r in database.execute(
        "SELECT message_id, session_rows FROM messages WHERE session_rows IS NOT NULL")]
    assert anchored == [("a2", 2)]
