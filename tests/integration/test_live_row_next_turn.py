"""A finished reply stays visible when the next turn starts streaming before
its transcript import lands (docs/live-items.md §2), read through the real
authenticated GET /log.

The next turn's first live row used to delete every live row of another
trace, so a completed, finalized reply vanished from /log until the late
import wrote it back under a new id and at its old position. Only a row a
turn left unfinished (interrupted, failed) is cleared that way."""
import importlib.util
import json
from pathlib import Path
import threading
from types import SimpleNamespace
import urllib.request

import pytest

from lib import agents as agents_db
from lib import message_store, turn_lifecycle
from lib.turn_lifecycle import TurnEvent

_spec = importlib.util.spec_from_file_location(
    "live_row_next_turn_server", Path(__file__).resolve().parents[2] / "server/server.py")
server = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(server)
TOKEN = "isolated-live-row-test"
SESSION = "koda"
BACKEND_SESSION = "b-1"
REPLY_A = "Here is the overview. 1. Slides 2. Script 3. Rehearsal"
REPLY_B = "Report received; nothing to do."


@pytest.fixture
def host(tmp_path):
    agent_id = agents_db.create_agent(persona="Koda", voice_id="v", cwd=str(tmp_path),
                                      session=SESSION, backend="claude")
    agents_db.start_runtime(agent_id, SESSION)
    agents_db.bind_backend_session(agent_id, BACKEND_SESSION)
    agents_db.set_focus(agent_id)
    ctx = SimpleNamespace(auth_token=TOKEN, default_session=SESSION,
                          agents_path=tmp_path / "unused.json",
                          stream=SimpleNamespace(broadcast=lambda *_: None))
    srv = server.ContextHTTPServer(("127.0.0.1", 0), server.Handler, ctx)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield SimpleNamespace(base=f"http://127.0.0.1:{srv.server_port}", agent_id=agent_id)
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=2)


def _log(host):
    req = urllib.request.Request(f"{host.base}/log?session={SESSION}&limit=100",
                                 headers={"Authorization": "Bearer " + TOKEN})
    with urllib.request.urlopen(req, timeout=5) as response:
        body = json.load(response)
    return [(t.get("id"), t.get("text")) for t in body.get("turns", [])
            if t.get("role") == "assistant"]


def _start(agent_id, trace):
    agents_db.open_turn(agent_id=agent_id, source="pwa", trace_id=trace)
    turn_lifecycle.try_transition(agent_id, TurnEvent.SPAWN_STARTED,
                                  {"source": "pwa", "dispatch": "claude", "trace_id": trace})


def _stream(agent_id, trace, text, key):
    return message_store.upsert_live_assistant_message(
        agent_id=agent_id, backend_session_id=BACKEND_SESSION, trace_id=trace,
        text=text, item_key=key)


def _finish(agent_id, trace, event=TurnEvent.PROCESS_EXITED_OK):
    turn_lifecycle.try_transition(agent_id, event, {"trace_id": trace})
    agents_db.conn().execute(
        "UPDATE turns SET ended_at = 1 WHERE agent_id = ? AND trace_id = ?", (agent_id, trace))


def test_a_finalized_reply_survives_the_next_turn_streaming_before_its_import(host):
    agent_id = host.agent_id
    _start(agent_id, "trace-a")
    a = _stream(agent_id, "trace-a", REPLY_A, "msg_a")
    assert message_store.mark_live_message_final(
        agent_id=agent_id, backend_session_id=BACKEND_SESSION, trace_id="trace-a",
        item_key="msg_a")
    _finish(agent_id, "trace-a")
    # The next queued turn streams before trace-a's transcript import lands.
    _start(agent_id, "trace-b")
    b = _stream(agent_id, "trace-b", REPLY_B, "msg_b")
    assert b is not None
    assert _log(host) == [(a["id"], REPLY_A), (b["id"], REPLY_B)]


def test_the_late_import_adopts_the_kept_reply_once_under_its_id(host):
    agent_id = host.agent_id
    _start(agent_id, "trace-a")
    a = _stream(agent_id, "trace-a", REPLY_A, "msg_a")
    message_store.mark_live_message_final(
        agent_id=agent_id, backend_session_id=BACKEND_SESSION, trace_id="trace-a",
        item_key="msg_a")
    _finish(agent_id, "trace-a")
    _start(agent_id, "trace-b")
    _stream(agent_id, "trace-b", REPLY_B, "msg_b")
    message_store.store_transcript_turns(
        agent_id=agent_id, backend_session_id=BACKEND_SESSION, source_file="/x/t.jsonl",
        turns=[{"role": "user", "text": "work on the slides", "timestamp": "2026-10-18T09:33:53Z"},
               {"role": "assistant", "text": REPLY_A, "timestamp": "2026-10-18T09:35:10Z"}])
    rows = _log(host)
    assert [row for row in rows if row[1] == REPLY_A] == [(a["id"], REPLY_A)]
    assert [text for _id, text in rows] == [REPLY_A, REPLY_B]


def test_a_completed_turns_last_row_survives_even_if_never_marked_final(host):
    """Runners that settle a message only when the next one starts (grok,
    opencode) leave the turn's last row live; the turn itself completed."""
    agent_id = host.agent_id
    _start(agent_id, "trace-a")
    a = _stream(agent_id, "trace-a", REPLY_A, "")
    _finish(agent_id, "trace-a")
    _start(agent_id, "trace-b")
    b = _stream(agent_id, "trace-b", REPLY_B, "msg_b")
    assert _log(host) == [(a["id"], REPLY_A), (b["id"], REPLY_B)]


@pytest.mark.parametrize("event", [TurnEvent.STOP_REQUESTED, TurnEvent.PROCESS_EXITED_FAILED])
def test_an_unfinished_row_of_an_interrupted_or_failed_turn_is_still_cleared(host, event):
    agent_id = host.agent_id
    _start(agent_id, "trace-a")
    _stream(agent_id, "trace-a", "Half a sent", "msg_a")
    _finish(agent_id, "trace-a", event)
    _start(agent_id, "trace-b")
    b = _stream(agent_id, "trace-b", REPLY_B, "msg_b")
    assert _log(host) == [(b["id"], REPLY_B)]


def test_a_stale_trace_writes_nothing_and_clears_nothing(host):
    agent_id = host.agent_id
    _start(agent_id, "trace-a")
    a = _stream(agent_id, "trace-a", "Still streaming", "msg_a")
    # Not the agent's open turn: the ownership fence refuses the write.
    assert _stream(agent_id, "trace-old", "late delta", "msg_x") is None
    assert _log(host) == [(a["id"], "Still streaming")]


def test_a_chain_of_turns_keeps_every_finished_reply_until_one_late_batch_import(host):
    """Several queued turns each finish before a single late import (as when
    the import lags under load): every reply stays, each under its own id,
    including a turn's earlier finished message, and the batch import adopts
    them all in place."""
    agent_id = host.agent_id
    replies = {}
    for n, trace in enumerate(["trace-1", "trace-2", "trace-3"]):
        _start(agent_id, trace)
        if n == 0:
            note = _stream(agent_id, trace, "Checking the outline first.", "msg_1a")
            message_store.mark_live_message_final(
                agent_id=agent_id, backend_session_id=BACKEND_SESSION, trace_id=trace,
                item_key="msg_1a")
            replies["Checking the outline first."] = note["id"]
        text = f"Reply {n + 1} is done."
        row = _stream(agent_id, trace, text, f"msg_{n + 1}")
        message_store.mark_live_message_final(
            agent_id=agent_id, backend_session_id=BACKEND_SESSION, trace_id=trace,
            item_key=f"msg_{n + 1}")
        _finish(agent_id, trace)
        replies[text] = row["id"]
    expected = list(replies.items())
    assert [(text, id_) for id_, text in _log(host)] == expected
    turns = []
    for n in range(3):
        turns.append({"role": "user", "text": f"prompt {n + 1}",
                      "timestamp": f"2026-10-18T09:3{n}:00Z"})
        if n == 0:
            turns.append({"role": "assistant", "text": "Checking the outline first.",
                          "timestamp": "2026-10-18T09:30:05Z"})
        turns.append({"role": "assistant", "text": f"Reply {n + 1} is done.",
                      "timestamp": f"2026-10-18T09:3{n}:10Z"})
    message_store.store_transcript_turns(
        agent_id=agent_id, backend_session_id=BACKEND_SESSION, source_file="/x/t.jsonl",
        turns=turns)
    assert [(text, id_) for id_, text in _log(host)] == expected


# ---- identity: a kept reply is adopted only by its own turn's transcript reply ----

def _prompt(agent_id, trace, text):
    """What /send records: the turn and the durable client row naming its trace."""
    _start(agent_id, trace)
    message_store.record_user_message(
        agent_id=agent_id, backend_session_id=BACKEND_SESSION,
        client_msg_id="c-" + trace, text=text, trace_id=trace)


def _reply(agent_id, trace, text, end=TurnEvent.PROCESS_EXITED_OK):
    row = _stream(agent_id, trace, text, "msg_" + trace)
    message_store.mark_live_message_final(
        agent_id=agent_id, backend_session_id=BACKEND_SESSION, trace_id=trace,
        item_key="msg_" + trace)
    _finish(agent_id, trace, end)
    return row["id"]


def _import(agent_id, turns):
    message_store.store_transcript_turns(
        agent_id=agent_id, backend_session_id=BACKEND_SESSION, source_file="/x/t.jsonl",
        turns=turns)


def _owners(agent_id):
    return {r["message_id"]: (r["text"], r["trace_id"]) for r in agents_db.conn().execute(
        "SELECT message_id, text, trace_id FROM messages WHERE agent_id = ? AND role = 'assistant'",
        (agent_id,)).fetchall()}


def _two_kept_turns(agent_id, text_a, text_b):
    _prompt(agent_id, "trace-a", "first prompt")
    a = _reply(agent_id, "trace-a", text_a, TurnEvent.STOP_REQUESTED)
    _prompt(agent_id, "trace-b", "second prompt")
    b = _reply(agent_id, "trace-b", text_b)
    return a, b


_ONLY_B = [{"role": "user", "text": "first prompt", "timestamp": "2026-10-18T09:30:00Z"},
           {"role": "user", "text": "second prompt", "timestamp": "2026-10-18T09:31:00Z"}]


def test_a_final_marked_reply_of_an_interrupted_turn_is_kept_when_the_next_turn_streams(host):
    """The user already saw that completed provider message; it stays."""
    agent_id = host.agent_id
    a, b = _two_kept_turns(agent_id, "Stopped here.", "Next answer.")
    assert _log(host) == [(a, "Stopped here."), (b, "Next answer.")]


@pytest.mark.parametrize("text_a, text_b", [
    ("Done.", "Done. The tests pass."),      # the later reply extends the earlier one
    ("Done. The tests pass.", "Done."),      # the earlier reply extends the later one
    ("Done.", "Done."),                      # exact duplicates
])
def test_an_import_without_the_earlier_reply_never_moves_identity_across_turns(host, text_a, text_b):
    agent_id = host.agent_id
    a, b = _two_kept_turns(agent_id, text_a, text_b)
    batch = _ONLY_B + [{"role": "assistant", "text": text_b, "timestamp": "2026-10-18T09:31:10Z"}]
    for _ in range(2):                       # a repeated import changes nothing
        _import(agent_id, batch)
        owners = _owners(agent_id)
        assert owners[a] == (text_a, "trace-a")
        assert owners[b] == (text_b, "trace-b")
        assert len(owners) == 2
        # Membership, not order: the adopted row takes the transcript's
        # timestamp (ordering by timestamp is a separate follow-up).
        assert sorted(_log(host)) == sorted([(a, text_a), (b, text_b)])


def test_a_late_earlier_reply_appended_after_the_later_one_loses_nothing(host):
    """Appended after B's prompt, a late copy of A's reply reads as B's turn.
    Its identity is ambiguous, so neither kept row is taken or deleted."""
    agent_id = host.agent_id
    a, b = _two_kept_turns(agent_id, "Done.", "Done. The tests pass.")
    batch = _ONLY_B + [{"role": "assistant", "text": "Done. The tests pass.",
                        "timestamp": "2026-10-18T09:31:10Z"}]
    _import(agent_id, batch)
    _import(agent_id, batch + [{"role": "assistant", "text": "Done.",
                                "timestamp": "2026-10-18T09:31:20Z"}])
    owners = _owners(agent_id)
    assert owners[a] == ("Done.", "trace-a")
    assert owners[b] == ("Done. The tests pass.", "trace-b")


def test_prefix_sharing_replies_imported_complete_and_in_order_keep_their_own_ids(host):
    agent_id = host.agent_id
    _prompt(agent_id, "trace-a", "first prompt")
    a = _reply(agent_id, "trace-a", "Done.")
    _prompt(agent_id, "trace-b", "second prompt")
    b = _reply(agent_id, "trace-b", "Done. The tests pass.")
    _import(agent_id, [
        {"role": "user", "text": "first prompt", "timestamp": "2026-10-18T09:30:00Z"},
        {"role": "assistant", "text": "Done.", "timestamp": "2026-10-18T09:30:10Z"},
        {"role": "user", "text": "second prompt", "timestamp": "2026-10-18T09:31:00Z"},
        {"role": "assistant", "text": "Done. The tests pass.", "timestamp": "2026-10-18T09:31:10Z"}])
    owners = _owners(agent_id)
    assert owners == {a: ("Done.", "trace-a"), b: ("Done. The tests pass.", "trace-b")}


def test_a_reply_whose_turn_is_unknown_takes_and_clears_no_traced_row(host):
    """No client row names the reply's turn and its time lies outside every
    turn window: its identity is unavailable. Kept rows of other turns are
    neither adopted nor deleted, whatever the text match; the reply gets a
    durable row of its own."""
    agent_id = host.agent_id
    _prompt(agent_id, "trace-a", "first prompt")
    a = _reply(agent_id, "trace-a", "Done.", TurnEvent.STOP_REQUESTED)
    _prompt(agent_id, "trace-b", "second prompt")
    b = _reply(agent_id, "trace-b", "Done. The tests pass.")
    batch = [{"role": "user", "text": "an unrelated prompt", "timestamp": "2020-01-01T00:00:00Z"},
             {"role": "assistant", "text": "Done. The tests pass.", "timestamp": "2020-01-01T00:00:10Z"}]
    for _ in range(2):
        _import(agent_id, batch)
        owners = _owners(agent_id)
        assert owners[a] == ("Done.", "trace-a")
        assert owners[b] == ("Done. The tests pass.", "trace-b")
