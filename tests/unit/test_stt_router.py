"""stt_router: the pure engine decision transcribe uses outside a call (S7 step 6).
End-to-end behaviour is pinned by test_stt_engine_plan_goldens.py."""
from __future__ import annotations

import pytest

from lib import stt_router


def plan(**kw):
    args = {"requested_model": "", "agent_engines": [], "cloud_engine": "", "long_model": "", "in_call": False}
    args.update(kw)
    return stt_router.plan_engines(**args)


@pytest.mark.parametrize("kw,expected", [
    ({}, ("", None, (), None)),
    ({"requested_model": "faster-whisper:small"}, ("faster-whisper:small", None, (), None)),
    ({"agent_engines": ["deepgram:nova-3"], "requested_model": "faster-whisper:small"},
     ("deepgram:nova-3", "deepgram:nova-3", (), None)),
    ({"agent_engines": ["a", "b", "c"]}, ("a", "a", ("b", "c"), None)),
    ({"cloud_engine": "deepgram:nova-3"}, ("deepgram:nova-3", None, (), None)),
    ({"cloud_engine": "deepgram:nova-3", "requested_model": "x"}, ("x", None, (), None)),
    ({"long_model": "big", "requested_model": "x"}, ("big", None, (), "x")),
    ({"long_model": "big"}, ("big", None, (), "")),
    ({"long_model": "big", "agent_engines": ["a"]}, ("a", "a", (), None)),
    ({"long_model": "x", "requested_model": "x"}, ("x", None, (), None)),
    ({"long_model": "big", "cloud_engine": "c", "in_call": True, "requested_model": "r"}, ("r", None, (), None)),
    ({"agent_engines": ["", "b"], "requested_model": "r"}, ("r", "", ("b",), None)),
])
def test_plan(kw, expected):
    p = plan(**kw)
    assert (p.model, p.agent_engine, p.agent_fallbacks, p.escalated_from) == expected


@pytest.mark.parametrize("kw,wanted", [
    ({}, True),
    ({"requested_model": "x"}, False),
    ({"agent_engines": ["a"]}, False),
    ({"agent_engines": [""]}, True),
    ({"in_call": True}, False),
])
def test_the_cloud_setting_is_read_only_when_it_could_be_used(kw, wanted):
    args = {"requested_model": "", "agent_engines": [], "in_call": False}
    args.update(kw)
    assert stt_router.wants_cloud_engine(**args) is wanted
