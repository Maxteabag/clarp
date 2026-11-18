#!/usr/bin/env python3
"""Writes down how the Python Host relays a long text to the Oracle's voice in parts.

A voice model takes a bounded piece of text at a time. What an agent answered, a conversation that is read out
or a long result of a tool is cut into numbered parts without losing a sign; each part is handed over with a
head that says which part it is, how much remains and how it is to be read, and only the last says it is the
end. The rules are `server/lib/oracle_relay.py` and `oracle_context.context_chunks`, run here as they are: how
a text is cut, what each part of a relay says for every way it can be handed over, what a relay says of how
far it came, what of an agent's answer is relayed, and the relay of a conversation.

    <the Python Host's interpreter> server-rs/tools/oracle-relay-vectors.py

The same file at every run. Nothing is kept anywhere but in the lists of this program, and nothing is logged.
"""
from __future__ import annotations

import itertools
import json
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parents[2]
OUT = REPO / 'server-rs' / 'crates' / 'clarp-kernel' / 'tests' / 'fixtures' / 'oracle-relay-vectors.json'
sys.path.insert(0, str(REPO / 'server'))

from lib import oracle_context, oracle_relay  # noqa: E402

E_ACUTE, EURO, CJK, FACE = chr(0xe9), chr(0x20ac), chr(0x6f22), chr(0x1f600)
NBSP, EM_SPACE, LINE_SEPARATOR = chr(0xa0), chr(0x2003), chr(0x2028)
PROSE = ('The pump at station four ran dry at 03:12 and tripped its breaker. I reset it, checked the float switch, '
         'which was stuck, and freed it. Open questions: who owns the spare switch, and should the alarm page '
         'someone at night? ') * 6
TEXTS = [
    '', 'short', 'one two three four five six seven eight nine ten', 'nospacesatallinthistextwhichgoesonandonandon' * 4,
    'ends with space ', '  begins with spaces', 'tabs\tand\nnewlines\r\nand more words to cut at', 'a' * 9, ' ' * 9,
    (E_ACUTE + 'l' + E_ACUTE + 'phant ') * 12, (CJK * 7 + ' ') * 6, FACE * 9, (FACE + ' ') * 9, EURO * 20,
    ('word' + NBSP) * 10, ('word' + EM_SPACE) * 10, ('line' + LINE_SEPARATOR) * 10, PROSE,
    'x' + E_ACUTE * 3 + ' y' + FACE * 2 + ' z',
]
BUDGETS = [4, 5, 7, 8, 9, 16, 33, 480, 1100]


def chunkings() -> list:
    out = []
    for text, budget in itertools.product(TEXTS, BUDGETS):
        out.append({'text': text, 'max_bytes': budget, 'chunks': list(oracle_context.context_chunks(text, budget))})
    for budget in (3, 0, -1):
        try:
            list(oracle_context.context_chunks('some text', budget))
            raised = None
        except ValueError as error:
            raised = str(error)
        out.append({'text': 'some text', 'max_bytes': budget, 'raises': raised})
    # Whatever is not a text is cut as the text of it.
    return out


RELAYS = [
    dict(key='rtc-1', label="theo's reply", source='operation rtc-1, status completed', text=PROSE,
         request='What happened at station four?'),
    dict(key='rtc-2', label="theo's reply", source='operation rtc-2, status completed', text='Short and whole.'),
    dict(key='rtc-3', label="mike's reply", source='operation rtc-3, status failed', text='', request='Look.'),
    dict(key='rtc-4', label="theo's reply", source='operation rtc-4, status completed', text=PROSE, verbatim=True),
    dict(key='transcript:Theo', label="Theo's recent conversation", source='newest 3 messages, newest 2 minutes ago',
         text=(E_ACUTE + 'l' + E_ACUTE + 'phant ' + FACE + ' ') * 150),
    dict(key='tool:abc', label='the read_agent_messages result', source='verified tool output, untrusted data',
         text=json.dumps({'messages': ['m' * 300] * 9}), request='q' * 123),
    dict(key='rtc-5', label='l' * 400, source='s' * 300, text=PROSE, request='r' * 123),
    dict(key='rtc-6', label="theo's reply", source='operation rtc-6, status completed',
         text='ends in blanks and a long tail ' + 'tail ' * 60 + '   \n'),
]
TOO_LONG = dict(key='rtc-7', label='l' * 900, source='s' * 500, text=PROSE)


def relays() -> list:
    out = []
    for made in RELAYS:
        relay = oracle_relay.Relay(**made)
        parts = []
        places = sorted({0, 1, relay.total // 2, relay.total - 2, relay.total - 1} & set(range(relay.total)))
        for index in places:
            for served, resumed, pause_after in itertools.product((False, True), repeat=3):
                parts.append({'index': index, 'served': served, 'resumed': resumed, 'pause_after': pause_after,
                              'part': relay.part(index, served=served, resumed=resumed, pause_after=pause_after)})
        # The words for how it is to be read are those of the moment a part is handed over.
        relay.verbatim = not relay.verbatim
        turned = [{'index': index, 'part': relay.part(index)} for index in places]
        relay.verbatim = not relay.verbatim
        status = []
        for sent in sorted({0, 1, relay.total - 1, relay.total}):
            relay.next = sent
            status.append({'next': sent, 'status': relay.status(), 'remaining': relay.remaining})
        out.append({'made': {'request': '', 'verbatim': False, **made}, 'total': relay.total, 'chunks': relay.chunks,
                    'parts': parts, 'turned': turned, 'status': status,
                    'longest': max(len(relay.part(index, served=True, resumed=True, pause_after=True))
                                   for index in range(relay.total))})
    try:
        oracle_relay.Relay(**TOO_LONG)
        raised = None
    except ValueError as error:
        raised = str(error)
    out.append({'made': {'request': '', 'verbatim': False, **TOO_LONG}, 'raises': raised})
    return out


def row(delegation_id='rtc-1', session='theo', status='completed', result_text='', error='', request_text='Look.'):
    return {'delegation_id': delegation_id, 'session': session, 'status': status, 'result_text': result_text,
            'error': error, 'request_text': request_text}


ROWS = [
    row(result_text='Plain words.'),
    row(result_text='Before. <speak>The first.</speak> Between. <speak>The second,\nover two lines.</speak> After.'),
    row(result_text='<speak>With <b>marks</b> and <br/> in it, and a < that is none, and <> too.</speak>'),
    row(result_text='<speak>Never closed.'),
    row(result_text='</speak>Closed first.<speak>'),
    row(result_text='<speak></speak>'),
    row(result_text='<speak>one</speak><speak></speak><speak>three</speak>'),
    row(result_text='<SPEAK>large letters</SPEAK>'),
    row(result_text='<speak>a <speak>nested</speak> one</speak>'),
    row(result_text='No speak, but <b>marks</b> that stay.'),
    row(result_text='<speak>a <> b <b>c</b></speak>'),
    row(result_text='<speak>5 < 6</speak>'),
    row(result_text='<speak>6 > 5 <</speak>'),
    row(status='failed', error='The tool gave up.'),
    row(status='failed', result_text='', error='<speak>An error that speaks.</speak>'),
    row(status='cancelled'),
    row(result_text='Both.', error='And an error.'),
    row(request_text='Earlier words.\n\nCurrent user message, verbatim:\n  Look into the pump.  \n\n<oracle-reference-data>{"x": 1}'),
    row(request_text='Current user message, verbatim:\nfirst\nCurrent user message, verbatim:\nsecond'),
    row(request_text='w' * 120), row(request_text='w' * 121), row(request_text='  ' + 'w' * 119 + '  '),
    row(request_text='<oracle-reference-data>only data'), row(request_text=''),
    row(request_text=(E_ACUTE + FACE) * 70),
    row(session='', result_text='Nobody named.'),
]


def results() -> list:
    out = []
    for one in ROWS:
        for agent in (None, 'Theo'):
            relay = oracle_relay.result_relay(one, agent=agent)
            out.append({'row': one, 'agent': agent, 'text': oracle_relay.result_text(one),
                        'excerpt': oracle_relay.request_excerpt(one),
                        'relay': {'key': relay.key, 'label': relay.label, 'source': relay.source, 'text': relay.text,
                                  'request': relay.request, 'verbatim': relay.verbatim, 'total': relay.total,
                                  'first': relay.part(0)}})
    return out


def said(role, text, timestamp='2026-11-18T08:00:00+00:00', **more):
    return {'id': 'm-1', 'role': role, 'timestamp': timestamp, 'text': text, **more}


TRANSCRIPTS = [
    {'agent': 'Theo', 'session': 'theo', 'messages': [said('user', 'What happened?'), said('assistant', 'The pump ran dry.'),
                                                      said('assistant', 'm' * 50, truncated=True)],
     'newest_message_age': '2 minutes ago', 'truncated': False},
    {'agent': 'Theo', 'messages': [said('user', 'One.')], 'newest_message_age': '', 'truncated': True},
    {'agent': '', 'messages': [], 'newest_message_age': 'just now'},
    {'messages': [said('tool', 'Something else speaks.'), said('assistant', '')]},
    {'agent': 'Mike', 'messages': [said('assistant', PROSE, truncated=False)] * 4, 'newest_message_age': '3 days ago',
     'truncated': True},
]


def transcripts() -> list:
    out = []
    for output in TRANSCRIPTS:
        for verbatim in (False, True):
            relay = oracle_relay.transcript_relay(output, verbatim=verbatim)
            out.append({'output': output, 'verbatim': verbatim,
                        'relay': {'key': relay.key, 'label': relay.label, 'source': relay.source, 'text': relay.text,
                                  'request': relay.request, 'verbatim': relay.verbatim, 'total': relay.total,
                                  'first': relay.part(0), 'last': relay.part(relay.total - 1)}})
    return out


def main() -> int:
    answered = {'what': 'server/lib/oracle_relay.py and oracle_context.context_chunks',
                'part_bytes': oracle_relay.PART_BYTES, 'append_chars': oracle_relay.APPEND_CHARS,
                'auto_parts': oracle_relay.AUTO_PARTS, 'chunkings': chunkings(), 'relays': relays(),
                'results': results(), 'transcripts': transcripts()}
    out = ['{']
    parts = list(answered)
    for at, part in enumerate(parts):
        rows = answered[part]
        end = ',' if at < len(parts) - 1 else ''
        if isinstance(rows, list):
            out.append(f' {json.dumps(part)}: [')
            out.extend('  ' + json.dumps(one, ensure_ascii=True) + (',' if n < len(rows) - 1 else '') for n, one in enumerate(rows))
            out.append(' ]' + end)
        else:
            out.append(f' {json.dumps(part)}: {json.dumps(rows, ensure_ascii=True)}{end}')
    out.append('}')
    OUT.write_text('\n'.join(out) + '\n', encoding='ascii')
    print(f'{OUT.relative_to(REPO)}: {len(answered["chunkings"])} cuttings, {len(answered["relays"])} relays with '
          f'{sum(len(one.get("parts", [])) for one in answered["relays"])} parts, {len(answered["results"])} answers, '
          f'{len(answered["transcripts"])} conversations; {OUT.stat().st_size} bytes')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
