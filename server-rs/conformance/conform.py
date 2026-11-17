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
SCRATCH = Path(os.environ.get('CLARP_CONFORM_SCRATCH', '/var/tmp/cfm'))
HTTP_OPS = ('get', 'post', 'put', 'delete', 'patch')
# How long a conversation must stay as it is before it counts as settled. The Python Host goes on
# working after a turn has ended: it imports the tool's own transcript and folds it into the rows
# it wrote while the turn ran. A read before that is done sees a state no client is meant to keep.
SETTLED_S = 3.0
SETTLE_TIMEOUT_S = 30
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
        claude_shim(provider / 'bin' / 'claude')
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
    'send_delivery_state': {
        'fields': ('delivery_scope', 'delivery_stop_generation', 'delivery'),
        'events': ('delivery-updated',),
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
# The lists an app's screens are made of, for what the Rust Host keeps nothing of yet: janitors,
# the engines that listen as a Host with other engines lists them, the models an agent can be
# given. (The catalogue of voices is not among them any more: it is compared whole.) It answers each as the Python Host
# answers a list, and empty, so that an app shows nothing there instead of an error. What is
# compared of such an answer is what an app's reader needs of it: the status, and every field
# with its kind. What the fields hold is in server-rs/docs/differences.md as not built.
NOT_KEPT = frozenset({'/janitors', '/transcription-capabilities', '/transcription-providers', '/agent-model-options',
                      # Screens of what this Host has not, where the Python Host's first day says something
                      # this Host must not: that a picture could be made with a key, what the Oracle
                      # could do. Compared as their fields.
                      '/agent-portrait-generation', '/oracle/status'})
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


def what_was_spent(body):
    """`GET /backend-usage` as what both Hosts count in the same way: the row of the tool the
    scenarios' agent runs on, which is what the Host's own turns used (its sums over the two
    windows, where they come from and how fresh they are), with the moment of the newest turn
    as just a moment. The rest is each Host's own: how much of an account is left, which the
    Python Host asks providers for with the accounts' logins and this Host asks nobody; and
    the other tools each knows. Of that, the fields and their kinds are compared."""
    if not isinstance(body, dict) or not isinstance(body.get('backends'), list):
        return body
    spent = [{**row, 'fetched_at': '<time>' if row.get('fetched_at') else row.get('fetched_at')}
             for row in body['backends'] if isinstance(row, dict) and row.get('backend') == 'claude']
    return {**outline(body), 'backends': spent}


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
    janitors_made = made_new(raw['http'], {})
    if janitors_made:
        raw = {**raw, 'http': by_name(raw['http'], janitors_made), 'sse': by_name(raw['sse'], janitors_made)}
    http = [{**exchange, 'body': own_particulars(without_features(exchange['body']))} for exchange in raw['http']]
    http = [{**exchange, 'body': outline(exchange['body'])} if exchange.get('path') in NOT_KEPT and exchange.get('status') == 200
            else exchange for exchange in http]
    http = [{**exchange, 'body': kept_where(exchange['body'])} if exchange.get('path') == '/upload' else exchange
            for exchange in http]
    http = [{**exchange, 'body': what_was_spent(exchange['body'])} if exchange.get('path') == '/backend-usage'
            else exchange for exchange in http]
    http = [{**exchange, 'body': by_fiat(exchange.get('path'), exchange['body'])} for exchange in http]
    sse = []
    for frame in raw['sse']:
        data = frame.get('data')
        if isinstance(data, dict):
            if data.get('type') in NOT_YET_EVENTS:
                continue
            data = without_features(data)
            if data.get('type') == 'server-version' and 'version' in data:
                data = {**data, 'version': '<version>'}
        sse.append({**frame, 'data': data})
    found: dict = {}
    cursors([exchange['body'] for exchange in http], found)
    held = {conversation: sorted(values - {0}) for conversation, values in found.items()}
    names = agent_rows(http)
    http = [{**exchange, 'body': renamed(ranked(exchange['body'], held), names)} for exchange in http]
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


def made_new(http, found: dict) -> dict:
    """The session name of each janitor a capture made as a new agent, with the name it is given here:
    the letters of the janitor's name and twelve signs its Host drew (`POST /janitors` without a
    session). What a client relies on is that the name the request answered is the janitor's name
    wherever it is named."""
    for exchange in http:
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
    janitors_made = made_new(raw['http'], {})
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


def load_intended(path) -> set:
    if not path or not Path(path).is_file():
        return set()
    return {line.split()[0] for line in Path(path).read_text().splitlines()
            if line.strip() and not line.lstrip().startswith('#')}


# How often a scenario is recorded again when both of its recordings carry the mark of the
# reference's defect R19 (`names_no_row`).
MORE_TRIES = 3


def recorded_again(recordings: list, chosen, again) -> tuple:
    """The recordings, with a third where one could be made, and what the report says of it.

    A scenario whose every recording carries the mark of the reference's defect R19 is recorded
    again (`again(scenarios, attempt)` answers scenario id -> its two views), up to `MORE_TRIES`
    times; the first answer that ran to its end without the mark is kept. The third recording
    is the second with those answers in their places, so that it has the whole corpus."""
    marked = [scenario for scenario in steady(chosen)
              if all(names_no_row(recording[scenario['id']]['core']) for recording in recordings)]
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
            if not views['full']['error'] and not names_no_row(views['core']):
                without[scenario['id']] = views
    lines = ['', "# The reference's defect R19 in every recording of a scenario (a notification names a row the "
             'conversation does not have)']
    for scenario in marked:
        name = scenario['id']
        lines.append(f'{name}: recorded again, and its answer without it (try {tries[name]}) is kept as one more '
                     'recording' if name in without else
                     f'{name}: recorded {tries[name]} more time(s) with it every time: compared as it is')
    if without:
        recordings = [*recordings, {**recordings[-1], **without}]
    return recordings, lines


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
    return ['the rule for a scenario whose every recording carries the mark of defect R19: tried on 5 cases, holds']




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
    errors = run_errors(first, 'python') + run_errors(second, 'python again')
    report = differences(first, second, steady(chosen), view='core')
    lines = [diff.render(report, set(), 'the Python Host against itself', errors)]
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


def against_rust(binary: Path, recordings: list, chosen, intended: set, run: Run, jobs: int) -> tuple:
    """The Rust Host against what the Python Host answered. Where the reference was recorded
    answering in more than one way, the answer the Rust Host is nearest to is the one it is
    held to; the report says which recording that was."""
    seen = corpus(rust_host(binary), chosen, run.label('rust'), jobs)
    errors = run_errors(seen, 'rust')
    report, held_to = diff.Report(), []
    for scenario in steady(chosen):
        against = [differences(recording, seen, [scenario], view='core').items for recording in recordings]
        # A place counts when the Rust Host differs there from every recording. Where it gives
        # what one recording gave and another did not, the reference itself answers in more
        # than one way at that place, and the Rust Host gives one of its answers.
        places = [{place(d) for d in items} for items in against]
        everywhere = set.intersection(*places)
        counted = [[d for d in items if place(d) in everywhere] for items in against]
        nearest = min(range(len(recordings)),
                      key=lambda n: sum(1 for d in counted[n] if d['id'] not in intended))
        report.items.extend(counted[nearest])
        if nearest:
            held_to.append(f"{scenario['id']}: recording {nearest + 1}")
        elsewhere = sorted(set().union(*places) - everywhere)
        if elsewhere:
            held_to.append(f"{scenario['id']}: at {len(elsewhere)} place(s) the recordings differ and the Rust "
                           f"Host gives one of their answers ({'; '.join(' / '.join(p) for p in elsewhere[:6])}"
                           f"{' ...' if len(elsewhere) > 6 else ''})")
    unexplained = [d for d in report.items if d['id'] not in intended]
    lines = [diff.render(report, intended, 'the Python Host (base) against the Rust Host (head)', errors)]
    if held_to:
        lines += ['', 'held to more than the first recording:', *(f'  {line}' for line in held_to)]
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
    parser.add_argument('mode', choices=('record', 'plants', 'compare', 'cross', 'rust', 'selftest'))
    parser.add_argument('--build', type=Path, help='a checkout of the repository: the Python Host under ./server')
    parser.add_argument('--rust', type=Path, help='the clarp-host binary (clarp-shim beside it)')
    parser.add_argument('--golden', type=Path, default=HERE / 'golden')
    parser.add_argument('--out', type=Path, help='cross: where the report and the written-down answers go; '
                        'compare: where what the Rust Host answered goes, when it is wanted')
    parser.add_argument('--intended', default=str(HERE / 'intended.txt'))
    parser.add_argument('--scenarios', help='a comma-separated subset of the corpus')
    parser.add_argument('--jobs', type=int, default=4)
    parser.add_argument('--keep', action='store_true', help='keep the scratch directory')
    args = parser.parse_args(argv)
    chosen = choose(args.scenarios)
    for needed, modes in (('build', ('record', 'plants', 'cross')), ('rust', ('compare', 'cross', 'rust')),
                          ('out', ('cross', 'rust'))):
        if args.mode in modes and getattr(args, needed) is None:
            parser.error(f'{args.mode} needs --{needed}')
    build = args.build.resolve() if args.build else None
    rust = args.rust.resolve() if args.rust else None
    if args.mode == 'selftest':
        print('\n'.join(selftest()))
        return 0
    run = Run()
    ok, lines = True, []
    try:
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
            lines, ok = prove(build, load_golden(args.golden, chosen), chosen, plants.CHANGES, run, args.jobs)
        elif args.mode == 'compare':
            recordings = [load_golden(directory, chosen) for directory in golden_dirs(args.golden)
                          if directory == args.golden or directory.is_dir()]
            lines, ok, rust_seen = against_rust(rust, recordings, chosen,
                                                load_intended(args.intended), run, args.jobs)
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
            else:
                proof, proved = prove(build, recordings[0], chosen, plants.CHANGES, run, args.jobs)
            rust_lines, conforms, rust_seen = against_rust(
                rust, recordings, chosen, load_intended(args.intended), run, args.jobs)
            write_golden(args.out / 'rust', rust_seen)
            lines += ['', *proof, '', *rust_lines]
            ok = ok and proved and conforms
            (args.out / 'report.txt').write_text('\n'.join(lines) + '\n')
    finally:
        run.close(args.keep or not ok)
    print('\n'.join(lines))
    return 0 if ok else 1


if __name__ == '__main__':
    with contextlib.suppress(AttributeError):
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    sys.exit(main())
