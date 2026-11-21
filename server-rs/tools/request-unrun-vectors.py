#!/usr/bin/env python3
"""Run the Python Host's peer request lookup on synthetic queue and notice rows."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'server'))

QUEUE = 'queued'
FAILED = 'failed'
LAUNCH = 'launch_refused'
HELD = 'held_send_not_run'
CASES = [
    ('no row', None, None, []),
    ('queued', QUEUE, None, []),
    ('claimed', 'running', 10, []),
    ('completed', 'completed', 10, []),
    ('failed after delivery', FAILED, 10, [('turn_failed', 'Tool failed')]),
    ('missing start', FAILED, None, []),
    ('notice for a refused launch', FAILED, 10, [(LAUNCH, 'Headline\n\n  The tool is missing. ')]),
    ('notice for a held send', FAILED, 10, [(HELD, 'Mailbox could not be released')]),
    ('refusal notice takes priority', FAILED, None, [(HELD, 'Held'), (LAUNCH, 'Launch')]),
    ('empty notice does not prove anything', FAILED, 10, [(LAUNCH, '')]),
    ('notice after a nonfailed end', 'completed', 10, [(LAUNCH, 'Stale')]),
]


def main():
    from lib import peer_requests as reference

    vectors = []
    with closing(sqlite3.connect(':memory:')) as store:
        store.row_factory = sqlite3.Row
        store.executescript("""
            CREATE TABLE queued_turns (queue_id TEXT, client_msg_id TEXT, queue_seq INTEGER,
                                       status TEXT, started_at INTEGER);
            CREATE TABLE messages (message_id TEXT, text TEXT);
        """)
        reference.conn = lambda: store
        request_id = 'r0123456789ab'
        client_id = reference.request_client_id(request_id)
        for case, status, started_at, notices in CASES:
            store.execute('DELETE FROM queued_turns')
            store.execute('DELETE FROM messages')
            if status is not None:
                # The newest row alone is the request, even if an older row with the id failed.
                store.executemany('INSERT INTO queued_turns VALUES (?, ?, ?, ?, ?)', [
                    ('older', client_id, 1, FAILED, None),
                    ('chosen', client_id, 2, status, started_at),
                ])
            store.executemany('INSERT INTO messages VALUES (?, ?)', [
                (f'notice:{kind}:chosen', text) for kind, text in notices
            ])
            vectors.append(dict(case=case, status=status, started_at=started_at,
                                notices=dict(notices), reason=reference.unrun_reason(request_id)))
    path = ROOT / 'server-rs/crates/clarp-kernel/tests/fixtures/request-unrun-vectors.json'
    path.write_text(json.dumps(vectors, indent=1) + '\n')


if __name__ == '__main__':
    main()
