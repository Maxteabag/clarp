"""Characterization tests for lib.voice_verbosity (spoken narration levels)."""
from __future__ import annotations

import pytest

from lib import voice_verbosity as vv


@pytest.mark.parametrize("value,expected", [
    (0, 0), (1, 1), (2, 2), (3, 3),
    (-1, 0), (99, 3),
    ("2", 2), ("  3 ", 3), (2.9, 2), (True, 1),
    (None, 0), ("", 0), ("loud", 0), ([], 0), ("1.5", 0),
])
def test_clamp(value, expected):
    assert vv.clamp(value) == expected


def test_level_constants_are_contiguous_and_labelled():
    assert (vv.MIN_LEVEL, vv.MAX_LEVEL, vv.DEFAULT_LEVEL) == (0, 3, 0)
    assert list(vv.LABELS) == [0, 1, 2, 3]
    assert set(vv._CLAUSES) == set(vv.LABELS)


def test_quiet_contributes_nothing():
    assert vv.narration_clause(0) == ""
    assert vv.narration_clause(None) == ""
    assert vv.narration_clause("garbage") == ""


@pytest.mark.parametrize("level,prefix", [
    (1, "Narration level: milestones."),
    (2, "Narration level: steps."),
    (3, "Narration level: running commentary."),
    (7, "Narration level: running commentary."),  # clamped
])
def test_narration_clause_prefix_and_speak_tag(level, prefix):
    clause = vv.narration_clause(level)
    assert clause.startswith(prefix)
    assert "<speak>" in clause


def test_options_catalogue_shape():
    assert vv.options() == [
        {"level": 0, "label": "Quiet"},
        {"level": 1, "label": "Milestones"},
        {"level": 2, "label": "Steps"},
        {"level": 3, "label": "Running commentary"},
    ]
