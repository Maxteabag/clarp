"""A live row beside old replies of its own turn (``carry_live_rows_by_words``).

``carry_live_rows`` lets a tail run when a live row changed, unless a reply
before the boundary carries the row's trace: the running turn then spans the
boundary. That is refused whatever the words are. What a full replay does
with such a row is narrower: it removes a leftover live row only when a reply
of the row's own trace in its batch has words RELATED to the row's (one text
begins with the other; ``message_text._related``), and a tail's batch holds
only the replies from the boundary on. So a tail and a full replay differ
only when a reply before the boundary has the trace and related words, and
with the switch on only that is a full import.

Where it happens (issue 352): a context compaction leaves a user record
inside a running turn. The next full import puts the boundary there, the
turn's earlier replies lie before it, and every word the turn streams after
that was a full import of the whole conversation. And a second shape: the
last reply of a turn written within two seconds before the next turn began
is given the next turn's trace, and lies before the boundary at that turn's
prompt.

The words are not taken from the rows: a reply is stored as the automation
left it. The import that chooses the boundary keeps them in the store carry
(``reply_words``), for the newest turns before the boundary.

Every case is a pair against the full replay, in the three frontier states,
Claude and Codex; ``World.imported`` compares every product table before it
says what the candidate decided.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from tests.readmodel import corpus
from tests.readmodel import rm_n14_harness as harness
from tests.readmodel.test_rm_n14_carry import (  # noqa: F401 - frontier_state is a fixture
    FAR, World, _history, _turn, frontier_state)
from tests.readmodel.test_rm_n14_equivalence import _clones

BACKENDS = pytest.mark.parametrize("backend", harness.BACKENDS)
LIVE = {"carry_live_rows": True}
WORDS = {"carry_live_rows": True, "carry_live_rows_by_words": True}
BY_WORDS = pytest.mark.parametrize("by_words", [True, False], ids=["by words", "by the trace alone"])
OLD = "an old reply that nothing else resembles"
REFUSED = ("full_carry_inputs_changed", ["live_row_of_old_reply"])
TAIL = ("tail_applied", [])
COMPACTED = "This session is being continued from a previous conversation that ran out of context."


def _rows(world, where, parameters=()):
    with sqlite3.connect(world.candidate) as stored:
        return stored.execute(
            "SELECT message_id, source_file, trace_id, text FROM messages WHERE " + where,
            parameters).fetchall()


def _kept(world):
    """The words the checkpoint's carry keeps, by trace (None: none kept)."""
    with sqlite3.connect(world.candidate) as stored:
        [(carry,)] = stored.execute("SELECT store_carry_json FROM transcript_import_checkpoints")
    return json.loads(carry).get("reply_words")


def _audited(world):
    """One import whose audit is due: a full import, which moves the boundary
    up to the last user turn."""
    kept = world.faults.get("settings")
    # A full import, said and not left to the default: with
    # ``audit_grown_by_digest``, on in the Hosts' configuration, a due audit
    # of a grown file is a tail and the boundary stays.
    world.faults["settings"] = {**(kept or {}), "audit_every": 1, "audit_grown_by_digest": False}
    try:
        return world.imported()
    finally:
        world.faults["settings"] = kept


def _spanned(tmp_path, backend, settings, **replaced):
    """A turn that began before every old turn, so that the replies before
    the boundary carry its trace."""
    world = World(tmp_path, backend, _history(backend, **replaced), settings=settings)
    world.__enter__()
    world.turn_row("spanning", at=-500)
    return world


# ---- 1 and 2: related words, and other words ------------------------------------------

@pytest.mark.parametrize("words", ["the same words", "words that begin with the old reply's",
                                   "words the old reply begins with", "other words"])
@BY_WORDS
@BACKENDS
def test_a_live_row_is_refused_for_the_words_of_an_old_reply_of_its_turn(
        tmp_path, backend, by_words, words):
    """The harmful twin and its harmless neighbour. With words that are an
    old reply's, begin with them or are what it begins with, a full replay
    removes the leftover live row and a tail would leave it: a full import.
    With other words both leave it: a tail. By the trace alone all four are
    full imports."""
    text = {"the same words": OLD, "words that begin with the old reply's": OLD + ", and more",
            "words the old reply begins with": OLD[:17], "other words": "nothing like it"}[words]
    related = words != "other words"
    world = _spanned(tmp_path, backend, WORDS if by_words else LIVE, answer2=OLD)
    with world:
        ordinal = world.started(6)
        assert _rows(world, "role='assistant' AND text=? AND trace_id='spanning'", (OLD,))
        assert (_kept(world) is not None) is by_words
        world.live("leftover", text, at=FAR, trace="spanning")
        world.grow(_turn(backend, ordinal))
        assert world.imported() == (REFUSED if related or not by_words else TAIL)
        assert bool(_rows(world, "message_id='live-leftover'")) is not related
        world.grow(_turn(backend, ordinal + 1))
        assert world.imported() == TAIL


# ---- 4: no words on either side --------------------------------------------------------

@pytest.mark.parametrize("empty", ["the live row", "the old reply", "both"])
@BACKENDS
def test_a_side_without_words_is_related_to_nothing(tmp_path, backend, empty):
    """A full replay skips a live row that shows nothing, and a reply
    without words is not in its batch. Either way the row stays, and a tail
    leaves it too."""
    old = "" if empty in ("the old reply", "both") else OLD
    showing = "<speak></speak>" if empty in ("the live row", "both") else "nothing like it"
    replaced = {f"answer{number}": old for number in range(6)} if not old else {"answer2": old}
    world = _spanned(tmp_path, backend, WORDS, **replaced)
    with world:
        ordinal = world.started(6)
        world.live("leftover", showing, at=FAR, trace="spanning")
        world.grow(_turn(backend, ordinal))
        assert world.imported() == TAIL
        assert _rows(world, "message_id='live-leftover'")
        world.grow(_turn(backend, ordinal + 1))
        assert world.imported() == TAIL


# ---- 5: the live row's words grow through the beginning of an old reply ----------------

@BACKENDS
def test_a_live_row_that_grows_through_the_beginning_of_an_old_reply(tmp_path, backend):
    """As a reply streams, its row holds a longer and longer beginning. While
    that beginning is also an old reply's, the full replay would take the row
    for that reply's leftover: a full import. From the first character that
    differs on it is a tail."""
    world = _spanned(tmp_path, backend, WORDS, answer2=OLD)
    with world:
        ordinal = world.started(6)
        steps = [("a", True), (OLD[:8], True), (OLD[:8] + "x", False), (OLD[:8] + "x and on", False),
                 (OLD, True), (OLD + " and on", True), ("an older reply, then", False)]
        for step, (showing, related) in enumerate(steps):
            # A full replay removes the related row; the next one is new.
            world.live(f"step{step}", showing, at=FAR + step, trace="spanning")
            world.grow(_turn(backend, ordinal + step))
            assert world.imported() == (REFUSED if related else TAIL), showing
            assert bool(_rows(world, "message_id=?", (f"live-step{step}",))) is not related


# ---- 3: the real shapes -----------------------------------------------------------------

@BY_WORDS
@BACKENDS
def test_a_compaction_inside_a_running_turn_and_the_turn_streams_on(tmp_path, backend, by_words):
    """A turn runs; its context is compacted, which leaves a user record in
    the middle of it; a full import (the audit) puts the boundary there, and
    the turn's earlier replies lie before it. Then the turn streams on:
    every delta of its live row is a full import by the trace alone, and a
    tail by words. A delta that shows the beginning of one of the turn's
    earlier replies is still a full import."""
    with World(tmp_path, backend, _history(backend), settings=WORDS if by_words else LIVE) as world:
        ordinal = world.started(6)
        world.turn_row("running", at=ordinal * 1000 - 500)
        world.grow(_turn(backend, ordinal, answer=OLD))
        assert world.imported() == TAIL
        world.grow(_turn(backend, ordinal + 1, request=COMPACTED))
        assert _audited(world) == ("full_audit", [])
        assert _rows(world, "role='assistant' AND text=? AND trace_id='running'", (OLD,))
        if by_words:
            assert OLD in _kept(world)["running"]
        world.live("stream", "The next thing", at=FAR, trace="running")
        world.grow(_turn(backend, ordinal + 2))
        assert world.imported() == (TAIL if by_words else REFUSED)
        for step, showing in enumerate(("The next thing I did", "The next thing I did was this.")):
            world.live_change("stream", "text=?", (showing,))
            world.grow(_turn(backend, ordinal + 3 + step))
            assert world.imported() == (TAIL if by_words else REFUSED)
        assert _rows(world, "message_id='live-stream'")[0][3] == "The next thing I did was this."
        world.live("again", OLD[:12], at=FAR + 9, trace="running")
        world.grow(_turn(backend, ordinal + 5))
        assert world.imported() == REFUSED
        assert not _rows(world, "message_id='live-again'")


@BY_WORDS
@BACKENDS
def test_the_last_reply_of_a_turn_that_is_given_the_next_turns_trace(tmp_path, backend, by_words):
    """A reply that follows no linked prompt is given the trace of the turn
    that was running at its time, and a turn counts as running from two
    seconds before it began. So the last reply of one turn, written within
    those two seconds, carries the next turn's trace, and lies before the
    boundary at that turn's prompt. Every delta of the next turn was a full
    import; by words it is a tail."""
    with World(tmp_path, backend, _history(backend), settings=WORDS if by_words else LIVE) as world:
        ordinal = world.started(6)
        world.grow(_turn(backend, ordinal, answer=OLD))
        world.turn_row("next", at=ordinal * 1000 + 1500)       # within two seconds after it
        assert world.imported()[0] in ("tail_applied", "full_carry_inputs_changed")
        world.grow(_turn(backend, ordinal + 2))
        assert _audited(world) == ("full_audit", [])
        assert _rows(world, "role='assistant' AND text=? AND trace_id='next'", (OLD,))
        world.live("stream", "Something else entirely", at=FAR, trace="next")
        world.grow(_turn(backend, ordinal + 3))
        assert world.imported() == (TAIL if by_words else REFUSED)
        world.live_change("stream", "text=?", ("Something else entirely, and more",))
        world.grow(_turn(backend, ordinal + 4))
        assert world.imported() == (TAIL if by_words else REFUSED)


# ---- what the carry keeps, and what it does not ------------------------------------------

@BACKENDS
def test_a_frontier_from_before_the_switch_is_refused_until_the_next_full_import(tmp_path, backend):
    """The words are kept by the import that chooses the boundary. A carry
    captured while the switch was off has none: refused, by the trace alone,
    and that full import keeps them."""
    world = _spanned(tmp_path, backend, LIVE, answer2=OLD)
    with world:
        ordinal = world.started(6)
        assert _kept(world) is None
        world.faults["settings"] = WORDS
        world.live("first", "nothing like it", at=FAR, trace="spanning")
        world.grow(_turn(backend, ordinal))
        assert world.imported() == REFUSED
        assert OLD in _kept(world)["spanning"]
        world.live_change("first", "text=?", ("nothing like it, at all",))
        world.grow(_turn(backend, ordinal + 1))
        assert world.imported() == TAIL


@BACKENDS
def test_a_full_import_that_certifies_its_publication_again_keeps_the_words_too(
        tmp_path, backend, monkeypatch):
    """Another writer commits something unrelated while the full import's
    write has given the lock away. The publication is certified a second
    time, from a carry captured afresh: the words are in that one as well."""
    from lib import db, message_writes
    from lib import transcript_incremental as inc

    _reference, candidate = _clones(tmp_path, backend)
    transcript = tmp_path / "synthetic.jsonl"
    transcript.write_bytes(b"".join(_history(backend, answer2=OLD)))
    with sqlite3.connect(candidate) as other:
        other.execute("INSERT INTO turns(agent_id,source,trace_id,started_at) VALUES (?,'pwa','spanning',?)",
                      (corpus.AGENT, harness.BASE_MS - 500))
    plain_yield = message_writes._ImportWriter.yield_writer
    committed = []

    def unrelated_commit(writer):
        plain_yield(writer)
        if not committed:
            with sqlite3.connect(candidate) as other:
                other.execute("INSERT INTO sse_events(ts,type,session,agent_id,payload) VALUES (?,?,?,?,?)",
                              (harness.BASE_MS, "synthetic-unrelated", "other-native", "other-agent", "{}"))
            committed.append(True)

    monkeypatch.setattr(message_writes._ImportWriter, "yield_writer", unrelated_commit)
    report = harness.run_lane(candidate, transcript, backend, "candidate", tick=0,
                              faults={"single_row_batches": True, "settings": WORDS})
    assert committed and report["apply_mode"] == "full"
    db.reset_for_tests(candidate)
    checkpoint = inc.load_checkpoint(db.conn(), corpus.AGENT, corpus.SESSION, str(transcript))
    db.close_local()
    assert checkpoint is not None, "the publication was not certified again"
    assert OLD in checkpoint.store_carry["reply_words"]["spanning"]


@BACKENDS
def test_a_reply_stored_under_other_words_than_it_said(tmp_path, backend):
    """An acknowledgement the automation replaced: the row says 'Heartbeat
    check: no action needed.', the transcript said HEARTBEAT_OK, and that is
    what a full replay compares a live row with. Taken from the rows, the
    words would let this row through and a tail would leave it."""
    world = _spanned(tmp_path, backend, WORDS, answer2="HEARTBEAT_OK")
    with world:
        ordinal = world.started(6)
        assert _rows(world, "role='assistant' AND text='Heartbeat check: no action needed.'")
        assert not _rows(world, "role='assistant' AND text='HEARTBEAT_OK'")
        world.live("leftover", "HEARTBEAT_OK", at=FAR, trace="spanning")
        world.grow(_turn(backend, ordinal))
        assert world.imported() == REFUSED
        assert not _rows(world, "message_id='live-leftover'")
        world.live("notice", "Heartbeat check: no action needed.", at=FAR + 1, trace="spanning")
        world.grow(_turn(backend, ordinal + 1))
        assert world.imported() == REFUSED


@BACKENDS
def test_only_the_newest_turns_before_the_boundary_have_their_words_kept(tmp_path, backend):
    """Three turns' words are kept, the newest before the boundary. A live
    row of an older turn that has replies there is refused as before."""
    with World(tmp_path, backend, _history(backend), settings=WORDS) as world:
        for number in range(5):
            # One turn row a transcript turn: a turn counts as running from
            # two seconds before it began, so this one takes the turn at
            # second ``number`` and none before it.
            world.turn_row(f"turn{number}", at=number * 1000 + 1900,
                           settled=number * 1000 + 2800, outcome="completed")
        ordinal = world.started(6)
        for number in range(5):
            assert _rows(world, "role='assistant' AND text=? AND trace_id=?",
                         (f"synthetic answer {number}", f"turn{number}"))
        kept = _kept(world)
        assert sorted(kept) == ["turn2", "turn3", "turn4"]
        world.live("old", "nothing like it", at=FAR, trace="turn1")
        world.grow(_turn(backend, ordinal))
        assert world.imported() == REFUSED
        world.live("newer", "nothing like it", at=FAR + 1, trace="turn3")
        world.grow(_turn(backend, ordinal + 1))
        assert world.imported() == TAIL


@pytest.mark.parametrize("ended", ["the range is open", "the range is closed"])
@BACKENDS
def test_a_conversation_with_turns_the_stream_owns_keeps_the_full_import(tmp_path, backend, ended):
    """A turn whose text the stream owns has replies before the boundary
    that are stored as no row; the rule for live rows is left out for such a
    conversation, and this one with it."""
    end, status = (None, "pending") if ended == "the range is open" else (9, "success")
    world = _spanned(tmp_path, backend, WORDS, answer2=OLD)
    with world:
        world.write(
            "INSERT INTO agy_turn_authority(agent_id,backend_session_id,trace_id,"
            "assistant_start_ordinal,assistant_end_ordinal,terminal_status,"
            "authoritative_message_id,updated_at) VALUES (?,?,?,?,?,?,NULL,?)",
            (corpus.AGENT, corpus.SESSION, "owned-turn", 2, end, status, harness.BASE_MS))
        ordinal = world.started(6)
        world.live("leftover", "nothing like it", at=FAR, trace="owned-turn")
        world.grow(_turn(backend, ordinal))
        decision, named = world.imported()
        assert decision == "full_carry_inputs_changed" and named


def test_the_switch_is_off_until_switched_on():
    from lib import message_carry, transcript_incremental as inc
    assert inc._DEFAULTS["carry_live_rows_by_words"] is False
    assert message_carry.LIVE_WORDS == "carry_live_rows_by_words" in message_carry.RULES
