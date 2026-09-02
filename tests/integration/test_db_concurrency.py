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
