"""Regression: the sqlite connection must be safe under concurrent request
threads.

ThreadingHTTPServer runs each request on its own thread. A single shared
sqlite3.Connection (the old model) is not thread-safe for concurrent
execute/fetch — two threads racing on the same cursor raise
`sqlite3.InterfaceError: bad parameter or other API misuse`, and an
`UPDATE ... RETURNING` + fetchone() can return None (→ `int(None)` TypeError in
the message store). That crashed `GET /log` whenever the client fired a burst of
requests (e.g. while recording a voice message), making the agent look dead.

With per-thread connections (lib.db.conn), concurrent access is safe and this
test passes reliably; against the shared-connection model it fails most runs.
"""
from __future__ import annotations

import threading

from lib import agents as agents_db
from lib import message_store


def _turns(n: int) -> list[dict]:
    return [
        {
            "role": "user" if i % 2 == 0 else "assistant",
            "text": f"message {i}",
            "timestamp": f"2026-06-03T00:00:{i % 60:02d}",
            "kind": None,
        }
        for i in range(n)
    ]


def test_concurrent_transcript_store_and_read_has_no_connection_race(tmp_path):
    agent_id = agents_db.create_agent(
        persona="Adam", voice_id="V", cwd="/tmp", session="adam", backend="claude")
    sess = "sess-94aa9fd8"
    turns = _turns(25)
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            for _ in range(20):
                message_store.store_transcript_turns(
                    agent_id=agent_id, backend_session_id=sess,
                    source_file="/x.jsonl", turns=turns)
                message_store.list_messages(
                    agent_id=agent_id, backend_session_id=sess, limit=60)
        except BaseException as e:   # noqa: BLE001 — capture the race
            errors.append(e)
        finally:
            from lib import db
            db.close_local()

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, (
        f"sqlite connection race under concurrent requests: "
        f"{type(errors[0]).__name__}: {errors[0]} ({len(errors)} threads failed)"
    )


def test_reset_cannot_mark_another_database_migrated(tmp_path, monkeypatch):
    from lib import db
    db.reset_for_tests(tmp_path / 'first.sqlite')
    opened, release, reset_done = threading.Event(), threading.Event(), threading.Event()
    original = db._open_connection
    errors = []
    def delayed_open():
        connection = original()
        if threading.current_thread().name == 'opening-database':
            opened.set()
            assert release.wait(3)
        return connection
    monkeypatch.setattr(db, '_open_connection', delayed_open)
    def open_first():
        try:
            db.conn()
        except Exception as error:
            errors.append(error)
        finally:
            db.close_local()
    def reset_second():
        db.reset_for_tests(tmp_path / 'second.sqlite')
        reset_done.set()
    opener = threading.Thread(target=open_first, name='opening-database')
    resetter = threading.Thread(target=reset_second)
    opener.start()
    try:
        assert opened.wait(3)
        resetter.start()
        assert not reset_done.wait(.05), 'reset must wait for the open/migrate operation'
    finally:
        release.set()
        opener.join(3)
        if resetter.ident is not None: resetter.join(3)
    assert not errors
    assert reset_done.is_set()
    assert db.conn().execute('SELECT count(*) FROM settings').fetchone()[0] >= 0
