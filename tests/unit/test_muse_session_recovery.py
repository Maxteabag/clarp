"""Recover only our abandoned headless Muse turn, retaining its session."""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'server'))
from lib.backend.muse import MuseBackend
from lib import agents, provider_background_jobs, turn_lifecycle, error_classify

SID = '06a488a5-eb75-4aff-9c34-7ef0b3c89246'
AGENT = 'muse-recovery-agent'
SESSION = 'muse-recovery-session'
pytestmark = pytest.mark.skipif(sys.platform != 'linux', reason='Linux orphan identity and flock')


@pytest.fixture
def recovery(tmp_path, monkeypatch, fake_backend_cli):
    fake_backend_cli.dir(tmp_path)
    home = tmp_path / 'muse-home'
    directory = home / 'sessions/2026/11/05' / SID
    directory.mkdir(parents=True)
    (directory / 'session.jsonl').touch()
    lock = directory / '.session.lock'
    monkeypatch.setenv('CLAUDE_PWA_MUSE_HOME', str(home))
    monkeypatch.setenv('MUSE_RECOVERY_LOCK', str(lock))
    attempts = tmp_path / 'attempts.jsonl'
    monkeypatch.setenv('MUSE_RECOVERY_ATTEMPTS', str(attempts))
    binary = tmp_path / 'muse'
    binary.write_text('''#!/usr/bin/env python3
import fcntl, json, os, signal, sys, socket
lock = os.environ['MUSE_RECOVERY_LOCK']
fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
if sys.argv[-1] == 'no-lock':
    print('ready', flush=True)
    while True: signal.pause()
if sys.argv[-1] in ('hold', 'ignore-term'):
    if sys.argv[-1] == 'ignore-term':
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    fcntl.flock(fd, fcntl.LOCK_EX)
    os.ftruncate(fd, 0)
    os.write(fd, ('pid=' + str(os.getpid()) + '\\nhost=' + socket.gethostname() + '\\n').encode())
    print('ready', flush=True)
    while True: signal.pause()
with open(os.environ['MUSE_RECOVERY_ATTEMPTS'], 'a') as out:
    out.write(json.dumps(sys.argv[1:]) + '\\n')
sid = sys.argv[sys.argv.index('--session-id') + 1]
try:
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    print('session ' + sid + ' is already in use', file=sys.stderr)
    sys.exit(1)
if os.environ.get('MUSE_REFUSE_RETRY'):
    print('session ' + sid + ' is already in use', file=sys.stderr)
    sys.exit(1)
print(json.dumps({'payload_type':'run.output.delta','payload':{'text':'recovered'}}))
print(json.dumps({'payload_type':'run.terminal.completed','payload':{'terminal':'completed'}}))
''')
    binary.chmod(0o700)
    monkeypatch.setenv('PATH', str(tmp_path) + os.pathsep + os.environ['PATH'])
    monkeypatch.setattr(agents, 'get_by_agent_id', lambda _: {'persona': ''})
    monkeypatch.setattr(agents, 'upsert_live_assistant_message', lambda **_: {'changed': True})
    monkeypatch.setattr(agents, 'latest_turn_synthesize_audio', lambda _: False)
    monkeypatch.setattr(turn_lifecycle, 'transition', lambda *_, **__: None)
    backend = MuseBackend()
    holders = []

    def hold(*, ignore=False, foreign=False, active=False, unverified=False, command="exec", wrong_sid=False, no_lock=False):
        token, identity = provider_background_jobs.identity_env(AGENT)
        cmd = backend.build_cmd('06a488a5-eb75-4aff-9c34-7ef0b3c89247' if wrong_sid else SID)
        cmd[1] = command
        cmd += ['--', 'no-lock' if no_lock else 'ignore-term' if ignore else 'hold']
        proc, handle = backend.launch(cmd, cwd=tmp_path,
                                      session='foreign-session' if foreign else SESSION,
                                      env_extra=identity)
        holders.append(handle)
        assert proc.stdout.readline().strip() == 'ready'
        if not unverified:
            provider_background_jobs.record_identity(token, agent_id=AGENT, provider='muse', pid=proc.pid)
        if active:
            backend.register_handle(AGENT, handle)
        return handle

    yield backend, hold, attempts
    for handle in holders:
        backend.unregister_handle(AGENT, handle)
        if handle.proc.poll() is None:
            os.killpg(handle.pid, signal.SIGKILL)
        handle.proc.wait(timeout=5)


def run_turn(backend, tmp_path):
    errors, results, bound = [], [], []
    handle = backend.start_turn(text='continue precisely', cwd=tmp_path,
                                backend_session_id=SID, agent_id=AGENT, session=SESSION,
                                on_session_init=lambda sid: bound.append(sid) or True,
                                on_error=errors.append, on_result=results.append,
                                enqueue=lambda **_: 1)
    rc = handle.wait(timeout=15)
    assert not handle.drain_thread.is_alive()
    return rc, errors, results, bound


@pytest.mark.parametrize('ignore_term', [False, True])
def test_owned_orphan_reclaimed_and_same_turn_retried_once(recovery, tmp_path, ignore_term):
    backend, hold, attempts = recovery
    holder = hold(ignore=ignore_term)
    rc, errors, results, bound = run_turn(backend, tmp_path)
    assert errors == []
    assert rc == 0 and [r['last_agent_message'] for r in results] == ['recovered']
    assert not Path(f'/proc/{holder.pid}').exists(), 'owned child must be reaped'
    calls = [json.loads(line) for line in attempts.read_text().splitlines()]
    assert len(calls) == 2 and calls[0] == calls[1]
    assert calls[0][calls[0].index('--session-id') + 1] == SID
    assert bound and set(bound) == {SID}


@pytest.mark.parametrize('protected', ['foreign', 'live-handle', 'other-turn-handle', 'unverifiable'])
def test_other_holder_never_killed_and_reports_clear_error(recovery, tmp_path, protected):
    backend, hold, attempts = recovery
    holder = hold(foreign=protected == 'foreign', active=protected == 'live-handle',
                  unverified=protected == 'unverifiable')
    if protected == 'other-turn-handle':
        backend.register_handle('another-agent', holder)
    try:
        rc, errors, results, bound = run_turn(backend, tmp_path)
    finally:
        backend.unregister_handle('another-agent', holder)
    assert holder.is_alive() and not results
    assert rc != 0 and len(errors) == 1
    assert error_classify.classify_error(errors[0]) == 'muse_session_locked'
    assert str(holder.pid) in errors[0] and 'close' in errors[0].lower()
    assert len(attempts.read_text().splitlines()) == 1
    assert set(bound) == {SID}


def test_reclaim_does_not_loop_or_replace_session_on_second_refusal(recovery, tmp_path, monkeypatch):
    backend, hold, attempts = recovery
    hold()
    monkeypatch.setenv('MUSE_REFUSE_RETRY', '1')
    rc, errors, results, bound = run_turn(backend, tmp_path)
    assert rc != 0 and len(errors) == 1 and not results
    calls = [json.loads(line) for line in attempts.read_text().splitlines()]
    assert len(calls) == 2 and calls[0] == calls[1]
    assert set(bound) == {SID}


@pytest.mark.parametrize('evidence', ['wrong-command', 'wrong-session', 'wrong-host', 'wrong-start', 'false-pid'])
def test_partial_ownership_evidence_never_authorizes_a_signal(recovery, tmp_path, evidence):
    from lib import db
    import socket
    backend, hold, attempts = recovery
    holder = hold(command='resume' if evidence == 'wrong-command' else 'exec',
                  wrong_sid=evidence == 'wrong-session')
    lock = Path(os.environ['MUSE_RECOVERY_LOCK'])
    protected = holder
    if evidence == 'wrong-host':
        lock.write_text(f'pid={holder.pid}\nhost=another-host\n')
    elif evidence == 'wrong-start':
        db.conn().execute("UPDATE provider_turns SET start_token='not-this-process' WHERE pid=?", (holder.pid,))
    elif evidence == 'false-pid':
        protected = hold(no_lock=True)
        lock.write_text(f'pid={protected.pid}\nhost={socket.gethostname()}\n')
    rc, errors, results, _ = run_turn(backend, tmp_path)
    assert holder.is_alive() and protected.is_alive()
    assert rc != 0 and not results and len(errors) == 1
    assert error_classify.classify_error(errors[0]) == 'muse_session_locked'
    assert len(attempts.read_text().splitlines()) == 1


def test_stop_during_reclaim_prevents_the_followup_spawn(recovery, tmp_path, monkeypatch):
    import threading
    backend, hold, attempts = recovery
    holder = hold(ignore=True)
    entered, release = threading.Event(), threading.Event()
    send = os.killpg

    def held(group, sig):
        send(group, sig)
        if group == holder.pid and sig == signal.SIGTERM:
            entered.set()
            assert release.wait(5)

    monkeypatch.setattr(os, 'killpg', held)
    results, errors = [], []
    handle = backend.start_turn(text='continue precisely', cwd=tmp_path,
                                backend_session_id=SID, agent_id=AGENT, session=SESSION,
                                on_session_init=lambda _: True,
                                on_error=errors.append, on_result=results.append,
                                enqueue=lambda **_: 1)
    try:
        assert entered.wait(5)
        handle.terminate()
    finally:
        release.set()
        handle.wait(timeout=15)
    assert not results
    assert len(attempts.read_text().splitlines()) == 1


def test_retry_publication_protects_the_live_replacement(recovery, tmp_path, monkeypatch):
    """A real competing reclaim sees ownership across the retry hand-off."""
    import threading
    from lib.backend import muse
    backend, hold, attempts = recovery
    hold()
    binary = tmp_path / 'muse'
    before = "print(json.dumps({'payload_type':'run.output.delta'"
    replacement = ("os.ftruncate(fd, 0)\n"
                   "os.write(fd, ('pid=' + str(os.getpid()) + '\\nhost=' + socket.gethostname() + '\\n').encode())\n"
                   "print('replacement-ready', flush=True)\n"
                   "while True: signal.pause()\n" + before)
    binary.write_text(binary.read_text().replace(before, replacement))
    publication, release, attempted, completed = (threading.Event() for _ in range(4))
    launch = backend.launch
    spawned, refusals, lock_was_free, failures = [], [], [], []
    real_lock = muse._RECOVERY_LOCK
    competitor = None

    class ObservedLock:
        def __enter__(self):
            if threading.current_thread() is competitor:
                acquired = real_lock.acquire(blocking=False)
                lock_was_free.append(acquired)
                attempted.set()
                if not acquired:
                    real_lock.acquire()
            else:
                real_lock.acquire()
            return self

        def __exit__(self, *_):
            real_lock.release()

    class PublicationBoundary:
        def __init__(self, proc, actual):
            self.proc, self.actual = proc, actual

        @property
        def process_group(self):
            assert self.proc.stdout.readline().strip() == 'replacement-ready'
            publication.set()
            assert release.wait(10)
            return self.actual.process_group

    def capture(*args, **kwargs):
        proc, handle = launch(*args, **kwargs)
        spawned.append(proc)
        if len(spawned) == 2:
            return proc, PublicationBoundary(proc, handle)
        return proc, handle

    def compete():
        try:
            refusals.append(backend._reclaim_session(SID, agent_id=AGENT, session=SESSION))
        except BaseException as error:
            failures.append(error)
        finally:
            completed.set()

    monkeypatch.setattr(muse, '_RECOVERY_LOCK', ObservedLock())
    monkeypatch.setattr(backend, 'launch', capture)
    handle = None
    try:
        handle = backend.start_turn(text='continue precisely', cwd=tmp_path,
                                    backend_session_id=SID, agent_id=AGENT, session=SESSION,
                                    on_session_init=lambda _: True, on_error=lambda _: None,
                                    enqueue=lambda **_: 1)
        assert publication.wait(5), 'retry did not reach live publication'
        live = spawned[1]
        assert live.poll() is None
        competitor = threading.Thread(target=compete)
        competitor.start()
        assert attempted.wait(5), 'competitor never attempted the real recovery lock'
        # Observe actual acquisition rather than sleeping and hoping the
        # competing thread ran. If the old gap let it enter, complete its real
        # reclaim before releasing publication; otherwise unblock publication.
        if lock_was_free[0]:
            assert completed.wait(5)
        release.set()
        competitor.join(timeout=5)
        assert not competitor.is_alive() and not failures
        assert len(refusals) == 1 and refusals[0], 'live replacement was reclaimed'
        assert live.poll() is None, 'live replacement must not be signaled'
    finally:
        release.set()
        if competitor is not None:
            competitor.join(timeout=5)
        for proc in spawned:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=5)
        if handle is not None:
            handle.drain_thread.join(timeout=5)
