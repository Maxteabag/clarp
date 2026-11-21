"""What a chat list shows of coordination (issue 478, feature coordination_working_state): the
snapshot's preview and working state follow the chat's coordination setting as GET /log
coordination=auto does, a late reader learns who a running turn is for, and the agents-working
push names the partner, never the hidden work."""
from __future__ import annotations

import pytest

from lib import agents, backends, coordination, db, live_activity, message_writes, snapshot
from lib.state_watcher import StateLogWatcher
from test_coordination_filter import _Chat


@pytest.fixture(autouse=True)
def _no_live_work(monkeypatch):
    monkeypatch.setattr(backends, "active_handles", lambda *a: [])


@pytest.fixture
def chat(tmp_path):
    return _Chat(tmp_path)


def _row(chat):
    return next(r for r in snapshot.build_agent_snapshot(None)["agents"] if r["agent_id"] == chat.id)


def _running(chat, trace, monkeypatch):
    """The chat's turn ``trace`` is running: an open turns row, a working state and a live handle
    (so the snapshot's repair of a stuck busy row leaves it alone)."""
    from lib import reconcile
    monkeypatch.setattr(backends, "active_handles", lambda backend, agent_id=None, *a: [object()]
                        if agent_id == chat.id else [])
    monkeypatch.setattr(reconcile, "reconcile_agent", lambda *a, **k: set())
    db.conn().execute("INSERT INTO turns(agent_id, source, trace_id, started_at) VALUES (?, 'test', ?, 1)",
                      (chat.id, trace))
    agents.record_state(chat.id, "tool", {"trace_id": trace, "tool": "Bash",
                                          "input": {"command": "cat secret-plan.md"}})


# ---- the preview ------------------------------------------------------------------------------

def test_a_hidden_coordination_reply_is_not_the_preview_of_a_chat_that_hides_it(chat):
    chat.user_turn("u1")
    chat.coordination_turn("c1")
    assert "reply c1" in _row(chat)["last_message"]                 # 'show': as before
    coordination.set_chat(chat.id, "hide")
    row = _row(chat)
    assert row["last_message"] == "answer u1" and "c1" not in row["last_completed_message"]
    coordination.set_chat(chat.id, "show")                          # no row was written: the key moved
    assert "reply c1" in _row(chat)["last_message"]


def test_a_note_in_a_hidden_turn_is_the_preview(chat):
    chat.user_turn("u1")
    chat.coordination_turn("c1")
    message_writes.record_note(agent_id=chat.id, backend_session_id="native", note_id="n1",
                               text="For you.", trace_id="c1")
    coordination.set_global("hide")
    assert _row(chat)["last_message"] == "For you."


def test_an_agent_that_cannot_leave_a_note_keeps_its_preview(chat):
    chat.user_turn("u1")
    chat.coordination_turn("c1")
    db.conn().execute("UPDATE agents SET backend = 'codex' WHERE agent_id = ?", (chat.id,))
    coordination.set_global("hide")
    assert "reply c1" in _row(chat)["last_message"]


# ---- the working state ------------------------------------------------------------------------

def test_a_snapshot_says_who_a_running_turn_is_for(chat, monkeypatch):
    chat.user_turn("u1")
    assert _row(chat)["working_audience"] is None and _row(chat)["working_with"] is None
    chat.coordination_turn("c1")
    _running(chat, "c1", monkeypatch)
    row = _row(chat)
    assert row["working_audience"] == "agents"
    assert row["working_with"] == {"agent_id": chat.boss, "name": "Boss"}


def test_a_users_running_turn_reads_user(chat, monkeypatch):
    chat.user_turn("u1")
    _running(chat, "u1", monkeypatch)
    row = _row(chat)
    assert row["working_audience"] == "user" and row["working_with"] is None


def test_a_hiding_chat_shows_who_its_agent_works_with_not_what_it_does(chat, monkeypatch):
    chat.coordination_turn("c1")
    _running(chat, "c1", monkeypatch)
    shown = _row(chat)["activity"]
    assert "secret-plan" in (shown["summary"] + shown["file_path"] + shown["action"])
    coordination.set_chat(chat.id, "hide")
    veiled = _row(chat)["activity"]
    assert veiled["summary"] == "Working with Boss" and veiled["tool"] == "" and veiled["file_path"] == ""
    assert "secret-plan" not in repr(veiled)


def test_every_working_state_carries_the_turns_audience(chat):
    chat.coordination_turn("c1")
    watcher = StateLogWatcher(stream=None)
    fields = watcher._working_with(chat.id, "tool", {"trace_id": "c1"})
    assert fields == {"audience": "agents", "sender_agent_id": chat.boss, "sender_name": "Boss"}
    assert watcher._working_with(chat.id, "done", {"trace_id": "c1"}) == {}
    chat.user_turn("u1")
    assert watcher._working_with(chat.id, "thinking", {"trace_id": "u1"}) == {"audience": "user"}


# ---- the agents-working push ------------------------------------------------------------------

def _pushed_lead(chat, activity):
    pusher = live_activity.LiveActivityPusher(lambda payload, token: None, schedule=lambda *a: None)
    pusher.observe({"type": "live", "agent_id": chat.id, "session": "ivy",
                    "ops": [{"op": "status", "activity": activity}]})
    return live_activity.content_state(pusher._agents, 0)["lead"]


def test_the_push_names_the_partner_not_the_hidden_work(chat):
    chat.coordination_turn("c1")
    activity = {"state": "tool", "turn_id": "c1", "headline": "Reading secret-plan.md",
                "tool": {"name": "Read", "label": "secret-plan.md", "category": "read"}}
    assert "secret-plan" in _pushed_lead(chat, activity)["headline"]
    coordination.set_global("hide")
    lead = _pushed_lead(chat, activity)
    assert lead["headline"] == "Working with Boss" and lead["tool"] is None


def test_the_push_of_a_users_turn_is_unchanged_under_hide(chat):
    chat.user_turn("u1")
    coordination.set_global("hide")
    activity = {"state": "thinking", "turn_id": "u1", "headline": "Thinking: the plan"}
    assert _pushed_lead(chat, activity)["headline"] == "Thinking: the plan"


def test_a_coordination_turn_of_an_agent_that_cannot_leave_a_note_reads_user(chat, monkeypatch):
    """The working audience is the one /log gives the turn's rows: such an agent's turns are
    shown, so they read 'user' and name no partner (F6-b, F6B)."""
    db.conn().execute("UPDATE agents SET backend = 'codex' WHERE agent_id = ?", (chat.id,))
    chat.coordination_turn("c1")
    _running(chat, "c1", monkeypatch)
    row = _row(chat)
    assert row["working_audience"] == "user" and row["working_with"] is None


class _Stream:
    def __init__(self):
        self.events = []

    def broadcast(self, event):
        self.events.append(event)

    def broadcast_ephemeral(self, event):
        self.events.append(event)


def test_the_state_stream_of_a_hidden_turn_names_the_partner_not_the_work(chat):
    """After 558 the state watcher's agent-activity and agent-state still carried the tool, its
    input and summary of a coordination turn a chat hides; clients draw the row from them."""
    chat.coordination_turn("c1")
    coordination.set_global("hide")
    stream = _Stream()
    watcher = StateLogWatcher(stream)
    agents.record_state(chat.id, "tool", {"trace_id": "c1", "tool": "Bash",
                                          "input": {"command": "cat secret-plan.md"}})
    watcher._poll_once()
    mine = [e for e in stream.events if (e.get("agent_id") if isinstance(e, dict) else None) == chat.id]
    assert mine and "secret-plan" not in repr(mine)
    activity = next(e for e in mine if e.get("type") == "agent-activity")
    assert activity["summary"] == "Working with Boss" and activity["tool"] == ""
    state = next(e for e in mine if e.get("type") == "agent-state")
    assert state["audience"] == "agents" and state["sender_name"] == "Boss"


def test_the_state_stream_of_a_shown_turn_is_unchanged(chat):
    chat.coordination_turn("c1")                       # 'show': nothing veiled
    stream = _Stream()
    watcher = StateLogWatcher(stream)
    agents.record_state(chat.id, "tool", {"trace_id": "c1", "tool": "Bash",
                                          "input": {"command": "cat secret-plan.md"}})
    watcher._poll_once()
    assert "secret-plan" in repr([e for e in stream.events if isinstance(e, dict) and e.get("agent_id") == chat.id])


def test_a_hidden_turn_waiting_for_an_account_still_says_so():
    """review2 on 560: the veil keeps account_recovery, so such a turn reads as waiting."""
    detail = {"trace_id": "c1", "account_recovery": "waiting", "tool": "Bash", "input": {"command": "x"}}
    kept = coordination.veil_detail(detail, "Working with Boss")
    assert kept == {"trace_id": "c1", "account_recovery": "waiting", "summary": "Working with Boss"}
    activity = {"phase": "account_recovery", "action": "waiting for account",
                "summary": "Waiting for an account with available usage", "tool": "Bash", "file_path": "/x"}
    veiled = coordination.veil_activity(activity, "Working with Boss")
    assert veiled["action"] == "waiting for account" and veiled["tool"] == "" and veiled["file_path"] == ""


def test_a_parents_row_describes_a_helpers_hidden_turn_by_who_it_works_with(chat):
    """Arnold on 561: a parent's status line built from its helper's activity follows the
    helper's hide rule: 'Working with <Name>' for its coordination turn, not the work."""
    db.conn().execute("UPDATE agents SET parent_agent_id = ?, role = 'helper', helper_state = 'running'"
                      " WHERE agent_id = ?", (chat.boss, chat.id))
    chat.coordination_turn("c1")
    agents.record_state(chat.id, "tool", {"trace_id": "c1", "tool": "Bash",
                                          "input": {"command": "cat secret-plan.md"}})

    def parent_status():
        rows = snapshot.build_agent_snapshot(None)["agents"]
        return next(r for r in rows if r["agent_id"] == chat.boss)["status_text"] or ""

    assert "secret-plan" in parent_status()
    coordination.set_chat(chat.id, "hide")
    shown = parent_status()
    assert "Working with Boss" in shown and "secret-plan" not in shown
