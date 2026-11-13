"""What a tail may find changed among the rows its frontier was published with.

A tail is allowed only while every database row a full replay of the turns
before the boundary could read still stands. Until now that was every message
row of the agent, every turn row, the session's /send rows, the latest user
row and the Oracle rows, all identical: during a running turn about every
second import was a full one. ``lib.message_carry`` names three kinds of row
those old turns cannot read, each behind a switch in ``[transcript_shadow]``.

Every rule is run here in pairs, the full-replay reference lane beside the
candidate, both databases given the same rows:

- the change the rule lets through is imported as a tail and every product
  table equals the full replay's;
- its nearest harmful twin, a change the rule must not let through, is a full
  import and equals it too. Imported as a tail, the twin leaves different
  rows: every twin's comparison fails when its condition is taken out of
  ``message_carry``.

The comparison comes before any word about the decision, so that a missing
condition shows as different products, not as a different code.

Every test runs three times, for the three places a frontier can be when the
import comes: held by the process, taken from the store by a new process (a
Host start), and pushed out of the process's cache by other conversations
(taken from the store again). What the frontier keeps of the rows is a
record of constant size, the same in the process and in the store, so the
decisions are the same in all three.
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3

import pytest

from tests.readmodel import corpus
from tests.readmodel import rm_n14_harness as harness
from tests.readmodel.test_rm_n14_equivalence import _clones

NOTICE = "<clarp-delivery-context>the user heard nothing</clarp-delivery-context>"
OPENING = "<clarp-call-opening>synthetic initiative</clarp-call-opening>"
OTHER_SESSION = "bs-another"
FAR = 10_000_000          # ms after the lane's epoch: later than every synthetic turn


def _stamp(ms: int) -> str:
    return (dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc)
            .isoformat(timespec="milliseconds").replace("+00:00", "Z"))


def _escaped(text: str) -> bytes:
    return json.dumps(text)[1:-1].encode()


def _turn(backend: str, ordinal: int, *, request: str | None = None,
          answer: str | None = None) -> bytes:
    """One complete turn at second ``ordinal``; ``request`` replaces its
    words, ``answer`` those of its last reply."""
    data = harness.append_bytes(backend, ordinal)
    for said, instead in ((f"synthetic request {ordinal}", request),
                          (f"synthetic answer {ordinal}", answer)):
        if instead is not None:
            assert data.count(said.encode()) == 1
            data = data.replace(said.encode(), _escaped(instead))
    return data


STATES = ("held", "after a Host start", "evicted")


@pytest.fixture(autouse=True, params=STATES)
def frontier_state(request):
    """Where the frontier is when each import after the first two comes."""
    World.state = request.param
    yield request.param
    World.state = "held"


class World:
    """Both lanes over one transcript; every row is written to both stores."""

    state = "held"

    def __init__(self, tmp_path, backend, history, *, settings=None):
        self.backend = backend
        self.reference, self.candidate = _clones(tmp_path, backend)
        self.transcript = tmp_path / "synthetic.jsonl"
        self.transcript.write_bytes(b"".join(history))
        # One row a batch in both lanes: a full replay after a removed row
        # numbers its rows by the batches it commits, and a default batch
        # ends on wall clock.
        self.faults = {"single_row_batches": True}
        if settings:
            self.faults["settings"] = settings
        self.tick = 0
        self.rows = 0
        self.standing = False     # a frontier has been published
        self.published = True     # ... and the last import published one too

    def __enter__(self):
        self.full = harness.PersistentLane(self.reference, self.transcript, self.backend, "reference")
        self.actual = harness.PersistentLane(self.candidate, self.transcript, self.backend, "candidate")
        self.full.__enter__()
        self.actual.__enter__()
        return self

    def __exit__(self, *error):
        self.actual.__exit__(*error)
        self.full.__exit__(*error)

    def write(self, sql, parameters=()):
        for database in (self.reference, self.candidate):
            with sqlite3.connect(database) as other:
                assert other.execute(sql, parameters).rowcount == 1

    def next_revision(self, other) -> int:
        return other.execute(
            "UPDATE message_clock SET revision=revision+1 WHERE singleton=0 RETURNING revision"
        ).fetchone()[0]

    def message(self, message_id, *, source_file, role, text, at, origin="user", sender=None,
                trace=None, session=corpus.SESSION, revision=None):
        """A message row as its writer leaves it, under the clock's next
        revision (or, for a row that claims to be an old one, ``revision``)."""
        self.rows += 1
        claimed = revision
        for database in (self.reference, self.candidate):
            with sqlite3.connect(database) as other:
                revision = self.next_revision(other) if claimed is None else claimed
                other.execute(
                    "INSERT INTO messages(message_id,agent_id,backend_session_id,source_file,seq,role,"
                    "timestamp,text,updated_at,revision,origin,sender_agent_id,trace_id) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (message_id, corpus.AGENT, session, source_file, -5000 - self.rows, role,
                     _stamp(harness.BASE_MS + at), text, harness.BASE_MS + at, revision, origin,
                     sender, trace))

    def send(self, name, text, *, at, trace=None, origin="user", sender=None,
             session=corpus.SESSION):
        """A /send row: the words, when they were sent, the turn they are for."""
        self.message(f"u-{name}", source_file=f"client:{name}", role="user", text=text, at=at,
                     origin=origin, sender=sender, trace=trace, session=session)

    def _head(self, other, revision, session=corpus.SESSION):
        other.execute(
            "INSERT INTO conversation_heads(agent_id,backend_session_id,revision,replace_revision) "
            "VALUES (?,?,?,0) ON CONFLICT(agent_id,backend_session_id) DO UPDATE SET "
            "revision=MAX(conversation_heads.revision,excluded.revision)",
            (corpus.AGENT, session, revision))

    def live(self, name, text, *, at, trace, session=corpus.SESSION, settled=False):
        """The row a reply streams into, as message_live inserts it."""
        self.rows += 1
        for database in (self.reference, self.candidate):
            with sqlite3.connect(database) as other:
                revision = self.next_revision(other)
                other.execute(
                    "INSERT INTO messages(message_id,agent_id,backend_session_id,source_file,seq,role,"
                    "timestamp,text,kind,tool_name,tools_json,display_cells_json,updated_at,revision,"
                    "origin,sender_agent_id,trace_id) VALUES (?,?,?,?,?,'assistant',?,?,?,NULL,'[]','[]',"
                    "?,?,'user',NULL,?)",
                    (f"live-{name}", corpus.AGENT, session, f"live:{trace or name}",
                     -900000 + self.rows, _stamp(harness.BASE_MS + at), text,
                     None if settled else "live", harness.BASE_MS + at, revision, trace or None))
                self._head(other, revision, session)

    def live_change(self, name, sql, parameters=()):
        """One more write to that row under the clock's next revision: a
        delta (``text=?``), the settling (``kind=NULL``)."""
        for database in (self.reference, self.candidate):
            with sqlite3.connect(database) as other:
                revision = self.next_revision(other)
                assert other.execute(
                    f"UPDATE messages SET {sql}, revision=?, updated_at=updated_at+1 "
                    "WHERE message_id=? AND source_file LIKE 'live:%'",
                    (*parameters, revision, f"live-{name}")).rowcount == 1
                self._head(other, revision)

    def turn_row(self, trace, *, at, settled=None, outcome=None):
        self.write("INSERT INTO turns(agent_id,source,trace_id,started_at,ended_at,settled_at,outcome) "
                   "VALUES (?,'pwa',?,?,?,?,?)",
                   (corpus.AGENT, trace, harness.BASE_MS + at,
                    None if settled is None else harness.BASE_MS + settled,
                    None if settled is None else harness.BASE_MS + settled, outcome))

    def grow(self, data: bytes):
        with self.transcript.open("ab") as output:
            output.write(data)

    def imported(self):
        """Run both lanes, compare every product table, THEN say what the
        candidate decided and which rows it named."""
        faults = self.faults
        if self.standing and self.state == "after a Host start":
            self.actual.__exit__(None, None, None)
            self.actual = harness.PersistentLane(
                self.candidate, self.transcript, self.backend, "candidate")
            self.actual.__enter__()
        elif self.standing and self.state == "evicted":
            faults = {**faults, "evict": True}
        self.full.tick(self.tick, faults=self.faults)
        report = self.actual.tick(self.tick, faults=faults)
        self.tick += 1
        harness.assert_product_equal(self.reference, self.candidate)
        if self.standing and self.published:
            assert report["frontier_held"] is (self.state == "held")
        self.report = report
        [event] = [e for e in report["tail_events"] if e["event"] == "transcriptTailDecision"]
        return event["code"], [word for line in report["carry_changed"]
                               for word in line.removeprefix("what=").split(",")]

    def started(self, turns: int):
        """The first import (full), then one plain tail: a frontier stands."""
        assert self.imported()[0] == "full_no_frontier"
        self.grow(_turn(self.backend, turns))
        assert self.imported() == ("tail_applied", [])
        self.standing = True
        return turns + 1


def _history(backend, turns=6, **replaced):
    return [_turn(backend, i, request=replaced.get(f"request{i}"),
                  answer=replaced.get(f"answer{i}")) for i in range(turns)]


ON_OFF = pytest.mark.parametrize("rule_on", [True, False], ids=["rule on", "rule off"])
BACKENDS = pytest.mark.parametrize("backend", harness.BACKENDS)


# ---- rows of the agent's other sessions ----------------------------------------

@ON_OFF
@BACKENDS
def test_a_row_in_another_session_of_the_agent_does_not_cost_a_full_import(
        tmp_path, backend, rule_on):
    settings = None if rule_on else {"carry_session_rows": False}
    with World(tmp_path, backend, _history(backend), settings=settings) as world:
        ordinal = world.started(6)
        world.send("elsewhere", "synthetic request 2", at=FAR, session=OTHER_SESSION)
        world.message("live-elsewhere", source_file="live:elsewhere", role="assistant",
                      text="synthetic answer 2", at=2000, trace="elsewhere", session=OTHER_SESSION)
        world.grow(_turn(backend, ordinal))
        decision, named = world.imported()
        assert (decision, named) == (("tail_applied", []) if rule_on else
                                     ("full_carry_inputs_changed", ["row_other_session"]))
        world.grow(_turn(backend, ordinal + 1))
        assert world.imported() == ("tail_applied", [])


@pytest.mark.parametrize("twin", [
    "a finalized row with an old reply's words",
    "the same row, claiming an old revision",
    "an old row edited without a new revision",
    "an old row edited with a new revision",
    "an old row removed",
    "a row that a tail wrote, edited",
    "an old linked /send row reworded",
])
@BACKENDS
def test_a_row_of_this_session_that_an_old_turn_reads_is_a_full_import(tmp_path, backend, twin):
    """The twins: rows of this session. A full replay removes a finalized row
    that shows an old reply's words beside that reply, puts an edited row
    back, writes a removed one again, and no longer links a turn to a /send
    row whose words were changed.

    They are also the shapes the record's mark has to catch, none of which
    trusts the revision. A new row lies above the mark and is judged as new.
    A row that appears with a revision at or below the mark lands among the
    old rows and breaks their digest. An old row edited without a new
    revision changes its digest; edited with one, or removed, it is missing
    from it. The same for a row a tail wrote after the first full import:
    those are hashed apart from the rows before them."""
    with World(tmp_path, backend, _history(backend)) as world:
        world.send("linked", "synthetic request 3", at=2900)
        ordinal = world.started(6)
        answer = ("backend_session_id=? AND role='assistant' AND text='synthetic answer 2'",
                  (corpus.SESSION,))
        if twin.startswith(("a finalized", "the same row")):
            world.message("final-twin", source_file="final:twin", role="assistant",
                          text="synthetic answer 2", at=2000, trace="twin",
                          revision=1 if twin.endswith("old revision") else None)
            expected = "row_final"
        elif twin.endswith("without a new revision"):
            world.write(f"UPDATE messages SET text='edited by another writer' WHERE {answer[0]}",
                        answer[1])
            expected = "row_transcript"
        elif twin.endswith("with a new revision"):
            for database in (world.reference, world.candidate):
                with sqlite3.connect(database) as other:
                    assert other.execute(
                        f"UPDATE messages SET text='edited by another writer', revision=? "
                        f"WHERE {answer[0]}", (world.next_revision(other), *answer[1])).rowcount == 1
            expected = "row_transcript"
        elif twin.endswith("removed"):
            world.write(f"DELETE FROM messages WHERE {answer[0]}", answer[1])
            expected = "row_transcript"
        elif twin.startswith("a row that a tail wrote"):
            world.write("UPDATE messages SET text='edited by another writer' WHERE backend_session_id=? "
                        "AND role='assistant' AND text='synthetic answer 6'", (corpus.SESSION,))
            expected = "row_transcript"
        else:
            for database in (world.reference, world.candidate):
                with sqlite3.connect(database) as other:
                    assert other.execute(
                        "UPDATE messages SET text='other words now', revision=? WHERE message_id='u-linked'",
                        (world.next_revision(other),)).rowcount == 1
            expected = "row_send"
        world.grow(_turn(backend, ordinal))
        assert world.imported() == ("full_carry_inputs_changed", [expected])
        world.grow(_turn(backend, ordinal + 1))
        assert world.imported() == ("tail_applied", [])


@BACKENDS
def test_a_message_that_came_after_the_full_import_is_an_old_row_once_a_tail_has_published(
        tmp_path, backend):
    """The rows a frontier is published with are hashed in two parts: those
    of the last full import, and those written since. A new message with new
    words is let through and is then one of the second part. Reworded
    afterwards to the words of an old turn, with no new revision, it is an
    old row that changed: a full replay links that old turn to it."""
    with World(tmp_path, backend, _history(backend)) as world:
        ordinal = world.started(6)
        world.send("later", "words nobody said before", at=FAR)
        world.grow(_turn(backend, ordinal))
        assert world.imported() == ("tail_applied", [])
        world.write("UPDATE messages SET text='synthetic request 2' WHERE message_id='u-later'")
        world.grow(_turn(backend, ordinal + 1))
        assert world.imported() == ("full_carry_inputs_changed", ["row_send"])
        world.grow(_turn(backend, ordinal + 2))
        assert world.imported() == ("tail_applied", [])


# ---- turn rows that began after the boundary's turns ---------------------------

@ON_OFF
@BACKENDS
def test_a_turn_that_began_after_the_old_turns_does_not_cost_a_full_import(
        tmp_path, backend, rule_on):
    """Opened, then settled: neither is read by a turn before the boundary."""
    settings = None if rule_on else {"carry_later_turns": False}
    wanted = ("tail_applied", []) if rule_on else ("full_carry_inputs_changed", ["turn_rows"])
    with World(tmp_path, backend, _history(backend), settings=settings) as world:
        ordinal = world.started(6)
        world.turn_row("later", at=FAR)
        world.grow(_turn(backend, ordinal))
        assert world.imported() == wanted
        world.write("UPDATE turns SET settled_at=started_at+900, ended_at=started_at+900, "
                    "outcome='completed' WHERE trace_id='later'")
        world.grow(_turn(backend, ordinal + 1))
        assert world.imported() == wanted
        world.grow(_turn(backend, ordinal + 2))
        assert world.imported() == ("tail_applied", [])


@pytest.mark.parametrize("began", ["among the old turns", "inside the start slack"])
@BACKENDS
def test_a_turn_that_was_running_at_an_old_turns_time_is_a_full_import(tmp_path, backend, began):
    """The twins by time. An old row with no trace takes the trace of the
    turn that was running at its time, and a turn counts as running from two
    seconds before it began: a turn row that began up to two seconds after
    the last old turn changes that turn's rows."""
    with World(tmp_path, backend, _history(backend)) as world:
        ordinal = world.started(6)
        # After the first import the boundary is the last user turn then (5):
        # the old turns are 0 to 4, the last at second 4.
        world.turn_row("was-running", at=2500 if began == "among the old turns" else 4000 + 1500,
                       settled=FAR, outcome="completed")
        world.grow(_turn(backend, ordinal))
        assert world.imported() == ("full_carry_inputs_changed", ["turn_before_boundary"])
        world.grow(_turn(backend, ordinal + 1))
        assert world.imported() == ("tail_applied", [])


@BACKENDS
def test_a_turn_for_the_trace_of_a_row_linked_before_the_boundary_is_a_full_import(
        tmp_path, backend):
    """The twin by trace. A message steered into a running turn has a trace
    and no turn of its own, so the replies after it take the running turn's
    trace. When a turn with that trace appears, however late it began, a
    full replay gives those replies the message's own trace."""
    with World(tmp_path, backend, _history(backend)) as world:
        world.turn_row("running", at=1500)
        world.send("steered", "synthetic request 2", at=1900, trace="steered-trace")
        ordinal = world.started(6)
        world.turn_row("steered-trace", at=FAR, settled=FAR + 900, outcome="completed")
        world.grow(_turn(backend, ordinal))
        assert world.imported() == ("full_carry_inputs_changed", ["turn_of_old_row"])
        world.grow(_turn(backend, ordinal + 1))
        assert world.imported() == ("tail_applied", [])


# ---- a new /send row ----------------------------------------------------------

@ON_OFF
@BACKENDS
def test_a_new_message_whose_words_were_not_said_before_does_not_cost_a_full_import(
        tmp_path, backend, rule_on):
    """The row alone, from another agent: it becomes the conversation's
    latest user row, which no reply before the boundary reads here."""
    settings = None if rule_on else {"carry_new_sends": False}
    with World(tmp_path, backend, _history(backend), settings=settings) as world:
        ordinal = world.started(6)
        world.send("new", f"synthetic request {ordinal}", at=ordinal * 1000 - 500,
                   origin="agent", sender=corpus.SENDER)
        world.grow(_turn(backend, ordinal))
        assert world.imported() == (("tail_applied", []) if rule_on else
                                    ("full_carry_inputs_changed", ["latest_user_row", "row_send"]))
        world.grow(_turn(backend, ordinal + 1))
        assert world.imported() == ("tail_applied", [])


@pytest.mark.parametrize("rules", ["all three", "none"])
@BACKENDS
def test_an_ordinary_new_message_is_imported_by_its_tail(tmp_path, backend, rules):
    """What /send leaves when a message arrives: its row and its turn's row.
    Then the provider writes the turn. With every rule off this is the full
    import it always was."""
    settings = None if rules == "all three" else {
        "carry_session_rows": False, "carry_later_turns": False, "carry_new_sends": False}
    with World(tmp_path, backend, _history(backend), settings=settings) as world:
        ordinal = world.started(6)
        for number in (ordinal, ordinal + 1):
            sent = number * 1000 - 500
            world.send(f"message-{number}", f"synthetic request {number}", at=sent,
                       trace=f"turn-{number}")
            world.turn_row(f"turn-{number}", at=sent)
            world.grow(_turn(backend, number))
            decision, named = world.imported()
            assert decision == ("tail_applied" if rules == "all three"
                                else "full_carry_inputs_changed")
            assert named == ([] if rules == "all three"
                             else ["latest_user_row", "row_send", "turn_rows"])


@pytest.mark.parametrize("words", ["synthetic request 2", "  synthetic request 2 \n"],
                         ids=["the same words", "with other whitespace around them"])
@BACKENDS
def test_a_new_message_with_the_words_of_an_old_turn_typed_in_a_terminal_is_a_full_import(
        tmp_path, backend, words):
    """The twin. The old turn found no /send row when it was imported and is
    stored as a transcript row. A full replay links it to the new row. The
    linker compares stripped words, so the new row's words are stripped too:
    spaces and a newline around them do not make them new."""
    with World(tmp_path, backend, _history(backend)) as world:
        ordinal = world.started(6)
        world.send("again", words, at=FAR)
        world.grow(_turn(backend, ordinal))
        assert world.imported() == ("full_carry_inputs_changed", ["send_said_before"])
        world.grow(_turn(backend, ordinal + 1))
        assert world.imported() == ("tail_applied", [])


@pytest.mark.parametrize("words", ["Synthetic Request 2", "synthetic  request 2"],
                         ids=["another case", "other whitespace inside"])
@BACKENDS
def test_words_the_linker_would_not_link_to_an_old_turn_are_new_words(tmp_path, backend, words):
    """The other side of the same key: nothing but the strip. Another case
    or another space between the words is another message to the linker, so
    it is one here."""
    with World(tmp_path, backend, _history(backend)) as world:
        ordinal = world.started(6)
        world.send("nearly", words, at=FAR)
        world.grow(_turn(backend, ordinal))
        assert world.imported() == ("tail_applied", [])


@BACKENDS
def test_a_message_that_arrives_after_its_turn_was_imported_is_linked_though_the_file_stands_still(
        tmp_path, backend):
    """A turn after the boundary is imported with no /send row (as typed in a
    terminal), and then its /send row arrives. The words are new to the
    turns before the boundary, so the rules let the row through. They do not
    make the file's import a no-op: the turn the row is for is after the
    boundary, and the tail links it. An unchanged file is left alone only
    when every recorded row stands with no rule applied; taken for unchanged
    here (after a Host start or an eviction, where that question is asked),
    the turn would stay unlinked."""
    with World(tmp_path, backend, _history(backend)) as world:
        ordinal = world.started(6)
        world.grow(_turn(backend, ordinal))
        assert world.imported() == ("tail_applied", [])
        world.send("late", f"synthetic request {ordinal}", at=ordinal * 1000 - 500,
                   origin="agent", sender=corpus.SENDER)
        assert world.imported() == ("tail_applied", [])        # the file has not changed
        with sqlite3.connect(world.candidate) as linked:
            assert linked.execute(
                "SELECT count(*) FROM messages WHERE role='user' AND text=? "
                "AND source_file NOT LIKE 'client:%'", (f"synthetic request {ordinal}",)
            ).fetchone()[0] == 0
        # Nothing has changed now: left alone where that is asked.
        assert world.imported() == (
            ("tail_applied" if world.state == "held" else "unchanged_file"), [])
        world.grow(_turn(backend, ordinal + 1))
        assert world.imported() == ("tail_applied", [])


@BACKENDS
def test_a_later_turn_after_a_message_a_tail_let_through_is_still_a_tail(tmp_path, backend):
    """The rows a tail writes for a new message carry its turn's trace and
    lie in the second part of the record (written since its base). The
    traces the record was published with are those of both parts. A turn row
    that begins later, for another trace, changes nothing an old turn reads;
    with the traces taken from the first part only it would be refused."""
    with World(tmp_path, backend, _history(backend)) as world:
        ordinal = world.started(6)
        world.send("own", f"synthetic request {ordinal}", at=FAR, trace="own-turn")
        world.turn_row("own-turn", at=FAR)
        world.grow(_turn(backend, ordinal))
        assert world.imported() == ("tail_applied", [])
        with sqlite3.connect(world.candidate) as stored:
            recorded = json.loads(stored.execute(
                "SELECT inputs_digest FROM transcript_import_frontiers").fetchone()[0])
            traced = stored.execute(
                "SELECT count(*) FROM messages WHERE trace_id='own-turn' AND revision>?",
                (recorded["base"],)).fetchone()[0]
        assert recorded["base"] < recorded["mark"] and traced > 0
        world.turn_row("another-turn", at=FAR + 60_000)
        world.grow(_turn(backend, ordinal + 1))
        assert world.imported() == ("tail_applied", [])


@BACKENDS
def test_a_new_message_with_the_words_of_an_old_linked_message_is_a_full_import(tmp_path, backend):
    """The twin for a turn that WAS linked, so no transcript row holds its
    words; the carry lists the row it took. The old row was sent before the
    turn began that was running when the provider wrote the words (a queued
    message, steered in). With a second row of the same words, sent after
    that turn began, a full replay links the old turn to the newer row."""
    with World(tmp_path, backend, _history(backend)) as world:
        world.send("queued", "synthetic request 2", at=1000, trace="queued-trace")
        world.turn_row("running", at=1500)
        ordinal = world.started(6)
        world.send("again", "synthetic request 2", at=FAR, trace="again-trace")
        world.turn_row("again-trace", at=FAR)
        world.grow(_turn(backend, ordinal))
        assert world.imported() == ("full_carry_inputs_changed", ["send_said_before"])
        world.grow(_turn(backend, ordinal + 1))
        assert world.imported() == ("tail_applied", [])


@BACKENDS
def test_a_new_message_that_begins_with_the_delivery_notice_is_a_full_import(tmp_path, backend):
    """The twin by the notice. An old prompt that carries the Host's delivery
    notice is stored without it, unless the user's own row begins with the
    notice. A new row that does so makes a full replay keep the notice and
    link that old turn to the row. Its words as a whole were never said."""
    prompt = NOTICE + "\nsynthetic request 2"
    with World(tmp_path, backend, _history(backend, request2=prompt)) as world:
        ordinal = world.started(6)
        world.send("pasted", prompt, at=FAR)
        world.grow(_turn(backend, ordinal))
        assert world.imported() == ("full_carry_inputs_changed", ["send_said_before"])
        world.grow(_turn(backend, ordinal + 1))
        assert world.imported() == ("tail_applied", [])


@BACKENDS
def test_a_new_message_is_a_full_import_when_an_old_reply_reads_the_latest_user_row(
        tmp_path, backend):
    """The twin by the latest user row. A transcript that opens with a call
    opening has replies before any user turn; they take whose turn it is
    from the conversation's latest user row. The checkpoint records that
    such replies exist before the boundary; a new message from another agent
    then changes them."""
    with World(tmp_path, backend, _history(backend, request0=OPENING)) as world:
        ordinal = world.started(6)
        world.send("new", f"synthetic request {ordinal}", at=ordinal * 1000 - 500,
                   origin="agent", sender=corpus.SENDER)
        world.grow(_turn(backend, ordinal))
        assert world.imported() == ("full_carry_inputs_changed", ["latest_user_row"])
        world.grow(_turn(backend, ordinal + 1))
        assert world.imported() == ("tail_applied", [])
