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


# ---- the live rows of the session -----------------------------------------------

def _live(name, text, *, trace, revision, kind="live", session=SESSION):
    return {**_row(f"live-{name}", source=f"live:{trace}", text=text, seq=-900000,
                   trace=trace, revision=revision, session=session), "kind": kind}


def test_a_live_row_is_let_through_unless_a_reply_before_the_boundary_carries_its_trace():
    """What the lane shows with real imports (test_rm_n14_live_rows.py), in
    the comparator's own words. The boundary is at seq 5."""
    old = [{**_row("m-1", seq=1, trace="old-turn", revision=1), "kind": None},
           {**_row("m-2", seq=2, trace="old-turn", role="user", revision=2), "kind": None},
           {**_row("m-7", seq=7, trace="running", revision=3), "kind": None},       # after the boundary
           _live("left", "left by the old turn", trace="old-turn", revision=4, kind=None)]
    before = _inputs(rows=old)
    stored = dict(prefix_latest_ms=0, initial_provenance_used=False)

    def said(rows, rules=carry.RULES):
        return _changed(before, _inputs(rows=rows), rules=rules, **stored)

    running = _live("reply", "so far", trace="running", revision=9)
    assert said([*old, running]) == ""                                   # a new one
    assert said([*old, running], rules=carry.RULES[:3]) == "row_live"   # the rule not given
    assert said([*old, {**running, "text": "further", "revision": 10}]) == ""
    assert said(old[:3]) == ""                                           # the old one gone
    assert said([*old, _live("stray", "no trace", trace=None, revision=9)]) == ""
    # Of the old turn, whose reply m-1 lies before the boundary:
    assert said([*old, _live("more", "other words", trace="old-turn", revision=9)]) == \
        "live_row_of_old_reply"
    assert said([*old[:3], {**old[3], "text": "rewritten", "revision": 9}]) == "live_row_of_old_reply"
    assert said([*old[:3], {**old[3], "text": "rewritten"}]) == "live_row_of_old_reply"   # no new revision
    # As it was when the frontier was published, it is not asked; and one
    # harmful row among harmless ones is enough.
    assert said([*old, running, _live("more", "x", trace="old-turn", revision=11)]) == \
        "live_row_of_old_reply"
    # A user row's trace is not a reply's, and a reply from the boundary on
    # is in a tail's batch as well.
    only_user = [old[1], old[2], _live("left", "left", trace="old-turn", revision=4, kind=None)]
    recorded = carry.record(_inputs(rows=only_user), carry=stored, **WHERE)
    changed = [*only_user[:2], {**only_user[2], "text": "rewritten", "revision": 9}]
    assert carry.changed(recorded, _inputs(rows=changed), rules=frozenset(carry.RULES),
                         carry=stored, boundary_seq=5, **WHERE) == ""


# ---- a live row beside old replies of its turn, by their words (LIVE_WORDS) ----------

def test_a_live_row_of_an_old_turn_is_refused_for_related_words_only():
    """What the lane shows with real imports (test_rm_n14_live_words.py), in
    the comparator's own words. The carry holds what the old turn's replies
    said, as the import that chose the boundary kept it."""
    old = [{**_row("m-1", seq=1, trace="old-turn", revision=1, text="stored otherwise"), "kind": None},
           {**_row("m-7", seq=7, trace="running", revision=3), "kind": None}]
    before = _inputs(rows=old)
    stored = dict(prefix_latest_ms=0, initial_provenance_used=False,
                  reply_words={"old-turn": ["Done.", "the old reply's words"]})

    def said(text, *, rules=carry.RULES, trace="old-turn", carried=stored):
        rows = [*old, _live("more", text, trace=trace, revision=9)]
        return _changed(before, _inputs(rows=rows), rules=rules, **carried)

    # Two words for two reasons: the row shows what an old reply said, or
    # nothing was kept to compare it with and the trace alone decides.
    refused, by_trace = "live_row_shows_old_reply", "live_row_of_old_reply"
    assert said("other words") == ""
    assert said("the old reply's words") == refused                     # the same
    assert said("the old reply's words, and more") == refused           # begin with them
    assert said("the old") == refused                                   # what they begin with
    assert said("Done. And then some") == refused                       # the other reply
    assert said("the old reply's worDs") == ""                          # differ at one letter
    assert said("<speak>the old reply's words</speak>") == refused      # compared without markup
    assert said("") == "" and said("<speak></speak>") == ""             # shows nothing
    assert said("stored otherwise") == ""           # the row's text is not what is asked
    # Without the rule, without the words, or for a turn the carry did not
    # keep: by the trace alone, as before.
    assert said("other words", rules=carry.RULES[:5]) == by_trace
    assert said("other words", carried={**stored, "reply_words": None}) == by_trace
    assert said("other words", carried={k: v for k, v in stored.items() if k != "reply_words"}) == by_trace
    assert said("other words", carried={**stored, "reply_words": {"another": []}}) == by_trace
    # By the trace alone it is that word whatever the row shows.
    assert said("the old reply's words", rules=carry.RULES[:5]) == by_trace
    # A turn that is kept and said nothing is related to nothing.
    assert said("the old reply's words", carried={**stored, "reply_words": {"old-turn": []}}) == ""
    # The rule adds nothing where LIVE_ROWS already lets the row through.
    assert said("the old reply's words", trace="running") == ""


def test_the_words_kept_are_those_of_the_newest_turns_while_they_fit(monkeypatch):
    said = [("a", "one"), ("a", ""), ("", "no turn"), ("b", "two"), ("a", "one"), ("c", ""),
            ("d", "four"), ("b", "two more")]
    assert carry.reply_words(said) == {"b": ["two", "two more"], "d": ["four"], "c": []}
    assert list(carry.reply_words(said)) == ["b", "d", "c"]             # newest first
    assert carry.reply_words([]) == {} == carry.reply_words([("", "words of no turn")])
    monkeypatch.setattr(carry, "_WORDS_TRACES", 1)
    assert carry.reply_words(said) == {"b": ["two", "two more"]}
    monkeypatch.setattr(carry, "_WORDS_TRACES", 3)
    # A turn whose words do not fit is left out, and every older one with it.
    # Each reply counts as it is stored: its characters and its two quotes.
    monkeypatch.setattr(carry, "_WORDS_BYTES", 14)
    assert carry.reply_words(said) == {}
    monkeypatch.setattr(carry, "_WORDS_BYTES", 15)                      # "two" + "two more"
    assert carry.reply_words(said) == {"b": ["two", "two more"]}
    monkeypatch.setattr(carry, "_WORDS_BYTES", 21)                      # and "four"
    assert carry.reply_words(said) == {"b": ["two", "two more"], "d": ["four"], "c": []}
    monkeypatch.setattr(carry, "_WORDS_BYTES", 20)
    assert carry.reply_words(said) == {"b": ["two", "two more"]}


def test_the_limit_counts_the_words_as_the_checkpoint_stores_them(monkeypatch):
    """The checkpoint stores its carry as JSON in ASCII: a letter that is not
    ASCII takes six bytes there, an emoji twelve, a quote or a newline two.
    Counted in characters, 60,152 of them became a carry of 360 KB under a
    limit of 64 KB (review of #353)."""
    import json
    for text, size in (("abc", 5), ("å", 8), ("åäö", 20), ("\U0001F600", 14), ('say "x"', 11),
                       ("a\nb", 6), ("", 2)):
        assert carry.stored_size(text) == size == len(json.dumps(text)), text
    words = "ø" * 100                                  # 100 characters, 602 bytes stored
    monkeypatch.setattr(carry, "_WORDS_BYTES", 601)
    assert carry.reply_words([("a", words)]) == {}
    monkeypatch.setattr(carry, "_WORDS_BYTES", 602)
    assert carry.reply_words([("a", words)]) == {"a": [words]}
    # What is kept is never stored larger than the limit says, whatever it holds.
    monkeypatch.setattr(carry, "_WORDS_BYTES", 65536)
    for filler in ("x", "é", "\U0001F600", '"', "\n"):
        said = [(f"t{turn}", filler * 9000 + str(reply)) for turn in range(3) for reply in range(3)]
        kept = carry.reply_words(said)
        assert sum(len(json.dumps(text)) for texts in kept.values() for text in texts) <= 65536, filler
        assert len(json.dumps(kept, separators=(",", ":"))) <= 65536 + 64, filler
    assert carry._WORDS_BYTES == 65536


def test_related_is_one_function():
    """The full import and the guard ask the same test, not two copies."""
    from lib import message_import_rows, message_text, message_writes
    assert message_import_rows._related is message_text._related is message_writes._related \
        is carry._related
    related = message_text._related
    assert related("abc", "abc") and related("abc", "ab") and related("ab", "abc")
    assert not related("abc", "abd") and not related("", "abc") and not related("abc", "")
    assert not related("", "")


def test_the_live_rows_rule_is_for_live_rows_of_this_session_only():
    old = [{**_row("m-1", seq=1, trace="old-turn", revision=1), "kind": None}]
    before = _inputs(rows=old)
    stored = dict(prefix_latest_ms=0, initial_provenance_used=False)
    final = {**_row("final-1", source="final:running", trace="running", revision=9), "kind": None}
    elsewhere = _live("there", "x", trace="old-turn", revision=9, session="bs-2")
    assert _changed(before, _inputs(rows=[*old, final]), **stored) == "row_final"
    # Another session's rows are not this record's when it is the session's;
    # when it is the agent's (carry_session_rows off) they count as before.
    assert _changed(before, _inputs(rows=[*old, elsewhere]), **stored) == "row_other_session"


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


def test_every_rule_has_its_switch_and_the_live_rows_rule_is_off_until_switched_on():
    assert set(carry.RULES) == {key for key in inc._DEFAULTS if key.startswith("carry_")}
    off = {carry.LIVE_ROWS, carry.SETTLED_TURNS, carry.LIVE_WORDS}
    on = frozenset(carry.RULES) - off
    assert {rule for rule in carry.RULES if inc._DEFAULTS[rule] is True} == on
    assert all(inc._DEFAULTS[rule] is False for rule in off)
    assert inc.carry_rules(inc._DEFAULTS) == on
    assert inc.carry_rules({**inc._DEFAULTS, **dict.fromkeys(off, True)}) == frozenset(carry.RULES)
    for rule in on:
        assert inc.carry_rules({**inc._DEFAULTS, rule: False}) == on - {rule}
    # A module the parser version does not hash would let a change to a rule
    # keep every checkpoint that was certified under the old one.
    assert "message_carry.py" in inc._COMMON_SOURCES


# ---- a turn that settled (SETTLED_TURNS) ------------------------------------------

LATEST = 300_000            # the last old turn's time
HOURS = 3_600_000
SETTLING = frozenset(carry.RULES) - {carry.LIVE_ROWS}


def _settled_turn(trace, started, settled=None, outcome=None):
    return {"trace_id": trace, "started_at": started, "settled_at": settled, "outcome": outcome}


def _tail_row(message_id, seq, *, revision, trace="t", tools="[]", text="x", turn=None):
    return {**_row(message_id, seq=seq, revision=revision, trace=trace, text=text),
            "tools_json": tools, "display_cells_json": "[]", "turn_json": turn, "updated_at": revision}


def _all_columns(rows):
    return [{"display_cells_json": "[]", "turn_json": None, "updated_at": 0, **row} for row in rows]


def test_a_turn_row_above_the_mark_is_put_back_to_unsettled_and_no_other():
    turns = carry._turn_values([_settled_turn("old", 1, 50, "completed"),
                                _settled_turn("at-the-mark", 2, 90, "failed"),
                                _settled_turn("since", 3, 91, "interrupted"),
                                _settled_turn("running", 4)])
    assert carry._settled_mark(turns) == 91
    restored, settled = carry._unsettled(turns, 90)
    assert restored == [("old", 1, 50, "completed"), ("at-the-mark", 2, 90, "failed"),
                        ("since", 3, None, None), ("running", 4, None, None)]
    assert settled == [("since", 3, 91, "interrupted")]
    assert carry._unsettled(turns, 91) == (turns, [])
    assert carry._settled_mark(carry._turn_values([_settled_turn("running", 4)])) == 0


@pytest.mark.parametrize("started,settled,covers", [
    (LATEST + 2_001, LATEST + 5_000, True),          # began and ended after every old turn
    (1_000, LATEST - 120_000, True),                 # two minutes before the last old turn: just covers
    (1_000, LATEST - 120_001, False),                # a millisecond short: the last old turn falls out
    (1_000, LATEST + 50_000, True),
    (LATEST - 6 * HOURS, LATEST + 1, True),          # six hours before: the old reach just covered it
    (LATEST - 6 * HOURS - 1, LATEST + 1, False),     # a millisecond more: the turn would gain it
])
def test_the_reach_of_a_settled_turn_covers_the_old_turns_or_it_does_not(started, settled, covers):
    row = ("t", started, settled, "completed")
    assert carry._reach_covers(row, {"prefix_latest_ms": LATEST}) is covers
    assert carry._reach_covers(row, {}) is False            # a checkpoint that recorded no time


def _settle_world(*, boundary=10, after=None, started=LATEST + 5_000):
    """A frontier published while turn "t" ran, its rows from the boundary on,
    and what a settle then leaves: the turn row, and a stamp on its last row."""
    rows = _all_columns([_row(f"m-{n}", seq=n, revision=n + 1, trace="old") for n in range(boundary)])
    tail = [_tail_row("m-10", 10, revision=20), _tail_row("m-11", 11, revision=21)]
    stored = {"prefix_latest_ms": LATEST}
    before = _inputs(turns=[_turn("old", 1, "completed"), _settled_turn("t", started)],
                     rows=rows + tail)
    recorded = carry.record(before, carry=stored, boundary_seq=boundary, **WHERE)
    stamped = [tail[0], {**tail[1], "revision": 30, "updated_at": 30,
                         "turn_json": '{"turn_id":"t"}', **(after or {})}]
    now = _inputs(turns=[_turn("old", 1, "completed"),
                         _settled_turn("t", started, LATEST + 9_000, "completed")],
                  rows=rows + stamped)
    return recorded, now, stored, boundary


def _asked(recorded, now, stored, boundary, rules=SETTLING):
    return carry.changed(recorded, now, rules=frozenset(rules), carry=stored,
                         boundary_seq=boundary, **WHERE)


def test_a_settle_and_its_stamp_are_let_through_and_the_stamp_alone_is_not_enough():
    recorded, now, stored, boundary = _settle_world()
    assert recorded["version"] == 3 and recorded["boundary"] == boundary
    assert recorded["base"] == 19 and recorded["settled"] == 0      # below the turn's rows
    assert _asked(recorded, now, stored, boundary) == ""
    without = SETTLING - {carry.SETTLED_TURNS}
    assert _asked(recorded, now, stored, boundary, without) == "row_transcript"
    assert _asked(recorded, now, stored, boundary, SETTLING - {carry.LATER_TURNS}) \
        == "row_transcript,turn_rows"
    # The same row with other words is not a stamp.
    recorded, now, stored, boundary = _settle_world(after={"text": "other words"})
    assert _asked(recorded, now, stored, boundary) == "row_transcript"
    recorded, now, stored, boundary = _settle_world(after={"trace_id": "another"})
    assert _asked(recorded, now, stored, boundary) == "row_transcript"
    # A turn that holds old turns by time and ends too early for the last of them.
    recorded, now, stored, boundary = _settle_world(started=1_000)
    turns, _sends, user, rows, oracle = now
    early = (turns[:1] + [_settled_turn("t", 1_000, LATEST - 130_000, "completed")], [], user,
             rows, oracle)
    assert _asked(recorded, now, stored, boundary) == ""
    assert _asked(recorded, early, stored, boundary) == "turn_settled_out_of_reach"


def test_a_stamp_is_let_through_only_on_a_transcript_row_from_the_boundary_on():
    """The same stamp on the same row, with the row somewhere else each time."""
    _recorded, now, stored, boundary = _settle_world()
    turns, sends, user, rows, oracle = now
    running = (turns[:1] + [_settled_turn("t", LATEST + 5_000)], sends, user)
    for change, word in (({}, ""),
                         ({"seq": boundary - 1}, "row_transcript"),         # before the boundary
                         ({"seq": 10 ** 12 + 5}, "row_transcript"),         # a parked row
                         ({"source_file": "final:t"}, "row_final"),
                         ({"source_file": "client:t"}, "row_send"),
                         ({"source_file": "live:t"}, "row_live"),
                         ({"backend_session_id": "another-session"}, "row_other_session")):
        stamped = {**rows[-1], **change}
        unstamped = {**stamped, "revision": 21, "updated_at": 21, "turn_json": None}
        recorded = carry.record((*running, rows[:-1] + [unstamped], oracle), carry=stored,
                                boundary_seq=boundary, **WHERE)
        after = (turns, sends, user, rows[:-1] + [stamped], oracle)
        assert _asked(recorded, after, stored, boundary) == word, change


def test_a_full_imports_record_keeps_the_rows_from_the_boundary_on_out_of_its_base():
    rows = _all_columns([_row(f"m-{n}", seq=n, revision=100 - n) for n in range(12)])
    whole = carry.record(_inputs(rows=rows), boundary_seq=None, **WHERE)
    assert whole["base"] == whole["mark"] == 100 and whole["boundary"] is None
    cut = carry.record(_inputs(rows=rows), boundary_seq=8, **WHERE)
    # Rows 8 to 11 hold revisions 92 to 89: the base ends just below the lowest.
    assert (cut["mark"], cut["base"], cut["boundary"]) == (100, 88, 8)
    assert cut["rows"]["base"]["row_tail"] == whole["rows"]["since"]["row_tail"]
    assert cut["rows"]["since"]["row_tail"] != whole["rows"]["since"]["row_tail"]
    assert cut["tail"] != whole["tail"]
    assert carry.loads(carry.dumps(cut)) == cut
    for broken in ({"settled": None}, {"tail": None}, {"boundary": "8"}, {"version": 2}):
        assert carry.loads(carry.dumps({**cut, **broken})) is None


def test_an_interrupted_turn_with_a_row_before_the_boundary_is_not_let_through():
    stored = {"prefix_latest_ms": LATEST}
    rows = _all_columns([_row("m-0", seq=0, revision=1, trace="old"),
                         _row("m-1", seq=1, revision=2, trace="t")])
    before = _inputs(turns=[_settled_turn("t", 1_000)], rows=rows)
    recorded = carry.record(before, carry=stored, boundary_seq=5, **WHERE)
    for outcome, wanted in (("completed", ""), ("failed", ""), ("interrupted", "turn_of_old_row")):
        now = _inputs(turns=[_settled_turn("t", 1_000, LATEST + 9_000, outcome)], rows=rows)
        assert _asked(recorded, now, stored, 5) == wanted, outcome
    # With its rows from the boundary on only, an interrupted turn is let through.
    recorded = carry.record(before, carry=stored, boundary_seq=1, **WHERE)
    now = _inputs(turns=[_settled_turn("t", 1_000, LATEST + 9_000, "interrupted")], rows=rows)
    assert _asked(recorded, now, stored, 1) == ""
