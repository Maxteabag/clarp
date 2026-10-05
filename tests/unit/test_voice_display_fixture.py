"""The shared voice-display fixture (contract/fixtures/voice-display.json):
the written text left once <vox> fillers are hidden. Web, desktop and iOS run
the same cases; the Host never renders a still-streaming reply, so it skips
those."""
from __future__ import annotations

import json
import pathlib

import pytest

from lib.voice_markup import clean_for_display

FIXTURE = pathlib.Path(__file__).resolve().parents[2] / "contract/fixtures/voice-display.json"
CASES = [case for case in json.loads(FIXTURE.read_text())["cases"]
         if not case.get("streaming")]


@pytest.mark.parametrize("case", CASES, ids=[case["name"] for case in CASES])
def test_voice_display_fixture(case):
    assert clean_for_display(case["input"]) == case["expect"]
