#!/usr/bin/env python3
"""Writes the vectors the Rust Host's push payloads are tested against.

What a phone is sent when a turn ends, or when an agent asks its user something, is built in
the Python Host by pure functions of `server/lib/apns.py`: `turn_done_payload`,
`decision_payload`, `_collapse_id`, `decision_needs_interruption`. The app reads these
payloads (its notification extension, its deep links), so they are part of the contract. This
calls the functions for many inputs and writes what they built to
`crates/clarp-kernel/tests/fixtures/push-vectors.json`.

    python3 server-rs/tools/push-vectors.py

Nothing of a Host is touched or started, and nothing is sent: the library is pointed at an
empty directory under /var/tmp before it is imported, and only functions that build a payload
are called. Every text is synthetic.
"""
from __future__ import annotations

import itertools
import json
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import scratch as tool_scratch

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[1]
OUT = HERE.parent / 'crates' / 'clarp-kernel' / 'tests' / 'fixtures' / 'push-vectors.json'

PERSONAS = ['Vera', '', '  Ada  ', 'Ünïcödé']
SESSIONS = ['vera', '', None]
BODIES = ['The build is green.', '', None, 'x' * 600, 'Two\nlines']
AVATARS = [(None, False), ('https://host.example/a.png', False), ('https://host.example/a.png', True), ('', True)]
IDS = ['pn-0123456789abcdef0123', '', 'n' * 80]
SERVERS = ['srv-1', '']
TITLES = ['Deploy?', '', '  Deploy the thing  ', 't' * 200]
QUESTIONS = ['Deploy the synthetic thing?', '', 'Deploy?', 'q' * 700, '  padded  ']


def isolate(root) -> None:
    os.environ.update({
        'CLAUDE_PWA_DB': str(root / 'state.sqlite'), 'CLAUDE_PWA_LOG_DIR': str(root / 'logs'),
        'CLARP_CONFIG_DIR': str(root / 'config'), 'CLARP_SHARE_DIR': str(root / 'share'),
        'CLARP_CACHE_DIR': str(root / 'cache'), 'CLAUDE_PWA_CONFIG': str(root / 'no-such-config.toml'),
        'CLARP_CODE_ROOT': str(REPO / 'server'),
    })
    sys.path.insert(0, str(REPO / 'server'))


def main() -> int:
    with tool_scratch.directory('clarp-push-vectors-') as name:
        return generate(pathlib.Path(name))


def generate(root):
    isolate(root)
    from lib import apns

    turns = []
    for persona, session, body, (avatar, custom), notification_id, server, needs in itertools.product(
            PERSONAS, SESSIONS, BODIES, AVATARS, IDS, SERVERS, [False, True]):
        payload = apns.turn_done_payload(persona, session, body, avatar, custom, notification_id, server, needs)
        turns.append({'persona': persona, 'session': session, 'body': body, 'avatar_url': avatar,
                      'avatar_custom': custom, 'notification_id': notification_id, 'server_instance_id': server,
                      'needs_response': needs, 'payload': payload, 'collapse_id': apns._collapse_id(payload)})
    # Every sixteenth: the fields vary independently, and the file stays small.
    turns = turns[::16]

    decisions = []
    for title, question, time_sensitive, text_input, expires_at in itertools.product(
            TITLES, QUESTIONS, [False, True], [False, True], [None, 1_794_800_000_000]):
        payload = apns.decision_payload(
            'Vera', 'vera', title, question, decision_id='decision-0123456789abcdef01234567',
            artifact_id='artifact-0123456789abcdef01234567', time_sensitive=time_sensitive, text_input=text_input,
            revision=3, expires_at=expires_at, avatar_url='https://host.example/a.png', avatar_custom=True,
            server_instance_id='srv-1')
        decisions.append({'title': title, 'question': question, 'time_sensitive': time_sensitive,
                          'text_input': text_input, 'expires_at': expires_at, 'payload': payload,
                          'collapse_id': apns._collapse_id(payload)})
    plain = apns.decision_payload('', None, 'Only a title', '', decision_id='d' * 80, artifact_id='a1')
    decisions.append({'persona': '', 'session': None, 'title': 'Only a title', 'question': '', 'time_sensitive': False,
                      'text_input': False, 'expires_at': None, 'decision_id': 'd' * 80, 'artifact_id': 'a1',
                      'revision': 1, 'avatar_url': None, 'avatar_custom': False, 'server_instance_id': '',
                      'payload': plain, 'collapse_id': apns._collapse_id(plain)})

    interruptions = [{'decision': decision, 'interrupts': apns.decision_needs_interruption(decision)}
                     for decision in ({'blocks_progress': blocks, 'urgency': urgency}
                                      for blocks in (True, False, 1, 0, None, 'yes')
                                      for urgency in ('time_sensitive', 'normal', '', None))]
    collapse = [{'payload': payload, 'collapse_id': apns._collapse_id(payload)} for payload in (
        {}, {'session': 'vera'}, {'session': '  vera  '}, {'notification_id': '  pn-1  ', 'session': 'vera'},
        {'session': 's' * 80}, {'notification_id': None, 'session': None})]

    OUT.parent.mkdir(parents=True, exist_ok=True)
    vectors = {'about': 'Written by server-rs/tools/push-vectors.py from server/lib/apns.py. Do not edit by hand.',
               'turns': turns, 'decisions': decisions, 'interruptions': interruptions, 'collapse': collapse}
    OUT.write_text(json.dumps(vectors, ensure_ascii=False, indent=1) + '\n')
    print(f'{OUT.relative_to(REPO)}: {len(turns)} turn payloads, {len(decisions)} decision payloads, '
          f'{len(interruptions)} interruptions, {len(collapse)} collapse ids, {OUT.stat().st_size} bytes')
    return 0


if __name__ == '__main__':
    sys.exit(main())
