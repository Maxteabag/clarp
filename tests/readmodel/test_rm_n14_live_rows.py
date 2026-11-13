"""The rows a reply streams into, and a tail (``carry_live_rows``).

While a reply streams, the runtime rewrites one row in place, four times a
second: the live row of that provider message. Each rewrite changed a row a
frontier was published with, so the import that followed was a full one; on
an active Host by day that was nearly every refusal left in the journal.

``lib.message_carry.LIVE_ROWS`` lets a live row of the session be new,
rewritten or gone, unless a reply stored before the boundary carries its
trace: then the running turn spans the boundary, and a full replay may
remove the row where a tail leaves it. The switch is off by default.

It is a different kind of rule from the other three: it lets a CHANGED old
row through, not only a new one. Every case here is a pair with the
full-replay reference lane, in the three places a frontier can be (held,
taken from the store after a Host start, pushed out of the cache), and the
comparison of every product table comes before any word about the decision.
"""
from __future__ import annotations

import sqlite3

import pytest

from tests.readmodel import corpus
from tests.readmodel import rm_n14_harness as harness
from tests.readmodel.test_rm_n14_carry import (  # noqa: F401 - frontier_state is a fixture
    FAR, OTHER_SESSION, World, _history, _turn, frontier_state)

ON = {"carry_live_rows": True}
BACKENDS = pytest.mark.parametrize("backend", harness.BACKENDS)


def _rows(world, where, parameters=()):
    with sqlite3.connect(world.candidate) as stored:
        return stored.execute(
            f"SELECT message_id, source_file, trace_id, text FROM messages WHERE {where} "
            "ORDER BY message_id", parameters).fetchall()


# ---- the rule, on and off ------------------------------------------------------

@pytest.mark.parametrize("rule_on", [True, False], ids=["rule on", "rule off"])
@BACKENDS
def test_a_reply_that_streams_does_not_cost_a_full_import(tmp_path, backend, rule_on):
    """The row is created, rewritten, settled (the provider message is over,
    the row stays where it is) and retracted, with the transcript growing in
    between. None of it is read by a turn before the boundary: no reply
    there carries the running turn's trace. Off, each step is the full
    import it was."""
    wanted = ("tail_applied", []) if rule_on else ("full_carry_inputs_changed", ["row_live"])
    with World(tmp_path, backend, _history(backend), settings=ON if rule_on else None) as world:
        ordinal = world.started(6)
        world.turn_row("running", at=FAR)
        world.live("reply", "so far", at=FAR + 100, trace="running")
        for step, change in enumerate((None, ("text=?", ("so far, and further",)),
                                       ("text=?", ("so far, and further still",)),
                                       ("kind=NULL", ()))):
            if change:
                world.live_change("reply", *change)
            world.grow(_turn(backend, ordinal + step))
            assert world.imported() == wanted
        for database in (world.reference, world.candidate):       # retracted: empty output
            with sqlite3.connect(database) as other:
                assert other.execute("DELETE FROM messages WHERE message_id='live-reply'").rowcount == 1
        world.grow(_turn(backend, ordinal + 4))
        assert world.imported() == wanted
        world.grow(_turn(backend, ordinal + 5))
        assert world.imported() == ("tail_applied", [])


# ---- 1 and 2: the running turn spans the boundary, or it does not ---------------

@pytest.mark.parametrize("words", ["the same words", "words that begin with the old reply's",
                                   "words the old reply begins with", "other words"])
@BACKENDS
def test_a_live_row_of_a_turn_that_has_replies_before_the_boundary_is_a_full_import(
        tmp_path, backend, words):
    """The harmful twin. The running turn began before the old turns, so the
    replies before the boundary carry its trace. A full replay's batch holds
    them, and it removes a leftover live row of that trace whose words equal
    one of them, begin with it, or are what it begins with; a tail's batch
    holds only the replies from the boundary and leaves the row.

    The rule does not look at the words: with other words too it is a full
    import (which then leaves the row, as a tail would have)."""
    old = "an old reply that nothing else resembles"
    text = {"the same words": old, "words that begin with the old reply's": old + ", and more",
            "words the old reply begins with": old[:17], "other words": "nothing like it"}[words]
    with World(tmp_path, backend, _history(backend, answer2=old), settings=ON) as world:
        world.turn_row("spanning", at=-500)
        ordinal = world.started(6)
        assert _rows(world, "role='assistant' AND text=? AND trace_id='spanning'", (old,))
        world.live("leftover", text, at=FAR, trace="spanning")
        world.grow(_turn(backend, ordinal))
        assert world.imported() == ("full_carry_inputs_changed", ["live_row_of_old_reply"])
        assert bool(_rows(world, "message_id='live-leftover'")) is (words == "other words")
        world.grow(_turn(backend, ordinal + 1))
        assert world.imported() == ("tail_applied", [])


@BACKENDS
def test_the_words_of_an_old_reply_in_a_live_row_of_another_turn_are_harmless(tmp_path, backend):
    """The harmless twin. The same words, but the old replies belong to a
    turn that is over and the live row to the one that runs now: no batch
    names the row's trace, full replay or tail."""
    old = "an old reply that nothing else resembles"
    with World(tmp_path, backend, _history(backend, answer2=old), settings=ON) as world:
        world.turn_row("an-old-turn", at=-500, settled=4500, outcome="completed")
        ordinal = world.started(6)
        assert _rows(world, "role='assistant' AND text=? AND trace_id='an-old-turn'", (old,))
        world.turn_row("running", at=FAR)
        world.live("reply", old, at=FAR + 100, trace="running")
        world.grow(_turn(backend, ordinal))
        assert world.imported() == ("tail_applied", [])
        assert _rows(world, "message_id='live-reply'")


@BACKENDS
def test_it_is_the_old_replies_that_carry_the_trace_not_the_old_prompts(tmp_path, backend):
    """An old turn whose prompts were /send rows: they have no transcript
    rows, so before the boundary only that turn's replies carry its trace. A
    live row of that trace with an old reply's words is removed by a full
    replay all the same."""
    old = "an old reply that nothing else resembles"
    with World(tmp_path, backend, _history(backend, answer2=old), settings=ON) as world:
        for number in (0, 1, 2):          # one message and two steered into its turn
            world.send(f"asked-{number}", f"synthetic request {number}", at=number * 1000 - 100,
                       trace="an-old-turn")
        world.turn_row("an-old-turn", at=-100, settled=2500, outcome="completed")
        world.turn_row("the-next-turn", at=4100, settled=5500, outcome="completed")
        ordinal = world.started(6)
        carrying = _rows(world, "trace_id='an-old-turn' AND source_file=?", (str(world.transcript),))
        assert carrying and old in {row[3] for row in carrying}
        with sqlite3.connect(world.candidate) as stored:
            assert stored.execute(
                "SELECT count(*) FROM messages WHERE trace_id='an-old-turn' AND role='user' "
                "AND source_file=?", (str(world.transcript),)).fetchone()[0] == 0
        world.live("left", old, at=2400, trace="an-old-turn", settled=True)
        world.grow(_turn(backend, ordinal))
        assert world.imported() == ("full_carry_inputs_changed", ["live_row_of_old_reply"])
        assert not _rows(world, "message_id='live-left'")


# ---- 3: a live row that is not the running turn's ----------------------------------

@BACKENDS
def test_a_live_row_left_by_an_earlier_turn_is_not_asked_while_it_stays_as_it_was(tmp_path, backend):
    """A turn that was interrupted left its settled row behind; its replies
    lie before the boundary. The frontier was published with that row there,
    so it is not asked again, and the reply that streams now is a tail.
    Rewritten with the words of one of its turn's old replies it is asked,
    and that is a full import, which removes it."""
    old = "an old reply that nothing else resembles"
    with World(tmp_path, backend, _history(backend, answer2=old), settings=ON) as world:
        world.turn_row("interrupted", at=-500, settled=4500, outcome="interrupted")
        world.live("left", "words of its own", at=4400, trace="interrupted", settled=True)
        ordinal = world.started(6)
        world.turn_row("running", at=FAR)
        world.live("reply", "streaming", at=FAR + 100, trace="running")
        world.grow(_turn(backend, ordinal))
        assert world.imported() == ("tail_applied", [])
        world.live_change("reply", "text=?", ("streaming on",))
        world.grow(_turn(backend, ordinal + 1))
        assert world.imported() == ("tail_applied", [])
        assert _rows(world, "message_id='live-left'")
        world.live_change("left", "text=?", (old,))
        world.grow(_turn(backend, ordinal + 2))
        assert world.imported() == ("full_carry_inputs_changed", ["live_row_of_old_reply"])
        assert not _rows(world, "message_id='live-left'")
        world.grow(_turn(backend, ordinal + 3))
        assert world.imported() == ("tail_applied", [])


# ---- 4: the row's whole life ----------------------------------------------------

@BACKENDS
def test_a_live_row_from_its_first_words_to_the_transcripts_reply(tmp_path, backend):
    """A message arrives and its turn runs; the reply streams into a live
    row, which is rewritten, settled, and then taken over in place by the
    reply the provider writes to the transcript (the row keeps its id). Every
    import on the way is a tail."""
    with World(tmp_path, backend, _history(backend), settings=ON) as world:
        ordinal = world.started(6)
        sent = ordinal * 1000 - 500
        world.send("own", f"synthetic request {ordinal}", at=sent, trace="own-turn")
        world.turn_row("own-turn", at=sent)
        answer = "the reply, said in full"
        records = _turn(backend, ordinal, answer=answer).splitlines(keepends=True)
        world.grow(b"".join(records[:-1]))                  # the prompt and a step; no answer yet
        world.live("answer", "the reply", at=sent + 600, trace="own-turn")
        assert world.imported() == ("tail_applied", [])
        for text in ("the reply, said", answer):
            world.live_change("answer", "text=?", (text,))
            assert world.imported() == ("tail_applied", [])     # the file stands still
        world.live_change("answer", "kind=NULL")
        assert world.imported() == ("tail_applied", [])
        assert _rows(world, "message_id='live-answer'")[0][1] == "live:own-turn"
        world.grow(records[-1])                             # the provider writes the answer
        assert world.imported() == ("tail_applied", [])
        [(_, source, trace, text)] = _rows(world, "message_id='live-answer'")
        assert (source, trace, text) == (str(world.transcript), "own-turn", answer)
        world.grow(_turn(backend, ordinal + 1))
        assert world.imported() == ("tail_applied", [])


@BACKENDS
def test_a_reply_that_took_over_its_live_row_is_parked_when_its_message_arrives(
        tmp_path, backend):
    """The parking, the one write to a message row that takes no new
    revision. A turn typed in a terminal streams and is imported; its reply
    takes over the live row under the running turn's trace. Then the /send
    row for that turn arrives, with a turn of its own. The next tail links
    the turn, finds the reply's slot held by a row of another trace and
    parks it; the reply is stored anew under the message's trace and the
    parked row goes back to a finalized row of its own trace, which the
    import after that removes as the reply's twin. A full replay does each
    of those steps too."""
    with World(tmp_path, backend, _history(backend), settings=ON) as world:
        ordinal = world.started(6)
        at = ordinal * 1000
        world.turn_row("running", at=at - 800)
        world.live("answer", f"synthetic answer {ordinal}", at=at + 100, trace="running")
        world.grow(_turn(backend, ordinal))
        assert world.imported() == ("tail_applied", [])
        [(_, source, trace, _text)] = _rows(world, "message_id='live-answer'")
        assert (source, trace) == (str(world.transcript), "running")
        world.send("late", f"synthetic request {ordinal}", at=at - 500, trace="late-turn")
        world.turn_row("late-turn", at=FAR, settled=FAR + 900, outcome="completed")
        world.grow(_turn(backend, ordinal + 1))
        assert world.imported() == ("tail_applied", [])
        assert world.report["parked"] == 1
        said = _rows(world, "role='assistant' AND text=?", (f"synthetic answer {ordinal}",))
        assert sorted((source, trace) for _id, source, trace, _text in said) == sorted(
            [("final:running", "running"), (str(world.transcript), "late-turn")])
        world.grow(_turn(backend, ordinal + 2))
        assert world.imported() == ("tail_applied", [])
        [(_, source, trace, _text)] = _rows(world, "role='assistant' AND text=?",
                                            (f"synthetic answer {ordinal}",))
        assert (source, trace) == (str(world.transcript), "late-turn")


# ---- 5: several live rows ------------------------------------------------------------

@BACKENDS
def test_several_live_rows_of_one_turn_and_one_of_another_session_with_its_trace(tmp_path, backend):
    """A turn streams one row per provider message, and a row of another
    session of the agent can carry the same trace. Each row of this session
    is asked by itself; the other session's is not read at all."""
    with World(tmp_path, backend, _history(backend), settings=ON) as world:
        ordinal = world.started(6)
        world.turn_row("running", at=FAR)
        world.live("first", "the first message", at=FAR + 100, trace="running", settled=True)
        world.live("second", "the second", at=FAR + 200, trace="running")
        world.live("elsewhere", "synthetic answer 2", at=FAR + 300, trace="running",
                   session=OTHER_SESSION)
        world.grow(_turn(backend, ordinal))
        assert world.imported() == ("tail_applied", [])
        world.live_change("second", "text=?", ("the second, longer",))
        world.grow(_turn(backend, ordinal + 1))
        assert world.imported() == ("tail_applied", [])
        assert len(_rows(world, "source_file LIKE 'live:%'")) == 3


@BACKENDS
def test_one_of_several_live_rows_with_an_old_replys_trace_is_enough_for_a_full_import(
        tmp_path, backend):
    """The twin: every live row is asked, not the first or the newest."""
    old = "an old reply that nothing else resembles"
    with World(tmp_path, backend, _history(backend, answer2=old), settings=ON) as world:
        world.turn_row("an-old-turn", at=-500, settled=4500, outcome="completed")
        ordinal = world.started(6)
        world.turn_row("running", at=FAR)
        world.live("new", "the running turn's", at=FAR + 100, trace="running")
        world.live("old", old, at=4400, trace="an-old-turn", settled=True)
        world.live("newer", "and another of the running turn", at=FAR + 200, trace="running")
        world.grow(_turn(backend, ordinal))
        assert world.imported() == ("full_carry_inputs_changed", ["live_row_of_old_reply"])
        assert [row[0] for row in _rows(world, "source_file LIKE 'live:%'")] == ["live-new", "live-newer"]


@BACKENDS
def test_a_finalized_row_is_not_a_live_row(tmp_path, backend):
    """The rule is for the rows a reply streams into. A finalized row with
    an old reply's words is removed by a full replay as that reply's twin,
    whatever its trace; with the rule on it is the full import it was."""
    with World(tmp_path, backend, _history(backend), settings=ON) as world:
        ordinal = world.started(6)
        world.message("final-twin", source_file="final:twin", role="assistant",
                      text="synthetic answer 2", at=2000, trace="twin")
        world.grow(_turn(backend, ordinal))
        assert world.imported() == ("full_carry_inputs_changed", ["row_final"])
        assert not _rows(world, "message_id='final-twin'")


# ---- 6: a delta while the import yields the writer --------------------------------------

@pytest.mark.parametrize("yield_at", [1, 2])
@BACKENDS
def test_a_delta_that_lands_while_the_tail_yields_refuses_its_publication(tmp_path, backend, yield_at):
    """The rule lets the tail run; the publication fence is what it was. A
    delta committed while the tail has given the writer away is a foreign
    commit: the tail's rows stand, its frontier is not published, and the
    next import is a full one."""
    with World(tmp_path, backend, _history(backend), settings=ON) as world:
        ordinal = world.started(6)
        world.turn_row("running", at=FAR)
        world.live("reply", "so far", at=FAR + 100, trace="running")
        world.grow(_turn(backend, ordinal))
        assert world.imported() == ("tail_applied", [])
        with sqlite3.connect(world.candidate) as stored:
            boundary = stored.execute(
                "SELECT boundary_seq FROM transcript_import_checkpoints").fetchone()[0]
        world.grow(_turn(backend, ordinal + 1))
        delta = {"live_on_yield": "live-reply", "yield_at": yield_at}
        aligned = world.faults
        world.faults = {**aligned, **delta}
        try:
            # The reference replays from the first turn: the same delta, before
            # the same row of the transcript.
            world.full.tick(world.tick, faults={**world.faults,
                                                "yield_before_seq": boundary + yield_at - 1})
            report = world.actual.tick(world.tick, faults=(
                {**world.faults, "evict": True} if world.state == "evicted" else world.faults))
            world.tick += 1
        finally:
            world.faults = aligned
        harness.assert_product_equal(world.reference, world.candidate)
        codes = [(e["event"], e["code"]) for e in report["tail_events"]]
        assert ("transcriptTailRetention", "checkpoint_foreign_commit_yield") in codes
        assert ("transcriptTailDecision", "tail_applied") in codes
        assert _rows(world, "message_id='live-reply'")[0][3] == "so far and more"
        # The process holds no frontier now. One that looks in the store (a
        # new process, or after an eviction) finds the frontier of before the
        # refused tail, whose record does not know that tail's rows.
        world.published = False
        world.grow(_turn(backend, ordinal + 2))
        assert world.imported() == (
            ("full_no_frontier", []) if world.state == "held" else
            ("full_carry_inputs_changed", ["row_transcript"]))
        world.published = True
        world.grow(_turn(backend, ordinal + 3))
        assert world.imported() == ("tail_applied", [])


# ---- what the rule leaves: the end of the turn ------------------------------------

def _settle(world, trace, at):
    world.write("UPDATE turns SET ended_at=?, settled_at=?, outcome='completed' WHERE trace_id=?",
                (harness.BASE_MS + at, harness.BASE_MS + at, trace))


@pytest.mark.parametrize("live_row", ["as it was", "rewritten"])
@BACKENDS
def test_the_end_of_the_streamed_turn_is_still_a_full_import(tmp_path, backend, live_row):
    """The rule frees the deltas, not the turn's end. Once a frontier was
    published with the running turn's live row, the turn's trace is on an
    old row, and its turn row settling is what the rule for later turns
    refuses (turn_of_old_row), with the live row as it was or rewritten in
    the same moment. A streamed turn costs one full import, not one a delta."""
    with World(tmp_path, backend, _history(backend), settings=ON) as world:
        ordinal = world.started(6)
        world.turn_row("running", at=FAR)
        world.live("reply", "so far", at=FAR + 100, trace="running")
        world.grow(_turn(backend, ordinal))
        assert world.imported() == ("tail_applied", [])
        _settle(world, "running", FAR + 900)
        if live_row == "rewritten":
            world.live_change("reply", "text=?", ("so far, and to the end",))
        world.grow(_turn(backend, ordinal + 1))
        assert world.imported() == ("full_carry_inputs_changed", ["turn_of_old_row"])
        world.grow(_turn(backend, ordinal + 2))
        assert world.imported() == ("tail_applied", [])


@BACKENDS
def test_a_rewritten_live_row_whose_trace_no_other_old_row_carries_and_a_new_turn_row(
        tmp_path, backend):
    """Conservative, and pinned so that it is known. The traces whose turns
    are asked about are those of the rows at or below the mark. A live row
    that was rewritten is above it, so when it was the only old row with its
    trace the set of traces differs from the recorded one, and ANY change
    among the turn rows is then refused, here another turn that only
    started. With the live row as it was the same turn row is a tail. On a
    Host the running turn's trace is on its /send row as well, and then the
    set stands."""
    with World(tmp_path, backend, _history(backend), settings=ON) as world:
        ordinal = world.started(6)
        world.turn_row("running", at=FAR)
        world.live("reply", "so far", at=FAR + 100, trace="running")
        world.grow(_turn(backend, ordinal))
        assert world.imported() == ("tail_applied", [])
        world.turn_row("another", at=FAR + 2000)
        world.grow(_turn(backend, ordinal + 1))
        assert world.imported() == ("tail_applied", [])
        world.turn_row("a-third", at=FAR + 3000)
        world.live_change("reply", "text=?", ("so far, and further",))
        world.grow(_turn(backend, ordinal + 2))
        assert world.imported() == ("full_carry_inputs_changed", ["turn_of_old_row"])
        world.grow(_turn(backend, ordinal + 3))
        assert world.imported() == ("tail_applied", [])


@BACKENDS
def test_the_same_with_the_turns_message_among_the_old_rows_is_a_tail(tmp_path, backend):
    """As on a Host: the running turn's /send row carries its trace, so the
    trace stays among the old rows' when the live row is rewritten, and a
    turn row that only started is let through."""
    with World(tmp_path, backend, _history(backend), settings=ON) as world:
        ordinal = world.started(6)
        world.send("now", "a message nothing before it said", at=FAR - 50, trace="running")
        world.turn_row("running", at=FAR)
        world.live("reply", "so far", at=FAR + 100, trace="running")
        world.grow(_turn(backend, ordinal))
        assert world.imported() == ("tail_applied", [])
        world.turn_row("another", at=FAR + 3000)
        world.live_change("reply", "text=?", ("so far, and further",))
        world.grow(_turn(backend, ordinal + 1))
        assert world.imported() == ("tail_applied", [])
