"""A live row is the stream of one provider message, known by that message's id (R107).

When an import brings in a message whose slot it already holds, and a live row of the same turn
carries THAT message's id, the live row is the imported message's stream: it is retracted, with
replace_revision in the same commit. A live row of ANOTHER id is another message, whatever its
words: kept, as R19 rules. With no id on either side the rule is unchanged."""
from __future__ import annotations

import json

import pytest

from lib import clock, db, message_store
from tests.unit.test_live_import_identity import BASE, _import, _opened_case


def _line(path, text, offset, *, message_id, tool=False):
    """One Claude transcript line of an assistant message, its id as Claude writes it."""
    content = [{"type": "text", "text": text}]
    if tool:                                        # a commentary: text before a tool call
        content.append({"type": "tool_use", "id": f"toolu_{offset}", "name": "Bash",
                        "input": {"command": "ls"}})
    record = {"type": "assistant", "uuid": f"assistant-{offset}",
              "timestamp": clock.iso_from_ms(BASE + offset),
              "message": {"id": message_id, "role": "assistant", "model": "synthetic-fixture",
                          "content": content}}
    with path.open("a") as stream:
        stream.write(json.dumps(record) + "\n")


def _rows(aid):
    return [(r["id"], r["text"]) for r in message_store.list_messages(agent_id=aid, backend_session_id="native-session")
            if r["role"] == "assistant"]


def _replace_revision(aid):
    row = db.conn().execute("SELECT replace_revision FROM conversation_heads WHERE agent_id=? AND "
                            "backend_session_id='native-session'", (aid,)).fetchone()
    return row[0] if row else 0


def _stream(aid, key, text):
    return message_store.upsert_live_assistant_message(
        agent_id=aid, backend_session_id="native-session", trace_id="turn-1", item_key=key, text=text)


@pytest.mark.parametrize("message", ["reply", "commentary"])
@pytest.mark.parametrize("key", ["the same id", "another id"])
def test_a_live_row_with_the_same_words_is_retracted_by_its_own_id_and_kept_under_another(
        tmp_path, monkeypatch, message, key):
    aid, path, now = _opened_case(tmp_path, monkeypatch, "defaults")
    words = "Synthetic words of one message."
    _line(path, words, 200, message_id="msg_A", tool=message == "commentary")
    _import(aid, path)                                          # the message's row holds its slot
    now[0] = BASE + 300
    live = _stream(aid, "msg_A" if key == "the same id" else "msg_B", words)
    held = _replace_revision(aid)
    _import(aid, path)
    ids = [row_id for row_id, _ in _rows(aid)]
    if key == "the same id":
        assert live["id"] not in ids and len(ids) == 1        # its stream, retracted
        assert _replace_revision(aid) > held                   # and the apps told to replace
    else:
        assert live["id"] in ids and len(ids) == 2             # another message: kept (R19)
        assert _replace_revision(aid) == held


def test_a_live_row_of_the_same_id_that_goes_beyond_the_imported_words_is_retracted_too(tmp_path, monkeypatch):
    """Claude writes each content block of a message as a line of its own, so a message with two
    text blocks (text, a server tool, text) has its first block imported while the message streams
    on: its live row then holds more words than the imported line. By id it is that message's
    stream: retracted with the revision; the stream's next write starts its row again."""
    aid, path, now = _opened_case(tmp_path, monkeypatch, "defaults")
    _line(path, "First block of the message.", 200, message_id="msg_A")
    _import(aid, path)
    now[0] = BASE + 300
    live = _stream(aid, "msg_A", "First block of the message. Second block, still streaming")
    held = _replace_revision(aid)
    _import(aid, path)
    assert live["id"] not in [row_id for row_id, _ in _rows(aid)] and _replace_revision(aid) > held
    again = _stream(aid, "msg_A", "First block of the message. Second block, still streaming, on")
    assert again is not None and again["id"] == live["id"]


def test_with_no_id_on_the_imported_line_the_rule_is_unchanged(tmp_path, monkeypatch):
    """An older line without an id: identity is unknown, so a same-words live row is kept as R19
    keeps it today."""
    aid, path, now = _opened_case(tmp_path, monkeypatch, "defaults")
    _line(path, "Synthetic words of one message.", 200, message_id="")
    _import(aid, path)
    now[0] = BASE + 300
    live = _stream(aid, "msg_A", "Synthetic words of one message.")
    _import(aid, path)
    assert live["id"] in [row_id for row_id, _ in _rows(aid)]


def test_with_no_id_on_the_line_an_unkeyed_live_row_is_kept_as_on_main(tmp_path, monkeypatch):
    """The reviewer's surviving mutant: an imported line WITHOUT an id beside an UNKEYED live row
    (agy, grok, opencode and muse stream one row per trace, with no key). With no id there is no
    identity to match, so the row is kept and nothing asks the clients to reload, as on main."""
    aid, path, now = _opened_case(tmp_path, monkeypatch, "defaults")
    _line(path, "Synthetic words of one message.", 200, message_id="")
    _import(aid, path)
    now[0] = BASE + 300
    live = _stream(aid, "", "Synthetic words of one message.")
    before = _replace_revision(aid)
    _import(aid, path)
    assert live["id"] in [row_id for row_id, _ in _rows(aid)]
    assert _replace_revision(aid) == before


def test_each_retraction_of_its_own_stream_is_one_line_without_ids(tmp_path, monkeypatch):
    lines = []
    from lib import log as log_module
    real = log_module.log
    monkeypatch.setattr(log_module, "log", lambda event, detail="", *a, **k: (
        lines.append((event, detail)), real(event, detail, *a, **k)))
    aid, path, now = _opened_case(tmp_path, monkeypatch, "defaults")
    _line(path, "Synthetic words of one message.", 200, message_id="msg_A")
    _import(aid, path)
    now[0] = BASE + 300
    _stream(aid, "msg_A", "Synthetic words of one message.")
    _import(aid, path)
    retracted = [detail for event, detail in lines if event == "importLiveOwnStreamRetracted"]
    assert retracted == ["backend=claude count=1"]
    assert "msg_A" not in retracted[0] and aid not in retracted[0]
