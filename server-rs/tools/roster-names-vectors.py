#!/usr/bin/env python3
"""Writes the vectors the Rust Host's names of other agents on a row are tested against.

A row of a conversation that another agent is part of names that agent: a user's row that
agent wrote names it as its sender (`sender_agent_id`, `sender_name`, `sender_session`), and
an answer to it names it as whom it answers (`reply_to_*`). The Python Host keeps only the
agent's id on the row and reads the name and the session from its roster when the row is
read (`message_previews.list_messages`: `LEFT JOIN agents sender ON sender.agent_id =
m.sender_agent_id`, then `provenance_fields`). Every id it keeps is one its roster has: a
sender it does not know is resolved to none (`send_request._resolve_sender_agent_id`), and a
removed agent's row stays in the roster. This writes one conversation with such rows, read as
it was written, after its sender was renamed, and after its sender was removed, with the roster
as it stood each time, to `crates/clarp-kernel/tests/fixtures/roster-names-vectors.json`.

    python3 server-rs/tools/roster-names-vectors.py

Nothing of a Host is started; the library writes only in a scratch place of its own
(`tools/scratch.py`), which is gone when it ends.
"""
from __future__ import annotations

import json
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import scratch as tool_scratch  # noqa: E402

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[1]
OUT = HERE.parent / 'crates' / 'clarp-kernel' / 'tests' / 'fixtures' / 'roster-names-vectors.json'
FIELDS = ('sender_agent_id', 'sender_name', 'sender_session', 'reply_to_agent_id', 'reply_to_name',
          'reply_to_session')


def isolate(place: pathlib.Path) -> None:
    """Points the library at a place of its own before it is imported."""
    os.environ.update({
        'CLAUDE_PWA_DB': str(place / 'state.sqlite'), 'CLAUDE_PWA_LOG_DIR': str(place / 'logs'),
        'CLARP_CONFIG_DIR': str(place / 'config'), 'CLARP_SHARE_DIR': str(place / 'share'),
        'CLARP_CACHE_DIR': str(place / 'cache'), 'CLAUDE_PWA_CONFIG': str(place / 'no-such-config.toml'),
        'CLARP_CODE_ROOT': str(REPO / 'server'),
    })
    sys.path.insert(0, str(REPO / 'server'))


def generate(place: pathlib.Path) -> int:
    isolate(place)
    from lib import agents as agents_db, db, message_previews, message_store
    db.reset_for_tests(place / 'names.sqlite')
    # An agent's id is random: the same ids at every run, so that the file is the same.
    minted = iter(f'agent-{n}' for n in range(1, 100))
    agents_db._new_agent_id = lambda: next(minted)
    work = place / 'work'
    work.mkdir()
    hugo = agents_db.create_agent(persona='Hugo', voice_id='V', cwd=str(work), session='hugo')
    cpp = agents_db.create_agent(persona='C++ Agent', voice_id='V', cwd=str(work), session='cagent')
    transcript: list[dict] = []

    def stamp(row_id: str, timestamp: str) -> None:
        agents_db.conn().execute('UPDATE messages SET timestamp = ? WHERE message_id = ?', (timestamp, row_id))

    def said(sender: str, text: str, client_id: str, timestamp: str) -> None:
        # As tests/unit/test_agent_conversations.py has another agent write: the row, then
        # the turn's answer read from the tool's transcript.
        rec = message_store.record_user_message(
            agent_id=hugo, backend_session_id='bs1', client_msg_id=client_id,
            text=text, origin='agent', sender_agent_id=sender)
        stamp(rec['id'], timestamp)
        transcript.append({'role': 'user', 'text': text, 'timestamp': timestamp})

    def answered(text: str, timestamp: str, trace: str) -> None:
        agents_db.open_turn(agent_id=hugo, source='pwa', trace_id=trace)
        pending = agents_db.conn().execute(
            """SELECT source_file FROM messages WHERE agent_id = ? AND role = 'user'
                  AND source_file LIKE 'client:%' AND COALESCE(trace_id, '') = ''
                ORDER BY timestamp DESC, seq DESC LIMIT 1""", (hugo,)).fetchone()
        if pending:
            message_store.relink_client_message(pending['source_file'].split(':', 1)[1], trace_id=trace)
        message_store.upsert_live_assistant_message(
            agent_id=hugo, backend_session_id='bs1', trace_id=trace, text=text)
        transcript.append({'role': 'assistant', 'text': text, 'timestamp': timestamp})
        message_store.store_transcript_turns(
            agent_id=hugo, backend_session_id='bs1', source_file='f', turns=list(transcript))

    said(cpp, 'please look at the build', 'c1', '2026-11-20T03:21:06Z')
    answered('It is green.', '2026-11-20T03:21:30Z', 'trace-1')
    said(cpp, 'and the tests too', 'c2', '2026-11-20T03:22:06Z')
    answered('They pass.', '2026-11-20T03:22:30Z', 'trace-2')

    def roster() -> dict:
        rows = agents_db.conn().execute(
            'SELECT agent_id, persona, session, deleted_at IS NOT NULL AS removed FROM agents').fetchall()
        return {row['agent_id']: {'persona': row['persona'], 'session': row['session'],
                                  'removed': bool(row['removed'])} for row in rows}

    def read(state: str) -> dict:
        rows = message_previews.list_messages(agent_id=hugo, backend_session_id='bs1')
        return {'state': state, 'roster': roster(),
                'rows': [{'role': row['role'], 'text': row['text'], **{f: row[f] for f in FIELDS}}
                         for row in rows]}

    states = [read('as written')]
    agents_db.update_agent(cpp, persona='C++ Renamed')
    states.append(read('its sender renamed'))
    agents_db.soft_delete(cpp)
    states.append(read('its sender removed'))

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({
        'about': 'Written by server-rs/tools/roster-names-vectors.py from server/lib/message_previews.py '
                 '(list_messages, provenance_fields). Do not edit by hand.',
        'owner': hugo, 'sender': cpp, 'states': states}, ensure_ascii=False, sort_keys=True, indent=1) + '\n')
    print(f'{OUT.relative_to(REPO)}: {len(states)} states of {len(states[0]["rows"])} rows, {OUT.stat().st_size} bytes')
    return 0


def main(argv: list[str]) -> int:
    if os.environ.get('PYTHONHASHSEED') != '0':
        os.execve(sys.executable, [sys.executable, *argv], {**os.environ, 'PYTHONHASHSEED': '0'})
    with tool_scratch.directory('clarp-roster-names-') as place:
        return generate(pathlib.Path(place))


if __name__ == '__main__':
    sys.exit(main(sys.argv))
