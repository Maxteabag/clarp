"""Characterization tests for lib.elevenlabs_voices (server-side voice list).

Pins: no key -> [], language filter, name normalisation, description
assembly, case-insensitive sort, the 15-minute cache and `force`, and that
transport errors degrade to []. The HTTP call is faked.
"""
from __future__ import annotations

import json
import urllib.error
from types import SimpleNamespace

import pytest

from lib import elevenlabs_voices as ev


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return json.dumps(self.payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture(autouse=True)
def _reset_cache():
    with ev._lock:
        ev._cache = None
    yield
    with ev._lock:
        ev._cache = None


@pytest.fixture
def key(monkeypatch):
    monkeypatch.setattr(ev.config, "load", lambda: SimpleNamespace(eleven_key=lambda: "xi-key"))


@pytest.fixture
def fetch(monkeypatch):
    state = {"payload": {"voices": []}, "calls": []}

    def urlopen(request, timeout):
        state["calls"].append((request.full_url, request.get_header("Xi-api-key"), timeout))
        return _Response(state["payload"])

    monkeypatch.setattr(ev.urllib.request, "urlopen", urlopen)
    return state


def test_no_key_returns_empty_without_fetching(monkeypatch, fetch):
    monkeypatch.setattr(ev.config, "load", lambda: SimpleNamespace(eleven_key=lambda: ""))
    assert ev.english_voices() == []
    assert fetch["calls"] == []


def test_fetch_uses_key_header_and_timeout(key, fetch):
    ev.english_voices()
    assert fetch["calls"] == [("https://api.elevenlabs.io/v1/voices", "xi-key", 15)]


def test_rows_are_filtered_named_described_and_sorted(key, fetch):
    fetch["payload"] = {"voices": [
        {"voice_id": "v-sarah", "name": "Sarah - Warm", "labels": {
            "accent": "american", "gender": "female", "age": "young",
            "use_case": "narration", "description": "soft"}},
        {"voice_id": "v-de", "name": "Anton", "labels": {"language": "de"}},
        {"voice_id": "v-en-gb", "name": "alfie", "labels": {"language": "en-GB"}},
        {"voice_id": "", "name": "Ghost"},
        {"voice_id": "v-noname", "labels": None},
        {"voice_id": "v-Bella", "name": "Bella", "labels": {"accent": "  ", "gender": "female"}},
    ]}
    rows = ev.english_voices()
    assert rows == [
        ("v-en-gb", "alfie", ""),
        ("v-Bella", "Bella", "female"),
        ("v-sarah", "Sarah", "american, female, young, narration, soft"),
        ("v-noname", "v-noname", ""),
    ]


def test_description_truncated_to_120(key, fetch):
    fetch["payload"] = {"voices": [{"voice_id": "v", "name": "N",
                                    "labels": {"description": "d" * 300}}]}
    [(_, _, description)] = ev.english_voices()
    assert len(description) == 120


def test_missing_voices_key_is_empty(key, fetch):
    fetch["payload"] = {"voices": None}
    assert ev.english_voices() == []


def test_result_is_cached_and_force_refetches(key, fetch, monkeypatch):
    fetch["payload"] = {"voices": [{"voice_id": "a", "name": "A"}]}
    first = ev.english_voices()
    fetch["payload"] = {"voices": [{"voice_id": "b", "name": "B"}]}
    assert ev.english_voices() == first
    assert len(fetch["calls"]) == 1
    assert ev.english_voices(force=True) == [("b", "B", "")]
    assert len(fetch["calls"]) == 2


def test_cache_expires_after_window(key, fetch, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(ev.time, "monotonic", lambda: now[0])
    ev.english_voices()
    now[0] += ev._CACHE_SECONDS - 1
    ev.english_voices()
    assert len(fetch["calls"]) == 1
    now[0] += 2
    ev.english_voices()
    assert len(fetch["calls"]) == 2


def test_cached_list_is_a_copy(key, fetch):
    fetch["payload"] = {"voices": [{"voice_id": "a", "name": "A"}]}
    rows = ev.english_voices()
    rows.append(("z", "Z", ""))
    assert ev.english_voices() == [("a", "A", "")]


@pytest.mark.parametrize("exc", [urllib.error.URLError("down"), OSError("io"), ValueError("json")])
def test_transport_errors_return_empty_and_do_not_cache(key, monkeypatch, exc):
    calls = []

    def urlopen(request, timeout):
        calls.append(1)
        raise exc

    monkeypatch.setattr(ev.urllib.request, "urlopen", urlopen)
    assert ev.english_voices() == []
    assert ev.english_voices() == []
    assert len(calls) == 2  # a failure is not cached; next call retries
