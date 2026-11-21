"""The import takes the stream's id (R108).

A keyed assistant line (it carries its provider message's id, and the import knows its turn) is
stored under an identity id: block k of message M in turn T. Block 0 is the id the stream writes
M's live row under. Whichever comes first, the import and the stream name one row:
- the stream first (order A): the import adopts the live row in place, by its id;
- the import first (order B): the stream's later writes for M are refused, as they are once a
  keyed message has settled.
So order B ends as order A does: one row, no double, no reload. The ids are the same on every
import of the same transcript, wherever the line stands."""
from __future__ import annotations

import json
import threading

import pytest

from lib import backends, clock, db, message_live, message_store
from lib.message_import_common import _identity_id, _message_id
from tests.unit.test_live_import_identity import BASE, _import, _opened_case

S = "native-session"
FIRST = "First block of the message."
SECOND = "Second block, from the search."


def _line(path, offset, *, message_id="msg_A", text=None, kind="text"):
    """One Claude transcript line: Claude Code writes each content block of a message as a line of
    its own, all with the message's id."""
    if kind == "text":
        content = [{"type": "text", "text": text}]
    elif kind == "thinking":
        content = [{"type": "thinking", "thinking": "Synthetic thought.", "signature": "synthetic"}]
    elif kind == "server_tool_use":
        content = [{"type": "server_tool_use", "id": f"srvtoolu_{offset}", "name": "web_search",
                    "input": {"query": "synthetic"}}]
    else:
        content = [{"type": "web_search_tool_result", "tool_use_id": f"srvtoolu_{offset - 1}",
                    "content": []}]
    record = {"type": "assistant", "uuid": f"assistant-{offset}",
              "timestamp": clock.iso_from_ms(BASE + offset),
              "message": {"id": message_id, "role": "assistant", "model": "synthetic-fixture",
                          "content": content}}
    with path.open("a") as stream:
        stream.write(json.dumps(record) + "\n")


def _rows(aid):
    return [(r["id"], r["text"], r.get("kind") or "")
            for r in message_store.list_messages(agent_id=aid, backend_session_id=S)
            if r["role"] == "assistant"]


def _replace(aid):
    row = db.conn().execute("SELECT replace_revision FROM conversation_heads WHERE agent_id=? AND "
                            "backend_session_id=?", (aid, S)).fetchone()
    return row[0] if row else 0


def _stream(aid, text, key="msg_A"):
    return message_store.upsert_live_assistant_message(
        agent_id=aid, backend_session_id=S, trace_id="turn-1", item_key=key, text=text)


def _id(aid, block, key="msg_A", trace="turn-1"):
    return _identity_id(aid, S, trace, key, block)


def _block_two(path):
    _line(path, 400, kind="server_tool_use")
    _line(path, 401, kind="result")
    _line(path, 402, text=SECOND)


def _second_case(tmp_path, monkeypatch, now):
    """A second agent in the same store, for order B beside order A."""
    from lib import agents, turn_lifecycle
    from tests.unit.test_live_import_identity import _record
    tmp_path.mkdir(parents=True, exist_ok=True)
    aid = agents.create_agent(persona="Synthetic Two", session="r108-b", voice_id="v",
                              cwd=str(tmp_path), backend="claude")
    agents.bind_backend_session(aid, S)
    agents.open_turn(agent_id=aid, source="pwa", trace_id="turn-1")
    turn_lifecycle.transition(aid, turn_lifecycle.TurnEvent.PROMPT_ADMITTED, {"trace_id": "turn-1", "origin": "user"})
    message_store.record_user_message(agent_id=aid, backend_session_id=S, client_msg_id="r108-b-client",
                                      text="Synthetic question", trace_id="turn-1")
    path = tmp_path / "native.jsonl"
    _record(path, "user", "Synthetic question", 100)
    return aid, path, now


def _walk(tmp_path, monkeypatch, order, case=None):
    """The reviewer's walk, orders A and B, to one common tail; the state after each step of the
    tail: the assistant rows, and whether the step raised replace_revision."""
    aid, path, now = case or _opened_case(tmp_path, monkeypatch, "defaults")
    now[0] = BASE + 300
    if order == "A":
        assert _stream(aid, FIRST) is not None
        assert _stream(aid, FIRST + "\n\nSecond") is not None
        _line(path, 200, text=FIRST)
        _import(aid, path)
    else:
        _line(path, 200, text=FIRST)
        _import(aid, path)
        assert _stream(aid, FIRST + "\n\nSecond") is None             # refused: the message has settled
    states = [(_rows(aid), False)]
    tail = [
        lambda: _stream(aid, FIRST + "\n\nSecond block"),
        lambda: _import(aid, path),
        lambda: _stream(aid, FIRST + "\n\nSecond block, from"),
        lambda: (_block_two(path), _import(aid, path)),
        lambda: _stream(aid, FIRST + "\n\n" + SECOND),
        lambda: message_store.mark_live_message_final(
            agent_id=aid, backend_session_id=S, trace_id="turn-1", item_key="msg_A"),
        lambda: _import(aid, path),
    ]
    for step in tail:
        held = _replace(aid)
        step()
        states.append((_rows(aid), _replace(aid) > held))
    return aid, states


# The state after each step of the common tail, rows named by block: one row per block, no reload.
_FIRST_ONLY = [("id_0", FIRST, "")]
_BOTH = _FIRST_ONLY + [("id_1", SECOND, "")]
WALK = [(_FIRST_ONLY, False)] * 4 + [(_BOTH, False)] * 4


@pytest.mark.parametrize("order", ["A", "B"])
def test_order_b_reads_like_order_a_step_for_step(tmp_path, monkeypatch, order):
    """Both orders of the reviewer's walk give the same states, step for step, from the first
    import of block 1 on: B reads like A."""
    aid, walk = _walk(tmp_path, monkeypatch, order)
    names = {_id(aid, 0): "id_0", _id(aid, 1): "id_1"}
    assert [([(names.get(i, i), text, kind) for i, text, kind in rows], raised) for rows, raised in walk] == WALK


def test_the_stream_keys_its_live_row_by_block_zeros_id(tmp_path, monkeypatch):
    """The live channel's item names the live row by the stream's key (live_claude's row_id): in
    both orders that is the row block 1 is stored under."""
    aid, path, now = _opened_case(tmp_path, monkeypatch, "defaults")
    assert message_live._live_message_id(aid, S, "turn-1", "msg_A") == _id(aid, 0)


def test_a_full_reimport_and_an_import_from_scratch_give_the_same_ids(tmp_path, monkeypatch):
    aid, path, now = _opened_case(tmp_path, monkeypatch, "defaults")
    now[0] = BASE + 300
    _line(path, 200, text=FIRST)
    _block_two(path)
    _import(aid, path)
    ids = [i for i, _t, _k in _rows(aid)]
    assert ids == [_id(aid, 0), _id(aid, 1)]
    message_store.store_transcript_turns(agent_id=aid, backend_session_id=S, source_file=str(path),
                                         turns=backends.parse_turns("claude", path))
    assert [i for i, _t, _k in _rows(aid)] == ids                      # the same store, again
    db.conn().execute("DELETE FROM messages WHERE agent_id=?", (aid,))  # the rows gone
    db.conn().execute("DELETE FROM conversation_heads WHERE agent_id=?", (aid,))
    message_store.store_transcript_turns(agent_id=aid, backend_session_id=S, source_file=str(path),
                                         turns=backends.parse_turns("claude", path))
    assert [i for i, _t, _k in _rows(aid)] == ids                      # from scratch: the rule alone


def test_a_block_imported_in_a_later_tail_takes_its_block_index(tmp_path, monkeypatch):
    aid, path, now = _opened_case(tmp_path, monkeypatch, "defaults")
    now[0] = BASE + 300
    _line(path, 200, text=FIRST)
    _import(aid, path)
    _block_two(path)
    _import(aid, path)
    assert [i for i, _t, _k in _rows(aid)] == [_id(aid, 0), _id(aid, 1)]


def _seqs(aid):
    return {row["message_id"]: row["seq"] for row in db.conn().execute(
        "SELECT message_id, seq FROM messages WHERE agent_id=? AND role='assistant'", (aid,))}


def test_a_shift_moves_a_keyed_row_but_never_changes_its_id(tmp_path, monkeypatch):
    """R128's first step: an earlier record appears, the keyed lines move down one; their ids stay."""
    aid, path, now = _opened_case(tmp_path, monkeypatch, "defaults")
    now[0] = BASE + 300
    _line(path, 200, text=FIRST)
    _block_two(path)
    turns = backends.parse_turns("claude", path)
    message_store.store_transcript_turns(agent_id=aid, backend_session_id=S, source_file=str(path), turns=turns)
    before = _seqs(aid)
    shifted = turns[:1] + [{"role": "assistant", "text": "An earlier remark.",
                            "timestamp": clock.iso_from_ms(BASE + 150)}] + turns[1:]
    message_store.store_transcript_turns(agent_id=aid, backend_session_id=S, source_file=str(path), turns=shifted)
    after = _seqs(aid)
    for keyed in (_id(aid, 0), _id(aid, 1)):
        assert after[keyed] == before[keyed] + 1


def test_without_a_known_trace_a_line_keeps_its_positional_id_and_keeps_it_after(tmp_path, monkeypatch):
    """No turn the line can be placed in (the turn rows gone, a transcript older than its turns,
    an import before the turn row is written): its positional id. A later import that knows the
    trace finds the slot held by that row and keeps it, so a client holding it never sees it
    change within this store."""
    aid, path, now = _opened_case(tmp_path, monkeypatch, "defaults")
    now[0] = BASE + 300
    _line(path, 200, text=FIRST)
    turns = backends.parse_turns("claude", path)
    db.conn().execute("DELETE FROM messages WHERE agent_id=?", (aid,))       # its user row too
    saved = db.conn().execute("SELECT * FROM turns WHERE agent_id=?", (aid,)).fetchall()
    db.conn().execute("DELETE FROM turns WHERE agent_id=?", (aid,))          # no turn to place it in
    message_store.store_transcript_turns(agent_id=aid, backend_session_id=S, source_file=str(path), turns=turns)
    positional = _message_id(aid, S, str(path), 1)
    assert [i for i, _t, _k in _rows(aid)] == [positional]
    for row in saved:                                                       # the trace known again
        columns = row.keys()
        db.conn().execute(f"INSERT INTO turns ({','.join(columns)}) VALUES ({','.join('?' * len(columns))})",
                          tuple(row))
    message_store.store_transcript_turns(agent_id=aid, backend_session_id=S, source_file=str(path), turns=turns)
    assert [i for i, _t, _k in _rows(aid)] == [positional]


def test_a_row_stored_before_the_rule_keeps_its_id_and_r107_retracts_its_stream(tmp_path, monkeypatch):
    """The fallback: a slot already held by its positional row (stored before this release) keeps
    it; a live row of the same message is then retracted as R107 does, and counted."""
    from lib import message_writes
    aid, path, now = _opened_case(tmp_path, monkeypatch, "defaults")
    now[0] = BASE + 300
    _line(path, 200, text=FIRST)
    with monkeypatch.context() as before_the_rule:
        before_the_rule.setattr(message_writes, "_keyed_line_id", lambda *a, **k: "")
        _import(aid, path)
    positional = [i for i, _t, _k in _rows(aid)]
    assert positional and positional[0].startswith("msg-") and positional[0] != _id(aid, 0)
    lines = []
    monkeypatch.setattr("lib.log.log", lambda event, detail="", *a, **k: lines.append((event, detail)))
    assert _stream(aid, FIRST) is not None                                  # nothing holds id_0
    held = _replace(aid)
    message_store.store_transcript_turns(agent_id=aid, backend_session_id=S, source_file=str(path),
                                         turns=backends.parse_turns("claude", path))
    assert [i for i, _t, _k in _rows(aid)] == positional and _replace(aid) > held
    assert ("importLiveOwnStreamRetracted", "backend=claude count=1") in lines


@pytest.mark.parametrize("first", ["the import", "the live write"])
def test_the_live_insert_and_the_import_on_two_connections_in_both_orders(tmp_path, monkeypatch, first):
    """The live writer looks for its row, then inserts it in its own transaction. The import may
    store block 1 under that id in between: the insert re-checks inside its transaction and is
    refused, never an error. The other way round the import adopts the live row by its id."""
    aid, path, now = _opened_case(tmp_path, monkeypatch, "defaults")
    now[0] = BASE + 300
    _line(path, 200, text=FIRST)
    live_id = _id(aid, 0)
    result = {}

    def live_insert():                                      # another thread: another connection
        try:
            result["insert"] = message_live._insert_live_message_atomic(
                database=db.conn(), msg_id=live_id, agent_id=aid, backend_session_id=S,
                trace_id="turn-1", timestamp=clock.iso_from_ms(BASE + 300), timestamp_ms=BASE + 300,
                text=FIRST + " and more", origin="user", sender_agent_id="")
        except Exception as exc:  # noqa: BLE001 - the test reports it
            result["error"] = exc

    def on_other_connection():
        worker = threading.Thread(target=live_insert)
        worker.start()
        worker.join(10)

    if first == "the import":
        _import(aid, path)
        on_other_connection()
        assert "error" not in result and result["insert"][0] is False
    else:
        on_other_connection()
        assert "error" not in result and result["insert"][0] is True
        _import(aid, path)
    assert _rows(aid) == [(live_id, FIRST, "")]


def test_a_codex_message_imported_before_its_stream_takes_the_streams_id(tmp_path, monkeypatch):
    """Codex: a modern rollout names each completed message by the item id its stream is keyed by;
    the import stores it under the live row's id, and the stream's write is refused."""
    from lib.codex_transcript import parse_turns
    aid, path, now = _opened_case(tmp_path, monkeypatch, "defaults")
    rollout = tmp_path / "rollout.jsonl"
    stamp = clock.iso_from_ms(BASE + 200)
    records = [
        {"timestamp": clock.iso_from_ms(BASE + 100), "type": "response_item",
         "payload": {"type": "message", "role": "user",
                     "content": [{"type": "input_text", "text": "Synthetic question"}]}},
        {"timestamp": stamp, "type": "event_msg", "payload": {
            "type": "item_completed", "turn_id": "turn-0", "item": {
                "type": "AgentMessage", "id": "item_0", "content": [{"type": "Text", "text": FIRST}]}}},
    ]
    rollout.write_text("".join(json.dumps(r) + "\n" for r in records))
    turns = [t for t in parse_turns(rollout)]
    assert any(t.get("provider_id") == "item_0" for t in turns)
    now[0] = BASE + 300
    message_store.store_transcript_turns(agent_id=aid, backend_session_id=S, source_file=str(rollout), turns=turns)
    assert _stream(aid, FIRST, key="item_0") is None
    assert [i for i, _t, _k in _rows(aid)] == [_id(aid, 0, key="item_0")]


def _transcript(path):
    """A user line, message A as two text blocks with a server tool between, then message B."""
    _line(path, 200, text=FIRST)
    _block_two(path)
    _line(path, 500, message_id="msg_B", text="Another message.")


@pytest.mark.parametrize("cut", range(1, 7))
def test_ids_do_not_depend_on_where_the_import_was_cut(tmp_path, monkeypatch, cut):
    """(1) One import or two, cut at every line boundary of the transcript, inside message A's
    blocks too: the same ids. k does not depend on how the import was cut."""
    aid, path, now = _opened_case(tmp_path, monkeypatch, "defaults")
    now[0] = BASE + 600
    whole = tmp_path / "whole.jsonl"
    whole.write_text(path.read_text())
    _transcript(whole)
    lines = whole.read_text().splitlines(keepends=True)
    assert len(lines) == 6
    path.write_text("".join(lines[:cut]))
    _import(aid, path)
    with path.open("a") as stream:
        stream.write("".join(lines[cut:]))
    _import(aid, path)
    assert [i for i, _t, _k in _rows(aid)] == [_id(aid, 0), _id(aid, 1), _id(aid, 0, key="msg_B")]


def test_a_block_written_twice_is_two_rows_as_on_main(tmp_path, monkeypatch):
    """(2) The same block written twice (a retried line) is two lines of the message: blocks 0 and
    1, two rows, as main shows two. The next block of the message is block 2."""
    aid, path, now = _opened_case(tmp_path, monkeypatch, "defaults")
    now[0] = BASE + 600
    _line(path, 200, text=FIRST)
    _line(path, 201, text=FIRST)
    _line(path, 402, text=SECOND)
    _import(aid, path)
    assert _rows(aid) == [(_id(aid, 0), FIRST, ""), (_id(aid, 1), FIRST, ""), (_id(aid, 2), SECOND, "")]


def test_a_block_inserted_before_another_after_the_fact_moves_the_words_under_the_ids(tmp_path, monkeypatch):
    """(2) A rewrite that puts a block before another of the same message: k counts lines, so the
    ids keep their places and the words move under them (block 0 now holds the inserted words), as
    positional ids do today within the message; the message's last block is a new row."""
    aid, path, now = _opened_case(tmp_path, monkeypatch, "defaults")
    now[0] = BASE + 600
    _line(path, 200, text=FIRST)
    _line(path, 402, text=SECOND)
    turns = backends.parse_turns("claude", path)
    message_store.store_transcript_turns(agent_id=aid, backend_session_id=S, source_file=str(path), turns=turns)
    inserted = turns[:1] + [{**turns[1], "text": "An inserted block."}] + turns[1:]
    message_store.store_transcript_turns(agent_id=aid, backend_session_id=S, source_file=str(path), turns=inserted)
    assert _rows(aid) == [(_id(aid, 0), "An inserted block.", ""), (_id(aid, 1), FIRST, ""),
                          (_id(aid, 2), SECOND, "")]


def test_an_id_stays_when_a_later_import_reads_the_line_in_another_turn(tmp_path, monkeypatch):
    """(3) R91 places a reply in the latest turn begun before it. A turn learnt later that began
    before the reply moves the line's trace; the row it already has keeps its id."""
    from lib import agents
    aid, path, now = _opened_case(tmp_path, monkeypatch, "defaults")
    _line(path, 200, text=FIRST)
    db.conn().execute("DELETE FROM messages WHERE agent_id=? AND role='user'", (aid,))  # no linked request
    turns = [t for t in backends.parse_turns("claude", path) if t["role"] == "assistant"]
    now[0] = BASE + 600
    message_store.store_transcript_turns(agent_id=aid, backend_session_id=S, source_file=str(path), turns=turns)
    [(first_id, _t, _k)] = _rows(aid)
    assert first_id == _id(aid, 0)
    db.conn().execute("UPDATE turns SET ended_at=? WHERE agent_id=?", (BASE + 140, aid))
    now[0] = BASE + 150
    agents.open_turn(agent_id=aid, source="pwa", trace_id="turn-2")       # begun before the reply
    now[0] = BASE + 700
    for _ in range(2):
        message_store.store_transcript_turns(agent_id=aid, backend_session_id=S, source_file=str(path), turns=turns)
        row = db.conn().execute("SELECT message_id, trace_id FROM messages WHERE agent_id=? AND role='assistant'",
                                (aid,)).fetchall()
        assert [r["message_id"] for r in row] == [first_id]
    assert row[0]["trace_id"] == "turn-2"                                 # the trace did move


def _many(path, count):
    for n in range(count):
        record = {"type": "user", "uuid": f"user-{n}", "timestamp": clock.iso_from_ms(BASE + 1000 + 10 * n),
                  "message": {"role": "user", "content": f"Question {n}"}}
        with path.open("a") as stream:
            stream.write(json.dumps(record) + "\n")
        _line(path, 1005 + 10 * n, message_id=f"msg_{n}", text=f"Answer {n}.")


@pytest.mark.parametrize("shift", ["up by one record", "down by one pair"])
def test_a_shift_of_fifty_replies_keeps_every_keyed_id(tmp_path, monkeypatch, shift):
    """(7) R128's cases: every keyed row keeps its id and changes its seq only (and so takes a new
    revision, R119); no keyed id is replaced."""
    aid, path, now = _opened_case(tmp_path, monkeypatch, "defaults")
    path.write_text("")
    _many(path, 50)
    now[0] = BASE + 5000
    turns = backends.parse_turns("claude", path)
    message_store.store_transcript_turns(agent_id=aid, backend_session_id=S, source_file=str(path), turns=turns)
    def keyed():                                    # one block per message: block 0, the stream's id
        return {r["message_id"]: (r["seq"], r["revision"]) for r in db.conn().execute(
            "SELECT message_id, seq, revision FROM messages WHERE agent_id=? AND role='assistant' "
            "AND message_id LIKE 'live-%'", (aid,))}
    before = keyed()
    assert len(before) == 50
    if shift == "up by one record":
        moved, delta = turns[:0] + [{"role": "assistant", "text": "An earlier remark.",
                                     "timestamp": clock.iso_from_ms(BASE + 900)}] + turns, 1
    else:
        moved, delta = turns[2:], -2
    message_store.store_transcript_turns(agent_id=aid, backend_session_id=S, source_file=str(path), turns=moved)
    after = keyed()
    kept = [i for i in before if i in after]
    assert len(kept) == (50 if delta == 1 else 49)                         # no keyed id replaced
    assert all(after[i][0] == before[i][0] + delta for i in kept)
    assert all(after[i][1] > before[i][1] for i in kept)                   # each moved row: a new revision


# --- the live channel: the item of block k names the row of block k (R143's addition) ----------

def _drain(items, blocks, message_id="msg_A"):
    """Stream one Claude message through the live items, block by block, as stream-json gives it:
    ("text", words), ("server_tool_use",), ("result",), ("tool_use", name)."""
    def event(inner):
        items.on_event({"type": "stream_event", "event": inner})
    event({"type": "message_start", "message": {"id": message_id}})
    for index, block in enumerate(blocks):
        if block[0] == "text":
            event({"type": "content_block_start", "index": index, "content_block": {"type": "text", "text": ""}})
            event({"type": "content_block_delta", "index": index,
                   "delta": {"type": "text_delta", "text": block[1]}})
        elif block[0] == "thinking":
            event({"type": "content_block_start", "index": index,
                   "content_block": {"type": "thinking", "thinking": ""}})
            event({"type": "content_block_delta", "index": index,
                   "delta": {"type": "thinking_delta", "thinking": "Synthetic thought."}})
        elif block[0] == "server_tool_use":
            event({"type": "content_block_start", "index": index, "content_block": {
                "type": "server_tool_use", "id": f"srvtoolu_{index}", "name": "web_search", "input": {}}})
        elif block[0] == "tool_use":
            event({"type": "content_block_start", "index": index, "content_block": {
                "type": "tool_use", "id": f"toolu_{index}", "name": block[1], "input": {}}})
        else:
            event({"type": "content_block_start", "index": index, "content_block": {
                "type": "web_search_tool_result", "tool_use_id": f"srvtoolu_{index - 1}", "content": []}})
        event({"type": "content_block_stop", "index": index})
    event({"type": "message_stop"})


def _item_rows(aid, blocks, tmp_path):
    """The row_id each message item of the stream carries, by item id, with the hub the Host
    runs and the live items the Claude backend makes for a turn."""
    from lib import live_hub
    from lib.live_claude import ClaudeLiveItems
    from lib.live_hub import LiveHub
    from lib.backend import claude
    hub = LiveHub(sink=lambda event: None)
    live_hub.install(hub)
    try:
        items = claude._live_items(aid, lambda: S, "turn-1")
        assert isinstance(items, ClaudeLiveItems)
        _drain(items, blocks)
        snapshot = hub.snapshot(agent_id=aid)
    finally:
        live_hub.install(None)
    return {item["id"]: item.get("row_id") for item in snapshot["items"] if item.get("kind") == "message"}


@pytest.mark.parametrize("order", ["A", "B"])
def test_each_message_item_names_its_own_blocks_row_in_both_orders(tmp_path, monkeypatch, order):
    """The recording's message: text, a server tool, its result, text. Item :0 names block 0's row
    and item :3 block 1's (k counts the blocks the transcript keeps as lines: a server tool and its
    result are none), never the same id; and those are the ids the import stores the two blocks
    under, in order A (stream first) and order B (import first) alike."""
    blocks = [("text", FIRST), ("server_tool_use",), ("result",), ("text", SECOND)]
    aid, path, now = _opened_case(tmp_path, monkeypatch, "defaults")
    now[0] = BASE + 300
    if order == "B":
        _line(path, 200, text=FIRST)
        _import(aid, path)
    rows = _item_rows(aid, blocks, tmp_path)
    assert rows == {"cl:msg_A:0": _id(aid, 0), "cl:msg_A:3": _id(aid, 1)}
    if order == "A":
        _line(path, 200, text=FIRST)
    _block_two(path)
    _import(aid, path)
    assert [i for i, _t, _k in _rows(aid)] == [rows["cl:msg_A:0"], rows["cl:msg_A:3"]]


def test_a_client_tool_between_two_texts_is_a_line_of_its_own(tmp_path, monkeypatch):
    """A client tool use is a line of the message in the transcript (its tool row), so the text
    after it is block 2: the item says so, and so does the import."""
    aid, path, now = _opened_case(tmp_path, monkeypatch, "defaults")
    now[0] = BASE + 300
    rows = _item_rows(aid, [("text", FIRST), ("tool_use", "Read"), ("text", SECOND)], tmp_path)
    assert rows == {"cl:msg_A:0": _id(aid, 0), "cl:msg_A:2": _id(aid, 2)}
    _line(path, 200, text=FIRST)
    record = {"type": "assistant", "uuid": "assistant-300", "timestamp": clock.iso_from_ms(BASE + 300),
              "message": {"id": "msg_A", "role": "assistant", "model": "synthetic-fixture", "content": [
                  {"type": "tool_use", "id": "toolu_1", "name": "Read", "input": {"file_path": "/x"}}]}}
    with path.open("a") as stream:
        stream.write(json.dumps(record) + "\n")
    _line(path, 402, text=SECOND)
    _import(aid, path)
    ids = [r["message_id"] for r in db.conn().execute(
        "SELECT message_id FROM messages WHERE agent_id=? AND role='assistant' AND seq >= 0 ORDER BY seq", (aid,))]
    assert ids == [_id(aid, 0), _id(aid, 1), _id(aid, 2)]


def test_a_thinking_block_is_no_line_on_the_live_channel_or_in_the_transcript(tmp_path, monkeypatch):
    """Thinking, then text, then text: the thinking block is no line (one block count, shared by
    the parser, the live items and the live rows), so the first text is block 0 everywhere."""
    from lib.message_text import claude_block_is_line
    assert not claude_block_is_line({"type": "thinking", "thinking": "x"})
    assert not claude_block_is_line({"type": "server_tool_use", "id": "s", "name": "web_search"})
    assert not claude_block_is_line({"type": "text", "text": "  "})
    assert claude_block_is_line({"type": "tool_use", "id": "t", "name": "Read"})
    aid, path, now = _opened_case(tmp_path, monkeypatch, "defaults")
    now[0] = BASE + 300
    rows = _item_rows(aid, [("thinking",), ("text", FIRST), ("text", SECOND)], tmp_path)
    assert rows == {"cl:msg_A:1": _id(aid, 0), "cl:msg_A:2": _id(aid, 1)}
    _line(path, 199, kind="thinking")
    _line(path, 200, text=FIRST)
    _line(path, 402, text=SECOND)
    _import(aid, path)
    assert _rows(aid) == [(_id(aid, 0), FIRST, ""), (_id(aid, 1), SECOND, "")]


def test_later_blocks_have_a_prefix_no_other_id_has():
    """Block 0 is the keyed live id (adoption changes no id); every later block is 'blk-', which no
    positional ('msg-'), live ('live-') or client ('u-') id has."""
    assert _identity_id("a", S, "t", "m", 0).startswith("live-")
    assert all(_identity_id("a", S, "t", "m", k).startswith("blk-") for k in (1, 2, 7))
    assert _message_id("a", S, "f", 3).startswith("msg-")


# --- shift (1): up by one record, the positional occupant parked, never deleted ----------------

from lib.message_import_common import _PARKED_POSITIONAL_SEQ_BASE, _PARKED_SEQ_BASE  # noqa: E402


def _user(path, offset, text):
    record = {"type": "user", "uuid": f"user-{offset}", "timestamp": clock.iso_from_ms(BASE + offset),
              "message": {"role": "user", "content": text}}
    with path.open("a") as stream:
        stream.write(json.dumps(record) + "\n")


def _pairs(path, count, start=1000):
    """``count`` user lines the client never recorded (positional rows), each answered by a keyed
    message of one block."""
    for n in range(count):
        _user(path, start + 10 * n, f"Question {n}")
        _line(path, start + 10 * n + 5, message_id=f"msg_{n}", text=f"Answer {n}.")


def _all(aid):
    return {r["message_id"]: (r["seq"], r["role"], r["text"]) for r in db.conn().execute(
        "SELECT message_id, seq, role, text FROM messages WHERE agent_id=? AND backend_session_id=? AND seq >= 0",
        (aid, S))}


def _store(aid, path, turns, **kwargs):
    return message_store.store_transcript_turns(agent_id=aid, backend_session_id=S, source_file=str(path),
                                                turns=turns, **kwargs)


def _shift_case(tmp_path, monkeypatch, count=3):
    aid, path, now = _opened_case(tmp_path, monkeypatch, "defaults")
    path.write_text("")
    _pairs(path, count)
    now[0] = BASE + 5000
    turns = backends.parse_turns("claude", path)
    _store(aid, path, turns)
    return aid, path, turns


EARLIER = {"role": "assistant", "text": "An earlier remark.", "timestamp": clock.iso_from_ms(BASE + 900)}


def test_up_by_one_record_in_a_full_read_parks_the_positional_row_and_writes_it_again(tmp_path, monkeypatch):
    """A keyed row moves into the slot of the next user line's positional row: that row is parked,
    its line writes it at its new seq in the same pass, and nothing is left in the parked band:
    every question once, the same words, each keyed id unchanged, replace_revision moved."""
    aid, path, turns = _shift_case(tmp_path, monkeypatch)
    before = _all(aid)
    held = _replace(aid)
    _store(aid, path, [EARLIER] + turns)
    after = _all(aid)
    questions = sorted(text for _seq, role, text in after.values() if role == "user")
    assert questions == ["Question 0", "Question 1", "Question 2"]
    for n in range(3):
        keyed = _id(aid, 0, key=f"msg_{n}")
        assert after[keyed][0] == before[keyed][0] + 1 and after[keyed][2] == f"Answer {n}."
    assert not db.conn().execute("SELECT 1 FROM messages WHERE agent_id=? AND seq >= ?",
                                 (aid, _PARKED_SEQ_BASE)).fetchone()
    assert _replace(aid) > held
    # The occupant's id is positional: it changed with its seq (R128's remainder).
    question_1 = [i for i, (_s, role, text) in after.items() if text == "Question 1"]
    assert question_1 == [_message_id(aid, S, str(path), before[_id(aid, 0, key="msg_0")][0] + 2)]
    again = _replace(aid)
    _store(aid, path, [EARLIER] + turns)
    assert _all(aid) == after and _replace(aid) == again                # stable: nothing moves twice


def test_the_parked_copy_goes_in_the_batch_that_writes_its_line_again(tmp_path, monkeypatch):
    """Commit after every line: a parked question is in every commit (once, or twice from the
    commit that writes its line again until its copy goes, in that same commit here). Question 0
    is not parked: the remark's positional row takes its slot's id over, as every positional shift
    does today, so it is missing between two commits (R128's remainder, unchanged)."""
    from lib import message_import_writer
    aid, path, turns = _shift_case(tmp_path, monkeypatch)
    seen = []
    real_commit = message_import_writer._ImportWriter.commit

    def commit(self):
        real_commit(self)
        rows = db.conn().execute("SELECT seq, text FROM messages WHERE agent_id=? AND role='user' AND seq >= 0",
                                 (aid,)).fetchall()
        seen.append(sorted(r["text"] for r in rows))
    monkeypatch.setattr(message_import_writer._ImportWriter, "due", lambda self: True)
    monkeypatch.setattr(message_import_writer._ImportWriter, "commit", commit)
    _store(aid, path, [EARLIER] + turns)
    assert seen and all({"Question 1", "Question 2"} <= set(texts) for texts in seen)
    assert all(texts.count("Question 1") == 1 and texts.count("Question 2") == 1 for texts in seen)
    assert seen[-1].count("Question 1") == 1 and seen[-1].count("Question 2") == 1


def test_the_same_collision_in_a_tail_deletes_nothing_and_gives_up_to_a_full_read(tmp_path, monkeypatch):
    """A pass that does not hold every line (a tail from a checkpoint) meets the same collision:
    it changes nothing and raises, so the import falls back to a full read."""
    from lib import message_writes
    aid, path, now = _opened_case(tmp_path, monkeypatch, "defaults")
    path.write_text("")
    _pairs(path, 3)
    now[0] = BASE + 5000
    turns = backends.parse_turns("claude", path)
    carry: dict = {}
    _store(aid, path, turns, capture_at=2, captured=carry)
    before = _all(aid)
    shifted = turns[:3] + [{**EARLIER, "timestamp": clock.iso_from_ms(BASE + 1012)}] + turns[3:]
    with pytest.raises(message_writes._TailInvalidated) as refused:
        message_writes.store_transcript_tail(agent_id=aid, backend_session_id=S, source_file=str(path),
                                             turns=shifted[2:], start_seq=2, carry=carry)
    assert refused.value.what == "identity_shift_occupant"
    assert _all(aid) == before
    assert not db.conn().execute("SELECT 1 FROM messages WHERE agent_id=? AND seq >= ?",
                                 (aid, _PARKED_SEQ_BASE)).fetchone()


def test_a_parked_positional_copy_left_by_a_pass_that_never_ended_stops_every_tail(tmp_path, monkeypatch):
    """A crash between batches leaves a parked positional copy. A tail must not run the stale
    cleanup over it (its line may stand before the tail's boundary and never be written again):
    it refuses; the full read that follows writes every line and removes the copy."""
    from lib import message_writes
    aid, path, now = _opened_case(tmp_path, monkeypatch, "defaults")
    path.write_text("")
    _pairs(path, 3)
    now[0] = BASE + 5000
    turns = backends.parse_turns("claude", path)
    carry: dict = {}
    _store(aid, path, turns, capture_at=4, captured=carry)
    expected = _all(aid)
    question = _message_id(aid, S, str(path), 2)
    db.conn().execute("UPDATE messages SET seq=? WHERE message_id=?", (_PARKED_POSITIONAL_SEQ_BASE + 2, question))
    with pytest.raises(message_writes._TailInvalidated) as refused:
        message_writes.store_transcript_tail(agent_id=aid, backend_session_id=S, source_file=str(path),
                                             turns=turns[4:], start_seq=4, carry=carry)
    assert refused.value.what == "parked_positional_rows"
    _store(aid, path, turns)
    assert _all(aid) == expected


def test_the_tail_probe_is_one_range_seek_on_the_unique_index(tmp_path, monkeypatch):
    """Every tail runs the probe: a range on (agent, conversation, seq), LIMIT 1."""
    aid, _path, _now = _opened_case(tmp_path, monkeypatch, "defaults")
    plan = [r[-1] for r in db.conn().execute(
        "EXPLAIN QUERY PLAN SELECT 1 FROM messages WHERE agent_id = ? AND backend_session_id = ? AND seq >= ? "
        "LIMIT 1", (aid, S, _PARKED_POSITIONAL_SEQ_BASE))]
    assert len(plan) == 1 and "USING COVERING INDEX" in plan[0] and "seq>?" in plan[0].replace(" ", "")


# --- the parked band's readers never see the positional band -----------------------------------

def _plant(aid, path, *, seq, message_id, text, trace="turn-1", role="assistant", stamp=BASE + 2000):
    db.conn().execute(
        """INSERT INTO messages (message_id, agent_id, backend_session_id, source_file, seq, role, timestamp,
               text, kind, tool_name, tools_json, display_cells_json, updated_at, revision, origin,
               sender_agent_id, trace_id, phase)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, '[]', '[]', ?, 1, 'user', '', ?, NULL)""",
        (message_id, aid, S, str(path), seq, role, clock.iso_from_ms(stamp), text, stamp, trace))


def test_a_parked_positional_copy_is_never_restored_as_a_final_row(tmp_path, monkeypatch):
    """RM-N1c's restore reads only the adopted band: a positional copy with a trace goes with the
    stale cleanup, never becomes final:<trace>."""
    aid, path, turns = _shift_case(tmp_path, monkeypatch, count=1)
    copy = _message_id(aid, S, str(path), 7)
    _plant(aid, path, seq=_PARKED_POSITIONAL_SEQ_BASE + 7, message_id=copy, text="A parked copy.")
    _store(aid, path, turns)
    assert db.conn().execute("SELECT 1 FROM messages WHERE message_id=?", (copy,)).fetchone() is None


def test_a_parked_positional_copy_never_lends_its_id_to_an_unlinked_reply(tmp_path, monkeypatch):
    """An unlinked reply at an empty slot takes a parked reply's id by its words (RM-N1b); never a
    parked positional copy's."""
    aid, path, now = _opened_case(tmp_path, monkeypatch, "defaults")
    path.write_text("")
    _user(path, 1000, "Question 0")
    record = {"type": "assistant", "uuid": "assistant-unkeyed", "timestamp": clock.iso_from_ms(BASE + 1005),
              "message": {"role": "assistant", "model": "synthetic-fixture",
                          "content": [{"type": "text", "text": "Same words."}]}}
    with path.open("a") as stream:
        stream.write(json.dumps(record) + "\n")
    copy = _message_id(aid, S, str(path), 9)
    _plant(aid, path, seq=_PARKED_POSITIONAL_SEQ_BASE + 9, message_id=copy, text="Same words.", trace="")
    now[0] = BASE + 5000
    _store(aid, path, backends.parse_turns("claude", path))
    reply = [i for i, (_s, role, text) in _all(aid).items() if text == "Same words."]
    assert reply == [_message_id(aid, S, str(path), 1)]
    assert db.conn().execute("SELECT 1 FROM messages WHERE message_id=?", (copy,)).fetchone() is None


def test_a_parked_positional_copy_is_never_a_shifted_twin(tmp_path, monkeypatch):
    """RM-N1 moves an adopted reply of the same request found at a later slot; a positional copy in
    its own band looks adopted (its id is not its seq's) and must not be taken."""
    aid, path, now = _opened_case(tmp_path, monkeypatch, "defaults")
    record = {"type": "assistant", "uuid": "assistant-unkeyed", "timestamp": clock.iso_from_ms(BASE + 200),
              "message": {"role": "assistant", "model": "synthetic-fixture",
                          "content": [{"type": "text", "text": "Same words."}]}}
    with path.open("a") as stream:
        stream.write(json.dumps(record) + "\n")
    copy = _message_id(aid, S, str(path), 9)
    _plant(aid, path, seq=_PARKED_POSITIONAL_SEQ_BASE + 9, message_id=copy, text="Same words.")
    now[0] = BASE + 300
    _store(aid, path, backends.parse_turns("claude", path))
    reply = [i for i, (_s, role, text) in _all(aid).items() if text == "Same words."]
    assert reply == [_message_id(aid, S, str(path), 1)]


# --- shift (2): down by one pair, an identity row whose line left is deleted -------------------

def test_down_by_one_pair_deletes_the_identity_row_and_its_return_brings_the_same_id(tmp_path, monkeypatch):
    """Remove and return: a pair leaves in one read and comes back in the next. Its keyed row is
    deleted (not restored as final:<trace>) with replace_revision, and returns under the same id
    with the same words: two replace_revision moves, no final row left behind."""
    aid, path, turns = _shift_case(tmp_path, monkeypatch)
    answer = _id(aid, 0, key="msg_0")
    before = _all(aid)
    held = _replace(aid)
    _store(aid, path, turns[2:])
    assert answer not in _all(aid) and _replace(aid) > held
    assert db.conn().execute("SELECT 1 FROM messages WHERE message_id=?", (answer,)).fetchone() is None
    assert not db.conn().execute("SELECT 1 FROM messages WHERE agent_id=? AND source_file LIKE 'final:%'",
                                 (aid,)).fetchone()
    held = _replace(aid)
    _store(aid, path, turns)
    after = _all(aid)
    assert after[answer] == before[answer] and _replace(aid) > held
    assert sorted(t for _s, _r, t in after.values()) == sorted(t for _s, _r, t in before.values())
    assert not db.conn().execute("SELECT 1 FROM messages WHERE agent_id=? AND seq >= ?",
                                 (aid, _PARKED_SEQ_BASE)).fetchone()


def test_a_row_adopted_from_the_stream_whose_line_later_left_is_deleted_not_restored(tmp_path, monkeypatch):
    """(b) The border case: the reply was adopted from its live row (order A), so the transcript
    held its line; when that line leaves, the key (the keyed live id) says so: deleted, not
    restored as final:<trace>."""
    aid, path, now = _opened_case(tmp_path, monkeypatch, "defaults")
    now[0] = BASE + 300
    assert _stream(aid, FIRST) is not None
    _line(path, 200, text=FIRST)
    _import(aid, path)
    assert [i for i, _t, _k in _rows(aid)] == [_id(aid, 0)]
    _user(path, 600, "A later question")
    _line(path, 610, message_id="msg_B", text="A later answer.")
    turns = backends.parse_turns("claude", path)
    _store(aid, path, turns)
    without = [t for t in turns if t.get("provider_id") != "msg_A"]
    _store(aid, path, without)
    assert db.conn().execute("SELECT 1 FROM messages WHERE message_id=?", (_id(aid, 0),)).fetchone() is None
    assert not db.conn().execute("SELECT 1 FROM messages WHERE agent_id=? AND source_file LIKE 'final:%'",
                                 (aid,)).fetchone()


def _parked_after_import(aid, path, turns, message_id, *, trace="turn-1", stamp=BASE + 200):
    """Plant a reply parked by an earlier pass that nothing re-adopts, run a full read, and say
    where the row stands: 'deleted', or its source_file."""
    _plant(aid, path, seq=_PARKED_SEQ_BASE + 5, message_id=message_id, text="A reply the user saw.",
           trace=trace, stamp=stamp)
    _store(aid, path, turns)
    row = db.conn().execute("SELECT source_file FROM messages WHERE message_id=?", (message_id,)).fetchone()
    return "deleted" if row is None else row["source_file"]


def test_a_later_block_parked_is_known_by_its_prefix_and_deleted(tmp_path, monkeypatch):
    aid, path, turns = _shift_case(tmp_path, monkeypatch, count=1)
    assert _parked_after_import(aid, path, turns, _id(aid, 1, key="msg_Z")) == "deleted"


def test_a_keyed_block_zero_parked_is_deleted(tmp_path, monkeypatch):
    aid, path, turns = _shift_case(tmp_path, monkeypatch, count=1)
    assert _parked_after_import(aid, path, turns, _id(aid, 0, key="msg_Z")) == "deleted"


def test_a_keyless_live_row_parked_is_restored_as_today(tmp_path, monkeypatch):
    """RM-N1b unchanged: a reply adopted from a stream without a provider id (agy, grok, opencode,
    muse, an older row) is kept as its final:<trace> row."""
    aid, path, turns = _shift_case(tmp_path, monkeypatch, count=1)
    keyless = message_live._live_message_id(aid, S, "turn-1")
    assert _parked_after_import(aid, path, turns, keyless) == "final:turn-1"


@pytest.mark.parametrize("doubt", ["no turn row of its trace", "the turns begin after the row"])
def test_on_doubt_a_keyed_looking_live_row_is_restored_never_deleted(tmp_path, monkeypatch, doubt):
    """Block 0 is told from a key-less live row only by exclusion: when what it would be compared
    with is not all there (its turn row gone; the agent's turns begin after it, as when they did not
    come with a moved agent), the reply the user saw is kept, restored as today, never deleted."""
    aid, path, turns = _shift_case(tmp_path, monkeypatch, count=1)
    trace, stamp = ("turn-gone", BASE + 200) if doubt == "no turn row of its trace" else ("turn-1", BASE - 10_000)
    assert _parked_after_import(aid, path, turns, _id(aid, 0, key="msg_Z"), trace=trace,
                                stamp=stamp) == f"final:{trace}"


def test_a_line_that_comes_back_into_a_slot_a_user_line_took_meanwhile_keeps_its_id(tmp_path, monkeypatch):
    """Only the reply leaves (not its question), so the next question's positional row takes the
    reply's slot. When the reply comes back it finds a USER row there: that is another line's row,
    never its own row stored before the rule, so it is parked and the reply takes its own id again;
    the question is written again at its slot, nothing is lost."""
    aid, path, turns = _shift_case(tmp_path, monkeypatch, count=2)
    answer = _id(aid, 0, key="msg_0")
    before = _all(aid)
    _store(aid, path, [t for t in turns if t.get("provider_id") != "msg_0"])
    assert answer not in _all(aid)
    _store(aid, path, turns)
    after = _all(aid)
    assert after[answer] == before[answer]
    assert sorted((r, t) for _s, r, t in after.values()) == sorted((r, t) for _s, r, t in before.values())
    assert not db.conn().execute("SELECT 1 FROM messages WHERE agent_id=? AND seq >= ?",
                                 (aid, _PARKED_SEQ_BASE)).fetchone()


# --- the measured order-B recording through the importer and the live channel together ---------

def test_the_measured_order_b_reply_is_never_doubled_or_reordered(tmp_path, monkeypatch):
    """A measured many-block reply in order B (tests/fixtures/live_rec/order-b.jsonl: the stand-in
    CLI's stream events and transcript lines of one message, text, server tool, its result, text,
    with the recording's steps; block 0's line is imported before the stream's row is written).
    With block 0 under a positional id the iOS app drew the first block twice and then the second
    above the first, because it folds a stored row into a live block only when the item names the
    row's id. Here: after each import the stored row's id is the row_id its live item names; /log
    never holds the first block twice; first then second at every step. The stream writes one live
    row per text block (L2), so block 0's row is refused once its line is stored, and block 1
    streams into a row of its own, under the id its line takes, holding only its words; nothing
    is retracted and no reload is asked."""
    import pathlib
    from lib import live_hub
    from lib.backend import claude
    from lib.live_hub import LiveHub
    fixture = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "live_rec" / "order-b.jsonl"
    entries = [json.loads(line) for line in fixture.read_text().splitlines()]
    aid, path, now = _opened_case(tmp_path, monkeypatch, "defaults")
    hub = LiveHub(sink=lambda event: None)
    live_hub.install(hub)
    try:
        items = claude._live_items(aid, lambda: S, "turn-1")
        streamed = {}                      # block index -> its words so far, as the stream holds them
        clock_ms = BASE + 200
        replace = _replace(aid)
        first = "First block of the message."
        second = "Second block, from the search."
        for entry in entries:
            clock_ms += 10
            now[0] = clock_ms
            if "stream" in entry:
                event = entry["stream"]
                items.on_event({"type": "stream_event", "event": event})
                if event["type"] == "content_block_delta" and event["delta"].get("type") == "text_delta":
                    streamed[event["index"]] = streamed.get(event["index"], "") + event["delta"]["text"]
                if event["type"] == "content_block_stop" and event["index"] == 3:
                    # The drain settles a text block's row at its stop, before its line lands.
                    message_store.mark_live_message_final(agent_id=aid, backend_session_id=S,
                                                          trace_id="turn-1", item_key="msg_A", block=1)
                continue
            if "line" in entry:
                record = {"type": "assistant", "uuid": f"assistant-{clock_ms}", "timestamp": clock.iso_from_ms(clock_ms),
                          "message": entry["line"]}
                with path.open("a") as stream:
                    stream.write(json.dumps(record) + "\n")
                continue
            step = entry["step"]
            if step in ("B1", "T1", "T2", "C3"):
                _import(aid, path)
            elif step in ("B1b", "C1", "C2"):
                # The pacer writes the open text block's own row (content index 0 is block 0, 3 is
                # block 1: the server tool and its result are no lines).
                index = max(streamed)
                written = message_store.upsert_live_assistant_message(
                    agent_id=aid, backend_session_id=S, trace_id="turn-1", item_key="msg_A",
                    block={0: 0, 3: 1}[index], text=streamed[index])
                if step == "B1b":
                    assert written is None                    # block 0 is stored: refused
                else:
                    assert written is not None and written["id"] == _id(aid, 1)
                    assert written["text"] == streamed[3] and first not in written["text"]
            elif step == "E1":
                message_store.mark_live_message_final(agent_id=aid, backend_session_id=S, trace_id="turn-1",
                                                      item_key="msg_A")
            rows = _rows(aid)
            texts = [text for _i, text, _k in rows]
            assert sum(1 for text in texts if text.startswith(first)) <= 1, (step, rows)
            later = [n for n, text in enumerate(texts) if text != first]
            if later and first in texts:
                assert texts.index(first) < min(later), (step, rows)
            named = {item["id"]: item.get("row_id")
                     for item in (hub.snapshot(agent_id=aid) or {"items": []})["items"]
                     if item.get("kind") == "message"}
            if first in texts:
                assert [i for i, text, _k in rows if text == first] == [named["cl:msg_A:0"]], step
            if second in texts:
                assert [i for i, text, _k in rows if text == second] == [named["cl:msg_A:3"]], step
            for row_id, text, _k in rows:                      # every row is one block's words
                assert text == first or second.startswith(text), (step, rows)
                assert row_id in (named.get("cl:msg_A:0"), named.get("cl:msg_A:3")), (step, rows)
        assert _rows(aid) == [(_id(aid, 0), first, ""), (_id(aid, 1), second, "")]
        assert _replace(aid) == replace                       # no retraction, no reload asked
    finally:
        live_hub.install(None)


@pytest.mark.parametrize("key", ["seg1", "seg63"])
def test_a_keyless_streams_rolled_over_row_parked_is_restored_as_today(tmp_path, monkeypatch, key):
    """A key-less stream's later messages roll over to rows keyed seg1, seg2, ...: those are
    key-less rows too (RM-N1b keeps them), never taken for a keyed block 0."""
    aid, path, turns = _shift_case(tmp_path, monkeypatch, count=1)
    rolled = message_live._live_message_id(aid, S, "turn-1", key)
    assert _parked_after_import(aid, path, turns, rolled) == "final:turn-1"


def test_a_live_id_not_of_the_live_form_parked_is_restored_as_today(tmp_path, monkeypatch):
    """Only ids of the form live_message_id makes are told apart by exclusion; anything else is
    doubt, and doubt restores."""
    aid, path, turns = _shift_case(tmp_path, monkeypatch, count=1)
    assert _parked_after_import(aid, path, turns, "live-answer") == "final:turn-1"
