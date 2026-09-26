"""ServerContext is frozen; four services swap in place through one lock."""
from __future__ import annotations

import dataclasses
import threading

import pytest

from lib.audio_stream import AudioStream
from lib.context import REPLACEABLE_SERVICES, ServerContext, StubSTT
from lib.tts_engine import FakeTTSEngine


def _ctx(tmp_path) -> ServerContext:
    return ServerContext(
        root=tmp_path, static=tmp_path, audio_dir=tmp_path, agents_path=tmp_path / "agents.json",
        default_session="mike", tts=FakeTTSEngine(tmp_path), stream=AudioStream(tmp_path),
        stt=StubSTT(text="first"), roster_names=("Mike",))


def test_configuration_fields_refuse_assignment(tmp_path):
    ctx = _ctx(tmp_path)
    with pytest.raises(dataclasses.FrozenInstanceError):
        ctx.auth_token = "secret"
    with pytest.raises(dataclasses.FrozenInstanceError):
        ctx.replace_service("auth_token", "secret")
    assert ctx.auth_token == ""


def test_with_copies_configuration_and_shares_services(tmp_path):
    ctx = _ctx(tmp_path)
    changed = ctx.with_(auth_token="secret")
    assert (ctx.auth_token, changed.auth_token) == ("", "secret")
    assert changed.stt is ctx.stt and changed.stream is ctx.stream
    assert changed.clip_broker is ctx.clip_broker


def test_services_swap_in_place_for_every_holder(tmp_path):
    ctx = _ctx(tmp_path)
    holder = ctx  # the dispatch service and workers keep this same reference
    first = ctx.stt
    second = StubSTT(text="second")
    assert ctx.replace_stt(second) is first
    assert holder.stt is second
    herald, explanations = object(), object()
    ctx.install_herald(herald)
    ctx.install_tool_explanations(explanations)
    assert (holder.herald, holder.tool_explanations) == (herald, explanations)
    assert REPLACEABLE_SERVICES == {"stt", "tool_explanations", "herald", "runtime_client"}


def test_service_assignment_is_refused_and_names_the_replacement_path(tmp_path):
    ctx = _ctx(tmp_path)
    with pytest.raises(dataclasses.FrozenInstanceError, match="replace_service"):
        ctx.stt = StubSTT(text="swapped")


def test_concurrent_swaps_each_return_a_distinct_predecessor(tmp_path):
    ctx = _ctx(tmp_path)
    replacements = [StubSTT(text=str(i)) for i in range(32)]
    previous: list = []
    lock = threading.Lock()

    def swap(stt):
        old = ctx.replace_stt(stt)
        with lock:
            previous.append(old)

    threads = [threading.Thread(target=swap, args=(stt,)) for stt in replacements]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # Every value but the final one was handed back exactly once.
    assert len({id(p) for p in previous}) == 32
    assert ctx.stt in replacements and ctx.stt not in previous
