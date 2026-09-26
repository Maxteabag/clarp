"""The helper lifecycle policy as a table (rule 6: policies are pure)."""
from __future__ import annotations

import pytest

from lib.policies import helper_state as p

S, E = p.HelperState, p.HelperEvent

# (current, event) -> (new state, completed stamp) or None for "ignored".
CASES = [
    (S.RUNNING, E.REPORTED, (S.REPORTED, False)),
    (S.REPORTED, E.REPORTED, None),
    (S.REPORTED, E.PARENT_PROMPTED, (S.RUNNING, False)),
    (S.RUNNING, E.PARENT_PROMPTED, None),
    (S.RUNNING, E.MARKED_DONE, (S.DONE, True)),
    (S.REPORTED, E.MARKED_DONE, (S.DONE, True)),
    (S.FAILED, E.MARKED_DONE, (S.DONE, True)),
    (S.ABANDONED, E.MARKED_DONE, (S.DONE, True)),
    (S.DONE, E.MARKED_DONE, None),
    (S.DONE, E.MARKED_RUNNING, (S.RUNNING, False)),
    (S.FAILED, E.MARKED_RUNNING, (S.RUNNING, False)),
    (S.ABANDONED, E.MARKED_RUNNING, (S.RUNNING, False)),
    (S.REPORTED, E.MARKED_RUNNING, (S.RUNNING, False)),
    (S.RUNNING, E.MARKED_RUNNING, None),
    (S.RUNNING, E.FAILED, (S.FAILED, True)),
    (S.REPORTED, E.FAILED, (S.FAILED, True)),
    (S.DONE, E.FAILED, None),
    (S.RUNNING, E.PARENT_GONE, (S.ABANDONED, True)),
    (S.REPORTED, E.PARENT_GONE, (S.ABANDONED, True)),
    (S.DONE, E.PARENT_GONE, None),
    (S.FAILED, E.PARENT_GONE, None),
    (S.ABANDONED, E.PARENT_GONE, None),
    (S.DONE, E.REPORTED, None),
]


@pytest.mark.parametrize("current,event,expected", CASES)
def test_transition_table(current, event, expected):
    step = p.next_state(current, event)
    assert (None if step is None else (step.state, step.completed)) == expected


def test_every_state_event_pair_is_decided():
    decided = {(c, e) for c, e, _ in CASES}
    for state in S:
        for event in E:
            step = p.next_state(state, event)
            if (state, event) in p.TRANSITIONS:
                assert step is not None
            else:
                assert step is None
    assert set(p.TRANSITIONS) <= decided


@pytest.mark.parametrize("current,event", [
    (None, E.REPORTED), ("", E.MARKED_DONE), ("bogus", E.REPORTED), (S.RUNNING, "bogus"),
])
def test_non_helpers_and_unknown_values_are_ignored(current, event):
    assert p.next_state(current, event) is None


@pytest.mark.parametrize("raw,expected", [
    (None, p.Role.AGENT), ("", p.Role.AGENT), ("agent", p.Role.AGENT),
    ("Helper", p.Role.HELPER), (" helper ", p.Role.HELPER),
    ("janitor", None), ("boss", None),
])
def test_parse_role(raw, expected):
    assert p.parse_role(raw) == expected


def test_initial_state():
    assert p.initial_state("helper") == S.RUNNING
    assert p.initial_state("agent") is None
    assert p.initial_state("janitor") is None


@pytest.mark.parametrize("child,parent,ancestors,expected", [
    ("", "p1", (), ""),                 # a brand-new agent can take any parent
    ("c1", "", (), ""),                 # no parent
    ("c1", "c1", (), "self_parent"),
    ("c1", "p1", ("g1", "c1"), "parent_cycle"),
    ("c1", "p1", ("g1",), ""),
])
def test_parent_refusal(child, parent, ancestors, expected):
    assert p.parent_refusal(child, parent, ancestors) == expected


def test_archive_due_picks_only_done_helpers_past_the_grace():
    now, grace = 10_000, 1_000
    rows = [
        {"agent_id": "old", "role": "helper", "helper_state": "done",
         "helper_completed_at": 8_000, "archived_at": None},
        {"agent_id": "edge", "role": "helper", "helper_state": "done",
         "helper_completed_at": 9_000, "archived_at": None},
        {"agent_id": "fresh", "role": "helper", "helper_state": "done",
         "helper_completed_at": 9_500, "archived_at": None},
        {"agent_id": "reported", "role": "helper", "helper_state": "reported",
         "helper_completed_at": None, "archived_at": None},
        {"agent_id": "failed", "role": "helper", "helper_state": "failed",
         "helper_completed_at": 1, "archived_at": None},
        {"agent_id": "archived", "role": "helper", "helper_state": "done",
         "helper_completed_at": 1, "archived_at": 5},
        {"agent_id": "agent", "role": "agent", "helper_state": "done",
         "helper_completed_at": 1, "archived_at": None},
    ]
    assert p.archive_due(rows, now_ms=now, grace_ms=grace) == ["old", "edge"]
