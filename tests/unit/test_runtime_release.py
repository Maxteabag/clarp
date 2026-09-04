from __future__ import annotations

from lib.runtime_release import (
    RuntimeReleaseMonitor,
    consume_clean_handoff,
    mark_clean_handoff,
    read_runtime_release_id,
)


class FakeRuntime:
    def __init__(self, idle_results):
        self.idle_results = iter(idle_results)
        self.drain_calls = 0
        self.shutdown_calls = 0

    def begin_drain_if_idle(self):
        self.drain_calls += 1
        return next(self.idle_results)

    def shutdown(self):
        self.shutdown_calls += 1


def test_release_monitor_leaves_current_runtime_untouched():
    runtime = FakeRuntime([True])
    monitor = RuntimeReleaseMonitor(
        runtime, running_release_id="same",
        desired_release_id=lambda: "same")

    assert monitor.check_once() is False
    assert runtime.drain_calls == 0
    assert runtime.shutdown_calls == 0


def test_release_monitor_keeps_old_runtime_until_idle_then_restarts():
    runtime = FakeRuntime([False, False, True])
    monitor = RuntimeReleaseMonitor(
        runtime, running_release_id="old",
        desired_release_id=lambda: "new")

    assert monitor.check_once() is False
    assert monitor.check_once() is False
    assert monitor.check_once() is True
    assert runtime.drain_calls == 3
    assert runtime.shutdown_calls == 1


def test_release_monitor_marks_clean_handoff_before_shutdown():
    order = []

    class OrderedRuntime(FakeRuntime):
        def begin_drain_if_idle(self):
            order.append("drain")
            return True

        def shutdown(self):
            order.append("shutdown")

    monitor = RuntimeReleaseMonitor(
        OrderedRuntime([]), running_release_id="old",
        desired_release_id=lambda: "new",
        before_shutdown=lambda: order.append("handoff"),
    )

    assert monitor.check_once() is True
    assert order == ["drain", "handoff", "shutdown"]


def test_runtime_release_id_is_read_from_versioned_root(tmp_path):
    (tmp_path / "RUNTIME_RELEASE_ID").write_text("runtime-abc\n")

    assert read_runtime_release_id(tmp_path) == ""
    (tmp_path / "RUNTIME_READY").write_text("ready\n")
    assert read_runtime_release_id(tmp_path) == "runtime-abc"
    assert read_runtime_release_id(tmp_path / "missing") == ""


def test_clean_handoff_marker_is_private_and_consumed_once(tmp_path):
    marker = tmp_path / "runtime-clean-handoff"

    mark_clean_handoff(marker)

    assert marker.stat().st_mode & 0o777 == 0o600
    assert consume_clean_handoff(marker) is True
    assert consume_clean_handoff(marker) is False
