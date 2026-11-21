"""The import of a session a takeover left (issue 491, part B; the import commit).

The session file is built from the SHAPES of the entries a real Claude Code 2.1.321 wrote
in the case-7 rerun (one-line, before-init): meta lines (session-owner, queue-operation,
api-request ...), user entries with `uuid` and `parentUuid`, attachments, assistant entries.
Every id and word here is this test's own. Two variants:
- one-line: message 1's own entry is in the file (the held-and-killed turn wrote it), off
  the active chain; the takeover's ONE user line of two text blocks follows, its parent the
  entry before message 1;
- before-init: message 1 never reached the file; only the two-block line.
Either way the chat holds exactly the three client rows (m0, m1, m2), no row for the line
and none for the dropped entry, and the answer belongs to the takeover's trace (t-m2). A
second import pass changes nothing."""
from __future__ import annotations

import json

import pytest

from lib import agents as agents_db
from lib import message_store
from lib.claude_transcript import parse_turns

SESSION = "00000000-0000-4000-8000-000000000491"
M0, M1, M2 = "Remember the word harbour.", "What is the capital of Norway?", "And 6 times 7?"


def _meta(kind):
    return {"type": kind, "sessionId": SESSION}


def _user(uid, parent, blocks, ts):
    return {"type": "user", "uuid": uid, "parentUuid": parent, "isSidechain": False,
            "sessionId": SESSION, "timestamp": ts, "promptId": f"p-{uid}",
            "message": {"role": "user", "content": [{"type": "text", "text": b} for b in blocks]}}


def _attachment(uid, parent, ts):
    return {"type": "attachment", "uuid": uid, "parentUuid": parent, "isSidechain": False,
            "sessionId": SESSION, "timestamp": ts, "attachment": {"type": "skill_listing"}}


def _assistant(uid, parent, text, ts):
    return {"type": "assistant", "uuid": uid, "parentUuid": parent, "isSidechain": False,
            "sessionId": SESSION, "timestamp": ts,
            "message": {"id": f"msg-{uid}", "role": "assistant", "model": "claude-test",
                        "content": [{"type": "text", "text": text}]}}


def _session_file(path, *, message_one_written: bool):
    rows = [_meta("session-owner"), _meta("queue-operation"),
            _user("u0", None, [M0], "2026-11-20T10:00:00.000Z"),
            _attachment("at0", "u0", "2026-11-20T10:00:00.100Z"),
            _meta("api-request"),
            _assistant("a0", "at0", "OK", "2026-11-20T10:00:02.000Z"),
            _meta("cost-state")]
    if message_one_written:
        rows += [_meta("queue-operation"),
                 _user("u1", "a0", [M1], "2026-11-20T10:00:10.000Z"),
                 _attachment("at1", "u1", "2026-11-20T10:00:10.100Z")]
    rows += [_meta("queue-operation"),
             _user("u2", "a0", [M1, M2], "2026-11-20T10:00:20.000Z"),
             _attachment("at2", "u2", "2026-11-20T10:00:20.100Z"),
             _assistant("a2", "at2", "Oslo. 6 times 7 is 42.", "2026-11-20T10:00:23.000Z"),
             _meta("last-prompt")]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")


@pytest.mark.parametrize("message_one_written", [True, False], ids=["one-line", "before-init"])
def test_a_takeover_session_imports_as_the_three_client_rows_and_one_answer(
        tmp_path, message_one_written):
    agent = agents_db.create_agent(persona="Mike", voice_id="V", cwd=str(tmp_path),
                                   session="mike", backend="claude")
    for cid, text in (("m0", M0), ("m1", M1), ("m2", M2)):
        message_store.record_user_message(agent_id=agent, backend_session_id=SESSION,
                                          client_msg_id=cid, text=text, trace_id=f"t-{cid}")
    path = tmp_path / f"{SESSION}.jsonl"
    _session_file(path, message_one_written=message_one_written)
    turns = parse_turns(path)
    assert [t.get("parts") for t in turns if t["role"] == "user"][-1] == [M1, M2]

    def imported():
        message_store.store_transcript_turns(agent_id=agent, backend_session_id=SESSION,
                                             source_file=str(path), turns=parse_turns(path))
        return [(r["id"], r["role"], r["text"], r["trace_id"]) for r in
                message_store.list_messages(agent_id=agent, backend_session_id=SESSION)]

    rows = imported()
    users = [(i, text) for i, role, text, _t in rows if role == "user"]
    assert users == [("u-m0", M0), ("u-m1", M1), ("u-m2", M2)]
    answer = [r for r in rows if r[1] == "assistant" and "42" in r[2]]
    assert len(answer) == 1 and answer[0][3] == "t-m2"
    assert imported() == rows                                  # a second pass: the same


def test_a_two_block_line_with_a_block_nobody_sent_is_kept_as_it_is(tmp_path):
    """No client row for a block: the line is imported as one row, as before (never invented,
    never dropped)."""
    agent = agents_db.create_agent(persona="Mike", voice_id="V", cwd=str(tmp_path),
                                   session="mike", backend="claude")
    message_store.record_user_message(agent_id=agent, backend_session_id=SESSION,
                                      client_msg_id="m1", text=M1, trace_id="t-m1")
    path = tmp_path / f"{SESSION}.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in [
        _user("u2", None, [M1, "typed in a terminal"], "2026-11-20T10:00:20.000Z"),
        _assistant("a2", "u2", "Done.", "2026-11-20T10:00:23.000Z")]) + "\n")
    message_store.store_transcript_turns(agent_id=agent, backend_session_id=SESSION,
                                         source_file=str(path), turns=parse_turns(path))
    users = [r["text"] for r in message_store.list_messages(agent_id=agent, backend_session_id=SESSION)
             if r["role"] == "user"]
    assert M1 in users and any("typed in a terminal" in t for t in users)
