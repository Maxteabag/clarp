"""Oracle v2 earcons: format of the synthesised cues."""
from __future__ import annotations

import array
import math

import pytest

from lib import oracle_earcons


@pytest.mark.parametrize("name", oracle_earcons.CUES)
def test_each_cue_is_short_quiet_24k_mono_pcm16(name):
    data = oracle_earcons.pcm(name, "Theo")
    assert isinstance(data, bytes) and len(data) % 2 == 0
    samples = array.array("h")
    samples.frombytes(data)
    seconds = len(samples) / oracle_earcons.RATE
    assert 0.12 <= seconds <= 0.40
    peak_dbfs = 20 * math.log10(max(abs(s) for s in samples) / 32767)
    assert -32 <= peak_dbfs <= -17.9
    # Soft attack and release: no click at either end.
    assert abs(samples[0]) < 50 and abs(samples[-1]) < 50


def test_cues_are_distinct_and_cached():
    rendered = {name: oracle_earcons.pcm(name) for name in oracle_earcons.CUES}
    assert len(set(rendered.values())) == len(rendered)
    assert oracle_earcons.pcm("result") is oracle_earcons.pcm("result")


def test_connected_chime_is_tinted_per_agent():
    assert oracle_earcons.pcm("connected", "Theo") != oracle_earcons.pcm("connected", "Marcus")
    assert oracle_earcons.tint("theo") == oracle_earcons.tint("Theo")


def test_unknown_cue_is_an_error():
    with pytest.raises(ValueError):
        oracle_earcons.pcm("fanfare")
