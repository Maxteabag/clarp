#!/usr/bin/env python3
"""Writes down what the Python Host's memory of the Oracle's threads does.

A call of the Oracle belongs to a thread: what one owner and the Oracle's contact said to each other, across
calls. The Host keeps of a thread which connection has it now, what was kept of the talk, the actions that were
begun for the user's words (each under a name that is the same whenever its place is asked for, so that none is
begun twice), the work that was handed on in it, what its voice was told of, and what its user added. When a
call begins the voice is handed an excerpt of all that, whole records within a bound.

The rules are `server/lib/oracle_memory.py`, with `oracle_attention.claim_context` and
`oracle_result_context.result_payload`, run here as they are on a database of this program's own, through
stories of what is asked of them; after every step it is written down what was answered and what the tables
have.

    <the Python Host's interpreter> server-rs/tools/oracle-memory-vectors.py

It runs in a process of its own whose every home, data, config and cache directory is a new empty directory
(the replay harness's own isolation), with no way out of the machine, in one time zone that is named in the
file. The same file at every run. Nothing is logged.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import re
import subprocess
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parents[2]
OUT = REPO / 'server-rs' / 'crates' / 'clarp-kernel' / 'tests' / 'fixtures' / 'oracle-memory-vectors.json'
NOW = 1_800_000_000_000
ZONE = 'Europe/Oslo'
PHONE, OTHER = 'device-phone-1', 'device-phone-2'
LONG = ('The pump at station four ran dry at 03:12 and tripped its breaker. I reset it, checked the float switch, '
        'which was stuck, and freed it! Who owns the spare switch? ')


def story(name, *steps):
    return {'name': name, 'steps': list(steps)}


def opened(connection, owner=PHONE, contact='theo', **more):
    """A call begins: the thread of this owner and contact is opened for a connection."""
    return {'do': 'open', 'connection': connection, 'owner': owner, 'contact': contact, **more}


def passes(ms):
    return {'do': 'passes', 'ms': ms}


def asked(connection, what, **said):
    """Something is asked of the memory of the thread a connection opened."""
    return {'do': what, 'connection': connection, **said}


def forged(connection, thread_id, owner, as_connection):
    """A question is asked of a thread by one who did not open it: as this owner, under this connection's name."""
    return {'do': 'forge', 'connection': connection, 'thread': thread_id, 'owner': owner, 'as': as_connection}


def begun(connection, revision, index, tool='delegate_to_agent', **arguments):
    return asked(connection, 'admission', revision=revision, index=index, tool=tool, arguments=arguments)


def delegation(delegation_id, owner=PHONE, status='completed', text='Done.', **more):
    """A delegation as the book of delegations has it."""
    return {'do': 'delegation', 'row': {'delegation_id': delegation_id, 'owner_principal': owner, 'session': 'theo',
                                        'request_text': 'Look into the pump.', 'status': status, 'result_text': text,
                                        'error': '', **more}}


def fragment(role, text, at, **more):
    return {'role': role, 'text': text, 'provider_session': 'conn-0', 'end_ms': 10, 'revision': 1, 'at': at, **more}


def rtc(owner, call_id, replacement=False):
    """The name of the work an action hands on, as the tools of the fleet make it."""
    import hashlib
    return 'rtc-' + hashlib.sha256((owner + (':follow:' if replacement else ':') + call_id).encode()).hexdigest()[:40]


def call_id(thread, revision, index):
    import hashlib
    return 'oracle-' + hashlib.sha256(f'{thread}:{revision}:{index}'.encode()).hexdigest()[:40]


FIRST = 'oracle-' + f'{1:032x}'
ROSTER = {'agents': [{'name': 'Theo', 'session': 'theo', 'backend': 'claude', 'personality': ''},
                     {'name': 'Mike', 'session': 'mike', 'backend': 'codex', 'personality': 'Dry.'}],
          'oracle_contact': 'theo', 'note': 'These are the agents you can reach.'}
MANY = {'agents': [{'name': f'N{n}', 'session': f's{n}'} for n in range(33)], 'oracle_contact': None}
# As many agents as a voice is told by name, and not one more.
THIRTY = {'agents': [{'name': f'N{n}', 'session': f's{n}'} for n in range(30)], 'oracle_contact': None}
STORIES = [
    story('a thread is opened for a call, and the next call of the same owner and contact goes on in it',
          opened('c-1'), asked('c-1', 'load'), passes(1000), opened('c-2'), opened('c-3', contact='mike'),
          opened('c-4', owner=OTHER), opened('c-5', owner=''), passes(1000), opened('c-6', fresh=True),
          opened('c-7'), opened('c-8', thread_id=FIRST), opened('c-9'), passes(5),
          opened('c-10', thread_id=FIRST, fresh=True), opened('c-11', thread_id='oracle-' + 'f' * 32),
          opened('c-12', thread_id=FIRST, owner=OTHER), opened('c-13', thread_id=FIRST, contact='mike'),
          opened('c-14', thread_id='not an id!'), opened('c-15', thread_id=''), opened('c-16', fresh=True),
          opened('c-17', fresh=True), opened('c-18')),
    story('a thread is asked for by a name of letters, digits and four signs, no longer than a name may be',
          opened('c-1'), opened('c-2', thread_id='a' * 160), opened('c-3', thread_id='a' * 161),
          opened('c-4', thread_id='a.b'), opened('c-5', thread_id='a_b'), opened('c-6', thread_id='a:b'),
          opened('c-7', thread_id='a-b'), opened('c-8', thread_id='AZaz09'), opened('c-9', thread_id='a b'),
          opened('c-10', thread_id='a/b'), opened('c-11', thread_id='a' + chr(0xe9))),
    story('of several threads the one a connection has goes on, else the one that was last written',
          opened('c-1'), passes(10), opened('c-2', fresh=True), passes(10), asked('c-2', 'save', state={'revision': 1}),
          passes(10), opened('c-3', fresh=True), asked('c-3', 'close'), passes(10), opened('c-4'),
          asked('c-4', 'close'), passes(10), opened('c-5', thread_id=FIRST), asked('c-5', 'close'), opened('c-6'),
          # And of two that were last written at the same moment, the one that was made last.
          asked('c-6', 'close'), opened('c-7', thread_id='oracle-' + f'{3:032x}'), asked('c-7', 'close'), opened('c-8')),
    story('what is kept of the talk is kept by the connection that has the thread, within a bound',
          opened('c-1'), asked('c-1', 'save', state={'revision': 3, 'fragments': [fragment('user', 'Hello.', NOW)],
                                                     'narration': 'off'}),
          asked('c-1', 'load'), passes(50), opened('c-2'), asked('c-2', 'load'),
          asked('c-1', 'save', state={'revision': 4}), asked('c-1', 'load'),
          asked('c-1', 'observe', event={'type': 'x'}, key='k'), asked('c-1', 'admission', revision=4, index=0,
                                                                     tool='get_agent_status', arguments={}),
          asked('c-1', 'link', operation_id='rtc-1'), asked('c-1', 'claim', kind='decision', id='dec-1', reference_ts=5),
          asked('c-2', 'save', state={'revision': 4, 'text': 'x' * (2 * 1024 * 1024 - 40)}),
          asked('c-2', 'save', state={'revision': 5, 'text': 'x' * (2 * 1024 * 1024)}),
          # Exactly as much as may be kept, a byte more, and letters of two bytes that are few enough as signs.
          asked('c-2', 'save', state={'revision': 6, 'text': 'x' * (2 * 1024 * 1024 - 24)}),
          asked('c-2', 'save', state={'revision': 7, 'text': 'x' * (2 * 1024 * 1024 - 23)}),
          asked('c-2', 'save', state={'revision': 8, 'text': chr(0xe9) * (1024 * 1024 - 6)}),
          asked('c-2', 'save', state={'revision': 9, 'text': 'kept after all that'}),
          asked('c-2', 'load'),
          asked('c-2', 'observe', event={'type': 'session.input_transcript.delta', 'delta': 'Hello'}, key='conn:1'),
          asked('c-2', 'observe', event={'type': 'other'}, key='conn:1'),
          asked('c-2', 'observe', event={'type': 'other'}, key='conn:2'),
          asked('c-2', 'observe', event={'text': 'x' * 262144}, key='conn:3')),
    story('an action that is begun is written down once, under a name of its place, and ended once',
          opened('c-1'),
          asked('c-1', 'admission', revision=1, index=0, tool='delegate_to_agent', arguments={'request': 'x', 'agent': 'theo'}),
          asked('c-1', 'admission', revision=1, index=0, tool='delegate_to_agent', arguments={'agent': 'theo', 'request': 'x'}),
          asked('c-1', 'admission', revision=1, index=0, tool='delegate_to_agent', arguments={'agent': 'mike', 'request': 'x'}),
          asked('c-1', 'admission', revision=1, index=0, tool='cancel_agent', arguments={'agent': 'theo', 'request': 'x'}),
          asked('c-1', 'admission', revision=1, index=1, tool='get_agent_status', arguments={'agent': 'Th' + chr(0xe9) + 'o'}),
          asked('c-1', 'admission', revision=2, index=0, tool='cancel_agent', arguments={'agent': 'theo'}),
          passes(7), asked('c-1', 'finish', revision=1, index=1, result={'agent': 'Theo', 'state': 'idle'}),
          passes(7), asked('c-1', 'finish', revision=1, index=1, result={'agent': 'Theo', 'state': 'another answer'}),
          asked('c-1', 'finish', revision=9, index=9, result={'state': 'of no action'}),
          asked('c-1', 'admissions'), asked('c-1', 'admissions', revision=1), asked('c-1', 'admissions', revision=3)),
    story('work that an action handed on is the work of the thread, where it is the owner\'s',
          opened('c-1'), delegation('rtc-1', created_at=30), delegation('rtc-2', created_at=10),
          delegation('rtc-3', created_at=30), delegation('rtc-0', created_at=30, status='accepted', text=''),
          delegation('rtc-theirs', owner=OTHER),
          asked('c-1', 'admission', revision=1, index=0, tool='delegate_to_agent', arguments={'agent': 'theo', 'request': 'x'}),
          asked('c-1', 'finish', revision=1, index=0, result={'status': 'accepted', 'operation_id': 'rtc-1'}),
          asked('c-1', 'link', operation_id='rtc-2'), asked('c-1', 'link', operation_id='rtc-2'),
          asked('c-1', 'link', operation_id='rtc-3'), asked('c-1', 'link', operation_id='rtc-0'),
          asked('c-1', 'link', operation_id='rtc-theirs'), asked('c-1', 'link', operation_id='rtc-nobodys'),
          asked('c-1', 'admission', revision=2, index=0, tool='delegate_to_agent', arguments={'agent': 'theo', 'request': 'y'}),
          asked('c-1', 'finish', revision=2, index=0, result={'status': 'accepted', 'operation_id': 'rtc-theirs'}),
          asked('c-1', 'work'), opened('c-2', owner=OTHER), asked('c-2', 'work')),
    story('an action whose receipt was lost is ended by the work it is known to have handed on',
          opened('c-1'),
          asked('c-1', 'admission', revision=1, index=0, tool='delegate_to_agent', arguments={'agent': 'theo', 'request': 'a'}),
          asked('c-1', 'admission', revision=1, index=1, tool='investigate_with_oracle', arguments={'request': 'b'}),
          asked('c-1', 'admission', revision=1, index=2, tool='cancel_agent', arguments={'agent': 'theo', 'request': 'c'}),
          asked('c-1', 'admission', revision=1, index=3, tool='cancel_agent', arguments={'agent': 'theo'}),
          asked('c-1', 'admission', revision=1, index=4, tool='get_agent_status', arguments={'agent': 'theo'}),
          asked('c-1', 'admission', revision=2, index=0, tool='delegate_to_agent', arguments={'agent': 'theo', 'request': 'd'}),
          asked('c-1', 'admission', revision=2, index=1, tool='delegate_to_agent', arguments={'agent': 'theo', 'request': 'e'}),
          asked('c-1', 'admission', revision=3, index=0, tool='delegate_to_agent', arguments={'agent': 'theo', 'request': 'f'}),
          asked('c-1', 'finish', revision=3, index=0, result={'status': 'queued', 'operation_id': 'rtc-done'}),
          delegation(rtc(PHONE, call_id(FIRST, 1, 0)), status='accepted', text=''),
          delegation(rtc(PHONE, call_id(FIRST, 1, 1))),
          delegation(rtc(PHONE, call_id(FIRST, 1, 2), replacement=True), status='queued', text=''),
          delegation(rtc(PHONE, call_id(FIRST, 1, 2)), status='failed', text=''),
          delegation(rtc(PHONE, call_id(FIRST, 1, 3), replacement=True)),
          delegation(rtc(PHONE, call_id(FIRST, 1, 4))),
          delegation(rtc(PHONE, call_id(FIRST, 2, 1)), owner=OTHER), delegation('rtc-done'),
          passes(9), asked('c-1', 'reconcile'), asked('c-1', 'work'), asked('c-1', 'reconcile')),
    story('the voice is told of a thing once in a thread, and what a user added is listed in its order',
          opened('c-1'), asked('c-1', 'claim', kind='decision', id='dec-1', reference_ts=5),
          asked('c-1', 'claim', kind='decision', id='dec-1', reference_ts=9), passes(3),
          asked('c-1', 'claim', kind='completion', id='dec-1', reference_ts=7, stale=True),
          asked('c-1', 'claim', kind='', id='dec-2', reference_ts=7), asked('c-1', 'claim', kind='decision', id=' ', reference_ts=7),
          asked('c-1', 'claim', kind='decision', id=' dec-3 ', reference_ts=7),
          asked('c-1', 'claim', kind='decision', id='dec-4', reference_ts=True),
          asked('c-1', 'release', kind='decision', id='dec-1'), asked('c-1', 'claim', kind='decision', id='dec-1', reference_ts=11),
          opened('c-2'), asked('c-1', 'claim', kind='decision', id='dec-5', reference_ts=1),
          asked('c-1', 'release', kind='decision', id='dec-1'), asked('c-2', 'claim', kind='decision', id='dec-1', reference_ts=1),
          asked('c-2', 'context', id='ctx-b', text='The pump is a Grundfos.'), passes(1),
          asked('c-2', 'context', id='ctx-a', text='Station four is in Drammen.', captured_at=NOW),
          asked('c-2', 'context', id='ctx-a', text='Station four is in Drammen.', captured_at=NOW),
          asked('c-2', 'context', id='ctx-a', text='Another text under the same name.'),
          asked('c-2', 'context', id='ctx c', text='A name that is none.'), asked('c-2', 'context', id='ctx-d', text='   '),
          asked('c-2', 'context', id='ctx-e', text='x' * 16001), asked('c-2', 'context', id='ctx-f', text='y' * 16000),
          asked('c-2', 'context', id='ctx-g', text='In time to come.', captured_at=NOW + 400_000),
          asked('c-2', 'contexts'), asked('c-2', 'uncontext', id='ctx-b'), asked('c-2', 'uncontext', id='ctx-b'),
          asked('c-2', 'contexts')),
    story('when a call begins the voice is handed an excerpt of the thread: whole records within a bound',
          opened('c-1'), asked('c-1', 'history'), asked('c-1', 'history', roster=ROSTER),
          asked('c-1', 'save', state={'revision': 4, 'fragments': [
              fragment('user', 'What happened at station four?', NOW - 3 * 86_400_000),
              fragment('assistant', 'Let me ask Theo.', NOW - 3 * 86_400_000 + 4000),
              fragment('user', 'And the valve?', NOW - 7_200_000), fragment('assistant', 'Asked.', NOW - 119_000),
              fragment('user', 'Thanks.', NOW - 59_000), {'role': 'user', 'text': 'Kept long ago.'},
              fragment('user', 'With a time that is none.', 'yesterday'), fragment('user', 'In summer.', 1_782_000_000_000)]}),
          delegation('rtc-1', text='<speak>The pump ran dry.</speak> Details. <speak>I reset <b>it</b>.</speak>',
                     created_at=NOW - 9000, updated_at=NOW - 8000,
                     request_text='  What happened?  <oracle-reference-data>not shown</oracle-reference-data>'),
          delegation('rtc-2', status='accepted', text='', created_at=NOW - 5000, updated_at=NOW - 5000),
          delegation('rtc-3', status='failed', text='', error='The tool gave up.', created_at=NOW - 7000, updated_at=NOW - 7000),
          asked('c-1', 'link', operation_id='rtc-1'), asked('c-1', 'link', operation_id='rtc-2'),
          asked('c-1', 'link', operation_id='rtc-3'),
          asked('c-1', 'context', id='ctx-a', text='Station four is in Drammen.'),
          asked('c-1', 'history', roster=ROSTER), asked('c-1', 'history', roster=MANY, later=86_400_000 * 200)),
    story('an excerpt leaves whole records out rather than cut one, and the newest result is in it first',
          opened('c-1'),
          asked('c-1', 'save', state={'revision': 9, 'fragments': [
              fragment('user' if n % 2 else 'assistant', f'Fragment {n}: ' + 'words ' * (40 + 9 * n), NOW - 60_000 * (30 - n))
              for n in range(12)]}),
          delegation('rtc-1', text=LONG * 20, created_at=NOW - 9000, updated_at=NOW - 8000, request_text='r' * 300),
          delegation('rtc-2', text='A short and older answer.', created_at=NOW - 19000, updated_at=NOW - 18000),
          delegation('rtc-3', status='queued', text='', created_at=NOW - 1000, updated_at=NOW - 1000),
          delegation('rtc-4', text='w' * 3000, created_at=NOW - 29000, updated_at=NOW - 28000),
          asked('c-1', 'link', operation_id='rtc-1'), asked('c-1', 'link', operation_id='rtc-2'),
          asked('c-1', 'link', operation_id='rtc-3'), asked('c-1', 'link', operation_id='rtc-4'),
          asked('c-1', 'context', id='ctx-a', text='z' * 900), asked('c-1', 'context', id='ctx-b', text='A short one.'),
          asked('c-1', 'history', roster=ROSTER)),
    story('an excerpt of a thread whose only work is not over names that work',
          opened('c-1'), delegation('rtc-1', status='accepted', text='', created_at=NOW - 2000, updated_at=NOW - 2000),
          delegation('rtc-2', status='queued', text='', created_at=NOW - 1000, updated_at=NOW - 1000),
          asked('c-1', 'link', operation_id='rtc-1'), asked('c-1', 'link', operation_id='rtc-2'),
          asked('c-1', 'history', roster={'agents': [], 'oracle_contact': ''})),
    story("a thread is its owner's, and what changes it is done by the connection that has it",
          opened('c-1'), forged('f-1', FIRST, OTHER, 'c-1'), asked('f-1', 'load'),
          asked('f-1', 'save', state={'revision': 1}), asked('f-1', 'work'), asked('f-1', 'admissions'),
          begun('c-1', 1, 0, agent='theo', request='a'), opened('c-2'),
          asked('c-1', 'finish', revision=1, index=0, result={'status': 'accepted', 'operation_id': 'rtc-1'}),
          asked('c-1', 'work'), asked('c-1', 'admissions'), asked('c-1', 'load')),
    story('two threads of one owner keep their own actions, work and what their voices were told of',
          opened('c-1'), opened('c-2', contact='mike'), delegation('rtc-1', created_at=30),
          begun('c-1', 1, 0, agent='theo', request='a'), begun('c-2', 1, 0, tool='cancel_agent', agent='mike'),
          asked('c-1', 'link', operation_id='rtc-1'), asked('c-2', 'work'),
          asked('c-2', 'link', operation_id='rtc-1'), asked('c-2', 'work'), asked('c-1', 'work'),
          asked('c-1', 'admissions'), asked('c-2', 'admissions'),
          asked('c-1', 'claim', kind='decision', id='dec-1', reference_ts=5),
          asked('c-2', 'claim', kind='decision', id='dec-1', reference_ts=5),
          asked('c-1', 'release', kind='decision', id='dec-1'),
          asked('c-2', 'claim', kind='decision', id='dec-1', reference_ts=6),
          asked('c-1', 'release', kind='decision', id='  '),
          asked('c-1', 'claim', kind='k' * 41, id='x', reference_ts=1),
          asked('c-1', 'claim', kind='k' * 40, id='x', reference_ts=1)),
    story('the actions of a thread are told in the order of their places, whenever they were begun',
          opened('c-1'), begun('c-1', 3, 1, agent='theo', request='c'), begun('c-1', 3, 0, agent='theo', request='b'),
          begun('c-1', 2, 0, agent='theo', request='a'), asked('c-1', 'admissions'),
          asked('c-1', 'admissions', revision=3)),
    story('an action that was ended is not ended again by the work it is known to have handed on',
          opened('c-1'), begun('c-1', 1, 0, agent='theo', request='a'),
          asked('c-1', 'finish', revision=1, index=0, result={'error': 'The agent refused.'}),
          delegation(rtc(PHONE, call_id(FIRST, 1, 0))), begun('c-1', 1, 1, agent='theo', request='b'),
          asked('c-1', 'finish', revision=1, index=1, result={'status': 'accepted', 'operation_id': ''}),
          asked('c-1', 'reconcile'), asked('c-1', 'work'), asked('c-1', 'admissions')),
    story('room is kept for the newest work that is over and said something; other work is told by its answer',
          opened('c-1'),
          delegation('rtc-3', status='accepted', text='Half an answer.', created_at=NOW - 500, updated_at=NOW - 500),
          delegation('rtc-1', text='', created_at=NOW - 1000, updated_at=NOW - 1000),
          delegation('rtc-2', text='An older answer.', created_at=NOW - 5000, updated_at=NOW - 4000),
          delegation('rtc-4', text='The oldest answer.', error='And an error beside it.', created_at=NOW - 9000,
                     updated_at=NOW - 8000),
          asked('c-1', 'link', operation_id='rtc-1'), asked('c-1', 'link', operation_id='rtc-2'),
          asked('c-1', 'link', operation_id='rtc-3'), asked('c-1', 'link', operation_id='rtc-4'),
          asked('c-1', 'save', state={'revision': 2, 'fragments': [
              fragment('user', 'At a time no clock shows.', 10 ** 17), fragment('user', 'Just now.', NOW - 2000)]}),
          asked('c-1', 'history', roster=THIRTY)),
]
# What of a result is shown in an excerpt, by how much room it is given.
RESULTS = [
    ({'result_text': 'Plain and short.'}, 1400), ({'result_text': '', 'error': 'It gave up.'}, 1400),
    ({'result_text': '', 'error': ''}, 1400), ({'result_text': None, 'error': None, 'request_text': None}, 1400),
    ({'result_text': '<speak>One.</speak> not said <speak>Two <i>and</i> a half.</speak>'}, 1400),
    ({'result_text': '<speak>Open and never closed.'}, 1400), ({'result_text': 'a < b and c > d'}, 1400),
    ({'result_text': '<speak>line\none</speak><speak></speak>'}, 1400),
    ({'result_text': LONG * 20}, 1400), ({'result_text': LONG * 20}, 1800), ({'result_text': LONG * 20}, 300),
    ({'result_text': LONG * 20}, 200), ({'result_text': 'w' * 3000}, 1400), ({'result_text': 'w' * 3000}, 100),
    ({'result_text': 'One sentence. ' + 'w' * 3000}, 1400), ({'result_text': 'Is it? ' + 'w' * 3000}, 1400),
    ({'result_text': '. ' + 'w' * 3000}, 1400), ({'result_text': chr(0x1f600) * 2000}, 1400),
    ({'result_text': 'Short.', 'request_text': 'r' * 120}, 1400), ({'result_text': 'Short.', 'request_text': 'r' * 121}, 1400),
    ({'result_text': 'Short.', 'request_text': '  blanks around  <oracle-reference-data>x'}, 1400),
    ({'result_text': 'Short.', 'request_text': chr(0xe9) * 500}, 150),
]
AGES = [0, 1, 999, 59_999, 60_000, 119_999, 120_000, 3_599_999, 3_600_000, 7_200_000, 86_399_999, 86_400_000,
        172_800_000, 86_400_000 * 400, -5000, -86_400_000 * 2]
TIMES = [NOW, NOW + 1, NOW + 999, 0, 1, -1000, 1_782_000_000_000, 1_774_746_000_000, 1_774_749_600_000,
         1_792_890_000_000, 1_792_893_600_000, 253_402_300_799_000]


def run() -> dict:
    """Runs in a process of its own: what the Python Host's modules do."""
    import os
    import time
    os.environ['TZ'] = ZONE
    time.tzset()
    spec = importlib.util.spec_from_file_location('replay_host', REPO / 'tests' / 'replay' / 'host.py')
    host = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(host)
    root = pathlib.Path(tempfile.mkdtemp(prefix='clarp-oracle-memory-vectors-', dir='/var/tmp'))
    (root / 'scenario-config.toml').write_text('')
    host.isolate(root)
    host.loopback_only()
    sys.path.insert(0, str(REPO / 'server'))
    from lib import agents, db, oracle_attention, oracle_memory as memory, oracle_result_context

    def oracle_memory_result_room():
        """The room the newest result of an excerpt is shown in, as the module has it."""
        import inspect
        found = re.search(r'result_payload\(\w+, budget=(\d+)\)', inspect.getsource(memory.ThreadStore.startup_history))
        return int(found.group(1))

    clock = {'now': NOW, 'made': 0}
    db.now_ms = lambda: clock['now']
    memory.log_fault = lambda *said, **more: None

    class Ids:
        """Names of threads in the order they are made, where the Host draws them at random."""

        @staticmethod
        def uuid4():
            clock['made'] += 1

            class Drawn:
                hex = f'{clock["made"]:032x}'
            return Drawn()
    memory.uuid = Ids
    work = root / 'work'
    work.mkdir()
    agent = agents.create_agent(persona='Theo', voice_id='', cwd=str(work), session='theo', backend='claude')
    tables = {
        'threads': 'SELECT thread_id, owner_principal, contact, active_connection, revision, state_json, created_at, '
                   'updated_at FROM oracle_threads ORDER BY rowid',
        'admissions': 'SELECT * FROM oracle_admissions ORDER BY thread_id, revision, action_index',
        'work': 'SELECT * FROM oracle_thread_work ORDER BY thread_id, operation_id',
        'told': 'SELECT * FROM oracle_context_notifications ORDER BY thread_id, source_kind, source_id',
        'observed': 'SELECT thread_id, source_key, event_json, created_at FROM oracle_observations ORDER BY sequence',
        'contexts': 'SELECT thread_id, context_id, kind, text, mime_type, captured_at, added_at, removed_at, content_hash '
                    'FROM oracle_user_context ORDER BY thread_id, context_id',
    }

    def kept():
        return {name: [dict(row) for row in db.conn().execute(sql)] for name, sql in tables.items()}

    def played(one):
        clock.update(now=NOW, made=0)
        for name in ('oracle_admissions', 'oracle_thread_work', 'oracle_context_notifications', 'oracle_observations',
                     'oracle_user_context', 'oracle_threads', 'oracle_delegations'):
            db.conn().execute(f'DELETE FROM {name}')
        db.conn().execute("DELETE FROM sqlite_sequence WHERE name = 'oracle_observations'")
        stores, steps, before = {}, [], kept()
        for step in one['steps']:
            said = {key: value for key, value in step.items() if key not in ('do', 'connection')}
            if step['do'] == 'forge':
                said = {}
            store = stores.get(step.get('connection'))
            answered = {}
            try:
                what = step['do']
                if what == 'passes':
                    clock['now'] += step['ms']
                elif what == 'open':
                    made = memory.open_thread(step['owner'], step['contact'], thread_id=step.get('thread_id'),
                                              connection_id=step['connection'], fresh=step.get('fresh', False))
                    stores[step['connection']] = made
                    answered['returned'] = made.thread_id
                elif what == 'forge':
                    stores[step['connection']] = memory.ThreadStore(step['thread'], step['owner'], step['as'])
                elif what == 'close':
                    # A call that ended lets go of its thread: as the Host's own loop leaves it, nothing is written.
                    # (Here the thread is let go of so that the stories can tell which thread a next call takes.)
                    db.conn().execute("UPDATE oracle_threads SET active_connection='' WHERE thread_id=?", (store.thread_id,))
                elif what == 'delegation':
                    row = {'trace_id': 't-' + step['row']['delegation_id'], 'client_msg_id': 'm-' + step['row']['delegation_id'],
                           'agent_id': agent, 'backend_session_id': '', 'result_message_id': None,
                           'created_at': clock['now'], 'updated_at': clock['now'], 'delivered_at': None, **step['row']}
                    names = ', '.join(row)
                    db.conn().execute(f'INSERT INTO oracle_delegations ({names}) VALUES ({", ".join("?" for _ in row)})',
                                      tuple(row.values()))
                elif what == 'load':
                    answered['returned'] = store.load()
                elif what == 'save':
                    store.save(said['state'])
                elif what == 'observe':
                    answered['returned'] = store.observe(said['event'], said['key'])
                elif what == 'admission':
                    answered['returned'] = store.admission(said['revision'], said['index'], said['tool'], said['arguments'])
                elif what == 'finish':
                    store.finish_admission(said['revision'], said['index'], said['result'])
                elif what == 'admissions':
                    answered['returned'] = store.admissions(said.get('revision'))
                elif what == 'link':
                    store.link_work(said['operation_id'])
                elif what == 'work':
                    answered['returned'] = store.work()
                elif what == 'reconcile':
                    store.reconcile()
                elif what == 'claim':
                    answered['returned'] = oracle_attention.claim_context(
                        store, source_kind=said['kind'], source_id=said['id'], reference_ts=said['reference_ts'],
                        stale=said.get('stale', False))
                elif what == 'release':
                    oracle_attention.release_context(store, source_kind=said['kind'], source_id=said['id'])
                elif what == 'context':
                    answered['returned'] = store.add_context(said['id'], text=said['text'], captured_at=said.get('captured_at'))
                elif what == 'uncontext':
                    answered['returned'] = store.remove_context(said['id'])
                elif what == 'contexts':
                    answered['returned'] = store.contexts()
                elif what == 'history':
                    answered['returned'] = store.startup_history(roster=said.get('roster'),
                                                                 now_ms=clock['now'] + said.get('later', 0))
                else:
                    raise SystemExit(f'no such step: {what}')
            except ValueError as refused:
                answered['raises'] = str(refused)
            after = kept()
            # Which of the connections of the story has the thread it opened, now.
            has = {}
            for connection, one_store in sorted(stores.items()):
                try:
                    one_store._owned(active=True)
                    has[connection] = True
                except ValueError:
                    has[connection] = False
            # Only the tables a step changed are written down again.
            steps.append({**step, **answered, 'at': clock['now'], 'has': has,
                          'kept': {name: rows for name, rows in after.items() if rows != before[name]}})
            before = after
        return {'name': one['name'], 'steps': steps}

    def row_of(row):
        return {'delegation_id': 'rtc-1', 'session': 'theo', 'status': 'completed', 'request_text': 'Look into the pump.', **row}

    def largest(fits, low, high):
        """The largest number from `low` up for which `fits` holds: it holds for `low`, not for `high`, and for
        none after the first it does not hold for."""
        assert fits(low) and not fits(high)
        while high - low > 1:
            middle = (low + high) // 2
            low, high = (middle, high) if fits(middle) else (low, middle)
        return low

    # The least room in which a result of several sentences is shown whole, and a sign less.
    several = {'result_text': 'One sentence. ' * 40}
    whole = largest(lambda room: oracle_result_context.result_payload(row_of(several), budget=room)['finding_excerpt'],
                    100, 4000) + 1
    exact = [(several, whole), (several, whole - 1)]
    # The longest answer of one word that the newest result of an excerpt shows whole, as the excerpt shows it.
    room = oracle_memory_result_room()
    word = largest(lambda signs: not oracle_result_context.result_payload(
        row_of({'result_text': 'w' * signs}), budget=room)['finding_excerpt'], 10, 4000)

    def talk(signs):
        return {'revision': 1, 'fragments': [fragment('user', 'x' * signs, NOW - 5000)]}

    def excerpt_has(signs):
        """Whether an excerpt of a thread with one thing said, so many signs long, has it."""
        clock.update(now=NOW, made=0)
        for name in ('oracle_threads',):
            db.conn().execute(f'DELETE FROM {name}')
        store = memory.open_thread(PHONE, 'theo', connection_id='c-1')
        store.save(talk(signs))
        return 'x' * signs in store.startup_history()[0]['content'][0]['text']
    fitting = largest(excerpt_has, 10, 8000)
    found = [
        story('the newest result is shown whole where it is exactly as long as its room, and cut where a sign longer',
              opened('c-1'), delegation('rtc-1', text='w' * word, request_text='Look into the pump.',
                                        created_at=NOW - 9000, updated_at=NOW - 8000),
              asked('c-1', 'link', operation_id='rtc-1'), asked('c-1', 'history'),
              opened('c-2', contact='mike'),
              delegation('rtc-2', text='w' * (word + 1), request_text='Look into the pump.', created_at=NOW - 9000,
                         updated_at=NOW - 8000),
              asked('c-2', 'link', operation_id='rtc-2'), asked('c-2', 'history')),
        story('an excerpt has what was said where it is exactly as long as an excerpt may be, and not where a byte longer',
              opened('c-1'), asked('c-1', 'save', state=talk(fitting)), asked('c-1', 'history'),
              asked('c-1', 'save', state=talk(fitting + 1)), asked('c-1', 'history')),
    ]
    stories = [played(one) for one in STORIES + found]
    results = []
    for row, budget in RESULTS + exact:
        row = row_of(row)
        results.append({'row': row, 'budget': budget, 'payload': oracle_result_context.result_payload(row, budget=budget)})
    ages = [{'ms_before': before, 'age': memory.age_phrase(NOW - before, NOW)} for before in AGES]
    times = []
    for ms in TIMES:
        try:
            times.append({'ms': ms, 'time': memory.iso_time(ms)})
        except (OverflowError, OSError, ValueError) as refused:
            times.append({'ms': ms, 'raises': type(refused).__name__})
    said = {'what': 'server/lib/oracle_memory.py, oracle_attention.claim_context, oracle_result_context.result_payload',
            'now': NOW, 'zone': ZONE, 'phone': PHONE, 'results': results, 'ages': ages, 'times': times, 'stories': stories}
    # The agent all this work is handed to has a name that is drawn anew at every run: it is told as one name.
    return json.loads(json.dumps(said).replace(json.dumps(agent), json.dumps('a-theo')))


def shrunk(value):
    """What is long and says nothing here is written down short: one sign many times over as the sign and how
    many (`{"runs": [...]}`: texts as they are, and `[sign, count]` for a run of it). Whoever reads the file
    writes them out again."""
    if isinstance(value, str):
        parts = []
        for found in re.finditer(r'(.)\1{63,}|(?:(.)(?!\2{63}))+', value, flags=re.S):
            parts.append([found.group(1), len(found.group(0))] if found.group(1) else found.group(0))
        return {'runs': parts} if any(isinstance(part, list) for part in parts) else value
    if isinstance(value, list):
        return [shrunk(item) for item in value]
    if isinstance(value, dict):
        return {key: shrunk(item) for key, item in value.items()}
    return value


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == '--run':
        print(json.dumps(run()))
        return 0
    ran = subprocess.run([sys.executable, '-I', __file__, '--run'], capture_output=True, text=True,
                         env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}, check=False)
    if ran.returncode != 0:
        print(ran.stderr[-3000:], file=sys.stderr)
        return 1
    said = shrunk(json.loads(ran.stdout.splitlines()[-1]))
    out = ['{']
    parts = list(said)
    for at, part in enumerate(parts):
        end = ',' if at < len(parts) - 1 else ''
        if part not in ('stories', 'results'):
            out.append(f' {json.dumps(part)}: {json.dumps(said[part], ensure_ascii=True)}{end}')
            continue
        if part == 'results':
            out.append(' "results": [')
            out.extend('  ' + json.dumps(row, ensure_ascii=True) + (',' if n < len(said[part]) - 1 else '')
                       for n, row in enumerate(said[part]))
            out.append(' ]' + end)
            continue
        out.append(' "stories": [')
        for index, one in enumerate(said[part]):
            out.append('  {"name": ' + json.dumps(one['name'], ensure_ascii=True) + ', "steps": [')
            out.extend('   ' + json.dumps(row, ensure_ascii=True) + (',' if n < len(one['steps']) - 1 else '')
                       for n, row in enumerate(one['steps']))
            out.append('  ]}' + (',' if index < len(said[part]) - 1 else ''))
        out.append(' ]' + end)
    out.append('}')
    OUT.write_text('\n'.join(out) + '\n', encoding='ascii')
    steps = [step for one in said['stories'] for step in one['steps']]
    refused = sum(1 for step in steps if 'raises' in step)
    print(f'{OUT.relative_to(REPO)}: {len(said["stories"])} stories, {len(steps)} steps, {refused} refused, '
          f'{len(said["results"])} results, {OUT.stat().st_size} bytes')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
