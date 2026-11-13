"""The carried-inputs guard's own words (lib.message_carry).

What each rule lets through, and what it must not, is shown with real
imports in tests/readmodel/test_rm_n14_carry.py. Here: the record a frontier
keeps of the rows, the order its digests are taken in, the cases no lane
reaches, and that the switches are the rules.
"""
from __future__ import annotations

import random
import sqlite3

import pytest

from lib import message_carry as carry
from lib import transcript_incremental as inc

SESSION = "bs-1"
SOURCE = "transcript.jsonl"
COLUMNS = ("message_id", "agent_id", "backend_session_id", "source_file", "seq", "role", "text",
           "revision", "trace_id", "origin", "tools_json")


def _row(message_id, *, session=SESSION, source=SOURCE, role="assistant", text="x",
         seq=0, trace=None, revision=1):
    return {"message_id": message_id, "agent_id": "a", "backend_session_id": session,
            "source_file": source, "seq": seq, "role": role, "text": text, "revision": revision,
            "trace_id": trace, "origin": "user", "tools_json": "[]"}


def _turn(trace, started, outcome=None):
    return {"trace_id": trace, "started_at": started, "settled_at": None, "outcome": outcome}


def _inputs(*, turns=(), rows=(), user=("", "user", ""), oracle=()):
    return (list(turns), [], user, list(rows), list(oracle))


WHERE = dict(source_file=SOURCE, backend_session_id=SESSION, session_rows=True)


def _changed(before, after, rules=carry.RULES, **stored):
    recorded = carry.record(before, carry=stored, **WHERE)
    return carry.changed(recorded, after, rules=frozenset(rules), carry=stored,
                         boundary_seq=5, **WHERE)


# ---- the record -----------------------------------------------------------------

def test_the_record_is_the_same_few_hundred_bytes_for_any_number_of_rows():
    small = carry.record(_inputs(rows=[_row("m-1")]), **WHERE)
    large = carry.record(_inputs(rows=[_row(f"m-{n}", text="x" * 2000, seq=n, revision=n)
                                       for n in range(1, 5001)]), **WHERE)
    assert len(carry.dumps(small)) == len(carry.dumps(large)) - 2 * (len("5000") - len("1")) < 1400
    assert large["mark"] == large["base"] == 5000
    assert set(large["rows"]) == {"base", "since"}
    assert all(set(part) == set(carry.KINDS) for part in large["rows"].values())
    assert carry.loads(carry.dumps(large)) == large


@pytest.mark.parametrize("text", ["", "not json", "0123456789abcdef0123456789abcdef", "[]", "{}",
                                  '{"version":2,"mark":1,"rows":{}}',
                                  '{"version":1,"mark":1,"base":1,"rows":{"base":{},"since":{}}}', None])
def test_a_stored_text_that_is_not_a_record_is_not_taken_for_one(text):
    """Before the record existed the column held one digest of all the rows
    (32 hex digits); that, and anything else, is no frontier."""
    assert carry.loads(text) is None


def test_a_record_of_another_version_is_not_taken_for_one():
    """Whatever a later release keeps under the same name is not this."""
    recorded = carry.record(_inputs(rows=[_row("m-1")]), **WHERE)
    assert carry.loads(carry.dumps(recorded)) == recorded
    assert carry.loads(carry.dumps({**recorded, "version": recorded["version"] + 1})) is None
    assert carry.loads(carry.dumps({key: value for key, value in recorded.items()
                                    if key != "version"})) is None


def _table(declared, rows, inserted):
    """Real SQLite rows: the columns as declared, the rows as inserted."""
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute(f"CREATE TABLE messages ({', '.join(declared)})")
    for row in inserted:
        connection.execute(
            f"INSERT INTO messages ({', '.join(COLUMNS)}) VALUES ({', '.join('?' * len(COLUMNS))})",
            [rows[row][name] for name in COLUMNS])
    return connection.execute("SELECT * FROM messages").fetchall()      # no ORDER BY


def _mixed_rows():
    rows = [_row(f"m-{n:03d}", seq=n, revision=n, trace=f"t{n % 7}") for n in range(1, 61)]
    rows += [_row(f"u-{n}", source=f"client:{n}", role="user", text=f"words {n}", seq=-n,
                  revision=100 + n) for n in range(1, 9)]
    rows += [_row("live-1", source="live:t1", seq=-100, revision=200),
             _row("final-1", source="final:t2", seq=-101, revision=201),
             _row("n-1", source="marker:1", seq=-102, revision=202),
             _row("m-900", session="bs-2", seq=1, revision=203)]
    return rows


def test_the_digests_do_not_depend_on_the_order_sqlite_returns_rows_or_columns_in():
    """The order is fixed here: the columns by name, the rows by message id.
    The same rows inserted in another order, into a table that declares its
    columns in another order, are the same record."""
    rows = _mixed_rows()
    straight = _table(COLUMNS, rows, range(len(rows)))
    shuffled_rows = list(range(len(rows)))
    shuffled_columns = list(COLUMNS)
    random.Random(7).shuffle(shuffled_rows)
    random.Random(11).shuffle(shuffled_columns)
    other = _table(shuffled_columns, rows, shuffled_rows)
    assert [r["message_id"] for r in straight] != [r["message_id"] for r in other]
    assert straight[0].keys() != other[0].keys()
    first, second = (carry.record(_inputs(rows=found), **WHERE) for found in (straight, other))
    assert first == second
    assert len(set(first["rows"]["base"].values())) == len(carry.KINDS)      # each kind has rows
    # And it is the rows it describes: against either it stands.
    assert carry.changed(first, _inputs(rows=other), **WHERE) == ""


@pytest.mark.parametrize("column", [name for name in COLUMNS if name != "message_id"])
@pytest.mark.parametrize("target,kind", [("m-030", "row_transcript"), ("u-3", "row_send"),
                                         ("live-1", "row_live"), ("final-1", "row_final"),
                                         ("n-1", "row_other"), ("m-900", "row_other_session")])
def test_one_column_changed_in_one_row_changes_that_kinds_digest(column, target, kind):
    """Every column of every row is in its kind's digest."""
    rows = _mixed_rows()
    before = carry.record(_inputs(rows=_table(COLUMNS, rows, range(len(rows)))), **WHERE)
    index = next(i for i, row in enumerate(rows) if row["message_id"] == target)
    value = rows[index][column]
    edited = {**rows[index], column: (value + 1 if isinstance(value, int)
                                       else "changed" if value is None else None if column == "trace_id"
                                       else value + " ")}
    after = carry.record(
        _inputs(rows=_table(COLUMNS, [*rows[:index], edited, *rows[index + 1:]], range(len(rows)))),
        **WHERE)
    differing = {name for name in carry.KINDS
                 if before["rows"]["base"][name] != after["rows"]["base"][name]}
    # The session and the source file decide a row's kind: changed, the row
    # can be missing from one digest and present in another.
    assert differing == {kind, carry._kind_of(edited["source_file"], edited["backend_session_id"],
                                             SOURCE, SESSION)}
    assert before["rows"]["since"] == after["rows"]["since"]


# ---- the mark ---------------------------------------------------------------------

def test_the_rows_at_or_below_the_mark_must_be_the_recorded_rows():
    """The three ways an old row can be wrong, and a row that claims to be
    old. None of them trusts the revision: the digest of what is at or below
    the mark says it."""
    old = [_row("m-1", revision=1), _row("m-2", revision=2, seq=1), _row("u-1", source="client:1",
                                                                         role="user", revision=3, seq=-1)]
    before = _inputs(rows=old)
    edited_same_revision = [{**old[0], "text": "edited"}, *old[1:]]
    edited_new_revision = [{**old[0], "text": "edited", "revision": 9}, *old[1:]]
    removed = old[1:]
    appeared_old = [*old, _row("final-9", source="final:t", revision=2, seq=-9)]
    appeared_new = [*old, _row("final-9", source="final:t", revision=9, seq=-9)]
    new_send = [*old, _row("u-2", source="client:2", role="user", text="new words", revision=9, seq=-2)]
    stored = dict(prefix_latest_ms=0, initial_provenance_used=False)
    assert _changed(before, _inputs(rows=edited_same_revision), **stored) == "row_transcript"
    assert _changed(before, _inputs(rows=edited_new_revision), **stored) == "row_transcript"
    assert _changed(before, _inputs(rows=removed), **stored) == "row_transcript"
    assert _changed(before, _inputs(rows=appeared_old), **stored) == "row_final"
    assert _changed(before, _inputs(rows=appeared_new), **stored) == "row_final"
    # Only a row above the mark is new, and only a new /send row may be let through.
    assert _changed(before, _inputs(rows=new_send), **stored) == ""
    assert _changed(before, _inputs(rows=new_send), rules=(), **stored) == "row_send"


def test_a_tail_keeps_the_digests_of_the_rows_it_left_alone_and_hashes_the_rest():
    """The guard hashes the rows up to the record's base and hands them on
    (the witness). The record a tail then publishes keeps those digests when
    the rows are still those rows, value for value, and hashes only what is
    above the base. Either way the record describes the same rows."""
    old = [_row(f"m-{n}", seq=n, revision=n) for n in range(1, 21)]
    first = carry.record(_inputs(rows=old), **WHERE)
    assert (first["base"], first["mark"]) == (20, 20)
    added = [*old, _row("m-21", seq=21, revision=21), _row("m-22", seq=22, revision=22)]
    stored = dict(prefix_latest_ms=0, initial_provenance_used=False)

    def witnessed(rows):
        witness = {}
        said = carry.changed(first, _inputs(rows=rows), rules=frozenset(carry.RULES), carry=stored,
                             boundary_seq=5, witness=witness, **WHERE)
        return said, witness.get("base")

    # A tail's own rows are above the mark: transcript rows, so the guard
    # names them, and it still hands on the rows up to the base.
    said, witness = witnessed(added)
    assert said == "row_transcript" and witness[0] == 20 and len(witness[1]) == 20
    built = carry.record(_inputs(rows=added), witness=witness, **WHERE)
    flat = carry.record(_inputs(rows=added), **WHERE)
    assert (built["base"], built["mark"]) == (20, 22) and (flat["base"], flat["mark"]) == (22, 22)
    assert built["rows"]["base"] == first["rows"]["base"] != flat["rows"]["base"]
    assert built["rows"]["since"] != first["rows"]["since"]
    for recorded in (built, flat):
        assert carry.changed(recorded, _inputs(rows=added), **WHERE) == ""
        edited = [*added[:-1], {**added[-1], "text": "edited"}]
        assert carry.changed(recorded, _inputs(rows=edited), **WHERE) == "row_transcript"
        early = [{**added[0], "text": "edited"}, *added[1:]]
        assert carry.changed(recorded, _inputs(rows=early), **WHERE) == "row_transcript"
    # The tail itself changed a row up to the base (it linked an old turn,
    # say): the witness is not those rows any more, and everything is hashed.
    touched = [{**added[0], "seq": 900}, *added[1:]]
    again = carry.record(_inputs(rows=touched), witness=witness, **WHERE)
    assert again == carry.record(_inputs(rows=touched), **WHERE) and again["base"] == 22
    # And a base digest that did not match hands nothing on.
    assert witnessed([{**old[0], "text": "edited"}, *old[1:]]) == ("row_transcript", None)


# ---- the cases no lane reaches -------------------------------------------------

def test_identical_rows_stand_under_any_rules():
    rows = _inputs(turns=[_turn("t", 1)], rows=[_row("m-1")])
    assert _changed(rows, _inputs(turns=[_turn("t", 1)], rows=[_row("m-1")]), rules=()) == ""
    assert _changed(rows, rows) == ""


def test_rows_that_were_never_recorded_certify_nothing():
    recorded = _inputs(rows=[_row("m-1")])
    unread = (recorded[0], recorded[1], recorded[2], None, recorded[4])
    assert carry.record(unread, **WHERE) is None
    assert carry.changed(None, recorded, **WHERE) == "not_recorded"
    assert carry.changed(carry.record(recorded, **WHERE), unread, **WHERE) == "not_recorded"


def test_a_record_of_all_the_agents_rows_does_not_stand_for_the_sessions_alone():
    """carry_session_rows switched between the publication and the tail."""
    rows = _inputs(rows=[_row("m-1")])
    agent_wide = carry.record(rows, **{**WHERE, "session_rows": False})
    assert carry.changed(agent_wide, rows, **WHERE) == "rows_scope"
    assert carry.changed(agent_wide, rows, **{**WHERE, "session_rows": False}) == ""


def test_with_no_rule_every_kind_of_change_is_named():
    before = _inputs(turns=[_turn("t", 1)], rows=[_row("m-1")])
    after = _inputs(
        turns=[_turn("t", 1), _turn("later", 10**9)], user=("u-new", "user", ""),
        oracle=[{"delegation_id": 1}],
        rows=[_row("m-1"), _row("u-new", source="client:new", role="user", text="new words", revision=2),
              _row("live-1", source="live:t", revision=3), _row("final-1", source="final:t", revision=4),
              _row("m-9", session="bs-2", revision=5), _row("n-1", source="marker:1", revision=6)])
    assert _changed(before, after, rules=()).split(",") == [
        "latest_user_row", "oracle_rows", "row_final", "row_live", "row_other",
        "row_other_session", "row_send", "turn_rows"]


def test_a_checkpoint_that_recorded_no_time_keeps_every_turn_row_in():
    before = _inputs(turns=[_turn("t", 1)], rows=[_row("m-1")])
    after = _inputs(turns=[_turn("t", 1), _turn("later", 10**9)], rows=[_row("m-1")])
    assert _changed(before, after) == "turn_rows"                      # nothing recorded
    assert _changed(before, after, prefix_latest_ms=None) == "turn_rows"
    assert _changed(before, after, prefix_latest_ms=100) == ""


def test_oracle_rows_count_under_every_rule():
    before = _inputs(rows=[_row("m-1")])
    after = _inputs(rows=[_row("m-1")], oracle=[{"delegation_id": 1}])
    assert _changed(before, after, prefix_latest_ms=100,
                    initial_provenance_used=False) == "oracle_rows"
    edited = _inputs(rows=[_row("m-1")], oracle=[{"delegation_id": 1, "state": "done"}])
    with_history = _inputs(rows=[_row("m-1")], oracle=[{"delegation_id": 1, "state": "open"}])
    assert _changed(with_history, edited) == "oracle_rows"


def test_the_latest_user_row_counts_unless_the_checkpoint_says_no_reply_read_it():
    before = _inputs(rows=[_row("m-1")])
    after = _inputs(rows=[_row("m-1")], user=("u-2", "agent", "sender"))
    assert _changed(before, after) == "latest_user_row"                # not recorded
    assert _changed(before, after, initial_provenance_used=True) == "latest_user_row"
    assert _changed(before, after, initial_provenance_used=False) == ""
    assert _changed(before, after, rules=(), initial_provenance_used=False) == "latest_user_row"


def test_every_rule_has_its_switch_and_is_on_by_default():
    assert set(carry.RULES) == {key for key in inc._DEFAULTS if key.startswith("carry_")}
    assert all(inc._DEFAULTS[rule] is True for rule in carry.RULES)
    assert inc.carry_rules(inc._DEFAULTS) == frozenset(carry.RULES)
    for rule in carry.RULES:
        assert inc.carry_rules({**inc._DEFAULTS, rule: False}) == frozenset(carry.RULES) - {rule}
    # A module the parser version does not hash would let a change to a rule
    # keep every checkpoint that was certified under the old one.
    assert "message_carry.py" in inc._COMMON_SOURCES
