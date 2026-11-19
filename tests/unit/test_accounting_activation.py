"""The heartbeat accounting boundary is written by the settings table's owner (G2, ruled (a)):
settings_store.set_text_if_blank, one atomic statement, the first non-blank stamp wins. Two Host
starts at once agree on one boundary, and a later start never moves it."""
from __future__ import annotations

import threading

from lib import db, heartbeat, settings_store
from tests.patching import patch_shared


def test_two_simultaneous_starts_keep_one_boundary(monkeypatch):
    stamps = iter([1_000, 2_000])
    lock = threading.Lock()

    def now():
        with lock:
            return next(stamps)
    patch_shared(monkeypatch, heartbeat, "now_ms", now)
    barrier = threading.Barrier(2)
    seen: list[int] = []
    failures: list[BaseException] = []

    def start():
        try:
            barrier.wait(5)
            seen.append(heartbeat.activate_accounting())
        except BaseException as exc:  # noqa: BLE001 - reported below
            failures.append(exc)
    threads = [threading.Thread(target=start) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert not failures
    stored = settings_store.get_text(heartbeat.KEY_ACCOUNTING_ACTIVATION)
    assert len(seen) == 2 and seen[0] == seen[1] == int(stored) and stored in {"1000", "2000"}


def test_a_later_start_never_moves_the_boundary(monkeypatch):
    patch_shared(monkeypatch, heartbeat, "now_ms", lambda: 5_000)
    assert heartbeat.activate_accounting() == 5_000
    patch_shared(monkeypatch, heartbeat, "now_ms", lambda: 9_000)
    assert heartbeat.activate_accounting() == 5_000
    rows = db.conn().execute("SELECT COUNT(*) FROM settings WHERE key=?",
                             (heartbeat.KEY_ACCOUNTING_ACTIVATION,)).fetchone()[0]
    assert rows == 1
