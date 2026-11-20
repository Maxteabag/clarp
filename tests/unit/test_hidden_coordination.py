"""Coordination between agents (issue 478), first part: the mark and the note.

A turn another agent's message started is coordination: every row of it carries
``audience: agents`` on /log, read from the turn's trace and never from a row's
origin (the transcript import rewrites that on every pass). The user may keep it
out of the chat later (feature ``hidden_coordination``); nothing is filtered yet.
An agent shows something to the user only by an explicit act: a note
(``POST /agent-notes``), always shown and notified on its own."""
from __future__ import annotations

import json
import os
import time
from types import SimpleNamespace

import pytest

from lib import (agent_notes, agents, clock, coordination, db, events, live_core, message_store,
                 provider_background_jobs as provider_jobs, transcript_incremental, turn_lifecycle,
                 user_notifications)
from lib.policies import notifications as notification_policy
from lib.routes.agents import AgentRoutes
from lib.turn_command import _turn_protocol
from lib.turn_dispatch import TurnDispatchService
from lib.turn_handoff import _working_with

BASE = 1_795_000_000_000


class _World:
    """One agent (``worker``) with a Claude transcript, and two others that may message it."""

    def __init__(self, tmp_path, monkeypatch):
        self.now = [BASE]
        monkeypatch.setattr(time, "time", lambda: self.now[0] / 1000)
        self.id = agents.create_agent(persona="Worker", session="worker", voice_id="v",
                                      cwd=str(tmp_path), backend="claude")
        self.boss = agents.create_agent(persona="Boss", session="boss", voice_id="v", cwd=str(tmp_path))
        self.peer = agents.create_agent(persona="Peer", session="peer", voice_id="v", cwd=str(tmp_path))
        agents.bind_backend_session(self.id, "native")
        self.path = tmp_path / "native.jsonl"
        self.path.write_text("")
        self.step = 0

    def _line(self, role, content, at, message_id=None):
        message = {"role": role, "model": "synthetic-fixture", "content": content}
        if message_id:
            message["id"] = message_id
        with self.path.open("a") as stream:
            stream.write(json.dumps({"type": role, "uuid": f"{role}-{at}", "timestamp": clock.iso_from_ms(at),
                                     "message": message}) + "\n")

    def send(self, trace, text, *, origin="user", sender=None, client=None):
        agents.record_user_message(agent_id=self.id, backend_session_id="native", client_msg_id=client or trace,
                                   text=text, trace_id=trace, origin=origin, sender_agent_id=sender)

    def turn(self, trace, text, answer, *, origin="user", sender=None, settle=True):
        """A whole turn: the prompt, a tool call, a live row, the transcript's reply."""
        self.step += 1
        t0 = BASE + self.step * 60_000
        self.now[0] = t0
        self.send(trace, text, origin=origin, sender=sender)
        self.now[0] = t0 + 500
        agents.open_turn(agent_id=self.id, source="pwa", trace_id=trace)
        turn_lifecycle.transition(self.id, turn_lifecycle.TurnEvent.PROMPT_ADMITTED,
                                  {"trace_id": trace, "origin": origin})
        self._line("user", text, t0 + 1000)
        self._line("assistant", [{"type": "tool_use", "id": f"toolu_{self.step}", "name": "Bash",
                                  "input": {"command": "ls"}}], t0 + 2000, f"msg_tool_{self.step}")
        self._line("user", [{"type": "tool_result", "tool_use_id": f"toolu_{self.step}", "content": "ok"}],
                   t0 + 3000)
        self.now[0] = t0 + 4000
        message_store.upsert_live_assistant_message(agent_id=self.id, backend_session_id="native",
                                                    trace_id=trace, item_key=f"msg_text_{self.step}",
                                                    text=answer[:10])
        self._line("assistant", [{"type": "text", "text": answer}], t0 + 5000, f"msg_text_{self.step}")
        if settle:
            self.now[0] = t0 + 6000
            turn_lifecycle.transition(self.id, turn_lifecycle.TurnEvent.PROCESS_EXITED_OK, {"trace_id": trace})
        self.imported()

    def imported(self):
        self.now[0] += 3
        transcript_incremental.import_transcript(backend="claude", path=self.path, agent_id=self.id,
                                                 backend_session_id="native")
        db.close_local()

    def log(self):
        return agents.list_messages(agent_id=self.id, backend_session_id="native", limit=500)

    def audiences(self, trace):
        return sorted({row["audience"] for row in self.log() if row.get("trace_id") == trace})


@pytest.fixture
def world(tmp_path, monkeypatch):
    return _World(tmp_path, monkeypatch)


# ---- who decides -----------------------------------------------------------------------------

def test_coordination_is_another_agent_addressing_this_one():
    is_c = coordination.is_coordination
    assert is_c(agent_id="a", origin="agent", sender_agent_id="b")
    assert is_c(agent_id="a", origin="automation", sender_agent_id="b")     # a sender is a sender
    assert is_c(agent_id="a", origin="agent", sender_agent_id="")           # a peer Host's agent
    assert not is_c(agent_id="a", origin="agent", sender_agent_id="a")      # an agent to itself
    for origin in ("user", "oracle", "automation", "schedule", "watcher", "heartbeat", ""):
        assert not is_c(agent_id="a", origin=origin, sender_agent_id=None), origin


# ---- the mark: every row of the turn, from the trace -----------------------------------------

def test_every_row_of_a_turn_another_agent_started_is_for_agents(world):
    world.turn("t-user", "Hello there, please list the files", "Here are the files you asked for.")
    world.turn("t-boss", "Report on the build please, briefly", "The build is green on every target.",
               origin="agent", sender=world.boss)
    rows = [row for row in world.log() if row["trace_id"] == "t-boss"]
    assert {row["role"] for row in rows} == {"user", "assistant"} and len(rows) >= 3   # prompt, tool, reply
    assert world.audiences("t-boss") == ["agents"]
    assert world.audiences("t-user") == ["user"]
    # The turn summary sits on its last row, which is a coordination row too.
    assert any(row.get("turn") for row in rows)


def test_the_mark_survives_every_import_and_the_rows_origin_it_rewrites(world):
    world.turn("t-boss", "Report on the build please, briefly", "The build is green on every target.",
               origin="agent", sender=world.boss)
    for _ in range(3):
        world.imported()
        assert world.audiences("t-boss") == ["agents"]
    # The import rewrites a reply's origin from the latest user row; the mark does not follow it.
    with db.conn() as con:
        con.execute("UPDATE messages SET origin='user', sender_agent_id=NULL"
                    " WHERE trace_id='t-boss' AND role='assistant'")
    db.close_local()
    assert world.audiences("t-boss") == ["agents"]


def test_a_message_from_an_agent_of_a_peer_host_is_coordination_and_one_to_itself_is_not(world):
    world.turn("t-peer-host", "From the other Host: status?", "Status: all good here.", origin="agent")
    world.turn("t-self", "Continue with the task", "Continuing now.", origin="agent", sender=world.id)
    assert world.audiences("t-peer-host") == ["agents"]
    assert world.audiences("t-self") == ["user"]


def test_a_retried_message_keeps_its_mark_under_its_new_trace(world):
    world.send("t-first", "Please check the logs", origin="agent", sender=world.boss, client="c1")
    message_store.relink_client_message("c1", trace_id="t-second")
    assert coordination.audience_of(world.id, "t-second") == "agents"
    assert coordination.sender_of(world.id, "t-second") == world.boss


def test_a_turn_the_user_started_is_never_hidden_whatever_arrives_in_it(world):
    """An agent's message steered into the user's turn is a row of its own trace: only it is marked."""
    world.turn("t-user", "Hello there, please list the files", "Here are the files you asked for.",
               settle=False)
    world.send("t-steer", "Also include the hidden files", origin="agent", sender=world.boss)
    world.imported()
    steered = [row for row in world.log() if row["id"] == "u-t-steer"]
    assert [row["audience"] for row in steered] == ["agents"]
    assert world.audiences("t-user") == ["user"]


# ---- 'mixed' only loosens ---------------------------------------------------------------------

def test_a_coordination_turn_the_user_steps_into_is_shown_from_then_on_and_never_hidden_again(world):
    world.turn("t-boss", "Report on the build please, briefly", "The build is green on every target.",
               origin="agent", sender=world.boss)
    before = {row["id"]: row["revision"] for row in world.log() if row["trace_id"] == "t-boss"}
    head = db.conn().execute("SELECT revision FROM conversation_heads WHERE agent_id=?", (world.id,)).fetchone()[0]
    assert coordination.loosen(world.id, "t-boss") == ["native"]
    after = {row["id"]: row for row in world.log() if row["trace_id"] == "t-boss"}
    assert {row["audience"] for row in after.values()} == {"user"}
    revisions = [row["revision"] for row in after.values()]
    assert all(after[i]["revision"] > before[i] for i in before) and len(set(revisions)) == len(revisions)
    assert db.conn().execute("SELECT revision FROM conversation_heads WHERE agent_id=?",
                             (world.id,)).fetchone()[0] == max(revisions) > head
    delta = agents.list_messages(agent_id=world.id, backend_session_id="native", after_revision=head)
    assert {row["id"] for row in delta} >= set(before)          # a delta carries them again
    assert coordination.loosen(world.id, "t-boss") == []        # once
    with db.conn() as con:                                      # a second mark never takes it back
        coordination.mark(con, agent_id=world.id, trace_id="t-boss", sender_agent_id=world.boss)
    db.close_local()
    assert world.audiences("t-boss") == ["user"]


class _Stream:
    def __init__(self):
        self.events = []

    def broadcast(self, event):
        self.events.append(event)


def test_a_users_steer_into_a_coordination_turn_says_so_on_the_live_channel_at_once(world):
    sent = []
    hub = live_core.LiveHub(sink=sent.append)
    live_core.install(hub)
    try:
        world.turn("t-boss", "Report on the build please, briefly", "The build is green.",
                   origin="agent", sender=world.boss, settle=False)
        hub.begin_turn(agent_id=world.id, session="worker", conv="native", turn_id="t-boss")
        assert hub.snapshot(agent_id=world.id)["turn"]["audience"] == "agents"
        service = SimpleNamespace(ctx=SimpleNamespace(stream=_Stream()))
        spec = SimpleNamespace(agent_id=world.id, session="worker", origin="user", sender_agent_id="")
        TurnDispatchService._steered_into(service, spec, "t-boss")
        turn_ops = [op for event in sent for op in event["ops"] if op["op"] == "turn"]
        assert turn_ops[-1]["turn"] == {**turn_ops[0]["turn"], "audience": "user"}
        assert hub.snapshot(agent_id=world.id)["turn"]["audience"] == "user"   # GET /live, reconnecting
        assert [e["type"] for e in service.ctx.stream.events] == ["transcript-updated"]
        # Another agent's message steered in changes nothing.
        world.turn("t-boss-2", "One more report please", "Done.", origin="agent", sender=world.boss,
                   settle=False)
        peer = SimpleNamespace(agent_id=world.id, session="worker", origin="agent", sender_agent_id=world.peer)
        TurnDispatchService._steered_into(service, peer, "t-boss-2")
        assert coordination.audience_of(world.id, "t-boss-2") == "agents"
    finally:
        live_core.install(None)


def test_the_working_state_says_who_the_agent_is_working_with(world):
    world.send("t-boss", "Report please", origin="agent", sender=world.boss)
    world.send("t-peer-host", "From afar", origin="agent")
    world.send("t-user", "Hello")
    spec = lambda trace: SimpleNamespace(agent_id=world.id, trace_id=trace)  # noqa: E731
    assert _working_with(spec("t-boss")) == {"audience": "agents", "sender_agent_id": world.boss,
                                              "sender_name": "Boss"}
    assert _working_with(spec("t-peer-host")) == {"audience": "agents"}
    assert _working_with(spec("t-user")) == {"audience": "user"}
    event = events.agent_state(session="worker", agent_id=world.id, kind="thinking", **_working_with(spec("t-boss")))
    assert event["audience"] == "agents" and event["sender_name"] == "Boss"


# ---- unread and push never fire for these turns (requirement f) -------------------------------

@pytest.mark.parametrize("content", ["speak", "text-reply"])
def test_hidden_coordination_turn_never_lights_the_chat_up(content):
    decision = notification_policy.notify_decision(
        "agent", notification_policy.CompletedTurn(agent_id="a", has_cause=True, content=content),
        notification_policy.AgentCapabilities(present=True), notification_policy.NotificationSettings())
    assert isinstance(decision, notification_policy.Suppress) and decision.reason == "agent-chatter:agent"


# ---- the reminder: only when the chat hides coordination ---------------------------------------

def _set_visibility(*, chat=None, host=None, agent_id=""):
    from lib import settings_store
    if host is not None:
        settings_store.set_text(coordination.GLOBAL_KEY, host)
    if chat is not None:
        with db.conn() as con:
            con.execute("UPDATE agents SET coordination_visibility=? WHERE agent_id=?", (chat, agent_id))
        db.close_local()


def test_the_reminder_tells_the_truth_for_the_chat_at_that_moment(world):
    brief = lambda origin="agent", sender=world.boss: _turn_protocol(  # noqa: E731
        world.id, origin=origin, sender_agent_id=sender)
    assert coordination.effective_visibility(world.id) == "show" and brief() == ""   # the default
    _set_visibility(host="hide")
    assert "clarp-admin tell-user --from worker" in brief() and "(Boss)" in brief()
    assert brief(origin="user", sender="") == ""                 # the user's own turn
    assert brief(sender=world.id) == ""                           # an agent to itself
    _set_visibility(chat="show", agent_id=world.id)              # this chat shows it
    assert brief() == ""
    _set_visibility(host="show", chat="hide", agent_id=world.id)
    assert "tell-user" in brief()


# ---- the note --------------------------------------------------------------------------------

class _Request(AgentRoutes):
    def __init__(self, body, token=""):
        self.headers = {"X-Clarp-Turn": token} if token else {}
        self.body = body
        self.ctx = SimpleNamespace(stream=_Stream())
        self.answer = None

    def _read_json(self):
        return self.body

    def _json_ok(self, payload):
        self.answer = (200, payload)

    def _json_error(self, code, message):
        self.answer = (code, {"error": message})


def _token(agent_id, trace):
    token = provider_jobs.new_turn_token()
    provider_jobs.turn_started(token, agent_id=agent_id, provider="claude", pid=os.getpid(), trace_id=trace)
    return token


def _note(text, *, token="", session="worker", note_id=None):
    body = {"session": session, "text": text}
    if note_id:
        body["note_id"] = note_id
    request = _Request(body, token)
    request._handle_agent_note()
    return request


@pytest.fixture
def pushes(monkeypatch):
    from lib import apns
    sent = []
    monkeypatch.setattr(apns, "on_user_notification", sent.append)
    return sent


def test_a_note_from_a_coordination_turn_is_shown_notified_and_counted(world, pushes):
    world.turn("t-boss", "Report on the build please, briefly", "The build is green.",
               origin="agent", sender=world.boss, settle=False)
    request = _note("The release is out; nothing for you to do.", token=_token(world.id, "t-boss"),
                    note_id="n-1")
    status, body = request.answer
    assert status == 200 and body["created"] and body["notified"] and body["reason"] == "agent-note"
    assert body["trace_id"] == "t-boss"
    row = next(row for row in world.log() if row["id"] == body["message_id"])
    assert (row["role"], row["kind"], row["origin"], row["audience"], row["trace_id"]) == (
        "assistant", "note", "note", "user", "t-boss")
    assert not row["sender_agent_id"] and not row["reply_to_agent_id"] and not row["automated"]
    types = [event["type"] for event in request.ctx.stream.events]
    assert types == ["transcript-updated", "user-notification"]
    assert pushes and pushes[0]["push"] and pushes[0]["preview"].startswith("The release is out")
    # A retry is the same note, not a second one, and not a second notification.
    again = _note("The release is out; nothing for you to do.", token=_token(world.id, "t-boss"), note_id="n-1")
    assert again.answer[1]["created"] is False and again.answer[1]["notified"] is False
    assert sum(1 for row in world.log() if row["kind"] == "note") == 1
    # The note stays through every import, and the turn's summary counts it.
    world.now[0] += 10_000
    turn_lifecycle.transition(world.id, turn_lifecycle.TurnEvent.PROCESS_EXITED_OK, {"trace_id": "t-boss"})
    world.imported()
    world.imported()
    rows = [row for row in world.log() if row["trace_id"] == "t-boss"]
    assert sum(1 for row in rows if row["kind"] == "note") == 1
    assert [row["turn"].get("note_count") for row in rows if row.get("turn")] == [1]


def test_a_turns_own_notification_never_takes_a_note_as_its_reply(world, pushes):
    world.turn("t-user", "Hello there, please list the files", "", settle=False)
    _note("By the way: the disk is nearly full.", token=_token(world.id, "t-user"))
    candidates = user_notifications._assistant_candidates(world.id, "native", 0, BASE * 2,
                                                          trace_id="t-user", identified=True)
    assert all(row["kind"] != "note" for row in candidates)


def test_a_note_is_refused_outside_the_agents_own_running_turn(world, pushes):
    world.turn("t-boss", "Report please", "Done.", origin="agent", sender=world.boss, settle=False)
    other = _note("Hello", token=_token(world.peer, "t-other"))
    assert other.answer[0] == 403 and "clarp-admin tell-user --from peer" in other.answer[1]["error"]
    stale = _note("Hello", token=_token(world.id, "t-old"))
    assert stale.answer[0] == 409 and "clarp-admin tell-user" in stale.answer[1]["error"]
    running = _token(world.id, "t-boss")
    assert _note("Hello from the turn", token=running).answer[1]["trace_id"] == "t-boss"
    turn_lifecycle.close_turns_for_trace(world.id, "t-boss")
    idle = _note("Hello", token=running)
    assert idle.answer[0] == 409 and "works only inside a running turn of worker" in idle.answer[1]["error"]
    empty = _note("   ")
    assert empty.answer[0] == 400 and "a note needs words: clarp-admin tell-user" in empty.answer[1]["error"]
    assert _note("x" * (agent_notes.MAX_TEXT + 1)).answer[0] == 413
    assert _note("Hello", session="nobody").answer[0] == 404


def test_a_muted_agents_note_badges_without_a_push_and_a_quiet_agents_stays_quiet(world, pushes):
    world.turn("t-user", "Hello", "Hi.", settle=False)
    agents.update_agent(world.id, muted=True)
    muted = _note("Muted, but here.", token=_token(world.id, "t-user")).answer[1]
    assert muted["notified"] and muted["reason"] == "agent-note-muted"
    assert pushes[-1]["push"] is False and pushes[-1]["badge"] is True
    quiet = notification_policy.note_decision(notification_policy.AgentCapabilities(present=True, quiet=True))
    assert isinstance(quiet, notification_policy.Suppress) and quiet.reason == "janitor-maintenance"


def test_a_notes_row_is_exactly_what_every_app_draws_as_an_ordinary_reply(world, pushes):
    """iOS, Android, desktop and the web app draw this row as the agent's reply: role assistant,
    kind and origin 'note' (a word none of them treats specially), no sender fields, not automated,
    no automation category, words in it. Pinned field by field, stored and on /log."""
    from lib import origins
    world.turn("t-boss", "Report please", "Done.", origin="agent", sender=world.boss, settle=False)
    body = _note("  The release is out.  ", token=_token(world.id, "t-boss"), note_id="n-exact").answer[1]
    stored = dict(db.conn().execute(
        "SELECT role, kind, origin, sender_agent_id, source_file, seq < 0 AS negative, trace_id, tool_name,"
        " tools_json, display_cells_json, text FROM messages WHERE message_id=?", (body["message_id"],)).fetchone())
    assert stored == {"role": "assistant", "kind": "note", "origin": "note", "sender_agent_id": None,
                      "source_file": "note:n-exact", "negative": 1, "trace_id": "t-boss", "tool_name": None,
                      "tools_json": "[]", "display_cells_json": "[]", "text": "The release is out."}
    row = next(row for row in world.log() if row["id"] == message_store.note_message_id(world.id, "n-exact"))
    assert {key: row[key] for key in (
        "role", "kind", "origin", "text", "tool_name", "tools", "display_cells", "trace_id",
        "sender_agent_id", "sender_name", "sender_session", "reply_to_agent_id", "reply_to_name",
        "reply_to_session", "audience", "automated", "automation_kind", "phase")} == {
        "role": "assistant", "kind": "note", "origin": "note", "text": "The release is out.",
        "tool_name": None, "tools": [], "display_cells": [], "trace_id": "t-boss",
        "sender_agent_id": "", "sender_name": "", "sender_session": "", "reply_to_agent_id": "",
        "reply_to_name": "", "reply_to_session": "", "audience": "user", "automated": False,
        "automation_kind": "", "phase": None}
    assert "note" in origins.CHAT_ACTIVITY_ORIGINS


def test_a_note_after_its_turn_completed_normally_is_refused(tmp_path, pushes):
    """Driven through the real dispatcher: the turn ends by the dispatcher's own normal
    completion (on_result), not by closing its row by hand. A settled turn keeps ended_at
    empty; it is not running, and no newer turn started, so a note is refused, with or
    without the settled turn's own token."""
    from lib.turn_dispatch import DispatchCommand
    from tests.unit.test_turn_dispatch import _make_service
    service, backends, agent_id = _make_service(tmp_path)
    boss = agents.create_agent(persona="Boss", session="boss", voice_id="v", cwd=str(tmp_path))
    service.submit(DispatchCommand(text="Report please", requested_session="mike", forced_session="mike",
                                   trace_id="t-done", client_msg_id="c-done", synthesize_audio=False,
                                   origin="agent", sender_agent_id=boss))
    token = _token(agent_id, "t-done")
    assert _note("While it runs.", session="mike", token=token).answer[0] == 200
    _, call = backends.spawned[0]
    call["on_result"]({"duration_ms": 50})
    row = db.conn().execute("SELECT ended_at, settled_at FROM turns WHERE trace_id='t-done'").fetchone()
    assert row["settled_at"] is not None          # what the dispatcher's completion leaves
    request = _note("After it ended.", session="mike", token=token)
    assert request.answer[0] == 409, request.answer
    assert "works only inside a running turn of mike" in request.answer[1]["error"]


# ---- the user's word, and who said it (the /send boundary) -----------------------------------

def _boundary(world, *, origin, to="worker", caller="", sender="", peer=False):
    from lib.send_request import SendRequest
    payload = {"text": "hello", "session": to, "origin": origin, "client_msg_id": f"c-{origin}-{caller}"}
    if sender:
        payload["sender"] = sender
    req = SendRequest.from_payload(payload, default_session="worker", trace_id_factory=lambda: "t",
                                   authenticated=True, caller_agent_id=caller, peer=peer)
    mark = coordination.is_coordination(agent_id=agents.get_by_session(to)["agent_id"],
                                        origin=req.origin, sender_agent_id=req.sender_agent_id)
    return req, mark


@pytest.mark.parametrize("claimed", ["user", "oracle"])
def test_a_send_from_an_agents_own_turn_is_that_agents_whatever_origin_it_claims(world, claimed):
    req, mark = _boundary(world, origin=claimed, caller=world.boss)
    assert (req.origin, req.sender_agent_id, mark) == ("agent", world.boss, True)
    # Kept out of the user's own prompt history too.
    assert req.admit().cooperative_principal != "user"


def test_the_users_word_is_never_coordination_whatever_sender_it_names(world):
    req, mark = _boundary(world, origin="user", sender="boss")     # a claim, no turn token
    assert (req.origin, req.sender_agent_id, mark) == ("user", world.boss, False)
    for origin in ("user", "oracle"):
        assert not coordination.is_coordination(agent_id=world.id, origin=origin, sender_agent_id=world.boss)


def test_an_agent_prompting_itself_from_its_own_turn_is_not_coordination(world):
    req, mark = _boundary(world, origin="automation", caller=world.id)
    assert (req.origin, req.sender_agent_id, mark) == ("automation", "", False)


def test_a_settled_turns_token_and_an_archived_agents_are_still_that_agent(world):
    """The principal does not depend on its turn still running."""
    token = _token(world.boss, "t-gone")
    turn_lifecycle.open_turn(agent_id=world.boss, source="pwa", trace_id="t-gone")
    turn_lifecycle.close_turns_for_trace(world.boss, "t-gone")
    from lib import turn_identity
    caller = turn_identity.caller_agent_id({provider_jobs.TURN_ENV: token})
    assert caller == world.boss
    assert _boundary(world, origin="user", caller=caller)[1] is True
    agents.set_archived(world.peer, True)
    req, mark = _boundary(world, origin="user", caller=world.peer)
    assert (req.origin, req.sender_agent_id, mark) == ("agent", world.peer, True)


def test_a_deleted_agents_token_names_no_sender_but_is_never_the_users_word(world):
    gone = agents.create_agent(persona="Gone", session="gone", voice_id="v", cwd="/tmp")
    with db.conn() as con:
        con.execute("UPDATE agents SET deleted_at=1 WHERE agent_id=?", (gone,))
    db.close_local()
    req, mark = _boundary(world, origin="user", caller=gone)
    assert (req.origin, req.sender_agent_id, mark) == ("agent", "", True)


def test_a_peer_hosts_claim_of_the_users_word_is_an_agents_and_a_devices_is_untouched(world):
    req, mark = _boundary(world, origin="user", peer=True)
    assert (req.origin, mark) == ("agent", True)
    req, mark = _boundary(world, origin="user")                     # a device, or the Host's own token
    assert (req.origin, mark) == ("user", False)


def _dispatched(tmp_path, *, retry_scheduler=None):
    from tests.unit.test_turn_dispatch import _make_service
    service, backends, agent_id = _make_service(tmp_path, retry_scheduler=retry_scheduler or (lambda d, fn: None))
    return service, backends, agent_id


def _start(service, trace):
    from lib.turn_dispatch import DispatchCommand
    service.submit(DispatchCommand(text="Go", requested_session="mike", forced_session="mike", trace_id=trace,
                                   client_msg_id="c-" + trace, synthesize_audio=False, origin="user"))


def test_a_note_with_an_earlier_turns_token_while_a_newer_turn_runs_is_refused(tmp_path, pushes):
    service, backends, agent_id = _dispatched(tmp_path)
    _start(service, "t-1")
    old = _token(agent_id, "t-1")
    backends.spawned[-1][1]["on_result"]({"duration_ms": 5})
    _start(service, "t-2")
    refused = _note("From the old turn.", session="mike", token=old)
    assert refused.answer[0] == 409
    taken = _note("From the running turn.", session="mike", token=_token(agent_id, "t-2"))
    assert taken.answer[0] == 200 and taken.answer[1]["trace_id"] == "t-2"


def test_a_note_from_a_failed_turn_is_refused(tmp_path, pushes):
    service, backends, agent_id = _dispatched(tmp_path)
    _start(service, "t-fail")
    token = _token(agent_id, "t-fail")
    backends.spawned[-1][1]["on_error"]("something nobody recognises went wrong")   # not retried: failed
    assert db.conn().execute("SELECT outcome FROM turns WHERE trace_id='t-fail'").fetchone()[0] == "failed"
    assert _note("Too late.", session="mike", token=token).answer[0] == 409


def test_a_note_from_a_stopped_turn_is_refused(tmp_path, pushes):
    """The steps the Host's /stop records (its handler needs an HTTP request): the dispatch
    state cleared, the backend interrupted, STOP_REQUESTED for the stopped turn."""
    from lib import turn_dispatch as td
    service, backends, agent_id = _dispatched(tmp_path)
    _start(service, "t-stop")
    token = _token(agent_id, "t-stop")
    td.clear_for_agent(agent_id, preserve_queue=True, pause_queue=True, user_stop=True)
    turn_lifecycle.try_transition(agent_id, turn_lifecycle.TurnEvent.STOP_REQUESTED,
                                  {"source": "user_stop", "message": "Turn stopped", "trace_id": "t-stop"})
    assert db.conn().execute("SELECT settled_at FROM turns WHERE trace_id='t-stop'").fetchone()[0] is not None
    assert _note("Stopped.", session="mike", token=token).answer[0] == 409


def test_a_note_from_a_turn_parked_for_a_retry_is_its_turns(tmp_path, pushes):
    """A turn waiting to be delivered again (a dropped connection) has neither ended nor
    settled: it is still the agent's turn, and a note is a row of it.

    The rule is turn_identity.running_trace's (PR 470), ruled on purpose: a turn parked for a
    retry after a dropped connection is running (a note in that gap is that turn's), a turn
    parked for its account is not (the next test)."""
    parked = []
    service, backends, agent_id = _dispatched(tmp_path, retry_scheduler=lambda d, fn: parked.append(fn))
    _start(service, "t-park")
    token = _token(agent_id, "t-park")
    backends.spawned[-1][1]["on_error"]("connection reset by peer")
    assert parked
    taken = _note("Still on it.", session="mike", token=token)
    assert taken.answer[0] == 200 and taken.answer[1]["trace_id"] == "t-park"


# ---- F1: no caller attaches a message to another message's trace -----------------------------

def _agent_send(trace, *, sender, client="c-boss", origin="agent", to="mike"):
    from lib.send_request import SendRequest, SendRequestError
    req = SendRequest.from_payload(
        {"text": "Report", "session": to, "origin": origin, "sender": sender, "trace_id": trace,
         "client_msg_id": client},
        default_session=to, trace_id_factory=lambda: "minted", authenticated=True)
    try:
        req.require_new_trace()
    except SendRequestError as refused:
        return refused.status, str(refused)
    return 200, ""


def _audiences(agent_id, trace):
    rows = agents.list_messages(agent_id=agent_id, backend_session_id=agents.live_backend_session(agent_id),
                                limit=500)
    return sorted({row["audience"] for row in rows if row.get("trace_id") == trace})


def test_an_agent_cannot_attach_its_message_to_a_users_turn(tmp_path, pushes):
    """The reviewer's probe: a user's turn through the real dispatcher, then an agent's send
    naming that turn's trace. Refused at /send while the turn runs and after it ended."""
    service, backends, agent_id = _dispatched(tmp_path)
    boss = agents.create_agent(persona="Boss", session="boss", voice_id="v", cwd=str(tmp_path))
    _start(service, "t-user")
    status, words = _agent_send("t-user", sender=boss)
    assert status == 409 and "trace_id already belongs to another message" in words
    backends.spawned[-1][1]["on_result"]({"duration_ms": 5})
    assert _agent_send("t-user", sender=boss)[0] == 409
    assert _audiences(agent_id, "t-user") == ["user"]


def test_a_queued_users_trace_and_the_oracles_are_refused_and_a_retry_is_not(tmp_path, pushes):
    from lib.turn_dispatch import DispatchCommand
    service, backends, agent_id = _dispatched(tmp_path)
    boss = agents.create_agent(persona="Boss", session="boss", voice_id="v", cwd=str(tmp_path))
    _start(service, "t-run")
    service.submit(DispatchCommand(text="Next", requested_session="mike", forced_session="mike", trace_id="t-queued",
                                   client_msg_id="c-queued", synthesize_audio=False, origin="user",
                                   queue_if_busy=True))
    assert _agent_send("t-queued", sender=boss)[0] == 409
    service.submit(DispatchCommand(text="Said aloud", requested_session="mike", forced_session="mike",
                                   trace_id="t-oracle", client_msg_id="c-oracle", synthesize_audio=False,
                                   origin="oracle", queue_if_busy=True))
    assert _agent_send("t-oracle", sender=boss)[0] == 409
    # The agent's own message, sent again with its own client id, sender and origin: a retry.
    service.submit(DispatchCommand(text="Report", requested_session="mike", forced_session="mike",
                                   trace_id="t-boss", client_msg_id="c-boss", synthesize_audio=False,
                                   origin="agent", sender_agent_id=boss, queue_if_busy=True,
                                   prompt_admission=__import__("lib.prompt_admissions", fromlist=["x"]).create(
                                       authenticated_at_admission=True, origin="agent", sender_agent_id=boss,
                                       channel="chat", observed_at=1, client_admission_id="c-boss",
                                       trace_id="t-boss", original_text="Report")))
    assert _agent_send("t-boss", sender=boss) == (200, "")
    assert _agent_send("t-boss", sender=boss, client="c-other")[0] == 409
    assert _agent_send("never-seen", sender=boss) == (200, "")


def test_the_mark_is_never_written_or_read_for_a_trace_that_holds_the_users_word(tmp_path, pushes):
    """Past /send (an internal path, a row the import wrote): the mark itself refuses."""
    from lib.turn_dispatch import DispatchCommand
    service, backends, agent_id = _dispatched(tmp_path)
    boss = agents.create_agent(persona="Boss", session="boss", voice_id="v", cwd=str(tmp_path))
    _start(service, "t-user")
    backends.spawned[-1][1]["on_result"]({"duration_ms": 5})
    service.submit(DispatchCommand(text="Mine now", requested_session="mike", forced_session="mike",
                                   trace_id="t-user", client_msg_id="c-sneak", synthesize_audio=False,
                                   origin="agent", sender_agent_id=boss))
    assert db.conn().execute("SELECT 1 FROM messages WHERE message_id='u-c-sneak'").fetchone() is not None
    assert coordination.audience_of(agent_id, "t-user") == "user"
    assert _audiences(agent_id, "t-user") == ["user"]
    backends.spawned[-1][1]["on_result"]({"duration_ms": 5})
    # The other order: marked first, then the user's word arrives in it.
    service.submit(DispatchCommand(text="Report", requested_session="mike", forced_session="mike",
                                   trace_id="t-boss", client_msg_id="c-boss2", synthesize_audio=False,
                                   origin="agent", sender_agent_id=boss))
    assert coordination.audience_of(agent_id, "t-boss") == "agents"
    agents.record_user_message(agent_id=agent_id, backend_session_id=agents.live_backend_session(agent_id),
                               client_msg_id="c-late-user", text="Me too", trace_id="t-boss", origin="user")
    assert coordination.audience_of(agent_id, "t-boss") == "user"
    assert _audiences(agent_id, "t-boss") == ["user"]
    # And the read guard alone: a mark forced past every writer still reads as the user's.
    with db.conn() as con:
        con.execute("UPDATE coordination_traces SET audience='agents' WHERE trace_id='t-boss'")
    db.close_local()
    assert _audiences(agent_id, "t-boss") == ["user"]



# ---- F5: a note's id is its agent's; F6: only the agent's own turn speaks for it -------------

def test_another_agents_note_id_is_a_new_note_and_a_repeat_is_the_same_one(world, pushes):
    world.turn("t-boss", "Report please", "Done.", origin="agent", sender=world.boss, settle=False)
    mine = _note("The worker's words.", token=_token(world.id, "t-boss"), note_id="n-shared").answer[1]
    agents.bind_backend_session(world.peer, "peer-native")
    agents.open_turn(agent_id=world.peer, source="pwa", trace_id="t-peer")
    theirs = _note("The peer's words.", session="peer", token=_token(world.peer, "t-peer"),
                   note_id="n-shared").answer[1]
    assert theirs["created"] and theirs["notified"] and theirs["trace_id"] == "t-peer"
    assert theirs["message_id"] != mine["message_id"]
    assert pushes[-1]["preview"] == "The peer's words." and pushes[-1]["session"] == "peer"
    before = len(pushes)
    again = _note("The worker's words.", token=_token(world.id, "t-boss"), note_id="n-shared").answer
    assert again[0] == 200 and again[1]["created"] is False and again[1]["notified"] is False
    assert len(pushes) == before
    other = _note("Other words.", token=_token(world.id, "t-boss"), note_id="n-shared")
    assert other.answer[0] == 409 and "already a note of worker with other words" in other.answer[1]["error"]


@pytest.mark.parametrize("headers", [{}, {"X-Clarp-Peer": "work"}, {"Authorization": "Bearer device-token"}],
                         ids=["host-token", "peer-host", "device"])
def test_a_note_without_the_agents_own_turn_token_is_refused_for_every_principal(world, pushes, headers):
    world.turn("t-boss", "Report please", "Done.", origin="agent", sender=world.boss, settle=False)
    request = _Request({"session": "worker", "text": "As if from the worker."})
    request.headers = dict(headers)
    request._handle_agent_note()
    assert request.answer[0] == 403 and "clarp-admin tell-user works only from a turn of worker" in \
        request.answer[1]["error"]
    assert not any(row["kind"] == "note" for row in world.log())
    assert pushes == []


def test_a_turn_token_in_the_query_string_or_of_a_deleted_agent_is_no_token(world, pushes):
    world.turn("t-boss", "Report please", "Done.", origin="agent", sender=world.boss, settle=False)
    request = _Request({"session": "worker", "text": "Hello", "X-Clarp-Turn": _token(world.id, "t-boss")})
    request._handle_agent_note()                                 # only the header counts
    assert request.answer[0] == 403
    gone = agents.create_agent(persona="Gone", session="gone", voice_id="v", cwd="/tmp")
    token = _token(gone, "t-gone")
    with db.conn() as con:
        con.execute("UPDATE agents SET deleted_at=1 WHERE agent_id=?", (gone,))
    db.close_local()
    refused = _note("Hello", token=token)
    assert refused.answer[0] == 403 and "no longer exists" in refused.answer[1]["error"]


def test_a_note_and_its_turns_done_in_the_same_millisecond_both_notify(world, pushes):
    """F9: the note took the millisecond first; the turn's reply was then dropped as a
    duplicate of it. The note moves to the next free millisecond and keeps its id."""
    world.turn("t-user", "Hello there, please list the files", "Here are the files you asked for.",
               settle=False)
    note = _note("A side note.", token=_token(world.id, "t-user")).answer[1]
    stored = db.conn().execute("SELECT notification_id, done_ts FROM user_notifications"
                               " WHERE source_message_id=?", (note["message_id"],)).fetchone()
    turn = user_notifications.classify_completed_turn(
        agent_id=world.id, session="worker", persona="Worker", done_ts=int(stored["done_ts"]),
        backend_session_id="native", trace_id="t-user", settle_timeout_s=0)
    assert turn["notify"] and turn["reason"] == "text-reply" and turn["source_message_id"] != note["message_id"]
    moved = db.conn().execute("SELECT notification_id, done_ts, reason FROM user_notifications"
                              " WHERE source_message_id=?", (note["message_id"],)).fetchone()
    assert moved["notification_id"] == stored["notification_id"] and moved["reason"] == "agent-note"
    assert moved["done_ts"] == stored["done_ts"] + 1


def test_a_notes_push_on_android_is_an_ordinary_notification_message():
    """The FCM data map as really built (fcm.data_payload over apns.turn_done_payload): the kind
    every reply has, the note's words as the body, and no reason (the apps take none)."""
    from lib import apns, fcm
    data = fcm.data_payload(apns.turn_done_payload("Worker", "worker", "The release is out.",
                                                   notification_id="pn-1"),
                            {"agent_id": "a", "badge": True, "unread": True})
    assert data["kind"] == "user-notification" and data["body"] == "The release is out."
    assert data["title"] == "Worker" and data["notification_id"] == "pn-1" and "reason" not in data



def test_a_note_from_a_turn_parked_waiting_for_its_account_is_refused(tmp_path, pushes):
    """The rule is turn_identity.running_trace's (PR 470), ruled on purpose: a turn parked for
    its account keeps its row open and unsettled, but makes no progress and its process may
    still hold the token: not running. A turn parked for a retry is (the test above)."""
    service, backends, agent_id = _dispatched(tmp_path)
    _start(service, "t-acct")
    token = _token(agent_id, "t-acct")
    turn_lifecycle.try_transition(agent_id, turn_lifecycle.TurnEvent.ACCOUNT_RECOVERY_WAIT, {
        "dispatch": "claude", "trace_id": "t-acct", "account_recovery": "waiting",
        "message": "waiting for an account"})
    assert _note("Waiting.", session="mike", token=token).answer[0] == 409
