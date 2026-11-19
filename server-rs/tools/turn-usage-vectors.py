#!/usr/bin/env python3
"""Writes the vectors of what a turn of Antigravity or of Codex took, in tokens.

The Python Host counts them since its PR 413 (R48) by two rules, run here as they are:

- Antigravity (`server/lib/backend/agy.py`): the runner's own `AgyBackend._handle_event` is handed made-up
  streams line by line (steps, and the result), and what it hands on is turned into the turn's figures as the
  Host turns it (`turn_stop._agy_accounting_event`, then `turn_stop._result_detail`): `tokens_in`, `tokens_out`,
  or none. A step's first usable usage counts once, an explicit zero included; where no step said any, the
  result's own figure counts. Every stream is played for a turn that begins its conversation and for one that
  continues it.
- Codex (`server/lib/codex_app_server.py`): its client's `_notification` is handed the app-server's
  `thread/tokenUsage/updated` of made-up turns of one thread, and `turn/completed`; what each turn says it took
  is written, with the thread's total where the turn ended (the highest it was told in the turn, each half on
  its own), which is all `codex exec --json` prints of a turn.

    nice "$(sed -n 1p ~/.local/share/clarp/current/SERVICE_PYTHON)" server-rs/tools/turn-usage-vectors.py

It writes `crates/clarp-backend/tests/fixtures/agy-usage-vectors.json` and
`crates/clarp-kernel/tests/fixtures/codex-turn-usage-vectors.json`, in a process of its own whose every home,
data, config and cache directory is a new empty directory (the replay harness's isolation), with no way out to
any network. The same files at every run.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess
import sys
import tempfile
import threading

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[1]
AGY_OUT = HERE.parent / 'crates' / 'clarp-backend' / 'tests' / 'fixtures' / 'agy-usage-vectors.json'
CODEX_OUT = HERE.parent / 'crates' / 'clarp-kernel' / 'tests' / 'fixtures' / 'codex-turn-usage-vectors.json'
CONVERSATION = '77777777-8888-9999-aaaa-bbbbbbbbbbbb'
# Of the Python Host's own test: a step of a continued conversation, and the conversation's total so far.
PER_STEP = {'input_tokens': 4969, 'output_tokens': 1, 'thinking_tokens': 0, 'cache_read_tokens': 4049, 'total_tokens': 4970}
CUMULATIVE = {'input_tokens': 13774, 'output_tokens': 3, 'thinking_tokens': 0, 'cache_read_tokens': 4049,
              'total_tokens': 13777}
OTHER = {'input_tokens': 17, 'output_tokens': 4}
DROP = object()


def step(index=2, usage=DROP, state='DONE', step_type='agent_response', **more):
    """A step's update, as agy prints one."""
    update = {'conversation_id': CONVERSATION, 'state': state, 'step_type': step_type, **more}
    if index is not DROP:
        update['step_index'] = index
    if usage is not DROP:
        update['usage'] = usage
    return {'event': 'step_update', 'step_update': update}


def result(usage=DROP, **more):
    body = {'conversation_id': CONVERSATION, 'status': 'SUCCESS', 'response': 'falcon\n', 'duration_seconds': 6.2,
            **more}
    if usage is not DROP:
        body['usage'] = usage
    return {'event': 'result', 'result': body}


def stream(name, *steps, usage=CUMULATIVE):
    begins = [step(2, DROP, step_type='user_input'), step(3, DROP, step_type='system_message', duration_seconds=0.1)]
    return {'name': name, 'lines': [json.dumps(row) for row in [*begins, *steps, result(usage)]]}


DONE = step(4, PER_STEP, duration_seconds=1.2)
MISSING = step(4, DROP, duration_seconds=1.2)
STREAMS = [
    # The Python Host's own test, its six orders.
    stream('a step told twice, the second time with more thought: the first counts', DONE,
           step(4, {**PER_STEP, 'thinking_tokens': 2}, duration_seconds=1.2)),
    stream('the same line twice', DONE, DONE),
    stream('a step that says nothing of what it took', MISSING),
    stream('what a step took comes on a later DONE of it', MISSING, DONE),
    stream('and then another step', MISSING, DONE, step(5, OTHER, duration_seconds=1.2)),
    stream('a step that said what it took, and then nothing', DONE, MISSING),
    stream('two DONEs of a step that disagree: the first counts', DONE, step(4, OTHER, duration_seconds=1.2)),
    # Halves the test does not walk.
    stream('a step whose first usage is nothing at all: it counts', step(4, {'input_tokens': 0, 'output_tokens': 0}), DONE),
    stream('every step said nothing: the turn took nothing, not the conversation',
           step(4, {'input_tokens': 0}), step(5, {'output_tokens': 0})),
    stream('a step that says only its total is no usage', step(4, {'total_tokens': 4970}), step(5, OTHER)),
    stream('a step that says only its total, and nothing else does', step(4, {'total_tokens': 4970})),
    stream('steps with no index each count', step(DROP, OTHER), step(DROP, {**OTHER, 'input_tokens': 18})),
    stream('a step with no index told twice word for word counts once', step(DROP, OTHER), step(DROP, OTHER)),
    stream('a done in small letters is a DONE', step(4, OTHER, state='done'), step(5, PER_STEP, state='Done')),
    stream('a done in small letters told twice: each line counts where it has no index',
           step(DROP, OTHER, state='done'), step(DROP, OTHER, state='done')),
    stream('a step under its index as a text is the same step', step(4, OTHER), step('4', PER_STEP)),
    stream('a step under a fraction is another step', step(4, OTHER), step(4.0, PER_STEP)),
    stream('a step that is not DONE says nothing of what it took', step(4, PER_STEP, state='ACTIVE'), step(5, OTHER)),
    stream('figures written as texts and fractions', step(4, {'input_tokens': '12', 'output_tokens': ' 3 ',
                                                              'thinking_tokens': 2.9, 'cache_read_tokens': '1_0'})),
    stream('figures that are none', step(4, {'input_tokens': True, 'output_tokens': None, 'thinking_tokens': 'x',
                                             'cache_read_tokens': [1]}), step(5, OTHER)),
    stream('a figure that is all the step said, and is none', step(4, {'input_tokens': 'many'})),
    stream('a usage that is no object', step(4, [1, 2]), step(5, 'all of it')),
    stream('thinking is output', step(4, {'input_tokens': 5, 'output_tokens': 2, 'thinking_tokens': 7})),
    stream('cache reads are input', step(4, {'input_tokens': 5, 'cache_read_tokens': 70})),
    stream('no step and no figure of the result', usage=DROP),
    stream('no step, and the result in texts', usage={'input_tokens': '20', 'output_tokens': 1.5,
                                                      'thinking_tokens': '2', 'cache_read_tokens': 3}),
    stream('no step, and a result that says only its total', usage={'total_tokens': 9}),
    stream('no step, and a result whose usage is no object', usage=[1]),
    stream('a tool step is a step', step(4, OTHER, step_type='tool'), step(5, OTHER, step_type='tool')),
]

# Codex: (total in, total out, last in, last out) of each notification; a turn's notifications in order; a
# notification of another turn than the one under way is the late one of the turn before.
SAMPLES = {'A': (426539683, 1158861, 265385, 200), 'B': (426807055, 1159192, 267372, 331),
           'C': (427075572, 1159282, 268517, 90), 'L': (1, 1, 1, 1), 'M': (426540000, 1158862, 10, 1),
           'X': (427075581, 1, 9, 1), 'Y': (1, 1159289, 1, 7), 'N': (427075582, 1159286, 10, 4),
           'F': (90, 7, 90, 7), 'G': (150, 9, 60, 2), 'H': (40, 3, 40, 3), 'Z': (5, 1, 6, 1),
           'W': (5, 1, 2, -1), 'V': (5, 1, 2, 1.0), 'P': (160, 12, 10, 3)}
THREADS = [
    # The Python Host's own test: each order, and then a turn that is told the last of the turn before late.
    *[{'name': f'the test: {order}', 'turns': [list(order), ['late:C', 'N']]}
      for order in ('ABBC', 'ABCB', 'ACB', 'ACLB', 'ACM', 'ACX', 'ACY')],
    {'name': 'a thread that begins with the turn', 'turns': [['F', 'G'], ['P']]},
    {'name': 'a later turn of a thread whose total fell', 'turns': [['A', 'C'], ['H']]},
    {'name': 'a first notification that is no measure, then one that is', 'turns': [['Z', 'W', 'V', 'C']]},
    {'name': 'a turn told nothing', 'turns': [['F'], []]},
    {'name': 'a late notification that comes first in its turn', 'turns': [['A', 'C'], ['late:B', 'late:C', 'N']]},
]


def run() -> dict:
    spec = importlib.util.spec_from_file_location('replay_host', REPO / 'tests' / 'replay' / 'host.py')
    host = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(host)
    root = pathlib.Path(tempfile.mkdtemp(prefix='clarp-turn-usage-vectors-', dir='/var/tmp'))
    (root / 'scenario-config.toml').write_text('')
    host.isolate(root)
    host.loopback_only()
    sys.path.insert(0, str(REPO / 'server'))
    from lib import codex_app_server, turn_stop
    from lib import log as host_log
    from lib.backend import agy
    from lib.backend.codex import TurnState

    host_log.log = lambda *said, **more: None
    agy_streams = []
    backend = object.__new__(agy.AgyBackend)
    # What the runner does beside counting (moving the turn's state, telling the live text and the
    # transcript) has nowhere to go here, and nothing of it is counted.
    for doing in ('_transition', '_broadcast_transcript', '_persist_live_text', '_speak', '_retract_live_text'):
        setattr(backend, doing, lambda *said, **more: None)
    agy.log = agy.log_exception = lambda *said, **more: None
    for one in STREAMS:
        played = {}
        for fresh in (True, False):
            st = agy._TurnState()
            errors, results = [], []
            for revision, line in enumerate(one['lines'], 1):
                backend._handle_event(json.loads(line), revision, st, agent_id='agent', session='elli',
                                      trace_id='trace', on_session_init=lambda *said, **more: None,
                                      on_result=results.append, on_error=errors.append, stream=None, enqueue=None)
            event = st.pending_result
            if errors or event is None:
                raise SystemExit(f'{one["name"]}: {errors or "no result"}')
            event = turn_stop._agy_accounting_event(event, fresh_session=fresh)
            detail = turn_stop._result_detail(event, trace_id='trace')
            played['fresh' if fresh else 'continued'] = (
                [detail['tokens_in'], detail['tokens_out']] if 'tokens_in' in detail else None)
        agy_streams.append({**one, **played})

    threads = []
    for thread in THREADS:
        client = object.__new__(codex_app_server._Client)
        client.agent_id = 'agent'
        client._callback = lambda callback, value: callback(value)
        told = []

        class Handle:
            def __init__(self):
                self._done = threading.Event()

        turns = []
        for number, notes in enumerate(thread['turns']):
            turn = f'turn-{number}'
            client.active = codex_app_server._ActiveTurn(
                turn, 'thread', 'agent', 'session', turn, TurnState(live_backend_session_id='thread'),
                Handle(), told.append, None, None, lambda **more: 0)
            high = None
            for note in notes:
                late = note.startswith('late:')
                ti, to, li, lo = SAMPLES[note.removeprefix('late:')]
                client._notification('thread/tokenUsage/updated', {
                    'threadId': 'thread', 'turnId': f'turn-{number - 1}' if late else turn, 'tokenUsage': {
                        'total': {'inputTokens': ti, 'outputTokens': to},
                        'last': {'inputTokens': li, 'outputTokens': lo}}})
                if not late and type(ti) is int and type(to) is int:
                    high = [max(high[0], ti), max(high[1], to)] if high else [ti, to]
            # Where the turn began, as the reference measured it from its first notification.
            opening = client.active.usage_entry
            before = len(told)
            client._notification('turn/completed', {'threadId': 'thread', 'turn': {'id': turn, 'status': 'completed'}})
            usage = told[before]['usage'] if len(told) > before else None
            turns.append({'notifications': notes, 'total_at_end': high,
                          'opening': None if opening is None else list(opening),
                          'took': None if not usage else [usage['input_tokens'], usage['output_tokens']]})
        threads.append({'name': thread['name'], 'turns': turns})
    return {'agy': agy_streams, 'codex': threads, 'samples': {key: list(value) for key, value in SAMPLES.items()}}


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == '--run':
        print(json.dumps(run()))
        return 0
    ran = subprocess.run([sys.executable, '-I', __file__, '--run'], capture_output=True, text=True,
                         env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}, check=False)
    if ran.returncode != 0:
        print(ran.stderr[-5000:], file=sys.stderr)
        return 1
    told = json.loads(ran.stdout.splitlines()[-1])
    rows = told['agy']
    AGY_OUT.write_text('{\n "streams": [\n' + ',\n'.join('  ' + json.dumps(row, ensure_ascii=True) for row in rows)
                       + '\n ]\n}\n', encoding='ascii')
    threads = told['codex']
    CODEX_OUT.write_text('{\n "samples": ' + json.dumps(told['samples'], ensure_ascii=True) + ',\n "threads": [\n'
                         + ',\n'.join('  ' + json.dumps(row, ensure_ascii=True) for row in threads) + '\n ]\n}\n',
                         encoding='ascii')
    for row in rows:
        print(f'agy: {row["name"]}: {row["fresh"]} / {row["continued"]}')
    for thread in threads:
        print(f'codex: {thread["name"]}: ' + '; '.join(f'{t["took"]} (end {t["total_at_end"]})' for t in thread['turns']))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
