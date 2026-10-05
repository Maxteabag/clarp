"""Recorded live streams (contract/live/*.json) are the shared test of
docs/live-items.md: every client feeds the same steps through its reducer
and must reach the expected state. Here the steps are checked against
contract/schemas/live.json and replayed through the Host's reference
reducer, so a fixture cannot describe a stream the Host would not send or a
state the rules do not produce."""
from __future__ import annotations

import json
import pathlib
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from schema_check import validate  # noqa: E402
from lib.live_items import LiveView  # noqa: E402

SCHEMA = json.loads((REPO / "contract/schemas/live.json").read_text())
FIXTURES = sorted((REPO / "contract/live").glob("*.json"))


def _subset(actual, expected, path="$"):
    if isinstance(expected, dict):
        assert isinstance(actual, dict), f"{path}: expected an object, got {actual!r}"
        for key, value in expected.items():
            assert key in actual, f"{path}.{key} missing"
            _subset(actual[key], value, f"{path}.{key}")
    else:
        assert actual == expected, f"{path}: {actual!r} != {expected!r}"


def test_live_fixtures_exist():
    names = {path.stem for path in FIXTURES}
    assert {"turn-full", "gap-needs-snapshot", "gap-recovers-from-snapshot",
            "new-turn-replaces-items", "stopped-mid-tool", "killed-without-stop",
            "new-prompt-over-unsettled-turn"} <= names


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.stem)
def test_live_fixture_steps_match_the_schema(path):
    body = json.loads(path.read_text())
    assert body["area"] == "live" and body["title"]
    for step in body["steps"]:
        (name, payload), = step.items()
        assert name in {"snapshot", "event"}, step
        if name == "snapshot":
            validate(payload, SCHEMA["$defs"]["snapshot"], SCHEMA)
        else:
            validate(payload, SCHEMA["$defs"]["live-event"], SCHEMA)


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.stem)
def test_live_fixture_reaches_the_expected_state(path):
    body = json.loads(path.read_text())
    view = LiveView()
    effects: list[str] = []
    for step in body["steps"]:
        (name, payload), = step.items()
        if name == "snapshot":
            view.apply_snapshot(payload)
        else:
            effects.extend(view.apply_event(payload))
    expect = body["expect"]
    assert effects == expect["effects"]
    assert view.lseq == expect["lseq"]
    _subset(view.activity, expect["activity"], "$.activity")
    _subset(view.turn, expect["turn"], "$.turn")
    items = view.items()
    assert [item["id"] for item in items] == [item["id"] for item in expect["items"]]
    for actual, expected in zip(items, expect["items"]):
        _subset(actual, expected, f"$.items[{expected['id']}]")
    for item in items:
        validate(item, SCHEMA["$defs"]["item"], SCHEMA)
