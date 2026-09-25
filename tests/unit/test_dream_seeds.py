"""Characterization tests for lib.dream_seeds (dream seeding recipe axes).

Pins the strategy/dose tables, that every dose drawn for a strategy is one
the table allows, the offline fallback for foreign material, and the crude
keyword extraction. The Wikipedia fetch is faked; the conftest network
guard would refuse it anyway.
"""
from __future__ import annotations

import json
import random
import urllib.error

import pytest

from lib import dream_seeds as ds


def test_axis_constants():
    assert ds.STRATEGIES == ("control", "lenses", "foreign", "roleplay")
    assert ds.CONTEXT_DOSES == ("none", "fragments", "full")
    assert set(ds._DOSE_WEIGHTS) == set(ds.STRATEGIES)
    for options, weights in ds._DOSE_WEIGHTS.values():
        assert len(options) == len(weights)
        assert set(options) <= set(ds.CONTEXT_DOSES)
        assert abs(sum(weights) - 1.0) < 1e-9


def test_choose_strategy_is_deterministic_with_seeded_rng():
    a = [ds.choose_strategy(random.Random(7)) for _ in range(5)]
    b = [ds.choose_strategy(random.Random(7)) for _ in range(5)]
    assert a == b and set(a) <= set(ds.STRATEGIES)


def test_choose_strategy_without_rng_uses_global_random():
    assert ds.choose_strategy() in ds.STRATEGIES


@pytest.mark.parametrize("strategy,allowed", [
    ("control", {"full"}),
    ("lenses", {"full", "fragments"}),
    ("foreign", {"fragments", "none"}),
    ("roleplay", {"none", "fragments"}),
])
def test_choose_dose_only_draws_allowed_doses(strategy, allowed):
    rng = random.Random(1)
    drawn = {ds.choose_dose(strategy, rng) for _ in range(300)}
    assert drawn == allowed


def test_choose_dose_unknown_strategy_falls_back_to_full():
    assert ds.choose_dose("mystery", random.Random(3)) == "full"


def test_choose_dose_weights_bias_matches_table():
    rng = random.Random(42)
    draws = [ds.choose_dose("roleplay", rng) for _ in range(2000)]
    share_none = draws.count("none") / len(draws)
    assert 0.72 < share_none < 0.88  # table says 0.8


def test_banks_are_non_empty_and_unique():
    for bank in (ds.LENS_BANK, ds.ROLEPLAY_PERSONAS, ds._OFFLINE_FOREIGN, ds._CONSTRAINTS):
        assert len(bank) >= 8
        assert len(set(bank)) == len(bank)
        assert all(isinstance(x, str) and x.strip() for x in bank)


def test_random_foreign_material_uses_offline_pool_when_fetch_fails(monkeypatch):
    monkeypatch.setattr(ds, "_random_wikipedia_summary", lambda timeout=6.0: "")
    logged = []
    monkeypatch.setattr(ds, "log", lambda event, detail="": logged.append(event))
    text = ds.random_foreign_material(random.Random(5))
    subject, _, constraint = text.partition("\n\nARBITRARY CONSTRAINT: ")
    assert subject.startswith("UNRELATED SUBJECT: ")
    assert subject[len("UNRELATED SUBJECT: "):] in ds._OFFLINE_FOREIGN
    assert constraint in ds._CONSTRAINTS
    assert logged == ["dreamForeignOffline"]


def test_random_foreign_material_prefers_live_article(monkeypatch):
    monkeypatch.setattr(ds, "_random_wikipedia_summary",
                        lambda timeout=6.0: "Kintsugi — golden repair")
    text = ds.random_foreign_material(random.Random(5))
    assert text.startswith("UNRELATED SUBJECT: Kintsugi — golden repair\n\nARBITRARY CONSTRAINT: ")


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_wikipedia_summary_formats_title_and_normalises_extract(monkeypatch):
    seen = {}

    def urlopen(request, timeout):
        seen["url"] = request.full_url
        seen["ua"] = request.get_header("User-agent")
        seen["timeout"] = timeout
        return _Response({"title": "Termite", "extract": "Mounds  breathe\n\nby  design. " + "x" * 2000})

    monkeypatch.setattr(ds.urllib.request, "urlopen", urlopen)
    text = ds._random_wikipedia_summary(timeout=2.5)
    assert text.startswith("Termite — Mounds breathe by design. ")
    assert len(text) == len("Termite — ") + 900
    assert seen["url"] == "https://en.wikipedia.org/api/rest_v1/page/random/summary"
    assert seen["ua"].startswith("clarp-dreaming/")
    assert seen["timeout"] == 2.5


@pytest.mark.parametrize("payload,expected", [
    ({"title": "Only", "extract": ""}, "Only"),
    ({"title": " ", "extract": "text"}, ""),
    ({}, ""),
])
def test_wikipedia_summary_edge_payloads(monkeypatch, payload, expected):
    monkeypatch.setattr(ds.urllib.request, "urlopen", lambda r, timeout: _Response(payload))
    assert ds._random_wikipedia_summary() == expected


@pytest.mark.parametrize("exc", [urllib.error.URLError("down"), OSError("io"),
                                 ValueError("bad json"), TimeoutError()])
def test_wikipedia_summary_swallows_fetch_errors(monkeypatch, exc):
    def urlopen(r, timeout):
        raise exc

    monkeypatch.setattr(ds.urllib.request, "urlopen", urlopen)
    logged = []
    monkeypatch.setattr(ds, "log_exception", lambda event, e, detail="": logged.append(event))
    assert ds._random_wikipedia_summary() == ""
    assert logged == ["dreamForeignFetchFail"]


def test_session_keywords_ranks_by_count_then_alphabetically():
    snapshot = "sqlite sqlite Sqlite whisper whisper clarp the and this that Router"
    assert ds.session_keywords(snapshot) == ["sqlite", "whisper", "clarp", "router"]


def test_session_keywords_drops_stopwords_short_words_and_leading_digits():
    snapshot = "the and this 123abc ab abc four-word under_score"
    assert ds.session_keywords(snapshot) == ["four-word", "under_score"]
    # "23abc" is matched after the leading digit is skipped? No: the regex
    # requires a letter first, so "abc" (3 chars) is too short and dropped.


def test_session_keywords_empty_and_none():
    assert ds.session_keywords("") == []
    assert ds.session_keywords(None) == []


def test_session_keywords_samples_from_top_sixty_when_over_limit():
    words = " ".join(f"word{i:03d}" for i in range(100))
    picked = ds.session_keywords(words, limit=12, rng=random.Random(9))
    assert len(picked) == 12 and len(set(picked)) == 12
    # All counts tie, so the ranked pool is the first 60 alphabetically.
    assert all(int(w[4:]) < 60 for w in picked)
    assert picked == ds.session_keywords(words, limit=12, rng=random.Random(9))


def test_session_keywords_returns_whole_pool_when_within_limit():
    assert ds.session_keywords("alpha beta gamma", limit=12) == ["alpha", "beta", "gamma"]
