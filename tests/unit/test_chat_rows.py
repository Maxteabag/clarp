"""What the chat shows for one message is one row, under the id it was announced with.

Two cases from a spoken check on 12 November 2026 (issues 284 and 285):

* A barge-in's delivery notice is for the agent's prompt only. The provider's
  transcript copies the whole prompt back, and the import stored that copy as
  a second user row, wrapper first, next to the row that holds the user's own
  words.
* A reply was stored under a new ``msg-`` id beside its ``live-`` row. Its
  prompt had been sent three times before while the agent could not run
  (those sends were removed from the queue, their rows stay in the chat), and
  the import linked the prompt's transcript copy to the oldest of the four
  rows with that text instead of the one whose turn ran.
"""
from __future__ import annotations

import pytest

from lib import agents as agents_db
from lib import message_previews, message_text, message_writes, transcript_incremental
from lib.clock import iso_from_ms, now_ms
from lib.message_text import strip_delivery_context, strip_injected_context
from lib.turn_command import _with_delivery_context, _with_team_context

BSID = "backend-1"
TRANSCRIPT = "/tmp/transcript.jsonl"
WORDS = "Stop. That is enough for now."


def _agent(tmp_path):
    return agents_db.create_agent(persona="Mike", voice_id="V", cwd=str(tmp_path), session="mike")


def _visible(agent_id):
    return agents_db.list_messages(agent_id=agent_id, backend_session_id=BSID)


def _import(agent_id, turns):
    agents_db.store_transcript_turns(
        agent_id=agent_id, backend_session_id=BSID, source_file=TRANSCRIPT, turns=turns)


# --- issue 284: the delivery notice of a barge-in ---------------------------------

def test_the_prompt_carries_the_notice_and_the_users_words_come_back_out():
    prompt = _with_delivery_context(WORDS, unheard_audio=True)
    assert prompt.startswith(message_text.DELIVERY_CONTEXT_OPEN) and prompt.endswith(WORDS)
    assert message_text.DELIVERY_CONTEXT_CLOSE in prompt
    assert _with_delivery_context(WORDS, unheard_audio=False) == WORDS

    assert strip_delivery_context(prompt) == WORDS
    assert strip_injected_context(prompt) == WORDS
    # As the dispatcher builds it: team context around the notice.
    whole = _with_team_context(prompt, digest="a teammate said hello", protocol="be brief")
    assert whole != prompt and strip_injected_context(whole) == WORDS


def test_words_that_only_mention_the_notice_are_left_alone():
    """Only a notice in front of the words is the server's. A message that
    quotes the tag (a bug report about it, say) is the user's own text."""
    quoting = f"Why does my chat show {message_text.DELIVERY_CONTEXT_OPEN} and " \
              f"{message_text.DELIVERY_CONTEXT_CLOSE} as a message?"
    assert strip_delivery_context(quoting) == quoting
    assert strip_injected_context(quoting) == quoting
    unclosed = f"{message_text.DELIVERY_CONTEXT_OPEN} never closed"
    assert strip_delivery_context(unclosed) == unclosed
    assert strip_delivery_context("") == "" and strip_delivery_context("plain") == "plain"


def test_a_barge_in_is_one_user_row_with_the_users_words(tmp_path):
    agent_id = _agent(tmp_path)
    sent = agents_db.record_user_message(
        agent_id=agent_id, backend_session_id=BSID, text=WORDS,
        client_msg_id="barge-1", trace_id="trace-1")

    # The provider's transcript holds the prompt it was given.
    _import(agent_id, [
        {"role": "user", "text": _with_delivery_context(WORDS, unheard_audio=True),
         "timestamp": iso_from_ms(now_ms())},
        {"role": "assistant", "text": "Okay, stopping here.", "timestamp": iso_from_ms(now_ms() + 1)},
    ])

    visible = _visible(agent_id)
    assert [(m["role"], m["text"]) for m in visible] == [
        ("user", WORDS), ("assistant", "Okay, stopping here.")]
    assert visible[0]["id"] == sent["id"]
    assert not any("clarp-delivery-context" in m["text"] for m in visible)


def _the_old_import_stored_the_wrapper(monkeypatch, agent_id, turns):
    """The rows as the import left them before the fix."""
    with monkeypatch.context() as old:
        old.setattr(message_writes, "strip_injected_context", lambda text: text)
        _import(agent_id, turns)
    stored = [m for m in _raw_user_rows(agent_id) if "clarp-delivery-context" in m]
    assert len(stored) == 1, "the fixture did not reproduce the stored wrapper row"


def _raw_user_rows(agent_id):
    return [row["text"] for row in agents_db.conn().execute(
        "SELECT text FROM messages WHERE agent_id=? AND role='user' ORDER BY timestamp, seq",
        (agent_id,))]


def test_a_wrapper_row_already_stored_is_removed_by_the_next_import(tmp_path, monkeypatch):
    """The transcript is imported again from its start when the import's code
    changes (the parser version), and that pass finds the turn owned by the
    client row and clears the slot the wrapper row sat in."""
    agent_id = _agent(tmp_path)
    agents_db.record_user_message(
        agent_id=agent_id, backend_session_id=BSID, text=WORDS,
        client_msg_id="barge-1", trace_id="trace-1")
    turns = [
        {"role": "user", "text": _with_delivery_context(WORDS, unheard_audio=True),
         "timestamp": iso_from_ms(now_ms())},
        {"role": "assistant", "text": "Okay, stopping here.", "timestamp": iso_from_ms(now_ms() + 1)},
    ]
    _the_old_import_stored_the_wrapper(monkeypatch, agent_id, turns)
    reply = _visible(agent_id)[-1]

    _import(agent_id, turns)

    assert _raw_user_rows(agent_id) == [WORDS]
    visible = _visible(agent_id)
    assert [(m["role"], m["text"]) for m in visible] == [
        ("user", WORDS), ("assistant", "Okay, stopping here.")]
    assert visible[-1]["id"] == reply["id"]
    # Why that import comes: the files that decide this are part of the version.
    for name in ("message_text.py", "message_writes.py", "message_context.py"):
        assert name in transcript_incremental._COMMON_SOURCES


def test_a_wrapper_row_whose_transcript_is_gone_is_served_without_the_wrapper(tmp_path, monkeypatch):
    """No import comes for it any more (old conversations: five such rows on
    one Host, from September). The row may be the only copy of what the user
    said, so it stays, and is served as the user's words."""
    agent_id = _agent(tmp_path)
    turns = [{"role": "user", "text": _with_delivery_context(WORDS, unheard_audio=True),
              "timestamp": iso_from_ms(now_ms())}]
    _the_old_import_stored_the_wrapper(monkeypatch, agent_id, turns)

    [served] = _visible(agent_id)
    assert (served["role"], served["text"]) == ("user", WORDS)
    preview = message_previews.last_message_preview(agent_id=agent_id)
    assert "clarp-delivery-context" not in preview and WORDS in preview
    # Nothing was rewritten: reading does not write.
    assert any("clarp-delivery-context" in text for text in _raw_user_rows(agent_id))


def test_an_agents_reply_that_quotes_the_tag_is_served_as_written(tmp_path):
    agent_id = _agent(tmp_path)
    said = f"{message_text.DELIVERY_CONTEXT_OPEN}x{message_text.DELIVERY_CONTEXT_CLOSE} is the tag."
    _import(agent_id, [
        {"role": "user", "text": "what is the tag?", "timestamp": "2020-01-01T10:00:00Z"},
        {"role": "assistant", "text": said, "timestamp": "2020-01-01T10:00:05Z"},
    ])
    assert _visible(agent_id)[-1]["text"] == said


# --- issue 285: the same words sent before, by sends that never ran ---------------

PROMPT = "Read me the last message, please."
REPLY = "My last message was: it feels like a grey day."


def _sent(agent_id, client_msg_id, trace_id, text, *, seconds_ago=0):
    """A user row as /send records it, ``seconds_ago`` in the past (rows are
    matched in the order they were recorded)."""
    row = agents_db.record_user_message(
        agent_id=agent_id, backend_session_id=BSID, text=text,
        client_msg_id=client_msg_id, trace_id=trace_id)
    if seconds_ago:
        agents_db.conn().execute(
            "UPDATE messages SET updated_at=updated_at-?, timestamp=? WHERE message_id=?",
            (seconds_ago * 1000, iso_from_ms(now_ms() - seconds_ago * 1000), row["id"]))
    return row


def _the_morning(tmp_path, earlier=3):
    """Three sends while the agent could not run; they were removed from the
    queue, and their rows stay in the chat. Then the same words again, and
    this time the turn runs and streams its reply."""
    agent_id = _agent(tmp_path)
    for number in range(earlier):
        _sent(agent_id, f"stale-{number}", f"stale-trace-{number}", PROMPT,
              seconds_ago=4 * 3600 - 60 * number)
    ran = _sent(agent_id, "ran", "trace-ran", PROMPT)
    agents_db.open_turn(agent_id=agent_id, source="pwa", trace_id="trace-ran")
    live = agents_db.upsert_live_assistant_message(
        agent_id=agent_id, backend_session_id=BSID, trace_id="trace-ran", text=REPLY)
    turns = [
        {"role": "user", "text": PROMPT, "timestamp": iso_from_ms(now_ms())},
        {"role": "assistant", "text": REPLY, "timestamp": iso_from_ms(now_ms() + 1)},
    ]
    return agent_id, ran, live, turns


def test_a_reply_keeps_its_live_id_though_the_same_words_were_sent_before(tmp_path):
    agent_id, ran, live, turns = _the_morning(tmp_path)

    _import(agent_id, turns)

    replies = [m for m in _visible(agent_id) if m["role"] == "assistant"]
    assert [(m["id"], m["text"], m["kind"]) for m in replies] == [(live["id"], REPLY, None)]
    assert replies[0]["trace_id"] == "trace-ran"
    # The three sends that never ran are still the user's messages, once each.
    assert [m["text"] for m in _visible(agent_id) if m["role"] == "user"] == [PROMPT] * 4


def test_words_sent_twice_and_run_twice_are_still_linked_in_order(tmp_path):
    """Both ran, in order. No turn was running when the first copy was
    written (a transcript timestamp outside every turn), so the oldest is
    taken, as it always was; the second finds its own turn."""
    agent_id = _agent(tmp_path)
    _sent(agent_id, "m1", "trace-1", "lol", seconds_ago=600)
    _sent(agent_id, "m2", "trace-2", "lol")
    agents_db.open_turn(agent_id=agent_id, source="pwa", trace_id="trace-2")
    live = agents_db.upsert_live_assistant_message(
        agent_id=agent_id, backend_session_id=BSID, trace_id="trace-2", text="second reply")

    _import(agent_id, [
        {"role": "user", "text": "lol", "timestamp": "2020-01-01T10:00:00Z"},
        {"role": "assistant", "text": "first reply", "timestamp": "2020-01-01T10:00:05Z"},
        {"role": "user", "text": "lol", "timestamp": iso_from_ms(now_ms())},
        {"role": "assistant", "text": "second reply", "timestamp": iso_from_ms(now_ms() + 1)},
    ])

    visible = _visible(agent_id)
    assert [m["text"] for m in visible if m["role"] == "user"] == ["lol", "lol"]
    replies = {m["text"]: m for m in visible if m["role"] == "assistant"}
    assert replies["first reply"]["trace_id"] == "trace-1"
    assert (replies["second reply"]["id"], replies["second reply"]["trace_id"]) == (
        live["id"], "trace-2")


def test_a_reply_already_stored_beside_its_live_row_is_put_right_by_the_next_import(
        tmp_path, monkeypatch):
    """What 12 November left: the reply as a ``msg-`` row, the ``live-`` row
    still there. The next import from the start knows the live row for the
    same reply and removes it; the row the chat was already served stays."""
    agent_id, ran, live, turns = _the_morning(tmp_path)
    with monkeypatch.context() as old:
        old.setattr(message_writes, "_own_turns_row", lambda *args, **kwargs: 0)
        _import(agent_id, turns)
    before = [m for m in _visible(agent_id) if m["role"] == "assistant"]
    assert sorted(m["id"][:4] for m in before) == ["live", "msg-"], \
        "the fixture did not reproduce the reply stored twice"
    durable = next(m for m in before if m["id"] != live["id"])

    _import(agent_id, turns)

    replies = [m for m in _visible(agent_id) if m["role"] == "assistant"]
    assert [(m["id"], m["text"]) for m in replies] == [(durable["id"], REPLY)]
    assert replies[0]["trace_id"] == "trace-ran"


@pytest.mark.parametrize("known", ["no turn at that time", "a turn none of the rows belongs to",
                                   "no turn, and a later row without a trace"])
def test_without_a_turn_to_go_by_the_oldest_row_is_linked(tmp_path, known):
    agent_id = _agent(tmp_path)
    _sent(agent_id, "m1", "trace-1", "lol", seconds_ago=600)
    _sent(agent_id, "m2", "" if known.endswith("without a trace") else "trace-2", "lol")
    if known == "a turn none of the rows belongs to":
        agents_db.open_turn(agent_id=agent_id, source="pwa", trace_id="some-other-trace")
    stamp = iso_from_ms(now_ms())

    _import(agent_id, [{"role": "user", "text": "lol", "timestamp": stamp},
                       {"role": "assistant", "text": "reply", "timestamp": stamp}])

    [reply] = [m for m in _visible(agent_id) if m["role"] == "assistant"]
    assert reply["trace_id"] == "trace-1"
