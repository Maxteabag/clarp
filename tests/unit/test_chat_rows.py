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
from lib import (message_previews, message_text, message_turns, message_writes,
                 transcript_incremental)
from lib.clock import iso_from_ms, now_ms
from lib.message_text import strip_delivery_context, strip_injected_context
from lib.turn_command import _with_delivery_context, _with_team_context

# The notice alone, as the dispatcher writes it.
NOTICE = _with_delivery_context("", unheard_audio=True).strip()
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


def test_the_tag_old_transcripts_and_stored_rows_contain_is_still_the_one_taken_off():
    """The prompt and the strip share one constant, so renaming it passes
    every other test. Transcripts and rows written before the rename keep
    the old tag for ever: this is that text, spelled out."""
    assert message_text.DELIVERY_CONTEXT_OPEN == "<clarp-delivery-context>"
    assert message_text.DELIVERY_CONTEXT_CLOSE == "</clarp-delivery-context>"
    stored = ("<clarp-delivery-context>\n"
              "The user began a new spoken turn before hearing one or more of your "
              "previous synthesized replies. Treat those replies as unheard. Carry "
              "forward anything still important, but answer the user's newest message "
              "naturally instead of blindly repeating the old response.\n"
              "</clarp-delivery-context>\n\n"
              "Stop. That is enough for now.")
    assert strip_delivery_context(stored) == "Stop. That is enough for now."
    assert _with_delivery_context("Stop. That is enough for now.", unheard_audio=True) == stored


def test_a_turn_that_is_only_the_notice_is_somebodys_words():
    """The Host never sends the notice without words behind it."""
    assert strip_delivery_context(NOTICE) == NOTICE
    assert strip_delivery_context(NOTICE + "\n\n  ") == NOTICE + "\n\n  "
    assert strip_injected_context(NOTICE) == NOTICE


@pytest.mark.parametrize("team", [False, True], ids=["alone", "inside-team-context"])
def test_a_barge_in_is_one_user_row_with_the_users_words(tmp_path, team):
    agent_id = _agent(tmp_path)
    sent = agents_db.record_user_message(
        agent_id=agent_id, backend_session_id=BSID, text=WORDS,
        client_msg_id="barge-1", trace_id="trace-1")
    prompt = _with_delivery_context(WORDS, unheard_audio=True)
    if team:                                  # as the dispatcher builds it
        prompt = _with_team_context(prompt, digest="a teammate said hello", protocol="be brief")

    # The provider's transcript holds the prompt it was given.
    _import(agent_id, [
        {"role": "user", "text": prompt, "timestamp": iso_from_ms(now_ms())},
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
        old.setattr(message_writes, "strip_delivery_context", lambda text: text)
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


# --- the notice is the user's own when they typed it ------------------------------
# Somebody reporting issue 284 pastes the notice as the first thing in a
# message. That text is theirs: the Host takes off only what the Host put there.

PASTED = NOTICE + "\n\nWhy is this text in my chat?"


def _users(agent_id):
    return [m["text"] for m in _visible(agent_id) if m["role"] == "user"]


@pytest.mark.parametrize("typed", [PASTED, NOTICE], ids=["notice-then-words", "only-the-notice"])
def test_a_message_the_user_began_with_the_notice_is_shown_once_as_typed(tmp_path, typed):
    agent_id = _agent(tmp_path)
    agents_db.record_user_message(
        agent_id=agent_id, backend_session_id=BSID, text=typed,
        client_msg_id="paste-1", trace_id="trace-1")
    turns = [{"role": "user", "text": typed, "timestamp": iso_from_ms(now_ms())},
             {"role": "assistant", "text": "It is a bug.", "timestamp": iso_from_ms(now_ms() + 1)}]

    # The chat list shows it as typed too, while it is the newest message.
    assert "The user began a new spoken turn" in message_previews.last_message_preview(
        agent_id=agent_id, max_len=400)

    _import(agent_id, turns)
    assert _users(agent_id) == [typed]
    _import(agent_id, turns)                                  # and again
    assert _users(agent_id) == [typed]
    assert _raw_user_rows(agent_id) == [typed]


def test_a_pasted_notice_in_a_barge_in_loses_only_the_hosts_notice(tmp_path):
    """The user's own message begins with the notice AND the send was a
    barge-in: the provider's copy has the Host's notice in front of theirs."""
    agent_id = _agent(tmp_path)
    agents_db.record_user_message(
        agent_id=agent_id, backend_session_id=BSID, text=PASTED,
        client_msg_id="paste-1", trace_id="trace-1")

    _import(agent_id, [
        {"role": "user", "text": _with_delivery_context(PASTED, unheard_audio=True),
         "timestamp": iso_from_ms(now_ms())},
        {"role": "assistant", "text": "It is a bug.", "timestamp": iso_from_ms(now_ms() + 1)}])

    assert _users(agent_id) == [PASTED] and _raw_user_rows(agent_id) == [PASTED]


@pytest.mark.parametrize("typed", [PASTED, NOTICE], ids=["notice-then-words", "only-the-notice"])
def test_a_store_built_before_the_fix_shows_a_pasted_notice_once_as_typed(
        tmp_path, monkeypatch, typed):
    """Main linked the provider's copy to the user's row, both with the
    notice. The next import must find the same row again."""
    agent_id = _agent(tmp_path)
    agents_db.record_user_message(
        agent_id=agent_id, backend_session_id=BSID, text=typed,
        client_msg_id="paste-1", trace_id="trace-1")
    turns = [{"role": "user", "text": typed, "timestamp": iso_from_ms(now_ms())},
             {"role": "assistant", "text": "It is a bug.", "timestamp": iso_from_ms(now_ms() + 1)}]
    with monkeypatch.context() as old:
        old.setattr(message_writes, "strip_delivery_context", lambda text: text)
        _import(agent_id, turns)
    assert _raw_user_rows(agent_id) == [typed]

    _import(agent_id, turns)

    assert _users(agent_id) == [typed] and _raw_user_rows(agent_id) == [typed]


def test_without_a_row_of_the_users_a_notice_in_front_is_the_hosts(tmp_path):
    """A turn nobody sent through Clarp (typed in a terminal). A notice in
    front of words is taken for the Host's; a turn that is only the notice is
    kept, never an empty message."""
    agent_id = _agent(tmp_path)
    _import(agent_id, [
        {"role": "user", "text": _with_delivery_context(WORDS, unheard_audio=True),
         "timestamp": "2020-01-01T10:00:00Z"},
        {"role": "assistant", "text": "Okay.", "timestamp": "2020-01-01T10:00:05Z"},
        {"role": "user", "text": NOTICE, "timestamp": "2020-01-01T10:01:00Z"},
        {"role": "assistant", "text": "That is the notice.", "timestamp": "2020-01-01T10:01:05Z"}])

    assert _users(agent_id) == [WORDS, NOTICE]
    assert all(m["text"] for m in _visible(agent_id))


# --- issue 285: the same words sent before, by sends that never ran ---------------

PROMPT = "Read me the last message, please."
REPLY = "My last message was: it feels like a grey day."


def _sent(agent_id, client_msg_id, trace_id, text, *, seconds_ago=0, origin="user"):
    """A user row as /send records it, ``seconds_ago`` in the past (rows are
    matched in the order they were recorded)."""
    row = agents_db.record_user_message(
        agent_id=agent_id, backend_session_id=BSID, text=text,
        client_msg_id=client_msg_id, trace_id=trace_id, origin=origin)
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


def test_a_reply_answers_the_message_whose_turn_ran(tmp_path):
    """Whose message a reply answers comes from the row its prompt is linked
    to. Another agent sent the words and never got a turn; the user sent them
    and did. The reply is to the user."""
    agent_id = _agent(tmp_path)
    _sent(agent_id, "peer", "stale-trace", PROMPT, seconds_ago=4 * 3600, origin="agent")
    _sent(agent_id, "ran", "trace-ran", PROMPT)
    agents_db.open_turn(agent_id=agent_id, source="pwa", trace_id="trace-ran")

    _import(agent_id, [
        {"role": "user", "text": PROMPT, "timestamp": iso_from_ms(now_ms())},
        {"role": "assistant", "text": REPLY, "timestamp": iso_from_ms(now_ms() + 1)}])

    [reply] = [m for m in _visible(agent_id) if m["role"] == "assistant"]
    assert (reply["origin"], reply["trace_id"]) == ("user", "trace-ran")


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
        # As before the fix: the oldest row, and its trace for the reply.
        old.setattr(message_turns.TurnIndex, "running_at", lambda self, timestamp: ("", None))
        _import(agent_id, turns)
    before = [m for m in _visible(agent_id) if m["role"] == "assistant"]
    assert sorted(m["id"][:4] for m in before) == ["live", "msg-"], \
        "the fixture did not reproduce the reply stored twice"
    durable = next(m for m in before if m["id"] != live["id"])

    _import(agent_id, turns)

    replies = [m for m in _visible(agent_id) if m["role"] == "assistant"]
    assert [(m["id"], m["text"]) for m in replies] == [(durable["id"], REPLY)]
    assert replies[0]["trace_id"] == "trace-ran"


@pytest.mark.parametrize("known, reply_trace", [
    ("no turn at that time", "trace-1"),
    # The reply is the running turn's, whichever row its prompt is linked to.
    ("a turn none of the rows belongs to", "some-other-trace"),
    ("no turn, and a later row without a trace", "trace-1"),
])
def test_without_a_turn_to_go_by_the_oldest_row_is_linked(tmp_path, known, reply_trace):
    """Which row was linked shows in whose message the reply answers: the
    older row is the user's, the newer one another agent's."""
    agent_id = _agent(tmp_path)
    _sent(agent_id, "m1", "trace-1", "lol", seconds_ago=600)
    _sent(agent_id, "m2", "" if known.endswith("without a trace") else "trace-2", "lol",
          origin="agent", seconds_ago=5)                    # before any turn below began
    if known == "a turn none of the rows belongs to":
        agents_db.open_turn(agent_id=agent_id, source="pwa", trace_id="some-other-trace")
    stamp = iso_from_ms(now_ms())

    _import(agent_id, [{"role": "user", "text": "lol", "timestamp": stamp},
                       {"role": "assistant", "text": "reply", "timestamp": stamp}])

    [reply] = [m for m in _visible(agent_id) if m["role"] == "assistant"]
    assert (reply["origin"], reply["trace_id"]) == ("user", reply_trace)


# --- a message steered into the running turn --------------------------------------

def _running_turn(agent_id, trace_id, started_seconds_ago):
    agents_db.open_turn(agent_id=agent_id, source="pwa", trace_id=trace_id)
    agents_db.conn().execute(
        "UPDATE turns SET started_at=? WHERE trace_id=?",
        (now_ms() - started_seconds_ago * 1000, trace_id))


@pytest.mark.parametrize("stale", [True, False], ids=["after-a-send-that-never-ran", "nothing-before"])
def test_a_reply_is_shown_once_when_the_same_words_were_steered_into_its_turn(tmp_path, stale):
    """The same words: (a send that never ran,) one that runs, and one steered
    into that running turn, which has no turn of its own. The reply streams
    under the running turn's trace and is stored once, under its live id.
    With the stale send, linking the steered copy to the oldest row left gave
    the reply the never-run send's trace and stored it twice."""
    agent_id = _agent(tmp_path)
    if stale:                                  # another agent's, so the link shows
        _sent(agent_id, "m0", "stale-0", "go", seconds_ago=4 * 3600, origin="agent")
    _sent(agent_id, "m1", "trace-1", "go", seconds_ago=300)
    _running_turn(agent_id, "trace-1", 300)
    _sent(agent_id, "m2", "trace-2", "go", seconds_ago=250)            # steered into it
    live = agents_db.upsert_live_assistant_message(
        agent_id=agent_id, backend_session_id=BSID, trace_id="trace-1", text="done")
    turns = [{"role": "user", "text": "go", "timestamp": iso_from_ms(now_ms() - 299_000)},
             {"role": "user", "text": "go", "timestamp": iso_from_ms(now_ms() - 249_000)},
             {"role": "assistant", "text": "done", "timestamp": iso_from_ms(now_ms() - 5_000)}]

    for _again in range(3):
        _import(agent_id, turns)
        replies = [m for m in _visible(agent_id) if m["role"] == "assistant"]
        assert [(m["id"], m["text"], m["trace_id"]) for m in replies] == [
            (live["id"], "done", "trace-1")]
        # It answers the user's steered message, not the send that never ran.
        assert replies[0]["origin"] == "user"
        assert _users(agent_id) == ["go"] * (3 if stale else 2)


def test_a_steered_copy_is_linked_to_a_row_sent_after_the_turn_began(tmp_path):
    """Not to one sent before it: that one never ran, or it would be the
    turn's own row. The oldest only when none was sent after the start."""
    database = agents_db.conn()
    agent_id = _agent(tmp_path)
    _sent(agent_id, "m0", "stale-0", "go", seconds_ago=4 * 3600)
    _sent(agent_id, "m2", "trace-2", "go", seconds_ago=250)
    _sent(agent_id, "m3", "trace-3", "go", seconds_ago=100)
    rows = message_writes._ClientProvenance(database, agent_id, BSID).candidates("go")
    assert [row[0] for row in rows] == ["u-m0", "u-m2", "u-m3"]
    began = now_ms() - 300_000

    pick = message_writes._own_turns_row
    assert pick(database, rows, "trace-1", began) == 1                # the first sent after it began
    assert pick(database, rows, "trace-3", began) == 2                # the turn's own row, first of all
    assert pick(database, rows, "trace-1", now_ms()) == 0             # none after: the oldest
    sent_at = message_turns.iso_ms(rows[1][3])
    assert pick(database, rows, "trace-1", sent_at) == 2              # at the start is not after it
    assert pick(database, rows, "trace-1", sent_at - 1) == 1
    assert pick(database, rows, "", None) == 0                        # no turn running
    assert pick(database, rows[:1], "trace-1", began) == 0


def _ended_turn(agent_id, trace_id, started_seconds_ago, ended_seconds_ago):
    agents_db.open_turn(agent_id=agent_id, source="pwa", trace_id=trace_id)
    now = now_ms()
    agents_db.conn().execute(
        "UPDATE turns SET started_at=?, ended_at=?, settled_at=? WHERE trace_id=?",
        (now - started_seconds_ago * 1000, now - ended_seconds_ago * 1000,
         now - ended_seconds_ago * 1000, trace_id))


def test_a_copy_stamped_just_before_its_own_turn_began_keeps_that_turns_trace(tmp_path):
    """The provider stamps the prompt three seconds before its turn row
    begins, inside the reach of the turn before it. The linked row has a turn
    of its own, so the reply is that turn's: the turn that happened to be
    "running" at the stamp overrides only a row that has none."""
    agent_id = _agent(tmp_path)
    _sent(agent_id, "m1", "trace-1", "first question", seconds_ago=600)
    _ended_turn(agent_id, "trace-1", 600, 400)
    _sent(agent_id, "m2", "trace-2", "second question", seconds_ago=399)
    _ended_turn(agent_id, "trace-2", 395, 300)
    turns = [{"role": "user", "text": "first question", "timestamp": iso_from_ms(now_ms() - 599_000)},
             {"role": "assistant", "text": "first answer", "timestamp": iso_from_ms(now_ms() - 405_000)},
             {"role": "user", "text": "second question", "timestamp": iso_from_ms(now_ms() - 398_000)},
             {"role": "assistant", "text": "second answer", "timestamp": iso_from_ms(now_ms() - 305_000)}]

    for _again in range(2):
        _import(agent_id, turns)
        replies = {m["text"]: m["trace_id"] for m in _visible(agent_id) if m["role"] == "assistant"}
        assert replies == {"first answer": "trace-1", "second answer": "trace-2"}
