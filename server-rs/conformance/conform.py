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


class RustHost(replay.Host):
    """The Rust Host behind the same fake Claude."""

    def start(self, config=None):
        (self.root / replay.OWNED).touch()
        provider = self.root / 'provider'
        for path in (provider / 'bin', self.root / 'workspace', self.root / 'home', self.root / 'tmp'):
            path.mkdir(parents=True)
        claude_shim(provider / 'bin' / 'claude')
        self.log = (self.root.parent / f'{self.root.name}.host.log').open('a')
        # The reference Host has its agents before it serves its first request. This one is given
        # the same agents and then started again, so that making them is not part of what a client
        # sees of the scenario. The scenarios are about the first; the other two are the reference
        # Host's codex agents, which no scenario runs: they are there to be other agents.
        self._launch()
        for session, backend in ((scenarios.AGENT, 'claude'), *((other, 'codex') for other in OTHER_AGENTS)):
            status, _, made = self.request('POST', '/agents', {
                'name': session.title(), 'backend': backend, 'cwd': str(self.root / 'workspace')})
            if status != 200 or (made or {}).get('session') != session:
                raise replay.StepFailed(f'the Rust Host did not make the agent {session} ({status})')
        self._end()
        self._launch()
        return self

    def _launch(self):
        provider = self.root / 'provider'
        env = replay.child_env()
        env.update({'HOME': str(self.root / 'home'), 'TMPDIR': str(self.root / 'tmp'),
                    'CLARP_HOST_TOKEN': replay.TOKEN, 'CLARP_CLAUDE_PROGRAM': str(provider / 'bin' / 'claude'),
                    'CLARP_QA_PROVIDER_ROOT': str(provider)})
        self.process = subprocess.Popen(
            [str(self.build), '--data', str(self.root / 'data'), '--run', str(self.root / 'run'),
             '--cwd', str(self.root / 'home'), '--listen', '127.0.0.1:0'],
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
    session = step.get('session', scenarios.AGENT)
    if op in HTTP_OPS:
        last = http[-1]['body'] if http else None
        path_values = dict(variables)
        # {name} in the path, and fields of the body, taken from the answer before: an id the
        # Host made, the time an artifact last changed.
        for name, dotted in (step.get('from_last') or {}).items():
            path_values[name] = urllib.parse.quote(str(dig(last, dotted, name)), safe='')
        path = replay.fill(step['path'], path_values)
        body = replay.fill(step.get('body'), variables)
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
        host.start()
        events = Events(host)
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
    'portraits and the persona catalogue': {
        'fields': ('avatar_url', 'model_avatar_url', 'personas'),
        'events': (),
    },
}
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
                        'helper_completed_at'})
# The ids a Host gives to what agents make and ask. Under their own keys the harness's
# normalizer names them by the order they appear in (`artifact-<id:1>`). They are also in the
# paths a client calls (`/decisions/<id>/resolve`) and in what an agent is told and says back
# (`Decision ID: ...`), and there the normalizer leaves them: hex in free text is content to it.
ISSUED = re.compile(r'^(?:artifact|decision)-[0-9a-f]{12,64}$')
# An id a preview cut short: `Decision ID: decision-7be0a016ae…`.
CUT_ID = re.compile(r'((?:artifact|decision)-[0-9a-f]{4,})…$')
# The id a Host gives a file an agent published: `asset_` and twenty-four signs of an alphabet
# that has an underscore in it. The harness's normalizer names such an id under its own key only
# when it happens to have no underscore, which about half of them do; and the id is also in what
# a client is told to fetch (`url`, `uri`), in what an agent writes into a chat (`markdown`) and
# in the path a client then asks for. So these are named here, before the normalizer reads the
# capture, in the order a client first learns of them.
ASSET = re.compile(r'asset_[A-Za-z0-9_]{24}')


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
            item = '<time>'  # a reading no TurnClock has told: there is none in a capture of this suite
        elif key in CLOCK_KEYS and isinstance(item, int) and not isinstance(item, bool) and item in EPOCH_MS:
            item = '<time>'
        elif key == 'agents' and isinstance(item, list):
            # Both Hosts have two codex agents (rachel, mike) that no scenario is about. Both
            # sides are cut to the scenario's own.
            item = [row for row in item if not isinstance(row, dict) or row.get('session') in sessions]
        elif key == 'roster' and isinstance(item, list):
            item = [name for name in item if isinstance(name, str) and name.lower() in sessions]
        out[key] = settle(item, sessions)
    if 'server_id' in out and 'capabilities' in out and out.get('name'):
        out['name'] = '<name>'  # `GET /server-info`: a Host is called what its machine is called
    return out


def without_features(value):
    """A body or an event without the fields of the features the Rust Host does not have yet."""
    if isinstance(value, list):
        return [without_features(item) for item in value]
    if isinstance(value, dict):
        return {key: without_features(item) for key, item in value.items() if key not in NOT_YET_FIELDS}
    return value


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
    """The ids of the rows an agent wrote, named in the order a client first reads them. An id
    is the Host's own choice; what a client relies on is that the same row has the same id
    wherever it is named."""
    names: dict = {}
    for exchange in http:
        body = exchange['body']
        for turn in (body.get('turns') or ()) if isinstance(body, dict) else ():
            if isinstance(turn, dict) and turn.get('role') == 'assistant' and isinstance(turn.get('id'), str):
                names.setdefault(turn['id'], f'<row:{len(names) + 1}>')
    return names


def renamed(value, names: dict):
    if isinstance(value, list):
        return [renamed(item, names) for item in value]
    if isinstance(value, dict):
        return {key: renamed(item, names) for key, item in value.items()}
    if isinstance(value, str):
        return names.get(value, value)
    return value


def core(raw: dict) -> dict:
    """A raw capture as it is compared across Hosts: without what belongs to features the Rust
    Host does not have yet, with revisions as their order, with the ids of the agent's rows as
    the order they are first read in, and with each Host's own version as just "a version"."""
    http = [{**exchange, 'body': own_particulars(without_features(exchange['body']))} for exchange in raw['http']]
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
                      if key == 'archived_parents' and isinstance(items, list) else by_session(items, sessions))
                for key, items in value.items()}
    return value


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


def seen_by_a_client(raw: dict) -> dict:
    """A capture, normalized, and cut down to the channels a client sees."""
    assets = published([exchange['body'] for exchange in raw['http']], {})
    if assets:
        raw = {**raw, 'http': by_name(raw['http'], assets), 'sse': by_name(raw['sse'], assets)}
    # The agents a scenario is about: its own, unless it names more.
    sessions = tuple(scenarios.BY_ID.get(raw.get('scenario'), {}).get('sessions', (scenarios.AGENT,)))
    known = agents_by_id([exchange['body'] for exchange in raw['http']], {})
    raw = {**raw, 'http': [{**exchange, 'body': by_session(settle(exchange['body'], sessions), known)}
                           for exchange in raw['http']]}
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
    """Where the two recordings of the reference are written down: the first, and beside it
    the second."""
    return [first, first.with_name(first.name + '-again')]


def load_intended(path) -> set:
    if not path or not Path(path).is_file():
        return set()
    return {line.split()[0] for line in Path(path).read_text().splitlines()
            if line.strip() and not line.lstrip().startswith('#')}


def record(build: Path, chosen, run: Run, jobs: int) -> tuple:
    """The Python Host's answers, twice. Returns (both recordings, lines of the report, ok).

    Where the two recordings differ, the reference itself answers in more than one way. That
    is a defect of the reference (they are listed in docs/reference-defects.md), and it does
    not stop the run: both answers are kept, and the Rust Host conforms in such a scenario
    when it gives one of them."""
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
    return [first, second], lines, not errors


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
        report = differences(golden, seen, steady(chosen), view='core')
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
    parser.add_argument('mode', choices=('record', 'plants', 'compare', 'cross', 'rust'))
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
    run = Run()
    ok, lines = True, []
    try:
        if args.mode == 'record':
            recordings, lines, ok = record(build, chosen, run, args.jobs)
            if ok:
                for directory, seen in zip(golden_dirs(args.golden), recordings):
                    write_golden(directory, seen)
                lines.append(f'written down: {len(recordings[0])} scenario(s), twice, in {args.golden}')
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
