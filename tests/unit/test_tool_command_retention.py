"""Finite, synthetic command retention controls; failures expose counters only."""
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from lib import tool_explanation_commands as commands

BUDGET = 1024 * 1024
TTL = 30 * 60


@pytest.fixture(autouse=True)
def cache(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(commands.time, 'monotonic', lambda: now[0])
    commands.clear()
    yield now
    commands.clear()


def text(number, width=60000, wide=False):
    return f'synthetic-{number:04d} ' + ('\U0001f642' if wide else 'x') * width


def retained():
    # Independent retained-string accounting on both the old and new roots.
    with commands._lock:
        return {key: list(getattr(value, 'entries', value))
                for key, value in commands._recent.items()}


def charge(key):
    return sum(sys.getsizeof(s) for s in retained().get(key, ()))


def resolves(key, value):
    # Return booleans so pytest never prints synthetic full commands.
    return commands.resolve(key, value[:80]) == value


@pytest.mark.parametrize('wide', [False, True])
def test_large_commands_plateau_in_python_bytes_and_keep_recent(wide):
    for i in range(24):
        commands.remember('a', text(i, wide=wide))
    assert charge('a') <= BUDGET, 'retained string bytes exceed per-agent budget'
    assert resolves('a', text(23, wide=wide))
    assert not resolves('a', text(0, wide=wide))


def test_byte_boundary_duplicate_and_hot_entry_lru(monkeypatch):
    a, b, c = [text(i, width=100) for i in range(3)]
    # Fixed-width independent strings: two fit EXACTLY, a third overflows.
    monkeypatch.setattr(commands, 'PER_AGENT_BYTES', 2 * sys.getsizeof(a), raising=False)
    commands.remember('a', a)
    commands.remember('a', b)
    commands.remember('a', a)
    assert charge('a') == 2 * sys.getsizeof(a)
    commands.remember('a', b)  # make b newest before a lookup
    assert resolves('a', a)  # successful lookup, not only remember, touches LRU
    commands.remember('a', c)
    assert resolves('a', a) and resolves('a', c)
    assert not resolves('a', b)
    assert len(retained()['a']) == 2
    # Oversized admission must not evict current useful entries or allocate an agent.
    commands.remember('a', text(5))
    commands.remember('other', text(5))
    assert resolves('a', a) and resolves('a', c)
    assert int('other' in retained()) == 0


def test_idle_expiry_is_global_lazy_and_success_only(cache):
    a, b = text(1, 100), text(2, 100)
    commands.remember('cold', a)
    commands.remember('hot', b)
    cache[0] = TTL - 1
    assert resolves('hot', b)
    assert not commands.resolve('cold', 'missing')
    cache[0] = TTL
    commands.remember('new', text(3, 100))
    assert int('cold' in retained()) == 0, 'another agent operation must sweep expired data'
    assert resolves('hot', b)
    cache[0] += TTL
    assert not resolves('hot', b)
    assert not retained()


def test_ambiguous_misses_do_not_refresh_idle_lifetime(cache):
    a = text(1, 100)
    commands.remember('a', a)
    commands.remember('a', a + 'suffix')
    cache[0] = TTL - 1
    assert not commands.resolve('a', a[:80])
    cache[0] = TTL
    assert not commands.resolve('a', a[:80])
    assert int('a' in retained()) == 0


def test_count_agent_caps_and_agent_lru_are_preserved():
    for i in range(257):
        commands.remember('many', text(i, 100))
    assert len(retained()['many']) == 256
    assert not resolves('many', text(0, 100))
    assert resolves('many', text(1, 100))
    commands.clear()
    for i in range(128):
        commands.remember(str(i), text(i, 100))
    assert resolves('0', text(0, 100))
    commands.remember('new', text(128, 100))
    assert len(retained()) == 128
    assert resolves('0', text(0, 100))
    assert int('1' in retained()) == 0
    assert not resolves('2', text(0, 100))


def test_concurrent_mutations_and_clear_leave_bounded_coherent_cache():
    barrier = threading.Barrier(3)
    def writer(agent):
        barrier.wait(timeout=5)
        for i in range(24):
            value = text(i, wide=bool(i % 2))
            commands.remember(agent, value)
            commands.resolve(agent, value[:80])
    def clearer():
        barrier.wait(timeout=5)
        commands.clear()
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(writer, key) for key in ('a', 'b')] + [pool.submit(clearer)]
        for f in futures:
            f.result(timeout=10)
    assert all(sum(sys.getsizeof(s) for s in values) <= BUDGET for values in retained().values())
    commands.clear()
    assert not retained()
    value = text(900, 100)
    commands.remember('fresh', value)
    assert charge('fresh') == sys.getsizeof(value)
    assert resolves('fresh', value)


def test_equivalent_redacted_matches_touch_lru_without_exposing_bodies(cache):
    prefix = '/usr/bin/bash -lc "python3 - <<\'PY\'\n'
    first = prefix + "#" * 100 + "x" * 100 + '\nPY"'
    second = prefix + "#" * 100 + "y" * 100 + '\nPY"'
    # Existing redaction equivalence is a successful logical lookup.
    commands.remember('equivalent', first)
    commands.remember('equivalent', second)
    cache[0] = TTL - 1
    result = commands.answer('equivalent', first[:80])
    assert bool(result)
    assert int('x' * 100 in result) == 0
    assert int('y' * 100 in result) == 0
    cache[0] = TTL
    commands.remember('other', text(3, 100))
    assert int('equivalent' in retained()) == 1
    commands.clear()
    assert len(retained()) == 0
