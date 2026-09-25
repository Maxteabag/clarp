"""Characterization tests for lib.mcp_selection (per-agent MCP column codec).

Pins: the stored envelope shape, that the legacy default `'[]'` and any
malformed value decode as "never configured", and that only an explicit
`configured: true` counts.
"""
from __future__ import annotations

import json

import pytest

from lib import mcp_selection


def test_encode_shape():
    raw = mcp_selection.encode(["github", "filesystem"])
    assert json.loads(raw) == {"configured": True,
                               "servers": ["github", "filesystem"]}


def test_encode_empty_choice_is_still_configured():
    assert mcp_selection.decode(mcp_selection.encode([])) == (True, [])


def test_round_trip():
    servers = ["a", "b", "c"]
    assert mcp_selection.decode(mcp_selection.encode(servers)) == (True, servers)


@pytest.mark.parametrize("raw", [
    None,
    "",
    "[]",                                   # legacy column default
    "[\"github\"]",                         # bare list, no envelope
    "{}",
    '{"configured": false, "servers": ["x"]}',
    '{"configured": "true", "servers": ["x"]}',   # string, not bool
    '{"configured": 1, "servers": ["x"]}',        # int, not bool
    '{"servers": ["x"]}',
    "not json",
    "null",
    "42",
])
def test_decode_unconfigured_or_invalid(raw):
    assert mcp_selection.decode(raw) == (False, [])


@pytest.mark.parametrize("servers,expected", [
    ("github", []),          # non-list servers -> configured but empty
    (None, []),
    ({"a": 1}, []),
    ([1, 2], ["1", "2"]),    # items coerced to str
    (["x", None], ["x", "None"]),
])
def test_decode_configured_with_odd_servers(servers, expected):
    raw = json.dumps({"configured": True, "servers": servers})
    assert mcp_selection.decode(raw) == (True, expected)
