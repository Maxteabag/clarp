"""A turn's end, and a tail (``carry_settled_turns``).

When a turn settles two things change. Its turn row: ``settled_at`` and
``outcome`` are set. And message rows: the turn's summary is stamped on its
last row, and an interrupted turn's unfinished tools read ``interrupted``
(``message_writes.stamp_turn``, called by the turn's own settling). Either
was a full import: the turn row is the turn of an old row, and a stamped row
is a changed old row. So a streamed turn cost one full import, at its end.

What a turn before the boundary reads of a turn row is which turn was
running at its time. A settle moves that turn's reach from six hours after
its start to two minutes after its end, and that is all it changes for an
old turn. ``lib.message_carry.SETTLED_TURNS`` lets the settle through when
every old turn lies within both reaches (or the turn began after them all),
and lets the stamped rows through where a tail writes them again anyway:
transcript rows from the boundary on that differ in nothing but the stamps.
An interrupted turn with a row before the boundary stays a full import: its
unfinished tools there are written by a full import only.

Every case is a pair with the full-replay lane, in the three places a
frontier can be, and every product table is compared before a word is said
about the decision.
"""
from __future__ import annotations

import json
import sqlite3
from unittest.mock import patch

import pytest

from tests.readmodel import corpus
from tests.readmodel import rm_n14_harness as harness
from tests.readmodel.test_rm_n14_carry import (  # noqa: F401 - frontier_state is a fixture
    World, _history, _turn, frontier_state)

ON = {"carry_settled_turns": True}
WITH_LIVE = {"carry_settled_turns": True, "carry_live_rows": True}
ON_OFF = pytest.mark.parametrize("rules", [ON, None], ids=["rule on", "rule off"])
BACKENDS = pytest.mark.parametrize("backend", harness.BACKENDS)
# More records of a turn without a new prompt: the lane's Codex records of
# that kind name a turn that began before the boundary, which is a full
# import of its own (full_codex_turn_before_boundary).
CLAUDE = pytest.mark.parametrize("backend", ["claude"])
T = 20_000                      # the second a running turn's records are written at
HOUR = 3_600_000


def _audited(world):
    """One import whose audit is due: a full import, which moves the boundary
    up to the last turn."""
    kept = world.faults.get("settings")
    world.faults["settings"] = {**(kept or {}), "audit_every": 1}
    try:
        return world.imported()
    finally:
        if kept is None:
            del world.faults["settings"]
        else:
            world.faults["settings"] = kept


def _boundary(world):
    with sqlite3.connect(world.candidate) as stored:
        return stored.execute("SELECT boundary_seq FROM transcript_import_checkpoints").fetchone()[0]


def _seqs(world, trace):
    with sqlite3.connect(world.candidate) as stored:
        return [seq for (seq,) in stored.execute(
            "SELECT seq FROM messages WHERE trace_id=? AND source_file=? ORDER BY seq",
            (trace, str(world.transcript)))]


def _begin(world, trace, second, *, send=True):
    """A message is sent, its turn starts, and the provider writes the turn
    (prompt, a step with a tool, the answer) at ``second``."""
    if send:
        world.send(trace, f"synthetic request {second}", at=second * 1000 - 100, trace=trace)
    world.turn_row(trace, at=second * 1000 - 50)
    world.grow(_turn(world.backend, second))


def _stamp(world, trace):
    """What a settle does right after it wrote the turn row
    (turn_lifecycle._after_settle): message_writes.restamp_turn."""
    from lib import message_writes
    for database in (world.reference, world.candidate):
        other = sqlite3.connect(database)
        other.row_factory = sqlite3.Row
        try:
            with patch.object(message_writes, "now_ms", lambda: harness.BASE_MS + 1):
                message_writes.stamp_turn(other, corpus.AGENT, trace, world.next_revision)
            other.commit()
        finally:
            other.close()


def _settle(world, trace, at, *, outcome="completed", stamp=True):
    world.write("UPDATE turns SET ended_at=?, settled_at=?, outcome=? WHERE trace_id=?",
                (harness.BASE_MS + at, harness.BASE_MS + at, outcome, trace))
    if stamp:
        _stamp(world, trace)


def _summaries(world, database=None):
    """{trace: [message id of each row that carries a summary]}."""
    found = {}
    with sqlite3.connect(database or world.candidate) as stored:
        for message_id, trace, summary in stored.execute(
                "SELECT message_id, trace_id, turn_json FROM messages WHERE turn_json IS NOT NULL "
                "ORDER BY seq"):
            assert json.loads(summary)["turn_id"] == trace
            found.setdefault(trace, []).append(message_id)
    return found


def _last_row(world, trace):
    with sqlite3.connect(world.candidate) as stored:
        return stored.execute(
            "SELECT message_id, source_file FROM messages WHERE trace_id=? "
            "ORDER BY COALESCE(timestamp, ''), seq", (trace,)).fetchall()[-1]


# ---- the turn's end ------------------------------------------------------------------

@ON_OFF
@BACKENDS
def test_a_turn_that_settled_since_the_frontier_does_not_cost_a_full_import(
        tmp_path, backend, rules):
    """The ordinary end of a turn. Its rows were imported by a tail while it
    ran; it settles, and its summary is stamped on its last row; the next
    message comes. The turn began after every turn before the boundary and
    its rows lie from the boundary on, so nothing an old turn reads moved."""
    with World(tmp_path, backend, _history(backend), settings=rules) as world:
        world.started(6)
        _begin(world, "a-turn", T)
        assert world.imported() == ("tail_applied", [])
        _settle(world, "a-turn", T * 1000 + 500)
        answer, source = _last_row(world, "a-turn")
        assert source == str(world.transcript) and _summaries(world) == {"a-turn": [answer]}
        _begin(world, "the-next", T + 1)
        assert world.imported() == (("tail_applied", []) if rules else
                                    ("full_carry_inputs_changed", ["row_transcript"]))
        assert _summaries(world) == {"a-turn": [answer]}
        _settle(world, "the-next", (T + 1) * 1000 + 500)
        _begin(world, "a-third", T + 2)
        # Without the rule the import before this one was full and moved the
        # boundary up: the turn that settled now began among the old turns.
        assert world.imported() == (("tail_applied", []) if rules else (
            "full_carry_inputs_changed", ["row_transcript", "turn_before_boundary"]))
        assert set(_summaries(world)) == {"a-turn", "the-next"}


@ON_OFF
@CLAUDE
def test_a_turn_that_settles_before_its_last_rows_are_imported(tmp_path, backend, rules):
    """The settle comes first: the summary is stamped on the last row the
    store holds, a step. Then the transcript's last records arrive, within
    the settled turn's reach, and are the turn's too. The tail imports them
    and stamps the turn, which moves the summary to the row that is last
    now, as the full replay does."""
    with World(tmp_path, backend, _history(backend), settings=rules) as world:
        world.started(6)
        _begin(world, "a-turn", T)
        assert world.imported() == ("tail_applied", [])
        _settle(world, "a-turn", T * 1000 + 500)
        before, _source = _last_row(world, "a-turn")
        world.grow(harness.append_bytes(backend, T + 60, long_turn=True))       # no new prompt
        assert world.imported() == (("tail_applied", []) if rules else
                                    ("full_carry_inputs_changed", ["row_transcript"]))
        after, _source = _last_row(world, "a-turn")
        assert after != before and _summaries(world) == {"a-turn": [after]}


def _unfinished(world, trace, database=None):
    """The status of every tool on the turn's rows."""
    with sqlite3.connect(database or world.candidate) as stored:
        return [tool.get("status") for (tools,) in stored.execute(
            "SELECT tools_json FROM messages WHERE trace_id=? ORDER BY seq", (trace,))
            for tool in json.loads(tools)]


def _begin_unfinished(world, trace, second):
    """A turn that has asked for a tool and has no result yet."""
    world.send(trace, f"synthetic request {second}", at=second * 1000 - 100, trace=trace)
    world.turn_row(trace, at=second * 1000 - 50)
    lines = _turn(world.backend, second).splitlines(keepends=True)
    world.grow(b"".join(lines[:2 if world.backend == "claude" else 4]))


@ON_OFF
@pytest.mark.parametrize("stamped", [True, False], ids=["stamped by the settle", "stamp failed"])
@BACKENDS
def test_an_interrupted_turn_whose_rows_are_from_the_boundary_on(tmp_path, backend, rules, stamped):
    """The other thing a settle stamps: the tools an interrupted turn left
    running. The rows are from the boundary on: the tail writes them again
    from the transcript and settles them against the turn's outcome, as a
    full import does, whether the settle's own stamp ran or not."""
    with World(tmp_path, backend, _history(backend), settings=rules) as world:
        world.started(6)
        _begin_unfinished(world, "a-turn", T)
        assert world.imported() == ("tail_applied", [])
        [status] = _unfinished(world, "a-turn")
        assert status != "interrupted"
        _settle(world, "a-turn", T * 1000 + 500, outcome="interrupted", stamp=stamped)
        _begin(world, "the-next", T + 1)
        decision, words = world.imported()
        if rules:
            assert (decision, words) == ("tail_applied", [])
        else:
            assert decision == "full_carry_inputs_changed"
            assert words == (["row_transcript"] if stamped else ["turn_of_old_row"])
        assert _unfinished(world, "a-turn") == ["interrupted"]


@BACKENDS
def test_an_interrupted_turn_with_a_row_before_the_boundary_is_a_full_import(tmp_path, backend):
    """The harmful twin. A full import has moved the boundary past the
    turn's rows. The turn is interrupted and its own stamp fails, so its
    rows are as they were and only the turn row changed. A full replay
    writes the row before the boundary again, its tool now 'interrupted',
    and stamps the turn; a tail would leave both."""
    with World(tmp_path, backend, _history(backend), settings=ON) as world:
        world.started(6)
        _begin_unfinished(world, "a-turn", T)
        assert world.imported() == ("tail_applied", [])
        _begin(world, "the-next", T + 1)
        assert _audited(world) == ("full_audit", [])
        assert max(_seqs(world, "a-turn")) < _boundary(world)
        _settle(world, "a-turn", (T + 1) * 1000 + 300, outcome="interrupted", stamp=False)
        assert _unfinished(world, "a-turn") != ["interrupted"]
        world.grow(_turn(backend, T + 2))
        assert world.imported() == ("full_carry_inputs_changed", ["turn_of_old_row"])
        assert _unfinished(world, "a-turn") == ["interrupted"]
        assert "a-turn" in _summaries(world)


@CLAUDE
def test_a_turn_whose_only_row_is_the_row_its_settling_stamped(tmp_path, backend):
    """The turn has no /send row and no prompt in the transcript, and one
    reply: the only row with its trace, and the settle stamps it. The
    stamped row is still one of the rows the frontier was published with:
    the turn is asked about as the turn of an old row, and found to be the
    recorded one."""
    with World(tmp_path, backend, _history(backend), settings=ON) as world:
        world.started(6)
        world.turn_row("a-turn", at=T * 1000 - 50)
        world.grow(harness.append_bytes(backend, T, long_turn=True))
        assert world.imported() == ("tail_applied", [])
        assert len(_seqs(world, "a-turn")) == 1
        _settle(world, "a-turn", T * 1000 + 500)
        assert len(_summaries(world)["a-turn"]) == 1
        world.grow(_turn(backend, T + 600))                   # past the settled turn's reach
        assert world.imported() == ("tail_applied", [])
        assert len(_seqs(world, "a-turn")) == 1 and len(_summaries(world)["a-turn"]) == 1


@BACKENDS
def test_a_settle_whose_own_stamp_failed_leaves_no_summary_in_either_import(tmp_path, backend):
    """An import stamps a turn's summary only when it changed a row of the
    turn, or the turn is the newest. A completed turn whose stamp failed has
    neither: a full replay leaves it without its summary, and so does the
    tail. (The lane found this: a tail that stamped it differed.)"""
    with World(tmp_path, backend, _history(backend), settings=ON) as world:
        world.started(6)
        _begin(world, "a-turn", T)
        assert world.imported() == ("tail_applied", [])
        _begin(world, "the-next", T + 1)
        assert world.imported() == ("tail_applied", [])
        _settle(world, "a-turn", (T + 1) * 1000 + 300, stamp=False)
        world.grow(_turn(backend, T + 2))
        assert world.imported() == ("tail_applied", [])
        assert "a-turn" not in _summaries(world)
        assert "a-turn" not in _summaries(world, world.reference)


@BACKENDS
def test_a_turn_that_began_within_two_seconds_of_the_last_old_turn(tmp_path, backend):
    """After a full import the boundary is at the last turn, and a turn that
    starts right after it began among the old turns as the turn index sees
    it (two seconds of slack). Its start is a full import, as before: it
    could hold the last old rows. Its end is not: it began before them and
    ends after them, so they are within its reach either way."""
    with World(tmp_path, backend, _history(backend), settings=ON) as world:
        ordinal = world.started(6)
        world.grow(_turn(backend, ordinal))
        assert _audited(world) == ("full_audit", [])
        with sqlite3.connect(world.candidate) as stored:
            last_old = json.loads(stored.execute(
                "SELECT store_carry_json FROM transcript_import_checkpoints").fetchone()[0]
            )["prefix_latest_ms"]
        _begin(world, "a-turn", ordinal + 1, send=False)
        world.write("UPDATE turns SET started_at=? WHERE trace_id='a-turn'", (last_old + 1500,))
        assert world.imported() == ("full_carry_inputs_changed", ["turn_before_boundary"])
        _settle(world, "a-turn", (ordinal + 1) * 1000 + 500)
        world.grow(_turn(backend, ordinal + 2))
        assert world.imported() == ("tail_applied", [])


# ---- the reach ---------------------------------------------------------------------

SPREAD = (0, 1, 2, 300, 301, 302)       # the old turns' seconds: five minutes apart


def _spread(backend):
    return [_turn(backend, second) for second in SPREAD]


def _traces(world, database=None):
    with sqlite3.connect(database or world.candidate) as stored:
        return dict(stored.execute(
            "SELECT seq, trace_id FROM messages WHERE source_file=? ORDER BY seq",
            (str(world.transcript),)))


@pytest.mark.parametrize("settled,wanted", [
    (310_000, ("tail_applied", [])),
    (100_000, ("full_carry_inputs_changed", ["turn_settled_out_of_reach"]))],
    ids=["after the old turns", "two minutes and more before the last old turns"])
@BACKENDS
def test_a_turn_that_holds_old_rows_by_time_and_settles(tmp_path, backend, settled, wanted):
    """The turn began before every turn in the transcript and never settled,
    so every reply was given it by time (six hours of reach). It settles.
    Settled after the old turns, they are all within two minutes of its end:
    nothing moves. Settled at 100 s, the turns at 300 s and after are past
    its reach: a full replay no longer finds it running at their time."""
    with World(tmp_path, backend, _spread(backend), settings=ON) as world:
        world.turn_row("long-running", at=-500)
        world.started(303)
        held = _traces(world)
        assert set(held.values()) == {"long-running"} and len(held) > 12
        _settle(world, "long-running", settled)
        world.grow(_turn(backend, 304))
        assert world.imported() == wanted
        # A trace once on a row is not taken off it by an import (it is kept
        # when the new answer is none), so this direction shows in no row
        # here; the other direction does, in the next test.
        now = _traces(world)
        assert {seq: now[seq] for seq in held} == held
        world.grow(_turn(backend, 305))
        assert world.imported() == ("tail_applied", [])


@BACKENDS
def test_a_turn_too_old_to_hold_the_rows_gains_them_when_it_settles_late(tmp_path, backend):
    """The other direction. The turn began seven hours before the old turns:
    unsettled, its reach ended an hour before them and it held none. Settled
    after them, its reach is two minutes past its end and covers them all: a
    full replay gives it every reply. A tail would not."""
    with World(tmp_path, backend, _spread(backend), settings=ON) as world:
        world.turn_row("ancient", at=-7 * HOUR)
        world.started(303)
        held = _traces(world)
        assert not any(held.values())
        _settle(world, "ancient", 310_000)
        world.grow(_turn(backend, 304))
        assert world.imported() == ("full_carry_inputs_changed", ["turn_settled_out_of_reach"])
        assert set(_traces(world).values()) == {"ancient"}
        world.grow(_turn(backend, 305))
        assert world.imported() == ("tail_applied", [])


@BACKENDS
def test_a_turn_that_had_settled_and_is_settled_again_later_is_a_full_import(tmp_path, backend):
    """Not a settle: the turn row was settled when the frontier was
    published, at 100 s, and held only the first old turns. Its end is moved
    to 310 s. The rule knows a settle by the row having been unsettled; this
    one was not, so what it was before is not the six hours the reach check
    assumes, and it is refused as any changed turn row is. A full replay
    gives the turn the late rows."""
    with World(tmp_path, backend, _spread(backend), settings=ON) as world:
        world.turn_row("early-end", at=-500, settled=100_000, outcome="completed")
        world.started(303)
        held = _traces(world)
        assert held[max(held)] in (None, "") and "early-end" in held.values()
        world.write("UPDATE turns SET settled_at=?, ended_at=? WHERE trace_id='early-end'",
                    (harness.BASE_MS + 310_000, harness.BASE_MS + 310_000))
        world.grow(_turn(backend, 304))
        decision, words = world.imported()
        assert decision == "full_carry_inputs_changed"
        assert "turn_before_boundary" in words and "turn_settled_out_of_reach" not in words
        assert set(_traces(world).values()) == {"early-end"}


# ---- the rows a settle stamps ---------------------------------------------------------

@BACKENDS
def test_a_row_from_the_boundary_on_that_changed_in_more_than_its_stamps(tmp_path, backend):
    """The rule lets the stamped columns of those rows differ and nothing
    else of them: an old reply looks at later rows of its trace by id, slot
    and words when its own slot moved (message_import_rows._shifted_twin)."""
    with World(tmp_path, backend, _history(backend), settings=ON) as world:
        world.started(6)
        _begin(world, "a-turn", T)
        assert world.imported() == ("tail_applied", [])
        _settle(world, "a-turn", T * 1000 + 500)
        answer, _source = _last_row(world, "a-turn")
        world.write("UPDATE messages SET text='other words' WHERE message_id=?", (answer,))
        _begin(world, "the-next", T + 1)
        assert world.imported() == ("full_carry_inputs_changed", ["row_transcript"])
        world.grow(_turn(backend, T + 2))
        assert world.imported() == ("tail_applied", [])


@BACKENDS
def test_a_stamp_on_a_row_before_the_boundary_is_a_full_import(tmp_path, backend):
    """A full import has moved the boundary past the turn's rows, and then
    the turn settles: the settle stamps its last row, which a tail does not
    write again. Refused, as any changed old row: the rule lets a stamp
    through only where the tail rewrites the row."""
    with World(tmp_path, backend, _history(backend), settings=ON) as world:
        world.started(6)
        _begin(world, "a-turn", T)
        assert world.imported() == ("tail_applied", [])
        _begin(world, "the-next", T + 1)
        assert _audited(world) == ("full_audit", [])
        assert max(_seqs(world, "a-turn")) < _boundary(world)
        _settle(world, "a-turn", (T + 1) * 1000 + 300)
        world.grow(_turn(backend, T + 2))
        assert world.imported() == ("full_carry_inputs_changed", ["row_transcript"])
        world.grow(_turn(backend, T + 3))
        assert world.imported() == ("tail_applied", [])


@BACKENDS
def test_a_turn_with_no_reply_stamps_its_message_and_that_is_a_full_import(tmp_path, backend):
    """A turn that failed before it wrote anything: its only row is its
    /send row, and the summary lands there. A /send row is not a row a tail
    writes again."""
    with World(tmp_path, backend, _history(backend), settings=ON) as world:
        world.started(6)
        world.send("unanswered", "words nobody said before", at=T * 1000, trace="unanswered")
        world.turn_row("unanswered", at=T * 1000 + 50)
        world.grow(_turn(backend, 7))
        assert world.imported() == ("tail_applied", [])
        _settle(world, "unanswered", T * 1000 + 900, outcome="failed")
        assert _summaries(world) == {"unanswered": ["u-unanswered"]}
        world.grow(_turn(backend, 8))
        assert world.imported() == ("full_carry_inputs_changed", ["row_send"])
        world.grow(_turn(backend, 9))
        assert world.imported() == ("tail_applied", [])


@pytest.mark.parametrize("rules", [WITH_LIVE, ON], ids=["with carry_live_rows", "without it"])
@BACKENDS
def test_a_summary_that_lands_on_the_live_row_of_the_last_message(tmp_path, backend, rules):
    """As on a Host: the last message is still a live row when the turn
    settles, so the summary is stamped there. A live row is let through by
    its own rule; without that rule it is a changed old row."""
    refused = ("full_carry_inputs_changed", ["row_live"])
    with World(tmp_path, backend, _history(backend), settings=rules) as world:
        world.started(6)
        _begin(world, "a-turn", T)
        world.live("last", "the last words of the turn", at=T * 1000 + 300, trace="a-turn",
                   settled=True)
        assert world.imported() == (("tail_applied", []) if rules is WITH_LIVE else refused)
        _settle(world, "a-turn", T * 1000 + 500)
        assert _summaries(world) == {"a-turn": ["live-last"]}
        _begin(world, "the-next", T + 1)
        assert world.imported() == (("tail_applied", []) if rules is WITH_LIVE else refused)


# ---- what the rule does not let through ---------------------------------------------

@BACKENDS
def test_without_the_rule_for_later_turns_the_rule_does_nothing(tmp_path, backend):
    how = {"carry_settled_turns": True, "carry_later_turns": False}
    with World(tmp_path, backend, _history(backend), settings=how) as world:
        world.started(6)
        world.grow(_turn(backend, 7))
        assert world.imported() == ("tail_applied", [])
        world.turn_row("a-turn", at=T * 1000)
        world.grow(_turn(backend, 8))
        assert world.imported() == ("full_carry_inputs_changed", ["turn_rows"])
