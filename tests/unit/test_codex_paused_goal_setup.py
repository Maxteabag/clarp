"""Paused recovery fixtures must not leave a sleeper that revives on resume."""
import json
from pathlib import Path
import sys
import time

import pytest

from lib.backend.registry import by_id
from tests.unit.test_codex_generations import fake, _leftover, _goal_turns, _wait
from lib import codex_app_server as cas


# Disposable provider wrapper only: force the real old active/pause/resume
# setup across its pending-start window without adding a fixture timing flag.
FENCE = r'''
import json, os, pathlib, runpy, sys, threading, time
home = pathlib.Path(os.environ['CODEX_HOME'])
log_lock = threading.Lock()
original_start = threading.Thread.start

def record(event, thread):
    with log_lock:
        with (home / 'setup-workers.jsonl').open('a') as stream:
            stream.write(json.dumps({'event':event,'thread':thread}) + '\n')

def start(worker):
    if getattr(getattr(worker, '_target', None), '__name__', '') == 'goal_turn':
        thread = worker._args[0]
        record('scheduled', thread)
        original_run = worker.run
        def run():
            try:
                end = time.monotonic() + 6
                while not (home / 'release-setup-workers').exists():
                    if time.monotonic() > end:
                        raise TimeoutError('owned setup-worker fence unreleased')
                    time.sleep(.005)
                original_run()
            finally:
                record('finished', thread)
        worker.run = run
    return original_start(worker)
threading.Thread.start = start
runpy.run_path(SOURCE, run_name='__main__')
'''


def test_a_paused_leftover_resumes_exactly_one_native_continuation(fake, tmp_path, monkeypatch):
    provider = Path(by_id('codex').required_binary)
    wrapper = tmp_path / 'fenced-codex'
    wrapper.write_text('#!' + sys.executable + '\nSOURCE = ' + repr(str(provider)) + '\n' + FENCE)
    wrapper.chmod(0o700)
    monkeypatch.setattr(by_id('codex'), 'required_binary', str(wrapper))
    # Both old sleepers must reach admission before either can complete.
    monkeypatch.setenv('CLARP_QA_GOAL_TURN_HOLD', '0.2')
    journal = fake / 'setup-workers.jsonl'

    def records():
        return [json.loads(line) for line in journal.read_text().splitlines()] if journal.exists() else []

    try:
        agent, thread, paused = _leftover(fake, tmp_path, monkeypatch, 'paused-setup')
        assert paused['status'] == 'paused' and paused['objective'] == 'keep going'
        initial_workers = [r for r in records() if r['event'] == 'scheduled']
        assert all(r['thread'] == thread for r in initial_workers)
        assert _goal_turns(fake, thread) == []
        cas.goal(agent, 'resume')
        # The next actual RPC fences the preceding main-loop scheduling code.
        cas.goal(agent, 'get')
        scheduled = [r for r in records() if r['event'] == 'scheduled']
        assert scheduled and all(r['thread'] == thread for r in scheduled)
        (fake / 'release-setup-workers').touch()
        assert _wait(lambda: len([r for r in records() if r['event'] == 'finished']) == len(scheduled))
        native_count = len(_goal_turns(fake, thread))
        assert native_count == 1, ('native continuation count', native_count, 'pending during paused setup', len(initial_workers))
        assert initial_workers == [], 'paused setup itself scheduled active goal work'
        assert len(scheduled) == 1
        assert cas.goal(agent, 'get')['status'] == 'complete'
    finally:
        (fake / 'release-setup-workers').touch()


# Hold a real native execution after its durable start record, before its
# completion. This is independent of the blocked Host adoption callback.
NATIVE_GATE = r'''
import json, os, pathlib, runpy, sys, threading, time
home = pathlib.Path(os.environ['CODEX_HOME'])
def profile(frame, event, result):
    if (event == 'return' and frame.f_code.co_name == 'record_auth'
            and frame.f_locals.get('kind') == 'goal_turn'):
        caller = frame.f_back.f_locals
        state = {'thread':caller['thread_id'],'turn':caller['turn_id'],
                 'pid':os.getpid(),'phase':'running'}
        path = home / 'native-control.json'
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(state))
        os.replace(temporary, path)
        end = time.monotonic() + 20
        while not (home / 'release-native-control').exists():
            if time.monotonic() > end:
                raise TimeoutError('owned native execution gate unreleased')
            time.sleep(.005)
        state['phase'] = 'released'
        temporary.write_text(json.dumps(state))
        os.replace(temporary, path)
threading.setprofile(profile)
runpy.run_path(SOURCE, run_name='__main__')
'''


@pytest.fixture
def native_goal_gate(fake, tmp_path, monkeypatch):
    provider = Path(by_id('codex').required_binary)
    wrapper = tmp_path / 'native-gated-codex'
    wrapper.write_text('#!' + sys.executable + '\nSOURCE = ' + repr(str(provider)) + '\n' + NATIVE_GATE)
    wrapper.chmod(0o700)
    monkeypatch.setattr(by_id('codex'), 'required_binary', str(wrapper))
    # Covers existing delivered5s + count8s + action5s, with cleanup margin.
    # It bounds only our observation gate; no product deadline is changed.
    deadline = time.monotonic() + 20

    held_clients = []

    class Gate:
        def remaining(self):
            return max(0, deadline - time.monotonic())

        def assert_live(self, client, thread, turn):
            path = fake / 'native-control.json'
            assert _wait(path.exists), 'native execution did not enter its gate'
            state = json.loads(path.read_text())
            assert state == {'thread':thread,'turn':turn,'pid':client.proc.pid,'phase':'running'}
            assert client.proc.poll() is None and self.remaining() > 0
            held_clients.append(client)
            (fake / 'native-controls-verified').write_text(json.dumps(state))
            print('native-control: actual running identity verified before shutdown', file=sys.stderr)

    try:
        yield Gate()
    finally:
        (fake / 'release-native-control').touch()
        for client in held_clients:
            if client.proc.poll() is None:
                assert _wait(lambda: json.loads((fake / 'native-control.json').read_text())['phase'] == 'released'
                             or client.proc.poll() is not None, timeout=5), 'native execution gate did not release'
