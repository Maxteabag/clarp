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
