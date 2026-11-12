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
    desk = object()
    assert ctx.hotseat_desk is None and ctx.replace_service("hotseat_desk", desk) is None
    assert holder.hotseat_desk is desk
    assert REPLACEABLE_SERVICES == {"stt", "tool_explanations", "herald", "runtime_client", "hotseat_desk"}


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


def test_speak_announcement_async_closes_thread_local_db_handles_and_honours_sync_switch(tmp_path):
    import gc
    import os
    import sqlite3
    import time
    from lib import db, telemetry

    tracked_paths = {
        str((tmp_path / "state.sqlite").resolve()),
        str((tmp_path / "telemetry.sqlite").resolve()),
    }

    def _open_sqlite_fds() -> int:
        fd_dir = "/proc/self/fd"
        if not os.path.isdir(fd_dir):
            return 0
        count = 0
        for entry in os.listdir(fd_dir):
            try:
                target = os.readlink(f"{fd_dir}/{entry}")
            except OSError:
                continue
            if target in tracked_paths:
                count += 1
        return count

    seen_threads: list[str] = []
    lock = threading.Lock()

    class DbTouchingTTS:
        def synthesize(self, text: str, voice_id: str, session: str | None = None):
            db.conn().execute("SELECT 1").fetchone()
            telemetry.conn().execute("SELECT 1").fetchone()
            with lock:
                seen_threads.append(threading.current_thread().name)
            return tmp_path / "clip.mp3"

    # Warm main-thread connections + one off-thread WAL attach so SQLite's per-inode
    # pUnused/unixShmNode FD slot is initialized before measuring the steady baseline.
    db.conn().execute("SELECT 1").fetchone()
    telemetry.conn().execute("SELECT 1").fetchone()
    ctx = _ctx(tmp_path).with_(tts=DbTouchingTTS())

    def _drain(expected_count: int) -> None:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            live = [t for t in threading.enumerate() if t.name == "clarp-announce-tts"]
            if len(seen_threads) == expected_count and not live:
                return
            time.sleep(0.005)
        raise AssertionError(f"announcement threads did not drain: seen={len(seen_threads)}")

    ctx.speak_announcement_async("Warmup is ready.", "voice-1", session="mike")
    _drain(1)
    seen_threads.clear()
    gc.collect()
    baseline_fds = _open_sqlite_fds()
    baseline_conns = sum(1 for obj in gc.get_objects() if isinstance(obj, sqlite3.Connection))

    # 15 sequential announcements (matching repeated POST /agents requests): zero FD or Connection growth.
    for idx in range(15):
        ctx.speak_announcement_async(f"Agent {idx} is ready.", "voice-1", session="mike")
        _drain(idx + 1)

    gc.collect()
    assert seen_threads == ["clarp-announce-tts"] * 15
    if os.path.isdir("/proc/self/fd"):
        assert _open_sqlite_fds() == baseline_fds
    assert sum(1 for obj in gc.get_objects() if isinstance(obj, sqlite3.Connection)) == baseline_conns

    # Plain switch async_announcements=False runs inline on the caller's thread.
    seen_threads.clear()
    caller_name = threading.current_thread().name
    sync_ctx = ctx.with_(async_announcements=False)
    sync_ctx.speak_announcement_async("Mike is ready.", "voice-1", session="mike")
    assert seen_threads == [caller_name]
