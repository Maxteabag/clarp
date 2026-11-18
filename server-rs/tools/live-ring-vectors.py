#!/usr/bin/env python3
"""Writes the vectors the Rust Host's live rings are tested against.

A ring may be held as a live voice call (`docs/agent-rings.md`, feature `agent_ring_live`): placed
with what its producer says of an event, shown only to phones that can hold such a call, answered
with a grant that one phone joins once, and ended by a close or switched once to an ordinary call.
The owner may start such a call with an agent too (`POST /calls/live`).

This runs the Python Host's own modules (`server/lib/agent_rings.py`, `agent_ring_live.py`, and the
join of `agent_ring_live_socket.py`) with agents, phones, a clock, ids, a voice and profiles made up
here, through scripts of requests, and writes what was answered, announced, pushed, noted and put in
the audit trail after each step, and what the ring's own record says of the call, to
`crates/clarp-kernel/tests/fixtures/live-ring-vectors.json`.

    "$(sed -n 1p ~/.local/share/clarp/current/SERVICE_PYTHON)" server-rs/tools/live-ring-vectors.py

It runs in a process of its own whose every home, data, config and cache directory is a new empty
directory (the replay harness's own isolation, `tests/replay/host.py`), with no way out of the
machine. No Host is touched or started, no provider is asked anything, and nothing is pushed or
voiced: what would be is written down instead.
"""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[1]
OUT = HERE.parent / 'crates' / 'clarp-kernel' / 'tests' / 'fixtures' / 'live-ring-vectors.json'

AGENTS = [
    {'session': 'theo-97e5', 'agent_id': 'a-theo', 'persona': 'Theo'},
    {'session': 'mike-86db', 'agent_id': 'a-mike', 'persona': 'Mike'},
    {'session': 'nadia', 'agent_id': 'a-nadia', 'persona': 'Nadia'},
    {'session': 'quiet', 'agent_id': 'a-quiet', 'persona': 'Quiet', 'muted': 1},
    {'session': 'old', 'agent_id': 'a-old', 'persona': 'Old', 'archived_at': 1_700_000_000_000},
]

# The phones: two that are paired with all rights, one with few, one whose pairing was taken back.
A, B, C, D, ADMIN = 'device_a', 'device_b', 'device_c', 'device_d', 'administrator'
DEVICES = {A: 'full', B: 'full', C: 'limited', D: 'revoked'}
WATCHERS = [A, B, C, D, ADMIN, '']
SECOND, MINUTE = 1000, 60 * 1000
WHY = 'Possible water leak at pump 01'
LINE = 'Camera 2 shows a possible water leak at pump 01. Shall I find an available field worker?'
LIVE, CLASSIC = 'agent_live_v1', 'classic'

PERSONA = 'You are Theo, who guards the pumps.\n'
PROCEDURE = '# Procedure, revision 3\n1. Say what the camera saw.\n'
FILES = {'PERSONA.md': PERSONA, 'PROCEDURE.md': PROCEDURE}


def sha(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


PINS = {'persona': {'file': 'PERSONA.md', 'sha256': sha(PERSONA)}, 'procedure': {'file': 'PROCEDURE.md', 'sha256': sha(PROCEDURE)}}
PROFILES = {
    'pump-guard': {'profile_id': 'pump-guard', 'version': '3', 'agent_session': 'theo-97e5', 'display_name': 'Theo the guard',
                   'source': {'by': 'operator'}, 'files': PINS,
                   'stories': {'pump': ['persona', 'procedure'], 'bottle': ['persona'], 'user_call': ['persona']},
                   'user_call_default': 'live', 'user_call_opening': ' Hei,  det er Theo. '},
    'mike-desk': {'profile_id': 'mike-desk', 'version': '1', 'agent_session': 'mike-86db', 'display_name': 'Mike',
                  'files': PINS, 'stories': {'pump': ['persona'], 'user_call': ['persona']}},
    'quiet-one': {'profile_id': 'quiet-one', 'version': '1', 'agent_session': 'quiet', 'display_name': 'Quiet',
                  'files': PINS, 'stories': {'pump': ['persona'], 'user_call': ['persona']},
                  'user_call_default': 'live', 'user_call_opening': 'Hei.'},
    'old-one': {'profile_id': 'old-one', 'version': '1', 'agent_session': 'old', 'display_name': 'Old',
                'files': PINS, 'stories': {'user_call': ['persona']},
                'user_call_default': 'live', 'user_call_opening': 'Hei.'},
}


def event(event_id='evt-1', profile='pump-guard', story='pump', source='leak-watch', line=LINE, **more):
    told = {'version': 1, 'profile_id': profile, 'story': story, 'source': source,
            'event': {'id': event_id, 'time': '2026-11-18T04:08:00+01:00', 'camera_id': 'cam-2', 'camera_label': 'Camera 2',
                      'when_text': 'just now', 'what_text': 'Water on the floor below pump 01.', 'log': ['04:08 water seen']},
            'opening_text': line}
    told['event'].update(more)
    return told


def ring(turn='', agent='', reason=WHY, say=LINE, **more):
    return {'do': 'ring', 'turn': turn, 'agent': agent, 'reason': reason, 'opening_line': say, **more}


def live_ring(turn='a-theo', event_id='evt-1', **more):
    context = more.pop('event_context', None) or event(event_id, profile='pump-guard' if turn != 'a-mike' else 'mike-desk')
    return ring(turn, conversation_mode=LIVE, event_context=context, **more)


def look(principal, at=None):
    return {'do': 'look', 'principal': principal, 'ring': at}


def answer(principal, at=-1, **more):
    return {'do': 'answer', 'principal': principal, 'ring': at, **more}


def live_answer(principal, at=-1, request='answer-1', nonce='nonce-1', **more):
    return answer(principal, at, conversation_mode=LIVE, request_id=request, client_nonce=nonce, **more)


def decline(principal, at=-1):
    return {'do': 'decline', 'principal': principal, 'ring': at}


def cancel(turn, at=-1):
    return {'do': 'cancel', 'turn': turn, 'ring': at}


def voip(principal, token, modes=None):
    return {'do': 'voip', 'principal': principal, 'token': token, 'modes': modes}


def tick(ms):
    return {'do': 'tick', 'ms': ms}


def timer():
    return {'do': 'timer'}


def call(principal):
    return {'do': 'call', 'principal': principal}


def user_call(principal, agent='a-theo', request='call-1', nonce='nonce-1'):
    return {'do': 'user_call', 'principal': principal, 'agent': agent, 'request_id': request, 'client_nonce': nonce}


def join(principal, at=-1, **change):
    """A join with what the grant of the ring at `at` says, changed by `change` (`...` drops a key)."""
    return {'do': 'join', 'principal': principal, 'ring': at, 'change': {key: ('~drop' if value is ... else value)
                                                                        for key, value in change.items()}}


def close(principal, at=-1, reason='user_hangup', request='close-1', **change):
    if reason is ...:
        reason, change = '', {**change, 'reason': ...}
    return {'do': 'close', 'principal': principal, 'ring': at, 'reason': reason, 'request_id': request,
            'change': {key: ('~drop' if value is ... else value) for key, value in change.items()}}


def fallback(principal, at=-1, request='fallback-1', stopped=True, meanwhile=None, **change):
    """`meanwhile`: a close of the call that comes while the line of the ordinary call is voiced, between the two
    halves of the switch: its reason, its request, and how far its epoch is ahead of the one the switch names."""
    step = {'do': 'fallback', 'principal': principal, 'ring': at, 'request_id': request, 'audio_stopped': stopped,
            'change': {key: ('~drop' if value is ... else value) for key, value in change.items()}}
    if meanwhile is not None:
        reason, close_request, ahead = meanwhile
        step['meanwhile'] = {'reason': reason, 'request_id': close_request, 'ahead': ahead}
    return step


def restart():
    """The Host starts again: no call of the Host before it has a session any more."""
    return {'do': 'restart'}


def session(what, at=-1):
    """What the session of a call records on its ring: `opening` (it is said once), `active` (the voice took it),
    `audio` (the user's sound went to the voice); or `unsettled`: a session that does not end when it is told to."""
    return {'do': 'session', 'what': what, 'ring': at}


def device(principal, state):
    return {'do': 'device', 'principal': principal, 'state': state}


def prefs(principal, receive):
    return {'do': 'prefs', 'principal': principal, 'said': {'receive_rings': receive} if isinstance(receive, bool) else receive}


def android(owner, modes=(LIVE,), token=None):
    return {'do': 'android', 'owner': owner, 'modes': None if modes is None else list(modes), 'token': token or f'fcm-{owner}'}


def key(has):
    return {'do': 'key', 'has': has}


def fcm(usable):
    return {'do': 'fcm', 'usable': usable}


def taking():
    return {'do': 'taking'}


def callkit(has):
    """Whether the Host has a key of its own at the push service: only with one a ring is pushed as an incoming call."""
    return {'do': 'callkit', 'has': has}


def settings(said):
    """The owner's switches: whether agents may ring at all, and whether a locked phone shows why an agent calls."""
    return {'do': 'settings', 'said': said}


def call_push(principal, status=200, reason='', raises=False):
    """What the push service answers from now on when a ring is pushed as a call to this phone: a status and its
    reason, or nothing at all (the way to it fails)."""
    return {'do': 'call_push', 'principal': principal, 'answer': 'raises' if raises else [status, reason]}


TOKEN_A, TOKEN_B, TOKEN_C = 'a' * 64, 'b' * 64, 'c' * 64
PHONE_A = voip(A, TOKEN_A, [CLASSIC, LIVE])

SCRIPTS = [
    {'name': 'a live ring is placed for the phones that can hold one, answered with a grant, joined once, and closed',
     'steps': [live_ring(), PHONE_A, voip(B, TOKEN_B, [CLASSIC]), live_ring(), look(A), look(B), look(ADMIN), look(A, 0),
               look(B, 0), look(ADMIN, 0), call(A), live_answer(A), live_answer(A), look(A), look(A, 0), look(B, 0), call(A),
               join(A), join(A), session('opening'), session('active'), session('audio'), look(A, 0),
               close(A), close(A), close(A, request='close-2'), close(A, reason='transport_lost'), look(A, 0), join(A),
               live_answer(A)]},
    {'name': 'what a live ring must say, and who may place one',
     'steps': [PHONE_A, live_ring(event_context=None), ring('a-theo', conversation_mode=LIVE),
               ring('a-theo', conversation_mode='live', event_context=event()), ring('a-theo', event_context=event()),
               ring('a-theo', conversation_mode=CLASSIC, event_context=event()),
               live_ring(reason=' '), live_ring(say=' '), live_ring(opening_mode='listen_first'), live_ring(opening_mode='say'),
               tick(61 * SECOND), live_ring(opening_mode='sing'), live_ring(say=LINE + ' More.'), live_ring(say='x' * 601),
               live_ring(event_context=event(line='x' * 601), say='x' * 601),
               live_ring(event_context=event(story='user_call')), live_ring(event_context=event(profile='nobody')),
               live_ring(event_context=event(profile='mike-desk')), live_ring('a-mike', event_context=event(story='bottle')),
               live_ring('a-nadia', event_context=event(profile='pump-guard')), live_ring('a-quiet', event_context=event(profile='quiet-one')),
               live_ring('a-nope'), live_ring('', agent='Theo', event_id='evt-2'), live_ring('', agent='', event_id='evt-3'),
               live_ring('a-mike', event_id='evt-4'), tick(61 * SECOND), live_ring('a-mike', event_id='evt-4'), key(False),
               tick(61 * SECOND), live_ring('a-mike', event_id='evt-5'), key(True), live_ring('a-mike', event_id='evt-5')]},
    {'name': 'the same event rings once, and the same event told otherwise is refused',
     'steps': [PHONE_A, live_ring(event_id='evt-1'), live_ring(event_id='evt-1'),
               live_ring(event_context=event('evt-1', what_text='Something else.')), live_ring('a-mike', event_id='evt-1'),
               decline(A), live_ring(event_id='evt-1'), tick(61 * SECOND), live_ring(event_id='evt-1'),
               live_ring(event_context=event('evt-1', source='rk-demo-on-leak-event')),
               tick(61 * SECOND), live_ring(event_id='evt-1', reason='Another reason'), voip(A, ''), live_ring(event_id='evt-1'),
               live_ring(event_id='evt-9')]},
    {'name': 'who can hold a live call, and who is shown one',
     'steps': [live_ring(), voip(A, TOKEN_A), live_ring(), voip(A, TOKEN_A, [LIVE, LIVE]), voip(B, TOKEN_B, []),
               voip(B, TOKEN_B, [CLASSIC, LIVE, CLASSIC]), voip(B, TOKEN_B, ['other']), voip(B, TOKEN_B, 'agent_live_v1'),
               voip(C, TOKEN_C, [LIVE]), voip(D, 'd' * 64, [LIVE]), live_ring(), look(A), look(B), look(C), look(D), look(ADMIN),
               look(''), decline(B), tick(61 * SECOND), prefs(A, False), look(A), live_ring(event_id='evt-2'), look(A), look(C),
               cancel(''), prefs(C, False), prefs(D, False), tick(61 * SECOND), live_ring(event_id='evt-3'), prefs(A, True),
               prefs(C, True), look(A), cancel('a-theo'), prefs(A, 'no'), prefs(A, {'receive_rings': False, 'more': 1}),
               prefs(ADMIN, False), prefs(B, False), prefs(B, False), prefs(B, True),
               voip(A, ''), voip(C, ''), voip(D, ''), tick(61 * SECOND), live_ring(event_id='evt-4'), android(B), live_ring(event_id='evt-4'),
               fcm(True), live_ring(event_id='evt-4'), look(B), look(A), cancel(''), android(B, modes=[CLASSIC]),
               tick(61 * SECOND), live_ring(event_id='evt-5'), android(C), android(D), android(ADMIN), live_ring(event_id='evt-5'),
               android(A), prefs(A, False), live_ring(event_id='evt-5'), prefs(A, True), live_ring(event_id='evt-5'),
               live_answer(A), live_answer(B)]},
    {'name': 'a live ring is answered by one phone that asks for it, with its request and its nonce',
     'steps': [PHONE_A, voip(B, TOKEN_B, [LIVE]), live_ring(), answer(A), answer(A, conversation_mode=CLASSIC),
               answer(A, conversation_mode='live'), answer(A, conversation_mode=LIVE), live_answer(A, request=''),
               live_answer(A, nonce=5), live_answer(A, request='x' * 129), live_answer(ADMIN), live_answer(C), live_answer(D),
               live_answer(''), live_answer(A, at='rng_none'), live_answer(B, request='answer-b', nonce='nonce-b'),
               live_answer(B, request='answer-b', nonce='nonce-b'), live_answer(A), live_answer(B, request='answer-c', nonce='nonce-b'),
               live_answer(B, request='answer-b', nonce='nonce-c'), live_answer(B, request='', nonce='nonce-b'),
               answer(B), answer(B, conversation_mode=CLASSIC), decline(B), decline(A), cancel('a-theo'), look(B, 0), look(A, 0),
               tick(61 * SECOND), ring('a-theo'), answer(A, conversation_mode=LIVE), live_answer(A), answer(A),
               answer(A, conversation_mode=LIVE), tick(61 * SECOND), live_ring(event_id='evt-2'), decline(A), live_answer(A),
               tick(61 * SECOND), live_ring(event_id='evt-3'), tick(46 * SECOND), live_answer(A), tick(61 * SECOND),
               live_ring(event_id='evt-4'), device(A, 'limited'), live_answer(A), device(A, 'full'), live_answer(A)]},
    {'name': 'a grant is joined once, by the phone it was given, as it was given, in its minute',
     'steps': [PHONE_A, voip(B, TOKEN_B, [LIVE]), live_ring(), join(A), live_answer(A),
               join(B), join(ADMIN), join(A, type='agent_live.hello'), join(A, protocol=...), join(A, join_ticket=...),
               join(A, host_id=...), join(A, ring_id=...), join(A, context_digest=...), join(A, call_id=...),
               join(A, call_epoch=...), join(A, attempt_id=...), join(A, owner_nonce=...), join(A, type=...),
               join(A, call_id='alv_none'), join(A, owner_nonce='0' * 64), join(A, owner_nonce=5), join(A, attempt_id='att_none'),
               join(A, owner_nonce='~half'), join(A, join_ticket='~half'), join(A, owner_nonce=''), join(A, call_epoch=-1),
               join(A, protocol=-1), join(A, call_id=5), join(A, call_id=None),
               join(A, protocol=True), join(A, protocol='1'), join(A, protocol=1.0), join(A, call_epoch='1'), join(A, call_epoch=True),
               join(A, protocol=2), join(A, protocol=0), join(A, host_id='host-2'), join(A, ring_id='rng_none'), join(A, call_epoch=2),
               join(A, call_epoch=0), join(A, context_digest='0' * 64), join(A, join_ticket='0' * 64), join(A, join_ticket=5),
               join(A, more='is passed over'), look(A, 0), join(A), join(A, protocol=2), join(A, join_ticket='0' * 64),
               tick(61 * SECOND), live_ring(event_id='evt-2'), live_answer(A, request='answer-2'), tick(59_999), join(A, protocol=2),
               tick(1), join(A), look(A, 1), join(A), live_answer(A, request='answer-2'), close(A),
               tick(61 * SECOND), live_ring(event_id='evt-3'), live_answer(A, request='answer-3'), tick(60 * SECOND), look(B),
               join(A), tick(61 * SECOND), live_ring(event_id='evt-4'), live_answer(A, request='answer-4'), device(A, 'revoked'), join(A),
               device(A, 'full'), join(A), tick(61 * SECOND), live_ring(event_id='evt-5'), live_answer(A, request='answer-5'),
               close(A, reason='audio_not_ready'), join(A), tick(61 * SECOND), live_ring(event_id='evt-6'),
               live_answer(A, request='answer-6'), join(A, owner_nonce='n\u00f8nce')]},
    {'name': 'a call is closed by its phone, once, with the request that closed it',
     'steps': [PHONE_A, voip(B, TOKEN_B, [LIVE]), live_ring(), close(A), live_answer(A), close(B), close(ADMIN), close(C),
               close(A, call_id=...), close(A, call_id=''), close(A, owner_nonce='0' * 64), close(A, attempt_id='att_none'),
               close(A, call_id='alv_none'), close(A, owner_nonce='~half'), close(A, reason='bored'), close(A, reason=...), close(A, request=''),
               close(A, call_epoch=0), close(A, call_epoch='1'), close(A, call_epoch=...), close(A, call_epoch=2),
               close(A, reason='transport_lost', request='close-a'), look(A, 0), close(A, reason='transport_lost', request='close-a'),
               close(A, reason='user_hangup', request='close-a'), close(A, reason='transport_lost', request='close-b'),
               close(A, reason='transport_lost', request='close-a', call_epoch=2), join(A), live_answer(A),
               tick(61 * SECOND), live_ring(event_id='evt-2'), live_answer(A, request='answer-2'), join(A), session('opening'),
               session('opening'), session('active'), close(A, reason='audio_not_ready'), session('audio'), session('active'), look(A, 0),
               tick(61 * SECOND), live_ring(event_id='evt-3'), live_answer(A, request='answer-3'), tick(60 * SECOND), close(A),
               close(A), close(A, request='close-2')]},
    {'name': 'a live call falls back to an ordinary call once, before its voice opened and before the user was heard',
     'steps': [PHONE_A, voip(B, TOKEN_B, [LIVE]), live_ring(), live_answer(A), fallback(B), fallback(ADMIN),
               fallback(A, expected_epoch=...), fallback(A, expected_epoch='1'), fallback(A, stopped=False), fallback(A, stopped=1),
               fallback(A, request=''), fallback(A, expected_epoch=2), fallback(A, call_id='alv_none'), call(A),
               fallback(A), call(A), look(A, 0), fallback(A), fallback(A, request='fallback-2'), fallback(A, expected_epoch=2),
               live_answer(A), answer(A), join(A), close(A), close(A, call_epoch=2), look(A, 0),
               tick(61 * SECOND), live_ring(event_id='evt-2'), live_answer(A, request='answer-2'), join(A), session('opening'),
               fallback(A), close(A),
               tick(61 * SECOND), live_ring(event_id='evt-3'), live_answer(A, request='answer-3'), join(A), session('audio'),
               fallback(A), close(A, reason='transport_lost'), fallback(A),
               tick(61 * SECOND), live_ring(event_id='evt-4'), live_answer(A, request='answer-4'), close(A, reason='user_hangup'),
               fallback(A),
               tick(61 * SECOND), live_ring(event_id='evt-5'), live_answer(A, request='answer-5'), close(A, reason='transport_lost'),
               fallback(A), fallback(A), call(A), close(A, reason='transport_lost'), close(A, reason='user_hangup', request='close-2'),
               tick(61 * SECOND), live_ring(event_id='evt-6'), live_answer(A, request='answer-6'), join(A), session('unsettled'),
               fallback(A), fallback(A), fallback(A, request='fallback-2'), close(A), look(A, 0),
               tick(61 * SECOND), live_ring(event_id='evt-7'), live_answer(A, request='answer-7'), tick(60 * SECOND), fallback(A),
               fallback(A, expected_epoch=2)]},
    {'name': 'a close that comes while the ordinary call is being started calls the switch off',
     'steps': [PHONE_A, live_ring(), live_answer(A),
               fallback(A, meanwhile=('transport_lost', 'close-m', 0)), call(A), look(A, 0), close(A, call_epoch=2),
               tick(61 * SECOND), live_ring(event_id='evt-2'), live_answer(A, request='answer-2'),
               fallback(A, meanwhile=('transport_lost', 'close-m', 1)), call(A), look(A, 1), fallback(A),
               fallback(A, request='fallback-2'), close(A, reason='transport_lost', request='close-m', call_epoch=2), close(A),
               tick(61 * SECOND), live_ring(event_id='evt-3'), live_answer(A, request='answer-3'),
               fallback(A, meanwhile=('user_hangup', 'close-m', 1)), call(A), fallback(A),
               tick(61 * SECOND), live_ring(event_id='evt-4'), live_answer(A, request='answer-4'),
               fallback(A, stopped=False, meanwhile=('user_hangup', 'close-m', 1)), look(A, 3)]},
    {'name': 'a Host that starts again ends the live calls it finds joined, and no others',
     'steps': [PHONE_A, voip(B, TOKEN_B, [LIVE]), restart(), live_ring(), restart(), look(A, 0), live_answer(A), restart(),
               look(A, 0), join(A), restart(), restart(), look(A, 0), call(A), fallback(A), join(A), close(A, reason='transport_lost'),
               close(A, reason='transport_lost'), close(A, request='close-2'), fallback(A),
               tick(61 * SECOND), live_ring(event_id='evt-2'), live_answer(A, request='answer-2'), join(A), session('opening'),
               restart(), look(A, 1), session('active'), fallback(A), fallback(A, expected_epoch=2), close(A), fallback(A),
               tick(61 * SECOND), live_ring(event_id='evt-3'), live_answer(A, request='answer-3'), join(A), session('opening'),
               session('active'), session('audio'), restart(), look(A, 2), session('audio'), fallback(A), restart(),
               tick(61 * SECOND), live_ring(event_id='evt-4'), live_answer(A, request='answer-4'), fallback(A), restart(),
               fallback(A), look(A, 3), close(A),
               tick(61 * SECOND), live_ring(event_id='evt-5'), live_answer(A, request='answer-5'), join(A),
               close(A, reason='transport_lost'), restart(), fallback(A), look(A, 4),
               tick(61 * SECOND), live_ring(event_id='evt-6'), live_answer(A, request='answer-6'), tick(59 * SECOND), restart(),
               join(A), restart(), tick(2 * SECOND), look(A, 5)]},
    {'name': 'a call the user began is ended by a Host that starts again, and the user may call anew at once',
     'steps': [taking(), PHONE_A, voip(B, TOKEN_B, [LIVE]), user_call(B), restart(), look(B, 0), user_call(B, request='call-2'),
               join(B), restart(), look(B, 1), user_call(B, request='call-2'), user_call(B, request='call-3'), fallback(B, 1),
               close(B, 1), join(B), session('opening'), session('active'), restart(), user_call(B, request='call-4'),
               look(B), close(B)]},
    {'name': 'the user calls an agent that takes calls live',
     'steps': [taking(), user_call(A), PHONE_A, voip(B, TOKEN_B, [LIVE]), user_call(A), look(A), look(B), look(A, 0), look(B, 0),
               look(ADMIN, 0), call(A), user_call(A), user_call(A, nonce='nonce-2'), user_call(A, agent='a-mike'),
               user_call(B), user_call(A, request='call-2'), user_call(B, request='call-b'), close(B),
               user_call(B, agent='a-mike', request='call-m'),
               user_call(B, agent='a-nadia', request='call-n'), user_call(B, agent='a-quiet', request='call-q'), close(B),
               user_call(B, agent='a-old', request='call-o'), user_call(B, agent='a-nope', request='call-x'),
               user_call(B, agent='', request='call-x'), user_call(B, agent=5, request='call-x'), user_call(B, request=''),
               user_call(B, nonce=''), user_call(ADMIN), user_call(C), user_call(D), user_call(''),
               live_answer(B, at=0), live_answer(A, at=0, request='call-1'), live_answer(A, at=0, request='other'),
               decline(B, 0), decline(ADMIN, 0), decline('', 0), answer(B, 0), answer(ADMIN, 0),
               decline(A, 0), cancel('a-theo', 0), cancel('', 0), fallback(A, 0), join(A, 0), session('opening', 0), session('active', 0),
               user_call(A), user_call(A, request='call-3'), close(A, 0), user_call(A), user_call(A, request='call-3'),
               look(A, 0), look(A, 1), taking(),
               close(A, 1), close(A), live_ring(), user_call(A, request='call-4'), user_call(B, agent='a-quiet', request='call-r'),
               cancel(''), user_call(A, request='call-4'), user_call(A, request='call-9'), tick(60 * SECOND),
               user_call(A, request='call-4'), join(A), user_call(A, request='call-4'), look(A, 5),
               user_call(A, request='call-5'), tick(59_999), user_call(A, request='call-5'),
               join(A), tick(MINUTE), user_call(A, request='call-5'), user_call(A, request='call-9'), close(A),
               user_call(A, request='call-5'), key(False), user_call(A, request='call-6'),
               key(True), voip(A, TOKEN_A, [CLASSIC]), user_call(A, request='call-6'), android(A), user_call(A, request='call-6'),
               prefs(A, False), close(A), user_call(A, request='call-7'), device(A, 'limited'), user_call(A, request='call-8'),
               close(A)]},
    {'name': 'the ring a phone answered stays its own, whatever the phone says of itself afterwards',
     'steps': [PHONE_A, voip(B, TOKEN_B, [LIVE]), live_ring(), live_answer(A), prefs(A, False), look(A, 0), look(A), look(B, 0),
               look(B), look(ADMIN, 0), live_answer(A), join(A), session('opening'), session('active'), look(A, 0), close(A),
               look(A, 0), prefs(A, True),
               tick(61 * SECOND), live_ring(event_id='evt-2'), live_answer(A, request='answer-2'), voip(A, TOKEN_A, [CLASSIC]),
               look(A, 1), look(A), look(B, 1), live_answer(A, request='answer-2'), join(A), close(A), look(A, 1), voip(A, ''),
               look(A, 1), look(A, 0), PHONE_A,
               tick(61 * SECOND), ring('a-theo'), look(B), prefs(B, False), look(B), look(B, 2), answer(B), prefs(B, True),
               answer(A), prefs(A, False), look(A, 2), look(A), look(B, 2), call(A), answer(A), prefs(A, True),
               tick(61 * SECOND), live_ring(event_id='evt-3'), prefs(A, False), look(A), look(A, 3), live_answer(A, request='answer-3'),
               prefs(A, True), live_answer(A, request='answer-3'), prefs(A, False), look(A, 3), fallback(A), look(A, 3), call(A)]},
    {'name': 'a ring is pushed as an incoming call to the phones that left a token for one, and alerted where that reached none',
     'steps': [ring('a-theo'), decline(A), tick(61 * SECOND),
               PHONE_A, voip(B, TOKEN_B, [CLASSIC]), voip(C, TOKEN_C), voip(D, 'd' * 64, [LIVE]), voip(B, 'AB' * 32, 'sandbox'),
               ring('a-mike'), decline(A), tick(61 * SECOND),
               call_push(A, 410, 'Unregistered'), call_push(B, raises=True), ring('a-theo'), cancel(''), tick(61 * SECOND),
               call_push(C, 400, 'BadDeviceToken'), call_push(D, 403, 'InvalidProviderToken'), ring('a-mike'), tick(46 * SECOND),
               timer(), tick(61 * SECOND),
               call_push(A), prefs(B, False), prefs(C, False), prefs(D, False), ring('a-theo'), answer(A), tick(61 * SECOND),
               prefs(A, False), ring('a-mike'), cancel('a-mike'), prefs(A, True), tick(61 * SECOND),
               # Why an agent calls is in a call push only once the owner lets a locked phone show it.
               settings({'show_reason': True}), ring('a-theo'), decline(A), tick(61 * SECOND),
               live_ring('a-mike', event_id='evt-0'), decline(A), tick(61 * SECOND),
               settings({'show_reason': False}), ring('a-theo'), decline(A), tick(61 * SECOND),
               callkit(False), ring('a-theo'), decline(A), tick(61 * SECOND),
               callkit(True), prefs(D, True), call_push(D), live_ring('a-mike', event_id='evt-1'), live_answer(D), live_answer(A),
               close(A), tick(61 * SECOND),
               call_push(A, 500, 'InternalServerError'), call_push(D, raises=True), live_ring(event_id='evt-2'), cancel(''),
               tick(61 * SECOND), callkit(False), live_ring('a-mike', event_id='evt-3'), decline(A)]},
    {'name': 'rings are kept, and a live one keeps what it was told',
     'steps': [PHONE_A, voip(B, TOKEN_B, [LIVE, LIVE]), voip(C, TOKEN_C), voip(D, 'd' * 64, [CLASSIC, CLASSIC])]
     + [step for n in range(3) for step in (live_ring(event_id=f'evt-{n}'), live_answer(A, request=f'answer-{n}'),
                                                                 join(A), close(A), tick(61 * SECOND))],
     'kept': True},
]


def run() -> dict:
    """Runs in a process of its own: what the Python Host's modules do."""
    spec = importlib.util.spec_from_file_location('replay_host', REPO / 'tests' / 'replay' / 'host.py')
    host = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(host)
    root = pathlib.Path(tempfile.mkdtemp(prefix='clarp-live-ring-vectors-', dir='/var/tmp'))
    (root / 'scenario-config.toml').write_text('')
    host.isolate(root)
    host.loopback_only()
    sys.path.insert(0, str(REPO / 'server'))
    import datetime as datetimes
    import threading

    from lib import (agent_ring_live, agent_ring_live_socket, agent_ring_openings, agent_rings, agent_rings_base, agents,
                     apns, config, group_calls, oracle_contact, settings_store)

    live = agent_ring_live
    # The Python Host's own pushing of a ring as an incoming call, before anything stands in for it.
    push_as_calls = apns.send_agent_ring_voip
    group_calls._host_id = lambda: 'host-1'
    live.ROOT = root / 'agent-live'
    for name, held in PROFILES.items():
        folder = live.ROOT / 'profiles' / name
        folder.mkdir(parents=True)
        (folder / 'profile.json').write_text(json.dumps(held), encoding='utf-8')
        for file, text in FILES.items():
            (folder / file).write_text(text, encoding='utf-8')

    def played(script):
        for prefix in (group_calls.KEY, 'agent_ring.'):
            for key_ in settings_store.keys_with_prefix(prefix):
                settings_store.delete(key_)
        live.reset_for_tests()
        heard, noted, pushed, trail = [], [], [], []
        state = {'now': 1_800_000_000_000, 'made': 0, 'rings': [], 'devices': dict(DEVICES), 'android': [], 'key': True,
                 'fcm': False, 'callkit': True, 'answers': {}, 'sends': []}

        class Stream:
            def broadcast(self, told):
                told = json.loads(json.dumps(dict(told)))
                told['seen_by'] = [who for who in WATCHERS if agent_rings.event_visible(told, who or None)]
                heard.append(told)

        class Context:
            stream = Stream()
            tts = None

        class Minted:
            def __init__(self, number):
                self.hex = f'{number:032x}'

        def mint():
            state['made'] += 1
            return Minted(state['made'])

        def secret(count):
            state['made'] += 1
            return f'{state["made"]:0{count * 2}x}'

        class Moment(datetimes.datetime):
            @classmethod
            def now(cls, tz=None):
                return cls.fromtimestamp(state['now'] / 1000, tz)

        def known():
            return [dict(agent) for agent in AGENTS]

        oracle_contact.roster = lambda: [agent for agent in known() if not agent.get('archived_at')]
        agents.get_by_agent_id = lambda agent_id: next((agent for agent in known() if agent['agent_id'] == agent_id), None)
        group_calls.bind(Context())
        agent_rings.bind(Context())
        for module in (agent_rings, agent_rings_base, agent_ring_openings, group_calls):
            module.now_ms = lambda: state['now']
        live.datetime = Moment
        group_calls.uuid.uuid4 = mint
        agent_rings.secrets.token_hex = secret
        live.provider_problem = lambda: None if state['key'] else 'agent-live calls need an OpenAI API key on this Host'
        live.device_ok = lambda principal: (not principal.startswith('device_')) or state['devices'].get(principal) == 'full'
        apns.declared_mode_rows = lambda: [dict(row) for row in state['android']]
        apns.fcm_configured = lambda cfg: state['fcm']
        apns.owned_active_tokens = lambda: []
        group_calls._notify = lambda record, notices: noted.extend(
            {'agent_id': group_calls._participant(record, session_)['agent_id'],
             'id': f"{record['call_id']}:{record['revision']}:{group_calls._participant(record, session_)['agent_id']}",
             'text': 'Group call: ' + text}
            for session_, text in notices.items() if group_calls._participant(record, session_) is not None)
        agent_rings._voice = lambda text, agent: f"/audio/{state['now']}__{agent['session']}.mp3"
        agent_rings._schedule_expiry = lambda record: None

        class Direct:
            """A stand-in for the way to the push service with a key of the Host's own: it takes what it is handed for a
            phone and answers what the story says."""

            def __init__(self, cfg):
                pass

            def send(self, token, environment, payload, *, push_type='alert', priority='10', collapse_id=None,
                     expiration=None):
                principal = next((row['principal'] for row in agent_rings.voip_tokens() if row['token'] == token), '')
                answer = state['answers'].get(principal, [200, ''])
                state['sends'].append({'principal': principal, 'token': token, 'environment': environment,
                                       'payload': payload, 'push_type': push_type, 'priority': priority,
                                       'collapse_id': collapse_id, 'expiration': expiration, 'answered': answer})
                if answer == 'raises':
                    raise RuntimeError('the way to the push service is gone')
                return answer[0], answer[1], ''

            def reset(self):
                pass

        apns._DirectTransport = Direct
        apns._server_instance_id = lambda: 'host-1'
        apns.log = lambda *said, **more: None
        apns.log_exception = lambda *said, **more: None
        config.Config.apns_direct_ready = lambda self: state['callkit']

        def voip_push(record, tokens):
            """The Python Host's own pushing of a ring as a call, over the stand-in: what it handed over for each phone,
            and what it says became of it."""
            state['sends'] = []
            counted = push_as_calls(record, tokens)
            pushed.append({'ring_id': record['ring_id'], 'kind': 'ring', 'state': record['state'],
                           'voip': [row['principal'] for row in tokens], 'sends': state['sends'], 'counted': counted})
            return counted

        def alert(record, kind, tokens=None, exclude_owners=None):
            pushed.append({'ring_id': record['ring_id'], 'kind': kind, 'state': record['state'],
                           'alert': sorted(exclude_owners or [])})
            return {'enabled': True, 'sent': 1, 'failed': 0, 'disabled': 0}

        def by_fcm(record, kind, rows):
            pushed.append({'ring_id': record['ring_id'], 'kind': kind, 'state': record['state'],
                           'fcm_live': [row['owner'] for row in rows]})

        apns.send_agent_ring_voip = voip_push
        apns.send_agent_ring = alert
        agent_rings._LIVE_FCM_SENDER.submit = by_fcm
        agent_rings._notice = lambda record, text: noted.append(
            {'agent_id': record['agent_id'], 'id': f"{record['ring_id']}:{record['state']}", 'text': 'Call: ' + text})
        write = agent_rings_base._audit

        def audit(entry):
            trail.append(json.loads(json.dumps({'at': state['now'], **entry})))
            write(entry)

        agent_rings._audit = audit

        def which(step):
            at = step.get('ring')
            if isinstance(at, int):
                return state['rings'][at] if -len(state['rings']) <= at < len(state['rings']) else 'rng_none'
            return at or ''

        def record_of(ring_id):
            return next((record for record in agent_rings_base._load() if record['ring_id'] == ring_id), None)

        def changed(base, change):
            said = dict(base)
            for name, value in change.items():
                if value == '~drop':
                    said.pop(name, None)
                elif value == '~half':
                    said[name] = said[name][:len(said[name]) // 2]
                else:
                    said[name] = value
            return said

        class Unsettled:
            """A session that is told to end and does not: its provider never says that it closed."""

            def __init__(self, call_id):
                self.call_id = call_id
                self.upstream_close_done = threading.Event()
                self.upstream_close_done.set()

            def retire(self, reason):
                pass

            def settled(self):
                return False

        def act(step):
            do = step['do']
            if do == 'tick':
                state['now'] += step['ms']
                return None
            if do == 'key':
                state['key'] = step['has']
                return None
            if do == 'fcm':
                state['fcm'] = step['usable']
                return None
            if do == 'callkit':
                state['callkit'] = step['has']
                return None
            if do == 'call_push':
                state['answers'][step['principal']] = step['answer']
                return None
            if do == 'device':
                state['devices'][step['principal']] = step['state']
                return None
            if do == 'android':
                state['android'] = [row for row in state['android'] if row['owner'] != step['owner']]
                if step['modes'] is not None:
                    state['android'].append({'owner': step['owner'], 'token': step['token'],
                                             'supported_conversation_modes': step['modes']})
                return None
            if do == 'timer':
                return agent_rings.expire_due()
            if do == 'call':
                return group_calls.snapshot(step['principal'])
            if do == 'taking':
                return {'lenient': dict(sorted(live.user_call_profiles(strict=False).items())),
                        'kept': settings_store.get(live.USER_CALL_BINDINGS_KEY)}
            if do == 'voip':
                return agent_rings.register_voip_token(step['principal'], step['token'],
                                                       supported_conversation_modes=step['modes'])
            if do == 'prefs':
                return agent_rings.set_receive_rings(step['principal'], step['said'])
            if do == 'settings':
                return agent_rings.update_settings(step['said'])
            if do == 'ring':
                more = {name: step[name] for name in ('conversation_mode', 'event_context', 'opening_mode') if name in step}
                return agent_rings.ring(caller_agent_id=step['turn'], agent=step['agent'], reason=step['reason'],
                                        opening_line=step['opening_line'], **more)
            if do == 'user_call':
                return agent_rings.user_call_live(step['principal'], step['agent'], step['request_id'], step['client_nonce'])
            ring_id = which(step)
            if do == 'look':
                principal = step['principal']
                record = agent_rings.get(ring_id) if ring_id else agent_rings.current(principal or None)
                if record is not None and not agent_rings.event_visible(record, principal or None) and \
                        record.get('answered_by') != (principal or None):
                    record = None
                if ring_id and record is None:
                    raise agent_rings.CallError(404, 'unknown call')
                return {'ring': record, 'settings': agent_rings.settings()}
            if do == 'answer':
                more = {name: step[name] for name in ('conversation_mode', 'opening_mode', 'request_id', 'client_nonce')
                        if name in step}
                return agent_rings.answer(step['principal'], ring_id, **more)
            if do == 'decline':
                return agent_rings.decline(step['principal'], ring_id)
            if do == 'cancel':
                return agent_rings.cancel(step['turn'], ring_id)
            kept = record_of(ring_id) or {}
            if do == 'join':
                said = changed({'type': 'agent_live.join', 'protocol': 1, 'host_id': 'host-1', 'ring_id': ring_id,
                                'call_id': kept.get('_live_call_id', ''), 'call_epoch': kept.get('_live_call_epoch', 0),
                                'attempt_id': kept.get('_live_attempt_id', ''),
                                'context_digest': kept.get('_live_context_digest', ''),
                                'owner_nonce': kept.get('_live_owner_nonce', ''),
                                'join_ticket': kept.get('_live_join_ticket', '')}, step['change'])
                step['said'] = said
                try:
                    joined = agent_ring_live_socket._join(step['principal'], said)
                except agent_ring_live_socket.Refused as no:
                    return {'refused': no.code}
                except TypeError:
                    return {'raised': 'TypeError'}
                return {'joined': joined['_live_call_id'], 'ids': agent_ring_live_socket._claimed_ids(said)}
            if do == 'close':
                said = changed({'call_id': kept.get('_live_call_id', ''), 'owner_nonce': kept.get('_live_owner_nonce', ''),
                                'attempt_id': kept.get('_live_attempt_id', ''), 'call_epoch': kept.get('_live_call_epoch', 0),
                                'reason': step['reason'], 'request_id': step['request_id']}, step['change'])
                step['said'] = said
                return live.close(step['principal'], said)
            if do == 'fallback':
                said = changed({'call_id': kept.get('_live_call_id', ''), 'owner_nonce': kept.get('_live_owner_nonce', ''),
                                'attempt_id': kept.get('_live_attempt_id', ''),
                                'expected_epoch': kept.get('_live_call_epoch', 0), 'audio_stopped': step['audio_stopped'],
                                'request_id': step['request_id']}, step['change'])
                step['said'] = said
                between = step.get('meanwhile')
                if between is None:
                    return live.fallback(step['principal'], said)
                closing = {'call_id': said.get('call_id', ''), 'owner_nonce': said.get('owner_nonce', ''),
                           'attempt_id': said.get('attempt_id', ''), 'call_epoch': kept.get('_live_call_epoch', 0) + between['ahead'],
                           'reason': between['reason'], 'request_id': between['request_id']}
                between['said'] = closing
                between['came'] = False
                voice = agent_rings._voice

                def voiced(text, agent):
                    # The close comes while the line is voiced: once.
                    agent_rings._voice = voice
                    between['came'] = True
                    try:
                        between['result'], between['refused'] = live.close(step['principal'], closing), None
                    except agent_rings.CallError as error:
                        between['result'], between['refused'] = None, {'status': error.status, 'body': error.body()}
                    return voice(text, agent)

                agent_rings._voice = voiced
                try:
                    return live.fallback(step['principal'], said)
                finally:
                    agent_rings._voice = voice
            if do == 'restart':
                # A Host that starts has no session of a call and has retired none: both are of the process.
                live._SESSIONS.clear()
                with live._RETIRED_LOCK:
                    live._RETIRED.clear()
                return {'retired': agent_rings.recover_joined_live_calls()}
            if do == 'session':
                call_id, what = kept.get('_live_call_id', ''), step['what']
                if what == 'unsettled':
                    live._SESSIONS[call_id] = Unsettled(call_id)
                    return None
                # What the session of the call does on its record (agent_ring_live_socket.LiveSocket._record).
                with agent_rings._lock:
                    records = agent_rings._load()
                    record = next((r for r in records if r.get('_live_call_id') == call_id and call_id), None)
                    if (record is None or record.get('_live_call_epoch') != kept.get('_live_call_epoch')
                            or record.get('_live_phase') not in live.LIVE_PHASES):
                        return {'recorded': False}
                    if what == 'opening':
                        if record.get('_live_opening_consumed'):
                            return {'recorded': False}
                        record.update(_live_opening_consumed=True, _live_phase='ready')
                    elif what == 'active':
                        record.update(_live_phase='active')
                    else:
                        record.update(_live_audio_forwarded=True)
                    agent_rings._save(records)
                return {'recorded': True}
            raise ValueError(do)

        def of_the_call(record):
            """What a ring's own record says of its call: what the Host keeps and never shows."""
            if record is None:
                return None
            names = ('conversation_mode', '_live_call_id', '_live_call_epoch', '_live_attempt_id', '_live_owner_nonce',
                     '_live_join_ticket', '_live_join_expires_at', '_live_join_consumed', '_live_phase', '_live_retired_reason',
                     '_live_close_receipt', '_live_fallback_request_id', '_live_fallback_cancelled', '_live_opening_consumed',
                     '_live_audio_forwarded', '_live_request_id', '_live_client_nonce', '_user_call', '_live_event_key',
                     '_live_event_digest', '_live_context_digest', 'state', 'call_id', 'opening_audio_url', 'answered_by')
            said = {name: record[name] for name in names if name in record}
            if record.get('_live_snapshot'):
                said['_live_snapshot'] = True
            return said

        rows = []
        for step in script['steps']:
            step = copy.deepcopy(step)
            del heard[:], noted[:], pushed[:], trail[:]
            made = state['made']
            try:
                result, refused = act(step), None
            except agent_rings.CallError as error:
                result, refused = None, {'status': error.status, 'body': error.body()}
            placed = (result or {}).get('ring') if isinstance(result, dict) and step['do'] in ('ring', 'user_call') else None
            if isinstance(placed, dict) and placed['ring_id'] not in state['rings']:
                state['rings'].append(placed['ring_id'])
            row = {'step': step, 'now': state['now'], 'result': result, 'refused': refused, 'events': list(heard),
                   'notices': list(noted), 'pushes': list(pushed), 'audit': list(trail), 'minted': state['made'] - made}
            if 'ring' in step:
                row['step']['ring_id'] = which(step)
                row['kept'] = of_the_call(record_of(which(step)))
            elif step['do'] in ('ring', 'user_call') and isinstance(placed, dict):
                kept = record_of(placed['ring_id'])
                row['kept'] = of_the_call(kept)
                row['snapshot'] = live.digest(kept['_live_snapshot']) if kept.get('_live_snapshot') else None
            rows.append(json.loads(json.dumps(row)))
        kept = None
        if script.get('kept'):
            kept = {'rings': [record['ring_id'] for record in agent_rings_base._load()],
                    'snapshots': [live.digest(record['_live_snapshot']) if record.get('_live_snapshot') else None
                                  for record in agent_rings_base._load()],
                    'voip': agent_rings.voip_tokens()}
        return {'name': script['name'], 'steps': rows, 'kept': kept}

    answered = {'agents': AGENTS, 'devices': DEVICES, 'watchers': WATCHERS,
                'profiles': {name: json.dumps(held) for name, held in PROFILES.items()}, 'files': FILES,
                'scripts': [played(script) for script in SCRIPTS], 'join_ms': agent_rings.LIVE_JOIN_MS}
    shutil.rmtree(root, ignore_errors=True)
    return answered


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == '--run':
        print(json.dumps(run()))
        return 0
    ran = subprocess.run([sys.executable, '-I', __file__, '--run'], capture_output=True, text=True,
                         env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}, check=False)
    if ran.returncode != 0:
        print(ran.stderr[-4000:], file=sys.stderr)
        return 1
    said = json.loads(ran.stdout.splitlines()[-1])
    out = ['{', f' "join_ms": {said["join_ms"]},']
    for name in ('agents', 'devices', 'watchers', 'profiles', 'files'):
        out.append(f' {json.dumps(name)}: {json.dumps(said[name], ensure_ascii=True)},')
    out.append(' "scripts": [')
    for at, script in enumerate(said['scripts']):
        out.append('  {"name": ' + json.dumps(script['name']) + ', "kept": ' + json.dumps(script['kept'], ensure_ascii=True)
                   + ', "steps": [')
        rows = script['steps']
        out.extend('   ' + json.dumps(row, ensure_ascii=True) + (',' if index < len(rows) - 1 else '')
                   for index, row in enumerate(rows))
        out.append('  ]}' + (',' if at < len(said['scripts']) - 1 else ''))
    out.append(' ]')
    out.append('}')
    OUT.write_text('\n'.join(out) + '\n')
    steps = [row for script in said['scripts'] for row in script['steps']]
    refused = sum(1 for row in steps if row['refused'] or (isinstance(row['result'], dict) and 'refused' in row['result']))
    placed = sum(1 for row in steps if row['step']['do'] in ('ring', 'user_call') and row['result'] and row['result'].get('changed', True))
    joined = sum(1 for row in steps if isinstance(row['result'], dict) and 'joined' in row['result'])
    print(f'{OUT.relative_to(REPO)}: {len(said["scripts"])} scripts of {len(steps)} steps ({refused} refused, {placed} rings placed, '
          f'{joined} joined)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
