"""The Host answers the same on every interpreter a Host runs or will run (R90): CPython 3.12, 3.13
and 3.14 accept different event times and read a name ending in a dot differently. Where they
disagree, the Host's own rule decides. This file passes alike on 3.12.14, 3.13.16 and 3.14.8."""
from __future__ import annotations

import copy

import pytest

from lib import agent_ring_live as live
from lib import media_store
from lib.group_calls import CallError
from tests.unit.test_agent_ring_live import OPENING, _context

ACCEPTED = [
    "2026-11-19T06:38:00+01:00",
    "2026-11-19T06:38:00.123456+01:00",
    "2026-11-19T06:38:00.000Z",                 # leak-watch: isoformat milliseconds with Z
    "2026-11-19T06:38:00Z",
    "2026-11-19T06:38:00.123456789Z",
    "2026-11-19T06:38:00.1Z",
    "2026-11-19T06:38:00-00:00",
    "0001-01-01T00:00:00Z",
    "9999-12-31T23:59:59.999999999+23:59",
]
REFUSED = {
    "a comma fraction": "2026-11-19T06:38:00,123456789+01:00",
    "a space for the T": "2026-11-19 06:38:00+01:00",
    "a zone without its colon": "2026-11-19T06:38:00+0100",
    "a zone of hours only": "2026-11-19T06:38:00+01",
    "no seconds": "2026-11-19T06:38+01:00",
    "the basic form": "20261119T063800Z",
    "a lower-case t": "2026-11-19t06:38:00Z",
    "a lower-case z": "2026-11-19T06:38:00z",
    "an empty fraction": "2026-11-19T06:38:00.+01:00",
    "ten fraction digits": "2026-11-19T06:38:00.1234567890Z",
    "hour 24": "2026-11-19T24:00:00Z",
    "second 60": "2026-11-19T06:38:60Z",
    "the 30th of February": "2026-02-30T06:38:00Z",
    "year 0000": "0000-01-01T00:00:00Z",
    "zone hour 24": "2026-11-19T06:38:00+24:00",
    "a fraction on the zone": "2026-11-19T06:38:00+01:00.5",
    "non-ASCII digits": "٢٠٢٦-11-19T06:38:00Z",
    "a leading space": " 2026-11-19T06:38:00Z",
    "a trailing space": "2026-11-19T06:38:00Z ",
    "no zone": "2026-11-19T06:38:00",
}


def _with_time(text):
    context = copy.deepcopy(_context())
    context["event"]["time"] = text
    return context


@pytest.mark.parametrize("text", ACCEPTED)
def test_an_event_time_in_the_grammar_is_accepted(text):
    live.validate_context(_with_time(text), OPENING)


@pytest.mark.parametrize("name", sorted(REFUSED))
def test_an_event_time_outside_the_grammar_is_refused(name):
    with pytest.raises(CallError) as refused:
        live.validate_context(_with_time(REFUSED[name]), OPENING)
    assert (refused.value.status, refused.value.error) == (400, live.CONTEXT_INVALID)


@pytest.mark.parametrize("name, mime, expected", [
    ("photo.", "image/png", ".png"),               # 3.14 gave '.': a name ending in a dot has none
    ("report.json.", "application/json", ".json"),
    ("photo.png", "image/png", ".png"),
])
def test_a_name_ending_in_a_dot_has_no_extension(name, mime, expected):
    assert media_store._extension(name, mime) == expected

