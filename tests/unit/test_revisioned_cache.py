"""RevisionedCache: bounded, locked, revision-keyed; db.change_stamp sees other
connections' commits so a cache keyed by it cannot serve a hook's stale view."""
from __future__ import annotations

import sqlite3
import threading

import pytest

from lib import agent_conversations, agents as agents_db, db, message_store, revisioned_cache
from lib.revisioned_cache import RevisionedCache


@pytest.fixture
def cache():
    name = "test.cache"
    with revisioned_cache._REGISTRY_LOCK:
        revisioned_cache._REGISTRY.pop(name, None)
    made = RevisionedCache(name, max_entries=2, key_fn=int)
    yield made
    with revisioned_cache._REGISTRY_LOCK:
        revisioned_cache._REGISTRY.pop(name, None)


def test_get_or_compute_serves_same_revision_and_recomputes_on_change(cache):
    calls = []
    compute = lambda: calls.append(1) or len(calls)
    assert cache.get_or_compute("1", "r1", compute) == 1
    assert cache.get_or_compute(1, "r1", compute) == 1  # key_fn normalises "1" and 1
    assert cache.get_or_compute(1, "r2", compute) == 2
    assert (cache.hits, cache.misses) == (1, 2)


def test_bounded_lru_and_registry_reset(cache):
    for key in (1, 2, 3):
        cache.get_or_compute(key, "r", lambda: key)
    assert len(cache) == 2 and cache.peek(1) is None and cache.peek(3) == ("r", 3)
    cache.retain([3])
    assert len(cache) == 1
    assert revisioned_cache.registered()["test.cache"] is cache
    revisioned_cache.reset_all()
    assert len(cache) == 0 and cache.hits == cache.misses == 0
    with pytest.raises(ValueError):
        RevisionedCache("test.cache")


def test_concurrent_callers_never_corrupt_entries(cache):
    errors = []

    def worker(n):
        try:
            for i in range(200):
                assert cache.get_or_compute(n % 2, i // 50, lambda: i // 50) == i // 50
        except AssertionError as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors


def test_change_stamp_moves_on_foreign_commit_and_local_write():
    db.conn()
    before = db.change_stamp()
    assert before == db.change_stamp()
    foreign = sqlite3.connect(str(db.DB_PATH))
    foreign.execute("INSERT INTO settings(key,value,updated_at) VALUES ('k','v',1)")
    foreign.commit()
    foreign.close()
    after_foreign = db.change_stamp()
    assert after_foreign[0] != before[0] and after_foreign[1] == before[1]
    db.conn().execute("UPDATE settings SET value='w' WHERE key='k'")
    after_local = db.change_stamp()
    assert after_local[1] == before[1] + 1


def _pair(tmp_path):
    hugo = agents_db.create_agent(persona="Hugo", voice_id="V", cwd=str(tmp_path), session="hugo")
    cpp = agents_db.create_agent(persona="Cpp", voice_id="V", cwd=str(tmp_path), session="cpp")
    rec = message_store.record_user_message(
        agent_id=cpp, backend_session_id="bs1", client_msg_id="c1",
        text="first delivery", origin="agent", sender_agent_id=hugo)
    return hugo, cpp, rec["id"]


def test_second_connection_write_invalidates_the_pair_list_cache(tmp_path):
    """A hook process rewriting messages must not leave the sidebar stale."""
    _hugo, _cpp, message_id = _pair(tmp_path)
    first = agent_conversations.list_conversations()
    assert [c["latest_message"]["text"] for c in first] == ["first delivery"]
    assert agent_conversations.list_conversations() == first
    assert agent_conversations._LIST_CACHE.hits == 1
    generation_before = db.write_generation()

    other = sqlite3.connect(str(db.DB_PATH))  # a plain connection, like a hook
    other.execute("UPDATE messages SET text=? WHERE message_id=?", ("rewritten by hook", message_id))
    other.commit()
    other.close()

    assert db.write_generation() == generation_before  # our counter never saw it
    refreshed = agent_conversations.list_conversations()
    assert [c["latest_message"]["text"] for c in refreshed] == ["rewritten by hook"]
