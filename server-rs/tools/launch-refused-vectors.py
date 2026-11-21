#!/usr/bin/env python3
"""Writes the vectors of a send whose tool could not be started (R121, the release owner's PR 466).

The Python Host tries again a launch its backend refused before anything was sent (`backend.base.LaunchRefused`):
from its queue, after waits of 1, 2, 4 ... 60 s (`recovery_backoff.RecoveryBackoff.failed`), refusals that differ only
in ids, counts or times counted as one (`recovery_backoff.signature`), and after eight of one (`PARK_AFTER`) the send
ends `failed` and its sender is told by a notice (`turn_queue.REFUSED_LAUNCH_NOTICE`, `fail_refused_launch`). This
drives those functions as they are:

- `signature` of refusals of every shape, and `_brief`, which cuts what the notice says of one;
- the waits and the count `RecoveryBackoff.failed` keeps over a run of refusals, one and several, and over one that
  differs;
- the notice's words, for a count and a refusal;
- whole stories on the dispatcher itself (`TurnDispatchService`, on a backend that refuses, a clock and timers of the
  test's): when each try was made, and how the send ended, for one send and for two behind each other.

    "$(sed -n 1p ~/.local/share/clarp/current/SERVICE_PYTHON)" server-rs/tools/launch-refused-vectors.py

It runs in a process of its own whose every home, data, config and cache directory is new and empty (the replay
harness's own isolation); no Host is started and nothing leaves the machine. It writes
`crates/clarp-kernel/tests/fixtures/launch-refused-vectors.json`.
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
OUT = REPO / 'server-rs' / 'crates' / 'clarp-kernel' / 'tests' / 'fixtures' / 'launch-refused-vectors.json'

STUCK = 'Waiting for the previous Codex session to release this conversation'


@tool_scratch.case('clarp-launch-refused-vectors-')
def run(root) -> dict:
    spec = importlib.util.spec_from_file_location('replay_host', REPO / 'tests' / 'replay' / 'host.py')
    host = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(host)
    (root / 'scenario-config.toml').write_text('')
    host.isolate(root)
    host.loopback_only()
    sys.path.insert(0, str(REPO / 'server'))
    import itertools
    from types import SimpleNamespace

    from lib import agents as agents_db
    from lib import db, recovery_backoff, turn_queue
    from lib.backend.base import LaunchRefused
    import lib.turn_dispatch as dispatch

    def refusal(kind, text):
        return type(kind, (LaunchRefused,), {})(text)

    # ---- what is one refusal, and what the notice says of one
    texts = [
        STUCK,
        'Waiting for the previous Codex session to release thread 0199c0de-1111-7222-8333-444455556666',
        'Waiting for the previous Codex session to release thread 0199C0DE-1111-7222-8333-444455556666',
        '`codex` not on PATH — install it',
        'failed after 3 attempts in 120ms',
        'pid 48213 exited',
        'hash deadbeef and cafe and abcdef',
        'ABCDEF is no hex of its, nor is 0xFF',
        'digits of other scripts: ٣٤ and ३',
        'session 0199c0de-1111-7222-8333-44445555666 is one short',
        'a' * 290 + ' 12345678',
        'é' * 299 + 'x',
        '',
        'line one\nline two 7',
    ]
    signatures = []
    for kind in ('CodexSessionStuck', 'CliMissing', 'AgyLaunchInvalid'):
        for text in texts:
            signatures.append({'kind': kind, 'text': text, 'signature': recovery_backoff.signature(refusal(kind, text))})
    briefs = [{'text': text, 'brief': recovery_backoff._brief(text)}
              for text in ('short', 'x' * 240, 'x' * 241, 'a' * 100 + 'b' * 200,
                           'start ' + 'm' * 300 + ' end: rollout /x is empty', 'é' * 500)]

    # ---- the count and the waits over runs of refusals
    def counted(errors):
        backoff = recovery_backoff.RecoveryBackoff()
        steps = []
        now = 1000.0
        for kind, text in errors:
            state = backoff.failed('agent', 'q-1', refusal(kind, text), now, refused=True)
            ends = state.parked or state.refusals >= recovery_backoff.REFUSED_IN_ALL
            steps.append({'kind': kind, 'text': text, 'failures': state.failures, 'identical': state.identical,
                          'refusals': state.refusals, 'wait_s': state.retry_at - now, 'parked': state.parked,
                          'ends': ends, 'error': state.error})
            now = state.retry_at
        return steps

    same = [('CodexSessionStuck', STUCK)] * 20
    runs = [
        {'what': 'one refusal, again and again', 'steps': counted(same)},
        {'what': 'one refusal that differs only in its ids and counts',
         'steps': counted([('CodexSessionStuck', f'thread {n:08x}-1111-7222-8333-444455556666 held, try {n}')
                           for n in range(10)])},
        {'what': 'another refusal in the middle begins the count of one again',
         'steps': counted(same[:5] + [('CliMissing', '`codex` not on PATH')] + same[:9])},
        {'what': 'a long refusal is kept by both its ends',
         'steps': counted([('CodexSessionStuck', 'failed to resume: ' + 'x' * 300 + f' rollout {n} is empty')
                           for n in range(9)])},
        {'what': 'two kinds that alternate end the row at its twentieth refusal in all',
         'steps': counted([('CodexSessionStuck', STUCK), ('CliMissing', '`codex` not on PATH')] * 13)},
        {'what': 'another kind with the same words is another refusal',
         'steps': counted([('CodexSessionStuck', STUCK), ('AgyOwnershipLost', STUCK)] * 6)},
    ]
    waits = [{'failures': n, 'wait_s': min(recovery_backoff.MAX_S, recovery_backoff.BASE_S * 2 ** min(n - 1, 16))}
             for n in range(1, 25)]
    notices = [{'failures': failures, 'error': error,
                'notice': turn_queue.REFUSED_LAUNCH_NOTICE.format(failures=failures, error=error.rstrip('.'))}
               for failures, error in ((8, STUCK), (8, STUCK + '.'), (11, 'a refusal...'), (8, ''),
                                       (8, recovery_backoff._brief('start ' + 'm' * 300 + ' end.')))]

    # ---- whole stories on the dispatcher
    class Clock:
        def __init__(self):
            self.t = 1000.0

        def __call__(self):
            return self.t

    class Backends:
        CLAUDE = 'claude'

        def __init__(self):
            self.tries = []
            self.broken = {}
            self.live = set()

        def normalize(self, backend):
            return backend or self.CLAUDE

        def interrupt(self, backend, agent_id):
            return 0

        def active_handles(self, backend, agent_id):
            return ['handle'] if agent_id in self.live else []

        def spawn_turn(self, backend, **kwargs):
            refuse = self.broken.get(kwargs['agent_id'])
            refused = None if refuse is None else refuse(kwargs['trace_id'])
            self.tries.append({'trace': kwargs['trace_id'], 'at': clock.t - 1000.0, 'refused': refused is not None})
            if refused is not None:
                raise refused
            self.live.add(kwargs['agent_id'])

    def run_until(timers, until):
        while True:
            at = min((t for t, _ in timers if t <= until), default=None)
            if at is None:
                break
            for i, (t, fn) in enumerate(timers):
                if t == at:
                    timers.pop(i)
                    break
            clock.t = max(clock.t, at)
            fn()
        clock.t = until

    clock = Clock()

    def story(name, what, sends, refuse):
        dispatch._RUNTIME_CLIENT = None
        dispatch.reset_for_tests()
        recovery_backoff_state = getattr(dispatch, '_RECOVERY_BACKOFF', None)
        if recovery_backoff_state is not None:
            recovery_backoff_state.reset_for_tests()
        clock.t = 1000.0
        backends, timers, ids = Backends(), [], itertools.count(1)
        ctx = SimpleNamespace(default_session=name, agents_path=root / f'{name}-unused.json',
                              stream=SimpleNamespace(broadcast=lambda _e: None))
        service = dispatch.TurnDispatchService(
            ctx, backend_registry=backends, home=root,
            uuid_factory=lambda: f'backend-session-{next(ids)}', now=clock,
            retry_scheduler=lambda delay, fn: timers.append((clock.t + delay, fn)))
        agent_id = agents_db.create_agent(persona=name.title(), voice_id='V', cwd=str(root), session=name,
                                          backend='claude')
        agents_db.start_runtime(agent_id, name)
        backends.broken[agent_id] = lambda trace: refuse(trace, backends.tries)
        for at, trace, origin in sends:
            run_until(timers, 1000.0 + at)
            try:
                service.dispatch(text=f'message {trace}', requested_session=name, forced_session=name,
                                 trace_id=trace, client_msg_id=f'q-{trace}', synthesize_audio=False,
                                 queue_if_busy=True, origin=origin)
            except Exception:  # noqa: BLE001 - the refusal is the dispatch's answer
                pass
        run_until(timers, clock.t + 3600)
        rows = {}
        for _at, trace, _origin in sends:
            row = db.conn().execute('SELECT queue_id, status FROM queued_turns WHERE trace_id=?', (trace,)).fetchone()
            notice = None
            if row is not None:
                found = db.conn().execute('SELECT role, origin, text FROM messages WHERE message_id=?',
                                          (f'notice:{turn_queue.REFUSED_LAUNCH}:{row["queue_id"]}',)).fetchone()
                notice = None if found is None else {'role': found['role'], 'origin': found['origin'],
                                                     'text': found['text']}
            rows[trace] = {'status': None if row is None else row['status'], 'notice': notice}
        return {'name': name, 'what': what, 'sends': [{'at': at, 'trace': trace, 'origin': origin}
                                                      for at, trace, origin in sends],
                'tries': backends.tries, 'ended': rows}

    def stuck(_trace, _tries):
        return refusal('CodexSessionStuck', STUCK)

    def alternating(_trace, tries):
        if len(tries) % 2:
            return refusal('CliMissing', '`codex` not on PATH')
        return refusal('CodexSessionStuck', STUCK)

    def stuck_three_times(_trace, tries):
        return refusal('CodexSessionStuck', STUCK) if len(tries) < 3 else None

    stories = [
        story('miso', 'one send that is refused every time', [(0, 'miso-1', 'user')], stuck),
        story('nora', 'two sends, each refused every time', [(0, 'nora-1', 'user'), (0, 'nora-2', 'user')], stuck),
        story('pia', "a user's send while the first waits asks for the agent again",
              [(0, 'pia-1', 'user'), (10, 'pia-2', 'user')], stuck),
        story('quin', 'a send refused three times and then started', [(0, 'quin-1', 'user')], stuck_three_times),
        story('olga', 'a send refused by two kinds in turn ends at the twentieth counted refusal',
              [(0, 'olga-1', 'user')], alternating),
    ]
    return {'signatures': signatures, 'briefs': briefs, 'runs': runs, 'waits': waits, 'notices': notices,
            'stories': stories, 'park_after': recovery_backoff.PARK_AFTER,
            'refused_in_all': recovery_backoff.REFUSED_IN_ALL}


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == '--run':
        print(json.dumps(run(), ensure_ascii=True))
        return 0
    ran = subprocess.run([sys.executable, '-I', __file__, '--run'], capture_output=True, text=True,
                         env=tool_scratch.child_env({'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}), check=False)
    if ran.returncode != 0:
        print(ran.stderr[-5000:], file=sys.stderr)
        return 1
    told = json.loads(ran.stdout.splitlines()[-1])
    out = ['{']
    keys = list(told)
    for number, key in enumerate(keys):
        value = told[key]
        if isinstance(value, list):
            out.append(f' "{key}": [')
            out.extend('  ' + json.dumps(item, ensure_ascii=True) + (',' if n < len(value) - 1 else '')
                       for n, item in enumerate(value))
            out.append(' ]' + (',' if number < len(keys) - 1 else ''))
        else:
            out.append(f' "{key}": ' + json.dumps(value) + (',' if number < len(keys) - 1 else ''))
    out.append('}')
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text('\n'.join(out) + '\n')
    print(f'{OUT.relative_to(REPO)}: ' + ', '.join(f'{key} {len(value)}' for key, value in told.items()
                                                    if isinstance(value, list)))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
