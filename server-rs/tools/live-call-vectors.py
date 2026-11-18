#!/usr/bin/env python3
"""Writes the vectors the Rust Host's live call is tested against: what such a call is told.

A ring may be held as a live voice call (`docs/agent-rings.md`, feature `agent_ring_live`): the
phone talks to a voice model, and everything that model is told is built on the Host when the ring
is admitted, and frozen with it. This runs the Python Host's own rules for that
(`server/lib/agent_ring_live.py`, `agent_live_settings.py`, `agent_ring_live_socket.instructions`)
and writes what they answered to `crates/clarp-kernel/tests/fixtures/live-call-vectors.json`:

- how a conversation is asked to be held (`mode_of`), and what is a request id or an epoch;
- the one form of a value that a digest is taken of (`canonical`, `digest`);
- whether a text is a moment with a zone, as Python reads one (`datetime.fromisoformat`);
- what a producer says of its event (`validate_context`, CONTEXT-v1);
- the tools a profile opts into (`frozen_config`), who is on its team and what tools are offered;
- the frozen snapshot of a ring (`build_snapshot`) from a profile and its pinned files, given here
  as data, and what the voice model is told from it (`instructions`);
- the context of a call the user starts (`user_call_context`) and which agents take one
  (`user_call_profiles`), from folders made here.

    "$(sed -n 1p ~/.local/share/clarp/current/SERVICE_PYTHON)" server-rs/tools/live-call-vectors.py

It runs in a process of its own whose every home, data, config and cache directory is a new empty
directory (the replay harness's own isolation, `tests/replay/host.py`), with no way out of the
machine. No Host is touched or started and no provider is asked anything.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import importlib.util
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[1]
OUT = HERE.parent / 'crates' / 'clarp-kernel' / 'tests' / 'fixtures' / 'live-call-vectors.json'

AGENT = {'agent_id': 'a-theo', 'session': 'theo-97e5', 'persona': 'Theo'}
LINE = 'Camera 2 shows a possible water leak at pump 01. Shall I find an available field worker?'
FACE = '\U0001F600'

# ---- moments ------------------------------------------------------------------------------------

DATES = ['2026-11-18', '20261118', '2026-W47', '2026W47', '2026-W47-3', '2026W473', '2026-1118', '202611-18',
         '2026-11-1', '2026-13-01', '2026-02-29', '2024-02-29', '2026-00-10', '2026-11-00', '2026-11-31',
         '0000-01-01', '0001-01-01', '9999-12-31', '2026-W53', '2020-W53', '2026-W00', '2026-W47-0', '2026-W47-8',
         '9999-W52-7', '9999-W52-5', '0001-W01-1', '2026-w47', '2026-W4', '2026W4', '202-11-18', '2026/11/18',
         '2026-322', '2026322', '+026-11-18', '2026-11-18 ', '2100-02-29', '2000-02-29', '1900-02-29', '2400-02-29',
         '2026-04-31', '2026-06-31', '2026-09-31', '2026-12-31', '2026-01-32']
BETWEEN = ['T', ' ', 't', '_', 'x', '-', '0', ':', '\u00b7', '\u20ac', FACE, '']
HOURS = ['04', '04:08', '0408', '04:08:00', '040800', '04:08:00.1', '04:08:00.123', '04:08:00.123456',
         '04:08:00.1234567', '04:08:00,5', '040800.5', '04:0800', '0408:00', '24:00', '24:00:00', '23:59:59', '23:60',
         '23:59:60', '00:00:00', '4:08', '04:8', '04:08:', '04:', '', '04:08:00.', '04:08:00.x', 'ab', '04.5',
         '04:08.5', '0408.5', '04:08:00.5.5', ' 04:08', '04:08 ', '04:08:00:00', '0', '004:08', '04:08:00,', '04:08.',
         '04.', '04,', '04:08x', '0408x', '04x', '04:08:00x', '04:08:00.5x', '04:08:00.5 ', '04:08:00.123456x',
         '04:08:00.1234567x', '040800x', '0408005', '04080055', '04:08:0', '04:0', '04:08:00:5', '04:08:00:']
ZONES = ['', 'Z', 'z', '+00:00', '-00:00', '+01:00', '-05:30', '+0100', '+01', '+1', '+010', '+01:0', '+01:00:30',
         '+010030', '+01:00:30.5', '+01:00:30.123456', '+01:00:30.1234567', '+24:00', '-24:00', '+23:59',
         '-23:59:59.999999', '+23:59:59.999999', '+99', '+00:99', '+00:00:99', ' +01:00', '+01:00Z', 'Z+01:00', 'ZZ',
         '+', '-', '+aa:bb', '+01:00 ', 'UTC', '+0:00', '-0100', '+01.5', '+01:00,5', '+01:00:', '+0100:30', '+01:0030',
         '-00', '+00', 'Z ', '+-01:00', '--01:00', '+00:00:00.000000', '-00:00:00.000001', '+01:00.', '+01.',
         '+01:00:30.', '+01:00:30:', '+01:00:30:5', '+010030x', '+0100305', '+01003055', '-01', '-0100', 'Zx',
         '+23:59:99', '+23:60', '+01:00x', '+01:00:30x']
ODD_MOMENTS = ['', ' ', '2026', '2026-1', '2026-11', '202611', '2026W4', '2026-W', 'T04:08', '04:08:00+01:00',
               '2026-11-18T', '2026-11-18TZ', '2026-11-18T+01:00', '2026-11-18Z', '2026-11-18+01:00', '20261118Z',
               '2026-W47T04:08Z', '2026W47T04Z', '2026W473T04Z', '2026W4730400Z', '2026W47304Z', '2026-W47-304:08Z',
               '2026-W47-3-04:08', '2026-W47-04:08Z', '2026-W47-0408', '2026-W47-3T04:08+01:00',
               '\u0662\u0660\u0662\u0666-11-18T04:08:00Z', '2026-11-18T\u0660\u0664:08:00Z', '2026-11-18T04:08:00\u00b101:00',
               '2026-11-18T04:08:00+01:00\n', '\n2026-11-18T04:08:00+01:00', '2026-11-18T04:08:00+01:00\x00',
               '2026-11-18T04:08:00.123+01:00', '2026-11-18T04:08:00.123456789+01:00', '2026-11-18T04:08:00.+01:00',
               '2026-11-18T04:08:00-', '2026-11-18T04-08-00', '2026-11-18T04:08:00+1', '2026-11-18T04:08:00+001:00',
               '1e4-11-18T04:08Z', '2026-11-18T04:08:00+01:00:00:00', '2026-11-18T04:08:00+01:00.5',
               '2026-11-18T04:08:00+01.30', '2026-11-18T04:08+0130', '2026-11-18T04+0130', '2026-11-18T0408+0130',
               '2026-11-18T040800+013000', '2026-11-18T04:08:00+01:30:00', '2026-11-18T04:08:00+013000.5']


def moments() -> list[str]:
    """Each kind of every part with the usual ones of the rest, every hour with every zone, every date with every
    sign between, and a spread of the rest that is the same at every run."""
    usual = ('2026-11-18', 'T', '04:08:00', '+01:00')
    texts = list(ODD_MOMENTS)
    texts += [date + usual[1] + usual[2] + usual[3] for date in DATES]
    texts += [date for date in DATES]
    texts += [usual[0] + between + usual[2] + usual[3] for between in BETWEEN]
    texts += [usual[0] + usual[1] + hours + zone for hours in HOURS for zone in ZONES]
    texts += [date + between + usual[2] + zone for date in DATES for between in BETWEEN for zone in ('', 'Z')]
    state = 20261118
    for _ in range(700):
        picks = []
        for part in (DATES, BETWEEN, HOURS, ZONES):
            state = (state * 6364136223846793005 + 1442695040888963407) % (1 << 64)
            picks.append(part[(state >> 33) % len(part)])
        texts.append(''.join(picks))
    return list(dict.fromkeys(texts))


# ---- how a call is asked to be held, ids, the one form of a value --------------------------------

MODES = [None, 'classic', 'agent_live_v1', 'live', '', 'Classic', 1, True, 1.5, ['classic'], {'mode': 'classic'}]
REQUEST_IDS = ['r', 'request-1', 'x' * 128, 'x' * 129, '', ' ', '\u00e9' * 64, '\u00e9' * 65, FACE * 32, FACE * 33, None, 5,
               True, ['r'], {'id': 'r'}, 1.5]
EPOCHS = [1, 2, 10 ** 12, 0, -1, True, False, 1.0, '1', None, [1]]
CANONICAL = [
    None, True, False, 0, -7, 2 ** 63 - 1, -2 ** 63, 2 ** 64 - 1, 1.5, -0.25, 1e22, 1e-7, 0.1, 1.0, 100000000000000000.0, '', 'text',
    'bl\u00e5b\u00e6r \u00f8', FACE, 'a"b\\c/d', 'line\nfeed\ttab\r\x08\x0c', '\x00\x01\x1f\x7f', '\u2028\u2029', [],
    {}, [1, [2, [3, []]], {}], {'b': 1, 'a': 2, 'B': 3, 'aa': 4, '': 5, '\u00e5': 6, 'z': {'y': [True, None], 'x': ''}},
    {'10': 1, '9': 2, 'a b': 3, 'a': 4, 'A': 5, FACE: 6},
]

# ---- the event a producer tells of (CONTEXT-v1) ---------------------------------------------------

EVENT = {'id': 'evt-0042', 'time': '2026-11-18T04:08:00+01:00', 'camera_id': 'cam-2', 'camera_label': 'Camera 2',
         'when_text': 'just now', 'what_text': 'Water on the floor below pump 01.',
         'log': ['04:07 motion at pump 01', '04:08 water seen']}
CONTEXT = {'version': 1, 'profile_id': 'pump-guard', 'story': 'pump', 'source': 'leak-watch', 'event': EVENT,
           'opening_text': LINE}


def told(**change):
    """The usual context with top keys changed (`None` drops one) and, under `event`, the event's."""
    context = copy.deepcopy(CONTEXT)
    for key, value in change.items():
        if key == 'event' and isinstance(value, dict) and value.pop('_within', False):
            for name, inner in value.items():
                if inner is DROP:
                    context['event'].pop(name, None)
                else:
                    context['event'][name] = inner
        elif value is DROP:
            context.pop(key, None)
        else:
            context[key] = value
    return context


DROP = object()


def event(**change):
    return {'_within': True, **change}


def contexts() -> list[tuple[str, object, str]]:
    cases = [('the usual event', told(), LINE)]
    cases += [(f'what is no object: {json.dumps(value)}', value, LINE) for value in (None, [], 'text', 3, True)]
    cases += [(f'without {key}', told(**{key: DROP}), LINE) for key in CONTEXT]
    cases += [('with a key nobody knows', told(note='x'), LINE),
              ('with a key nobody knows that is too large', told(note='x' * 40000), LINE),
              ('too large by what it tells', told(event=event(what_text='w' * 8192, meta={'seen': 's' * 8192, 'room': 'r' * 512,
                                                                                            'item': 'i' * 512},
                                                              log=['l' * 512] * 30)), LINE),
              ('as large as may be', told(event=event(what_text='w' * 8192, meta={'seen': 's' * 8192},
                                                      log=['l' * 512] * 29)), LINE)]
    cases += [(f'version {json.dumps(value)}', told(version=value), LINE) for value in (1.0, True, 2, 0, '1', None, [1])]
    cases += [(f'profile {json.dumps(value)[:24]}', told(profile_id=value), LINE)
              for value in ('a', 'a.b_c-d', '0' * 64, '0' * 65, '', 'A', 'Pump', '-a', '.a', '_a', 'a b', 'a/b', 'a\n',
                            '\u00e9', 5, None, ['a'])]
    cases += [(f'story {json.dumps(value)}', told(story=value), LINE)
              for value in ('bottle', 'pump', 'user_call', 'Pump', '', None, 1, ['pump'], {'pump': 1})]
    cases += [(f'source {json.dumps(value)}', told(source=value), LINE)
              for value in ('leak-watch', 'rk-demo-on-leak-event', 'user-initiated', 'Leak-watch', '', None, 1, ['leak-watch'])]
    cases += [(f'an event that is {json.dumps(value)}', told(event=value), LINE) for value in (None, [], 'e', 1)]
    cases += [('an event with a key nobody knows', told(event=event(note='x')), LINE)]
    cases += [(f'an event without {key}', told(event=event(**{key: DROP})), LINE) for key in EVENT]
    limits = {'id': 128, 'time': 64, 'camera_id': 128, 'camera_label': 256, 'when_text': 64, 'what_text': 8192}
    for key, limit in limits.items():
        cases += [(f'{key} of {limit} signs', told(event=event(**{key: 'x' * limit})), LINE),
                  (f'{key} of {limit + 1} signs', told(event=event(**{key: 'x' * (limit + 1)})), LINE),
                  (f'{key} empty', told(event=event(**{key: ''})), LINE),
                  (f'{key} blank', told(event=event(**{key: ' \t\n'})), LINE),
                  (f'{key} a number', told(event=event(**{key: 7})), LINE),
                  (f'{key} nothing', told(event=event(**{key: None})), LINE)]
    cases += [('an id counted in signs, not bytes', told(event=event(id=FACE * 128)), LINE),
              ('an id one sign too long, of large signs', told(event=event(id=FACE * 129)), LINE),
              ('an id of blanks of other alphabets', told(event=event(id='\u00a0\u2003\u3000\x1c\x85')), LINE),
              ('an id of a sign that only looks blank', told(event=event(id='\u200b')), LINE),
              ('too long comes before blank', told(event=event(id='x' * 129, camera_id=' ')), LINE),
              ('blank comes before too long', told(event=event(id=' ', camera_id='x' * 129)), LINE),
              ('no text comes before too long', told(event=event(id=5, time='x' * 65)), LINE)]
    cases += [(f'the moment {moment!r}'.replace('\\', '/'), told(event=event(time=moment)), LINE)
              for moment in ('2026-11-18T04:08:00Z', '2026-11-18 04:08:00+01:00', '2026-11-18T04:08:00', '2026-11-18',
                             '20261118T040800+0100', '2026-W47-3T04:08+00:00', 'yesterday', '2026-11-18T04:08:00+24:00',
                             '2026-11-18T25:08:00+01:00', '2026-02-30T04:08:00+01:00', '2026-11-18T04:08:00.5-00:00')]
    cases += [('a log that is no list', told(event=event(log='04:07 motion')), LINE),
              ('a log that is an object', told(event=event(log={'0': 'x'})), LINE),
              ('a log that is nothing', told(event=event(log=None)), LINE),
              ('an empty log', told(event=event(log=[])), LINE),
              ('a log of 64 lines', told(event=event(log=['line'] * 64)), LINE),
              ('a log of 65 lines', told(event=event(log=['line'] * 65)), LINE),
              ('a log line of 512 signs', told(event=event(log=['x' * 512])), LINE),
              ('a log line of 513 signs', told(event=event(log=['ok', 'x' * 513])), LINE),
              ('a log line that is a number', told(event=event(log=['ok', 5])), LINE),
              ('a log line too long before one that is no text', told(event=event(log=['x' * 513, 5])), LINE),
              ('a log line that is no text before one too long', told(event=event(log=[5, 'x' * 513])), LINE),
              ('65 lines of which one is no text', told(event=event(log=[5] * 65)), LINE)]
    cases += [('with all that may be known beside', told(event=event(meta={'room': 'Pump room', 'item': 'pump 01',
                                                                             'seen': 'water'})), LINE),
              ('with nothing known beside', told(event=event(meta={})), LINE),
              ('beside: a list', told(event=event(meta=[])), LINE),
              ('beside: nothing', told(event=event(meta=None)), LINE),
              ('beside: a key nobody knows', told(event=event(meta={'floor': '2'})), LINE),
              ('beside: a number', told(event=event(meta={'room': 2})), LINE),
              ('beside: a room of 512 signs', told(event=event(meta={'room': 'r' * 512})), LINE),
              ('beside: a room of 513 signs', told(event=event(meta={'room': 'r' * 513})), LINE),
              ('beside: an item of 513 signs', told(event=event(meta={'item': 'i' * 513})), LINE),
              ('beside: what was seen, 8193 signs', told(event=event(meta={'seen': 's' * 8193})), LINE),
              ('beside: empty texts', told(event=event(meta={'room': '', 'item': '', 'seen': ''})), LINE)]
    long_line = ('word ' * 120).strip()
    cases += [('an opening that is no text', told(opening_text=5), LINE),
              ('an opening that is nothing', told(opening_text=None), LINE),
              ('an empty opening', told(opening_text=''), ''),
              ('a blank opening', told(opening_text='  \n '), ''),
              ('another opening than the ring has', told(opening_text='Hello.'), LINE),
              ('the opening in another case', told(opening_text=LINE.upper()), LINE),
              ('the opening with its blanks at will', told(opening_text='  ' + LINE.replace(' ', ' \n\t ') + '\u2003'), LINE),
              ('an opening of 600 signs', told(opening_text=long_line + 'x'), long_line + 'x'),
              ('an opening of 601 signs', told(opening_text=long_line + 'xx'), long_line + 'xx'),
              ('an opening whose blanks make it 601 signs', told(opening_text=long_line + ' x'), long_line + ' x'),
              ('an opening line with a blank at its end', told(), LINE + ' ')]
    return cases


# ---- the tools a profile opts into ---------------------------------------------------------------

TEAM = [{'session': 'drammen-jonas', 'name': 'Jonas', 'role': 'backend'}, {'session': 'drammen-noel', 'name': 'Noel'}]
TOOLS = {'enabled': True, 'stories': ['user_call'], 'from': 'theo-97e5', 'agents': TEAM, 'delegate': {'kind': 'prompt'}}
DIRECTOR = {'kind': 'http', 'url': 'http://127.0.0.1:7790/regi/sandra/deleger',
            'reconcile_url': 'http://127.0.0.1:7790/regi/sandra/intent'}
CALLBACK = {'kind': 'http', 'url': 'http://127.0.0.1:7790/regi/sandra/tilbakering'}


def opted(**change):
    block = copy.deepcopy(TOOLS)
    for key, value in change.items():
        if value is DROP:
            block.pop(key, None)
        else:
            block[key] = value
    return {'profile_id': 'pump-guard', 'agent_session': 'theo-97e5', 'live_tools': block}


def tool_cases() -> list[tuple[str, object, str]]:
    story = 'user_call'
    cases = [('the usual team', opted(), story), ('no block', {'profile_id': 'p', 'agent_session': 'theo-97e5'}, story),
             ('a block that is a list', {'agent_session': 'theo-97e5', 'live_tools': [TOOLS]}, story),
             ('not enabled', opted(enabled=False), story), ('enabled by a number', opted(enabled=1), story),
             ('enabled by a word', opted(enabled='true'), story), ('enabled left out', opted(enabled=DROP), story),
             ('stories left out, a call of the user', opted(stories=DROP), story),
             ('stories left out, another story', opted(stories=DROP), 'pump'),
             ('a story that is not listed', opted(), 'pump'), ('a story that is listed', opted(stories=['pump', 'bottle']), 'pump'),
             ('stories that are no list', opted(stories='user_call'), story), ('no stories', opted(stories=[]), story)]
    cases += [(f'agents: {name}', opted(agents=value), story) for name, value in (
        ('none', []), ('left out', DROP), ('nothing', None), ('an object', {'session': 'a'}),
        ('a number', 7), ('true', True),
        ('twelve', [{'session': f'm-{n}'} for n in range(12)]), ('thirteen', [{'session': f'm-{n}'} for n in range(13)]),
        ('one that is a text', ['drammen-jonas']), ('one without a session', [{'name': 'Jonas'}]),
        ('a session that is a number', [{'session': 5}]), ('a session with a blank', [{'session': 'a b'}]),
        ('a session that starts with a dot', [{'session': '.a'}]), ('a session of 64 signs', [{'session': 'a' * 64}]),
        ('a session of 65 signs', [{'session': 'a' * 65}]), ('a session with a line end', [{'session': 'a\n'}]),
        ('the same one twice', [{'session': 'a'}, {'session': 'a'}]), ('the agent itself', [{'session': 'theo-97e5'}]),
        ('names and roles cut', [{'session': 'a', 'name': 'N' * 41, 'role': 'r' * 201}]),
        ('a name and a role that are numbers', [{'session': 'a', 'name': 7, 'role': 0}]),
        ('an empty name', [{'session': 'a', 'name': '', 'role': None}]),
        ('a bad one after a good one', [{'session': 'a'}, {'session': ''}]))]
    cases += [('from: another agent', opted(**{'from': 'mike-86db'}), story), ('from: left out', opted(**{'from': DROP}), story),
              ('from: nothing', opted(**{'from': None}), story),
              ('a profile without its agent', {'live_tools': copy.deepcopy(TOOLS)}, story),
              ('a profile whose agent is a number', {'agent_session': 5, 'live_tools': {**copy.deepcopy(TOOLS), 'from': 5}}, story),
              ('a profile whose agent has a blank', {'agent_session': 'a b', 'live_tools': {**copy.deepcopy(TOOLS), 'from': 'a b'}},
               story)]
    cases += [(f'delegate: {name}', opted(delegate=value), story) for name, value in (
        ('left out', DROP), ('nothing', None), ('a word', 'prompt'), ('an unknown kind', {'kind': 'mail'}),
        ('no kind', {}), ('a prompt with more', {'kind': 'prompt', 'url': 'http://127.0.0.1:1/x'}),
        ('a director', DIRECTOR), ('a director without its second address', {'kind': 'http', 'url': DIRECTOR['url']}),
        ('a director with an empty second address', {**DIRECTOR, 'reconcile_url': ''}),
        ('a director with a second address elsewhere', {**DIRECTOR, 'reconcile_url': 'http://10.0.0.1/x'}),
        ('a director with a second address that is nothing', {**DIRECTOR, 'reconcile_url': None}),
        ('a director with a fallback', {**DIRECTOR, 'fallback': 'prompt'}),
        ('a director with no fallback, said', {**DIRECTOR, 'fallback': 'none'}),
        ('a director without an address', {'kind': 'http'}))]
    urls = ['http://127.0.0.1:7790/a', 'http://127.0.0.1/a', 'http://[::1]:7790/a', 'http://localhost:7790/a',
            'https://127.0.0.1:7790/a', 'http://127.0.0.1:7790/a?x=1', 'http://127.0.0.1:7790/a#x', 'http://u@127.0.0.1:7790/a',
            'http://u:p@127.0.0.1:7790/a', 'http://127.0.0.1:0/a', 'http://127.0.0.1:65535/a', 'http://127.0.0.1:65536/a',
            'http://127.0.0.1:x/a', 'http://127.0.0.2/a', 'http://127.1/a', 'http://0x7f.0.0.1/a', 'HTTP://127.0.0.1/a',
            'http://127.0.0.1', 'http://127.0.0.1:/a', 'http://[::1]/a', 'http://::1/a', 'http://[::1/a', 'ftp://127.0.0.1/a',
            '//127.0.0.1/a', '127.0.0.1/a', '', 'http://127.0.0.1/' + 'a' * 283, 'http://127.0.0.1/' + 'a' * 284, 5, None,
            ' http://127.0.0.1/a', 'http://127.0.0.1/a ', 'http://127.0.0.1/a?', 'http://127.0.0.1/a#', 'http://127.0.0.1./a',
            'http://[0:0:0:0:0:0:0:1]/a', 'http://127.0.0.1:07790/a', 'http://127.0.0.1:+80/a', 'http://127.0.0.1: 80/a',
            'http://127.0.0.1:\u0661/a', 'http://@127.0.0.1/a', 'http://:@127.0.0.1/a', 'http://127.0.0.1\\a', 'http:/127.0.0.1/a',
            'http://[::1]:99999/a', 'http://[::1]x/a', 'http://1.2.3.4@127.0.0.1/a', 'http://[127.0.0.1]/a',
            'http://[127.0.0.1]:80/a', 'http://127.0.0.1@/a', 'http://127.0.0.1:80@x/a', 'http://x@[::1]/a', 'http://[::1]@/a',
            'http://[::1]:80@x/a', 'http://[::1]', 'http://[::1]:/a', 'http://[::1]:080/a', 'http://[::1]:0/a', 'http://[]/a',
            'http://[::1]:x/a', 'http://[::0001]/a', 'http://[::1%25lo]/a', 'http://127.0.0.1:65535', 'http://[v1.x]/a', 'http://127.0.0.1/\u00e5', 'http://127.0.0.1/a b']
    cases += [(f'a director at {json.dumps(url)[:60]}', opted(delegate={'kind': 'http', 'url': url}), story) for url in urls]
    cases += [(f'callback: {name}', opted(callback=value), story) for name, value in (
        ('left out', DROP), ('none', {'kind': 'none'}), ('a director', CALLBACK), ('an unknown kind', {'kind': 'prompt'}),
        ('a word', 'none'), ('a director elsewhere', {'kind': 'http', 'url': 'http://example.org/x'}),
        ('a director without an address', {'kind': 'http'}), ('none with more', {'kind': 'none', 'url': CALLBACK['url']}))]
    cases += [(f'minutes: {name}', opted(callback=CALLBACK, callback_minutes=value), story) for name, value in (
        ('left out', DROP), ('a number', 5), ('its own', {'default': 10, 'min': 2, 'max': 20}),
        ('a least above a most', {'default': 10, 'min': 20, 'max': 5}), ('a default above the most', {'default': 99, 'max': 20}),
        ('a default below the least', {'default': 1, 'min': 3}), ('too much of everything', {'default': 500, 'min': 0, 'max': 121}),
        ('fractions and words', {'default': 5.0, 'min': '2', 'max': True}), ('the widest', {'default': 120, 'min': 120, 'max': 120}))]
    cases += [(f'a call back by itself: {name}', opted(callback=CALLBACK, callback_minutes={'default': 7, 'min': 2, 'max': 9},
                                                       callback_on_delegation=value), story) for name, value in (
        ('false', False), ('nothing', None), ('true', True), ('an empty object', {}), ('its minutes', {'minutes': 4}),
        ('minutes above the most', {'minutes': 60}), ('minutes below the least', {'minutes': 1}),
        ('minutes that are a word', {'minutes': 'five'}), ('zero', 0), ('a word', 'yes'), ('a list', [3]))]
    cases += [('a call back by itself without a director for it', opted(callback_on_delegation=True), story),
              ('no call back by itself without a director for it', opted(callback_on_delegation=False), story)]
    cases += [('the agent on its own team, with a contact of its own',
               opted(agents=[*TEAM, {'session': 'theo-97e5'}], contact={'session': 'sandra-own'}), story),
              ('a team member twice, with a contact of its own',
               opted(agents=[*TEAM, {'session': 'drammen-noel'}], contact={'session': 'sandra-own'}), story)]
    cases += [(f'contact: {name}', opted(contact=value), story) for name, value in (
        ('left out', DROP), ('a word', 'sandra'), ('its own agent', {'session': 'sandra-own', 'name': 'Sandra herself'}),
        ('a team member', {'session': 'drammen-jonas'}), ('a session that is a number', {'session': 5}),
        ('a session with a blank', {'session': 'a b'}), ('only a name', {'name': 'N' * 61}), ('an empty name', {'name': ''}),
        ('the agent itself, said', {'session': 'theo-97e5', 'name': 7}))]
    cases += [(f'what is kept: {name}', opted(**value), story) for name, value in (
        ('the words of the call', {'save_transcript': True}), ('the words, by a number', {'save_transcript': 1}),
        ('for 14 days', {'save_transcript': True, 'transcript_keep_days': 14}), ('for a day', {'transcript_keep_days': 1}),
        ('for a year', {'transcript_keep_days': 365}), ('for more than a year', {'transcript_keep_days': 366}),
        ('for no day', {'transcript_keep_days': 0}), ('for a fraction', {'transcript_keep_days': 14.0}),
        ('for true', {'transcript_keep_days': True}))]
    cases.append(('everything at once', opted(delegate=DIRECTOR, callback=CALLBACK, callback_on_delegation={'minutes': 3},
                                              contact={'session': 'sandra-own', 'name': 'Sandra herself'}, save_transcript=True,
                                              transcript_keep_days=14, callback_minutes={'default': 5, 'min': 1, 'max': 30}), story))
    return cases


# ---- profiles, their files, and the snapshot of a ring ---------------------------------------------

PERSONA = 'You are Theo, who guards the pumps.\nYou speak Norwegian bokm\u00e5l.\n'
PROCEDURE = '# Procedure, revision 3\n1. Say what the camera saw.\n2. Ask whether to send a field worker.\n'
USER_CALL = '# When the user calls\nGreet, then listen.\n'


def sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def as_bytes(spec: dict) -> bytes:
    if 'text' in spec:
        return spec['text'].encode('utf-8')
    if 'b64' in spec:
        return base64.b64decode(spec['b64'])
    unit, count = spec['repeat']
    return (unit * count).encode('utf-8')


FILES = {'PERSONA.md': {'text': PERSONA}, 'PROCEDURE.md': {'text': PROCEDURE}, 'USER-CALL.md': {'text': USER_CALL}}


def pins(files: dict, **keys) -> dict:
    return {key: {'file': name, 'sha256': sha(as_bytes(files[name]))} for key, name in keys.items()}


def profile(**change) -> dict:
    value = {'profile_id': 'pump-guard', 'version': '3', 'agent_session': 'theo-97e5', 'display_name': 'Theo the guard',
             'source': {'by': 'operator', 'date': '2026-11-01', 'note': 'bl\u00e5 pumpe'},
             'files': pins(FILES, persona='PERSONA.md', procedure='PROCEDURE.md', user_call='USER-CALL.md'),
             'stories': {'pump': ['persona', 'procedure'], 'bottle': ['persona'], 'user_call': ['persona', 'user_call']},
             'user_call_default': 'live', 'user_call_opening': '  Hei,   det er Theo.\n Hva kan jeg hjelpe med? '}
    for key, item in change.items():
        if item is DROP:
            value.pop(key, None)
        else:
            value[key] = item
    return value


def snapshot_cases() -> list[dict]:
    """`profile`: what `profile.json` holds (a value, a text as it is, or nothing: no such file)."""
    def case(name, held=None, files=None, context=None, agent=None, key=True, call=None):
        return {'name': name, 'profile': profile() if held is None else held, 'files': FILES if files is None else files,
                'context': told() if context is None else context, 'agent': AGENT if agent is None else agent, 'key': key,
                'call': call}

    big = {**FILES, 'BIG.md': {'repeat': ['b', 65536]}}
    fits = {**FILES, 'FITS.md': {'repeat': ['f', 62000]}}
    odd = {**FILES, 'ODD.bin': {'b64': base64.b64encode(b'\xff\xfe pump \xc3').decode('ascii')},
           'EMPTY.md': {'text': ''}, 'bom.md': {'b64': base64.b64encode(b'\xef\xbb\xbfwith a mark\r\n').decode('ascii')}}
    cases = [
        case('the pump'), case('the bottle', context=told(story='bottle')),
        case('with all that is known beside', context=told(event=event(meta={'room': 'Pump room', 'item': '', 'seen': 'water'},
                                                                      camera_label=''))),
        case('a Host without a key for the voice', key=False),
        case('a Host without a key and without the profile', held=DROP, key=False),
        case('no such profile', held=DROP), case('a profile that is no JSON', held='{"profile_id": "pump-guard",'),
        case('a profile that is a list', held=[profile()]), case('a profile that is empty', held=''),
        case('a profile of another name', held=profile(profile_id='pump-guard-2')),
        case('a profile of another agent', held=profile(agent_session='mike-86db')),
        case('a profile of no agent', held=profile(agent_session=DROP)),
        case('a profile of an empty agent', held=profile(agent_session=''), agent={**AGENT, 'session': ''}),
        case('an agent without a session', agent={'agent_id': 'a-theo', 'persona': 'Theo'}),
        case('stories that are a list', held=profile(stories=[['persona']])),
        case('files that are a list', held=profile(files=[])), case('no stories', held=profile(stories=DROP)),
        case('a story the profile does not have', held=profile(stories={'bottle': ['persona']})),
        case('a story of no files', held=profile(stories={'pump': []})),
        case('a story that is a word', held=profile(stories={'pump': 'persona'})),
        case('a story with a file that is a number', held=profile(stories={'pump': ['persona', 5]})),
        case('a story with a file nobody pinned', held=profile(stories={'pump': ['persona', 'handbook']})),
        case('a story with the same file twice', held=profile(stories={'pump': ['persona', 'procedure', 'persona']})),
    ]
    good = profile()['files']
    for name, pin in (('that is a word', 'PERSONA.md'), ('without a file', {'sha256': good['persona']['sha256']}),
                      ('without a sum', {'file': 'PERSONA.md'}), ('with a file that is a number', {'file': 5, 'sha256': 'a' * 64}),
                      ('with a sum that is a number', {'file': 'PERSONA.md', 'sha256': 5}),
                      ('with a file in a folder', {'file': 'sub/PERSONA.md', 'sha256': good['persona']['sha256']}),
                      ('with a file above', {'file': '../PERSONA.md', 'sha256': good['persona']['sha256']}),
                      ('with a hidden file', {'file': '.PERSONA.md', 'sha256': good['persona']['sha256']}),
                      ('with an empty file name', {'file': '', 'sha256': good['persona']['sha256']}),
                      ('with a file name of 129 signs', {'file': 'a' * 129, 'sha256': good['persona']['sha256']}),
                      ('with a sum written large', {'file': 'PERSONA.md', 'sha256': good['persona']['sha256'].upper()}),
                      ('with a sum of 63 signs', {'file': 'PERSONA.md', 'sha256': good['persona']['sha256'][:63]}),
                      ('with a sum of 65 signs', {'file': 'PERSONA.md', 'sha256': good['persona']['sha256'] + '0'}),
                      ('with another sum', {'file': 'PERSONA.md', 'sha256': good['procedure']['sha256']}),
                      ('with a file that is not there', {'file': 'GONE.md', 'sha256': good['persona']['sha256']}),
                      ('with more said of it', {'file': 'PERSONA.md', 'sha256': good['persona']['sha256'], 'note': 'x'})):
        cases.append(case(f'a pin {name}', held=profile(files={**good, 'persona': pin})))
    for name in ('.hidden.md', 'a' * 129, 'a b.md', 'sub/PERSONA.md', '-dash.md', 'a' * 128, 'a\n', 'PERSONA.md\n',
                 'A.b_c-d', '_under.md', 'caf\u00e9.md'):
        there = {**FILES, name: {'text': PERSONA}}
        cases.append(case(f'a pinned file that is there under the name {json.dumps(name)[:40]}',
                          held=profile(files={**good, 'persona': {'file': name, 'sha256': sha(as_bytes(there[name]))}}),
                          files=there))
    cases += [
        case('a file that is no text', held=profile(files=pins(odd, persona='ODD.bin'), stories={'pump': ['persona']}), files=odd),
        case('an empty file and one with a mark at its head',
             held=profile(files=pins(odd, a='EMPTY.md', b='bom.md'), stories={'pump': ['b', 'a']}), files=odd),
        case('a snapshot that is too large', held=profile(files=pins(big, persona='BIG.md'), stories={'pump': ['persona']}),
             files=big),
        case('a snapshot that just fits', held=profile(files=pins(fits, persona='FITS.md'), stories={'pump': ['persona']}),
             files=fits),
        case('a version that is a number', held=profile(version=3)), case('a version of nought', held=profile(version=0)),
        case('a version left out', held=profile(version=DROP)), case('a version that is true', held=profile(version=True)),
        case('a version that is a fraction', held=profile(version=1.5)),
        case('no name of its own', held=profile(display_name=DROP)),
        case('no name at all', held=profile(display_name=''), agent={'agent_id': 'a-theo', 'session': 'theo-97e5'}),
        case('a name that is a number', held=profile(display_name=7)),
        case('a source that is a word', held=profile(source='operator')), case('no source', held=profile(source=DROP)),
        case('a source with numbers', held=profile(source={'revision': 12, 'ratio': 0.5, 'large': 1e22, 'small': 1e-7,
                                                           'checked': True, 'by': None, 'parts': [1, 2.0]})),
        case('an agent whose id is a number', agent={'agent_id': 7, 'session': 'theo-97e5', 'persona': 'Theo'}),
        case('tools for a call of the user only', held=profile(live_tools=copy.deepcopy(TOOLS))),
        case('tools for the pump too', held=profile(live_tools={**copy.deepcopy(TOOLS), 'stories': ['pump', 'user_call'],
                                                            'delegate': DIRECTOR, 'callback': CALLBACK})),
        case('tools that are malformed', held=profile(live_tools={**copy.deepcopy(TOOLS), 'stories': ['pump'], 'agents': []})),
        case('the user calls', call='request-1'),
        case('the user calls an agent with tools', held=profile(live_tools=copy.deepcopy(TOOLS)), call='request-2'),
        case('the user calls an agent with a director',
             held=profile(live_tools={**copy.deepcopy(TOOLS), 'delegate': DIRECTOR, 'callback': CALLBACK,
                                      'callback_on_delegation': True}), call='request-3'),
        case('the user calls an agent whose profile has no story for that', held=profile(stories={'pump': ['persona']}),
             call='request-4'),
    ]
    return cases


def user_call_cases() -> list[dict]:
    def case(name, held=None, agent=None, request_id='request-1'):
        return {'name': name, 'profile': profile() if held is None else held, 'agent': AGENT if agent is None else agent,
                'request_id': request_id}

    cases = [case('the usual call'), case('no such profile', held=DROP),
             case('a profile of another agent', held=profile(agent_session='mike-86db')),
             case('an agent that does not take calls so', held=profile(user_call_default=DROP)),
             case('an agent that takes them the usual way', held=profile(user_call_default='classic')),
             case('taken so, written large', held=profile(user_call_default='Live')),
             case('no opening', held=profile(user_call_opening=DROP)), case('an empty opening', held=profile(user_call_opening='')),
             case('a blank opening', held=profile(user_call_opening=' \n\t')),
             case('an opening that is a number', held=profile(user_call_opening=5)),
             case('an opening of 600 signs', held=profile(user_call_opening='o' * 600)),
             case('an opening of 601 signs', held=profile(user_call_opening='o' * 601)),
             case('an opening of 601 signs with its blanks', held=profile(user_call_opening=' ' + 'o' * 600)),
             case('an opening of other alphabets', held=profile(user_call_opening='Hei\u2003p\u00e5\u00a0deg ' + FACE))]
    cases += [case(f'a request id that is {json.dumps(value)[:20]}', request_id=value)
              for value in ('', None, 5, 'x' * 128, 'x' * 129, ['r'], '\u00e9' * 64, '\u00e9' * 65)]
    cases.append(case('a bad request id and no profile', held=DROP, request_id=''))
    return cases


def folder(name, held=None, kind='folder'):
    """A folder of the profiles: `kind` is folder, file or link (a link to a folder beside)."""
    return {'name': name, 'kind': kind, 'profile': held}


def binding(profile_id, session, live=True, **more):
    value = {'profile_id': profile_id, 'agent_session': session, **more}
    if live:
        value['user_call_default'] = 'live'
    return value


def registry_cases() -> list[dict]:
    theo, mike = binding('pump-guard', 'theo-97e5'), binding('desk', 'mike-86db', live=False)
    def case(name, folders, known=None, there=True):
        return {'name': name, 'known': known, 'there': there, 'folders': folders}

    cases = [
        case('no folder of profiles, nothing known', [], there=False),
        case('no folder of profiles, nothing known, said so', [], known='[]', there=False),
        case('no folder of profiles, one known', [], known='["theo-97e5"]', there=False),
        case('an empty folder of profiles', []), case('an empty folder of profiles, one known', [], known='["theo-97e5"]'),
        case('one agent takes calls so', [folder('pump-guard', theo)]),
        case('one takes calls so and one does not', [folder('desk', mike), folder('pump-guard', theo)]),
        case('one that is known and says no more that it does', [folder('pump-guard', binding('pump-guard', 'theo-97e5', live=False))],
             known='["theo-97e5"]'),
        case('one that is known and says something else', [folder('pump-guard', binding('pump-guard', 'theo-97e5', live=False,
                                                                                         user_call_default='classic'))],
             known='["mike-86db", "theo-97e5"]'),
        case('known ones kept in order', [folder('pump-guard', theo)], known='["zoe", "anna", "theo-97e5"]'),
        case('what is known is written anew when it is written otherwise', [folder('pump-guard', theo)], known='["theo-97e5" ]'),
        case('two profiles for one agent', [folder('pump-guard', theo), folder('pump-guard-2', binding('pump-guard-2', 'theo-97e5'))]),
        case('one profile takes calls and another of the same agent does not',
             [folder('a-first', binding('a-first', 'theo-97e5', live=False)), folder('pump-guard', theo)], known='["theo-97e5"]'),
        case('a folder with a name no profile has', [folder('Pump Guard', binding('Pump Guard', 'theo-97e5')), folder('desk', mike)],
             known='["mike-86db"]'),
        case('a file among the folders', [folder('pump-guard', theo), folder('notes', kind='file')]),
        case('a link among the folders', [folder('pump-guard', theo), folder('linked', binding('linked', 'zoe'), kind='link')]),
        case('a folder without a profile', [folder('pump-guard', theo), folder('empty')]),
        case('a profile that is no JSON', [folder('pump-guard', theo), folder('broken', '{"profile_id":')]),
        case('a profile that is a list', [folder('listed', [binding('listed', 'zoe')])]),
        case('a profile of another name', [folder('renamed', binding('other', 'zoe'))]),
        case('a profile without its agent', [folder('nobody', {'profile_id': 'nobody', 'user_call_default': 'live'})]),
        case('a profile whose agent is empty', [folder('nobody', binding('nobody', ''))]),
        case('a profile whose agent is a number', [folder('nobody', binding('nobody', 5))]),
        case('a profile that is too large', [folder('large', binding('large', 'zoe', note='n' * 65536))]),
        case('a profile as large as may be', [folder('large', binding('large', 'zoe', note='n' * 65000))]),
        case('sixty-four profiles', [folder(f'p-{n:02}', binding(f'p-{n:02}', f's-{n:02}', live=n % 2 == 0)) for n in range(64)]),
        case('sixty-five profiles', [folder(f'p-{n:02}', binding(f'p-{n:02}', f's-{n:02}', live=n % 2 == 0)) for n in range(65)]),
        case('a profile with a mark at its head', [folder('pump-guard', '\ufeff' + json.dumps(theo))]),
        case('an unreadable one hides nothing that is known', [folder('broken', '{'), folder('desk', mike)],
             known='["mike-86db", "theo-97e5"]'),
    ]
    cases += [case(f'what is known is kept as {raw!r}'.replace('\\', '/'), [folder('pump-guard', theo)], known=raw)
              for raw in ('not json', '[1]', '[""]', '{"a": 1}', '""', '"ab"', 'null', '5', '[]', '[["a"]]', '[null]', '{}', '')]
    return cases


def run() -> dict:
    """Runs in a process of its own: what the Python Host's modules answer."""
    spec = importlib.util.spec_from_file_location('replay_host', REPO / 'tests' / 'replay' / 'host.py')
    host = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(host)
    root = pathlib.Path(tempfile.mkdtemp(prefix='clarp-live-call-vectors-', dir='/var/tmp'))
    (root / 'scenario-config.toml').write_text('')
    host.isolate(root)
    host.loopback_only()
    sys.path.insert(0, str(REPO / 'server'))
    import datetime as datetimes

    from lib import agent_live_settings, agent_ring_live, agent_ring_live_socket, group_calls, settings_store

    live, CallError = agent_ring_live, group_calls.CallError
    group_calls._host_id = lambda: 'host-1'

    class Moment(datetimes.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 11, 18, 3, 8, 9, 123456, tzinfo=datetimes.timezone.utc).astimezone(tz)

    live.datetime = Moment

    def tried(act):
        try:
            return act(), None
        except CallError as error:
            return None, {'status': error.status, 'error': error.error}

    def moment(text):
        try:
            read = datetimes.datetime.fromisoformat(text)
        except ValueError:
            return 0
        return 1 if read.tzinfo is None else 2

    times = {'not one': [], 'without a zone': [], 'with a zone': []}
    for text in moments():
        times[('not one', 'without a zone', 'with a zone')[moment(text)]].append(text)

    modes = []
    for value in MODES:
        for default in ('classic', None):
            result, refused = tried(lambda: live.mode_of(value, default=default))
            modes.append({'asked': value, 'default': default, 'mode': result, 'refused': refused})

    def epoch(value):
        return tried(lambda: live._epoch({'epoch': value}, 'epoch'))

    numbers = {'request_ids': [{'value': value, 'ok': live._request_id_ok(value)} for value in REQUEST_IDS],
               'epochs': [{'value': value, 'epoch': epoch(value)[0], 'refused': epoch(value)[1]} for value in EPOCHS]}
    forms = [{'value': value, 'canonical': live.canonical(value).decode('utf-8'), 'digest': live.digest(value)}
             for value in CANONICAL]

    read = []
    for name, context, line in contexts():
        result, refused = tried(lambda: live.validate_context(context, line))
        read.append({'name': name, 'context': context, 'opening_line': line, 'result': result, 'refused': refused})

    tools, offered = [], {'schema': [], 'overlay': []}
    for name, held, story in tool_cases():
        config = agent_live_settings.frozen_config(held, story)
        row = {'name': name, 'profile': held, 'story': story, 'config': config}
        if config is not None:
            # What is offered and what the voice is told differ by the team and the call back only: each once.
            schema, overlay = agent_live_settings.tools_schema(config), agent_live_settings.overlay({'live_tools': config})
            for kind, value in (('schema', schema), ('overlay', overlay)):
                if value not in offered[kind]:
                    offered[kind].append(value)
                row[kind] = offered[kind].index(value)
            row['team'] = agent_live_settings.team_text(config)
        tools.append(row)

    live.ROOT = root / 'agent-live'

    def folder_of(held, files):
        """Writes one profile folder as the case says, and answers what `profile.json` holds as a text."""
        target = live.ROOT / 'profiles' / 'pump-guard'
        shutil.rmtree(live.ROOT, ignore_errors=True)
        target.mkdir(parents=True)
        for name, spec_ in files.items():
            (target / name).parent.mkdir(parents=True, exist_ok=True)
            (target / name).write_bytes(as_bytes(spec_))
        if held is DROP:
            return None
        text = held if isinstance(held, str) else json.dumps(held, ensure_ascii=False)
        (target / 'profile.json').write_text(text, encoding='utf-8')
        return text

    snapshots = []
    for case in snapshot_cases():
        text = folder_of(case['profile'], case['files'])
        live.provider_problem = (lambda: None) if case['key'] else (lambda: 'agent-live calls need an OpenAI API key on this Host')
        context = case['context']
        if case['call'] is not None:
            context, refused = tried(lambda: live.user_call_context('pump-guard', case['agent'], case['call']))
            assert refused is None, case['name']
        result, refused = tried(lambda: live.build_snapshot(case['agent'], context))
        row = {'name': case['name'], 'profile': text, 'files': case['files'], 'context': context, 'agent': case['agent'],
               'key': case['key'], 'snapshot': result, 'refused': refused}
        if result is not None:
            row.update(digest=live.digest(result), size=len(live.canonical(result)),
                       instructions=agent_ring_live_socket.instructions(result))
        snapshots.append(row)

    calls = []
    for case in user_call_cases():
        text = folder_of(case['profile'], FILES)
        result, refused = tried(lambda: live.user_call_context('pump-guard', case['agent'], case['request_id']))
        calls.append({'name': case['name'], 'profile': text, 'agent': case['agent'], 'request_id': case['request_id'],
                      'context': result, 'refused': refused})

    class Scanned:
        """A folder read in the order of its names, so that what is found is the same at every run."""

        def __init__(self, entries):
            self.entries = sorted(entries, key=lambda entry: entry.name)

        def __enter__(self):
            return iter(self.entries)

        def __exit__(self, *_):
            return False

        def __iter__(self):
            return iter(self.entries)

        def close(self):
            pass

    scan = os.scandir

    def in_order(path='.'):
        with scan(path) as entries:
            return Scanned(list(entries))

    registry = []
    for case in registry_cases():
        shutil.rmtree(live.ROOT, ignore_errors=True)
        beside = root / 'beside'
        shutil.rmtree(beside, ignore_errors=True)
        folders = []
        if case['there']:
            (live.ROOT / 'profiles').mkdir(parents=True)
        for entry in sorted(case['folders'], key=lambda entry: entry['name']):
            target = live.ROOT / 'profiles' / entry['name']
            held = entry['profile']
            text = None if held is None else (held if isinstance(held, str) else json.dumps(held, ensure_ascii=False))
            if entry['kind'] == 'file':
                target.write_text('a file')
            elif entry['kind'] == 'link':
                (beside / entry['name']).mkdir(parents=True)
                (beside / entry['name'] / 'profile.json').write_text(text, encoding='utf-8')
                target.symlink_to(beside / entry['name'])
            else:
                target.mkdir()
                if text is not None:
                    (target / 'profile.json').write_text(text, encoding='utf-8')
            folders.append({'name': entry['name'], 'kind': entry['kind'], 'profile': text})
        answers = {}
        for strict in (True, False):
            if case['known'] is None:
                settings_store.delete(live.USER_CALL_BINDINGS_KEY)
            else:
                settings_store.set_text(live.USER_CALL_BINDINGS_KEY, case['known'])
            os.scandir = in_order
            try:
                result, refused = tried(lambda: live.user_call_profiles(strict=strict))
            finally:
                os.scandir = scan
            result = None if result is None else dict(sorted(result.items()))
            answers['strict' if strict else 'lenient'] = {'takes': result, 'refused': refused,
                                                           'kept': settings_store.get(live.USER_CALL_BINDINGS_KEY)}
        registry.append({'name': case['name'], 'known': case['known'], 'there': case['there'], 'folders': folders, **answers})

    answered = {
        'what': 'A ring held as a live voice call, by server/lib/agent_ring_live.py (mode_of, _request_id_ok, _epoch, canonical, '
                'digest, validate_context, build_snapshot, user_call_context, user_call_profiles), agent_live_settings.py '
                '(frozen_config, team_text, tools_schema, overlay) and agent_ring_live_socket.instructions; and whether a text '
                'is a moment with a zone, by datetime.fromisoformat. A file of a profile is its text, its bytes in base64, or a '
                'text repeated.',
        'limits': {'context_bytes': live.CONTEXT_BYTES, 'snapshot_bytes': live.SNAPSHOT_BYTES, 'log_lines': live.LOG_LINES,
                   'log_line_signs': live.LOG_LINE_CHARS, 'opening_signs': live.OPENING_TEXT_CHARS,
                   'profiles_scanned': live.PROFILE_SCAN_LIMIT, 'request_id_bytes': live.REQUEST_ID_MAX,
                   'team_most': agent_live_settings.MAX_AGENTS, 'request_signs': agent_live_settings.REQUEST_CHARS},
        'names': {'feature': live.FEATURE, 'model': live.MODEL, 'voice': live.VOICE, 'provider': live.PROVIDER,
                  'protocol': live.PROTOCOL, 'stories': list(live.STORIES), 'sources': list(live.SOURCES),
                  'bindings_key': live.USER_CALL_BINDINGS_KEY, 'phases': list(live.LIVE_PHASES),
                  'close_reasons': list(live.CLOSE_REASONS)},
        'host_id': 'host-1', 'now': '2026-11-18T03:08:09+00:00',
        'moments': times, 'modes': modes, 'numbers': numbers, 'forms': forms, 'contexts': read, 'tools': tools,
        'offered': offered,
        'snapshots': snapshots, 'user_calls': calls, 'registry': registry,
    }
    shutil.rmtree(root, ignore_errors=True)
    return answered


def shrunk(value):
    """A value with every long run of one sign written as what stands before it, the sign, how often, and what
    follows: `{"~run": [before, sign, count, after]}`. Nothing here is told in such an object otherwise."""
    if isinstance(value, dict):
        return {key: shrunk(item) for key, item in value.items()}
    if isinstance(value, list):
        return [shrunk(item) for item in value]
    if not isinstance(value, str) or len(value) < 256:
        return value
    start = 0
    while start < len(value):
        end = start
        while end < len(value) and value[end] == value[start]:
            end += 1
        if end - start >= 256:
            return {'~run': [value[:start], value[start], end - start, shrunk(value[end:])]}
        start = end
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
    said = json.loads(ran.stdout.splitlines()[-1])
    out = ['{']
    plain = ('what', 'limits', 'names', 'host_id', 'now', 'offered')
    out.extend(f' {json.dumps(name)}: {json.dumps(said[name], ensure_ascii=True)},' for name in plain)
    out.append(' "moments": {')
    kinds = list(said['moments'])
    for at, kind in enumerate(kinds):
        out.append(f'  {json.dumps(kind)}: {json.dumps(said["moments"][kind], ensure_ascii=True)}'
                   + (',' if at < len(kinds) - 1 else ''))
    out.append(' },')
    out.append(' "numbers": ' + json.dumps(said['numbers'], ensure_ascii=True) + ',')
    listed = ('modes', 'forms', 'contexts', 'tools', 'snapshots', 'user_calls', 'registry')
    for place, name in enumerate(listed):
        out.append(f' {json.dumps(name)}: [')
        rows = [shrunk(row) for row in said[name]]
        out.extend('  ' + json.dumps(row, ensure_ascii=True) + (',' if at < len(rows) - 1 else '') for at, row in enumerate(rows))
        out.append(' ]' + (',' if place < len(listed) - 1 else ''))
    out.append('}')
    OUT.write_text('\n'.join(out) + '\n')
    moments_ = said['moments']
    refused = sum(1 for row in said['contexts'] if row['refused'])
    built = sum(1 for row in said['snapshots'] if row['snapshot'])
    opted_in = sum(1 for row in said['tools'] if row['config'])
    print(f'{OUT.relative_to(REPO)}: {sum(len(texts) for texts in moments_.values())} moments '
          f'({len(moments_["with a zone"])} with a zone, {len(moments_["without a zone"])} without), {len(said["modes"])} modes, '
          f'{len(said["forms"])} forms, {len(said["contexts"])} contexts ({refused} refused), {len(said["tools"])} profiles of tools '
          f'({opted_in} opt in), {len(said["snapshots"])} snapshots ({built} built), {len(said["user_calls"])} calls of the user, '
          f'{len(said["registry"])} registries')
    return 0


if __name__ == '__main__':
    sys.exit(main())
