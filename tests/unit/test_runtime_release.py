from __future__ import annotations

from lib.runtime_release import (
    RuntimeReleaseMonitor,
    consume_clean_handoff,
    mark_clean_handoff,
    read_installed_release_id,
    read_runtime_release_id,
)


class FakeRuntime:
    """The RuntimeRPCServer surface the monitor drives. ``idle`` answers the
    fast path; ``blockers`` is what the seal reports while fenced."""

    def __init__(self, idle_results=(), blockers=None):
        self.idle_results = iter(idle_results)
        self.blockers = blockers if blockers is not None else [{}]
        self.calls = []
        self.fenced = False

    def begin_drain_if_idle(self):
        self.calls.append("idle?")
        return next(self.idle_results, False)

    def begin_admission_fence(self):
        self.calls.append("fence")
        self.fenced = True

    def release_admission_fence(self):
        self.calls.append("release")
        self.fenced = False

    def seal_if_drained(self):
        self.calls.append("seal")
        result = self.blockers.pop(0) if len(self.blockers) > 1 else self.blockers[0]
        if isinstance(result, Exception):
            raise result
        return result

    def cancel_handover(self):
        self.calls.append("unseal")

    def shutdown(self):
        self.calls.append("shutdown")


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def _monitor(runtime, desired="new", clock=None, **kwargs):
    value = [desired] if isinstance(desired, str) else desired
    return RuntimeReleaseMonitor(
        runtime, running_release_id="old", desired_release_id=lambda: value[0],
        clock=clock or Clock(), budget_sec=900, backoff_base_sec=300,
        backoff_cap_sec=3600, **kwargs)


def test_release_monitor_leaves_current_runtime_untouched():
    runtime = FakeRuntime([True])
    monitor = _monitor(runtime, "old")

    assert monitor.check_once() is False
    assert runtime.calls == []
    assert monitor.status()["phase"] == "idle"


def test_an_idle_runtime_hands_over_at_once_as_before():
    runtime = FakeRuntime([True])
    monitor = _monitor(runtime)

    assert monitor.check_once() is True
    assert runtime.calls == ["idle?", "shutdown"]


def test_a_busy_runtime_fences_admissions_and_hands_over_once_drained():
    runtime = FakeRuntime(blockers=[{"active": ["a"]}, {"active": ["a"]}, {}])
    monitor = _monitor(runtime)

    assert monitor.check_once() is False
    assert runtime.calls == ["idle?", "fence", "seal"]
    assert monitor.status()["blockers"] == {"active": ["a"]}
    assert monitor.check_once() is False
    assert monitor.check_once() is True
    assert runtime.calls.count("fence") == 1
    assert runtime.calls[-2:] == ["seal", "shutdown"]


def test_release_monitor_marks_clean_handoff_before_shutdown():
    order = []

    class OrderedRuntime(FakeRuntime):
        def seal_if_drained(self):
            order.append("seal")
            return {}

        def shutdown(self):
            order.append("shutdown")

    monitor = _monitor(OrderedRuntime(), before_shutdown=lambda: order.append("handoff"))

    assert monitor.check_once() is True
    assert order == ["seal", "handoff", "shutdown"]


def test_the_budget_reverts_and_backs_off_exponentially_up_to_the_cap():
    clock = Clock()
    runtime = FakeRuntime(blockers=[{"active": ["hung"]}])
    monitor = _monitor(runtime, clock=clock)
    delays = []
    for _ in range(6):
        monitor.check_once()                      # fence
        assert monitor.status()["phase"] == "draining"
        clock.now += 900
        monitor.check_once()                      # budget spent: revert
        assert monitor.status()["phase"] == "backoff"
        assert runtime.fenced is False
        start = clock.now
        while monitor.status()["phase"] == "backoff":
            clock.now += 60
            monitor.check_once()
        delays.append(clock.now - start)
    assert delays == [300, 600, 1200, 2400, 3600, 3600]
    assert "shutdown" not in runtime.calls


def test_an_error_mid_drain_never_leaves_the_fence_stuck():
    clock = Clock()
    runtime = FakeRuntime(blockers=[{"active": ["a"]}, RuntimeError("status failed"),
                                    {"active": ["a"]}])
    monitor = _monitor(runtime, clock=clock)
    monitor.check_once()
    clock.now += 100
    try:
        monitor.check_once()
    except RuntimeError:
        pass                                     # the loop logs and retries
    clock.now += 800
    monitor.check_once()
    assert runtime.fenced is False
    assert monitor.status()["phase"] == "backoff"


def test_a_restarted_runtime_starts_unfenced_and_decides_again():
    first = FakeRuntime(blockers=[{"active": ["a"]}])
    _monitor(first).check_once()
    assert first.fenced is True
    # The process died. Its successor's monitor knows nothing of the old fence.
    second = FakeRuntime(blockers=[{"active": ["a"]}])
    monitor = _monitor(second)
    assert monitor.status()["phase"] == "idle"
    monitor.check_once()
    assert second.calls == ["idle?", "fence", "seal"]
    assert monitor.status()["attempt"] == 1


def test_a_new_target_during_backoff_waits_for_the_backoff():
    clock = Clock()
    desired = ["r1"]
    runtime = FakeRuntime(blockers=[{"active": ["a"]}])
    monitor = _monitor(runtime, desired, clock)
    monitor.check_once()
    clock.now += 900
    monitor.check_once()
    desired[0] = "r2"
    clock.now += 10
    monitor.check_once()
    assert monitor.status()["phase"] == "backoff"
    assert monitor.status()["target_release"] == "r2"
    assert runtime.calls.count("fence") == 1


def test_runtime_release_id_is_read_from_versioned_root(tmp_path):
    (tmp_path / "RUNTIME_RELEASE_ID").write_text("runtime-abc\n")

    assert read_runtime_release_id(tmp_path) == ""
    (tmp_path / "RUNTIME_READY").write_text("ready\n")
    assert read_runtime_release_id(tmp_path) == "runtime-abc"
    assert read_runtime_release_id(tmp_path / "missing") == ""


def test_a_drain_targets_only_a_release_whose_install_finished(tmp_path):
    (tmp_path / "RUNTIME_RELEASE_ID").write_text("runtime-abc\n")
    (tmp_path / "RUNTIME_READY").write_text("ready\n")

    assert read_installed_release_id(tmp_path) == ""   # still installing
    (tmp_path / "INSTALL_OK").write_text("ok\n")
    assert read_installed_release_id(tmp_path) == "runtime-abc"
    (tmp_path / "RUNTIME_READY").unlink()
    assert read_installed_release_id(tmp_path) == ""


def test_clean_handoff_marker_is_private_and_consumed_once(tmp_path):
    marker = tmp_path / "runtime-clean-handoff"

    mark_clean_handoff(marker)

    assert marker.stat().st_mode & 0o777 == 0o600
    assert consume_clean_handoff(marker) is True
    assert consume_clean_handoff(marker) is False
