"""render_spec is drawn per client (issue 428): each client's contract numbers are its own series.
iOS from 74; every other client name, an unknown or absent one included, never, until its owner
names its number. The name is matched exactly."""
from __future__ import annotations

import pytest

from lib import config, server_identity


def test_the_default_is_ios_from_74_and_no_other_client():
    assert server_identity.RENDER_SPEC_MIN_CONTRACT == {"ios": 74}


@pytest.mark.parametrize("header,capable", [
    ("ios/2981 contract=74", True), ("ios/2990 contract=75", True), ("ios/2980 contract=73", False),
    ("android/500 contract=74", False), ("android/500 contract=9999", False),
    ("desktop/1 contract=200", False), ("web/1 contract=200", False), ("watch/1 contract=200", False),
    ("iosx/1 contract=74", False), ("ios-beta/1 contract=74", False), ("IOS/1 contract=74", False),
    ("/1 contract=74", False), ("ios/1", False), (None, False), ("", False),
])
def test_only_a_client_named_with_its_number_draws_it(header, capable):
    assert server_identity.render_spec_capable(header) is capable


def test_a_host_overrides_per_client_in_both_directions():
    assert not server_identity.render_spec_capable("ios/2981 contract=74", {"ios": 0})       # off
    assert server_identity.render_spec_capable("android/500 contract=30", {"android": 30})  # on
    assert server_identity.render_spec_capable("ios/2981 contract=74", {"android": 30})     # the rest stays
    assert not server_identity.render_spec_capable("ios/2981 contract=74", 80)              # a number is iOS's
    assert not server_identity.render_spec_capable("android/500 contract=99", 74)


def test_the_old_single_number_is_ios_only_and_said_so(monkeypatch):
    lines = []
    monkeypatch.setattr("lib.log.log", lambda event, detail="", *a, **k: lines.append(event))
    assert config._render_spec_minimums({"render_spec_min_client": 74}) == {"ios": 74}
    assert lines == ["config.render_spec_min_client"]


def test_the_table_refuses_a_malformed_key_or_value_in_one_line_naming_the_keys(monkeypatch):
    lines = []
    monkeypatch.setattr("lib.log.log", lambda event, detail="", *a, **k: lines.append((event, detail)))
    table = {"ios": 0, "android": 30, "bad name!": 5, "web": "x", "desk": True, "9lives": 3, "x" * 33: 1}
    assert config._render_spec_minimums({"render_spec_min_contract": table}) == {"ios": 0, "android": 30}
    assert len(lines) == 1 and lines[0][0] == "config.render_spec_min_contract"
    for key in ("bad name!", "web", "desk", "9lives", "x" * 33):
        assert repr(key) in lines[0][1]
    assert "'ios'" not in lines[0][1] and "'android'" not in lines[0][1]
    assert config._render_spec_minimums({}) is None


def test_a_name_is_matched_exactly_as_the_client_sends_it():
    """One rule for a client's name in every per-client table (server_identity.client_minimums):
    the header name's own form, capitals included, matched exactly."""
    table = server_identity.client_minimums({"Desktop": 5}, setting="test")
    assert table == {"Desktop": 5}
    assert server_identity.render_spec_capable("Desktop/1 contract=5", table)
    assert not server_identity.render_spec_capable("desktop/1 contract=5", table)
    assert not server_identity.render_spec_capable("DESKTOP/1 contract=5", table)
    assert not server_identity.render_spec_capable("Desktop/1 contract=4", table)


def test_a_key_differing_from_a_built_in_name_only_in_case_is_another_client_and_said_once(monkeypatch):
    lines = []
    monkeypatch.setattr("lib.log.log", lambda event, detail="", *a, **k: lines.append((event, detail)))
    table = config._render_spec_minimums({"render_spec_min_contract": {"IOS": 1, "Android": 2}})
    assert table == {"IOS": 1, "Android": 2}
    assert len(lines) == 1 and lines[0][0] == "config.render_spec_min_contract"
    assert "'IOS' is taken as its own client" in lines[0][1] and "the built-in 'ios' keeps its own number (74)" in lines[0][1]
    assert "'Android' is taken as its own client" in lines[0][1] and "the built-in 'android' keeps its own setting" in lines[0][1]
    assert server_identity.render_spec_capable("IOS/1 contract=1", table)            # another client
    assert not server_identity.render_spec_capable("ios/2980 contract=73", table)    # ios keeps 74
    assert server_identity.render_spec_capable("ios/2981 contract=74", table)


def test_the_strict_reader_gives_the_name_as_sent_and_the_number_of_its_own_match():
    read = server_identity.strict_client
    assert read("Desktop/1.2 contract=000000074") == ("Desktop", 74)
    for header in ("ios/1 contract=74 ", " ios/1 contract=74", "ios /1 contract=74", "ios/1  contract=74",
                   "ios/1\tcontract=74", "ios/1 contract=74\n", "ios/1 contract=74x", "ios/1 contract=0740000000",
                   "i\u0130s/1 contract=74", "ios\x00/1 contract=74", "ios/1", None, 74):
        assert read(header) is None, header
    assert server_identity.render_spec_capable("ios/2981 contract=074")


def test_a_minimum_of_nought_is_never():
    assert not server_identity.render_spec_capable("Desktop/1 contract=99", {"Desktop": 0})


def test_the_table_wins_over_the_old_key_and_says_so(monkeypatch):
    lines = []
    monkeypatch.setattr("lib.log.log", lambda event, detail="", *a, **k: lines.append((event, detail)))
    got = config._render_spec_minimums({"render_spec_min_client": 0, "render_spec_min_contract": {"ios": 80}})
    assert got == {"ios": 80}
    assert ("config.render_spec_min_client", "ignored: render_spec_min_contract names ios") in lines


@pytest.mark.parametrize("value", [True, 74.0, "74", -1])
def test_a_minimum_that_is_not_a_whole_number_is_refused(monkeypatch, value):
    monkeypatch.setattr("lib.log.log", lambda *a, **k: None)
    assert config._render_spec_minimums({"render_spec_min_contract": {"ios": value}}) is None


def test_an_empty_table_leaves_the_default(monkeypatch):
    monkeypatch.setattr("lib.log.log", lambda *a, **k: None)
    assert config._render_spec_minimums({"render_spec_min_contract": {}}) is None


def test_both_per_client_tables_are_read_by_the_one_rule(monkeypatch):
    """render_spec's table and the live model's go through server_identity.client_minimums, and both
    readers of the header through strict_client: one rule, one place."""
    from lib import agent_ring_live
    seen = []
    real = server_identity.client_minimums
    monkeypatch.setattr(server_identity, "client_minimums",
                        lambda table, **kw: seen.append(kw["setting"]) or real(table, **kw))
    config._render_spec_minimums({"render_spec_min_contract": {"Desktop": 5}})
    config._live_model_min_client({"Desktop": 5})
    assert seen == ["config.render_spec_min_contract", "config.live_model_min_client"]
    read = []
    real_strict = server_identity.strict_client
    monkeypatch.setattr(server_identity, "strict_client", lambda header: read.append(header) or real_strict(header))
    server_identity.render_spec_capable("ios/1 contract=74")
    agent_ring_live.model_for_client("ios/1 contract=74")
    assert read == ["ios/1 contract=74", "ios/1 contract=74"]
