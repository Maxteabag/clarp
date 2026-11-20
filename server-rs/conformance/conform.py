#!/usr/bin/env python3
"""Conformance of the Rust Host with the Python Host, on what a client can see.

  conform.py record  --build CHECKOUT --golden DIR
      Runs the corpus against the Python Host and writes down what it answered,
      normalized. Runs it twice: a reference that differs from itself proves nothing.
  conform.py plants  --build CHECKOUT --golden DIR
      Plants each change of contract_plants.py in a copy of the Python server and requires
      that it shows up against what was written down.
  conform.py compare --rust BINARY --golden DIR [--intended FILE] [--awaited FILE]
      Runs the corpus against the Rust Host and reports every difference from
      what was written down.
  conform.py cross   --build CHECKOUT --rust BINARY --out DIR
      All three, for the workflow. The written-down answers go to DIR/golden. Before a
      difference is called unexplained the reference is asked again (`asked_again`), and
      its answers go to DIR/golden-asked-<try>.
  conform.py rust    --rust BINARY --out DIR
      Runs the corpus against the Rust Host and writes down what it answered, compared
      with nothing: for a scenario that is new and has no reference yet.
  conform.py selftest
      Tries the tool's own rule for a reference that answers in more than one way on
      captures made for it. Starts no Host. record and cross do it first, by themselves.

record, plants and cross start Python test Hosts: they run only where the
replay harness may run (tests/replay/README.md), which is the contained
workflow, never a machine with a live Host. compare starts only the Rust Host.

It reuses the replay harness's capture, normalizer and differ as they are
(tests/replay), restricted to the two channels a client sees: `http` and `sse`.
The report prints structure, counts and codes, never message text.
"""
from __future__ import annotations

import argparse
import bisect
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
import urllib.parse
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import scratch as tool_scratch

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
# The agents the reference Host has besides the scenarios' own (tests/replay/host.py, SESSIONS).
OTHER_AGENTS = ('rachel', 'mike')
HTTP_OPS = ('get', 'post', 'put', 'delete', 'patch')
# How long a conversation must stay as it is before it counts as settled. The Python Host goes on
# working after a turn has ended: it imports the tool's own transcript and folds it into the rows
# it wrote while the turn ran. A read before that is done sees a state no client is meant to keep.
SETTLED_S = 3.0
SETTLE_TIMEOUT_S = 30
# Where what a test Host left of a scenario it did not run to its end is written: under the
# directory of the report (`--out`), when the run has one.
UNFINISHED = None
ASKED_LINE = re.compile(r'^\S+ "(?:GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS) \S+ HTTP/[0-9.]+" \d{3} ')


def diagnose(host, root: Path, scenario: str, error: str) -> None:
    """What a test Host left of a scenario it did not run to its end, for whoever reads the
    report: the agent's last states (the reference), what its stand-in tool was started for
    and how that went, and the end of the Host's own log. As synthetic as the corpus."""
    if UNFINISHED is None:
        return
    said = [f'{root.parent.name} / {scenario}: {error}']
    if isinstance(host, PythonHost):
        said.append(replay._state_tail(root, scenarios.AGENT))
    for title, path, most in (('the stand-in tool', root / 'provider' / 'claude-procs.jsonl', 40),
                              ('the Host', root.parent / f'{root.name}.host.log', 150)):
        try:
            written = path.read_text(errors='replace').splitlines()
        except OSError:
            written = []
        # What the Host wrote of each request it answered is left out: a wait asks it many
        # times a second, and those lines would be all there is.
        lines = [line for line in written if not ASKED_LINE.match(line)]
        said += ['', f'# {title}: the last {min(most, len(lines))} of {len(lines)} line(s)'
                 + (f' ({len(written) - len(lines)} more of requests it answered)' if len(written) > len(lines)
                    else ''), *lines[-most:]]
    with contextlib.suppress(OSError):
        UNFINISHED.mkdir(parents=True, exist_ok=True)
        (UNFINISHED / f'{root.parent.name}-{scenario}.txt').write_text('\n'.join(said) + '\n')
# What a normalized capture holds besides the two channels a client sees. Both sides get the
# same empty values, so the differ compares nothing there.
NO_STATE = {'schema': [], 'tables': {}, 'files': [], 'cli': [], 'sse_log': []}


def claude_shim(path: Path, pace: str | None = None) -> None:
    """Makes `path` the program that runs this suite's fake Claude. `pace`: how long the
    stand-in takes before it does anything in a first turn, where its Host is given another
    moment than the stand-in's own."""
    # The moment can be said by whoever runs the suite (`CLARP_QA_ANSWER_AFTER`, to try
    # another): a test Host hands its tools only the environment it makes for them.
    pace = os.environ.get('CLARP_QA_ANSWER_AFTER', '') if pace is None else pace
    env = f'CLARP_QA_ANSWER_AFTER={shlex.quote(pace)} ' if pace else ''
    path.write_text(f'#!/bin/sh\n{env}exec {shlex.quote(sys.executable)} {shlex.quote(str(FAKE_CLAUDE))} "$@"\n')
    path.chmod(0o755)


class PythonHost(replay.Host):
    """The replay harness's own Host, with this suite's fake Claude in place of the harness's."""

    def start(self, config=None):
        super().start(config)
        # The harness put its fake first on the Host's PATH as `claude`. No turn has run yet.
        claude_shim(self.root / 'provider' / 'bin' / 'claude')
        return self


# What the reference Host's stand-in for an engine that listens hears in every clip.
HEARD = scenarios.HEARD


# What the reference Host's config says when a scenario brings none, and where the Rust Host is
# given the same. No scenario turns speech out on: the reference speaks what a Claude agent says
# from the transcript Claude Code writes, and the harness's fake Claude writes none, so the
# reference says nothing aloud here and there is no clip to compare. What a clip is and how it
# is fetched is held by the Rust Host's own tests (crates/clarp-host/tests/host.rs, with a
# stand-in for the engine) and by its rules' vectors.
SPEECH_OFF = '[tts]\nprovider = "none"\n[network]\nadvertise_lan = false\n'
OWNERS_FILE = 'owner-config.toml'


class RustHost(replay.Host):
    """The Rust Host behind the same fake Claude."""

    def start(self, config=None):
        (self.root / replay.OWNED).touch()
        provider = self.root / 'provider'
        for path in (provider / 'bin', self.root / 'workspace', self.root / 'home', self.root / 'tmp'):
            path.mkdir(parents=True)
        # The same stand-in as the reference's, with the moment it takes before a first turn.
        # The moment keeps the reference out of a race of its own (R43), which this Host does
        # not have; but both Hosts are asked through the same tool. For one landing this Host
        # was asked without it: with the moment, the stand-in's turn, which it prints in one
        # breath, begins while this Host is already reading and may be read in two, and what
        # this Host told of the agent's states depended on that (run 44111344344, 29
        # differences). It no longer does (REBUILD.md, section 7). Whoever runs the suite can
        # give this Host another moment, or none (`CLARP_QA_ANSWER_AFTER_RUST=0`), to try that.
        claude_shim(provider / 'bin' / 'claude', os.environ.get('CLARP_QA_ANSWER_AFTER_RUST'))
        # The owner's file of settings, of the reference Host's kind: what a scenario gives the
        # reference Host as its config, this Host reads its owner's choices of speech from. With
        # none, speech out is off on both (tests/replay/host.py, isolate).
        (self.root / OWNERS_FILE).write_text(config or SPEECH_OFF)
        self.log = (self.root.parent / f'{self.root.name}.host.log').open('a')
        # The reference Host has its agents before it serves its first request. This one is given
        # the same agents and then started again, so that making them is not part of what a client
        # sees of the scenario. The scenarios are about the first; the other two are the reference
        # Host's codex agents, which no scenario runs: they are there to be other agents.
        # The reference Host's agents are written into its table without a voice. An agent that is
        # made as a persona takes that persona's voices, on both Hosts, and the other two are
        # named as personas: they are made with the word for no voice, which is what they have
        # there.
        self._launch()
        for session, backend in ((scenarios.AGENT, 'claude'), *((other, 'codex') for other in OTHER_AGENTS)):
            status, _, made = self.request('POST', '/agents', {
                'name': session.title(), 'backend': backend, 'cwd': str(self.root / 'workspace'),
                **({'voice_id': '{}'} if session in OTHER_AGENTS else {})})
            if status != 200 or (made or {}).get('session') != session:
                raise replay.StepFailed(f'the Rust Host did not make the agent {session} ({status})')
        self._end()
        self._launch()
        # The reference Host's stand-in for an engine that listens is there from its first
        # moment. This Host's is a worker that says when it is ready: it is waited for here, so
        # that a model being loaded is not part of what a client sees of a scenario.
        clip = {'bytes': 'is the engine there', 'content_type': 'audio/webm'}
        replay._poll('the engine that listens', lambda: self.request('POST', '/transcribe', None, clip)[0] != 503)
        return self

    def _launch(self):
        provider = self.root / 'provider'
        env = replay.child_env()
        env.update({'HOME': str(self.root / 'home'), 'TMPDIR': str(self.root / 'tmp'),
                    'CLARP_HOST_TOKEN': replay.TOKEN, 'CLARP_CLAUDE_PROGRAM': str(provider / 'bin' / 'claude'),
                    'CLARP_QA_PROVIDER_ROOT': str(provider),
                    # Speech in: the Host's own worker with no model in it, which hears in every
                    # clip what the reference Host's stand-in hears (tests/replay/host.py). No key
                    # of an engine outside is in this Host's world.
                    'CLARP_WHISPER_FAKE_SAYS': HEARD, 'ELEVEN_API_KEY': '',
                    # The reference's harness turns the heartbeats of its Hosts off (`host.isolate`):
                    # this Host is told the same.
                    'CLARP_HEARTBEATS_DISABLED': '1'})
        self.process = subprocess.Popen(
            [str(self.build), '--data', str(self.root / 'data'), '--run', str(self.root / 'run'),
             '--cwd', str(self.root / 'home'), '--listen', '127.0.0.1:0',
             '--whisper-python', sys.executable, '--whisper-worker', str(HERE.parent / 'tools' / 'whisper-worker.py'),
             '--speech-keys', str(self.root / OWNERS_FILE),
             # Where a scenario's owner has a selector of accounts, this Host runs it, as the
             # reference Host does; without one the mode changes nothing.
             '--accounts', 'select',
             # The pictures agents are shown with: the repository's own, as the reference Host has them.
             '--static', str(REPO / 'static')],
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

    def _end(self):
        with contextlib.suppress(ProcessLookupError):
            os.killpg(self.process.pid, signal.SIGKILL)
        self.process.wait(timeout=5)
        self.process = None


class Events(replay.SSE):
    """The event stream: plain, as a client that does not ask for live items gets it, or, for
    a scenario that watches turns as they run (`live`), with the live items of those agents
    (`/events?live=`, docs/live-items.md)."""

    def __init__(self, host, live=None):
        self.frames, self.errors = [], []
        self.ended = False
        self.last_at = time.monotonic()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        asked = '/events' if live is None else '/events?live=' + ','.join(live)
        request = urllib.request.Request(host.url + asked, headers={'Authorization': f'Bearer {replay.TOKEN}'})
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


def dig(value, dotted: str, name: str):
    """A value out of an answer, by a path such as `artifact.decision.decision_id` or `items.0.id`."""
    for part in dotted.split('.'):
        value = value[int(part)] if isinstance(value, list) and part.isdigit() and int(part) < len(value) \
            else (value or {}).get(part) if isinstance(value, dict) else None
    if value is None:
        raise replay.StepFailed(f'no {name} in the answer before')
    return value


class TurnClock:
    """What `turn_started_at` is told as: which turn's start it is, and after which send.

    An app counts a running turn from this field (issue 354). What has to agree between two
    Hosts is not the number, which is a reading of a clock, but what it is a reading of: one
    and the same moment for as long as a turn runs, whatever else is sent meanwhile, and another
    moment for the next turn. So a reading is written down as the turn it belongs to (the
    readings of a capture, numbered as they are first seen) and by the last message this client
    had sent when the turn began: a turn begins after the send that started it and before every
    later one. Host and client read the same clock, so no allowance is made."""

    def __init__(self):
        self.sent: list[int] = []     # when each message the Host took was sent, in ms
        self.turns: dict[int, int] = {}  # a start -> the number of the turn it was first read as

    def read(self, exchange: dict, asked_at: int) -> None:
        """Takes an exchange that was just recorded and was asked for at `asked_at`."""
        if (exchange['method'], exchange['path'], exchange['status']) == ('POST', '/send', 200):
            self.sent.append(asked_at)
        exchange['body'] = self.told(exchange['body'])

    def told(self, value):
        if isinstance(value, list):
            return [self.told(item) for item in value]
        if not isinstance(value, dict):
            return value
        out = {}
        for key, item in value.items():
            if key == 'turn_started_at' and isinstance(item, int) and not isinstance(item, bool) and item in EPOCH_MS:
                turn = self.turns.setdefault(item, len(self.turns) + 1)
                sends = sum(1 for at in self.sent if at <= item)
                item = (f'<turn {turn}, begun after send {sends}>' if sends
                        else f'<turn {turn}, begun before anything was sent>')
            else:
                item = self.told(item)
            out[key] = item
        return out


def run_step(host, events, step, http, variables, clock=None):
    op = step['op']
    # The agent a step waits for: the scenario's own, or one a step saved the name of (`{name}`): a
    # Host chooses the session name of an agent it makes.
    session = replay.fill(step.get('session', scenarios.AGENT), variables)
    if op in HTTP_OPS:
        last = http[-1]['body'] if http else None
        path_values = dict(variables)
        # {name} in the path, and fields of the body, taken from the answer before: an id the
        # Host made, the time an artifact last changed.
        for name, dotted in (step.get('from_last') or {}).items():
            path_values[name] = urllib.parse.quote(str(dig(last, dotted, name)), safe='')
        path = replay.fill(step['path'], path_values)
        body = replay.fill(step.get('body'), variables)
        # A body may say when it is sent (`body_now`: the names of its fields): an app says
        # when it sent what it says, and a Host does not take what is old.
        for name in step.get('body_now') or ():
            body = {**(body or {}), name: int(time.time() * 1000)}
        if step.get('body_from_last'):
            body = {**(body or {}), **{name: dig(last, dotted, name)
                                       for name, dotted in step['body_from_last'].items()}}
        # Recorded under the step's own name, or its path as written: never a value a Host made up.
        named = {**step, 'name': step.get('name') or f"{op} {step['path']}", 'path': path}
        asked_at = int(time.time() * 1000)
        # A step may send a body as it is instead of JSON (`raw`: its text and its declared type):
        # a file an agent publishes.
        replay._record(named, host.request(op.upper(), path, body, step.get('raw'), step.get('headers')),
                       http, variables)
        if clock is not None:
            clock.read(http[-1], asked_at)
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
    elif op == 'wait_running':
        # A turn runs and nothing waits behind it: the agent is busy, its queue is empty, and what
        # the snapshot says the turn began at is the same in two readings. Read off the snapshot
        # alone: when a message enters the conversation is each Host's own.
        last = {'began': None}

        def running():
            row = agent(host, session)
            began = row.get('turn_started_at')
            steady = (row.get('busy') is True and not row.get('queued_turn_count')
                      and bool(began) and began == last['began'])
            last['began'] = began
            return steady
        replay._poll('the turn running', running)
    elif op == 'quiesce':
        # Nothing runs, nothing waits (unless it is held on purpose), and the stream is quiet.
        def idle():
            row = agent(host, session)
            return row.get('busy') is False and (step.get('held') or not row.get('queued_turn_count'))
        replay._poll('the agent idle', idle)
        replay._poll('the stream quiet', lambda: time.monotonic() - events.last_at >= replay.QUIET_S)
        # And the conversation has stopped changing, as has what the snapshot says of the agent.
        last = {'seen': None, 'since': time.monotonic()}

        def settled():
            row = agent(host, session)
            seen = json.dumps([turns(host, session), row.get('head_revision'), row.get('latest_state')],
                              sort_keys=True)
            if seen != last['seen']:
                last['seen'], last['since'] = seen, time.monotonic()
                return False
            return time.monotonic() - last['since'] >= SETTLED_S
        replay._poll('the conversation settled', settled, timeout=SETTLE_TIMEOUT_S)
        replay._poll('the stream quiet', lambda: time.monotonic() - events.last_at >= replay.QUIET_S)
    else:
        raise replay.StepFailed(f'unknown step op {op!r}')


def capture(make_host, scenario, root: Path) -> dict:
    """Runs a scenario against a fresh Host in the new directory `root`."""
    replay.fresh_root(root)
    host = make_host(root)
    # A scenario names a directory of the Host it runs against as `{workspace}`.
    events, frames, http, error, variables = None, [], [], None, {'workspace': str(root / 'workspace')}
    clock = TurnClock()
    try:
        # A scenario may bring the owner's settings its Host runs with (`config`).
        host.start(scenario.get('config'))
        # A scenario that watches turns as they run names the agents it watches.
        watched = tuple(scenario.get('sessions', (scenarios.AGENT,))) if scenario.get('live') else None
        events = Events(host, watched)
        replay._poll('the stream connected', lambda: events.frames)
        for index, step in enumerate(scenario['steps']):
            try:
                run_step(host, events, step, http, variables, clock)
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
    if error is not None:
        diagnose(host, root, scenario['id'], error)
    return {'timings': {}, 'scenario': scenario['id'], 'error': error, 'root': str(root), 'build': str(host.build),
            'http': http, 'sse': frames, 'sse_health': health,
            'db': {'schema': [], 'pk': {}, 'tables': {}}, 'files': [], 'cli': []}


EPOCH_MS = range(1_500_000_000_000, 2_000_000_000_000)
# What belongs to a feature of the contract the Rust Host does not have yet (it leaves the
# feature out of `capabilities.features`, which is how an app learns not to expect it). These
# fields and events are left out of both sides before the Rust Host is compared: they are not
# differences to explain one by one, they are one feature that is not there yet. The register
# of what is not there yet is server-rs/docs/differences.md.
NOT_YET = {
    # What an agent sounds like and how it is heard, as the Host resolves them (the Python Host's
    # PR 485, its contract 109): `voice_profile` on an agent's row of the snapshot, on the
    # creation answer and on the roster's events; `voice_defaults` and `voice_profile_version` on
    # the snapshot's head; and the two kinds of roster event that only carry it (`voice-changed`
    # for one agent, `voice-profile` for a change of the whole Host). An event is named here by
    # its type and kind. The port to the Rust Host is owed; its contract stays 106 until it lands.
    'voice_profile': {
        'fields': ('voice_profile', 'voice_defaults', 'voice_profile_version'),
        'events': ('agent-roster/voice-changed', 'agent-roster/voice-profile'),
    },
    # Why a reply was not spoken (`reason`, `provider` and `trace_id` on `tts-error`, once a turn,
    # also when no provider had a voice for the agent; the same PR): no scenario of the corpus
    # has a reply that is not spoken, and those names are too common to be left out everywhere,
    # so nothing is left out for it. The port is owed with the one above.
    'speech_outcome': {
        'fields': (),
        'events': (),
    },
}
# Parts of the contract no feature flag stands for, which the Rust Host does not have yet: it
# sends them empty. Left out of both sides the same way, and listed in the same document.
NOT_BUILT = {
    # What `GET /status` says of the Python Host's own way of telling whether a turn's process
    # is still its own (budgets, a gate, counts of probes): no client reads it, and the Rust
    # Host has no such machinery to tell of.
    'the probes of the Python Host': {
        'fields': ('delivery_admission',),
        'events': (),
    },
    # Which run of the vocabulary a clip was heard with (`vocab_run_id` in the answer to
    # `POST /transcribe`): the Rust Host asks an engine to listen for what its agents are called
    # and keeps no runs of a vocabulary.
    'the vocabulary an engine is asked to listen for': {
        'fields': ('vocab_run_id',),
        'events': (),
    },
    # Whether a tool's use is explained in plain words, as the snapshot and `GET /live` tell an
    # app (`tool_explanations`): on, on the Python Host, unless its owner turned it off; off on
    # the Rust Host, where nothing explains. What the setting's own route answers is compared
    # (the scenario live-items turns it off on both).
    "the explaining of a tool's use": {
        'fields': ('tool_explanations',),
        'events': (),
    },
    # Whom a row, a live turn and a dispatch's working state are for (`audience`: 'agents' for a
    # turn another agent's message started, else 'user'), the coordination between agents of the
    # Python Host's PR 482 (its contract 108); no feature stands for it until `hidden_coordination`
    # is advertised. A name no other answer carries. The port to the Rust Host is owed; its
    # contract stays 107 until it lands.
    'coordination between agents': {
        'fields': ('audience',),
        'events': (),
    },
}
# What the reference Host's stand-in for an engine that listens says by fiat, whatever was heard
# (`StubSTT(ends_terminal=True)` in tests/replay/host.py): whether what was said ends as a
# sentence ends. A real engine's answer reads it off the text, and so does the Rust Host's;
# that rule is held by the speech vectors (crates/clarp-kernel/tests/speech_vectors.rs), not here.
BY_FIAT = {'/transcribe': ('ends_terminal',)}


def by_fiat(path, body):
    """A body without what the reference's stand-in decides by itself."""
    said = BY_FIAT.get(path, ())
    if not said or not isinstance(body, dict):
        return body
    return {key: value for key, value in body.items() if key not in said}


# Lists whose order is an accident of the reference, compared in the order of their names: the
# contacts an agent may take (`POST /agent-assign` with `options`) come in the order the Python
# Host lists its personas, by the millisecond each built-in one was written at its start, which
# moves from start to start (docs/differences.md, "The order of the personas a Host offers").
IN_ANY_ORDER = {'/agent-assign': 'contacts'}


def in_any_order(path, body):
    """A body with a list of no order of its own sorted by its names."""
    key = IN_ANY_ORDER.get(path)
    if not key or not isinstance(body, dict) or not isinstance(body.get(key), list):
        return body
    named = lambda item: str(item.get('name')) if isinstance(item, dict) else str(item)  # noqa: E731
    return {**body, key: sorted(body[key], key=named)}
# The lists an app's screens are made of, for what the Rust Host keeps nothing of yet: janitors,
# the engines that listen as a Host with other engines lists them, the models an agent can be
# given. (The catalogue of voices is not among them any more: it is compared whole.) It answers each as the Python Host
# answers a list, and empty, so that an app shows nothing there instead of an error. What is
# compared of such an answer is what an app's reader needs of it: the status, and every field
# with its kind. What the fields hold is in server-rs/docs/differences.md as not built.
NOT_KEPT = frozenset({'/janitors', '/transcription-capabilities', '/transcription-providers', '/agent-model-options',
                      # The Oracle's available functions depend on the installed providers.
                      '/oracle/status'})
ABSENT = (*NOT_YET.values(), *NOT_BUILT.values())
NOT_YET_FIELDS = frozenset(field for part in ABSENT for field in part['fields'])
NOT_YET_EVENTS = frozenset(event for part in ABSENT for event in part['events'])
# A conversation's revisions are what a client syncs by: it holds a cursor (the head an answer
# gave it) and asks what changed since. The two Hosts count differently, and the Python Host
# writes rows that change in the same breath in either order from run to run (reference defects
# R8 and R16), so a revision is compared as what a client can tell of it: a head as its place
# among the cursors of its capture, a row's revision as how many of those cursors it is newer
# than. A queue's revision is another thing: both Hosts count the same changes, and it is
# compared as the number it is.
HEAD_KEYS = frozenset({'latest_revision', 'head_revision'})
# Readings of the clock the harness's normalizer has no name for: when something an agent made
# was pinned, put away or thrown away, and when what it asked was answered. Whether there is
# one is compared; which moment it was is not.
CLOCK_KEYS = frozenset({'pinned_at', 'archived_at', 'deleted_at', 'resolved_at', 'delivered_at',
                        'helper_completed_at', 'server_now',
                        # A render spec's answers: when they were taken.
                        'accepted_at',
                        # A call: when an agent joined it and when its state last changed.
                        'joined_at', 'changed_at',
                        # A schedule: when it runs next. Which minute that is, is held by vectors.
                        'next_run_at',
                        # A heartbeat: when the last was started and when the next is due. How far
                        # apart they are is held by vectors and by the Host's own tests.
                        'last_started_at', 'next_heartbeat_at',
                        # The account seats: when the Host answered.
                        'served_at'})
# The ids a Host gives to what agents make and ask, and to a persona its owner makes. Under their
# own keys the harness's normalizer names them by the order they appear in (`artifact-<id:1>`).
# They are also in the paths a client calls (`/decisions/<id>/resolve`), in what an agent is told
# and says back (`Decision ID: ...`) and in where a persona's own picture is fetched
# (`avatar_url`), and there the normalizer leaves them: hex in free text is content to it.
ISSUED = re.compile(r'^(?:artifact|decision|persona)-[0-9a-f]{12,64}$')
# An id a preview cut short: `Decision ID: decision-7be0a016ae…`.
CUT_ID = re.compile(r'((?:artifact|decision)-[0-9a-f]{4,})…$')
# The id a Host gives a file an agent published: `asset_` and twenty-four signs of an alphabet
# that has an underscore in it. The harness's normalizer names such an id under its own key only
# when it happens to have no underscore, which about half of them do; and the id is also in what
# a client is told to fetch (`url`, `uri`), in what an agent writes into a chat (`markdown`) and
# in the path a client then asks for. So these are named here, before the normalizer reads the
# capture, in the order a client first learns of them.
ASSET = re.compile(r'asset_[A-Za-z0-9_]{24}')
# The id a Host gives a call: `gcl_` and thirty-two hex signs. It is in the call's record, in
# every event of the call, in what a refusal of a message of the call names, and in the ids of
# the notices in the agents' conversations. Named here in the order a client first learns of
# them, as the files are.
CALL = re.compile(r'gcl_[0-9a-f]{32}')
# The id a Host gives a ring: `rng_` and thirty-two hex signs. It is in the ring's record, in every
# event of the ring, in the path a client reads it by, in the trail, and in the id of the notice
# in the agent's conversation. Named as the calls are.
RING = re.compile(r'rng_[0-9a-f]{32}')
# The id a Host gives a schedule: `sched_` and twelve hex signs. It is in the schedule's record and
# in the path a client changes and removes it by. Named as the calls are.
SCHEDULE = re.compile(r'sched_[0-9a-f]{12}')
# The id a Host gives a portrait an agent keeps: `portrait_` and twenty-four signs for one chosen
# from what the agent published, and `portrait_legacy_`, the agent's id and the beginning of the
# picture's digest for the picture an agent wears. It is in the list of an agent's portraits, in
# where a portrait's picture is fetched, and in what a client names when it chooses one. Named as
# the calls are.
PORTRAIT = re.compile(r'portrait_(?:legacy_[0-9a-f]+_[0-9a-f]{24}|[A-Za-z0-9_]{24})')
# The clip of a ring's opening line, where a Host voiced it: the name of its file begins with the
# moment it was made. The harness's normalizer names such a file only under the keys it knows.
RING_CLIP = re.compile(r'/audio/1[5-9][0-9]{11}(__[A-Za-z0-9._-]+\.mp3)')


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
        elif key == 'host_id' and isinstance(item, str) and item:
            item = '<host>'  # a call says which Host keeps it: each Host's own id
        elif key == 'turn_started_at' and isinstance(item, int) and item in EPOCH_MS:
            item = '<time>'  # a reading no TurnClock has told: there is none in a capture of this suite
        elif key in CLOCK_KEYS and isinstance(item, int) and not isinstance(item, bool) and item in EPOCH_MS:
            item = '<time>'
        elif key == 'retry_after_s' and isinstance(item, int) and not isinstance(item, bool):
            item = '<seconds>'  # how long an agent waits before it rings again: what is left of a minute
        elif key == 'mtime' and isinstance(item, int) and not isinstance(item, bool) and item > 0:
            item = '<time>'  # when a session a tool kept was last written: a reading of that file's clock
        elif key == 'message' and isinstance(item, str):
            # What a refusal says in words names the session of a tool it is about ("Claude session
            # '...' is already used by ..."): the name is a draw of the tool's, or of the Host's.
            item = normalize.UUID.sub('<a session>', item)
        elif key in ('opening_audio_url', 'audio_url') and isinstance(item, str):
            item = RING_CLIP.sub(r'/audio/<time>\1', item)
        elif key == 'agents' and isinstance(item, list):
            # Both Hosts have two codex agents (rachel, mike) that no scenario is about. Both
            # sides are cut to the scenario's own.
            item = [row for row in item if not isinstance(row, dict) or row.get('session') in sessions]
        elif key == 'roster' and isinstance(item, list):
            item = [name for name in item if isinstance(name, str) and name.lower() in sessions]
        elif key == 'personas' and isinstance(item, list):
            # The personas a Host offers are compared each by its id. Their order is each
            # Host's own: the Python Host's is the millisecond a persona was first written and
            # then its id, the Rust Host's the roster's.
            item = sorted(item, key=lambda persona: str(persona.get('id')) if isinstance(persona, dict) else '')
        out[key] = settle(item, sessions)
    if 'server_id' in out and 'capabilities' in out and out.get('name'):
        out['name'] = '<name>'  # `GET /server-info`: a Host is called what its machine is called
    return out


def told_to_wait(headers):
    """An answer's headers, with how long an asker is told to wait named and not counted: what is left
    of a minute when the answer is written."""
    if isinstance(headers, dict) and 'retry-after' in headers:
        return {**headers, 'retry-after': '<seconds>'}
    return headers


def without_features(value):
    """A body or an event without the fields of the features the Rust Host does not have yet."""
    if isinstance(value, list):
        return [without_features(item) for item in value]
    if isinstance(value, dict):
        return {key: without_features(item) for key, item in value.items() if key not in NOT_YET_FIELDS}
    return value


def outline(body):
    """An answer as the names of its fields and the kind of each: what a reader of it needs."""
    def kind(value):
        if isinstance(value, bool):
            return '<yes or no>'
        if isinstance(value, (int, float)):
            return '<number>'
        if isinstance(value, str):
            return '<text>'
        if isinstance(value, list):
            return '<list>'
        if isinstance(value, dict):
            return '<fields>'
        return '<nothing>'
    return {key: kind(value) for key, value in body.items()} if isinstance(body, dict) else kind(body)


def kept_where(body):
    """The answer to a file a user attached, with where it is kept as the agent's directory and
    the file's name: under which directory a Host keeps its uploads is the Host's own, and the
    agent reads the file from wherever the answer says."""
    if not isinstance(body, dict) or not isinstance(body.get('path'), str):
        return body
    return {**body, 'path': '<uploads>/' + '/'.join(body['path'].split('/')[-2:])}


def own_particulars(body):
    """`GET /server-info` with what is each Host's own as just that: the list of its features
    (what a Host has is what it advertises there, and the two do not have the same) and its
    version."""
    if not isinstance(body, dict) or 'capabilities' not in body or 'contract' not in body:
        return body
    out = dict(body)
    if isinstance(out.get('capabilities'), dict):
        out['capabilities'] = {**out['capabilities'], 'features': '<features>'}
    if isinstance(out.get('contract'), dict):
        out['contract'] = {**out['contract'], 'features': '<features>'}
    if out.get('clarp_version'):
        out['clarp_version'] = '<version>'
    return out


def conversation_of(value: dict, otherwise: str) -> str:
    """The conversation an answer's cursors belong to: the one it names beside them."""
    own = value.get('conversation_id')
    return own if isinstance(own, str) and any(key in HEAD_KEYS for key in value) else otherwise


def cursors(value, found: dict, conversation=''):
    """Collects the cursors a capture hands its client: the heads of its answers, each under
    the conversation it is a cursor of. A client holds a cursor for one conversation; what it
    holds for another says nothing about this one, and the two Hosts number their conversations
    in different ways (the Python Host counts every row of the Host, the Rust Host each agent's
    own)."""
    if isinstance(value, list):
        for item in value:
            cursors(item, found, conversation)
    elif isinstance(value, dict):
        conversation = conversation_of(value, conversation)
        for key, item in value.items():
            if key in HEAD_KEYS and isinstance(item, int) and not isinstance(item, bool):
                found.setdefault(conversation, set()).add(item)
            else:
                cursors(item, found, conversation)


def ranked(value, held: dict, row=False, conversation=''):
    """Revisions as what a client can tell of them. A head is its place among the cursors the
    capture handed out for its conversation. A row's revision is how many of those cursors it
    is newer than: a client holds a cursor and asks what changed since, so what it can tell of
    a row is which of its cursors the row is newer than. How two rows that changed between the
    same two cursors stand to each other, no client can tell."""
    if isinstance(value, list):
        return [ranked(item, held, row, conversation) for item in value]
    if isinstance(value, dict):
        conversation = conversation_of(value, conversation)
        mine = held.get(conversation, [])
        out = {}
        for key, item in value.items():
            whole = isinstance(item, int) and not isinstance(item, bool)
            if whole and key in HEAD_KEYS:
                out[key] = mine.index(item) + 1 if item in mine else item
            elif whole and row and key == 'revision':
                out[key] = bisect.bisect_left(mine, item)
            else:
                out[key] = ranked(item, held, key == 'turns', conversation)
        return out
    return value


# Where a send's delivery stands in its feed (`delivery.seq`, `delivery_cursor`, `delivery_until`):
# the Python Host counts every transition of the Host in one counter, the Rust Host an agent's in
# the revisions of its own log. What a client can tell of them is their order: a cursor is newer
# or older than a send's last change. They are compared as their place among all of them in the
# capture, among those of the same agent (the scope an answer names): the Python Host's counter is
# one for all of its agents, and an agent's feed is read for that agent alone.
DELIVERY_ORDER_KEYS = ('delivery_cursor', 'delivery_until')


def delivery_agent(value, agent=None):
    scope = value.get('delivery_scope') if isinstance(value, dict) else None
    return scope.get('agent_id') if isinstance(scope, dict) else agent


def delivery_numbers(value, found: dict, agent=None) -> None:
    if isinstance(value, list):
        for item in value:
            delivery_numbers(item, found, agent)
    elif isinstance(value, dict):
        agent = delivery_agent(value, agent)
        for key, item in value.items():
            if key == 'delivery' and isinstance(item, dict) and isinstance(item.get('seq'), int):
                found.setdefault(agent, set()).add(item['seq'])
            else:
                delivery_numbers(item, found, agent)


def delivery_ranked(value, order: dict, agent=None):
    if isinstance(value, list):
        return [delivery_ranked(item, order, agent) for item in value]
    if isinstance(value, dict):
        agent = delivery_agent(value, agent)
        mine = order.get(agent, [])
        # How many of the agent's sends had changed by then: a seq is its rank, a cursor at
        # the end of the feed is all of them, wherever the Host's own counter stands.
        place = lambda number: bisect.bisect_right(mine, number)
        out = {}
        for key, item in value.items():
            whole = isinstance(item, int) and not isinstance(item, bool)
            if whole and key in DELIVERY_ORDER_KEYS:
                out[key] = place(item)
            elif key == 'delivery' and isinstance(item, dict) and isinstance(item.get('seq'), int):
                out[key] = {**item, 'seq': place(item['seq'])}
            else:
                out[key] = delivery_ranked(item, order, agent)
        return out
    return value


def agent_rows(http) -> dict:
    """The ids of the rows a Host named itself, in the order a client first reads them: the rows
    an agent wrote, and the rows of a user that a Host read from a tool's transcript (`msg-`: a
    message a client sent keeps the id its client gave it, and is compared as that). An id is
    the Host's own choice; what a client relies on is that the same row has the same id wherever
    it is named."""
    names: dict = {}
    for exchange in http:
        body = exchange['body']
        for turn in (body.get('turns') or ()) if isinstance(body, dict) else ():
            if not isinstance(turn, dict) or not isinstance(turn.get('id'), str):
                continue
            if turn.get('role') == 'assistant' or (turn.get('role') == 'user' and turn['id'].startswith('msg-')):
                names.setdefault(turn['id'], f'<row:{len(names) + 1}>')
    return names


# What a live item of a message says of the row its text is written into (`row_id`), and what
# a notification says of the row it is about (`source_message_id`), when no row of the
# conversation has that id. The id itself is random and names nothing a client can find, so it
# is compared as just this, and takes no place among the ids that are named in the order they
# appear.
NO_SUCH_ROW = '<a row the conversation does not have>'
NAMES_A_ROW = ('row_id', 'source_message_id')


def names_no_row(core: dict) -> bool:
    """Whether a recording carries the mark of the reference's defect R19: a notification of a reply
    names a row the conversation does not have, though the conversation as this capture read it has
    the answer to that message. The row the reply was written into while its turn ran was replaced
    by the transcript's row, and the turn's summary went with it
    (server-rs/docs/reference-defects.md). The reference does it in some runs and not in others,
    in the first turn of a conversation. It is a fact about one recording alone: no other Host is
    looked at to tell it."""
    answered: dict = {}
    for exchange in core.get('http', ()):
        body = exchange.get('body')
        turns = body.get('turns') if isinstance(body, dict) else None
        asked = None
        for turn in turns if isinstance(turns, list) else ():
            if not isinstance(turn, dict):
                continue
            if turn.get('role') == 'user':
                asked = turn.get('id')
            elif turn.get('role') == 'assistant' and asked:
                answered.setdefault(asked, set()).add(turn.get('id'))
    for frame in core.get('sse', ()):
        data = frame.get('data')
        if (isinstance(data, dict) and data.get('type') == 'user-notification'
                and data.get('source_message_id') == NO_SUCH_ROW and answered.get(data.get('cause_message_id'))):
            return True
    return False


def named_complete(core: dict, before: int, conversation, row_id, words):
    """The cursor at which a snapshot read before exchange `before` named this row as its agent's
    last message, complete with these words (its place among the capture's cursors, as the core
    view ranks a head); None where none did."""
    for exchange in (core.get('http') or [])[:before]:
        body = exchange.get('body')
        rows = body.get('agents') if isinstance(body, dict) else None
        for agent in rows if isinstance(rows, list) else ():
            if (isinstance(agent, dict) and agent.get('conversation_id') == conversation
                    and agent.get('last_message_id') == row_id and agent.get('last_completed_message') == words
                    and isinstance(agent.get('head_revision'), int)):
                return agent['head_revision']
    return None


def stamped_late(core: dict, exchange: int, row: dict) -> bool:
    """Whether a row a read of its conversation shows (exchange `exchange`) is newer than a cursor
    at which a snapshot of the same capture had already named it its agent's last message, with
    the same words: the row was stamped again after that cursor though what a client saw of its
    words had not changed. A client holding that cursor is told the row changed."""
    body = (core.get('http') or [])[exchange].get('body') if exchange < len(core.get('http') or []) else None
    words, revision = row.get('text'), row.get('revision')
    if not isinstance(body, dict) or not words or row.get('role') != 'assistant' or not isinstance(revision, int):
        return False
    named = named_complete(core, exchange, body.get('conversation_id'), row.get('id'), words)
    # A row's revision is how many of the capture's cursors it is newer than; the head it was
    # named at is its place among them, from one: newer than that head is at least that many.
    return named is not None and revision >= named


def stamped_again(core: dict) -> bool:
    """Whether a recording carries the mark of the reference's defect R115, the late stamp: an answer a snapshot named
    complete at a cursor is shown newer than that cursor by a later read of the conversation, with
    the same words (server-rs/docs/reference-defects.md, R115). The reference does it in
    some runs (the-queue-by-hand: 9 of 268 recordings), stamping the earlier answer again beside a
    later one. It is a fact about one recording alone: no other Host is looked at to tell it. A
    row that really changed after the snapshot (its turn settled later) is marked too, and costs a
    recording more: the mark only asks for another recording, and excuses nothing."""
    for exchange, answered in enumerate(core.get('http') or ()):
        body = answered.get('body')
        turns = body.get('turns') if isinstance(body, dict) else None
        for row in turns if isinstance(turns, list) else ():
            if isinstance(row, dict) and stamped_late(core, exchange, row):
                return True
    return False


def carries_a_mark(core: dict) -> bool:
    """Whether a recording says of itself that it carries a defect of the reference that comes and
    goes from run to run: R19, or R115 (the late stamp)."""
    return names_no_row(core) or stamped_again(core)


def renamed(value, names: dict):
    if isinstance(value, list):
        return [renamed(item, names) for item in value]
    if isinstance(value, dict):
        out = {key: renamed(item, names) for key, item in value.items()}
        for key in NAMES_A_ROW:
            named = value.get(key)
            if isinstance(named, str) and named and named not in names:
                out[key] = NO_SUCH_ROW
        return out
    if isinstance(value, str):
        return names.get(value, value)
    return value


def core(raw: dict) -> dict:
    """A raw capture as it is compared across Hosts: without what belongs to features the Rust
    Host does not have yet, with revisions as their order, with the ids of the agent's rows as
    the order they are first read in, and with each Host's own version as just "a version"."""
    # The session a Host drew for a janitor it made as a new agent is named first: the answer that
    # says it is compared as its outline below, and the name is needed wherever the janitor is named.
    janitors_made = made_new(raw['http'], {}, nameless=raw.get('scenario') in NAMELESS)
    if janitors_made:
        raw = {**raw, 'http': by_name(raw['http'], janitors_made), 'sse': by_name(raw['sse'], janitors_made)}
    http = [{**exchange, 'body': own_particulars(without_features(exchange['body']))} for exchange in raw['http']]
    http = [{**exchange, 'body': outline(exchange['body'])} if exchange.get('path') in NOT_KEPT and exchange.get('status') == 200
            else exchange for exchange in http]
    http = [{**exchange, 'body': kept_where(exchange['body'])} if exchange.get('path') == '/upload' else exchange
            for exchange in http]
    http = [{**exchange, 'body': by_fiat(exchange.get('path'), exchange['body'])} for exchange in http]
    http = [{**exchange, 'body': in_any_order(exchange.get('path'), exchange['body'])} for exchange in http]
    sse = []
    for frame in raw['sse']:
        data = frame.get('data')
        if isinstance(data, dict):
            if data.get('type') in NOT_YET_EVENTS or f"{data.get('type')}/{data.get('kind')}" in NOT_YET_EVENTS:
                continue
            data = without_features(data)
            if data.get('type') == 'server-version' and 'version' in data:
                data = {**data, 'version': '<version>'}
        sse.append({**frame, 'data': data})
    found: dict = {}
    cursors([exchange['body'] for exchange in http], found)
    held = {conversation: sorted(values - {0}) for conversation, values in found.items()}
    names = agent_rows(http)
    numbers: dict = {}
    delivery_numbers([exchange['body'] for exchange in http], numbers)
    order = {agent: sorted(found - {0}) for agent, found in numbers.items()}
    http = [{**exchange, 'body': renamed(delivery_ranked(ranked(exchange['body'], held), order), names)}
            for exchange in http]
    sse = [{**frame, 'data': renamed(frame.get('data'), names)} for frame in sse]
    return {**raw, 'http': http, 'sse': sse}


def issued(raw, named, found: dict) -> dict:
    """The ids a Host gave out in its answers, each with the name the normalizer gave it: read
    off the same place in an answer as it came and as it was normalized."""
    if isinstance(raw, dict) and isinstance(named, dict):
        for key, item in raw.items():
            if key in named:
                issued(item, named[key], found)
    elif isinstance(raw, list) and isinstance(named, list) and len(raw) == len(named):
        for item, other in zip(raw, named):
            issued(item, other, found)
    elif isinstance(raw, str) and isinstance(named, str) and raw != named and ISSUED.match(raw):
        found.setdefault(raw, named)
    return found


def by_name(value, names: dict):
    """Every id a Host gave out, under its name wherever it is written: in a path, in a text,
    cut short at the end of a preview. Only ids the Host gave out under an id's own key in an
    answer of this capture: any other hex in a text stays what it is, content."""
    if isinstance(value, list):
        return [by_name(item, names) for item in value]
    if isinstance(value, dict):
        return {key: by_name(item, names) for key, item in value.items()}
    if not isinstance(value, str):
        return value
    for raw, name in names.items():
        if raw in value:
            value = value.replace(raw, name)
    cut = CUT_ID.search(value)
    if cut:
        start = cut.group(1)
        for raw, name in names.items():
            if raw.startswith(start) and raw != start:
                value = f'{value[:cut.start(1)]}{name}[:{len(start)}]…'
                break
    return value


def agents_by_id(value, found: dict) -> dict:
    """The session of every agent a capture names with its id: an id is the Host's own choice, a
    session name is what the scenario gave the agent."""
    if isinstance(value, list):
        for item in value:
            agents_by_id(item, found)
    elif isinstance(value, dict):
        if isinstance(value.get('agent_id'), str) and isinstance(value.get('session'), str) and value['agent_id']:
            found.setdefault(value['agent_id'], value['session'])
        for item in value.values():
            agents_by_id(item, found)
    return found


def by_session(value, sessions: dict):
    """A list of agents' ids as a list of who they are. The harness's normalizer names an id
    where it stands under a key it knows; in a plain list it would stay each Host's own."""
    if isinstance(value, list):
        return [by_session(item, sessions) for item in value]
    if isinstance(value, dict):
        return {key: ([f'<agent:{sessions.get(item, "?")}>' if isinstance(item, str) else item for item in items]
                      if key == 'archived_parents' and isinstance(items, list)
                      else own_face(items, sessions) if key == 'avatar_url' and isinstance(items, str)
                      else by_session(items, sessions))
                for key, items in value.items()}
    return value


def own_face(address: str, sessions: dict) -> str:
    """Where an agent's own picture is fetched (`/avatars/<agent id>?v=...`), with the agent named
    as who it is: the id is each Host's own draw. What the address carries of the picture is
    compared."""
    if not address.startswith('/avatars/'):
        return address
    agent_id = address[len('/avatars/'):].split('?')[0]
    return address.replace(agent_id, f'<agent:{sessions[agent_id]}>', 1) if agent_id in sessions else address


def own_conversations(value, found: dict) -> dict:
    """The conversations a capture shows under a name a Host made up for them: an agent's row
    names its conversation and the session of its tool, and the two are not one name. Such a
    conversation is named here by its agent, wherever its name is written. A conversation that
    goes by the name of its tool's session is named as the session is, by the harness's
    normalizer, in the order the ids appear in. (The Rust Host gives an agent that takes a
    session up a conversation of its own, since that session may have been another agent's
    conversation: docs/differences.md. Named in order, the one more id would shift every id
    that comes after it.)"""
    if isinstance(value, list):
        for item in value:
            own_conversations(item, found)
    elif isinstance(value, dict):
        conversation, of_tool, session = (value.get(key) for key in ('conversation_id', 'backend_session_id', 'session'))
        if all(isinstance(name, str) and name for name in (conversation, of_tool, session)) \
                and conversation != of_tool and normalize.UUID.fullmatch(conversation):
            found.setdefault(conversation, f'<conversation:{session}>')
        for item in value.values():
            own_conversations(item, found)
    return found


def published(value, found: dict) -> dict:
    """The ids of the files a capture's agents published, each with the name it is given here."""
    if isinstance(value, list):
        for item in value:
            published(item, found)
    elif isinstance(value, dict):
        for key, item in value.items():
            if key == 'asset_id' and isinstance(item, str) and ASSET.fullmatch(item):
                found.setdefault(item, f'<asset:{len(found) + 1}>')
            else:
                published(item, found)
    return found


def called(value, found: dict) -> dict:
    """The ids of the calls, of the rings and of the schedules a capture names, each with the name it
    is given here."""
    if isinstance(value, list):
        for item in value:
            called(item, found)
    elif isinstance(value, dict):
        for key, item in value.items():
            if key == 'call_id' and isinstance(item, str) and CALL.fullmatch(item):
                known = sum(1 for name in found.values() if name.startswith('<call:'))
                found.setdefault(item, f'<call:{known + 1}>')
            elif key == 'ring_id' and isinstance(item, str) and RING.fullmatch(item):
                known = sum(1 for name in found.values() if name.startswith('<ring:'))
                found.setdefault(item, f'<ring:{known + 1}>')
            elif key == 'schedule_id' and isinstance(item, str) and SCHEDULE.fullmatch(item):
                known = sum(1 for name in found.values() if name.startswith('<schedule:'))
                found.setdefault(item, f'<schedule:{known + 1}>')
            elif key == 'portrait_id' and isinstance(item, str) and PORTRAIT.fullmatch(item):
                known = sum(1 for name in found.values() if name.startswith('<portrait:'))
                found.setdefault(item, f'<portrait:{known + 1}>')
            else:
                called(item, found)
    return found


def forked(http, found: dict) -> dict:
    """The session names of the children a capture's forks made, each with the name it is given here.
    A session name is the Host's to choose: the Python Host ends a new agent's with four signs it
    draws, the Rust Host only where the plain name is taken (server-rs/docs/differences.md, "The
    session name of a new agent"). What a client relies on is that the name a fork answered is the
    name the child has wherever it is named: in the snapshot, in what the child is told, in who
    wrote to whom."""
    for exchange in http:
        body = exchange.get('body')
        if (exchange.get('method') != 'POST' or not str(exchange.get('path') or '').endswith('/fork')
                or not isinstance(body, dict)):
            continue
        for child in body.get('children') or ():
            if isinstance(child, dict) and isinstance(child.get('session'), str) and child['session']:
                found.setdefault(child['session'], f'<forked:{len(found) + 1}>')
    return found


# Scenarios whose agents are made without a session of their own: the session each Host draws for one
# (`<name>-<four hex signs>` on the Python Host, the name alone where it is free on the Rust Host:
# docs/differences.md, "The session name of a new agent") is named in the order they are made.
NAMELESS = frozenset({'contacts-minted'})


def made_new(http, found: dict, nameless: bool = False) -> dict:
    """The session name of each janitor a capture made as a new agent, with the name it is given here:
    the letters of the janitor's name and twelve signs its Host drew (`POST /janitors` without a
    session). What a client relies on is that the name the request answered is the janitor's name
    wherever it is named. `nameless`: also of each agent `POST /agents` made in such a scenario."""
    for exchange in http:
        if nameless and exchange.get('method') == 'POST' and exchange.get('path') == '/agents' \
                and exchange.get('status') == 200 and isinstance(exchange.get('body'), dict):
            session = exchange['body'].get('session')
            if isinstance(session, str) and session:
                found.setdefault(session, f'<made:{len(found) + 1}>')
            continue
        body = exchange.get('body')
        janitor = body.get('janitor') if isinstance(body, dict) else None
        if exchange.get('method') != 'POST' or exchange.get('path') != '/janitors' or not isinstance(janitor, dict):
            continue
        session = janitor.get('session')
        if isinstance(session, str) and re.fullmatch(r'.+-[0-9a-f]{12}', session):
            found.setdefault(session, f'<made:{len(found) + 1}>')
    return found


def cut_lists(value, found: dict) -> dict:
    """The names of the lists of Updates a capture's pages were cut from, and the cursors its
    pages ended with: each Host's own, named here in the order a client learns of them. What is
    compared is whether two pages are of one list and whether a page has more after it."""
    if isinstance(value, list):
        for item in value:
            cut_lists(item, found)
    elif isinstance(value, dict):
        for key, item in value.items():
            if key in ('snapshot_revision', 'next_cursor') and isinstance(item, str) and item:
                kind = 'snapshot' if key == 'snapshot_revision' else 'cursor'
                known = len({name for name in found.values() if name.startswith(f'<{kind}:')})
                found.setdefault(item, f'<{kind}:{known + 1}>')
            else:
                cut_lists(item, found)
    return found


def seen_by_a_client(raw: dict) -> dict:
    """A capture, normalized, and cut down to the channels a client sees."""
    assets = published([exchange['body'] for exchange in raw['http']], {})
    if assets:
        raw = {**raw, 'http': by_name(raw['http'], assets), 'sse': by_name(raw['sse'], assets)}
    lists = cut_lists([exchange['body'] for exchange in raw['http']], {})
    if lists:
        raw = {**raw, 'http': by_name(raw['http'], lists)}
    calls = called([exchange['body'] for exchange in raw['http']], {})
    if calls:
        raw = {**raw, 'http': by_name(raw['http'], calls), 'sse': by_name(raw['sse'], calls)}
    children = forked(raw['http'], {})
    if children:
        raw = {**raw, 'http': by_name(raw['http'], children), 'sse': by_name(raw['sse'], children)}
    janitors_made = made_new(raw['http'], {}, nameless=raw.get('scenario') in NAMELESS)
    if janitors_made:
        raw = {**raw, 'http': by_name(raw['http'], janitors_made), 'sse': by_name(raw['sse'], janitors_made)}
    conversations = own_conversations([exchange['body'] for exchange in raw['http']], {})
    if conversations:
        raw = {**raw, 'http': by_name(raw['http'], conversations), 'sse': by_name(raw['sse'], conversations)}
    # The agents a scenario is about: its own, unless it names more.
    sessions = tuple(scenarios.BY_ID.get(raw.get('scenario'), {}).get('sessions', (scenarios.AGENT,)))
    known = agents_by_id([exchange['body'] for exchange in raw['http']], {})
    # An agent's own picture is asked for under the agent's id: the path is named as the address is.
    raw = {**raw, 'http': [{**exchange, 'body': by_session(settle(exchange['body'], sessions), known),
                            'path': own_face(str(exchange.get('path') or ''), known),
                            'headers': told_to_wait(exchange.get('headers'))}
                           for exchange in raw['http']]}
    # What a call's event says of the call is the call's record: its moments and its Host are
    # settled as in an answer.
    raw = {**raw, 'sse': [{**frame, 'data': by_session(settle(frame['data'], sessions), known)}
                          if isinstance(frame.get('data'), dict) and frame['data'].get('type') in ('group-call', 'agent-ring')
                          else frame for frame in raw['sse']]}
    whole = normalize.normalize(raw, str(REPO))
    # The normalizer keeps the answers in the order they were given, one for one.
    names: dict = {}
    for exchange, named in zip(raw['http'], whole['http']):
        issued(exchange['body'], named['body'], names)
    if names:
        whole = {**whole, 'http': by_name(whole['http'], names), 'sse': by_name(whole['sse'], names),
                 'live': by_name(whole.get('live', {}), names)}
    return {'scenario': whole['scenario'], 'error': whole['error'], 'http': whole['http'], 'sse': whole['sse'],
            'live': whole.get('live', {})}


def comparable(seen: dict) -> dict:
    return {**NO_STATE, **seen}


class Run:
    """A fresh store for the lifetime of one conformance run."""

    def __init__(self, path: Path):
        self.path = path
        self.labels = 0

    def label(self, name: str) -> Path:
        self.labels += 1
        path = self.path / f'{self.labels:02d}-{name}'
        path.mkdir()
        return path


def corpus(make_host, chosen, where: Path, jobs: int) -> dict:
    """Every chosen scenario against its own fresh Host; scenario id -> what a client saw."""
    def one(item):
        index, scenario = item
        raw = capture(make_host, scenario, where / f's{index:02d}')
        # Both views of the same run: everything, and what is compared across Hosts.
        return scenario['id'], {'full': seen_by_a_client(raw), 'core': seen_by_a_client(core(raw))}
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        return dict(pool.map(one, enumerate(chosen)))


def differences(a: dict, b: dict, chosen, view: str = 'full') -> diff.Report:
    report = diff.Report()
    for scenario in chosen:
        diff.compare(comparable(a[scenario['id']][view]), comparable(b[scenario['id']][view]), report)
    return report


def place(difference: dict) -> tuple:
    """Where a difference is: the channel and the place in it, whatever the values are."""
    return difference['channel'], difference['locator']


def run_errors(seen: dict, label: str) -> list:
    return [f'{label} / {scenario}: {views["full"]["error"]}' for scenario, views in sorted(seen.items())
            if views['full']['error']]


def steady(chosen):
    """The scenarios the reference answers the same way every time: the ones that count."""
    return [scenario for scenario in chosen if not scenario.get('unsteady')]


def python_host(build: Path):
    return lambda root: PythonHost(build, root)


def rust_host(binary: Path):
    return lambda root: RustHost(binary, root)


def load_golden(directory: Path, chosen) -> dict:
    golden = {}
    for scenario in chosen:
        views = {}
        for view, path in (('full', directory / f"{scenario['id']}.json"),
                           ('core', directory / 'core' / f"{scenario['id']}.json")):
            if not path.is_file():
                raise SystemExit(f'nothing is written down for {scenario["id"]} in {path.parent}: record it first')
            views[view] = json.loads(path.read_text())
        golden[scenario['id']] = views
    return golden


def write_golden(directory: Path, seen: dict):
    """What a Host answered: everything, and under core/ what is compared across Hosts."""
    (directory / 'core').mkdir(parents=True, exist_ok=True)
    for scenario, views in seen.items():
        for view, path in (('full', directory / f'{scenario}.json'), ('core', directory / 'core' / f'{scenario}.json')):
            path.write_text(json.dumps(views[view], indent=1, sort_keys=True) + '\n')


def golden_dirs(first: Path) -> list:
    """Where the recordings of the reference are written down: the first, beside it the second,
    and a third where a run made one (`record`)."""
    return [first, first.with_name(first.name + '-again'), first.with_name(first.name + '-more')]


def asked_dir(first: Path, attempt: int) -> Path:
    """Where the answers of the reference asked again in a try (`asked_again`) are written down:
    only the scenarios asked, beside the first recording."""
    return first.with_name(f'{first.name}-asked-{attempt}')


def load_asked(first: Path, chosen) -> dict:
    """What the reference answered when it was asked again, as it was written down: try ->
    scenario id -> views."""
    answers: dict = {}
    for attempt in range(1, MORE_TRIES + 1):
        directory = asked_dir(first, attempt)
        if directory.is_dir():
            answers[attempt] = load_golden(directory, [scenario for scenario in chosen
                                                       if (directory / f"{scenario['id']}.json").is_file()])
    return answers


def load_intended(path) -> set:
    if not path or not Path(path).is_file():
        return set()
    return {line.split()[0] for line in Path(path).read_text().splitlines()
            if line.strip() and not line.lstrip().startswith('#')}


def read_awaited(text: str) -> dict:
    """The register of awaited differences (`awaited.txt`), by id: the reference's PR, the
    branch that owes the port, the scenario and what it is. A line is `<id>  <PR>  <branch>
    <scenario> / ...`; one that says less is an error, named."""
    found = {}
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        fields = line.split(None, 3)
        if len(fields) < 4 or not fields[1].isdigit():
            raise SystemExit(f'awaited.txt, line {number}: an id, the PR, the branch and what it is: {line!r}')
        ident, pr, branch, what = fields
        if ident in found:
            raise SystemExit(f'awaited.txt, line {number}: {ident} is registered twice')
        found[ident] = {'pr': int(pr), 'branch': branch, 'scenario': what.split()[0], 'what': what,
                        'line': number}
    return found


def known_scenarios(awaited: dict, known) -> None:
    """Every line of the register names, first, a scenario the corpus has, whichever scenarios a
    run chose. A line is asked only where its scenario ran: one that names none would never be
    seen and never be stale, and would pass for ever. It is an error, named by its line."""
    unknown = sorted((line['line'], ident, line['scenario']) for ident, line in awaited.items()
                     if line['scenario'] not in known)
    if unknown:
        raise SystemExit('awaited.txt names no scenario of the corpus first: '
                         + '; '.join(f'line {number} ({ident}): {name!r}' for number, ident, name in unknown))


def load_awaited(path) -> dict:
    if not path or not Path(path).is_file():
        return {}
    return read_awaited(Path(path).read_text())


def both_registers(intended: set, awaited: dict) -> None:
    """An id is intended or awaited, never both: a difference meant to stay is no port owed."""
    both = sorted(intended & set(awaited))
    if both:
        raise SystemExit('registered as intended and as awaited, which cannot both be: '
                         f'{", ".join(both)}: take it out of one of intended.txt and awaited.txt')


# How often a scenario is recorded again when both of its recordings carry the mark of a defect
# of the reference that comes and goes (`carries_a_mark`: R19, R115).
MORE_TRIES = 3


def recorded_again(recordings: list, chosen, again) -> tuple:
    """The recordings, with a third where one could be made, and what the report says of it.

    A scenario whose every recording carries the mark of a defect of the reference that comes and
    goes (R19, R115: `carries_a_mark`) is recorded again (`again(scenarios, attempt)`
    answers scenario id -> its two views), up to `MORE_TRIES` times; the first answer that ran to
    its end without any mark is kept. The third recording
    is the second with those answers in their places, so that it has the whole corpus."""
    marked = [scenario for scenario in steady(chosen)
              if all(carries_a_mark(recording[scenario['id']]['core']) for recording in recordings)]
    if not marked:
        return recordings, []
    without: dict = {}
    tries = dict.fromkeys((scenario['id'] for scenario in marked), 0)
    for attempt in range(1, MORE_TRIES + 1):
        left = [scenario for scenario in marked if scenario['id'] not in without]
        if not left:
            break
        more = again(left, attempt)
        for scenario in left:
            views = more[scenario['id']]
            tries[scenario['id']] = attempt
            if not views['full']['error'] and not carries_a_mark(views['core']):
                without[scenario['id']] = views
    lines = ['', "# A defect of the reference in every recording of a scenario: R19 (a notification names a row the "
             'conversation does not have) or R115, the late stamp (an answer shown as changed after a cursor at which it '
             'was complete)']
    for scenario in marked:
        name = scenario['id']
        lines.append(f'{name}: recorded again, and its answer without it (try {tries[name]}) is kept as one more '
                     'recording' if name in without else
                     f'{name}: recorded {tries[name]} more time(s) with it every time: compared as it is')
    if without:
        recordings = [*recordings, {**recordings[-1], **without}]
    return recordings, lines


# How often the reference is asked again for a scenario it did not run to its end.
UNFINISHED_TRIES = 2


def finished_again(recording: dict, chosen, again, label: str) -> tuple:
    """A recording with the scenarios the reference did not run to its end asked again, and
    what the report says of it.

    Every scenario runs against a fresh Host of its own, so one that is asked again is asked
    as it was the first time (`again(scenarios, attempt)` answers scenario id -> its two
    views), up to `UNFINISHED_TRIES` times: the first answer that ran to its end takes the
    place of the one that did not. A test Host that does not come to rest is a defect of the
    reference (R43) and says nothing of the Rust Host, which is compared with what the
    reference answers when it does. Every time it happens is said. A scenario the reference
    never runs to its end is left as it is, and fails the run as before."""
    cut = [scenario for scenario in chosen if recording[scenario['id']]['full']['error']]
    if not cut:
        return recording, []
    recording = dict(recording)
    said = {scenario['id']: recording[scenario['id']]['full']['error'] for scenario in cut}
    done: dict = {}
    for attempt in range(1, UNFINISHED_TRIES + 1):
        left = [scenario for scenario in cut if scenario['id'] not in done]
        if not left:
            break
        more = again(left, attempt)
        for scenario in left:
            views = more[scenario['id']]
            if not views['full']['error']:
                recording[scenario['id']], done[scenario['id']] = views, attempt
    return recording, [
        f'{label} / {name}: {said[name]}; '
        + (f'asked again, and it ran to its end (try {done[name]})' if name in done else
           f'asked {UNFINISHED_TRIES} more time(s), and it never did')
        for name in said]


def selftest() -> list:
    """The rule above, tried on captures made for it: what it must do and what it must not.
    Raises when it does otherwise; the lines say what was tried. A tool that judges is tried
    before it judges."""
    def capture(names, answered=True, error=None):
        turns = [{'id': 'u-one', 'role': 'user'}]
        if answered:
            turns.append({'id': '<row:1>', 'role': 'assistant'})
        core = {'http': [{'path': '/log', 'body': {'turns': turns}}],
                'sse': [{'data': {'type': 'user-notification', 'cause_message_id': 'u-one',
                                  'source_message_id': names}}]}
        return {'full': {'error': error}, 'core': core}
    sound, marked = capture('<row:1>'), capture(NO_SUCH_ROW)
    scenario = [{'id': 's'}]
    assert not names_no_row(sound['core']), 'a recording whose notification names its row was marked'
    assert names_no_row(marked['core']), 'a recording whose notification names no row was not marked'
    assert not names_no_row(capture(NO_SUCH_ROW, answered=False)['core']), \
        'a recording that never read the answer was marked: what it names cannot be known'
    asked = []

    def again(answers):
        def give(left, attempt):
            asked.append(attempt)
            return {'s': answers[min(attempt, len(answers)) - 1]}
        return give
    # Both recordings marked, the second try sound: it is kept, and nothing is asked after it.
    kept, lines = recorded_again([{'s': marked}, {'s': marked}], scenario, again([marked, sound, marked]))
    assert len(kept) == 3 and kept[2]['s'] is sound and asked == [1, 2] and 'try 2' in lines[-1], (len(kept), asked)
    # Marked every time: nothing is added, and it was tried as often as it may be.
    asked.clear()
    kept, lines = recorded_again([{'s': marked}, {'s': marked}], scenario, again([marked]))
    assert len(kept) == 2 and asked == list(range(1, MORE_TRIES + 1)) and 'compared as it is' in lines[-1], asked
    # A try that did not run to its end is no answer.
    asked.clear()
    broken = capture('<row:1>', error='the scenario did not run to its end')
    kept, _ = recorded_again([{'s': marked}, {'s': marked}], scenario, again([broken]))
    assert len(kept) == 2, 'a recording that did not run to its end was kept'
    # One sound recording of two: the reference answers in two ways already, and nothing is asked.
    asked.clear()
    kept, lines = recorded_again([{'s': marked}, {'s': sound}], scenario, again([sound]))
    assert len(kept) == 2 and not asked and not lines, 'a scenario with a sound recording was recorded again'
    # A scenario the reference is known to waver in is not counted, and not recorded again.
    kept, lines = recorded_again([{'s': marked}, {'s': marked}], [{'id': 's', 'unsteady': True}], again([sound]))
    assert len(kept) == 2 and not asked, 'a scenario that is not counted was recorded again'
    # R115, the late stamp: an answer a snapshot named complete at its cursor, read later as newer.
    def stamped(revision, words='The answer', named='The answer', snapshot_first=True, error=None):
        snapshot = {'path': '/agents/snapshot', 'body': {'agents': [{
            'conversation_id': 'c', 'last_message_id': '<row:1>', 'last_completed_message': named,
            'head_revision': 1}]}}
        read = {'path': '/log', 'body': {'conversation_id': 'c', 'turns': [
            {'id': 'u-one', 'role': 'user', 'revision': 0, 'text': 'A question'},
            {'id': '<row:1>', 'role': 'assistant', 'revision': revision, 'text': words}]}}
        http = [snapshot, read] if snapshot_first else [read, snapshot]
        return {'full': {'error': error}, 'core': {'http': http, 'sse': []}}
    late, on_time = stamped(1), stamped(0)
    assert stamped_again(late['core']) and carries_a_mark(late['core']), 'an answer stamped late was not marked'
    assert not stamped_again(on_time['core']), 'an answer not newer than its cursor was marked'
    assert not stamped_again(stamped(1, words='Other words')['core']), 'an answer whose words changed was marked'
    assert not stamped_again(stamped(1, named='')['core']), 'a snapshot that named nothing complete marked a row'
    assert not stamped_again(stamped(1, snapshot_first=False)['core']), \
        'a row read before any snapshot named it was marked: no cursor of it was handed out yet'
    asked.clear()
    kept, lines = recorded_again([{'s': late}, {'s': late}], scenario, again([late, on_time]))
    assert len(kept) == 3 and kept[2]['s'] is on_time and asked == [1, 2] and 'R115, the late stamp' in lines[1], (asked, lines)
    # One recording late and one marked for R19: both carry a mark, and the sound answer is kept.
    asked.clear()
    kept, lines = recorded_again([{'s': late}, {'s': marked}], scenario, again([on_time]))
    assert len(kept) == 3 and kept[2]['s'] is on_time and asked == [1], asked
    # The other rule: a scenario the reference did not run to its end is asked again.
    cut = capture('<row:1>', error='step 7 (quiesce): timed out waiting for the agent idle')
    two = [{'id': 's'}, {'id': 't'}]
    asked.clear()
    # Nothing was cut short: nothing is asked, and the recording is the one that was given.
    whole = {'s': sound, 't': sound}
    kept, lines = finished_again(whole, two, again([sound]), 'python')
    assert kept is whole and not asked and not lines, 'a recording that ran to its end was asked again'
    # One scenario cut short, the first try whole: it takes the other's place, the scenario
    # beside it is not touched, the recording that was given is not changed, and it is said.
    def again_of(answers):
        def give(left, attempt):
            asked.append((attempt, [scenario['id'] for scenario in left]))
            return {scenario['id']: answers[min(attempt, len(answers)) - 1] for scenario in left}
        return give
    given = {'s': cut, 't': marked}
    kept, lines = finished_again(given, two, again_of([sound]), 'python')
    assert kept['s'] is sound and kept['t'] is marked and given['s'] is cut and asked == [(1, ['s'])], asked
    assert lines == ['python / s: step 7 (quiesce): timed out waiting for the agent idle; '
                     'asked again, and it ran to its end (try 1)'], lines
    # Cut short again, then whole: the second try is kept, and nothing is asked after it.
    asked.clear()
    kept, lines = finished_again({'s': cut, 't': sound}, two, again_of([cut, sound, cut]), 'python again')
    assert kept['s'] is sound and asked == [(1, ['s']), (2, ['s'])] and 'try 2' in lines[0], (asked, lines)
    # Never whole: it stays cut short, which fails the run, and it was tried as often as it may be.
    asked.clear()
    kept, lines = finished_again({'s': cut, 't': cut}, two, again_of([cut]), 'python')
    assert kept['s'] is cut and kept['t'] is cut, 'a scenario that never ran to its end was taken as run'
    assert asked == [(attempt, ['s', 't']) for attempt in range(1, UNFINISHED_TRIES + 1)], asked
    assert len(lines) == 2 and all('never did' in line for line in lines), lines
    asking = asking_tried(stamped)
    return ['the rule for a scenario whose every recording carries the mark of a defect (R19, R115): '
            'tried on 12 cases, holds',
            f'the rule that a difference is not called unexplained before the reference has been asked again: '
            f'tried on {asking} cases, holds',
            'the rule for a scenario the reference did not run to its end: tried on 4 cases, holds',
            f'the rules for defects of the reference told by their shape: tried on {shapes_tried()} cases, hold',
            f'the rule for an event\'s place in its lane: tried on {places_tried()} cases, holds']


def asking_tried(stamped) -> int:
    """`asked_again` and what the report says of it, tried on captures made for it. Raises when it
    does otherwise; answers how many cases were tried."""
    def answer(value, error=None, name='s'):
        return {'full': {'error': error}, 'core': {'scenario': name, 'error': error,
                                                   'http': [{'path': '/x', 'body': {'answer': value}}], 'sse': []}}
    common, rare = answer('common'), answer('rare')
    asked: list = []

    def each(value, scenarios):
        return {s['id']: answer(value, name=s['id']) for s in scenarios}

    def again(answers):
        def give(left, attempt):
            asked.append((attempt, [scenario['id'] for scenario in left]))
            given = {}
            for scenario in left:
                these = answers.get(scenario['id'], answers.get('*'))
                chosen = these[min(attempt, len(these)) - 1]
                given[scenario['id']] = answer(chosen, name=scenario['id']) if isinstance(chosen, str) else chosen
            return given
        return give

    def run(recordings, seen, chosen, answers, intended=frozenset()):
        asked.clear()
        before = {name: dict(views) for name, views in seen.items()}
        got, unfinished = asked_again(recordings, seen, chosen, set(intended), again(answers))
        lines, ok = judged(recordings, seen, chosen, set(intended), run_errors(seen, 'rust'), None, got)
        assert seen == before, 'the Rust Host was asked again'
        return got, unfinished, lines, ok
    one = [{'id': 's'}]
    tried = 0
    every = [(attempt, ['s']) for attempt in range(1, MORE_TRIES + 1)]
    place_line = "  s / http / [0].body.answer: "
    cut = answer('rare', error='timed out')
    # One of three does not explain: the place stays unexplained, said with its count, and the
    # scenario is asked all three times (nothing stops early).
    got, _, lines, ok = run([{'s': common}] * 2, {'s': rare}, one, {'s': ['rare', 'common', 'common']})
    assert not ok and asked == every and sorted(got) == [1, 2, 3], (asked, got)
    assert f's: recorded {MORE_TRIES} more time(s), 3 ran to their end' in lines, lines
    assert place_line + "1 of 3 gave the Rust Host's answer: STILL UNEXPLAINED" in lines, lines
    assert lines[-1] == 'THE RUST HOST DIFFERS: 1 unexplained difference(s), 0 run error(s)', lines[-1]
    tried += 1
    # Two of three explain: the reference's usual answer, said with its count; the third try is
    # still asked though two already agreed.
    got, _, lines, ok = run([{'s': common}] * 2, {'s': rare}, one, {'s': ['rare', 'common', 'rare']})
    assert ok and asked == every, asked
    assert place_line + "2 of 3 gave the Rust Host's answer: the reference's usual answer, explained" in lines, lines
    assert any("the reference's usual answer when asked again: 1  unexplained: 0" in line for line in lines), lines
    assert lines[-1] == 'THE RUST HOST CONFORMS', lines[-1]
    tried += 1
    # Three of three, said so.
    got, _, lines, ok = run([{'s': common}] * 2, {'s': rare}, one, {'s': ['rare']})
    assert ok and place_line + "3 of 3 gave the Rust Host's answer: the reference's usual answer, explained" in lines
    tried += 1
    # None of three: unexplained, 0 of 3.
    got, _, lines, ok = run([{'s': common}] * 2, {'s': rare}, one, {'s': ['common']})
    assert not ok and place_line + "0 of 3 gave the Rust Host's answer: STILL UNEXPLAINED" in lines, lines
    tried += 1
    # Three tries all cut short explain nothing, and each is said.
    got, unfinished, lines, ok = run([{'s': common}] * 2, {'s': rare}, one, {'s': [cut]})
    assert not ok and asked == every and not got and len(unfinished) == MORE_TRIES, (got, unfinished)
    tried += 1
    # Two that ran and agree explain, the third cut short and said.
    got, unfinished, lines, ok = run([{'s': common}] * 2, {'s': rare}, one, {'s': [cut, 'rare', 'rare']})
    assert ok and sorted(got) == [2, 3] and len(unfinished) == 1 and 'try 1' in unfinished[0], (got, unfinished)
    assert 's: recorded 3 more time(s), 2 ran to their end' in lines and \
        place_line + "2 of 2 gave the Rust Host's answer: the reference's usual answer, explained" in lines, lines
    tried += 1
    # One that ran, giving the Rust Host's answer, beside two cut short: not two, so unexplained.
    got, _, lines, ok = run([{'s': common}] * 2, {'s': rare}, one, {'s': [cut, cut, 'rare']})
    assert not ok and place_line + "1 of 1 gave the Rust Host's answer: STILL UNEXPLAINED" in lines, lines
    tried += 1
    # The first recordings count for nothing: the reference wavering between them (one of them
    # the Rust Host's answer) is held as before, and nothing is asked.
    got, _, lines, ok = run([{'s': common}, {'s': rare}], {'s': rare}, one, {'s': ['common']})
    assert ok and not asked and not got, asked
    tried += 1
    # A Rust Host that is late: an answer newer than the cursor its own snapshot handed out. The
    # reference, asked three times, is on time every time: it is never asked away.
    def stamped_in(revision):
        made = stamped(revision)
        return {**made, 'core': {**made['core'], 'scenario': 's', 'error': None}}
    late, on_time = stamped_in(1), stamped_in(0)
    got, _, lines, ok = run([{'s': on_time}] * 2, {'s': late}, one, {'s': [on_time]})
    assert not ok and asked == every and 'unexplained' in lines[-1], (asked, lines[-1])
    tried += 1
    # A Rust Host that changed: an answer the reference never gives, however often it is asked
    # (the planted changes are of this kind), while the reference wavers between two others.
    got, _, lines, ok = run([{'s': common}, {'s': rare}], {'s': answer('changed')}, one, {'s': ['rare', 'common']})
    assert not ok and asked == every and 'unexplained' in lines[-1], (asked, lines[-1])
    tried += 1
    # A Rust Host that did not run to its end is not asked about: that fails the run as it did.
    got, _, lines, ok = run([{'s': common}] * 2, {'s': answer('rare', error='timed out')}, one, {'s': ['rare']})
    assert not ok and not asked and not got, asked
    tried += 1
    # Nothing unexplained, or only what a register says: nothing is asked, nothing is said.
    got, _, lines, ok = run([{'s': common}] * 2, {'s': common}, one, {'s': ['rare']})
    assert ok and not asked and not got and not any(line.startswith('# Asked again') for line in lines), asked
    ident = differences({'s': common}, {'s': rare}, one, view='core').items[0]['id']
    got, _, lines, ok = run([{'s': common}] * 2, {'s': rare}, one, {'s': ['rare']}, {ident})
    assert ok and not asked and not got, asked
    tried += 2
    # Two scenarios: each is asked all three times, and each is judged on its own.
    two = [{'id': 's'}, {'id': 't'}]
    got, _, lines, ok = run([each('common', two)] * 2, each('rare', two), two, {'s': ['rare'], 't': ['common']})
    assert not ok and asked == [(attempt, ['s', 't']) for attempt in range(1, MORE_TRIES + 1)], asked
    assert place_line + "3 of 3 gave the Rust Host's answer: the reference's usual answer, explained" in lines and \
        "  t / http / [0].body.answer: 0 of 3 gave the Rust Host's answer: STILL UNEXPLAINED" in lines, lines
    tried += 1
    # The bound: more than ASK_MOST scenarios with an unexplained difference is a real difference,
    # judged as it stands, and the report says it was not asked again.
    many = [{'id': f'm{n}'} for n in range(ASK_MOST + 1)]
    got, _, lines, ok = run([each('common', many)] * 2, each('rare', many), many, {'*': ['rare']})
    assert not ok and not asked and not got, asked
    assert any(line.startswith(f'NOT ASKED AGAIN: {ASK_MOST + 1} scenarios') for line in lines), lines
    tried += 1
    # ... and ASK_MOST of them are asked.
    some = many[:ASK_MOST]
    got, _, lines, ok = run([each('common', some)] * 2, each('rare', some), some, {'*': ['rare']})
    assert ok and asked == [(attempt, [s['id'] for s in some]) for attempt in range(1, MORE_TRIES + 1)], asked
    tried += 1
    # What was written down is judged again the same way: the answers as `load_asked` gives them.
    with tool_scratch.directory() as where:
        first = Path(where) / 'golden'
        for attempt, answers in ((1, {'s': common}), (2, {'s': rare})):
            write_golden(asked_dir(first, attempt), answers)
        assert load_asked(first, one) == {1: {'s': common}, 2: {'s': rare}}, load_asked(first, one)
        assert load_asked(first, [{'id': 't'}]) == {1: {}, 2: {}}
    tried += 1
    return tried


def shapes_tried() -> int:
    """The rules for defects told by their shape, tried on rows made for them: each tells its
    defect where the rows give it away, and nothing anywhere else. Raises when one does
    otherwise; answers how many cases were tried."""
    tried = 0

    def answered(*bodies):
        return {'http': [{'body': body} for body in bodies]}

    def at(locator, kind='changed', channel='http'):
        return {'channel': channel, 'kind': kind, 'locator': locator}

    def told(expected, locator, base, head, bases=(), **more):
        nonlocal tried
        tried += 1
        found = told_by_shape(at(locator, **more), base, head, bases)
        assert found == expected, (tried, locator, expected, found)

    # R6: the reply that closes a completed turn, unmarked by the reference and final here.
    def closing(phase, status='completed', text='The answer'):
        return {'role': 'assistant', 'phase': phase, 'text': text, 'turn': {'status': status}}
    phase = '[0].body.turns[1].phase'
    theirs = answered({'turns': [{'role': 'user', 'text': 'A question'}, closing(None)]})
    ours = answered({'turns': [{'role': 'user', 'text': 'A question'}, closing('final')]})
    told('R6', phase, theirs, ours)
    told('', phase, ours, theirs)                                   # the other way round
    told('', phase, theirs, ours, kind='field-removed')             # another kind of difference
    told('', phase, theirs, ours, channel='sse')                    # another channel
    told('', '[0].body.turns[0].phase', theirs, ours)               # a row that closes nothing
    told('', '[0].body.turns[7].phase', theirs, ours)               # no such row
    told('', '[3].body.turns[1].phase', theirs, ours)               # no such exchange
    told('', '[0].body.turns[1].phase.more', theirs, ours)          # another place
    for theirs_row, ours_row in (
            (closing(None, 'failed'), closing('final', 'failed')),  # a turn that did not complete
            (closing(None), closing('final', 'failed')),
            (closing(None, text='One reply'), closing('final', text='Another')),   # not the same reply
            (closing(None, text=''), closing('final', text='')),
            (closing(None), closing('commentary')),                 # not what the contract asks for
            (closing('commentary'), closing('final')),              # the reference says something else
            ({'role': 'assistant', 'phase': None, 'text': 'The answer'}, closing('final')),
            ({**closing(None), 'role': 'user'}, {**closing('final'), 'role': 'user'})):
        told('', phase, answered({'turns': [{}, theirs_row]}), answered({'turns': [{}, ours_row]}))

    # R12: at rest, the last completed message; the reference's own row names the newer one.
    def row(completed, last='The answer', state='done', session='claire', **more):
        return {'session': session, 'latest_state': state, 'last_message': last,
                'last_completed_message': completed, **more}
    place = '[0].body.agents[0].last_completed_message'
    theirs, ours = answered({'agents': [row('You: the question')]}), answered({'agents': [row('The answer')]})
    told('R12', place, theirs, ours)
    told('R12', place, answered({'agents': [row('An older answer')]}), ours)
    told('', place, ours, theirs)
    told('', place, answered({'agents': [row('You: the question', last='Something else')]}), ours)
    told('', place, theirs, answered({'agents': [row('The answer', last='Not the answer')]}))
    told('', place, answered({'agents': [row('You: the question', state='thinking')]}), ours)
    told('', place, theirs, answered({'agents': [row('The answer', state='thinking')]}))
    told('', place, answered({'agents': [row('You: the question', session='mike')]}), ours)
    told('', place, theirs, answered({'agents': [row('', last='')]}))
    told('', '[0].body.agents[1].last_completed_message', theirs, ours)

    # R13: no model shown, though the reference shows that model for that agent elsewhere.
    def of(model, session='claire'):
        return {'agents': [{'session': session, 'model': model}]}
    place = '[0].body.agents[0].model'
    ours = answered(of('model-one'), of('model-one'))
    told('R13', place, answered(of(''), of('model-one')), ours)
    never = answered(of(''), of(''))
    told('', place, never, ours)                                    # the reference never shows it
    told('R13', place, never, ours, bases=(never, answered(of('model-one'))))   # another recording does
    told('', place, answered(of(''), of('model-two')), ours)        # it shows another model
    told('', place, answered(of(''), of('model-one', 'mike')), ours)   # for another agent
    told('', place, answered(of('model-two'), of('model-one')), ours)  # it shows one, another
    told('', place, ours, answered(of(''), of('model-one')))        # the other way round
    told('', place, answered(of(None), of('model-one')), ours)      # nothing at all is no empty text

    # R17: at rest after a turn, the line of what the agent is doing.
    def resting(line, state='done', session='claire'):
        return {'agents': [{'session': session, 'latest_state': state, 'live_activity': line}]}
    place = '[0].body.agents[0].live_activity'
    theirs, ours = answered(resting(None)), answered(resting({'state': 'idle'}))
    told('R17', place, theirs, ours)
    told('', place, ours, theirs)
    told('', place, answered(resting(None, state='thinking')), ours)
    told('', place, theirs, answered(resting({'state': 'idle'}, state='thinking')))
    told('', place, theirs, answered(resting('a text')))
    told('', place, answered(resting(None, session='mike')), ours)
    told('', place, answered(resting({'state': 'working'})), ours)

    # R19: the turn's end, which the reference has on no row of a conversation's first turn.
    ended = {'status': 'completed'}

    def reply(text='The answer', phase='final', **more):
        return {'role': 'assistant', 'phase': phase, 'text': text, **more}

    def spoken(*rows):
        return answered({'turns': [{'role': 'user', 'text': 'A question'}, *rows]})
    place = '[0].body.turns[2].turn'
    theirs = spoken(reply('A remark', 'commentary'), reply())
    ours = spoken(reply('A remark', 'commentary'), reply(turn=ended))
    for kind in ('field-added', 'changed'):
        told('R19', place, theirs, ours, kind=kind)
    told('R19', place, spoken(reply('A remark', 'commentary'), reply(turn=None)), ours)
    told('', place, theirs, ours, kind='field-removed')              # another kind of difference
    told('', place, ours, theirs, kind='field-added')                # the other way round
    told('', place, theirs, ours, kind='field-added', channel='sse')
    told('', '[0].body.turns[1].turn', theirs, ours, kind='field-added')    # a row that closes nothing
    told('', '[0].body.turns[9].turn', theirs, ours, kind='field-added')    # no such row
    for theirs_rows, ours_rows in (
            # The reference does not say the row closed the turn.
            ((reply('A remark', 'commentary'), reply(phase=None)), (reply('A remark', 'commentary'), reply(turn=ended))),
            # A turn that did not complete here.
            ((reply(), reply()), (reply(), reply(turn={'status': 'failed'}))),
            # Not the same reply.
            ((reply(), reply('One reply')), (reply(), reply('Another', turn=ended))),
            ((reply(), reply('')), (reply(), reply('', turn=ended))),
            # The reference has the end on another row of the turn: that is another difference.
            ((reply(turn=ended), reply()), (reply(), reply(turn=ended))),
            # Not what the contract asks for: the row that carries it here closed nothing.
            ((reply(), reply(phase='commentary')), (reply(), reply(phase='commentary', turn=ended))),
            ((reply(), {**reply(), 'role': 'user'}), (reply(), {**reply(turn=ended), 'role': 'user'}))):
        told('', place, spoken(*theirs_rows), spoken(*ours_rows), kind='field-added')
    # A later turn of what was read: the defect is known of a first turn, and anything else is looked at.
    later = [{'role': 'user', 'text': 'Before'}, reply('Earlier', turn=ended)]
    told('', '[0].body.turns[4].turn',
         answered({'turns': [*later, {'role': 'user', 'text': 'A question'}, reply('A remark', 'commentary'), reply()]}),
         answered({'turns': [*later, {'role': 'user', 'text': 'A question'}, reply('A remark', 'commentary'),
                             reply(turn=ended)]}), kind='field-added')
    # The row is not the turn's last: more of the agent's follows it.
    told('', place, spoken(reply('A remark', 'commentary'), reply(), reply('More')),
         spoken(reply('A remark', 'commentary'), reply(turn=ended), reply('More')), kind='field-added')
    # The next turn follows it: it is the first turn's last row still.
    told('R19', place, spoken(reply('A remark', 'commentary'), reply(), {'role': 'user', 'text': 'Next'}, reply(turn=ended)),
         spoken(reply('A remark', 'commentary'), reply(turn=ended), {'role': 'user', 'text': 'Next'}, reply(turn=ended)),
         kind='field-added')
    # R115, the late stamp: the reference's answer newer than the cursor at which a snapshot named it
    # complete; the Rust Host's not.
    def stamped(revision, words='The answer', named=True):
        agent = {'conversation_id': 'c', 'last_message_id': '<row:1>', 'head_revision': 1,
                 'last_completed_message': 'The answer' if named else ''}
        return answered({'agents': [agent]}, {'conversation_id': 'c', 'turns': [
            {'id': 'u-one', 'role': 'user', 'revision': 0, 'text': 'A question'},
            {'id': '<row:1>', 'role': 'assistant', 'revision': revision, 'text': words}]})
    place = '[1].body.turns[1].revision'
    told('R115', place, stamped(1), stamped(0))
    told('', place, stamped(0), stamped(1))                         # this Host late: never excused
    told('', place, stamped(1), stamped(1))                         # both late: nothing to tell
    told('', place, stamped(1), stamped(0, words='Other words'))    # not the same answer
    told('', place, stamped(1, words='Other words'), stamped(0, words='Other words'))  # its words not named
    told('', place, stamped(1, named=False), stamped(0))            # nothing was named complete there
    told('', place, stamped(1), stamped(0, named=False))            # nor here
    told('', place, stamped(1), stamped(0), kind='field-added')     # another kind of difference
    told('', '[1].body.turns[0].revision', stamped(1), stamped(0))  # a row nobody named
    return tried


def places_tried() -> int:
    """The rule for an event's place in its lane, tried on differences made for it: it holds
    where both names are the names of places in the lane the difference is in, and nowhere
    else. Raises when it does otherwise; answers how many cases were tried."""
    def named(kind, place, lane='0952e1'):
        return f'<eid:{kind}#{place}@{lane}>'

    def at(locator, base, head, kind='changed', channel='sse'):
        return {'channel': channel, 'kind': kind, 'locator': locator, 'base': base, 'head': head}
    lane = 'lane[agent-state@0952e1]'
    cases = (
        # The third event of the lane here, the eleventh there: under both of its names.
        (True, at(f'{lane}[2].id', named('agent-state', 3), named('agent-state', 11))),
        (True, at(f'{lane}[2].data.event_id', named('agent-state', 3), named('agent-state', 11))),
        # An activity in the lane of the states: the lane has the name of its first event.
        (True, at(f'{lane}[3].id', named('agent-activity', 4), named('agent-activity', 12))),
        # Fewer events before it there.
        (True, at(f'{lane}[9].id', named('agent-state', 10), named('agent-state', 2))),
        # A lane whose name is not one of the words the report prints as they are.
        (True, at('lane[str[12]#70fe911f@0952e1][0].id', named('agent-state', 1), named('agent-state', 5))),
        # Another kind of event at that place: not the same event at another place.
        (False, at(f'{lane}[2].id', named('agent-state', 3), named('agent-activity', 11))),
        # Another lane.
        (False, at(f'{lane}[2].id', named('agent-state', 3), named('agent-state', 11, 'f5caa0'))),
        (False, at(f'{lane}[2].id', named('agent-state', 3, 'f5caa0'), named('agent-state', 11, 'f5caa0'))),
        # The reference's name is not the name of the place the difference is at.
        (False, at(f'{lane}[2].id', named('agent-state', 7), named('agent-state', 11))),
        # An event named in another field, or in an answer: which event is meant is content.
        (False, at(f'{lane}[2].data.detail.event_id', named('agent-state', 3), named('agent-state', 11))),
        (False, at(f'{lane}[2].data.next.event_id', named('agent-state', 3), named('agent-state', 11))),
        (False, at('[4].body.event_id', named('agent-state', 3), named('agent-state', 11), channel='http')),
        (False, at(f'{lane}[2].id', named('agent-state', 3), named('agent-state', 11), channel='http')),
        # Another kind of difference at that place.
        (False, at(f'{lane}[2].id', named('agent-state', 3), named('agent-state', 11), kind='field-added')),
        (False, at(f'{lane}[2:2]->[2:10]', '-', 'message agent-state kind=tool', kind='sequence-insert')),
        # A name that is no place: an event the stream never carried, an event of the kept log.
        (False, at(f'{lane}[2].id', named('agent-state', 3), '<eid:?4>')),
        (False, at(f'{lane}[2].id', '<eid:log:agent-state#3@0952e1>', '<eid:log:agent-state#11@0952e1>')),
        (False, at(f'{lane}[2].id', named('agent-state', 3), 'str[12]#70fe911f')),
        (False, at(f'{lane}[2].id', named('agent-state', 3), 'null')),
    )
    for n, (expected, difference) in enumerate(cases):
        assert only_its_place(difference) is expected, (n, difference)
    return len(cases)




def record(build: Path, chosen, run: Run, jobs: int) -> tuple:
    """The Python Host's answers, twice. Returns (the recordings, lines of the report, ok).

    Where the two recordings differ, the reference itself answers in more than one way. That
    is a defect of the reference (they are listed in docs/reference-defects.md), and it does
    not stop the run: both answers are kept, and the Rust Host conforms in such a scenario
    when it gives one of them.

    One of those defects (R19) the reference shows in some recordings and not in others, and
    two recordings can both have it: then they agree, and neither is the answer the reference
    gives when it does not. A recording says of itself that it has it (`names_no_row`). A
    scenario whose two recordings both do is recorded again, up to `MORE_TRIES` times, and an
    answer without the mark is kept as a third recording: one more of the ways the reference
    answers. When the mark is there every time, that is how the reference answers, and
    nothing is added."""
    harness_run.pin_static_mtimes(build)
    first = corpus(python_host(build), chosen, run.label('py-a'), jobs)
    second = corpus(python_host(build), chosen, run.label('py-b'), jobs)
    # A test Host of the reference that did not come to rest in a scenario (its defect R43)
    # is asked again, and every time it happened is said.
    unfinished = []
    for at, (label, name) in enumerate((('python', 'py-a'), ('python again', 'py-b'))):
        whole, said = finished_again(
            (first, second)[at], chosen,
            lambda left, attempt, name=name: corpus(
                python_host(build), left, run.label(f'{name}-again-{attempt}'), jobs),
            label)
        first, second = (whole, second) if at == 0 else (first, whole)
        unfinished += said
    errors = run_errors(first, 'python') + run_errors(second, 'python again')
    report = differences(first, second, steady(chosen), view='core')
    lines = [diff.render(report, set(), 'the Python Host against itself', errors)]
    if unfinished:
        lines += ['', '# The reference did not run a scenario to its end (a test Host of it that did not come '
                  'to rest: its defect R43)', *unfinished]
    unsteady = differences(first, second, [s for s in chosen if s.get('unsteady')], view='core')
    if unsteady.items:
        lines.append(diff.render(unsteady, set(), 'scenarios the reference is known not to answer the same '
                                 'way twice (reported as defects of the reference; not counted)'))
    spread = sorted({d['scenario'] for d in report.items})
    lines.append('REFERENCE REPRODUCIBLE' if not spread and not errors else
                 'THE REFERENCE RAN WITH ERRORS: nothing below can be trusted' if errors else
                 f'THE REFERENCE ANSWERS IN MORE THAN ONE WAY in {", ".join(spread)}: '
                 'both answers are kept, and the Rust Host is compared with each')
    recordings = [first, second]
    if not errors:
        recordings, more = recorded_again(
            recordings, chosen,
            lambda left, attempt: corpus(python_host(build), left, run.label(f'py-more-{attempt}'), jobs))
        lines += more
    return recordings, lines, not errors


def planted_copy(build: Path, change: dict, where: Path) -> Path:
    """A copy of the build's server with one change planted; static, bin and contract are shared
    (the server reads the catalog of render specs beside itself, in `contract/catalogs`)."""
    if change.get('from_harness'):
        change = {**harness_planted.BY_ID[change['from_harness']], 'id': change['id']}
    shutil.copytree(build / 'server', where / 'server', ignore=shutil.ignore_patterns('__pycache__'))
    for shared in ('static', 'bin', 'contract'):
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


# How many scenarios may be set aside in a proof that writes the reference's answers down
# itself (`plants --fresh`) before it proves nothing.
FRESH_CUT_MOST = 3


def prove(build: Path, golden: dict, chosen, changes, run: Run, jobs: int) -> tuple:
    """Every planted change must show against what was written down.

    A planted change is caught when a scenario that ran to its end shows it. A scenario that
    did not run to its end under a plant (the planted reference Host timed out in it) differs
    from the recording in every way and proves nothing, for or against: what it shows is not
    counted, it is reported, and it does not undo what the other scenarios proved. The claim
    is that the suite can see the change, and one whole scenario that shows it is that."""
    lines, ok = ['# Planted changes', ''], True
    for change in changes:
        copy = planted_copy(build, change, run.label(f"plant-{change['id']}"))
        seen = corpus(python_host(copy), chosen, run.label(f"run-{change['id']}"), jobs)
        errors = run_errors(seen, change['id'])
        cut_short = {scenario for scenario, views in seen.items() if views['full']['error']}
        report = differences(golden, seen, steady(chosen), view='core')
        shown = [d for d in report.items if d['scenario'] not in cut_short]
        hits = caught(change, shown)
        verdict = 'CAUGHT' if hits else 'MISSED'
        ok = ok and verdict == 'CAUGHT'
        lines.append(f"{verdict:<7} {change['id']}: {change['what']} "
                     f"({len(hits)} of {len(shown)} differences show it)")
        lines += [f'        RUN ERROR {line} (not counted: the scenario did not run to its end)' for line in errors]
        if verdict == 'MISSED':
            lines += [f"        saw {d['scenario']} / {d['channel']} / {d['locator']} / {d['kind']}"
                      for d in shown[:12]]
    lines.append('')
    lines.append('EVERY PLANTED CHANGE WAS CAUGHT' if ok else 'A PLANTED CHANGE WAS MISSED: the suite cannot be trusted')
    return lines, ok


# ---- defects of the reference that are told by their shape ---------------------------------
#
# Some defects of the reference come and go with its clock, at places nobody can list: a reply
# that closes a turn left without its mark, an agent's row that is behind its own conversation.
# Two recordings can both have such a defect at a place where no run had it before, and then
# they agree with each other and the Rust Host differs from both. A difference there was
# registered by its id, one run and one place at a time, and every new place failed a run
# (three runs on 17 Nov 2026). Such a defect is told here by its shape instead: where the
# difference is, in which direction, and what the two rows it lies in say of themselves. A rule
# holds only where the reference's own row gives the defect away, or the Rust Host's row shows
# that what it says is the thing the contract asks for; anything else at such a place is a
# difference like any other. The defects are in server-rs/docs/reference-defects.md.

def _row(core: dict, exchange: int, list_name: str, at: int) -> dict:
    """The row of a list in the body of an exchange, or nothing that is a row."""
    exchanges = core.get('http') or []
    body = exchanges[exchange].get('body') if exchange < len(exchanges) else None
    rows = body.get(list_name) if isinstance(body, dict) else None
    row = rows[at] if isinstance(rows, list) and at < len(rows) else None
    return row if isinstance(row, dict) else {}


def _closes_a_completed_turn(row: dict) -> bool:
    turn = row.get('turn')
    return row.get('role') == 'assistant' and isinstance(turn, dict) and turn.get('status') == 'completed'


def _unmarked_reply(base: dict, head: dict, exchange: int, at: int, _bases) -> bool:
    """R6: the reply that closes a completed turn is `final`; the reference leaves it unmarked."""
    theirs, ours = _row(base, exchange, 'turns', at), _row(head, exchange, 'turns', at)
    return (_closes_a_completed_turn(theirs) and _closes_a_completed_turn(ours)
            and theirs.get('phase') is None and ours.get('phase') == 'final'
            and bool(ours.get('text')) and theirs.get('text') == ours.get('text'))


def _stale_last_completed(base: dict, head: dict, exchange: int, at: int, _bases) -> bool:
    """R12: at rest after a turn, the last completed message is the agent's last message; the
    reference's row still names an older one, beside the newer one it names as its last."""
    theirs, ours = _row(base, exchange, 'agents', at), _row(head, exchange, 'agents', at)
    said = ours.get('last_completed_message')
    return (bool(said) and said == ours.get('last_message') == theirs.get('last_message')
            and theirs.get('last_completed_message') != said
            and theirs.get('latest_state') == ours.get('latest_state') == 'done'
            and theirs.get('session') == ours.get('session'))


def _model_not_shown(base: dict, head: dict, exchange: int, at: int, bases) -> bool:
    """R13: the model that answered is shown after a turn; the reference shows none for the rest
    of a minute, though it shows that very model for that agent elsewhere in what was recorded."""
    theirs, ours = _row(base, exchange, 'agents', at), _row(head, exchange, 'agents', at)
    model, session = ours.get('model'), ours.get('session')
    if theirs.get('model') != '' or not model or not session or theirs.get('session') != session:
        return False
    for recording in bases:
        for answered in recording.get('http') or []:
            body = answered.get('body')
            rows = body.get('agents') if isinstance(body, dict) else None
            for row in rows if isinstance(rows, list) else ():
                if isinstance(row, dict) and row.get('session') == session and row.get('model') == model:
                    return True
    return False


def _no_line_at_rest(base: dict, head: dict, exchange: int, at: int, _bases) -> bool:
    """R17: after a turn that completed, the row says what the agent is doing (at rest); the
    reference leaves the line empty after an agent's first turn, in a row that says `done`."""
    theirs, ours = _row(base, exchange, 'agents', at), _row(head, exchange, 'agents', at)
    return (theirs.get('live_activity') is None and isinstance(ours.get('live_activity'), dict)
            and theirs.get('latest_state') == ours.get('latest_state') == 'done'
            and theirs.get('session') == ours.get('session'))


def _end_on_no_row(base: dict, head: dict, exchange: int, at: int, _bases) -> bool:
    """R19: once a turn has ended, its last row carries the turn's end (`turn`); in the first
    turn of a conversation the reference has it on no row, though its own row says that it
    closed the turn (`final`)."""
    theirs, ours = _row(base, exchange, 'turns', at), _row(head, exchange, 'turns', at)
    if not (_closes_a_completed_turn(ours) and ours.get('phase') == 'final' and bool(ours.get('text'))
            and theirs.get('role') == 'assistant' and theirs.get('phase') == 'final'
            and theirs.get('turn') is None and theirs.get('text') == ours.get('text')):
        return False
    body = base['http'][exchange].get('body')
    rows = [row if isinstance(row, dict) else {} for row in body['turns']]
    asked = [n for n, row in enumerate(rows) if row.get('role') == 'user']
    # The first turn of what was read, and it ends with this row: the next row, where there
    # is one, is a user's.
    if len([n for n in asked if n < at]) != 1 or (at + 1 < len(rows) and at + 1 not in asked):
        return False
    return not any(row.get('turn') is not None for row in rows[:at])


def _stamped_late_there(base: dict, head: dict, exchange: int, at: int, _bases) -> bool:
    """R115, the late stamp: an answer that was complete at a cursor a snapshot handed out is not newer
    than that cursor; the reference's row is, with the same words. Held only where the Rust Host's
    own row is the same answer and is not newer than the cursor its own snapshot handed out, so a
    Host that stamped its rows late is told nothing by this rule."""
    theirs, ours = _row(base, exchange, 'turns', at), _row(head, exchange, 'turns', at)
    if not theirs or theirs.get('id') != ours.get('id') or theirs.get('text') != ours.get('text'):
        return False
    if not stamped_late(base, exchange, theirs):
        return False
    body = head['http'][exchange].get('body') if exchange < len(head.get('http') or []) else None
    if not isinstance(body, dict) or not isinstance(ours.get('revision'), int):
        return False
    named = named_complete(head, exchange, body.get('conversation_id'), ours.get('id'), ours.get('text'))
    return named is not None and ours['revision'] < named


# The defect, the kinds of difference it shows as, where a difference of its shape lies (the
# exchange and the row), and what the rows have to say for it to be that defect.
SHAPES = (
    ('R6', ('changed',), re.compile(r'^\[(\d+)\]\.body\.turns\[(\d+)\]\.phase$'), _unmarked_reply),
    ('R12', ('changed',), re.compile(r'^\[(\d+)\]\.body\.agents\[(\d+)\]\.last_completed_message$'),
     _stale_last_completed),
    ('R13', ('changed',), re.compile(r'^\[(\d+)\]\.body\.agents\[(\d+)\]\.model$'), _model_not_shown),
    ('R17', ('changed',), re.compile(r'^\[(\d+)\]\.body\.agents\[(\d+)\]\.live_activity$'), _no_line_at_rest),
    # A row without the field at all, or with nothing in it.
    ('R19', ('field-added', 'changed'), re.compile(r'^\[(\d+)\]\.body\.turns\[(\d+)\]\.turn$'), _end_on_no_row),
    # Its number is the release owner's to give; until then it goes by what it is.
    ('R115', ('changed',), re.compile(r'^\[(\d+)\]\.body\.turns\[(\d+)\]\.revision$'), _stamped_late_there),
)


def told_by_shape(difference: dict, base: dict, head: dict, bases=()) -> str:
    """The defect of the reference a difference is an instance of, by its shape; '' for none.
    `base` and `head`: what the reference and the Rust Host answered in the scenario (the view
    that is compared); `bases`: every recording of the reference there is of it."""
    if difference.get('channel') != 'http':
        return ''
    for defect, kinds, where, holds in SHAPES:
        found = where.match(difference.get('locator') or '')
        if (found and difference.get('kind') in kinds
                and holds(base, head, int(found.group(1)), int(found.group(2)), bases or (base,))):
            return defect
    return ''


# ---- an event's place in its lane ----------------------------------------------------------
#
# An event of the stream is named after its lane and its place in it (`<eid:agent-state#3@0952e1>`:
# the third event of that lane; tests/replay/normalize.py, `lane_names`), since the number a Host
# gives an event is its own. A lane is compared as a sequence: events one Host has more or fewer
# show as that, and the events after them are paired and compared field by field. Each of those
# then differs in its name, twice (`id`, `data.event_id`), by as many places as were added or are
# missing before it. Such a difference says nothing of its own: it cannot be there without the
# events before it, each of which the report shows by itself and which is registered or
# unexplained as what it is. It was registered by its id, two lines for every event after the
# place (forty lines for the three scenarios an agent uses tools in). It is told by its shape
# instead: the name on each side is the name of that side's own place.
PLACE = 'its place'
ITS_PLACE = re.compile(r'^lane\[.+@([0-9a-f]{6})\]\[(\d+)\]\.(?:id|data\.event_id)$')
NAMED_BY_PLACE = re.compile(r'^<eid:([a-z][a-z0-9_-]{0,40})#(\d+)@([0-9a-f]{6})>$')


def only_its_place(difference: dict) -> bool:
    """Whether a difference says only where an event stands in its lane: on both sides the name
    of an event of the same kind in the same lane, the reference's the name of the place the
    difference is at, the Rust Host's the name of another place."""
    found = ITS_PLACE.match(difference.get('locator') or '')
    if difference.get('channel') != 'sse' or difference.get('kind') != 'changed' or not found:
        return False
    theirs, ours = (NAMED_BY_PLACE.match(str(difference.get(side) or '')) for side in ('base', 'head'))
    return bool(theirs and ours and theirs.group(1) == ours.group(1)
                and theirs.group(3) == ours.group(3) == found.group(1)
                and int(theirs.group(2)) == int(found.group(2)) + 1
                and int(ours.group(2)) != int(theirs.group(2)))


def told_by_rule(difference: dict, base: dict, head: dict, bases=()) -> str:
    """What a rule says a difference is, as the report words it; '' where no rule says anything."""
    defect = told_by_shape(difference, base, head, bases)
    return f'defect {defect}' if defect else PLACE if only_its_place(difference) else ''


def rendered(report, intended: set, shaped: dict, title: str, errors=(), awaited=()) -> str:
    """A report of differences in six kinds: registered by its id as intended, registered as
    awaited (the reference moved and this Host's port is owed), a defect of the reference told
    by its shape, an event's place in its lane, the reference's usual answer when it was asked
    again (`usual`), and unexplained. `shaped`: what a rule says of a
    difference, by its id (`told_by_rule`)."""
    registered = lambda d: d['id'] in intended or d['id'] in awaited  # noqa: E731
    explained = lambda d: registered(d) or d['id'] in shaped  # noqa: E731
    unexplained = [d for d in report.items if not explained(d)]
    ruled = [shaped[d['id']] for d in report.items if not registered(d) and d['id'] in shaped]
    waiting = sum(1 for d in report.items if d['id'] in awaited and d['id'] not in intended)
    placed = ruled.count(PLACE)
    usual_answer = ruled.count(ASKED)
    by_shape = len(ruled) - placed - usual_answer
    lines = [f'# Replay difference report: {title}', '',
             f'differences: {len(report.items)}  registered intended: '
             f'{len(report.items) - len(unexplained) - len(ruled) - waiting}  awaited: {waiting}  '
             f'a defect of the reference by its shape: '
             f'{by_shape}  only an event\'s place in its lane: {placed}  '
             + (f"the reference's usual answer when asked again: {usual_answer}  " if usual_answer else '')
             + f'unexplained: {len(unexplained)}']
    counts: dict = {}
    for d in report.items:
        counts[d['scenario']] = counts.get(d['scenario'], 0) + 1
    lines += [f'  {scenario}: {count}' for scenario, count in sorted(counts.items())]
    lines += [f'RUN ERROR {line}' for line in errors]
    if report.items:
        lines += ['', 'id            class        scenario / channel / locator / kind: base -> head']
    for d in report.items:
        kind = ('intended' if d['id'] in intended else 'awaited' if d['id'] in awaited
                else shaped[d['id']] if d['id'] in shaped else 'UNEXPLAINED')
        lines.append(f"{d['id']}  {kind:<11}  {d['scenario']} / {d['channel']} / {d['locator']} / "
                     f"{d['kind']}: {d['base']} -> {d['head']}")
    return '\n'.join(lines) + '\n'


def verdict(unexplained: list, errors, awaited: dict, shown: set, chosen) -> tuple:
    """The last lines of a report and whether the Rust Host conforms. Every awaited difference
    of a scenario that ran has to show (`shown`: the ids the run showed): one that does not is
    stale, and fails the run by its id, so a port cannot land without taking its lines out."""
    ran = {scenario['id'] for scenario in chosen}
    owed = {ident: line for ident, line in awaited.items() if line['scenario'] in ran}
    seen = sorted(ident for ident in owed if ident in shown)
    stale = sorted(ident for ident in owed if ident not in shown)
    lines = []
    if stale:
        lines.append(f'AWAITED AND NOT SEEN: take it out of awaited.txt: {", ".join(stale)}')
    ok = not errors and not unexplained and not stale
    if ok and seen:
        prs = sorted({owed[ident]['pr'] for ident in seen})
        named = '; '.join(f"PR {pr} ({', '.join(sorted({owed[i]['branch'] for i in seen if owed[i]['pr'] == pr}))}"
                          f" owes it): {', '.join(i for i in seen if owed[i]['pr'] == pr)}" for pr in prs)
        lines.append(f'THE RUST HOST CONFORMS BUT FOR {len(seen)} AWAITED DIFFERENCE(S): {named}')
    elif ok:
        lines.append('THE RUST HOST CONFORMS')
    else:
        said = f'THE RUST HOST DIFFERS: {len(unexplained)} unexplained difference(s), {len(errors)} run error(s)'
        if stale:
            said += f', {len(stale)} awaited difference(s) not seen'
        lines.append(said)
    return lines, ok


def held(recordings: list, seen: dict, chosen, registered: set) -> tuple:
    """The Rust Host held to the recordings of the reference: the differences that count (a
    report), those a rule tells (id -> what it says), and where it was held to more than the
    first recording (lines)."""
    report, held_to, shaped = diff.Report(), [], {}
    for scenario in steady(chosen):
        name = scenario['id']
        against = [differences(recording, seen, [scenario], view='core').items for recording in recordings]
        # A place counts when the Rust Host differs there from every recording. Where it gives
        # what one recording gave and another did not, the reference itself answers in more
        # than one way at that place, and the Rust Host gives one of its answers.
        places = [{place(d) for d in items} for items in against]
        everywhere = set.intersection(*places)
        counted = [[d for d in items if place(d) in everywhere] for items in against]
        bases = [recording[name]['core'] for recording in recordings]
        head = seen[name]['core']
        told = [{d['id']: told_by_rule(d, bases[n], head, bases) for d in counted[n] if d['id'] not in registered}
                for n in range(len(recordings))]
        nearest = min(range(len(recordings)), key=lambda n: sum(1 for defect in told[n].values() if not defect))
        report.items.extend(counted[nearest])
        shaped.update({ident: defect for ident, defect in told[nearest].items() if defect})
        if nearest:
            held_to.append(f"{name}: recording {nearest + 1}")
        elsewhere = sorted(set().union(*places) - everywhere)
        if elsewhere:
            held_to.append(f"{name}: at {len(elsewhere)} place(s) the recordings differ and the Rust "
                           f"Host gives one of their answers ({'; '.join(' / '.join(p) for p in elsewhere[:6])}"
                           f"{' ...' if len(elsewhere) > 6 else ''})")
    return report, shaped, held_to


def unexplained_at(recordings: list, seen: dict, chosen, registered: set) -> dict:
    """Scenario id -> the places (`place`) where the Rust Host differs from every recording and
    neither a register nor a rule says why."""
    report, shaped, _ = held(recordings, seen, chosen, registered)
    places: dict = {}
    for difference in report.items:
        if difference['id'] not in registered and difference['id'] not in shaped:
            places.setdefault(difference['scenario'], set()).add(place(difference))
    return places


# How many scenarios a run asks the reference again about, at most (`asked_again`). A run with
# more is a real difference, judged as it stands.
ASK_MOST = 5


def to_ask(recordings: list, seen: dict, chosen, registered: set) -> tuple:
    """The scenarios a difference is not called unexplained in before the reference has been
    asked again (those with an unexplained difference where the Rust Host ran to its end), and
    whether there are too many to ask: (their ids, too many)."""
    left = sorted(name for name in unexplained_at(recordings, seen, chosen, registered)
                  if not seen[name]['full']['error'])
    return left, len(left) > ASK_MOST


# How many of the recordings asked for (`asked_again`) must give the Rust Host's answer at a place
# for it to be the reference's usual answer there.
USUAL = 2
# What the report calls a difference explained so (`judged`).
ASKED = 'asked again'


def asked_again(recordings: list, seen: dict, chosen, registered: set, again) -> tuple:
    """A difference is not called unexplained before the reference has been asked again.

    Every scenario where the Rust Host would end with an unexplained difference is recorded
    again by the reference (`again(scenarios, attempt)` answers scenario id -> its two views),
    always `MORE_TRIES` times: nothing stops early. What those recordings say is `usual`: a
    place is explained only where at least `USUAL` of them give the Rust Host's answer. The
    Rust Host is never recorded again: the reference may waver, it may not. A scenario where
    the Rust Host did not run to its end is not asked about (that fails the run as before), and
    a run with more than `ASK_MOST` such scenarios is not asked about at all: it is judged as it
    stands. Answers (try -> scenario id -> views, only those that ran to their end) and lines
    for a try that did not."""
    left, too_many = to_ask(recordings, seen, chosen, registered)
    if not left or too_many:
        return {}, []
    asking = [scenario for scenario in steady(chosen) if scenario['id'] in left]
    answers: dict = {}
    unfinished = []
    for attempt in range(1, MORE_TRIES + 1):
        more = again(asking, attempt)
        got = {}
        for scenario in asking:
            views = more[scenario['id']]
            if views['full']['error']:
                unfinished.append(f"{scenario['id']}: asked again (try {attempt}), and it did not run to its end: "
                                  f"{views['full']['error']}")
            else:
                got[scenario['id']] = views
        if got:
            answers[attempt] = got
    return answers, unfinished


def usual(recordings: list, answers: dict, seen: dict, chosen, registered: set) -> tuple:
    """What the recordings asked for say (`asked_again`): (place (scenario, channel, locator) ->
    (how many of them gave the Rust Host's answer there, how many ran to their end), the
    scenarios asked about, whether there were too many to ask). Only the places that are
    unexplained against the first recordings are counted, and those recordings count for
    nothing here: they are why the scenario was asked."""
    left, too_many = to_ask(recordings, seen, chosen, registered)
    counts: dict = {}
    if too_many:
        return counts, left, True
    before = unexplained_at(recordings, seen, chosen, registered)
    for name in left:
        later = [got[name] for _, got in sorted(answers.items()) if name in got]
        scenario = [s for s in chosen if s['id'] == name]
        differing = [{place(d) for d in differences({name: views}, seen, scenario, view='core').items}
                     for views in later]
        for where in before[name]:
            counts[(name, *where)] = (sum(1 for found in differing if where not in found), len(later))
    return counts, left, False


def said_of_asking(counts: dict, left: list, too_many: bool, answers: dict) -> list:
    """What the report says of `asked_again`: every scenario asked again, how many of its later
    recordings ran to their end, and for each place how many of them gave the Rust Host's
    answer. That list goes to the release owner: a place explained is the reference wavering
    with this Host on its usual answer, or this Host having moved into an answer the reference
    gives often enough to be its usual one."""
    if too_many:
        return ['', '# Asked again', f'NOT ASKED AGAIN: {len(left)} scenarios end with an unexplained difference, '
                f'more than {ASK_MOST}: a real difference, judged as it stands ({", ".join(left)})']
    if not answers:
        return []
    lines = ['', f'# Asked again: a difference is not called unexplained before the reference has been asked again, '
             f'{MORE_TRIES} times; a place is explained where at least {USUAL} of those recordings give the Rust '
             "Host's answer (the first recordings count for nothing here; the Rust Host is never asked again)"]
    for name in left:
        ran = sum(1 for got in answers.values() if name in got)
        lines.append(f'{name}: recorded {MORE_TRIES} more time(s), {ran} ran to their end')
        for (scenario, channel, locator), (gave, of) in sorted(counts.items()):
            if scenario == name:
                lines.append(f"  {name} / {channel} / {locator}: {gave} of {of} gave the Rust Host's answer: "
                             + ("the reference's usual answer, explained" if gave >= USUAL else 'STILL UNEXPLAINED'))
    return lines


def judged(recordings: list, seen: dict, chosen, intended: set, errors, awaited=None, answers=None) -> tuple:
    """What the Rust Host answered (`seen`), held to the recordings of the reference: the lines
    of the report, and whether it conforms. Nothing is started here: a run that was written
    down can be judged again. `answers` are what the reference answered when it was asked again
    (`asked_again`), try -> scenario id -> views: they are not more recordings held to the rule
    above; a place they speak to is explained only where at least `USUAL` of them give the Rust
    Host's answer (`usual`), and the report says each."""
    awaited = awaited or {}
    both_registers(intended, awaited)
    registered = intended | set(awaited)
    answers = answers or {}
    counts, left, too_many = usual(recordings, answers, seen, chosen, registered)
    report, shaped, held_to = held(recordings, seen, chosen, registered)
    for d in report.items:
        if d['id'] not in registered and d['id'] not in shaped:
            if counts.get((d['scenario'], *place(d)), (0, 0))[0] >= USUAL:
                shaped[d['id']] = ASKED
    unexplained = [d for d in report.items if d['id'] not in registered and d['id'] not in shaped]
    lines = [rendered(report, intended, shaped, 'the Python Host (base) against the Rust Host (head)', errors,
                      awaited)]
    by_defect: dict = {}
    for said in shaped.values():
        if said not in (PLACE, ASKED):
            defect = said.removeprefix('defect ')
            by_defect[defect] = by_defect.get(defect, 0) + 1
    if by_defect:
        lines += ['defects of the reference told by their shape: '
                  + ', '.join(f'{defect} at {count} place(s)' for defect, count in sorted(by_defect.items())), '']
    if held_to:
        lines += ['', 'held to more than the first recording:', *(f'  {line}' for line in held_to)]
    lines += ['', '# The same differences by shape', *diff.shapes(report, top=200),
              *said_of_asking(counts, left, too_many, answers)]
    said, ok = verdict(unexplained, errors, awaited, {d['id'] for d in report.items}, chosen)
    lines += ['', *said]
    return lines, ok


def against_rust(binary: Path, recordings: list, chosen, intended: set, run: Run, jobs: int,
                 awaited=None, again=None) -> tuple:
    """The Rust Host against what the Python Host answered. Where the reference was recorded
    answering in more than one way, the answer the Rust Host is nearest to is the one it is
    held to; the report says which recording that was. With `again` (cross, where both Hosts
    are there) the reference is asked again before a difference is called unexplained
    (`asked_again`). The lines, whether it conforms, what the Rust Host answered and the answers
    of the reference asked again."""
    seen = corpus(rust_host(binary), chosen, run.label('rust'), jobs)
    answers, unfinished = {}, []
    if again is not None:
        answers, unfinished = asked_again(recordings, seen, chosen, intended | set(awaited or {}), again)
    lines, ok = judged(recordings, seen, chosen, intended, run_errors(seen, 'rust'), awaited, answers)
    if unfinished:
        lines[-1:-1] = ['', '# Asked again and not run to its end (no recording is made of it)', *unfinished, '']
    return lines, ok, seen, answers


def choose(names):
    if not names:
        return scenarios.SCENARIOS
    unknown = [name for name in names.split(',') if name not in scenarios.BY_ID]
    if unknown:
        raise SystemExit(f'unknown scenario(s): {", ".join(unknown)}')
    return [scenarios.BY_ID[name] for name in names.split(',')]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('mode', choices=('record', 'plants', 'compare', 'cross', 'rust', 'judge', 'selftest'),
                        help='judge: a run that was written down is judged again as the rules and the register '
                             'are now, without starting a Host: the recordings of the reference under --golden '
                             '(and beside it -again, -more), what the Rust Host answered under --out/rust')
    parser.add_argument('--build', type=Path, help='a checkout of the repository: the Python Host under ./server')
    parser.add_argument('--rust', type=Path, help='the clarp-host binary (clarp-shim beside it)')
    parser.add_argument('--golden', type=Path, default=HERE / 'golden')
    parser.add_argument('--out', type=Path, help='cross: where the report and the written-down answers go; '
                        'compare: where what the Rust Host answered goes, when it is wanted; in every mode: '
                        'what a test Host left of a scenario it did not run to its end goes under it '
                        '(unfinished/)')
    parser.add_argument('--intended', default=str(HERE / 'intended.txt'))
    parser.add_argument('--awaited', default=str(HERE / 'awaited.txt'),
                        help='the register of awaited differences: the reference moved on main and this '
                             "Host's port is owed (README.md, \"Awaited\")")
    parser.add_argument('--scenarios', help='a comma-separated subset of the corpus')
    parser.add_argument('--plant', type=int, help='plants: only the planted change of this number, from 0')
    parser.add_argument('--fresh', action='store_true',
                        help='plants: the Python Host of --build is asked first, once, and the planted changes '
                             'have to show against that, not against recordings that were kept')
    parser.add_argument('--plants-beside', type=int, metavar='N',
                        help='cross: the planted changes are tried beside this run, each in a run of its own '
                             '(conformance.yml), of which there are N: refused unless the suite has N')
    parser.add_argument('--jobs', type=int, default=4)
    parser.add_argument('--keep', action='store_true', help='export the run files next to the report (or to CLARP_CONFORM_SCRATCH)')
    args = parser.parse_args(argv)
    chosen = choose(args.scenarios)
    if args.out is not None:
        global UNFINISHED
        UNFINISHED = args.out.resolve() / 'unfinished'
    for needed, modes in (('build', ('record', 'plants', 'cross')), ('rust', ('compare', 'cross', 'rust')),
                          ('out', ('cross', 'rust', 'judge'))):
        if args.mode in modes and getattr(args, needed) is None:
            parser.error(f'{args.mode} needs --{needed}')
    build = args.build.resolve() if args.build else None
    rust = args.rust.resolve() if args.rust else None
    intended, awaited = load_intended(args.intended), load_awaited(args.awaited)
    both_registers(intended, awaited)
    known_scenarios(awaited, scenarios.BY_ID)
    if args.mode == 'selftest':
        print('\n'.join(selftest()))
        return 0
    if args.mode == 'judge':
        selftest()
        recordings = [load_golden(directory, chosen) for directory in golden_dirs(args.golden)
                      if directory == args.golden or directory.is_dir()]
        seen = load_golden(args.out / 'rust', chosen)
        errors = [error for n, recording in enumerate(recordings) for error in run_errors(recording, f'recording {n + 1}')]
        lines, ok = judged(recordings, seen, chosen, intended, errors + run_errors(seen, 'rust'), awaited,
                           load_asked(args.golden, chosen))
        print('\n'.join(lines))
        return 0 if ok else 1
    with tool_scratch.directory('cfm-') as place:
        run = Run(Path(place))
        ok, lines = True, []
        if args.mode in ('record', 'cross'):
            # The tool's own rule is tried before the tool judges with it.
            selftest()
        if args.mode == 'record':
            recordings, lines, ok = record(build, chosen, run, args.jobs)
            if ok:
                for directory, seen in zip(golden_dirs(args.golden), recordings):
                    write_golden(directory, seen)
                lines.append(f'written down: {len(recordings[0])} scenario(s), {len(recordings)} times, '
                             f'in {args.golden}')
        elif args.mode == 'plants':
            changes = plants.CHANGES
            if args.plant is not None:
                if not 0 <= args.plant < len(changes):
                    raise SystemExit(f'no planted change {args.plant}: the suite has {len(changes)}')
                changes = [changes[args.plant]]
            if args.fresh:
                # What a planted change has to show against is written down here and now, once.
                # A scenario the reference did not run to its end in (a test Host that was slow
                # to come to rest) is no ground to stand on: it is set aside and said, as a
                # scenario that is cut short under a plant is. More than a few and nothing is
                # proved at all.
                harness_run.pin_static_mtimes(build)
                golden = corpus(python_host(build), chosen, run.label('py-a'), args.jobs)
                cut = sorted(name for name, views in golden.items() if views['full']['error'])
                tried = [scenario for scenario in chosen if scenario['id'] not in cut]
                lines, ok = prove(build, golden, tried, changes, run, args.jobs)
                said = f'written down: {len(golden)} scenario(s), once'
                if cut:
                    said += (f'; {len(cut)} did not run to the end in the reference and are set aside: '
                             f'{", ".join(cut)}')
                if len(cut) > FRESH_CUT_MOST:
                    ok = False
                    said += f'\nTOO MANY ARE SET ASIDE (more than {FRESH_CUT_MOST}): nothing is proved'
                lines = [said, '', *lines]
            else:
                lines, ok = prove(build, load_golden(args.golden, chosen), chosen, changes, run, args.jobs)
        elif args.mode == 'compare':
            recordings = [load_golden(directory, chosen) for directory in golden_dirs(args.golden)
                          if directory == args.golden or directory.is_dir()]
            lines, ok, rust_seen, _ = against_rust(rust, recordings, chosen, intended, run, args.jobs, awaited)
            if args.out:
                write_golden(args.out / 'rust', rust_seen)
                (args.out / 'report.txt').write_text('\n'.join(lines) + '\n')
        elif args.mode == 'rust':
            rust_seen = corpus(rust_host(rust), chosen, run.label('rust'), args.jobs)
            errors = run_errors(rust_seen, 'rust')
            write_golden(args.out / 'rust', rust_seen)
            lines = [f'{len(rust_seen)} scenario(s) run against the Rust Host and compared with nothing; '
                     f'what it answered is in {args.out / "rust"}', *errors]
            ok = not errors
        else:
            args.out.mkdir(parents=True, exist_ok=True)
            recordings, lines, ok = record(build, chosen, run, args.jobs)
            for directory, seen in zip(golden_dirs(args.out / 'golden'), recordings):
                write_golden(directory, seen)
            if args.scenarios:
                # A question about some scenarios: the planted changes are tried on the whole
                # corpus only, where each of them has a scenario that can show it. A part of
                # the corpus proves nothing about the suite, and gates nothing.
                proof = ['# Planted changes', '',
                         'NOT TRIED: a part of the corpus was asked for; the whole corpus is the gate']
                proved = True
            elif args.plants_beside is not None:
                # Each planted change runs the whole corpus once: three quarters of what the gate
                # runs. They are tried at the same time as this, each in a run of its own, and the
                # gate is this run and all of those. A planted change nobody tries would be a
                # suite nobody proved: the count has to be the suite's.
                proved = args.plants_beside == len(plants.CHANGES)
                proof = ['# Planted changes', '',
                         f'TRIED BESIDE THIS RUN: {args.plants_beside} planted changes, each in a run of its own'
                         if proved else
                         f'{args.plants_beside} PLANTED CHANGES ARE TRIED BESIDE THIS RUN AND THE SUITE HAS '
                         f'{len(plants.CHANGES)}: give every one a run in conformance.yml']
            else:
                proof, proved = prove(build, recordings[0], chosen, plants.CHANGES, run, args.jobs)
            # The reference is asked again only where it ran without errors: otherwise nothing
            # below can be trusted anyway.
            again = (lambda left, attempt: corpus(python_host(build), left, run.label(f'py-ask-{attempt}'),
                                                  args.jobs)) if ok else None
            rust_lines, conforms, rust_seen, answers = against_rust(
                rust, recordings, chosen, intended, run, args.jobs, awaited, again)
            write_golden(args.out / 'rust', rust_seen)
            for attempt, got in answers.items():
                write_golden(asked_dir(args.out / 'golden', attempt), got)
            lines += ['', *proof, '', *rust_lines]
            ok = ok and proved and conforms
            (args.out / 'report.txt').write_text('\n'.join(lines) + '\n')
        if args.keep:
            archive = Path(os.environ.get('CLARP_CONFORM_SCRATCH') or args.out or Path.cwd())
            archive.mkdir(parents=True, exist_ok=True)
            destination = archive / run.path.name
            shutil.move(str(run.path), str(destination))
            print(f'kept: {destination}')
    print('\n'.join(lines))
    return 0 if ok else 1


if __name__ == '__main__':
    with contextlib.suppress(AttributeError):
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    sys.exit(main())
