"""Integration test for the inotify-driven transcript dispatcher.

inotify is real OS infrastructure — we drive it for real, append to a
real file, and verify the dispatcher wakes up + emits text within a
tight latency budget. If this test ever fails, file-change push is
broken on the host kernel and we should fall back to polling.
"""
from __future__ import annotations

import json
import pathlib
import sys
import threading
import time

import pytest

_SERVER_DIR = pathlib.Path(__file__).resolve().parents[2] / "server"
sys.path.insert(0, str(_SERVER_DIR))

from lib.transcript_watcher import (   # noqa: E402
    InotifyDispatcher, TranscriptWatcher, WatcherPool,
)


WAKEUP_BUDGET_SEC = 0.5     # generous bound for sub-ms inotify latency


def _append(path: pathlib.Path, *entries: dict) -> None:
    with path.open("a") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")
            f.flush()


def _assistant_text(text: str) -> dict:
    return {"type": "assistant",
            "message": {"content": [{"type": "text", "text": text}]}}


def _wait_for(pred, *, timeout: float = WAKEUP_BUDGET_SEC,
              interval: float = 0.01):
    """Spin until pred() is truthy or timeout — used to wait for the
    dispatcher's daemon thread to fire."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        v = pred()
        if v:
            return v
        time.sleep(interval)
    return pred()


# ---- Tests --------------------------------------------------------------


def test_inotify_dispatcher_fires_when_file_grows(tmp_path):
    tx = tmp_path / "tx.jsonl"
    received: list[str] = []
    pool = WatcherPool()
    pool.add("agent-A", TranscriptWatcher(tx, on_text=received.append))

    disp = InotifyDispatcher(pool)
    disp.watch("agent-A", tx)
    disp.start()
    try:
        # Wait for the startup tick to settle (nothing to read yet).
        time.sleep(0.05)
        _append(tx, _assistant_text("hello inotify"))
        got = _wait_for(lambda: received[:])
        assert got == ["hello inotify"]
    finally:
        disp.stop()


def test_inotify_dispatcher_multiplexes_multiple_agents(tmp_path):
    """One dispatcher, two agents — each agent's text goes only to its
    own callback. Pin the no-cross-routing invariant at the push layer."""
    tx_a = tmp_path / "a.jsonl"
    tx_b = tmp_path / "b.jsonl"
    a_text: list[str] = []
    b_text: list[str] = []
    pool = WatcherPool()
    pool.add("agent-A", TranscriptWatcher(tx_a, on_text=a_text.append))
    pool.add("agent-B", TranscriptWatcher(tx_b, on_text=b_text.append))

    disp = InotifyDispatcher(pool)
    disp.watch("agent-A", tx_a)
    disp.watch("agent-B", tx_b)
    disp.start()
    try:
        time.sleep(0.05)
        _append(tx_a, _assistant_text("A speaks"))
        _append(tx_b, _assistant_text("B speaks"))
        _wait_for(lambda: a_text and b_text)
        assert a_text == ["A speaks"]
        assert b_text == ["B speaks"]
    finally:
        disp.stop()


def test_inotify_dispatcher_fires_per_text_block_not_per_turn(tmp_path):
    """The whole point of the Phase-B-progressive pivot: each text block
    fires the callback the instant it lands, not after the whole turn."""
    tx = tmp_path / "tx.jsonl"
    received: list[str] = []
    pool = WatcherPool()
    pool.add("agent-X", TranscriptWatcher(tx, on_text=received.append))

    disp = InotifyDispatcher(pool)
    disp.watch("agent-X", tx)
    disp.start()
    try:
        time.sleep(0.05)
        _append(tx, _assistant_text("first chunk"))
        _wait_for(lambda: len(received) >= 1)
        assert received == ["first chunk"]
        # Tool call lands between text blocks — no new text emitted.
        _append(tx, {"type": "assistant",
                     "message": {"content": [{"type": "tool_use",
                                              "name": "Bash"}]}})
        time.sleep(0.05)
        assert received == ["first chunk"]
        # Second text block.
        _append(tx, _assistant_text("second chunk"))
        _wait_for(lambda: len(received) >= 2)
        assert received == ["first chunk", "second chunk"]
    finally:
        disp.stop()


def test_inotify_dispatcher_unwatch_stops_emitting(tmp_path):
    tx = tmp_path / "tx.jsonl"
    received: list[str] = []
    pool = WatcherPool()
    pool.add("agent-X", TranscriptWatcher(tx, on_text=received.append))

    disp = InotifyDispatcher(pool)
    disp.watch("agent-X", tx)
    disp.start()
    try:
        time.sleep(0.05)
        _append(tx, _assistant_text("before"))
        _wait_for(lambda: received[:])
        assert received == ["before"]
        disp.unwatch("agent-X")
        # Even though the watcher is still in the pool, no kernel events
        # will route to it anymore.
        _append(tx, _assistant_text("after"))
        time.sleep(WAKEUP_BUDGET_SEC)
        assert received == ["before"]
    finally:
        disp.stop()


def test_inotify_dispatcher_stop_is_clean(tmp_path):
    """stop() must drain the thread quickly and free the fds. We pin
    'thread joined' here, and absence of FD-leak warnings would show up
    in CI / journalctl if the test ever broke this."""
    tx = tmp_path / "tx.jsonl"
    pool = WatcherPool()
    pool.add("agent-X", TranscriptWatcher(tx, on_text=lambda _: None))
    disp = InotifyDispatcher(pool)
    disp.watch("agent-X", tx)
    disp.start()
    t = disp._thread
    assert t is not None and t.is_alive()
    disp.stop(timeout=2.0)
    assert not t.is_alive()


@pytest.mark.timeout(5)
def test_inotify_dispatcher_wakeup_latency_under_budget(tmp_path):
    """Push-vs-poll is only worth the dep if it's actually fast. Pin a
    coarse latency budget so a regression that accidentally re-introduced
    polling would show up here."""
    tx = tmp_path / "tx.jsonl"
    seen_at: list[float] = []

    def on_text(_t: str) -> None:
        seen_at.append(time.monotonic())

    pool = WatcherPool()
    pool.add("agent-X", TranscriptWatcher(tx, on_text=on_text))
    disp = InotifyDispatcher(pool)
    disp.watch("agent-X", tx)
    disp.start()
    try:
        time.sleep(0.05)
        sent_at = time.monotonic()
        _append(tx, _assistant_text("ping"))
        _wait_for(lambda: seen_at)
        latency = seen_at[0] - sent_at
        # 250ms is generous — typical inotify latency on Linux is sub-ms.
        assert latency < 0.25, f"inotify latency too high: {latency*1000:.1f}ms"
    finally:
        disp.stop()
