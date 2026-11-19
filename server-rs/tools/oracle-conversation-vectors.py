#!/usr/bin/env python3
"""Writes the vectors the Oracle's conversation is tested against.

A call of the Oracle is one conversation: the phone on one side sends sound and a few words of its own, the
voice model on the other sends sound, what it heard and said, and a word whenever it wants something done. The
Host between them forwards the sound, keeps what was said, has a router turn what the user asked into work for
agents, and tells the voice what came of that work: between two turns of the talk and never into one, long
answers in parts, with a cue of sound for the phone.

This runs the Python Host's own conversation (`server/lib/oracle_live_stable.py`, `Conversation`, and
`client_event` for what a phone may send) with a voice, a phone, the tools of the fleet, the memory of a
thread, a router and two clocks made up here, through stories of what each side says and of time passing,
and writes down what the voice and the phone were told after each step, and what was kept of the call.

    <the Python Host's interpreter> server-rs/tools/oracle-conversation-vectors.py

It also writes what the router is asked with every time (`crates/clarp-kernel/src/oracle/router.json`: its model,
what it is told to do and the tools it may name) and what the voice is told as it is (`voice.json` beside it:
what it is to be, and the sentences of the Host's own), which the kernel reads as they are.

It runs in a process of its own whose every home, data, config and cache directory is a new empty directory
(the replay harness's own isolation), with no way out of the machine. No Host is touched or started and no
provider is asked anything. The same file at every run.
"""
from __future__ import annotations

import base64
import copy
import importlib.util
import io
import json
import pathlib
import re
import subprocess
import sys
import tempfile
import types

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[1]
OUT = HERE.parent / 'crates' / 'clarp-kernel' / 'tests' / 'fixtures' / 'oracle-conversation-vectors.json'
# What the router is asked with whatever it is asked: its model, what it is told to do, and the tools it may name.
ROUTER = HERE.parent / 'crates' / 'clarp-kernel' / 'src' / 'oracle' / 'router.json'
# What the voice is told as it is: its model, what it is to be, and the sentences of the Host's own.
VOICE = HERE.parent / 'crates' / 'clarp-kernel' / 'src' / 'oracle' / 'voice.json'
THREAD = 'oracle-thread-1'
OWNER = 'device-phone-1'
LONG = ('The pump at station four ran dry at 03:12 and tripped its breaker. I reset it, checked the float switch, '
        'which was stuck, and freed it. Open questions: who owns the spare switch? ') * 9


def loud(samples=240):
    """Sound that is heard: so many samples of one height, in base64 as it goes between the three."""
    return base64.b64encode(b''.join((1000).to_bytes(2, 'little', signed=True) for _ in range(samples))).decode('ascii')


def still(samples=240):
    """Sound that is silence."""
    return base64.b64encode(bytes(2 * samples)).decode('ascii')


def faint(height, samples=240):
    """Sound of one height: 100 is as loud as sound may be and still be nobody speaking."""
    return base64.b64encode(b''.join(height.to_bytes(2, 'little', signed=True) for _ in range(samples))).decode('ascii')


def phone(raw):
    """The phone sends a text over its socket: as it is written, or an event that is written for it."""
    return {'do': 'phone', 'raw': raw if isinstance(raw, str) else json.dumps(raw)}


def hears(audio=None, **more):
    return phone({'type': 'session.input_audio.append', 'audio': loud() if audio is None else audio, **more})


def voice(kind, **more):
    return {'do': 'voice', 'said': {'type': kind, **more}}


def speaks(delta=None):
    return voice('session.output_audio.delta', delta=loud() if delta is None else delta)


def heard(text, start_ms=0, end_ms=0, **more):
    """The voice says what it heard the user say."""
    return voice('session.input_transcript.delta', delta=text, start_ms=start_ms, end_ms=end_ms, **more)


def said(text, start_ms=0, end_ms=0, **more):
    """The voice says what it said itself."""
    return voice('session.output_transcript.delta', delta=text, start_ms=start_ms, end_ms=end_ms, **more)


def wait(seconds):
    return {'do': 'wait', 'seconds': seconds}


def tick(count=1):
    return {'do': 'tick', 'count': count}


def result(delegation_id, status='completed', text='Done.', error='', session='theo', request='Look into the pump.', **more):
    """A delegation of this call's is over: its row, as the book of delegations has it."""
    return {'do': 'result', 'row': {'delegation_id': delegation_id, 'session': session, 'status': status,
                                    'result_text': text, 'error': error, 'request_text': request, 'created_at': 1000,
                                    'agent_id': 'a-' + session, 'backend_session_id': 'tool-1',
                                    'result_message_id': 'r-' + delegation_id, **more}}


def lands(row):
    """A delegation's result lands in the book right after the tools' results are next read: between that read and
    what is done with it."""
    return {'do': 'lands', 'row': row['row']}


def finding(session, delegation_id):
    """What the contact's answer of `result(delegation_id, session=session)` is known by, as a Host keeps it."""
    import hashlib
    parts = ['a-' + session, 'tool-1', 'r-' + delegation_id]
    return hashlib.sha256(json.dumps(parts).encode()).hexdigest()


def owns(*ids):
    """Delegations that are this call's own, as its tools know them."""
    return {'do': 'owns', 'ids': list(ids)}


def call(method, /, **said):
    """Something of the Host's own is asked of the conversation: a cue, a handoff's hook, a result read again."""
    return {'do': 'call', 'method': method, 'said': said}


def route(ident, *router, tools=None, meanwhile=None):
    """The Host routes what the voice asked for under this id. `router`: what the router answers each time it is asked
    (`asked(...)`); `tools`: what the tools of the fleet answer, by their names (a text: they refuse in these words);
    `meanwhile`: what the voice says while a tool of that name is at work."""
    return {'do': 'route', 'ident': ident, 'router': list(router), 'tools': tools or {}, 'meanwhile': meanwhile or {}}


def asked(*output, during=(), fails=None, **body):
    """One answer of the router: its output; what the voice says while the router thinks; or that it fails.
    `body`: the whole of what it answers, where that is not an output."""
    return {'output': list(output), 'during': list(during), 'fails': fails, **({'body': body['body']} if 'body' in body else {})}


def wants(name, call_id='call-1', **arguments):
    return {'type': 'function_call', 'name': name, 'call_id': call_id, 'arguments': json.dumps(arguments)}


def words(text):
    return {'type': 'message', 'content': [{'type': 'output_text', 'text': text}, {'type': 'other', 'text': 'not words'}]}


ACCEPTED = {'status': 'accepted', 'operation_id': 'rtc-a', 'session': 'theo', 'note': 'Handed off, not finished.'}
QUEUED = {'status': 'queued', 'operation_id': 'rtc-q', 'session': 'mike', 'note': 'Handed off, not finished.'}
WANTED = voice('session.delegation.created', delegation={'id': 'del-1'})


def wanted(number):
    """The voice wants something done: under an id of its own each time."""
    return voice('session.delegation.created', delegation={'id': f'del-{number}'})


def odd(name, arguments, call_id='c'):
    """A thing the router wants done, written as no router should write it."""
    return {'type': 'function_call', **({'name': name} if name else {}), 'call_id': call_id, 'arguments': arguments}


def unnamed(name, arguments):
    """A thing the router wants done that has no id of its own."""
    return {'type': 'function_call', 'name': name, 'arguments': arguments}


def saved_at(active, **more):
    """What was kept of a call that was last under way so long before this one begins (in ms; less than nothing: after)."""
    return {'revision': 3, 'fragments': [], 'results_sent': [], 'call_active_ms': active, 'call_work': ['rtc-1'], **more}


def attention(decisions=(), completions=()):
    return {'do': 'attention', 'decisions': list(decisions), 'completions': list(completions)}


def story(name, *steps, **opened):
    return {'name': name, 'opened': opened, 'steps': list(steps)}


def decision(decision_id, agent='Theo', question='Shall I order the spare switch?', **more):
    return {'decision_id': decision_id, 'artifact_id': 'art-' + decision_id, 'agent': agent, 'session': agent.lower(),
            'kind': 'approval', 'title': 'A spare switch', 'question': question, 'context': '', 'response_type': 'approval',
            'options': [], 'allow_custom_text': False, 'recommended_option_id': '', 'blocks_progress': True,
            'priority': None, 'created_at': 5, 'updated_at': 7, 'deadline_at': None, 'expires_at': None, **more}


def completion(message_id, agent='Mike', stale=False, **more):
    return {'notification_id': 'n-' + message_id, 'source_message_id': message_id, 'cause_message_id': 'c-' + message_id,
            'agent_id': 'a-' + agent.lower(), 'agent': agent, 'session': agent.lower(), 'backend_session_id': 'tool-1',
            'origin': 'user', 'reference_ts': 1_800_000_000_000, 'stale': stale,
            'source_text': 'The valve is replaced.', **more}


TALK = [heard('What happened ', 0, 900), heard('at station four?', 950, 1800), said('Let me ask Theo.', 0, 800)]
# The voice speaks, falls silent, and the Host looks again.
SPOKEN = [speaks(), wait(2.0), tick()]
STORIES = [
    story('Gemini interrupts, reports usage, and takes the answer of its route',
          voice('session.started'), speaks(), phone({'type':'oracle_v2.interrupt'}),
          voice('session.output_audio.interrupted'), voice('session.usage.updated', usage={'seconds': 3, 'voice_estimate_usd': .1}),
          heard('Ask Theo.'), wanted(1), wait(1.02), route('del-1', asked(words('Routed.'))),
          call('handoff_hold'), wanted(2), route('del-2'), voice('session.closed'), provider='gemini'),
    story('what a phone may send, and what is none of it',
          phone('not json'), phone('[1]'), phone('"text"'), phone({'type': 'session.start'}), phone({'type': 7}),
          phone({}), hears(''), hears(7), hears('!!!!'), hears('AAA='), hears('AA=='), hears(still(1)[:2] + '=='),
          hears(base64.b64encode(b'abc').decode()), hears(still(2), more='ignored'),
          phone({'type': 'session.close'}), phone({'type': 'session.close', 'handoff_id': None}),
          phone({'type': 'session.close', 'handoff_id': 7}), phone({'type': 'session.close', 'handoff_id': ''}),
          phone({'type': 'session.close', 'handoff_id': 'not an id'}),
          phone({'type': 'session.close', 'handoff_id': 'hof_' + 'a' * 32}),
          phone({'type': 'session.close', 'handoff_id': 'hof_' + '0123456789abcdef' * 2, 'more': 1}),
          phone({'type': 'session.close', 'handoff_id': 'hof_' + 'A' * 32}),
          phone({'type': 'session.close', 'handoff_id': 'hof_' + 'a' * 31}),
          phone({'type': 'session.close', 'handoff_id': 'hof_' + 'a' * 33}),
          phone({'type': 'session.close', 'handoff_id': 'hof_' + 'a' * 32 + chr(10)}),
          phone({'type': 'session.close', 'handoff_id': 'xhof_' + 'a' * 32}),
          phone({'type': 'session.close', 'handoff_id': 'a' * 32}),
          phone({'type': 'oracle_v2.interrupt', 'more': 1}), phone({'type': 'oracle_v2.preferences'}),
          phone({'type': 'oracle_v2.preferences', 'progress_interval_seconds': 5}),
          phone({'type': 'oracle_v2.preferences', 'progress_interval_seconds': 10}),
          phone({'type': 'oracle_v2.preferences', 'progress_interval_seconds': 121}),
          phone({'type': 'oracle_v2.preferences', 'progress_interval_seconds': 0}),
          phone({'type': 'oracle_v2.preferences', 'progress_interval_seconds': True}),
          phone({'type': 'oracle_v2.preferences', 'progress_interval_seconds': 30.0}),
          phone({'type': 'oracle_v2.preferences', 'narration': 'quiet'}),
          phone({'type': 'oracle_v2.preferences', 'narration': 'off', 'earcons': 'no'}),
          phone({'type': 'oracle_v2.preferences', 'earcons': 0}),
          phone({'type': 'oracle_v2.preferences', 'other': True})),
    story('sound goes both ways, and silence only for a moment after what was heard',
          voice('session.started', session={'model': 'gpt-live-1'}),
          hears(), hears(still()), speaks(), wait(0.5), speaks(still()), wait(0.5), speaks(still()), wait(0.45),
          speaks(still()), tick(), speaks(), wait(0.2), tick(), wait(1.2), tick(), wait(0.2), tick(), tick(),
          speaks(still()), wait(1.25), speaks(still()), speaks(), wait(1.15), speaks(still()), wait(0.1), speaks(still()),
          voice('session.output_audio.interrupted'), speaks(still()), tick(),
          voice('error', error={'message': 'the voice says something is wrong'}),
          voice('session.something_else', x=1), wait(3.0), tick(),
          # Sound that is nearly none is nobody speaking; a little more is.
          speaks(faint(100)), hears(faint(100)), tick(), speaks(faint(-101)), wait(0.3), speaks(faint(100)), wait(2.0), tick(),
          hears(faint(101)), owns('rtc-1'), result('rtc-1', text='Told once the user has been quiet.'),
          wait(4.4), tick(), wait(0.2), tick(), wait(4.0), tick(),
          # A few loud samples in much silence are somebody speaking: by the whole of the piece.
          owns('rtc-2'), result('rtc-2', text='Told once the user has been quiet again.'),
          hears(base64.b64encode(bytes([232, 3] * 3 + [0, 0] * 237)).decode('ascii')), wait(4.4), tick(), wait(0.2), tick(),
          voice('session.closed')),
    story('what was said is kept, a fragment at a time',
          voice('session.started'),
          heard('What happened ', 0, 900), heard('at station four?', 950, 1800), heard('And the valve?', 2950, 3600),
          heard('   ', 3700, 3800), said('Let me ', 0, 500), said('ask Theo.', 600, 900), heard('Yes please.', 5000, 5600),
          said('', 0, 0),
          heard('with an id', 7000, 7100, event_id='ev-1'), heard('with an id', 7000, 7100, event_id='ev-1'),
          heard('other words under the same id', 7200, 7300, event_id='ev-1'),
          heard('twice the same', 9000, 9100), heard('twice the same', 9000, 9100),
          {'do': 'voice', 'said': {'end_ms': 9100, 'start_ms': 9000, 'delta': 'twice the same', 'type': 'session.input_transcript.delta'}},
          heard('twice the same', 9000, 9101), said('twice the same', 9000, 9100),
          heard('with a number for its id', 9200, 9300, event_id=7), heard('with the same number', 9400, 9500, event_id=7),
          heard('no times'), said('and many fragments 1'), heard('2'), said('3'), heard('4'), said('5'), heard('6'), said('7'),
          heard('8'), said('9'), heard('10'), said('11'), heard('12'), said('13'), heard('14'), said('15'), heard('16'),
          said('17'), heard('18'), said('19'), heard('20'), said('21'), heard('22'), said('23'), heard('24'), said('25'),
          heard('26'), said('27'), heard('28')),
    story('a fragment grows no longer than what is kept of one',
          voice('session.started'), heard('x' * 31990, 0, 100), heard('y' * 10, 150, 200), heard('z', 250, 300),
          said('a' * 32000, 0, 100), said('b', 150, 200),
          heard(chr(0xe9) * 20000, 5000, 5100), heard(chr(0xe9) * 12000, 5150, 5200), heard(chr(0xe9), 5250, 5300)),
    story('what a phone prefers',
          voice('session.started'),
          phone({'type': 'oracle_v2.preferences', 'progress_interval_seconds': 30}),
          phone({'type': 'oracle_v2.preferences', 'narration': 'off'}),
          phone({'type': 'oracle_v2.preferences', 'narration': 'off'}),
          phone({'type': 'oracle_v2.preferences', 'narration': 'on', 'earcons': False}),
          phone({'type': 'oracle_v2.preferences', 'earcons': False}),
          phone({'type': 'oracle_v2.preferences', 'earcons': True, 'progress_interval_seconds': 0}),
          phone({'type': 'oracle_v2.interrupt'}),
          call('handoff_hold'), phone({'type': 'oracle_v2.preferences', 'narration': 'off'}),
          phone({'type': 'oracle_v2.interrupt'}), call('handoff_resume'),
          phone({'type': 'session.close'})),
    story('a result is told between two turns of the talk, with a cue before it',
          voice('session.started'), *TALK, speaks(), owns('rtc-1'),
          result('rtc-1', text='The pump ran dry and I reset it.', request='What happened at station four?'),
          tick(), wait(1.0), tick(), wait(2.0), tick(), wait(2.0), tick(), tick(), wait(5.0), tick(),
          result('rtc-1', text='The same again.'), tick(),
          owns('rtc-2', 'rtc-3', 'rtc-4'), result('rtc-2', status='cancelled', text=''),
          result('rtc-3', status='failed', text='', error='The tool gave up.'),
          wait(5.0), tick(), wait(1.0), tick(), wait(5.0), tick(),
          result('rtc-4', text='Late and short.'), hears(), wait(4.0), tick(), wait(1.0), tick(),
          speaks(), wait(0.5), tick(), wait(0.5), tick(), wait(4.0), tick()),
    story('a long answer is told in parts, each after the one before was spoken',
          voice('session.started'), *TALK, speaks(), owns('rtc-1'), wait(5.0),
          result('rtc-1', text=LONG * 9, request='What happened at station four?'),
          tick(), tick(), wait(1.0), tick(), speaks(), tick(), wait(1.0), tick(), wait(1.0), tick(), tick(),
          *SPOKEN, *SPOKEN, heard('Stop, I have a question.', 9000, 9900), *SPOKEN, wait(5.0), tick(),
          call('read_result'), *SPOKEN * 9, call('read_result'), *SPOKEN, call('read_result'),
          call('read_result', operation_id='rtc-1', from_part=2, verbatim=True), *SPOKEN,
          phone({'type': 'oracle_v2.interrupt'}), *SPOKEN, call('read_result', operation_id='rtc-1', from_part=99),
          call('read_result', operation_id='rtc-9'), call('read_result', from_part=0),
          call('read_result', from_part=True), call('read_result', from_part=3), voice('session.output_audio.interrupted'),
          *SPOKEN),
    story('a part nobody spoke stops holding back what came after it',
          voice('session.started'), *TALK, speaks(), owns('rtc-1', 'rtc-2'), wait(5.0),
          result('rtc-1', text=LONG), tick(), result('rtc-2', text='A second, short answer.'),
          wait(5.0), tick(), wait(9.0), tick(), wait(2.0), tick(), tick()),
    story('nothing is read where there is nothing, and the newest answer where none is named',
          voice('session.started'), call('read_result'), owns('rtc-1', 'rtc-2'),
          result('rtc-1', text='The newer answer.', created_at=20), result('rtc-2', text='The older answer.', created_at=10),
          call('read_result'), call('read_result', verbatim=False),
          result('rtc-3', status='cancelled', text='Never read.', created_at=30), owns('rtc-3'), call('read_result'),
          result('rtc-4', status='failed', text='', error='', created_at=40), owns('rtc-4'), call('read_result'),
          result('rtc-5', status='failed', text='', error='It gave up.', created_at=40), owns('rtc-5'), call('read_result', verbatim=1)),
    story('the newest answer is the newest that says something, and that was not called off',
          voice('session.started'), owns('rtc-1', 'rtc-2', 'rtc-3', 'rtc-4', 'rtc-5', 'rtc-6', 'rtc-7'),
          result('rtc-1', text='The newer answer.', created_at=20), result('rtc-2', text='The older answer.', created_at=10),
          result('rtc-3', status='cancelled', text='Never read.', created_at=60),
          result('rtc-4', status='failed', text='', error='', created_at=50),
          result('rtc-5', status='failed', text='', error='It gave up.', created_at=40),
          result('rtc-6', status='accepted', text='', created_at=70),
          # Work that is not over is no answer, whatever it is written to have said.
          result('rtc-7', status='accepted', text='Half an answer.', created_at=80), call('read_result', verbatim='yes')),
    story('cues wait until the voice is quiet, and are none where a phone wants none',
          voice('session.started'), call('cue', name='handed_off'), speaks(), call('cue', name='result'),
          call('cue', name='connected', persona='Theo'), tick(), wait(0.5), tick(), wait(0.2), tick(), wait(2.0), tick(),
          call('cue', name='switch_failed'), phone({'type': 'oracle_v2.preferences', 'earcons': False}),
          call('cue', name='back_to_oracle'), tick(), speaks(), call('handoff_offer', persona='Mike'),
          phone({'type': 'oracle_v2.preferences', 'earcons': True}), call('cue', name='handed_off'),
          phone({'type': 'oracle_v2.preferences', 'earcons': False}), wait(1.0), tick()),
    story('a call that is handed to an agent holds the Oracle, and comes back',
          voice('session.started'), *TALK, speaks(), wait(5.0),
          call('handoff_offer', persona='Theo'),
          call('handoff_mirror', record={'state': 'preparing', 'handoff_id': 'hof_' + 'a' * 32, 'agent': 'Theo'}),
          call('handoff_hold'), owns('rtc-1'), result('rtc-1', text='Held back while the call is away.'),
          attention(decisions=[decision('dec-1')]), wait(6.0), tick(), speaks(), wait(2.0), tick(),
          voice('session.delegation.created', delegation={'id': 'del-held'}), route('del-held'),
          voice('session.delegation.created', delegation={'id': 'del-held'}),
          call('handoff_failed', persona='Theo', reason='agent_busy', note=True),
          call('handoff_failed', persona='Theo', reason='agent_busy', note=False),
          call('handoff_resume'), call('handoff_returned', persona='Theo'), wait(6.0), tick(), wait(6.0), tick(),
          call('host_event', fact={'host_event': 'something', 'agent': 'Th' + chr(0xe9) + 'o'})),
    story('what waits for the user is told to the voice, one thing at a time, and not before the talk began',
          voice('session.started'), attention(decisions=[decision('dec-1'), decision('dec-2', agent='Mike')],
                                              completions=[completion('m-1'), completion('m-2', stale=True)]),
          wait(6.0), tick(), heard('Hello Oracle.', 0, 500), wait(6.0), tick(), speaks(), said('Hello.', 0, 300),
          tick(), wait(0.5), tick(), wait(1.2), tick(), wait(3.0), tick(), tick(), wait(0.5), tick(), wait(0.6), tick(),
          wait(3.0), tick(), wait(5.0), tick(), wait(5.0), tick(), wait(5.0), tick(),
          attention(decisions=[decision('dec-1'), decision('dec-3')]), wait(5.0), tick(), hears(), wait(3.0), tick(),
          wait(2.0), tick()),
    story('what waits for the user is written for the voice within its bounds',
          attention(decisions=[decision('dec-plain'),
                               decision('dec-options', options=[{'id': 'a', 'label': 'Order it', 'description': 'Two days.'},
                                                                {'id': 'b', 'label': 'Wait'}],
                                        allow_custom_text=1, recommended_option_id='a', priority=2.5, deadline_at=1_800_000_100_000,
                                        context='The switch at station four is stuck.', blocks_progress=0),
                               decision('dec-long', question='Shall I order it? ' * 60),
                               decision('dec-longer', question='q' * 2000, title='t' * 300),
                               decision('dec-wide', question=chr(0x1f600) * 300, title=''),
                               decision('dec-' + 'x' * 1300, question='Short?'),
                               decision('dec-huge', question=chr(0x1f600) * 300, artifact_id='a' * 900, agent=chr(0xe5) * 200),
                               {'decision_id': 'dec-bare'},
                               # The least record with a long title; a question of exactly as many signs as are shown
                               # of one, and a sign more; and blanks around what is shown.
                               decision('dec-least', title='t' * 90, artifact_id='a' * 1000, question='A question?'),
                               decision('dec-exact', question='q' * 240, context='c' * 900),
                               decision('dec-over', question='q' * 241, context='c' * 900),
                               decision('dec-blanks', question='  Shall I order it?  ', title='  A title  ', context='c' * 900),
                               decision('dec-none', title=None, question=None, kind=None, options=None, updated_at=None)],
                    completions=[completion('m-plain'), completion('m-stale', stale=True),
                                 completion('m-long', source_text='The valve is replaced. ' * 80),
                                 completion('m-wide', source_text='  ' + chr(0x1f600) * 500 + '  '),
                                 completion('m-huge-' + 'y' * 1300, source_text='z' * 3000),
                                 {'source_message_id': 'm-bare', 'reference_ts': 5},
                                 completion('m-blank', source_text='   ' + 'word ' * 400),
                                 {**completion('m-none', source_text=None, stale=None), 'agent': None}])),
    story('a tool said something long, and something short',
          voice('session.started'),
          call('deliver_tool_output', name='get_agent_status', output={'agent': 'Theo', 'state': 'working'}),
          call('deliver_tool_output', name='read_agent_transcript',
               output={'agent': 'Theo', 'session': 'theo', 'newest_message_age': '2 minutes ago', 'truncated': False,
                       'messages': [{'id': 'm-1', 'role': 'user', 'timestamp': 't1', 'text': 'What happened?'},
                                    {'id': 'm-2', 'role': 'assistant', 'timestamp': 't2', 'text': LONG, 'truncated': True}]}),
          speaks(), wait(2.0), tick(),
          call('read_result', operation_id='transcript:Theo', from_part=1),
          call('deliver_tool_output', name='read_agent_messages', output={'agent': 'Theo', 'messages': 'not a list'}),
          # A message that was cut says so where it is read out.
          call('deliver_tool_output', name='read_agent_transcript',
               output={'agent': 'Theo', 'session': 'theo', 'newest_message_age': 'just now', 'truncated': False,
                       'messages': [{'id': 'm-1', 'role': 'assistant', 'timestamp': 't1', 'text': 'A message that was cut',
                                     'truncated': True}]}),
          call('deliver_tool_output', name='read_agent_messages',
               output={'agent': 'Mike', 'newest_message_age': '', 'truncated': True,
                       'messages': [{'id': 'm-1', 'role': 'assistant', 'timestamp': 't1', 'text': 'Done.'},
                                    {'id': 'm-2', 'role': 'user', 'text': 'Thanks.', 'truncated': 0}]}),
          call('deliver_tool_output', name='get_agent_status',
               output={'agent': 'Th' + chr(0xe9) + 'o', 'messages': [{'role': 'assistant', 'text': 'Not a conversation.'}]}),
          call('deliver_tool_output', name='get_agent_status', output={'agent': 'Th' + chr(0xe9) + 'o', 'text': chr(0xe9) * 1500}),
          call('deliver_tool_output', name='list_agents', output={'agents': [{'name': 'n' * 200, 'session': 's%d' % n} for n in range(9)]}),
          call('append', kind='commentary', content='c' * 1500), call('append', kind='thinking', content='t' * 1501)),
    story('what the user asked is routed once they have stopped talking, and the work is handed on',
          voice('session.started'), heard('Ask Theo to check ', 0, 900), heard('the pump at station four.', 950, 1900), WANTED,
          WANTED, wait(0.02),
          route('del-1', asked(wants('delegate_to_agent', agent='theo', request='Check the pump at station four.')),
                tools={'delegate_to_agent': ACCEPTED}),
          voice('session.delegation.created', delegation={'id': 'del-2'}), wait(1.52), route('del-2'),
          said('Theo is on it.', 0, 600), speaks(), heard('And ask Mike about the valve.', 4000, 4900),
          voice('session.delegation.created', delegation={'id': 'del-3'}), wait(0.52),
          route('del-3', asked(wants('delegate_to_agent', 'call-9', agent='mike', request='What about the valve?'),
                               words('Mike is busy; it waits for him.')),
                tools={'delegate_to_agent': QUEUED}),
          voice('session.delegation.created', delegation={}), voice('session.delegation.created')),
    story('the router answers with several things, and with the same thing twice',
          voice('session.started'), heard('Stop Theo, tell me what Mike is doing, and read that again.', 0, 2000), WANTED,
          wait(1.02),
          route('del-1', asked(wants('cancel_agent', 'c-1', agent='theo'), wants('cancel_agent', 'c-2', agent='theo'),
                               wants('get_agent_status', 'c-3', agent='mike'), wants('read_result', 'c-4'),
                               wants('read_agent_transcript', 'c-5', agent='mike', limit=2),
                               wants('delegate_to_agent', 'c-6', agent='nobody', request='x'),
                               odd('read_agent_transcript', '{"limit": 2, "agent": "mike"}', 'c-7'),
                               odd('get_agent_status', '{"detail": true, "agent": "mike"}', 'c-8'),
                               {'type': 'reasoning', 'summary': []}, words(''), words('Two things are done.')),
                tools={'cancel_agent': {'cancelled': True, 'note': 'Confirm the cancellation in one sentence.'},
                       'get_agent_status': {'agent': 'Mike', 'session': 'mike', 'state': 'working', 'since': '2 minutes ago',
                                            'current_step': 'Reading the valve log', 'last_said': '', 'last_said_age': '',
                                            'note': 'Say what they are doing in one sentence; mention the age if it is more than an hour.'},
                       'read_agent_transcript': {'agent': 'Mike', 'session': 'mike', 'newest_message_age': 'just now',
                                                 'truncated': False, 'note': 'Read without prompting the agent.',
                                                 'messages': [{'id': 'm-1', 'role': 'assistant', 'timestamp': 't1',
                                                               'text': 'The valve log is clean.'}]},
                       'delegate_to_agent': {'error': 'Unknown or ambiguous agent; use a session from list_agents'}})),
    story('the user goes on talking while the router thinks, and is asked about again',
          voice('session.started'), heard('Ask Theo to check the pump.', 0, 1000), WANTED, wait(1.02),
          route('del-1', asked(wants('delegate_to_agent', agent='theo', request='Check the pump.'),
                               during=[heard('No, the valve, not the pump.', 2000, 2900)]),
                asked(wants('delegate_to_agent', agent='theo', request='Check the valve.')),
                tools={'delegate_to_agent': ACCEPTED}),
          heard('One.', 5000, 5200), voice('session.delegation.created', delegation={'id': 'del-2'}), wait(1.02),
          route('del-2', asked(words('first'), during=[heard('Two.', 6000, 6200)]),
                asked(words('second'), during=[heard('Three.', 7000, 7200)]),
                asked(words('third'), during=[heard('Four.', 8000, 8200)]), asked(words('never asked'))),
          voice('session.delegation.created', delegation={'id': 'del-3'}), wait(1.02),
          route('del-3', asked(words('The last words are routed now.')))),
    story('a router that fails, and tools that refuse',
          voice('session.started'), heard('Ask Theo to check the pump.', 0, 1000), WANTED, wait(1.02),
          route('del-1', asked(fails='the router is not there')),
          voice('session.delegation.created', delegation={'id': 'del-2'}), wait(0.02),
          route('del-2', asked({'type': 'function_call', 'name': 'delegate_to_agent', 'call_id': 'c', 'arguments': 'not json'})),
          voice('session.delegation.created', delegation={'id': 'del-3'}), wait(0.02),
          route('del-3', asked(wants('delegate_to_agent', agent='nobody', request='Check the pump.')),
                tools={'delegate_to_agent': 'Unknown or ambiguous agent; use a session from list_agents'}),
          # What was begun for these words is not begun again: the router is not asked.
          voice('session.delegation.created', delegation={'id': 'del-4'}), wait(0.02),
          route('del-4', asked(words('Never asked.'))),
          owns('rtc-1'), result('rtc-1', text='Held back while something is routed.'),
          heard('Then ask Mike.', 3000, 3600),
          voice('session.delegation.created', delegation={'id': 'del-5'}), wait(6.0), tick(),
          route('del-5', asked(wants('delegate_to_agent', agent='mike', request='Check the pump.'),
                               wants('cancel_agent', 'c-2', agent='theo'), wants('get_agent_status', 'c-3', agent='theo')),
                tools={'delegate_to_agent': {'error': 'Mike is not to be reached', 'status': 'refused'},
                       'cancel_agent': {'cancelled': True, 'session': 'theo'},
                       'get_agent_status': {'status': 'accepted', 'operation_id': 'rtc-odd'}}),
          tick()),
    story('a call in which everything goes to the one contact',
          voice('session.started'), heard('Check the pump at station four.', 0, 1500), WANTED, wait(1.02),
          route('del-1', tools={'investigate_with_oracle': ACCEPTED}),
          voice('session.delegation.created', delegation={'id': 'del-2'}), wait(0.02), route('del-2'),
          phone({'type': 'oracle_v2.preferences', 'narration': 'off'}), heard('And the valve.', 3000, 3600),
          voice('session.delegation.created', delegation={'id': 'del-3'}), wait(1.02),
          route('del-3', tools={'investigate_with_oracle': QUEUED}),
          owns('rtc-z', 'rtc-n1', 'rtc-n2'),
          result('rtc-a', text='The pump is fine.', request='x\nCurrent user message, verbatim:\nCheck the pump.'),
          result('rtc-q', text='The valve too.', agent_id='a-theo', result_message_id='r-rtc-a', backend_session_id='tool-1',
                 session='another'),
          said('Fine.', 0, 100), speaks(), wait(6.0), tick(), wait(6.0), tick(),
          # An answer that was told already is not told again, and what follows waits its while all the same.
          result('rtc-z', text='And the tank.', result_message_id='r-rtc-z'), wait(1.1), tick(), wait(3.5), tick(),
          # An answer that is not known by a message is told for every delegation it ended.
          result('rtc-n1', text='No message.', result_message_id=None), result('rtc-n2', text='No message.', result_message_id=None),
          wait(6.0), tick(), wait(6.0), tick(), wait(6.0), tick(),
          strategy='direct_contact'),
    story('a call that goes on where it stopped', voice('session.started'), tick(),
          # What was said before this connection is not routed again.
          WANTED, wait(1.02), route('del-1', asked(words('Never asked.'))),
          heard('And now something new.', 0, 900), wanted(2), wait(1.02),
          route('del-2', asked(wants('read_result', 'c-1', operation_id='rtc-1'), words('Read again.'))),
          saved={'revision': 7, 'routed_revision': 5, 'fragments': [{'role': 'user', 'text': 'Earlier words.', 'revision': 7,
                                                                    'provider_session': 'conn-0', 'end_ms': 10, 'at': 5}],
                 'results_sent': ['rtc-old'], 'forwarded_findings': {'k': 'rtc-old'}, 'relay_cursors': {'rtc-1': 1},
                 'narration': 'off', 'earcons': False, 'call_active_ms': -20_000, 'call_work': ['rtc-1']},
          work=['rtc-1', 'rtc-old', 'rtc-other']),
    story('a call that begins long after the one before', voice('session.started'), heard('New words.', 0, 400),
          said('Yes?', 0, 200), speaks(), owns(), result('rtc-1', text=LONG), result('rtc-new', text='Of this call.', created_at=2000),
          wait(6.0), tick(), wait(6.0), tick(), call('read_result', operation_id='rtc-1'),
          WANTED, wait(1.02), route('del-1', asked(wants('read_result', 'c-1', operation_id='rtc-1', from_part=1, verbatim=True))),
          saved={'revision': 3, 'fragments': [], 'results_sent': [], 'call_active_ms': -31_000, 'call_work': ['rtc-1'],
                 'relay_cursors': {'rtc-1': 1, 'rtc-gone': 'x'}},
          work=['rtc-1'], owned_later=['rtc-new']),
    story('what the user added to a thread is shown to the phone when the call begins',
          voice('session.started'), voice('session.started'),
          contexts=[{'thread_id': THREAD, 'context_id': 'ctx-1', 'kind': 'text', 'text': 'The pump is a Grundfos.',
                     'mime_type': None, 'captured_at': None, 'added_at': 5, 'removed_at': None, 'content_hash': 'h1'}],
          saved={'revision': 4, 'fragments': [{'role': 'user' if n % 2 else 'assistant', 'text': f'Kept {n}.', 'revision': n // 9,
                                               'provider_session': 'conn-0', 'end_ms': n, 'at': n} for n in range(35)]}),
    story('where fifty answers were read to is kept, and of an older one no more',
          voice('session.started'), heard('Hello.', 0, 300), wait(0.2), speaks(), said('Hello.', 0, 200),
          owns('rtc-new', 'rtc-short', 'rtc-neg', 'rtc-far'),
          result('rtc-new', text='A new answer.'), result('rtc-short', text='Short.', created_at=5),
          result('rtc-neg', text=LONG * 3, created_at=6), result('rtc-far', text=LONG * 3, created_at=7),
          call('read_result', operation_id='rtc-short'), call('read_result', operation_id='rtc-neg'),
          call('read_result', operation_id='rtc-far', from_part=1), call('read_result', operation_id='rtc-far'),
          call('read_result', operation_id='rtc-new'), wait(6.0), tick(),
          # And a router is shown no answer as read further than it goes.
          heard('And now?', 20000, 20400), WANTED, wait(1.02), route('del-1', asked(words('Nothing.'))),
          saved={'revision': 1, 'results_sent': ['rtc-short', 'rtc-neg', 'rtc-far'],
                 'relay_cursors': {**{f'old-{n:02}': 1 for n in range(47)}, 'rtc-short': 9, 'rtc-neg': -3, 'rtc-far': 3}}),
    story('a call goes on within half a minute of the one before, to the millisecond',
          voice('session.started'), saved=saved_at(-30_000), work=['rtc-1']),
    story('and is a new call a millisecond later', voice('session.started'), saved=saved_at(-30_001), work=['rtc-1']),
    story('and where the one before is said to be still to come', voice('session.started'), saved=saved_at(1), work=['rtc-1']),
    story('and where it is not said when the one before was', voice('session.started'),
          saved=saved_at('yesterday', narration='on', earcons=None, routed_revision=1), work=['rtc-1']),
    story('and where the one before was this very moment', voice('session.started'), saved=saved_at(0), work=['rtc-1', 'rtc-2']),
    story('words kept before fragments had numbers are new words until something was routed',
          voice('session.started'), heard('More words.', 150, 300), WANTED, wait(1.02),
          route('del-1', asked(words('Routed.'))), heard('And these.', 400, 600), wanted(2), wait(1.02),
          route('del-2', asked(words('Routed again.'))),
          saved={'revision': 2, 'routed_revision': 0, 'results_sent': [],
                 'fragments': [{'role': 'assistant', 'text': 'Kept long ago.', 'provider_session': 'conn-0', 'end_ms': 50},
                               {'role': 'user', 'text': 'Old words. ', 'provider_session': 'conn-0', 'end_ms': 100}]}),
    story('words that follow closely are one fragment, and after a pause another',
          voice('session.started'), heard('One', 0, 1000), heard(' two', 2099, 2500), heard(' three', 3600, 3900),
          heard(' four', 3900), heard(' five', 500, 600), said('Six', 0, 100), said(' seven', 1199, 1300),
          said(' eight', 2400, 2500), heard(chr(0x1f600) * 2, 2600, 2700)),
    story('the router is shown the work of the call: the newest first, and how far each answer was read',
          voice('session.started'), *TALK, speaks(), owns('rtc-1', 'rtc-2', 'rtc-3', 'rtc-4'),
          result('rtc-1', text=LONG * 3, request='What happened at station four?', created_at=1000),
          result('rtc-2', status='failed', text='', error='The tool gave up.', created_at=3000, request='r' * 2500),
          result('rtc-3', status='accepted', text='', created_at=2000),
          result('rtc-4', text='z' * 6001, created_at=4000, session='mike'),
          wait(6.0), tick(), heard('And what of the valve?', 9000, 9900), WANTED, wait(1.02),
          route('del-1', asked(words('Nothing to do.'))), *SPOKEN, *SPOKEN,
          heard('Read me the one of Mike.', 20000, 20900), wanted(2), wait(1.02),
          route('del-2', asked(wants('read_result', 'c-1', operation_id='rtc-4'), wants('read_result', 'c-2', operation_id='rtc-3'),
                               wants('read_result', 'c-3', operation_id='rtc-nobodys'))),
          heard('Thanks.', 30000, 30400), wanted(3), wait(1.02), route('del-3', asked(words('Nothing more.')))),
    story('of much work the router is shown the newest twenty',
          voice('session.started'), owns(*[f'rtc-{n:02}' for n in range(23)]),
          *[result(f'rtc-{n:02}', text=f'Answer {n}.', created_at=1000 + (n * 7) % 23) for n in range(23)],
          heard('What is going on?', 0, 900), WANTED, wait(1.02), route('del-1', asked(words('Much.')))),
    story('the user speaks while a tool is at work: what follows is left, and the new words wait for the next routing',
          voice('session.started'), heard('Ask Theo about the pump and Mike about the valve.', 0, 2000), WANTED, wait(1.02),
          route('del-1', asked(wants('delegate_to_agent', 'c-1', agent='theo', request='The pump?'),
                               wants('delegate_to_agent', 'c-2', agent='mike', request='The valve?'), words('Both asked.')),
                tools={'delegate_to_agent': ACCEPTED}, meanwhile={'delegate_to_agent': [heard('No, not Mike.', 5000, 5800)]}),
          wanted(2), wait(1.02), route('del-2', asked(words('Mike is left alone.')))),
    story('the user adds to their words while a tool is at work: the whole of them is new again',
          voice('session.started'), heard('Ask Theo about the pump and Mike about the valve.', 0, 2000), WANTED, wait(1.02),
          route('del-1', asked(wants('delegate_to_agent', 'c-1', agent='theo', request='The pump?'),
                               wants('delegate_to_agent', 'c-2', agent='mike', request='The valve?')),
                tools={'delegate_to_agent': {'status': 'failed', 'operation_id': 'rtc-f', 'session': 'theo',
                                             'note': 'Handed off, not finished.'}},
                meanwhile={'delegate_to_agent': [heard(' No, not Mike.', 2100, 2900)]}),
          wanted(2), wait(1.02), route('del-2', asked(words('Mike is left alone.'))),
          result('rtc-f', status='failed', text='', error='The agent is not there.'), wait(6.0), tick()),
    story('the user corrects themselves while it is written where a request came from: the request is not handed on',
          voice('session.started'), heard('Ask Theo about the pump.', 0, 2000), WANTED, wait(1.02),
          route('del-1', asked(wants('delegate_to_agent', 'c-1', agent='theo', request='The pump?'), words('Asked.')),
                tools={'delegate_to_agent': ACCEPTED}, meanwhile={'<written>': [heard('No, wait.', 5000, 5800)]}),
          wanted(2), wait(1.02), route('del-2', asked(words('Nothing was asked.'))),
          wanted(3), wait(0.02), route('del-3', asked(words('Never asked.')))),
    story('where a router decides, every delegation has its answer told',
          voice('session.started'), *TALK, speaks(), owns('rtc-1', 'rtc-2'),
          result('rtc-1', text='The same answer.', result_message_id='r-same'),
          result('rtc-2', text='The same answer.', result_message_id='r-same'),
          wait(6.0), tick(), speaks(), wait(6.0), tick(), wait(6.0), tick()),
    story('what an agent was told to leave is not read out',
          voice('session.started'), *TALK, speaks(), owns('rtc-1', 'rtc-2', 'rtc-3'),
          result('rtc-1', text='Of Theo.'), result('rtc-2', text='Of Mike.', session='mike'),
          result('rtc-3', text='Of Theo as well.'), tick(),
          call('supersede', session='theo'), wait(6.0), tick(), wait(6.0), tick(), call('supersede', session='nobody')),
    story('a router that answers what cannot be acted on',
          voice('session.started'), heard('Do something.', 0, 500), WANTED, wait(1.02),
          route('del-1', asked(odd('read_result', '[1]'))),
          heard('Again.', 3000, 3300), wanted(2), wait(1.02), route('del-2', asked(odd('delegate_to_agent', '[1]'))),
          heard('Once more.', 6000, 6300), wanted(3), wait(1.02), route('del-3', asked(odd('', '{}'))),
          heard('And again.', 9000, 9300), wanted(4), wait(1.02), route('del-4', asked(odd('fly_to_the_moon', '{}'))),
          heard('Last.', 12000, 12300), wanted(5), wait(1.02), route('del-5', asked(odd('get_agent_status', 7))),
          heard('Truly last.', 15000, 15300), wanted(6), wait(1.02), route('del-6', asked(body={'output': None})),
          heard('Or not.', 18000, 18300), wanted(7), wait(1.02), route('del-7', asked(body={'error': 'nothing'})),
          heard('Then this.', 21000, 21300), wanted(8), wait(1.02),
          route('del-8', asked({'type': 'message'}, {'type': 'message', 'content': [{'type': 'output_text'}]}, {'no': 'type'},
                               odd('get_agent_status', '{"agent": "theo"}'), odd('get_agent_status', '{"agent":"theo"}', 'c-2'),
                               odd('get_agent_status', '{"agent": "Theo"}', 'c-3')),
                tools={'get_agent_status': {'agent': 'Theo', 'state': 'idle', 'text': 'x' * 1500}}),
          heard('Nine.', 24000, 24300), wanted(9), wait(1.02), route('del-9', asked(body=[1])),
          heard('Ten.', 27000, 27300), wanted(10), wait(1.02), route('del-10', asked(words('Said.'), 'not a thing', words('Never.'))),
          heard('Eleven.', 30000, 30300), wanted(11), wait(1.02), route('del-11', asked({'type': 'message', 'content': 5})),
          heard('Twelve.', 33000, 33300), wanted(12), wait(1.02), route('del-12', asked({'type': 'message', 'content': ['x']})),
          heard('Thirteen.', 36000, 36300), wanted(13), wait(1.02),
          route('del-13', asked({'type': 'message', 'content': [{'type': 'output_text', 'text': 5}]})),
          heard('Fourteen.', 39000, 39300), wanted(14), wait(1.02),
          route('del-14', asked(wants('delegate_to_agent', 'c-1', agent='theo', request=5)), tools={'delegate_to_agent': ACCEPTED}),
          heard('Fifteen.', 42000, 42300), wanted(15), wait(1.02),
          route('del-15', asked(unnamed('delegate_to_agent', '{"agent": "theo", "request": "x"}')), tools={'delegate_to_agent': ACCEPTED}),
          heard('Sixteen.', 45000, 45300), wanted(16), wait(1.02),
          route('del-16', asked(wants('get_agent_status', 'c-1', agent='theo', request='Not a request to hand on.'),
                               wants('cancel_agent', 'c-2', agent='theo', request=''), unnamed('get_agent_status', '{"agent": "mike"}')),
                tools={'get_agent_status': {'agent': 'Theo', 'state': 'idle'}, 'cancel_agent': {'cancelled': 1}}),
          heard('Seventeen.', 48000, 48300), wanted(17), wait(1.02),
          route('del-17', asked(wants('get_agent_status', 'c-1', agent='theo'), words('Never.')), tools={'get_agent_status': ['no record']})),
    story('the one contact is asked with the words since the Oracle last spoke, and very many words by reference',
          voice('session.started'), heard('First this.', 0, 500), said('Yes?', 0, 200), WANTED, wait(1.02),
          route('del-1', tools={'investigate_with_oracle': ACCEPTED}),
          heard('Then that.', 3000, 3500), heard('And more.', 6000, 6500), wanted(2), wait(1.02),
          route('del-2', tools={'investigate_with_oracle': {'error': 'No Oracle contact selected; ask which contact should investigate'}}),
          said('Who?', 0, 100), heard('w' * 14001, 20000, 20500), wanted(3), wait(1.02),
          route('del-3', tools={'investigate_with_oracle': {'status': 'failed', 'operation_id': 'rtc-f', 'session': 'theo'}}),
          heard('x' * 14000, 30000, 30500), wanted(4), wait(1.02),
          route('del-4', tools={'investigate_with_oracle': 'The contact took nothing.'}),
          wanted(5), wait(0.02), route('del-5', tools={'investigate_with_oracle': ACCEPTED}),
          strategy='direct_contact'),
    story('no contact was chosen for a call in which everything goes to one',
          voice('session.started'), heard('Check the pump.', 0, 1000), WANTED, wait(1.02), route('del-1'),
          strategy='direct_contact', fallback=''),
    story('the one contact is no longer there',
          voice('session.started'), heard('Check the pump.', 0, 1000), WANTED, wait(1.02), route('del-1'),
          strategy='direct_contact', gone=['theo']),
    story('what was told to the voice in an earlier call is not told again',
          voice('session.started'), heard('Hello Oracle.', 0, 500), wait(0.3), speaks(), said('Hello.', 0, 300),
          wait(0.3), heard('Are you there?', 2000, 2600),
          attention(decisions=[decision('dec-1'), decision('dec-old', stale=True), decision('dec-2')],
                    completions=[completion('m-1')]),
          wait(6.0), tick(), wait(6.0), tick(), wait(6.0), tick(),
          claimed=[['decision', 'dec-1'], ['completion', 'm-1']]),
    # ---- what the first planted run of the kernel's rules missed
    story('the words that were handed to the contact are said back as they were said, six hundred signs of them',
          voice('session.started'), heard('Caf' + chr(0xe9) + ' ' + chr(0xe9) * 700, 0, 1500), WANTED, wait(1.02),
          route('del-1', tools={'investigate_with_oracle': ACCEPTED}), strategy='direct_contact'),
    story('the router is asked with the letters of the talk as they are',
          voice('session.started'), heard('Ask Th' + chr(0xe9) + 'o about the caf' + chr(0xe9) + '.', 0, 900), WANTED,
          wait(1.02), route('del-1', asked(words('Asked.')))),
    story('words that say nothing are nothing the user asked',
          voice('session.started'), heard('Check the pump.', 0, 900), heard('   ', 3000, 3100), WANTED, wait(1.02),
          route('del-1', asked(words('Checked.')))),
    story("the contact is handed the user's words since the voice last spoke, without the blanks around them",
          voice('session.started'), heard('First this.', 0, 500), said('Yes?', 0, 200), heard('  Then that.  ', 3000, 3500),
          heard(' And more. ', 6000, 6500), WANTED, wait(1.02), route('del-1', tools={'investigate_with_oracle': ACCEPTED}),
          strategy='direct_contact'),
    story('a voice that said nothing does not end the turn of the user',
          voice('session.started'), heard('First this.', 0, 500), said('   ', 0, 200), heard('Then that.', 3000, 3500),
          WANTED, wait(1.02), route('del-1', tools={'investigate_with_oracle': ACCEPTED}), strategy='direct_contact'),
    story('where the voice spoke last, the contact is handed the newest of what the user said',
          voice('session.started'), heard('First this.', 0, 500), heard('Then that.', 3000, 3500), said('Hm.', 0, 200),
          WANTED, wait(1.02), route('del-1', tools={'investigate_with_oracle': ACCEPTED}), strategy='direct_contact'),
    story('a thread that was kept with numbers that are not its own is routed as the Python Host routes it',
          voice('session.started'), heard('New words.', 0, 400), WANTED, wait(1.02), route('del-1', asked(words('Routed.'))),
          saved={'revision': 2, 'routed_revision': 9, 'results_sent': [],
                 'fragments': [{'role': 'user', 'text': 'Words under a number that was never reached.', 'revision': 10,
                                'provider_session': 'conn-0', 'end_ms': 10, 'at': 5}]}),
    story('what the user asked is routed though the voice said so much since that their words are kept no more',
          voice('session.started'), heard('Do it.', 0, 400),
          *[said(f'Word {n}.', 2000 * n, 2000 * n + 100) for n in range(1, 31)], WANTED, wait(1.02),
          route('del-1', asked(words('Nothing was asked.')))),
    story('an answer of exactly as many signs as a router is shown is not said to be cut',
          voice('session.started'), owns('rtc-1'), result('rtc-1', text='y' * 6000), heard('What came of it?', 0, 900), WANTED,
          wait(1.02), route('del-1', asked(words('That.')))),
    story('the router is shown how far an answer was told, also where it is read again from its beginning',
          voice('session.started'), *TALK, speaks(), owns('rtc-1'), wait(5.0), result('rtc-1', text=LONG * 9),
          tick(), *SPOKEN, *SPOKEN, call('read_result', operation_id='rtc-1', from_part=1),
          heard('And the valve?', 30000, 30900), WANTED, wait(1.02), route('del-1', asked(words('Nothing to do.')))),
    story('where a stop came from is written, where other work is handed on in its place',
          voice('session.started'), heard('Stop Theo and have him check the valve.', 0, 1500), WANTED, wait(1.02),
          route('del-1', asked(wants('cancel_agent', agent='theo', request='Check the valve instead.')),
                tools={'cancel_agent': {'status': 'accepted', 'operation_id': 'rtc-f', 'session': 'theo',
                                        'note': 'Handed off, not finished.'}})),
    story('what a tool answered that no text has room for fails its routing',
          voice('session.started'), heard('Do the long thing.', 0, 900), WANTED, wait(1.02),
          route('del-1', asked(wants('t' * 1500, agent='theo'), words('Never said.')), tools={'t' * 1500: {'text': 'x' * 3000}})),
    story('what waits for the user is looked for once a second, and no oftener',
          voice('session.started'), heard('Hello Oracle.', 0, 500), wait(0.3), speaks(), said('Hello.', 0, 300), wait(6.0),
          tick(), attention(decisions=[decision('dec-1')]), wait(0.5), tick(), wait(0.6), tick()),
    story('two things that wait are told four seconds apart',
          voice('session.started'), heard('Hello Oracle.', 0, 500), wait(0.3), speaks(), said('Hello.', 0, 300), wait(6.0),
          attention(decisions=[decision('dec-1'), decision('dec-2')]), tick(), wait(3.7), tick(), wait(1.1), tick()),
    story('a result is told when the voice has been quiet for a moment, and not at once',
          voice('session.started'), heard('What happened?', 0, 900), owns('rtc-1'), result('rtc-1', text='The pump ran dry.'),
          wait(5.0), speaks(), wait(0.6), tick(), wait(0.4), tick()),
    story('a second result waits four seconds after the first was told',
          voice('session.started'), heard('What happened?', 0, 900), owns('rtc-1', 'rtc-2'),
          result('rtc-1', text='The pump ran dry.'), result('rtc-2', text='The valve is fine.', session='mike'),
          wait(5.0), tick(), wait(3.7), tick(), wait(0.5), tick()),
    story('silence from the phone is nobody speaking: a result is told all the same',
          voice('session.started'), heard('What happened?', 0, 900), owns('rtc-1'), result('rtc-1', text='The pump ran dry.'),
          wait(3.0), hears(still()), wait(1.6), tick()),
    story('a long answer pauses after eight parts nobody asked for, and holds nothing back while it pauses',
          voice('session.started'), *TALK, speaks(), owns('rtc-1'), wait(5.0), result('rtc-1', text=LONG * 9), tick(),
          *([wait(0.1), speaks(), wait(2.0), tick()] * 8),
          owns('rtc-1', 'rtc-2'), result('rtc-2', text='Another answer.', session='mike'), wait(5.0), tick(),
          call('read_result', operation_id='rtc-1'), wait(0.1), speaks(), wait(2.0), tick()),
    story('a long answer that was interrupted holds nothing back',
          voice('session.started'), *TALK, speaks(), owns('rtc-1', 'rtc-2'), wait(5.0), result('rtc-1', text=LONG * 9),
          tick(), phone({'type': 'oracle_v2.interrupt'}), result('rtc-2', text='Another answer.', session='mike'),
          wait(5.0), tick()),
    story('a long answer holds nothing back once the user has spoken',
          voice('session.started'), *TALK, speaks(), owns('rtc-1', 'rtc-2'), wait(5.0), result('rtc-1', text=LONG * 9),
          tick(), wait(0.5), heard('And the valve?', 9000, 9500), wait(0.1), speaks(), wait(2.0), tick(),
          result('rtc-2', text='Another answer.', session='mike'), wait(5.0), tick()),
    story('the last part of an answer is something the voice was told: what else came waits its while after it',
          voice('session.started'), *TALK, speaks(), owns('rtc-1', 'rtc-2'), wait(5.0), result('rtc-1', text=LONG),
          tick(), result('rtc-2', text='Another answer.', session='mike'), wait(0.1), speaks(), wait(4.0), tick(),
          wait(3.7), tick(), wait(0.5), tick(), wait(0.1), speaks(), wait(2.0), tick()),
    story('a long answer does not go on while something is routed',
          voice('session.started'), *TALK, speaks(), owns('rtc-1'), wait(5.0), result('rtc-1', text=LONG * 9), tick(),
          wait(0.1), speaks(), WANTED, wait(2.0), tick(), route('del-1', asked(words('Noted.'))), tick()),
    story('a part the voice spoke holds back what came after it, however long the voice goes on',
          voice('session.started'), *TALK, speaks(), owns('rtc-1', 'rtc-2'), wait(5.0), result('rtc-1', text=LONG * 9), tick(),
          result('rtc-2', text='Another answer.', session='mike'),
          *([wait(0.1), speaks(), wait(0.9), tick()] * 17)),
    story('an answer that was read on request in the call before is told in the call that goes on in its place',
          voice('session.started'), result('rtc-1', text=LONG * 3), tick(), wait(6.0), tick(),
          saved={'revision': 1, 'fragments': [], 'results_sent': [], 'relay_cursors': {'rtc-1': 2}, 'call_active_ms': -5000,
                 'call_work': ['rtc-1']},
          work=['rtc-1']),
    story('an answer that was read word for word on request is told by itself as it was asked to be read',
          voice('session.started'), *TALK, speaks(), owns('rtc-1'), result('rtc-1', text='The pump ran dry.'),
          call('read_result', operation_id='rtc-1', verbatim=True), wait(6.0), tick()),
    story('words go on in what this connection said before fragments had numbers',
          voice('session.started'), heard('More words.', 150, 300), WANTED, wait(1.02), route('del-1', asked(words('Routed.'))),
          saved={'revision': 2, 'routed_revision': 0, 'results_sent': [],
                 'fragments': [{'role': 'user', 'text': 'Old words. ', 'provider_session': 'conn-1', 'end_ms': 100}]}),
    story('silence of a voice that was interrupted is not handed on',
          voice('session.started'), speaks(), wait(0.2), voice('session.output_audio.interrupted'), speaks(still()),
          wait(0.1), speaks(still())),
    story('an answer counts as told once its last part was read on request, also where only that part was asked for',
          voice('session.started'), *TALK, speaks(), owns('rtc-1', 'rtc-2', 'rtc-3'),
          result('rtc-1', text=LONG * 3), call('read_result', operation_id='rtc-1', from_part=5),
          result('rtc-2', text=LONG * 3), call('read_result', operation_id='rtc-2', from_part=1, verbatim=True),
          phone({'type': 'oracle_v2.interrupt'}),
          result('rtc-3', status='queued', text=''), call('read_result', operation_id='rtc-3'),
          wait(6.0), tick(), *SPOKEN * 6, wait(6.0), tick(), wait(6.0), tick(),
          call('read_result', operation_id='rtc-3'),
          owns('rtc-4'), result('rtc-4', text='Waits to be told.'), heard('One more thing.', 30000, 30500), tick(),
          call('read_result', operation_id='rtc-4'), wait(6.0), tick(), wait(6.0), tick()),
    story("the one contact's answer read to its end on request is not told again under another delegation",
          voice('session.started'), *TALK, speaks(), owns('rtc-1', 'rtc-2'),
          result('rtc-1', text='The pump ran dry.', result_message_id='r-same'),
          result('rtc-2', text='The pump ran dry.', result_message_id='r-same'),
          call('read_result', operation_id='rtc-1'), wait(6.0), tick(), wait(6.0), tick(),
          call('read_result', operation_id='rtc-2'), wait(6.0), tick(),
          strategy='direct_contact'),
    story("the one contact's answer that was begun to be told by itself goes on in the call that goes on in its place",
          voice('session.started'), result('rtc-1', text=LONG * 3), tick(), wait(6.0), tick(), *SPOKEN,
          result('rtc-2', text='Told, though what it is known by was kept as nothing.'), wait(0.3), *SPOKEN * 3, wait(6.3), tick(),
          wait(6.3), tick(),
          saved={'revision': 1, 'fragments': [], 'results_sent': [], 'relay_cursors': {'rtc-1': 1}, 'call_active_ms': -5000,
                 'call_work': ['rtc-1', 'rtc-2'],
                 'forwarded_findings': {finding('theo', 'rtc-1'): 'rtc-1', finding('theo', 'rtc-2'): ''}},
          work=['rtc-1', 'rtc-2'], strategy='direct_contact'),
    story("what a stop silenced is not told, and what landed after the stop's read is told once",
          voice('session.started'), *TALK, speaks(), owns('rtc-1', 'rtc-2', 'rtc-3', 'rtc-4', 'rtc-5'),
          result('rtc-0', text='Handed on before this call.'), result('rtc-5', text='Told before the stop.'), wait(6.0), tick(),
          result('rtc-3', text=LONG * 3), wait(5.0), tick(), result('rtc-1', text='Of Theo.'), tick(),
          result('rtc-4', status='cancelled', text=''), result('rtc-6', text='Of Mike.', session='mike'), owns('rtc-6'),
          owns('rtc-7', 'rtc-8'), result('rtc-7', status='accepted', text=''), result('rtc-8', text='Over, and not seen yet.'),
          lands(result('rtc-2', text='Landed after the read.')), call('supersede', session='theo'),
          result('rtc-7', text='Over after the stop.'),
          *SPOKEN, wait(6.0), tick(), wait(6.0), tick(), wait(6.0), tick(), wait(6.0), tick(),
          call('read_result', operation_id='rtc-3'), *SPOKEN * 6, wait(6.0), tick(),
          call('read_result', operation_id='rtc-1'), wait(6.0), tick(), call('supersede', session='theo'),
          call('supersede', session='mike'), wait(6.0), tick(),
          work=['rtc-0']),
    story('what a newer Host kept of a stop is read: what it silenced is not told by itself, and is read on request',
          voice('session.started'), result('rtc-1', text='Told before.'), result('rtc-2', text='Silenced by a stop.'),
          result('rtc-3', text='Not told yet.'), tick(), wait(6.0), tick(), wait(6.0), tick(),
          call('read_result', operation_id='rtc-2'), wait(6.0), tick(),
          result('rtc-4', text='Read to its end before.'), wait(6.0), tick(), wait(6.0), tick(),
          saved={'revision': 1, 'fragments': [], 'results_sent': ['rtc-1'], 'silenced_results': ['rtc-2', 'rtc-9'],
                 'relay_cursors': {'rtc-4': 1}, 'call_active_ms': -5000, 'call_work': ['rtc-1', 'rtc-2', 'rtc-3', 'rtc-4']},
          work=['rtc-1', 'rtc-2', 'rtc-3', 'rtc-4']),
    story('of the answers the contact gave under a delegation, the newest five hundred are kept',
          voice('session.started'), heard('Hello.', 0, 300),
          saved={'revision': 1, 'fragments': [], 'results_sent': [],
                 'forwarded_findings': {f'finding-{n:03}': f'rtc-{n}' for n in range(501)}},
          strategy='direct_contact'),
    story('an answer that ends at its eighth part is told to its end, and not paused',
          voice('session.started'), *TALK, speaks(), owns('rtc-1'), wait(5.0), result('rtc-1', text=LONG * 5),
          tick(), *SPOKEN * 16),
    story('an answer that waits to be told and is read on request to the eighth part is not gone on with by itself',
          voice('session.started'), *TALK, speaks(), owns('rtc-1'), result('rtc-1', text=LONG * 9), tick(),
          call('read_result', operation_id='rtc-1'), *SPOKEN * 16, wait(6.0), tick(), wait(6.0), tick(),
          call('read_result', operation_id='rtc-1')),
    story("the contact's answer known only beyond the newest five hundred is told again once, under its next alias",
          voice('session.started'), *TALK, speaks(), owns('rtc-2', 'rtc-3'),
          result('rtc-2', text='The pump ran dry.', result_message_id='r-rtc-1'),
          result('rtc-3', text='The pump ran dry.', result_message_id='r-rtc-1'),
          wait(6.0), tick(), wait(6.0), tick(), wait(6.0), tick(),
          saved={'revision': 1, 'fragments': [], 'results_sent': [], 'call_active_ms': -5000, 'call_work': [],
                 'forwarded_findings': {f'finding-{n:03}': f'rtc-old-{n}' for n in range(500)}},
          strategy='direct_contact'),
]


ROSTER = {'agents': [{'name': 'Theo', 'session': 'theo', 'backend': 'claude', 'personality': ''},
                     {'name': 'Mike', 'session': 'mike', 'backend': 'codex', 'personality': 'Dry.'}],
          'oracle_contact': 'theo', 'note': 'These are the agents you can reach; oracle_contact is your main colleague.'}


def run() -> dict:
    """Runs in a process of its own: what the Python Host's conversation does."""
    spec = importlib.util.spec_from_file_location('replay_host', REPO / 'tests' / 'replay' / 'host.py')
    host = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(host)
    root = pathlib.Path(tempfile.mkdtemp(prefix='clarp-oracle-conversation-vectors-', dir='/var/tmp'))
    (root / 'scenario-config.toml').write_text('')
    host.isolate(root)
    host.loopback_only()
    sys.path.insert(0, str(REPO / 'server'))
    import hashlib
    import threading

    from lib import oracle_attention, oracle_delegations, oracle_handoff, oracle_live_stable as live

    live.log = lambda *said, **more: None
    live.log_exception = lambda *said, **more: None
    # Two clocks, both counted in whole milliseconds: the one a call is timed by, and the wall's.
    clocks = {'ms': 0, 'wall': 1_800_000_000_000, 'ids': 0}
    live.wall_ms = lambda: clocks['wall']

    def passes(ms):
        clocks['ms'] += ms
        clocks['wall'] += ms

    class Ids:
        """Ids of events in the order they are made, where the Host draws them at random."""

        @staticmethod
        def uuid4():
            clocks['ids'] += 1

            class Drawn:
                hex = f'e-{clocks["ids"]}'
            return Drawn()
    live.uuid = Ids

    media = root / 'media'

    write = oracle_handoff.materialize
    pending_decisions = oracle_attention.pending_decisions

    def played(one, scale=1.0):
        opened = one['opened']
        clocks.update(ms=0, wall=1_800_000_000_000, ids=0)
        told = {'voice': [], 'phone': [], 'saved': None}
        rows = {}
        watched = {'decisions': [], 'completions': [], 'claimed': set()}
        script = {'router': [], 'tools': {}, 'meanwhile': {}}
        executed, router_asked = [], []

        def handed(arguments):
            """What a tool was handed: the file of where a request came from is read, and its name left out."""
            arguments = copy.deepcopy(arguments)
            found = re.search(r'excerpt at (\S+) before acting', str(arguments.get('request') or ''))
            if not found:
                return arguments, None
            arguments['request'] = arguments['request'].replace(found.group(1), '<written>')
            return arguments, json.loads(pathlib.Path(found.group(1)).read_text(encoding='utf-8'))

        class Voice:
            provider = opened.get('provider')

            def local_interrupt(self):
                told['voice'].append({'hook': 'local_interrupt'})

            def delegation_routing(self, ident):
                told['voice'].append({'hook': {'delegation_routing': ident}})

            def delegation_finished(self, ident):
                told['voice'].append({'hook': {'delegation_finished': ident}})

            def send(self, written):
                told['voice'].append(json.loads(written))

        class Tools:
            lock = threading.Lock()
            delegations = set()
            fallback = opened.get('fallback', 'theo')
            principal = OWNER
            ctx = types.SimpleNamespace(media_dir=str(media))

            def results(self):
                found = [copy.deepcopy(rows[ident]) for ident in sorted(self.delegations)
                         if ident in rows and rows[ident]['status'] in oracle_delegations.TERMINAL]
                # A result that is to land between this read and what is done with it.
                landing = script.pop('lands', None)
                if landing:
                    rows[landing['delegation_id']] = copy.deepcopy(landing)
                return found

            def resolve(self, name):
                if name in opened.get('gone', ()):
                    raise ValueError('Unknown or ambiguous agent; use a session from list_agents')
                return {'session': name}

            def execute(self, name, arguments, call_id):
                if name == 'list_agents':
                    return copy.deepcopy(ROSTER)
                arguments, provenance = handed(arguments)
                executed.append({'name': name, 'arguments': arguments, 'call_id': call_id, 'provenance': provenance})
                for event in script['meanwhile'].pop(name, ()):
                    conversation.receive(copy.deepcopy(event['said']))
                answer = script['tools'].get(name)
                if answer is None:
                    raise ValueError('Unknown Oracle tool')
                if isinstance(answer, str):
                    raise ValueError(answer)
                answer = copy.deepcopy(answer)
                # The tools of the fleet own every delegation they made, whatever came of it.
                if isinstance(answer, dict) and answer.get('operation_id'):
                    with self.lock:
                        self.delegations.add(answer['operation_id'])
                return answer

        class Memory:
            thread_id = THREAD
            owner = OWNER
            connection_id = 'conn-1'
            seen = set()
            admitted = {}

            def reconcile(self):
                pass

            def load(self):
                state = copy.deepcopy(opened.get('saved') or {})
                if type(state.get('call_active_ms')) is int:
                    state['call_active_ms'] += clocks['wall']
                return {'revision': 0, **state}

            def save(self, state):
                told['saved'] = json.loads(json.dumps(state))

            def observe(self, event, source_key):
                new = source_key not in self.seen
                self.seen.add(source_key)
                return new

            def work(self):
                return [{'delegation_id': ident} for ident in opened.get('work', [])]

            def contexts(self):
                return copy.deepcopy(opened.get('contexts', []))

            def _owned(self, active=False):
                return {}

            def admission(self, revision, index, tool, arguments):
                encoded = json.dumps(arguments, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
                call_id = 'oracle-' + hashlib.sha256(f'{self.thread_id}:{revision}:{index}'.encode()).hexdigest()[:40]
                row = self.admitted.setdefault((revision, index), {
                    'revision': revision, 'action_index': index, 'call_id': call_id, 'tool': tool,
                    'arguments_json': encoded, 'result_json': None, 'status': 'planned'})
                if row['tool'] != tool or row['arguments_json'] != encoded:
                    raise ValueError('A voice request cannot acquire a second conflicting action')
                return dict(row)

            def finish_admission(self, revision, index, answer):
                row = self.admitted[(revision, index)]
                if row['status'] == 'planned':
                    row.update(status='completed', result_json=json.dumps(answer, sort_keys=True))

            def admissions(self, revision=None):
                return [dict(row) for key, row in sorted(self.admitted.items()) if revision is None or key[0] == revision]

        Tools.delegations = set()
        Memory.seen = set()
        Memory.admitted = {}
        tools = Tools()
        oracle_delegations.get = lambda ident: copy.deepcopy(rows.get(ident))
        def looked_for():
            watched['looks'] = watched.get('looks', 0) + 1
            return copy.deepcopy(watched['decisions'])
        oracle_attention.pending_decisions = looked_for
        oracle_attention.pending_completion_notifications = lambda: copy.deepcopy(watched['completions'])

        def claim(memory, *, source_kind, source_id, reference_ts, stale=False):
            key = (source_kind, source_id)
            new = key not in watched['claimed']
            watched['claimed'].add(key)
            return new
        oracle_attention.claim_context = claim
        watched['claimed'].update(tuple(key) for key in opened.get('claimed', ()))
        # Every call of the Host's has the memory of its thread.
        memory = Memory()
        conversation = live.Conversation(Voice(), lambda event: told['phone'].append(json.loads(json.dumps(event))),
                                         tools, 'a-key', lambda: 100.0 + clocks['ms'] * scale / 1000.0,
                                         delegation_strategy=opened.get('strategy', 'operator'),
                                         memory=memory, provider_session='conn-1')
        if conversation.gemini:
            from lib.oracle_live_usage import LiveUsage
            conversation.usage = LiveUsage('gemini')
        conversation.principal = OWNER
        conversation.direct_call_prefix = 'prefix'
        conversation.pool.shutdown(wait=False)
        waiting = []

        class Pool:
            """Stands in for the threads that route: what is handed to it waits for the story to run it."""

            @staticmethod
            def submit(run, *given):
                waiting.append(given[0])
        conversation.pool = Pool

        class Stop:
            """Stands in for the word that ends a call: time passes while it is waited for."""
            ended = False

            def wait(self, seconds=None):
                passes(round((seconds or 0) * 1000))
                return self.ended

            def is_set(self):
                return self.ended

            def set(self):
                self.ended = True
        conversation.stop = Stop()

        class Answer:
            def __init__(self, body):
                self.body = body

            def __enter__(self):
                return io.StringIO(json.dumps(self.body))

            def __exit__(self, *given):
                return False

        def urlopen(request, timeout=None):
            body = json.loads(request.data)
            router_asked.append({'url': request.full_url, 'authorization': request.headers.get('Authorization'),
                                 'model': body['model'], 'input': json.loads(body['input']),
                                 'ascii': body['input'].isascii(),
                                 'tools_are_the_routers': body['tools'] == live.router_tools(),
                                 'instructions_are_the_routers': body['instructions'] == live.ROUTING,
                                 'rest': {key: body[key] for key in sorted(body) if key not in ('model', 'input', 'tools', 'instructions')},
                                 'at_ms': clocks['ms'], 'timeout': timeout})
            if not script['router']:
                raise RuntimeError('the router was asked more often than the story has answers')
            answer = script['router'].pop(0)
            # A router takes its time; what the voice says meanwhile is said while it thinks.
            passes(10)
            for event in answer['during']:
                conversation.receive(copy.deepcopy(event['said']))
            passes(20)
            if answer['fails']:
                raise OSError(answer['fails'])
            return Answer(copy.deepcopy(answer['body']) if 'body' in answer else {'output': copy.deepcopy(answer['output'])})
        live.urlopen = urlopen

        def materialize(*given, **named):
            """Where a request came from is written, and the voice may say something meanwhile."""
            note = write(*given, **named)
            for event in script['meanwhile'].pop('<written>', ()):
                conversation.receive(copy.deepcopy(event['said']))
            return note
        oracle_handoff.materialize = materialize

        def state():
            relay = conversation.active_relay
            return {'revision': conversation.revision, 'routed_revision': conversation.routed_revision,
                    'narration': conversation.narration, 'earcons': conversation.earcons,
                    'opening_answered': conversation.opening_answered,
                    'background_work': sorted(conversation.background_work),
                    'results_seen': sorted(conversation.results_seen), 'fragments': len(conversation.fragments),
                    'routing': conversation.routing, 'held': conversation.held_for_handoff,
                    'owned': sorted(tools.delegations), 'pending': [row['delegation_id'] for row in conversation.pending],
                    'cues': [list(cue) for cue in conversation.cue_queue],
                    'speaking': conversation.last_output_active,
                    'progress_interval': conversation.progress_interval,
                    'relay': relay and {'key': relay.key, 'next': relay.next, 'sent': relay.sent, 'auto': relay.auto,
                                        'held': relay.held, 'total': relay.total, 'verbatim': relay.verbatim},
                    'relays': sorted(conversation.relays)}
        steps = [{'do': 'open', 'voice': [], 'phone': [], 'saved': None, 'state': state()}]
        for step in one['steps']:
            told.update(voice=[], phone=[], saved=None)
            kind = step['do']
            more = {}
            if kind == 'wait':
                passes(round(step['seconds'] * 1000))
            elif kind == 'phone':
                event = live.client_event(step['raw'])
                more['event'] = event
                if event is None:
                    told['phone'].append({'type': 'oracle_v2.notice', 'message': 'Unsupported Oracle v2 command'})
                else:
                    # As the Host's own loop has it: a close that names a handoff is a plain close for the voice.
                    conversation.input({'type': 'session.close'} if event['type'] == 'session.close' else event)
            elif kind == 'voice':
                conversation.receive(copy.deepcopy(step['said']))
                more['to_route'] = list(waiting)
                more['closed'] = conversation.closed.is_set()
            elif kind == 'tick':
                # Whether what waits for the user was looked for, at each tick.
                more['looked'] = []
                for _ in range(step['count']):
                    before = watched.get('looks', 0)
                    conversation.tick()
                    more['looked'].append(watched.get('looks', 0) > before)
            elif kind == 'result':
                rows[step['row']['delegation_id']] = copy.deepcopy(step['row'])
            elif kind == 'lands':
                script['lands'] = copy.deepcopy(step['row'])
            elif kind == 'owns':
                with tools.lock:
                    tools.delegations.update(step['ids'])
                    tools.delegations.update(opened.get('owned_later', []))
            elif kind == 'attention':
                watched['decisions'], watched['completions'] = step['decisions'], step['completions']
                more['texts'] = {**{'decision:' + row['decision_id']: oracle_attention.context_text(row) for row in step['decisions']},
                                 **{'completion:' + row['source_message_id']: oracle_attention.completion_context_text(row)
                                    for row in step['completions']}}
            elif kind == 'call':
                answered = getattr(conversation, step['method'])(**copy.deepcopy(step['said']))
                more['answered'] = answered if isinstance(answered, bool) else None
            elif kind == 'route':
                waiting.remove(step['ident'])
                script.update(router=copy.deepcopy(step['router']), tools=copy.deepcopy(step['tools']),
                              meanwhile=copy.deepcopy(step['meanwhile']))
                del executed[:], router_asked[:]
                began = clocks['ms']
                conversation.route(step['ident'])
                more.update(router_asked=copy.deepcopy(router_asked), executed=copy.deepcopy(executed),
                            answers_left=len(script['router']), began_ms=began,
                            admissions=memory.admissions())
            else:
                raise SystemExit(f'no such step: {kind}')
            steps.append({**step, **more, 'at_ms': clocks['ms'], 'voice': told['voice'], 'phone': told['phone'],
                          'saved': told['saved'], 'state': state()})
        return {'name': one['name'], 'opened': opened, 'steps': steps}

    def at_the_edge(make, text_of, limit):
        """The two records around a bound: the longest that is told one way, and the one a byte longer."""
        for n in range(6000):
            here, after = len(text_of(make(n, 0)).encode()), len(text_of(make(n + 1, 1)).encode())
            if here == limit and after != limit + 1:
                return [make(n, 0), make(n + 1, 1)]
        raise SystemExit(f'no record at the edge of {limit} bytes')

    def with_edges(stories):
        """The story of what waits for the user, with records that are exactly as long as each bound allows."""
        stories = copy.deepcopy(stories)
        told = next(one for one in stories if one['name'].endswith('within its bounds'))['steps'][0]
        text, quote = oracle_attention.context_text, oracle_attention.completion_context_text
        head = len(text(decision('x')).split('{', 1)[0].encode())
        long = 'Shall I order it? ' * 60
        told['decisions'] += at_the_edge(lambda n, k: decision(f'dec-whole-{k}', question='q' * n), text,
                                         head + oracle_attention.MAX_CONTEXT_BYTES)
        told['decisions'] += at_the_edge(lambda n, k: decision(f'dec-short-{k}', question=long, artifact_id='a' * n), text,
                                         oracle_attention.MAX_APPEND_BYTES)
        told['decisions'] += at_the_edge(lambda n, k: decision(f'dec-least-{k}', question=long, artifact_id='a' * (n + 400)), text,
                                         oracle_attention.MAX_APPEND_BYTES)
        told['completions'] += at_the_edge(lambda n, k: completion(f'm-whole-{k}', source_text='s' * n), quote,
                                           oracle_attention.MAX_APPEND_BYTES)
        told['completions'] += at_the_edge(lambda n, k: completion(f'm-begins-{k}', source_text='s' * 2000, notification_id='n' * n),
                                           quote, oracle_attention.MAX_APPEND_BYTES)
        return stories

    def whatever_the_clock(one):
        """A story as it is played; and it is played the same by a clock that runs a little fast or slow:
        nothing in it is decided by two moments that are exactly a threshold apart."""
        plain = played(one)
        for scale in (1 + 1e-7, 1 - 1e-7):
            other = played(one, scale)
            for number, (step, again) in enumerate(zip(plain['steps'], other['steps'])):
                if json.dumps(step, sort_keys=True) != json.dumps(again, sort_keys=True):
                    edges.append(f'the story {one["name"]!r} is decided on the edge of a threshold of the clock: '
                                 f'at its step {number} ({step["do"]}, at {step.get("at_ms")} ms)')
                    break
        return plain

    edges = []
    stories = [whatever_the_clock(one) for one in with_edges(STORIES)]
    if edges:
        raise SystemExit('\n'.join(edges))
    # How much sound one piece from a phone may be: told by its length alone, the sound being silence.
    longest = [{'chars': chars, 'taken': live.client_event(json.dumps({'type': 'session.input_audio.append',
                                                                        'audio': 'A' * chars})) is not None}
               for chars in (4, 8, 262140, 262144, 262148, 262152, 400000)]
    # What the voice is told when a call begins: one thing differs at a time, and all of them at once.
    configs = []
    many = {'agents': [{'name': f'N{n}', 'session': f's{n}'} for n in range(45)], 'oracle_contact': None}
    spoken_before = [{'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'Saved.'}]}]
    vocabulary = {'terms': ['Drammen', 'Stra' + chr(0xdf) + 'e']}
    usual = {'roster': ROSTER, 'delegation_strategy': 'operator', 'voice_context': None, 'history': (), 'languages': ()}
    for differs in ({}, {'roster': None}, {'roster': {'agents': [], 'oracle_contact': ''}}, {'roster': many},
                    {'roster': {'oracle_contact': 'theo'}}, {'roster': {}}, {'delegation_strategy': 'direct_contact'},
                    {'voice_context': vocabulary}, {'history': spoken_before}, {'languages': ('nb',)},
                    {'languages': ('nb', 'en')}, {'languages': ('nb-NO', 'EN', 'xx', 'de')},
                    {'languages': ('NN', 'zh-Hant-TW', 'tlh')},
                    {'roster': many, 'delegation_strategy': 'direct_contact', 'voice_context': vocabulary,
                     'history': spoken_before, 'languages': ('en', 'nb', 'sv')}):
        given = {**usual, **differs}
        configs.append({**{key: (list(value) if isinstance(value, tuple) else value) for key, value in given.items()},
                        'config': live.live_config(**given)})
    # What waits for the user, as the voice is shown it: of what the Host's own list of it has.
    waits = {
        'decision_id': ' dec-1 ', 'artifact_id': 'art-1', 'agent_name': 'Theo', 'session': 'theo', 'kind': 'decision',
        'response_type': 'approval', 'title': 'A spare switch', 'question': '  Shall I order it?  ', 'context': ' Two days. ',
        'options': [{'id': 'a', 'label': 'Order it', 'description': 'Two days.'}, {'id': 'b', 'label': 'Wait', 'description': ''},
                    'not an option', {'id': 'c' * 90, 'label': None}, {'id': 'd', 'label': 'A fourth'}],
        'allow_custom_text': 1, 'recommended_option_id': 'a', 'blocks_progress': 0, 'priority': 2, 'created_at': 5,
        'updated_at': 7, 'deadline_at': None, 'expires_at': 9, 'status': 'pending', 'summary': 'not shown', 'agent_id': 'a-theo'}
    listed = [
        waits, {**waits, 'decision_id': 'dec-answered', 'status': 'answered'}, {'status': 'pending'},
        {**waits, 'decision_id': 'd' * 200, 'artifact_id': 'a' * 200, 'agent_name': '', 'session': 's' * 130,
         'kind': '', 'response_type': 'r' * 50, 'title': 't' * 170, 'recommended_option_id': 'o' * 90, 'options': None},
        {**waits, 'decision_id': 'dec-none', 'agent_name': None, 'session': None, 'kind': None, 'response_type': None,
         'title': None, 'question': None, 'context': None, 'options': [], 'recommended_option_id': None},
        {**waits, 'decision_id': 7, 'title': 7, 'question': 7, 'priority': 1.5},
        *[{**waits, 'decision_id': f'dec-{n:02}'} for n in range(24)],
    ]
    oracle_attention.artifacts.attention = lambda include_questions=False: copy.deepcopy(listed)
    pending = []
    for limit in (20, 0, 1, 3, 30, True, -1, 2.0, None):
        try:
            got = pending_decisions() if limit == 20 else pending_decisions(limit=limit)
            pending.append({'limit': limit, 'pending': got})
        except ValueError as refused:
            pending.append({'limit': limit, 'raises': str(refused)})
    # The languages an owner wrote for the Oracle, as the Host takes them: codes that are well made, each once.
    from lib import config
    config.log = lambda *said, **more: None
    written = [None, [], ['nb', 'en'], ['nb', ' en ', 'nb'], ['nb-NO', 'zh-Hant-TW', 'EN'], ['n', 'norsk', 'nb_NO', 'nb-', '-nb', 'nb--NO'],
               ['nb', 7, None, 'en'], 'nb', ['abc', 'ab-12345678', 'ab-123456789', 'ab-1'], ['n' + chr(0xe5)], ['nb\n', '\ten']]
    languages = [{'written': raw, 'taken': list(config._oracle_languages(raw))} for raw in written]
    return {'what': 'server/lib/oracle_live_stable.py: Conversation, client_event, live_config, router_tools',
            'listed': listed, 'pending': pending, 'languages': languages,
            'thread': THREAD, 'owner': OWNER, 'roster': ROSTER, 'longest_sound': longest,
            'router': {'model': live.ROUTER, 'instructions': live.ROUTING, 'tools': live.router_tools()},
            'voice': {'model': live.MODEL, 'voice': live.VOICE, 'prompt': live.PROMPT,
                      'direct': live.oracle_strategy.DIRECT_INSTRUCTIONS, 'narration_off': live.NARRATION_OFF,
                      'narration_on': live.NARRATION_ON, 'cut_note': live.APPEND_CUT_NOTE,
                      'languages': live.LANGUAGE_NAMES},
            'quiet_after_s': live.QUIET_AFTER_SECONDS, 'result_release_silence_s': live.RESULT_RELEASE_SILENCE_SECONDS,
            'relay_stall_s': live.RELAY_STALL_SECONDS, 'earcon_quiet_s': live.EARCON_QUIET_SECONDS,
            'output_hangover_s': live.OUTPUT_HANGOVER, 'call_resume_grace_s': live.CALL_RESUME_GRACE_SECONDS,
            'narration_off': live.NARRATION_OFF, 'narration_on': live.NARRATION_ON, 'configs': configs, 'stories': stories}


def shrunk(value, cue=None):
    """What is long and says nothing here is written down short: one sign many times over as the sign and how
    many (`{"runs": [...]}`: texts as they are, and `[sign, count]` for a run of it), and the sound of a cue as the
    cue it is. Whoever reads the file writes them out again."""
    if isinstance(value, str):
        parts = []
        for found in re.finditer(r'(.)\1{63,}|(?:(.)(?!\2{63}))+', value, flags=re.S):
            parts.append([found.group(1), len(found.group(0))] if found.group(1) else found.group(0))
        return {'runs': parts} if any(isinstance(part, list) for part in parts) else value
    if isinstance(value, list):
        out, before = [], None
        for item in value:
            is_cue = isinstance(before, dict) and before.get('type') == 'oracle_v2.cue'
            if is_cue and isinstance(item, dict) and item.get('type') == 'session.output_audio.delta':
                out.append({**item, 'delta': {'earcon': [before['name'], before.get('agent') or '']}})
            else:
                out.append(shrunk(item))
            before = item
        return out
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
        print(ran.stderr[-4000:], file=sys.stderr)
        return 1
    whole = json.loads(ran.stdout.splitlines()[-1])
    ROUTER.write_text(json.dumps(whole['router'], ensure_ascii=True, indent=1) + '\n', encoding='ascii')
    VOICE.write_text(json.dumps(whole.pop('voice'), ensure_ascii=True, indent=1) + '\n', encoding='ascii')
    said = shrunk(whole)
    out = io.StringIO()
    out.write('{\n')
    parts = list(said)
    for at, part in enumerate(parts):
        end = ',' if at < len(parts) - 1 else ''
        if part == 'configs':
            out.write(' "configs": [\n')
            for n, row in enumerate(said[part]):
                out.write('  ' + json.dumps(row, ensure_ascii=True) + (',' if n < len(said[part]) - 1 else '') + '\n')
            out.write(' ]' + end + '\n')
            continue
        if part != 'stories':
            out.write(f' {json.dumps(part)}: {json.dumps(said[part], ensure_ascii=True)}{end}\n')
            continue
        out.write(' "stories": [\n')
        for index, one in enumerate(said[part]):
            out.write('  {"name": ' + json.dumps(one['name'], ensure_ascii=True) + ', "opened": '
                      + json.dumps(one['opened'], ensure_ascii=True) + ', "steps": [\n')
            for n, row in enumerate(one['steps']):
                out.write('   ' + json.dumps(row, ensure_ascii=True) + (',' if n < len(one['steps']) - 1 else '') + '\n')
            out.write('  ]}' + (',' if index < len(said[part]) - 1 else '') + '\n')
        out.write(' ]' + end + '\n')
    out.write('}\n')
    OUT.write_text(out.getvalue(), encoding='ascii')
    steps = [step for one in said['stories'] for step in one['steps']]
    print(f'{OUT.relative_to(REPO)}: {len(said["stories"])} stories, {len(steps)} steps, '
          f'{sum(len(step["voice"]) for step in steps)} things told to the voice, '
          f'{sum(len(step["phone"]) for step in steps)} to the phone, {OUT.stat().st_size} bytes')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
