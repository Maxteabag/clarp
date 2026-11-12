"""The carried-inputs guard's own words (lib.message_carry).

What each rule lets through, and what it must not, is shown with real
imports in tests/readmodel/test_rm_n14_carry.py. Here: the cases no lane
reaches, and that the switches are the rules.
"""
from __future__ import annotations

from lib import message_carry as carry
from lib import transcript_incremental as inc

SESSION = "bs-1"


def _row(message_id, *, session=SESSION, source="transcript.jsonl", role="assistant", text="x",
         seq=0, trace=None):
    return {"message_id": message_id, "backend_session_id": session, "source_file": source,
            "role": role, "text": text, "seq": seq, "trace_id": trace}


def _turn(trace, started, outcome=None):
    return {"trace_id": trace, "started_at": started, "settled_at": None, "outcome": outcome}


def _inputs(*, turns=(), rows=(), user=("", "user", ""), oracle=()):
    return (list(turns), [], user, list(rows), list(oracle))


def _changed(expected, current, rules=carry.RULES, **stored):
    return carry.changed(expected, current, rules=frozenset(rules), carry=stored,
                         boundary_seq=5, source_file="transcript.jsonl",
                         backend_session_id=SESSION)


def test_identical_rows_stand_under_any_rules():
    rows = _inputs(turns=[_turn("t", 1)], rows=[_row("m-1")])
    assert _changed(rows, _inputs(turns=[_turn("t", 1)], rows=[_row("m-1")]), rules=()) == ""
    assert _changed(rows, rows) == ""


def test_rows_that_were_never_recorded_certify_nothing():
    recorded = _inputs(rows=[_row("m-1")])
    unrecorded = (recorded[0], recorded[1], recorded[2], None, recorded[4])
    assert _changed(unrecorded, recorded) == "not_recorded"
    assert _changed(recorded, unrecorded) == "not_recorded"


def test_with_no_rule_every_kind_of_change_is_named():
    before = _inputs(turns=[_turn("t", 1)], rows=[_row("m-1")])
    after = _inputs(
        turns=[_turn("t", 1), _turn("later", 10**9)], user=("u-new", "user", ""),
        oracle=[{"delegation_id": 1}],
        rows=[_row("m-1"), _row("u-new", source="client:new", role="user", text="new words"),
              _row("live-1", source="live:t"), _row("final-1", source="final:t"),
              _row("m-9", session="bs-2"), _row("n-1", source="marker:1")])
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
