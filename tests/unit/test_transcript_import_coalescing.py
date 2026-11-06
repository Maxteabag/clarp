"""Real worker admission under the signed completion-based owner window.

A manual timer advances scheduling, while real threads/Events exercise the owner
boundary. No production-only clock hook, network, or wall-clock ten-second wait.
"""
from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

from lib import transcript_import_cache as cache


class ManualTimers:
    def __init__(self):
        self.now = 0.0
        self.timers = []
        owner = self

        class Timer:
            def __init__(self, delay, function, args=(), kwargs=None):
                self.due = owner.now + delay
                self.function, self.args, self.kwargs = function, args, kwargs or {}
                self.daemon = True
                self.alive = False
                owner.timers.append(self)

            def start(self):
                self.alive = True

            def cancel(self):
                self.alive = False

            def is_alive(self):
                return self.alive

            def join(self, timeout=None):
                pass

        self.Timer = Timer

    def monotonic(self):
        return self.now

    def advance(self, now):
        self.now = now
        for _ in range(100):
            due = [t for t in self.timers if t.alive and t.due <= now]
            if not due:
                return
            for timer in due:
                try:
                    timer.function(*timer.args, **timer.kwargs)
                finally:
                    timer.alive = False
        pytest.fail("timer scheduling did not converge")


@pytest.fixture
def clock(monkeypatch):
    assert cache.reset_for_tests()
    manual = ManualTimers()
    real_time = cache.time
    monkeypatch.setattr(cache, 'time', SimpleNamespace(monotonic=manual.monotonic))
    monkeypatch.setattr(cache, 'threading', SimpleNamespace(
        Thread=threading.Thread, Timer=manual.Timer, Lock=threading.Lock))
    monkeypatch.setattr(cache, '_BACKGROUND_MIN_INTERVAL', 10.0)
    try:
        yield manual
    finally:
        # A frozen scheduler clock must not freeze a failed-test drain deadline.
        monkeypatch.setattr(cache, 'time', real_time)
        assert cache.reset_for_tests(deadline_s=3)


def completed():
    event = threading.Event()
    return event, event.set


@pytest.mark.parametrize('different_path', [False, True])
def test_held_import_followup_waits_until_completion_plus_window(tmp_path, clock, different_path):
    first = tmp_path / 'first.jsonl'
    first.write_text('v1')
    follow = tmp_path / 'follow.jsonl' if different_path else first
    if different_path:
        follow.write_text('v2')
    entered, release = threading.Event(), threading.Event()
    observed = []
    finished, after = completed()
    other_done, other_after = completed()
    follow_done, follow_after = completed()

    def initial():
        observed.append(('initial', clock.now, first.read_text()))
        entered.set()
        assert release.wait(3)

    assert cache.schedule_import(first, initial, owner='agent', after=after)
    try:
        assert entered.wait(2)
        clock.now = 15.0
        follow.write_text('v2 with appended bytes')
        assert cache.schedule_import(follow, lambda: observed.append(
            ('follow', clock.now, follow.read_text())), owner='agent', after=follow_after)
        assert cache.schedule_import(first, lambda: observed.append(
            ('other', clock.now, '')), owner='other', after=other_after)
        clock.now = 20.0
    finally:
        release.set()
    assert finished.wait(2)
    # The eligible other owner is an admission barrier: ineligible same-owner
    # work must be deferred rather than sleeping/spinning in the only worker.
    assert other_done.wait(2)
    assert [x[0] for x in observed] == ['initial', 'other'], observed
    clock.advance(139.999)
    assert not follow_done.is_set()
    # Continuous dirty updates must retain the original earned deadline.
    follow.write_text('v3 newest bytes')
    assert cache.schedule_import(follow, lambda: observed.append(
        ('follow', clock.now, follow.read_text())), owner='agent', after=follow_after)
    clock.advance(140.0)
    assert follow_done.wait(2)
    assert observed[-1] == ('follow', 140.0, 'v3 newest bytes')
    assert cache.wait_for_background(2)


def test_urgent_token_survives_later_normal_update_and_opens_next_window(tmp_path, clock):
    path = tmp_path / 'urgent.jsonl'
    path.write_text('v1')
    first_done, first_after = completed()
    assert cache.schedule_import(path, lambda: None, owner='agent', after=first_after)
    assert first_done.wait(2)
    assert cache.wait_for_background(2)
    held, release = threading.Event(), threading.Event()
    blocker = tmp_path / 'blocker'
    blocker.write_text('x')

    def block():
        held.set()
        assert release.wait(3)

    clock.now = 1
    assert cache.schedule_import(blocker, block, owner='another')
    try:
        assert held.wait(2)
        path.write_text('v2 changed length')
        done, after = completed()
        assert cache.schedule_import(path, lambda: None, owner='agent', after=after)
        assert cache.schedule_import(path, lambda: None, owner='agent', urgent=True)
        path.write_text('v3 latest urgent coverage')
        seen = []
        assert cache.schedule_import(path, lambda: seen.append((clock.now, path.read_text())),
                                     owner='agent')
        clock.now = 2
    finally:
        release.set()
    assert done.wait(2)
    assert seen == [(2, 'v3 latest urgent coverage')]
    assert cache.wait_for_background(2)
    path.write_text('v4 normal')
    later, later_after = completed()
    assert cache.schedule_import(path, lambda: seen.append((clock.now, path.read_text())),
                                 owner='agent', after=later_after)
    clock.advance(11.999)
    assert not later.is_set()
    clock.advance(12)
    assert later.wait(2)
    assert seen[-1] == (12, 'v4 normal')


def test_successful_unchanged_callback_validation_opens_owner_window(tmp_path, clock):
    first, second = tmp_path / 'first', tmp_path / 'second'
    first.write_text('unchanged')
    second.write_text('new path')
    assert cache.import_if_changed(first, lambda: None, owner='agent')
    done, after = completed()
    assert cache.schedule_import(first, lambda: pytest.fail('unchanged file parsed'),
                                 owner='agent', after=after)
    assert done.wait(2)
    assert cache.wait_for_background(2)
    clock.now = 1
    later, later_after = completed()
    assert cache.schedule_import(second, lambda: None, owner='agent', after=later_after)
    assert not later.wait(.05)
    clock.advance(10)
    assert later.wait(2)


def test_failed_urgent_keeps_prior_success_window_and_dirty_version(tmp_path, clock):
    path = tmp_path / 'retry'
    path.write_text('v1')
    done, after = completed()
    assert cache.schedule_import(path, lambda: None, owner='agent', after=after)
    assert done.wait(2)
    assert cache.wait_for_background(2)
    clock.now = 1
    path.write_text('v2 retained')
    failed = threading.Event()
    callbacks = []

    def fail():
        failed.set()
        raise OSError('synthetic importer failure')

    assert cache.schedule_import(path, fail, owner='agent', urgent=True,
                                 after=lambda: callbacks.append('wrong'))
    assert failed.wait(2)
    assert cache.wait_for_background(2)
    assert callbacks == []
    seen = []
    retry, retry_after = completed()
    assert cache.schedule_import(path, lambda: seen.append(path.read_text()),
                                 owner='agent', after=retry_after)
    clock.advance(9.999)
    assert not retry.is_set()
    clock.advance(10)
    assert retry.wait(2)
    assert seen == ['v2 retained']


def test_ownerless_paths_keep_independent_windows(tmp_path, clock):
    for name in ('a', 'b'):
        path = tmp_path / name
        path.write_text(name)
        done, after = completed()
        assert cache.schedule_import(path, lambda: None, after=after)
        assert done.wait(2)
        assert cache.wait_for_background(2)


def test_cooldown_capacity_never_evicts_hot_owner_and_refused_dirty_retries(tmp_path, clock, monkeypatch):
    monkeypatch.setattr(cache, '_BACKGROUND_LIMIT', 1)
    path = tmp_path / 'capacity'
    path.write_text('dirty version')
    # Cooldown bound is four times the waiting-key bound (256 in production).
    for n in range(4):
        done, after = completed()
        assert cache.schedule_import(path, lambda: None, owner=str(n), after=after)
        assert done.wait(2)
        assert cache.wait_for_background(2)
    seen = []
    assert not cache.schedule_import(path, lambda: seen.append(path.read_text()), owner='refused')
    path.write_text('owner zero still hot')
    zero, zero_after = completed()
    assert cache.schedule_import(path, lambda: None, owner='0', after=zero_after)
    assert not zero.wait(.05)
    clock.advance(10)
    assert zero.wait(2)
    assert cache.wait_for_background(2)
    # No new append after refusal: the version remains unread for this owner.
    admitted, admitted_after = completed()
    assert cache.schedule_import(path, lambda: seen.append(path.read_text()),
                                 owner='refused', after=admitted_after)
    assert admitted.wait(2)
    assert seen == ['owner zero still hot']


def test_callback_duration_failure_and_reentrant_request_do_not_shift_window(tmp_path, clock):
    first, second = tmp_path / 'callback-first', tmp_path / 'callback-second'
    first.write_text('one')
    second.write_text('two')
    seen = []
    done, after = completed()

    def callback():
        clock.now = 100  # callback cost is not import duration/completion
        assert cache.schedule_import(second, lambda: seen.append(('two', clock.now)),
                                     owner='agent', after=after)
        raise RuntimeError('synthetic callback failure after reentrant admission')

    assert cache.schedule_import(first, lambda: seen.append(('one', clock.now)),
                                 owner='agent', after=callback)
    assert done.wait(2)
    assert cache.wait_for_background(2)
    assert seen == [('one', 0), ('two', 100)]


def test_deferred_keys_occupy_capacity_and_refused_version_can_retry(tmp_path, clock, monkeypatch):
    monkeypatch.setattr(cache, '_BACKGROUND_LIMIT', 1)
    first, second = tmp_path / 'deferred-first', tmp_path / 'refused-second'
    first.write_text('v1')
    second.write_text('retained without another append')
    done, after = completed()
    assert cache.schedule_import(first, lambda: None, owner='first', after=after)
    assert done.wait(2)
    assert cache.wait_for_background(2)
    first.write_text('v2')
    later, later_after = completed()
    assert cache.schedule_import(first, lambda: None, owner='first', after=later_after)
    seen = []
    assert not cache.schedule_import(second, lambda: seen.append(second.read_text()), owner='second')
    clock.advance(10)
    assert later.wait(2)
    assert cache.wait_for_background(2)
    retry, retry_after = completed()
    assert cache.schedule_import(second, lambda: seen.append(second.read_text()),
                                 owner='second', after=retry_after)
    assert retry.wait(2)
    assert seen == ['retained without another append']


def test_hot_path_timer_order_cannot_starve_older_same_owner_path(tmp_path, clock):
    a, b = tmp_path / 'hot-a', tmp_path / 'waiting-b'
    a.write_text('seed')
    b.write_text('waiting version')
    seed, seed_after = completed()
    assert cache.schedule_import(a, lambda: None, owner='agent', after=seed_after)
    assert seed.wait(2) and cache.wait_for_background(2)
    a.write_text('hot first')
    progressed = threading.Event()
    seen = []
    repeat = [True]

    def after_a():
        seen.append(('a', clock.now))
        if repeat[0]:
            a.write_text(f'hot next {clock.now}')
            assert cache.schedule_import(a, lambda: None, owner='agent', after=after_a)
        progressed.set()

    def import_b():
        seen.append(('b', clock.now))
        progressed.set()

    assert cache.schedule_import(a, lambda: None, owner='agent', after=after_a)
    assert cache.schedule_import(b, import_b, owner='agent')
    key_a, key_b = f'agent\0{a}', f'agent\0{b}'

    def fire_if_waiting(key):
        with cache._guard:
            entry = cache._deferred.get(key)
        if entry is not None:
            timer = entry[1]
            if timer.due <= clock.now:
                timer.alive = False
                timer.function(*timer.args, **timer.kwargs)

    try:
        # Valid adversarial scheduling: A's timer, worker and reentrant after
        # complete before B's due timer gets CPU. All due callbacks are delivered.
        for instant in (10., 20., 30.):
            clock.now = instant
            progressed.clear()
            fire_if_waiting(key_a)
            assert progressed.wait(2) and cache.wait_for_background(2)
            fire_if_waiting(key_b)
            assert cache.wait_for_background(2)
            if any(path == 'b' for path, _ in seen):
                break
        assert any(path == 'b' for path, _ in seen), seen
        assert len([x for x in seen if x[0] == 'a']) <= 2, seen
    finally:
        repeat[0] = False
        assert cache.wait_for_background(2)


def test_fast_urgent_completion_rearms_waiting_path_for_shorter_deadline(tmp_path, clock):
    a, b = tmp_path / 'slow-a', tmp_path / 'waiting-b'
    a.write_text('first')
    b.write_text('waiting')
    seed, seed_after = completed()

    def slow():
        clock.now = 20

    assert cache.schedule_import(a, slow, owner='agent', after=seed_after)
    assert seed.wait(2) and cache.wait_for_background(2)
    seen = []
    done, after = completed()
    assert cache.schedule_import(b, lambda: seen.append(clock.now), owner='agent', after=after)
    clock.now = 25
    a.write_text('new fast import')
    urgent, urgent_after = completed()

    def fast():
        clock.now = 26

    assert cache.schedule_import(a, fast, owner='agent', urgent=True, after=urgent_after)
    assert urgent.wait(2) and cache.wait_for_background(2)
    clock.advance(35.999)
    assert not done.is_set()
    clock.advance(36)
    assert done.wait(2)
    assert seen == [36]


def test_unchanged_validation_does_not_erase_last_expensive_import_cost(tmp_path, clock):
    a, b = tmp_path / 'cost-a', tmp_path / 'cost-b'
    a.write_text('unchanged input')
    b.write_text('waiting input')
    seed, seed_after = completed()

    def slow():
        clock.now = 20

    assert cache.schedule_import(a, slow, owner='agent', after=seed_after)
    assert seed.wait(2) and cache.wait_for_background(2)
    clock.now = 25
    checked, checked_after = completed()
    assert cache.schedule_import(a, lambda: pytest.fail('unchanged source parsed'),
                                 owner='agent', urgent=True, after=checked_after)
    assert checked.wait(2) and cache.wait_for_background(2)
    seen = []
    done, after = completed()
    assert cache.schedule_import(b, lambda: seen.append(clock.now), owner='agent', after=after)
    clock.advance(144.999)
    assert not done.is_set()
    clock.advance(145)
    assert done.wait(2)
    assert seen == [145]


@pytest.mark.parametrize('duration,next_admission', [(1, 11), (5, 15), (5.001, 35.007), (20, 140), (100, 400)])
def test_slow_source_backoff_is_measured_from_completion_with_threshold_and_cap(
        tmp_path, clock, duration, next_admission):
    first, follow = tmp_path / 'first', tmp_path / 'follow'
    first.write_text('initial bytes')
    follow.write_text('new dirty source')
    done, after = completed()
    def importer():
        clock.now = duration
    assert cache.schedule_import(first, importer, owner='agent', after=after)
    assert done.wait(2)
    assert cache.wait_for_background(2)
    later, later_after = completed()
    observed = []
    assert cache.schedule_import(follow, lambda: observed.append(clock.now),
                                 owner='agent', after=later_after)
    clock.advance(next_admission - .001)
    assert not later.wait(.05), observed
    clock.advance(next_admission)
    assert later.wait(2)
    assert observed == [next_admission]
    assert cache.wait_for_background(2)
