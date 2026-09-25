"""Characterization tests for lib.text_util.truncate."""
from __future__ import annotations

import pytest

from lib.text_util import truncate


@pytest.mark.parametrize("value,n,expected", [
    ("", 5, ""),
    ("abc", 5, "abc"),
    ("abcde", 5, "abcde"),
    ("abcdef", 5, "abcde…"),
    ("x" * 600, 600, "x" * 600),
    ("x" * 601, 600, "x" * 600 + "…"),
    ("日本語テキスト", 3, "日本語…"),          # characters, not bytes
    ("abc", 0, "…"),
])
def test_truncate(value, n, expected):
    assert truncate(value, n) == expected


def test_default_limit_is_600():
    assert truncate("y" * 601) == "y" * 600 + "…"


@pytest.mark.parametrize("value", [None, 12, b"bytes", ["a"], {"a": 1}])
def test_non_strings_become_empty(value):
    assert truncate(value) == ""
