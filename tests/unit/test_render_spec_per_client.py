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


def test_the_table_takes_whole_numbers_per_client_name(monkeypatch):
    lines = []
    monkeypatch.setattr("lib.log.log", lambda event, detail="", *a, **k: lines.append(event))
    table = {"ios": 0, "android": 30, "bad name!": 5, "web": "x", "desk": True}
    assert config._render_spec_minimums({"render_spec_min_contract": table}) == {"ios": 0, "android": 30}
    assert lines.count("config.render_spec_min_contract") == 3
    assert config._render_spec_minimums({}) is None
