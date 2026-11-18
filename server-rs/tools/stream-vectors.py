#!/usr/bin/env python3
"""Writes the vectors the Rust Host's stream of speech is tested against.

When the engine that hears finds the end of a turn of speech itself, a phone streams what it
hears to the Host over a socket (`GET /stt/stream`) and the Host carries it to the engine
(Deepgram Flux) and tells the phone what the engine says of the turns. What is no carrying in
that is pure, in `server/lib/stt_stream.py`, and is asked here as it is:

* the address the engine is reached at, by how its listening is tuned in the phone's request
  and the words it is asked to listen for (`_float`, `flux_url`, put together as
  `serve_stt_stream` puts them together);
* what a message of the engine means (`normalize_flux_message`);
* the books of one stream (`TurnLedger`): which sound belongs to which turn, what the phone is
  told of an event, and what is written down and kept when a turn ends. Each story is a list
  of what happens (so much is heard, the engine says something, time passes), with a clock
  that is held, names of turns that count up, and a writer of runs that answers what the story
  says or fails;
* the sound of a turn as a file (`pcm16_wav`).

    python3 server-rs/tools/stream-vectors.py

writes `crates/clarp-kernel/tests/fixtures/stream-vectors.json`. Nothing of a Host is touched or
started, and nothing is asked of any engine: pure functions are called.
"""
from __future__ import annotations

import hashlib
import itertools
import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[1]
OUT = HERE.parent / 'crates' / 'clarp-kernel' / 'tests' / 'fixtures' / 'stream-vectors.json'

# How a phone may tune the listening. `None`: it does not say.
THRESHOLDS = [None, '', '0.7', '0.5', '0.49', '1', '1.01', '0.625', '0.875', '0.705', '0.715', '0.995', '0.999',
              '0.3', '0.29', '0.9', '0.91', '0.305', '0.895', '1e-1', '7e-1', ' 0.8 ', '0.8x', 'nan', 'inf', '-inf',
              '-1', '0', '.5', '5.', '+0.6', 'NaN', 'Infinity', '0_7', '0.6_5', '0x10', 'seven', '0,7', '0.7\n', '1e400',
              '-0.0', '_0.8', '0.8_', '0._8', '0__8', '8e-1_0', '\xa00.8\u2003', 'INF', '-nan', '+inf', '0.8e', '1e', '.',
              '0.8 5', '1,000']
TIMEOUTS = [None, '', '60000', '500', '499', '100000', '5000.9', '5000.1', '-5', '0', '1e3', 'abc', ' 700 ',
            '60000.999', '59999.5', 'nan', 'inf', '-inf', '1e400', '2_000']
KEYTERMS = [[], ['Clarp'], ['two words', 'Clarp'], ['\xfcn\xef', 'a&b=c', 'x/y?z', "it's", '100%', 'a+b', '~tilde',
                                                     'dot.dash-under_score', 'emoji \U0001f642', '', ' ', '#:@!$,;*()[]']]
MODELS = ['flux-general-en', 'another model/2']

# What an engine may say.
MESSAGES = [
    {'type': 'TurnInfo', 'event': 'StartOfTurn', 'turn_index': 0, 'transcript': ''},
    {'type': 'TurnInfo', 'event': 'Update', 'turn_index': 1, 'transcript': 'hello there', 'end_of_turn_confidence': 0.12},
    {'type': 'TurnInfo', 'event': 'EagerEndOfTurn', 'turn_index': 1, 'transcript': 'hello there', 'end_of_turn_confidence': 0.6},
    {'type': 'TurnInfo', 'event': 'TurnResumed', 'turn_index': 1, 'transcript': 'hello there you'},
    {'type': 'TurnInfo', 'event': 'EndOfTurn', 'turn_index': 1, 'transcript': 'hello there you', 'end_of_turn_confidence': 0.91,
     'words': [{'word': 'hello'}, 'not a word', {'word': 'there'}]},
    {'type': 'TurnInfo', 'event': 'SomethingNew', 'turn_index': 1, 'transcript': 'x'},
    {'type': 'TurnInfo', 'event': '', 'turn_index': 1},
    {'type': 'TurnInfo', 'turn_index': 1},
    {'type': 'TurnInfo', 'event': None},
    {'type': 'TurnInfo', 'event': 'endofturn'},
    {'type': 'TurnInfo', 'event': ' EndOfTurn '},
    {'type': 'TurnInfo', 'event': 7},
    {'type': 'TurnInfo', 'event': ['EndOfTurn']},
    {'type': 'turninfo', 'event': 'EndOfTurn'},
    {'type': 'Connected', 'request_id': 'r'},
    {'type': 'Error', 'description': 'no'},
    {'event': 'EndOfTurn'},
    {},
    'EndOfTurn',
    ['TurnInfo'],
    None,
    7,
    {'type': 'TurnInfo', 'event': 'Update', 'turn_index': '4', 'transcript': 'a text index'},
    {'type': 'TurnInfo', 'event': 'Update', 'turn_index': ' 5 ', 'transcript': 'blanks'},
    {'type': 'TurnInfo', 'event': 'Update', 'turn_index': 2.9, 'transcript': 'a part'},
    {'type': 'TurnInfo', 'event': 'Update', 'turn_index': -3.9, 'transcript': 'below'},
    {'type': 'TurnInfo', 'event': 'Update', 'turn_index': True},
    {'type': 'TurnInfo', 'event': 'Update', 'turn_index': False},
    {'type': 'TurnInfo', 'event': 'Update', 'turn_index': None},
    {'type': 'TurnInfo', 'event': 'Update', 'turn_index': 'four'},
    {'type': 'TurnInfo', 'event': 'Update', 'turn_index': '2.5'},
    {'type': 'TurnInfo', 'event': 'Update', 'turn_index': [3]},
    {'type': 'TurnInfo', 'event': 'Update', 'turn_index': {'n': 3}},
    {'type': 'TurnInfo', 'event': 'Update', 'turn_index': '1_0'},
    {'type': 'TurnInfo', 'event': 'Update', 'turn_index': 12345678901234567890},
    {'type': 'TurnInfo', 'event': 'Update', 'turn_index': 0, 'end_of_turn_confidence': '0.5'},
    {'type': 'TurnInfo', 'event': 'Update', 'turn_index': 0, 'end_of_turn_confidence': ' 0.25 '},
    {'type': 'TurnInfo', 'event': 'Update', 'turn_index': 0, 'end_of_turn_confidence': 'high'},
    {'type': 'TurnInfo', 'event': 'Update', 'turn_index': 0, 'end_of_turn_confidence': None},
    {'type': 'TurnInfo', 'event': 'Update', 'turn_index': 0, 'end_of_turn_confidence': 1},
    {'type': 'TurnInfo', 'event': 'Update', 'turn_index': 0, 'end_of_turn_confidence': 0},
    {'type': 'TurnInfo', 'event': 'Update', 'turn_index': 0, 'end_of_turn_confidence': True},
    {'type': 'TurnInfo', 'event': 'Update', 'turn_index': 0, 'end_of_turn_confidence': [0.5]},
    {'type': 'TurnInfo', 'event': 'Update', 'turn_index': 0, 'end_of_turn_confidence': '1e-3'},
    {'type': 'TurnInfo', 'event': 'Update', 'turn_index': 0, 'transcript': None},
    {'type': 'TurnInfo', 'event': 'Update', 'turn_index': 0, 'transcript': 0},
    {'type': 'TurnInfo', 'event': 'Update', 'turn_index': 0, 'transcript': 12},
    {'type': 'TurnInfo', 'event': 'Update', 'turn_index': 0, 'transcript': 1.5},
    {'type': 'TurnInfo', 'event': 'Update', 'turn_index': 0, 'transcript': True},
    {'type': 'TurnInfo', 'event': 'Update', 'turn_index': 0, 'transcript': False},
    {'type': 'TurnInfo', 'event': 'Update', 'turn_index': 0, 'transcript': ['a', 1, None]},
    {'type': 'TurnInfo', 'event': 'Update', 'turn_index': 0, 'transcript': {'text': "it's", 'n': 1.0}},
    {'type': 'TurnInfo', 'event': 'Update', 'turn_index': 0, 'transcript': []},
    {'type': 'TurnInfo', 'event': 'Update', 'turn_index': 0, 'transcript': '  kept with its blanks  '},
    {'type': 'TurnInfo', 'event': 'Update', 'turn_index': 0, 'transcript': 'na\xefve \U0001f642'},
]

# What happens on one stream. `heard`: so many bytes of sound; `said`: the engine says this; `wait`: so
# many seconds pass. `runs`: what the writer of runs answers, in turn (`None`: it fails).
E = lambda event, turn, text='', **more: {'said': {'type': 'TurnInfo', 'event': event, 'turn_index': turn, 'transcript': text, **more}}  # noqa: E731
STORIES = {
    'a turn of speech from its first sound to its end': {
        'runs': [41],
        'steps': [{'heard': 3200}, {'wait': 0.2}, E('StartOfTurn', 0), {'heard': 6400}, {'wait': 0.4},
                  E('Update', 0, 'hello', end_of_turn_confidence=0.1234), {'heard': 3200}, {'wait': 1.0345},
                  E('EagerEndOfTurn', 0, 'hello there', end_of_turn_confidence=0.55551),
                  E('TurnResumed', 0, 'hello there'), {'heard': 3201}, {'wait': 0.5},
                  E('EndOfTurn', 0, 'hello there you', end_of_turn_confidence=0.9995)],
    },
    'turns that follow one another each have a name and their own sound': {
        'runs': [7, 8, 9],
        'steps': [{'heard': 100}, {'wait': 1}, E('EndOfTurn', 0, 'one'), {'heard': 200}, {'heard': 300}, {'wait': 2.5},
                  E('EndOfTurn', 1, 'two'), {'wait': 0.001}, E('StartOfTurn', 2), {'wait': 0.0004}, E('EndOfTurn', 2, 'three')],
    },
    'the engine speaks before any sound was heard': {
        'runs': [1],
        'steps': [E('StartOfTurn', 0), {'wait': 3}, E('Update', 0, 'early'), {'wait': 0.9999}, E('EndOfTurn', 0, 'early')],
    },
    'a turn that ends with no sound is written down and nothing is kept of it': {
        'runs': [5, 6],
        'steps': [E('EndOfTurn', 0, ''), {'heard': 0}, {'wait': 0.5}, E('EndOfTurn', 1, 'silence')],
    },
    'a run that cannot be written is not worth a turn': {
        'runs': [None, 0, 12],
        'steps': [{'heard': 640}, {'wait': 0.25}, E('EndOfTurn', 0, 'not written'), {'heard': 640}, {'wait': 0.25},
                  E('EndOfTurn', 1, 'written as nothing'), {'heard': 640}, {'wait': 0.25}, E('EndOfTurn', 2, 'written')],
    },
    'sound that cannot be kept is not worth a turn': {
        'runs': [3, 4],
        'keeping fails': True,
        'steps': [{'heard': 1000}, {'wait': 0.1}, E('EndOfTurn', 0, 'not kept'), {'heard': 2}, E('EndOfTurn', 1, 'nor this')],
    },
    'of a long turn the last two minutes are kept': {
        'runs': [2],
        'steps': [{'heard': 3_000_000}, {'heard': 839_999}, {'heard': 1}, {'heard': 1}, {'heard': 500_000}, {'wait': 130},
                  E('EndOfTurn', 0, 'long')],
    },
    'what the engine says that is no event of a turn changes nothing': {
        'runs': [9],
        'steps': [{'heard': 10}, {'said': {'type': 'Connected'}}, {'said': {'type': 'TurnInfo', 'event': 'Other'}},
                  {'wait': 1}, E('Update', 3, 'still the first turn', end_of_turn_confidence=None),
                  E('Update', 3, 'a confidence of nothing', end_of_turn_confidence=0),
                  E('Update', 3, 'one in a text', end_of_turn_confidence='0.33349'), {'wait': 1},
                  E('EndOfTurn', 3, 'ended', end_of_turn_confidence=0.0005)],
    },
    'confidences are told to three places': {
        'runs': [],
        'steps': [E('Update', 0, 'a', end_of_turn_confidence=value) for value in
                  (0.0005, 0.0015, 0.0025, 0.1235, 0.9995, 0.99949, 1, 0.5, 1e-9, 0.123456789, 2.675, -0.0004, 0.0625,
                   0.1875, 0.3125)],
    },
}
WAVS = [0, 1, 2, 3, 320, 32000, 65537]


def sound(count: int, start: int) -> bytes:
    """So many bytes that are not all alike, so that which of them were kept can be told."""
    return bytes((start + at) % 251 for at in range(count))


def main() -> int:
    sys.path.insert(0, str(REPO / 'server'))
    from lib import stt_stream as stream

    # A run that cannot be written and a sound that cannot be kept are logged by that module. Here
    # they are part of the stories: nothing is logged anywhere, whatever a Host's logging would do.
    stream.log_exception = lambda *said, **more: None

    def address(model, threshold, eager, timeout, keyterms):
        # As `serve_stt_stream` puts the address together from the phone's request.
        query = {'eot_threshold': threshold, 'eager_eot_threshold': eager, 'eot_timeout_ms': timeout}
        try:
            return stream.flux_url(
                model=model,
                eot_threshold=stream._float(query.get('eot_threshold'), stream.DEFAULT_EOT_THRESHOLD),
                eager_eot_threshold=stream._float(query.get('eager_eot_threshold'), stream.DEFAULT_EAGER_EOT_THRESHOLD),
                eot_timeout_ms=int(stream._float(query.get('eot_timeout_ms'), stream.DEFAULT_EOT_TIMEOUT_MS)),
                keyterms=list(keyterms))
        except (ValueError, OverflowError) as failed:
            return {'fails': type(failed).__name__}

    # Each way of tuning by itself, and some together.
    addresses = []
    for threshold in THRESHOLDS:
        addresses.append({'threshold': threshold, 'url': address(MODELS[0], threshold, None, None, [])})
    for eager in THRESHOLDS:
        addresses.append({'eager': eager, 'url': address(MODELS[0], None, eager, None, [])})
    for timeout in TIMEOUTS:
        addresses.append({'timeout': timeout, 'url': address(MODELS[0], None, None, timeout, [])})
    for model, keyterms in itertools.product(MODELS, KEYTERMS):
        addresses.append({'model': model, 'keyterms': keyterms, 'threshold': '0.8', 'eager': '0.4', 'timeout': '7000',
                          'url': address(model, '0.8', '0.4', '7000', keyterms)})

    def meant(message):
        event = stream.normalize_flux_message(message)
        if event is None:
            return None
        return {'kind': event.kind, 'turn': event.turn, 'text': event.text, 'confidence': event.confidence}

    messages = [{'said': message, 'means': meant(message)} for message in MESSAGES]

    stories = []
    for name, story in STORIES.items():
        clock = [1000.0]
        names = itertools.count(1)
        runs = iter(story['runs'])
        written, kept = [], []

        def record_turn(**run):
            written.append({**run, 'keyterms': list(run['keyterms'])})
            answer = next(runs)
            if answer is None:
                raise RuntimeError('the run could not be written')
            return answer

        def retain(trace_id, wav, fails=story.get('keeping fails', False)):
            kept.append({'trace_id': trace_id, 'bytes': len(wav), 'sha256': hashlib.sha256(wav).hexdigest()})
            if fails:
                raise OSError('the sound could not be kept')

        ledger = stream.TurnLedger(
            session='a-session', provider='deepgram', model='flux-general-en', keyterms=('Clarp', 'two words'),
            capacity=50, new_trace=lambda: f'trace-{next(names)}', record_turn=record_turn, retain=retain,
            now=lambda: clock[0])
        steps, heard = [], 0
        for step in story['steps']:
            if 'wait' in step:
                clock[0] += step['wait']
                steps.append(step)
            elif 'heard' in step:
                ledger.heard(sound(step['heard'], heard))
                heard += step['heard']
                steps.append({**step, 'trace_id': ledger.trace_id, 'held': len(ledger.audio)})
            else:
                event = stream.normalize_flux_message(step['said'])
                told = ledger.apply(event) if event is not None else None
                steps.append({**step, 'told': told, 'trace_id': ledger.trace_id, 'held': len(ledger.audio),
                              'ended': ledger.turns_ended})
        # What the writer of runs answered, in turn: a number, nothing (0), or that it failed (null).
        stories.append({'story': name, 'steps': steps, 'answers': story['runs'], 'written': written, 'kept': kept})

    wavs = []
    for count in WAVS:
        wav = stream.pcm16_wav(sound(count, 0))
        wavs.append({'bytes': count, 'head': wav[:44].hex(), 'whole': len(wav), 'sha256': hashlib.sha256(wav).hexdigest()})

    vectors = {
        'what': 'The stream of speech, by server/lib/stt_stream.py: the address of the engine (_float, flux_url), what '
                'a message of the engine means (normalize_flux_message), the books of one stream (TurnLedger) and the '
                'sound of a turn as a file (pcm16_wav). Sound is bytes that count up modulo 251 from where the stream is.',
        'defaults': {'model': stream.FLUX_MODEL, 'url': stream.FLUX_URL, 'sample_rate': stream.SAMPLE_RATE,
                     'eot_threshold': stream.DEFAULT_EOT_THRESHOLD, 'eager_eot_threshold': stream.DEFAULT_EAGER_EOT_THRESHOLD,
                     'eot_timeout_ms': stream.DEFAULT_EOT_TIMEOUT_MS, 'flush_grace_s': stream.UPSTREAM_FLUSH_GRACE_SEC,
                     'upstream_timeout_s': stream.UPSTREAM_TIMEOUT_SEC},
        'addresses': addresses,
        'messages': messages,
        'stories': stories,
        'wavs': wavs,
    }
    OUT.write_text(json.dumps(vectors, indent=1, ensure_ascii=True) + '\n')
    failing = sum(1 for row in addresses if isinstance(row['url'], dict))
    events = sum(1 for story in stories for step in story['steps'] if 'said' in step)
    print(f'{OUT.relative_to(REPO)}: {len(addresses)} addresses ({failing} fail there), {len(messages)} messages '
          f'({sum(1 for row in messages if row["means"])} mean an event), {len(stories)} stories '
          f'({sum(len(story["steps"]) for story in stories)} steps, {events} things said, '
          f'{sum(len(story["written"]) for story in stories)} runs written, {sum(len(story["kept"]) for story in stories)} '
          f'sounds kept), {len(wavs)} files of sound')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
