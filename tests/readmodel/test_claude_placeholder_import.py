"""Structural provider noise never becomes a stored assistant row.

The real importer preserves user IDs and genuine identical/quoted text; this
is parser provenance only, with no durable final-answer/carry certificate.
"""
import json
import time

import pytest

from lib import db, message_store, transcript_incremental as inc
from tests.readmodel import corpus, world



# The transcript's copy of the request is dated 2026-11-06T10:00:00Z, while the test recorded its
# client row at the real time: a copy before its own send, which a copy cannot be. As 505a7b0a did
# for three other files, this file's clock starts two seconds before that copy and runs on at the
# real pace, so the send comes just before its copy.
_COPY_AT_S = 1793959200              # 2026-11-06T10:00:00Z


@pytest.fixture(autouse=True)
def _clock_before_the_copy(monkeypatch):
    real = time.time
    offset = (_COPY_AT_S - 2) - real()
    monkeypatch.setattr(time, "time", lambda: real() + offset)

@pytest.mark.parametrize('noise_kind', ['model', 'api-error', 'nested-api-error', 'meta-parent'])
def test_structural_noise_is_absent_from_rows_while_genuine_same_text_and_user_ids_survive(
        tmp_path, noise_kind):
    world.prepare(tmp_path / 'state.sqlite', 'claude')
    phrase = 'No response requested.'
    message_store.record_user_message(agent_id=corpus.AGENT, backend_session_id=corpus.SESSION,
        client_msg_id='preserved-user', text=phrase, origin='user', trace_id='request')
    before = db.conn().execute(
        "SELECT message_id FROM messages WHERE role='user'").fetchone()['message_id']
    noise = {'type': 'assistant', 'timestamp': '2026-11-06T10:00:01Z',
        'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': phrase}]}}
    records = [{'type': 'user', 'timestamp': '2026-11-06T10:00:00Z',
                'message': {'content': phrase}}]
    if noise_kind == 'model':
        noise['message']['model'] = '<synthetic>'
    elif noise_kind == 'api-error':
        noise['isApiErrorMessage'] = True
    elif noise_kind == 'nested-api-error':
        noise['message']['isApiErrorMessage'] = True
    else:
        records += [{'type': 'user', 'isMeta': True, 'uuid': 'meta',
                     'message': {'content': 'Injected input'}}]
        noise['parentUuid'] = 'meta'
    records += [noise, {'type': 'assistant', 'timestamp': '2026-11-06T10:00:02Z',
        'message': {'id': 'genuine', 'model': 'claude-real', 'usage': {'input_tokens': 0},
                    'content': [{'type': 'text', 'text': phrase}]}}]
    transcript = tmp_path / 'claude.jsonl'
    transcript.write_text(''.join(json.dumps(record) + '\n' for record in records))
    inc.import_transcript(backend='claude', path=transcript, agent_id=corpus.AGENT,
                         backend_session_id=corpus.SESSION)
    rows = db.conn().execute('SELECT message_id,role,text FROM messages ORDER BY seq').fetchall()
    assert [(row['role'], row['text']) for row in rows] == [('user', phrase), ('assistant', phrase)]
    assert next(row['message_id'] for row in rows if row['role'] == 'user') == before
