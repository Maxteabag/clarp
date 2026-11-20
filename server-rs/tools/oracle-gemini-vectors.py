#!/usr/bin/env python3
"""Writes down how the Python Host speaks with Gemini Live in the words of the Oracle's first voice.

A call of the Oracle speaks with its voice in the events of GPT-Live (`session.*`). Where the owner chose the
second voice, `server/lib/oracle_gemini_live.GeminiUpstream` stands between the call and Gemini Live: what the call
says becomes Gemini's messages, and what Gemini says becomes the events the call knows. That translation is run
here as it is, on a socket of this program's own that keeps what it is sent and says what a story hands it, with a
clock and ids of this program's own.

For every story it is written down, step by step, what was done (the call said an event, Gemini said a message,
the phone interrupted, a routing began or ended, time passed, the connection was taken up again), what went to
Gemini for it, which events the call was given, and what could be asked of the translation after it: whether it
began, whether it is closed, whether it is time to take the connection up again, by what name of Gemini's a new
connection takes the call up, and what the call has used. Beside the stories: whether the second voice is held to be well after it
failed, as time passes.

    <the Python Host's interpreter> server-rs/tools/oracle-gemini-vectors.py

It runs in a process of its own whose every home, data, config and cache directory is a new empty directory (the
replay harness's own isolation), with no way out of the machine: no connection is opened. The same file at every
run. Nothing is logged.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import scratch as tool_scratch

REPO = pathlib.Path(__file__).resolve().parents[2]
OUT = REPO / 'server-rs' / 'crates' / 'clarp-kernel' / 'tests' / 'fixtures' / 'oracle-gemini-vectors.json'

LOUD = 'AQIDBAUGBwg='
START = {'type': 'session.start', 'session': {
    'model': 'gpt-live', 'instructions': 'You are the Oracle.\nRoster: Theo (theo).',
    'audio': {'format': {'type': 'audio/pcm', 'rate': 24000}, 'output': {'voice': 'marin'}},
    'delegation': {'type': 'client'}}}
HISTORY = [
    {'type': 'message', 'role': 'user', 'content': [
        {'type': 'input_text', 'text': 'Saved Oracle reference data; not a new user request:\n{"thread_id": "oracle-1"}'}]},
    {'type': 'message', 'role': 'user', 'content': [
        {'type': 'input_text', 'text': 'A second part, with Bod' + chr(0xf8) + '.'}, {'type': 'input_audio'}, 'no part',
        {'type': 'input_text', 'text': 7}]},
    'no item',
    {'type': 'message', 'role': 'user'},
    {'type': 'message', 'role': 'user', 'content': None},
]


def started(**more):
    """The steps every story of a call that began begins with."""
    session = dict(START['session'], **more)
    return [('send', {'type': 'session.start', 'session': session}), ('message', {'setupComplete': {}})]


def audio(data=LOUD):
    return {'serverContent': {'modelTurn': {'parts': [{'inlineData': {'data': data, 'mimeType': 'audio/pcm;rate=24000'}}]}}}


def wants(ident, request='Ask Theo about the pump.', name='delegate_to_agent', **more):
    call = {'id': ident, 'name': name, 'args': {'request': request}}
    call.update(more)
    return {'toolCall': {'functionCalls': [call]}}


def says(kind, content):
    return ('send', {'type': f'session.{kind}.append', 'content': content})


def handle(name='handle-1', resumable=True):
    return ('message', {'sessionResumptionUpdate': {'resumable': resumable, 'newHandle': name}})


def usage(**said):
    return ('message', {'usageMetadata': said})


# A story: its name, how the translation is made (`languages`), and its steps. A step: `('send', event)` the call
# says an event; `('message', message)` Gemini says a message; `('interrupt',)` the phone cut what it plays;
# `('routing', id)` and `('finished', id)` a routing of what the voice wanted began and ended; `('wait', seconds)`;
# `('resume', reason, [what the new connection says])` the connection is taken up again; `('breaks',)` the next
# thing that is sent cannot be sent.
MALFORMED = [
    ('modelTurn', {'serverContent': {'modelTurn': 7}}),
    ('inlineData', {'serverContent': {'modelTurn': {'parts': [{'inlineData': 7}]}}}),
    ('inputTranscription', {'serverContent': {'inputTranscription': 7}}),
    ('outputTranscription', {'serverContent': {'outputTranscription': 7}}),
    ('parts', {'serverContent': {'modelTurn': {'parts': 7}}}),
    ('functionCalls', {'toolCall': {'functionCalls': 7}}),
    ('args', wants('bad-call', args=7)),
    ('ids', {'toolCallCancellation': {'ids': 7}}),
    ('promptTokensDetails', {'usageMetadata': {'promptTokensDetails': 7}}),
    ('responseTokensDetails', {'usageMetadata': {'responseTokensDetails': 7}}),
]
STORIES = [
    *[('malformed ' + field, {}, started() + [('message', payload)]) for field, payload in MALFORMED],
    *[('malformed departure ' + name, {}, started() + [('message', audio()),
        ('message', {'goAway': {'timeLeft': '60s'}}), ('message', {'goAway': value}), ('wait', 2.999), ('wait', .002)])
        for name, value in [('text', 'soon'), ('number', 7), ('list', [3])]],
    ('a lost connection holds back new calls until another setup is accepted', {}, [
        ('lost',), ('wait', 599), ('message', {'setupComplete': {}}), ('lost',),
    ]),
    ('several effects in one provider message keep their order', {}, [
        ('message', {'setupComplete': {}, 'usageMetadata': {}, 'serverContent': {
            'interrupted': True, 'modelTurn': {'parts': [{'inlineData': {'data': 'AAAA'}}]},
            'inputTranscription': {'text': 'Asked.'}, 'outputTranscription': {'text': 'Heard.'}},
            'toolCall': {'functionCalls': [{'name': 'delegate_to_agent', 'id': 'd'}]},
            'toolCallCancellation': {'ids': ['d']}}),
        ('routing', 'd'), says('commentary', 'A cancelled answer.'), ('finished', 'd'),
    ]),
    ('a call begins, with nothing kept of before', {}, [
        ('send', START), ('message', {'setupComplete': {}}), ('message', {'setupComplete': {}}),
    ]),
    ('a call begins with what was kept of before, and the languages its user speaks', {'languages': ['nb-NO', 'en-US']}, [
        ('send', {'type': 'session.start', 'session': dict(START['session'], input=HISTORY)}),
        ('message', {'setupComplete': {}, 'sessionResumptionUpdate': {'resumable': True, 'newHandle': 'h0'}}),
    ]),
    ('a call begins with a session that says nothing', {}, [
        ('send', {'type': 'session.start'}), ('send', {'type': 'session.start', 'session': None}),
        ('send', {'type': 'session.start', 'session': {'instructions': None, 'input': 'no list'}}),
        ('send', {'type': 'session.start', 'session': {'instructions': 7, 'input': [{'content': [{'text': ''}]}]}}),
        ('send', {'type': 'session.start', 'session': {'input': [{'content': [{'text': 'one'}, {'text': 'two'}]}]}}),
    ]),
    ('what the call says before it began, and what is no event of its own', {}, [
        ('send', {'type': 'session.input_audio.append', 'audio': LOUD}),
        ('send', {'type': 'session.update', 'session': {}}), ('send', {'type': 'oracle_v2.interrupt'}),
        ('send', {'no type': 1}), says('commentary', 'Said before the call began.'),
    ]),
    ('sound goes up and comes down', {}, started() + [
        ('send', {'type': 'session.input_audio.append', 'audio': LOUD}),
        ('message', audio()), ('message', audio('AAAA')),
        ('message', {'serverContent': {'modelTurn': {'parts': [
            {'inlineData': {'data': ''}}, {'inlineData': {'data': 7}}, {'inlineData': None}, {'text': 'thinking'}, 'no part',
            {'inlineData': {'data': 'BBBB'}}, {'inlineData': {'data': 'CCCC'}}]}}}),
        ('message', {'serverContent': {'modelTurn': None}}), ('message', {'serverContent': {'modelTurn': {'parts': None}}}),
        ('message', {'serverContent': 'no content'}), ('message', {'serverContent': {'turnComplete': True}}),
        ('message', {}), ('message', {'something': 'else'}),
    ]),
    ('the phone cuts what it plays: the rest of that answer is not passed on', {}, started() + [
        ('interrupt',), ('message', audio('AAAA')), ('interrupt',), ('message', audio('BBBB')),
        ('message', {'serverContent': {'modelTurn': {'parts': [{'inlineData': {'data': 'CCCC'}}]}, 'turnComplete': True}}),
        ('message', audio('DDDD')), ('interrupt',), ('message', audio('EEEE')),
        ('message', {'serverContent': {'interrupted': True}}), ('message', audio('FFFF')),
    ]),
    ('Gemini stops for its user', {}, started() + [
        ('message', audio('AAAA')),
        ('message', {'serverContent': {'interrupted': True, 'modelTurn': {'parts': [{'inlineData': {'data': 'BBBB'}}]}}}),
        ('message', {'serverContent': {'interrupted': False}}), ('message', {'serverContent': {'interrupted': 1}}),
    ]),
    ('what was heard and what was said, written down', {}, started() + [
        ('wait', 1.25),
        ('message', {'serverContent': {'inputTranscription': {'text': 'Ask Theo'}}}),
        ('wait', 0.0004),
        ('message', {'serverContent': {'outputTranscription': {'text': 'I will.'}, 'inputTranscription': {'text': ' about the pump.'}}}),
        ('message', {'serverContent': {'inputTranscription': {'text': ''}, 'outputTranscription': {'text': 7}}}),
        ('message', {'serverContent': {'inputTranscription': None, 'outputTranscription': {}}}),
        ('wait', 3600),
        ('message', {'serverContent': {'outputTranscription': {'text': 'Bod' + chr(0xf8)}, 'turnComplete': True}}),
    ]),
    ('the voice wants something done, and is answered when the routing ended', {}, started() + [
        ('message', wants('call-1')), ('routing', 'call-1'),
        says('thinking', 'Theo is asked.'), says('commentary', 'Theo says the pump is mended.'),
        says('instructions', 'Say it briefly.'), ('finished', 'call-1'),
        says('commentary', 'Said after the routing.'), ('finished', 'call-1'),
    ]),
    ('a routing that says nothing, one that only thinks, and one nobody began', {}, started() + [
        ('message', wants('call-1', request='')), ('routing', 'call-1'), ('finished', 'call-1'),
        ('message', wants('call-2', args=None)), ('routing', 'call-2'), says('thinking', 'Looked, found nothing.'),
        ('finished', 'call-2'),
        ('message', wants('call-3', args={'request': 7})), ('finished', 'call-3'),
        ('routing', 'call-9'), says('thinking', 'Nobody is routed now.'), ('finished', 'call-9'),
    ]),
    ('two things wanted at once: what is said goes to the one that is routed', {}, started() + [
        ('message', {'toolCall': {'functionCalls': [
            {'id': 'call-1', 'name': 'delegate_to_agent', 'args': {'request': 'One.'}},
            {'id': 'call-2', 'name': 'delegate_to_agent', 'args': {'request': 'Two.'}}]}}),
        ('routing', 'call-1'), says('commentary', 'For the first.'), ('routing', 'call-2'),
        says('thinking', 'For the second.'), ('finished', 'call-1'), says('commentary', 'For the second, still.'),
        ('finished', 'call-2'), says('thinking', 'For nobody.'),
    ]),
    ('what is wanted and cannot be: another function, no name, no id', {}, started() + [
        ('message', wants('call-1', name='look_up')), ('message', wants('call-2', name=None)),
        ('message', wants('', request='No id.')), ('message', wants(7)), ('message', wants(None)),
        ('message', {'toolCall': {'functionCalls': None}}), ('message', {'toolCall': 'no call'}),
        ('message', {'toolCall': {}}), ('routing', 'call-1'), ('finished', 'call-1'),
    ]),
    ('Gemini takes back what it wanted: what the routing says is told all the same', {}, started() + [
        ('message', wants('call-1')), ('message', wants('call-2')), ('message', wants('call-3')),
        ('message', {'toolCallCancellation': {'ids': ['call-1', 'call-9', 'call-3']}}),
        ('routing', 'call-1'), says('thinking', 'Theo is asked.'), says('commentary', 'Theo answers.'),
        says('thinking', 'Background after the answer.'), ('finished', 'call-1'),
        ('routing', 'call-3'), says('thinking', 'Only thought.'), says('thinking', 'Twice.'), ('finished', 'call-3'),
        ('routing', 'call-2'), ('message', {'toolCallCancellation': {'ids': None}}),
        ('message', {'toolCallCancellation': 'no ids'}), ('message', {'toolCallCancellation': {'ids': ['call-2']}}),
        ('finished', 'call-2'),
    ]),
    ('what the call says to its voice outside a routing', {}, started() + [
        says('commentary', 'An agent finished.'), says('thinking', 'Something from before.'),
        says('instructions', 'Stop speaking now and listen.'), says('commentary', ''), says('thinking', None),
        says('commentary', 7), ('send', {'type': 'session.commentary.append'}),
    ]),
    ('what a call used', {}, started() + [
        ('wait', 12.34),
        usage(promptTokensDetails=[{'modality': 'AUDIO', 'tokenCount': 1000}, {'modality': 'TEXT', 'tokenCount': 400}],
              responseTokensDetails=[{'modality': 'AUDIO', 'tokenCount': 250}], thoughtsTokenCount=30),
        ('wait', 0.06),
        usage(promptTokensDetails=[{'modality': 'IMAGE', 'tokenCount': 7}, {'modality': 'VIDEO', 'tokenCount': 11},
                                   {'tokenCount': 13}, {'modality': None, 'tokenCount': 17}, {'modality': '', 'tokenCount': 19}],
              responseTokensDetails=[{'modality': 'TEXT', 'tokenCount': 23}, {'modality': 'IMAGE', 'tokenCount': 29}]),
        usage(promptTokensDetails=[{'modality': 'AUDIO', 'tokenCount': True}, {'modality': 'AUDIO', 'tokenCount': 1.5},
                                   {'modality': 'AUDIO', 'tokenCount': '9'}, {'modality': 'AUDIO'}, 'no row',
                                   {'modality': 'AUDIO', 'tokenCount': -5}, {'modality': 'AUDIO', 'tokenCount': 0}],
              responseTokensDetails=None, thoughtsTokenCount=True),
        usage(thoughtsTokenCount=1.5), usage(thoughtsTokenCount=1000000), usage(),
        ('message', {'usageMetadata': 'no usage'}), ('message', {'usageMetadata': None}),
        ('wait', 0.05), usage(promptTokensDetails=[{'modality': 'audio', 'tokenCount': 3}]),
        ('send', {'type': 'session.close'}),
    ]),
    ('a call is ended by its phone, and nothing goes to Gemini after', {}, started() + [
        ('wait', 2.26), ('send', {'type': 'session.close'}),
        ('send', {'type': 'session.input_audio.append', 'audio': LOUD}), says('commentary', 'Too late.'),
        ('message', audio()), ('send', {'type': 'session.close'}), ('interrupt',), ('finished', 'call-1'),
    ]),
    ('Gemini says how the call can be taken up again', {}, started() + [
        handle('h1'), handle('h2', resumable=False), handle('', resumable=True), handle(None), handle(7),
        ('message', {'sessionResumptionUpdate': {'newHandle': 'h3'}}),
        ('message', {'sessionResumptionUpdate': {'resumable': 1, 'newHandle': 'h4'}}),
        ('message', {'sessionResumptionUpdate': 'no update'}), ('message', {'sessionResumptionUpdate': None}),
    ]),
    ('Gemini says it will go: the connection is taken up again between two answers, or just before', {}, started() + [
        handle('h1'), ('message', audio('AAAA')), ('message', {'goAway': {'timeLeft': '9.5s'}}),
        ('wait', 7.4), ('wait', 0.2), ('message', {'serverContent': {'turnComplete': True}}),
        ('resume', 'go_away', [{'setupComplete': {}}]),
        ('message', {'goAway': {'timeLeft': '3s'}}), ('resume', 'go_away', [{'sessionResumptionUpdate': {}}, {'setupComplete': {}}]),
    ]),
    ('how long Gemini says it has left, as it is written', {}, started() + [handle('h1')] + [
        step for left in ['9.5s', ' 3s ', '3', '1.s', '.5s', '0s', '2s', '1.9s', '2.5s', 5, None, '', 's', '1e3s', '-4s', '10.25s\n', ['5s']]
        for step in (('message', audio('AAAA')), ('message', {'goAway': {'timeLeft': left}}), ('wait', 0.4), ('wait', 2.7),
                     ('wait', 2.0), ('message', {'serverContent': {'turnComplete': True}}))
    ] + [('message', {'goAway': None}), ('message', {'goAway': {}}), ('message', {'goAway': 'soon'})]),
    ('a connection is taken up again: what it is begun with, and what comes of it', {'languages': ['nb-NO']}, started() + [
        ('resume', 'connection_lost', [{'setupComplete': {}}]),
        handle('h1'), ('message', audio('AAAA')), ('interrupt',),
        ('resume', 'connection_lost', [{'serverContent': {}}, 'timeout', {'setupComplete': {}}, {'serverContent': {}}]),
        ('message', audio('BBBB')), handle('h2'),
        ('resume', 'go_away', ['timeout', 'timeout', {'setupComplete': {}}]),
        ('resume', 'anything', [{'setupComplete': {}}]),
    ]),
    ('a connection that cannot be taken up again', {}, started() + [
        handle('h1'), ('resume', 'connection_lost', ['closed']),
        ('resume', 'connection_lost', ['timeout', ('wait', 9.9), 'timeout', ('wait', 0.2), 'timeout']),
        ('resume', 'connection_lost', ['unreachable']),
        ('resume', 'connection_lost', [{'setupComplete': {}}]),
    ]),
    ('nothing is taken up again of a call that did not begin, or that is over', {}, [
        ('resume', 'connection_lost', [{'setupComplete': {}}]), handle('h1'),
        ('resume', 'connection_lost', [{'setupComplete': {}}]),
        ('send', START), ('resume', 'connection_lost', [{'setupComplete': {}}]),
        ('send', {'type': 'session.close'}), ('resume', 'connection_lost', [{'setupComplete': {}}]),
    ]),
    ('what cannot be sent: sound is let go while a call can be taken up again, and nothing else is', {}, started() + [
        ('breaks',), ('send', {'type': 'session.input_audio.append', 'audio': LOUD}),
        handle('h1'), ('breaks',), ('send', {'type': 'session.input_audio.append', 'audio': LOUD}),
        ('breaks',), says('commentary', 'An agent finished.'),
        ('message', wants('call-1')), ('breaks',), ('finished', 'call-1'),
        ('breaks',), ('message', wants('call-2', name='look_up')),
    ]),
]

# Whether the second voice is held to be well, as time passes: `('fails',)`, `('begins',)` (a call of it began),
# `('wait', seconds)`.
WELL = [
    [('wait', 5)], [('fails',), ('wait', 599.9), ('wait', 0.1), ('wait', 1)],
    [('fails',), ('wait', 300), ('fails',), ('wait', 300), ('wait', 300)],
    [('fails',), ('wait', 10), ('begins',), ('wait', 1), ('fails',), ('begins',)],
]


@tool_scratch.case('clarp-oracle-gemini-vectors-')
def run(root) -> dict:
    """Runs in a process of its own: what the Python Host's module does."""
    spec = importlib.util.spec_from_file_location('replay_host', REPO / 'tests' / 'replay' / 'host.py')
    host = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(host)
    (root / 'scenario-config.toml').write_text('')
    host.isolate(root)
    host.loopback_only()
    sys.path.insert(0, str(REPO / 'server'))
    import websocket

    from lib import oracle_gemini_live as rule

    class Clock:
        now = 1000.0

        def __call__(self):
            return self.now

    class Ids:
        made = 0

        def uuid4(self):
            Ids.made += 1
            return self

        @property
        def hex(self):
            return f'{Ids.made:032x}'

    class Socket:
        """A connection of this program's own: it keeps what it is sent and says what it was handed."""

        def __init__(self, says=()):
            self.sent, self.says, self.broken, self.closed = [], list(says), False, False

        def send(self, raw):
            if self.broken:
                self.broken = False
                raise OSError('the connection is gone')
            self.sent.append(json.loads(raw))

        def recv(self):
            while self.says:
                said = self.says.pop(0)
                if isinstance(said, tuple):
                    clock.now += said[1]
                    continue
                if said == 'timeout':
                    raise websocket.WebSocketTimeoutException()
                if said == 'closed':
                    return ''
                return json.dumps(said)
            raise websocket.WebSocketTimeoutException()

        def settimeout(self, _value):
            pass

        def close(self):
            self.closed = True

    clock = Clock()
    rule.uuid = Ids()
    told = []
    for name, how, steps in STORIES:
        clock.now, Ids.made = 1000.0, 0
        opened = []

        def connect(_key, opened=opened, following=None):
            # A story's `resume` says what the next connection says; the first says nothing by itself.
            says = opened[-1].next if opened and getattr(opened[-1], 'next', None) is not None else ()
            if says == ['unreachable']:
                raise OSError('no way to Gemini')
            made = Socket(says)
            opened.append(made)
            return made

        rule._connect = connect
        rule._failed_at = None
        upstream = rule.GeminiUpstream('a-key-of-the-test', model='gemini-live-of-the-test', voice='Kore',
                                       languages=how.get('languages', ()), clock=clock)
        # Which of the things that go to Gemini are let go where they cannot be sent while the call can be taken
        # up again: asked of the translation's own sending.
        flagged = []
        send_of = upstream._send

        def send(message, *, drop_when_reconnecting=False, send_of=send_of, flagged=flagged):
            flagged.append(drop_when_reconnecting)
            return send_of(message, drop_when_reconnecting=drop_when_reconnecting)

        upstream._send = send
        played = []
        for step in steps:
            what = step[0]
            done = {'do': what, 'at': round(clock.now - 1000.0, 4)}
            before = [len(one.sent) for one in opened]
            del flagged[:]
            raised = None
            try:
                if what == 'send':
                    done['event'] = step[1]
                    upstream.send(json.dumps(step[1]))
                elif what == 'message':
                    done['message'] = step[1]
                    upstream.translate(step[1])
                elif what == 'lost':
                    upstream.lost()
                elif what == 'interrupt':
                    upstream.local_interrupt()
                elif what == 'routing':
                    done['id'] = step[1]
                    upstream.delegation_routing(step[1])
                elif what == 'finished':
                    done['id'] = step[1]
                    upstream.delegation_finished(step[1])
                elif what == 'wait':
                    done['seconds'] = step[1]
                    clock.now += step[1]
                elif what == 'breaks':
                    if upstream.ws is not None:
                        upstream.ws.broken = True
                elif what == 'resume':
                    done['reason'] = step[1]
                    done['says'] = [list(one) if isinstance(one, tuple) else one for one in step[2]]
                    opened[-1].next = list(step[2])
                    done['resumed'] = upstream.resume(step[1])
                    opened[-1].next = None
            except (rule.GeminiUnavailable, OSError) as failed:
                raised = type(failed).__name__ + ': ' + str(failed)
                if what == 'resume':
                    opened[-1].next = None
            done['raised'] = raised
            # What went to Gemini in this step, on the connection it went on (counted from the first), in its
            # order; and whether it is let go where it cannot be sent.
            sent = []
            for number, one in enumerate(opened):
                was = before[number] if number < len(before) else 0
                sent.extend({'on': number, 'message': message} for message in one.sent[was:])
            done['sent'] = sent
            done['asked_to_send'] = list(flagged)
            done['events'] = list(upstream.events)
            upstream.events.clear()
            done['began'] = upstream.started
            done['closed'] = upstream.closed
            done['connections'] = len(opened)
            done['hung_up'] = [number for number, one in enumerate(opened) if one.closed]
            done['due'] = bool(upstream.reconnect_at is not None
                               and (not upstream.speaking or clock.now >= upstream.reconnect_at))
            # Whether the call can be taken up again, and by what name of Gemini's: a new connection is begun with
            # what the first was begun with, and that name in it (a step `resume` shows it sent).
            can = not upstream.closed and bool(upstream.handle) and upstream.setup is not None
            done['taken_up_by'] = upstream.handle if can else None
            done['used'] = upstream.usage()
            done['well'] = rule.healthy(clock)
            played.append(done)
        told.append({'name': name, 'model': 'gemini-live-of-the-test', 'voice': 'Kore',
                     'languages': list(how.get('languages', ())), 'steps': played})

    well = []
    for steps in WELL:
        clock.now = 1000.0
        rule._failed_at = None
        played = []
        for step in steps:
            if step[0] == 'fails':
                rule.mark_failed(clock)
            elif step[0] == 'begins':
                rule.mark_healthy()
            else:
                clock.now += step[1]
            played.append({'do': step[0], 'seconds': step[1] if step[0] == 'wait' else None,
                           'at': round(clock.now - 1000.0, 4), 'well': rule.healthy(clock)})
        well.append(played)

    # A call that has no key, and one whose connection cannot be made, are no calls; the second is held against
    # the voice.
    rule._failed_at = None
    without = None
    try:
        rule.GeminiUpstream('', model='m', voice='v', clock=clock)
    except rule.GeminiUnavailable as failed:
        without = str(failed)
    held = rule.healthy(clock)

    def unreachable(_key):
        raise OSError('no way to Gemini')

    rule._connect = unreachable
    try:
        rule.GeminiUpstream('a-key-of-the-test', model='m', voice='v', clock=clock)
    except OSError:
        pass
    return {'what': 'server/lib/oracle_gemini_live.py: GeminiUpstream', 'stories': told, 'well': well,
            'held_for_seconds': rule.FAILURE_HOLD_SECONDS, 'setup_within_seconds': rule.SETUP_TIMEOUT_SECONDS,
            'address': rule.URL, 'without_a_key': without, 'well_without_a_key': held,
            'well_when_unreachable': rule.healthy(clock), 'instructions': rule.INSTRUCTIONS}


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == '--run':
        print(json.dumps(run()))
        return 0
    ran = subprocess.run([sys.executable, '-I', __file__, '--run'], capture_output=True, text=True,
                         env=tool_scratch.child_env({'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}), check=False)
    if ran.returncode != 0:
        print(ran.stderr[-3000:], file=sys.stderr)
        return 1
    said = json.loads(ran.stdout.splitlines()[-1])
    lines = ['{']
    for name in ('what', 'address', 'held_for_seconds', 'setup_within_seconds', 'without_a_key', 'well_without_a_key',
                 'well_when_unreachable', 'instructions', 'well'):
        lines.append(f' {json.dumps(name)}: {json.dumps(said[name], ensure_ascii=True)},')
    lines.append(' "stories": [')
    stories = said['stories']
    for number, story in enumerate(stories):
        head = {name: story[name] for name in ('name', 'model', 'voice', 'languages')}
        lines.append('  {' + json.dumps(head, ensure_ascii=True)[1:-1] + ', "steps": [')
        steps = story['steps']
        lines.extend('   ' + json.dumps(step, ensure_ascii=True) + (',' if n < len(steps) - 1 else '')
                     for n, step in enumerate(steps))
        lines.append('  ]}' + (',' if number < len(stories) - 1 else ''))
    lines.append(' ]')
    lines.append('}')
    OUT.write_text('\n'.join(lines) + '\n', encoding='ascii')
    steps = [step for story in stories for step in story['steps']]
    print(f'{OUT.relative_to(REPO)}: {len(stories)} stories, {len(steps)} steps, '
          f'{sum(len(step["sent"]) for step in steps)} things sent to Gemini, '
          f'{sum(len(step["events"]) for step in steps)} events for the call, '
          f'{sum(1 for step in steps if step["raised"])} steps that failed, '
          f'{sum(1 for step in steps if step["do"] == "resume")} times taken up again, '
          f'{OUT.stat().st_size} bytes')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
