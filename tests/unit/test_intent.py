"""Tests for the strict-regex permission-intent classifier."""
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "server"))
from lib.intent import classify_intent  # noqa: E402


# ---------- positive grants ----------

def test_affirmative_with_name_grants():
    assert classify_intent("sure rachel", ["Rachel"]).grants == ["Rachel"]
    assert classify_intent("yeah, go ahead rachel", ["Rachel"]).grants == ["Rachel"]
    assert classify_intent("ok rachel tell me", ["Rachel"]).grants == ["Rachel"]


def test_name_then_interrogative_grants():
    assert classify_intent("rachel what's up", ["Rachel"]).grants == ["Rachel"]
    assert classify_intent("Rachel, what is it?", ["Rachel"]).grants == ["Rachel"]
    assert classify_intent("rachel go", ["Rachel"]).grants == ["Rachel"]
    assert classify_intent("rachel tell me", ["Rachel"]).grants == ["Rachel"]


def test_trailing_question_after_name_grants():
    assert classify_intent("Rachel?", ["Rachel"]).grants == ["Rachel"]


def test_grant_works_for_each_candidate_independently():
    # the user addresses Rachel; Bella stays held.
    r = classify_intent("go ahead rachel", ["Rachel", "Bella"])
    assert r.grants == ["Rachel"]
    assert r.declines == []


# ---------- declines ----------

def test_decline_with_name():
    assert classify_intent("not now rachel", ["Rachel"]).declines == ["Rachel"]
    assert classify_intent("rachel hold on", ["Rachel"]).declines == ["Rachel"]


def test_bare_decline_with_single_pending_grants_decline():
    # Only Rachel is held → "not now" obviously refers to her.
    assert classify_intent("not now", ["Rachel"]).declines == ["Rachel"]
    assert classify_intent("later", ["Rachel"]).declines == ["Rachel"]


def test_bare_decline_with_multiple_pending_is_ambiguous():
    # Don't decline anyone we can't unambiguously match.
    r = classify_intent("not now", ["Rachel", "Bella"])
    assert r.declines == []


# ---------- mentions (no signal) ----------

def test_mention_only_does_not_grant():
    # User is talking ABOUT Rachel, not addressing her.
    r = classify_intent("I think rachel would know that, mike",
                        ["Rachel", "Mike"])
    assert r.grants == []
    assert r.declines == []


def test_directive_about_does_not_grant():
    # "Tell rachel I said hi" — mike is the addressee, not rachel.
    r = classify_intent("tell rachel I said hi", ["Rachel"])
    assert r.grants == []


def test_question_about_name_does_not_grant():
    r = classify_intent("what did rachel say about that earlier", ["Rachel"])
    assert r.grants == []


def test_unrelated_affirmative_without_name_no_signal_when_many_pending():
    r = classify_intent("yes that's great", ["Rachel", "Bella"])
    assert r.grants == []
    assert r.declines == []


def test_unmentioned_candidates_stay_silent():
    # Bella's herald is pending; user talks to Rachel.
    r = classify_intent("sure rachel", ["Rachel", "Bella"])
    assert r.grants == ["Rachel"]
    assert "Bella" not in r.grants
    assert "Bella" not in r.declines


# ---------- edge cases ----------

def test_empty_inputs():
    assert classify_intent("", ["Rachel"]).has_signal is False
    assert classify_intent("anything", []).has_signal is False


def test_case_insensitive():
    assert classify_intent("SURE RACHEL", ["rachel"]).grants == ["rachel"]
    assert classify_intent("rachel YES", ["RACHEL"]).grants == ["RACHEL"]


def test_punctuation_doesnt_block_match():
    assert classify_intent("Sure, Rachel!", ["Rachel"]).grants == ["Rachel"]
    assert classify_intent("Rachel: what's up?", ["Rachel"]).grants == ["Rachel"]


def test_has_signal_property():
    r = classify_intent("go ahead rachel", ["Rachel"])
    assert r.has_signal is True
    r = classify_intent("just a thought", ["Rachel"])
    assert r.has_signal is False


def test_grants_and_declines_can_coexist():
    # "go ahead rachel, but bella later"
    r = classify_intent("go ahead rachel but bella later",
                        ["Rachel", "Bella"])
    assert r.grants == ["Rachel"]
    assert r.declines == ["Bella"]
