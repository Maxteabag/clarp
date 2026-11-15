#!/usr/bin/env python3
"""Conformance of the Rust Host with the Python Host, on what a client can see.

  conform.py record  --build CHECKOUT --golden DIR
      Runs the corpus against the Python Host and writes down what it answered,
      normalized. Runs it twice: a reference that differs from itself proves nothing.
  conform.py plants  --build CHECKOUT --golden DIR
      Plants each change of contract_plants.py in a copy of the Python server and requires
      that it shows up against what was written down.
  conform.py compare --rust BINARY --golden DIR [--intended FILE]
      Runs the corpus against the Rust Host and reports every difference from
      what was written down.
  conform.py cross   --build CHECKOUT --rust BINARY --out DIR
      All three, for the workflow. The written-down answers go to DIR/golden.

record, plants and cross start Python test Hosts: they run only where the
replay harness may run (tests/replay/README.md), which is the contained
workflow, never a machine with a live Host. compare starts only the Rust Host.

It reuses the replay harness's capture, normalizer and differ as they are
(tests/replay), restricted to the two channels a client sees: `http` and `sse`.
The report prints structure, counts and codes, never message text.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import contextlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.request

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
# The harness's own modules first: its run.py imports `scenarios` and `planted` by those names.
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO / 'tests' / 'replay'))

import capture as replay  # noqa: E402  (tests/replay)
import diff  # noqa: E402
import normalize  # noqa: E402
import planted as harness_planted  # noqa: E402
import run as harness_run  # noqa: E402

import contract_plants as plants  # noqa: E402
import contract_scenarios as scenarios  # noqa: E402

# The harness's fake Claude, and one thing more: it writes the transcript the real CLI writes,
# which is where the Python Host reads a Claude agent's conversation from.
FAKE_CLAUDE = HERE / 'fake_claude.py'
SCRATCH = Path(os.environ.get('CLARP_CONFORM_SCRATCH', '/var/tmp/cfm'))
HTTP_OPS = ('get', 'post', 'put', 'delete', 'patch')
# What a normalized capture holds besides the two channels a client sees. Both sides get the
# same empty values, so the differ compares nothing there.
NO_STATE = {'schema': [], 'tables': {}, 'files': [], 'cli': [], 'sse_log': []}


def claude_shim(path: Path) -> None:
    """Makes `path` the program that runs this suite's fake Claude."""
    path.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(FAKE_CLAUDE))} "$@"\n')
    path.chmod(0o755)


class PythonHost(replay.Host):
    """The replay harness's own Host, with this suite's fake Claude in place of the harness's."""

    def start(self, config=None):
        super().start(config)
        # The harness put its fake first on the Host's PATH as `claude`. No turn has run yet.
        claude_shim(self.root / 'provider' / 'bin' / 'claude')
        return self


class RustHost(replay.Host):
    """The Rust Host behind the same fake Claude."""

    def start(self, config=None):
        (self.root / replay.OWNED).touch()
        provider = self.root / 'provider'
        for path in (provider / 'bin', self.root / 'workspace', self.root / 'home', self.root / 'tmp'):
            path.mkdir(parents=True)
        shim = provider / 'bin' / 'claude'
        claude_shim(shim)
        env = replay.child_env()
        env.update({'HOME': str(self.root / 'home'), 'TMPDIR': str(self.root / 'tmp'),
                    'CLARP_HOST_TOKEN': replay.TOKEN, 'CLARP_CLAUDE_PROGRAM': str(shim),
                    'CLARP_QA_PROVIDER_ROOT': str(provider)})
        self.log = (self.root.parent / f'{self.root.name}.host.log').open('a')
        self.process = subprocess.Popen(
            [str(self.build), '--data', str(self.root / 'data'), '--run', str(self.root / 'run'),
             '--cwd', str(self.root / 'workspace'), '--listen', '127.0.0.1:0', '--name', 'conformance'],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
            env=env, cwd=str(self.root), start_new_session=True)
        first = self.process.stderr.readline().decode(errors='replace')
        if not first.startswith('clarp-host listens on '):
            raise replay.StepFailed('the Rust Host did not say where it listens')
        self.url = 'http://' + first.split()[-1]
        log, stderr = self.log, self.process.stderr

        def drain():
            for line in stderr:
                with contextlib.suppress(ValueError):
                    log.write(line.decode(errors='replace'))
        threading.Thread(target=drain, daemon=True).start()
        # The agent the Python replay Host starts with, as far as this Host can run it.
        status, _, made = self.request('POST', '/agents', {
            'name': scenarios.AGENT.title(), 'backend': 'claude', 'cwd': str(self.root / 'workspace')})
        if status != 200 or (made or {}).get('session') != scenarios.AGENT:
            raise replay.StepFailed(f'the Rust Host did not make the agent ({status})')
        return self


class Events(replay.SSE):
    """The plain event stream, as a client that does not ask for live items gets it."""

    def __init__(self, host):
        self.frames, self.errors = [], []
        self.ended = False
        self.last_at = time.monotonic()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        request = urllib.request.Request(host.url + '/events', headers={'Authorization': f'Bearer {replay.TOKEN}'})
        self._resp = urllib.request.urlopen(request, timeout=60)
        self._thread = threading.Thread(target=self._read, daemon=True)
        self._thread.start()


def agent(host, session):
    """The agent's row in the snapshot: what a client knows about it."""
    body = host.request('GET', '/agents/snapshot')[2]
    for row in (body or {}).get('agents') or ():
        if row.get('session') == session:
            return row
    return {}


def turns(host, session):
    body = host.request('GET', f'/log?session={session}&limit=200')[2]
    return (body or {}).get('turns') or [] if isinstance(body, dict) else []


def run_step(host, events, step, http, variables):
    op = step['op']
    session = step.get('session', scenarios.AGENT)
    if op in HTTP_OPS:
        path = replay.fill(step['path'], variables)
        body = replay.fill(step.get('body'), variables)
        # Recorded under the step's own name, or its path as written: never a value a Host made up.
        named = {**step, 'name': step.get('name') or f"{op} {step['path']}", 'path': path}
        replay._record(named, host.request(op.upper(), path, body, None, step.get('headers')), http, variables)
    elif op == 'wait_reply':
        # The agent has answered the message with this id.
        def replied():
            seen = turns(host, session)
            at = next((i for i, turn in enumerate(seen) if turn.get('id') == step['after']), None)
            return at is not None and any(
                turn.get('role') == 'assistant' and (turn.get('text') or '').strip() for turn in seen[at + 1:])
        replay._poll('a reply', replied)
    elif op == 'wait_busy':
        replay._poll('busy state', lambda: agent(host, session).get('busy') is step['busy'])
    elif op == 'quiesce':
        # Nothing runs, nothing waits (unless it is held on purpose), and the stream is quiet.
        def idle():
            row = agent(host, session)
            return row.get('busy') is False and (step.get('held') or not row.get('queued_turn_count'))
        replay._poll('the agent idle', idle)
        replay._poll('the stream quiet', lambda: time.monotonic() - events.last_at >= replay.QUIET_S)
    else:
        raise replay.StepFailed(f'unknown step op {op!r}')


def capture(make_host, scenario, root: Path) -> dict:
    """Runs a scenario against a fresh Host in the new directory `root`."""
    replay.fresh_root(root)
    host = make_host(root)
    events, frames, http, error, variables = None, [], [], None, {}
    try:
        host.start()
        events = Events(host)
        replay._poll('the stream connected', lambda: events.frames)
        for index, step in enumerate(scenario['steps']):
            try:
                run_step(host, events, step, http, variables)
            except replay.StepFailed as exc:
                raise replay.StepFailed(f'step {index} ({step["op"]}): {exc}') from exc
        replay._poll('the stream quiet', lambda: time.monotonic() - events.last_at >= replay.QUIET_S)
    except replay.StepFailed as exc:
        error = str(exc)
    except OSError as exc:
        error = f'{type(exc).__name__} while running the scenario'
    finally:
        if events is not None:
            events.freeze()
        with contextlib.suppress(Exception):
            host.stop()
        if events is not None:
            frames = events.close()
    health = {'errors': events.errors if events else ['not connected'], 'ended_early': bool(events and events.ended)}
    if error is None and (health['errors'] or health['ended_early']):
        error = 'the event stream was unhealthy: ' + ', '.join(health['errors'] or ['ended early'])
    return {'timings': {}, 'scenario': scenario['id'], 'error': error, 'root': str(root), 'build': str(host.build),
            'http': http, 'sse': frames, 'sse_health': health,
            'db': {'schema': [], 'pk': {}, 'tables': {}}, 'files': [], 'cli': []}


EPOCH_MS = range(1_500_000_000_000, 2_000_000_000_000)


def settle(value, sessions):
    """Rewrites what differs between two runs of the same Host by nature, where the harness's
    normalizer does not know the field, and leaves out the agents a scenario is not about."""
    if isinstance(value, list):
        return [settle(item, sessions) for item in value]
    if not isinstance(value, dict):
        return value
    out = {}
    for key, item in value.items():
        if key == 'runtime_instance' and isinstance(item, str) and item:
            item = '<instance>'  # one per Host process
        elif key == 'turn_started_at' and isinstance(item, int) and item in EPOCH_MS:
            item = '<time>'  # a clock reading while a turn runs, 0 when none does
        elif key == 'agents' and isinstance(item, list):
            # The reference Host starts with two codex agents (rachel, mike) that the Rust Host
            # cannot run until it has a Codex adapter. Both sides are cut to the scenario's own.
            item = [row for row in item if not isinstance(row, dict) or row.get('session') in sessions]
        elif key == 'roster' and isinstance(item, list):
            item = [name for name in item if isinstance(name, str) and name.lower() in sessions]
        out[key] = settle(item, sessions)
    return out


def seen_by_a_client(raw: dict) -> dict:
    """A capture, normalized, and cut down to the channels a client sees."""
    sessions = (scenarios.AGENT,)
    raw = {**raw, 'http': [{**exchange, 'body': settle(exchange['body'], sessions)} for exchange in raw['http']]}
    whole = normalize.normalize(raw, str(REPO))
    return {'scenario': whole['scenario'], 'error': whole['error'], 'http': whole['http'], 'sse': whole['sse'],
            'live': whole.get('live', {})}


def comparable(seen: dict) -> dict:
    return {**NO_STATE, **seen}


class Run:
    """One run's own scratch directory. Nothing in it is reused, and it is removed at the end
    unless the run failed or was asked to keep it."""

    def __init__(self):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        self.path = SCRATCH / f'{int(time.time())}-{os.getpid()}'
        self.path.mkdir()
        self.labels = 0

    def label(self, name: str) -> Path:
        self.labels += 1
        path = self.path / f'{self.labels:02d}-{name}'
        path.mkdir()
        return path

    def close(self, keep: bool):
        if keep:
            print(f'kept: {self.path}')
        else:
            shutil.rmtree(self.path, ignore_errors=True)


def corpus(make_host, chosen, where: Path, jobs: int) -> dict:
    """Every chosen scenario against its own fresh Host; scenario id -> what a client saw."""
    def one(item):
        index, scenario = item
        return scenario['id'], seen_by_a_client(capture(make_host, scenario, where / f's{index:02d}'))
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        return dict(pool.map(one, enumerate(chosen)))


def differences(a: dict, b: dict, chosen) -> diff.Report:
    report = diff.Report()
    for scenario in chosen:
        diff.compare(comparable(a[scenario['id']]), comparable(b[scenario['id']]), report)
    return report


def run_errors(seen: dict, label: str) -> list:
    return [f'{label} / {scenario}: {capture["error"]}' for scenario, capture in sorted(seen.items())
            if capture['error']]


def python_host(build: Path):
    return lambda root: PythonHost(build, root)


def rust_host(binary: Path):
    return lambda root: RustHost(binary, root)


def load_golden(directory: Path, chosen) -> dict:
    golden = {}
    for scenario in chosen:
        path = directory / f"{scenario['id']}.json"
        if not path.is_file():
            raise SystemExit(f'nothing is written down for {scenario["id"]} in {directory}: record it first')
        golden[scenario['id']] = json.loads(path.read_text())
    return golden


def write_golden(directory: Path, seen: dict):
    directory.mkdir(parents=True, exist_ok=True)
    for scenario, capture in seen.items():
        (directory / f'{scenario}.json').write_text(json.dumps(capture, indent=1, sort_keys=True) + '\n')


def load_intended(path) -> set:
    if not path or not Path(path).is_file():
        return set()
    return {line.split()[0] for line in Path(path).read_text().splitlines()
            if line.strip() and not line.lstrip().startswith('#')}


def record(build: Path, chosen, run: Run, jobs: int) -> tuple:
    """The Python Host's answers, twice. Returns (what a client saw, lines of the report, ok)."""
    harness_run.pin_static_mtimes(build)
    first = corpus(python_host(build), chosen, run.label('py-a'), jobs)
    second = corpus(python_host(build), chosen, run.label('py-b'), jobs)
    errors = run_errors(first, 'python') + run_errors(second, 'python again')
    report = differences(first, second, chosen)
    lines = [diff.render(report, set(), 'the Python Host against itself', errors)]
    ok = not errors and not report.items
    lines.append('REFERENCE REPRODUCIBLE' if ok else 'REFERENCE NOT REPRODUCIBLE: nothing below can be trusted')
    return first, lines, ok


def planted_copy(build: Path, change: dict, where: Path) -> Path:
    """A copy of the build's server with one change planted; static and bin are shared."""
    if change.get('from_harness'):
        change = {**harness_planted.BY_ID[change['from_harness']], 'id': change['id']}
    shutil.copytree(build / 'server', where / 'server', ignore=shutil.ignore_patterns('__pycache__'))
    for shared in ('static', 'bin'):
        (where / shared).symlink_to(build / shared)
    harness_planted.apply(change, where)
    return where


def caught(change: dict, found: list) -> list:
    """The differences that show this planted change, and not just any difference."""
    want = change['expect']
    pattern = re.compile(want['locator'])
    return [d for d in found
            if (want['channel'] is None or d['channel'] == want['channel'])
            and (want['kinds'] is None or d['kind'] in want['kinds'])
            and pattern.search(d['locator'])]


def prove(build: Path, golden: dict, chosen, changes, run: Run, jobs: int) -> tuple:
    """Every planted change must show against what was written down."""
    lines, ok = ['# Planted changes', ''], True
    for change in changes:
        copy = planted_copy(build, change, run.label(f"plant-{change['id']}"))
        seen = corpus(python_host(copy), chosen, run.label(f"run-{change['id']}"), jobs)
        errors = run_errors(seen, change['id'])
        report = differences(golden, seen, chosen)
        hits = caught(change, report.items)
        verdict = 'CAUGHT' if hits and not errors else 'MISSED'
        ok = ok and verdict == 'CAUGHT'
        lines.append(f"{verdict:<7} {change['id']}: {change['what']} "
                     f"({len(hits)} of {len(report.items)} differences show it)")
        lines += [f'        RUN ERROR {line}' for line in errors]
        if verdict == 'MISSED':
            lines += [f"        saw {d['scenario']} / {d['channel']} / {d['locator']} / {d['kind']}"
                      for d in report.items[:12]]
    lines.append('')
    lines.append('EVERY PLANTED CHANGE WAS CAUGHT' if ok else 'A PLANTED CHANGE WAS MISSED: the suite cannot be trusted')
    return lines, ok


def against_rust(binary: Path, golden: dict, chosen, intended: set, run: Run, jobs: int) -> tuple:
    seen = corpus(rust_host(binary), chosen, run.label('rust'), jobs)
    errors = run_errors(seen, 'rust')
    report = differences(golden, seen, chosen)
    unexplained = [d for d in report.items if d['id'] not in intended]
    lines = [diff.render(report, intended, 'the Python Host (base) against the Rust Host (head)', errors)]
    lines += ['', '# The same differences by shape', *diff.shapes(report, top=200)]
    ok = not errors and not unexplained
    lines.append('')
    lines.append('THE RUST HOST CONFORMS' if ok else
                 f'THE RUST HOST DIFFERS: {len(unexplained)} unexplained difference(s), {len(errors)} run error(s)')
    return lines, ok, seen


def choose(names):
    if not names:
        return scenarios.SCENARIOS
    unknown = [name for name in names.split(',') if name not in scenarios.BY_ID]
    if unknown:
        raise SystemExit(f'unknown scenario(s): {", ".join(unknown)}')
    return [scenarios.BY_ID[name] for name in names.split(',')]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('mode', choices=('record', 'plants', 'compare', 'cross'))
    parser.add_argument('--build', type=Path, help='a checkout of the repository: the Python Host under ./server')
    parser.add_argument('--rust', type=Path, help='the clarp-host binary (clarp-shim beside it)')
    parser.add_argument('--golden', type=Path, default=HERE / 'golden')
    parser.add_argument('--out', type=Path, help='cross: where the report and the written-down answers go')
    parser.add_argument('--intended', default=str(HERE / 'intended.txt'))
    parser.add_argument('--scenarios', help='a comma-separated subset of the corpus')
    parser.add_argument('--jobs', type=int, default=4)
    parser.add_argument('--keep', action='store_true', help='keep the scratch directory')
    args = parser.parse_args(argv)
    chosen = choose(args.scenarios)
    for needed, modes in (('build', ('record', 'plants', 'cross')), ('rust', ('compare', 'cross')),
                          ('out', ('cross',))):
        if args.mode in modes and getattr(args, needed) is None:
            parser.error(f'{args.mode} needs --{needed}')
    build = args.build.resolve() if args.build else None
    rust = args.rust.resolve() if args.rust else None
    run = Run()
    ok, lines = True, []
    try:
        if args.mode == 'record':
            seen, lines, ok = record(build, chosen, run, args.jobs)
            if ok:
                write_golden(args.golden, seen)
                lines.append(f'written down: {len(seen)} scenario(s) in {args.golden}')
        elif args.mode == 'plants':
            lines, ok = prove(build, load_golden(args.golden, chosen), chosen, plants.CHANGES, run, args.jobs)
        elif args.mode == 'compare':
            lines, ok, _ = against_rust(rust, load_golden(args.golden, chosen), chosen,
                                        load_intended(args.intended), run, args.jobs)
        else:
            args.out.mkdir(parents=True, exist_ok=True)
            seen, lines, ok = record(build, chosen, run, args.jobs)
            write_golden(args.out / 'golden', seen)
            if ok:
                proof, proved = prove(build, seen, chosen, plants.CHANGES, run, args.jobs)
                rust_lines, conforms, rust_seen = against_rust(
                    rust, seen, chosen, load_intended(args.intended), run, args.jobs)
                write_golden(args.out / 'rust', rust_seen)
                lines += ['', *proof, '', *rust_lines]
                ok = proved and conforms
            (args.out / 'report.txt').write_text('\n'.join(lines) + '\n')
    finally:
        run.close(args.keep or not ok)
    print('\n'.join(lines))
    return 0 if ok else 1


if __name__ == '__main__':
    with contextlib.suppress(AttributeError):
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    sys.exit(main())
