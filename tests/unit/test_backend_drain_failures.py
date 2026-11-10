"""BACKENDS-03 and the quick part of BACKENDS-04.

A CLI that writes bytes that are not UTF-8 must not stop its pipes from being
read, and a Claude or Codex drain that fails reading must still end its turn:
one terminal outcome and no process left running on the session."""
from __future__ import annotations

import subprocess
import sys
from unittest.mock import Mock

from lib.backend.stream_json import launch
from lib.process_registry import TurnHandle


def _run(tmp_path, code):
    proc, handle = launch([sys.executable, "-c", code], cwd=tmp_path, session="synthetic")
    try:
        try:
            proc.wait(timeout=5)
            exited = True
        except subprocess.TimeoutExpired:
            exited = False
        out = proc.stdout.read() if exited else ""
    finally:
        if proc.poll() is None:
            handle.kill()
        proc.wait(timeout=5)
    err = proc._stderr_drain.text(timeout=2)
    proc.stdout.close()
    proc.stderr.close()
    return exited, out, err


def test_invalid_stderr_bytes_do_not_stop_the_stderr_drain(tmp_path):
    # Past the 64 KiB pipe: a dead drain would leave the child blocked.
    exited, _out, err = _run(
        tmp_path, "import os; os.write(2, b'\\xff\\xfe'); os.write(2, b'x' * 200000)")
    assert exited, "the child blocked on a stderr pipe nobody reads"
    assert err.endswith("x" * 1000)


def test_invalid_stdout_bytes_are_read_not_raised(tmp_path):
    exited, out, _err = _run(
        tmp_path, "import os; os.write(1, b'\\xff noise\\n{\"type\": \"ok\"}\\n')")
    assert exited
    assert out.splitlines()[-1] == '{"type": "ok"}'


class _FailingStdout:
    def __iter__(self):
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "synthetic")


def _proc():
    proc = Mock(stdout=_FailingStdout())
    proc.poll.return_value = None
    proc.wait.return_value = 0
    return proc


def test_a_codex_drain_that_fails_reading_ends_its_turn():
    from lib.backend.codex import CodexBackend
    proc = _proc()
    handle = TurnHandle(proc, None)
    backend = CodexBackend()
    backend.register_handle("synthetic", handle)
    result, error = Mock(), Mock()
    backend._drain_stdout(
        proc=proc, agent_id="synthetic", session="synthetic", trace_id="synthetic-trace",
        backend_session_id="synthetic-thread", handle=handle, on_session_init=None,
        on_result=result, on_error=error, stream=None, enqueue=lambda **kw: 0)
    assert result.call_count + error.call_count == 1
    assert proc.terminate.called, "the CLI was left running on the session"


def test_a_claude_drain_that_fails_reading_ends_its_turn():
    from lib.backend.claude import ClaudeBackend
    proc = _proc()
    handle = TurnHandle(proc, None)
    backend = ClaudeBackend()
    backend.register_handle("synthetic", handle)
    result, error = Mock(), Mock()
    backend._drain_stdout(proc, None, result, error, "synthetic-trace",
                          agent_id="synthetic", handle=handle, session="synthetic")
    assert result.call_count + error.call_count == 1
    assert proc.terminate.called, "the CLI was left running on the session"


import pytest


@pytest.mark.parametrize("backend_name", ["grok", "muse", "opencode"])
def test_the_other_runners_end_their_turn_and_stop_the_cli_when_reading_fails(backend_name):
    """BACKENDS-04, the remaining runners: each reported an error but left the
    CLI running on the session after unregistering its handle."""
    import importlib
    module = importlib.import_module(f"lib.backend.{backend_name}")
    backend_cls = next(value for name, value in vars(module).items()
                       if name.endswith("Backend") and isinstance(value, type)
                       and value.__module__ == module.__name__)
    backend = backend_cls()
    proc = _proc()
    handle = TurnHandle(proc, None)
    backend.register_handle("synthetic", handle)
    result, error = Mock(), Mock()
    kwargs = dict(proc=proc, agent_id="synthetic", session="synthetic", trace_id="synthetic-trace",
                  handle=handle, backend_session_id="", on_session_init=None,
                  on_result=result, on_error=error, stream=None, enqueue=lambda **kw: 0)
    if backend_name != "opencode":
        kwargs["minted"] = False
    backend._drain_stdout(**kwargs)
    assert result.call_count + error.call_count == 1
    assert proc.terminate.called, "the CLI was left running on the session"


# 937: only NEW fault evidence is code/class-only. Legacy primary diagnostics
# retain their original content, and all process operations below are fakes.
_CLEANUP_SECRET = 'SYNTHETIC_CLEANUP_SECRET_937'
_FAULT_CODES = {'claudeExitWaitFailed', 'grokErrorCallbackFailed',
                'museErrorCallbackFailed', 'agyForceKillFailed'}


class CleanupFault(RuntimeError):
    def __str__(self):
        raise AssertionError('secondary cleanup message must not be formatted')

    def __repr__(self):
        raise AssertionError('secondary cleanup repr must not be formatted')


class _ScriptedStdout:
    def __init__(self, rows=(), *, fail=True, after_rows=None):
        self.rows, self.fail, self.after_rows = rows, fail, after_rows

    def __iter__(self):
        yield from self.rows
        if self.after_rows is not None:
            self.after_rows()
        if self.fail:
            raise RuntimeError('synthetic read failure')


class _CleanupProc:
    def __init__(self, timeline, *, stdout=None, returncode=0, exited=False):
        import threading
        from types import SimpleNamespace
        self.timeline, self.stdout, self.returncode = timeline, stdout, returncode
        self.pid, self.args, self.running = 42, ['synthetic'], not exited
        self.wait_fault = self.terminate_fault = self.kill_fault = False
        self.wait_lock = threading.RLock()
        self._stderr_drain = SimpleNamespace(text=lambda **kw: 'synthetic exit failure')

    def poll(self):
        self.timeline.append('poll')
        return None if self.running else self.returncode

    def terminate(self):
        self.timeline.append('terminate')
        if self.terminate_fault:
            raise OSError('synthetic terminate failure')

    def wait(self, timeout=None):
        with self.wait_lock:
            self.timeline.append(('wait', timeout))
            if self.wait_fault:
                raise CleanupFault(_CLEANUP_SECRET)
            self.running = False
            return self.returncode

    def kill(self):
        self.timeline.append('kill')
        if self.kill_fault:
            raise CleanupFault(_CLEANUP_SECRET)
        self.running = False


@pytest.fixture
def cleanup_faults(monkeypatch):
    import io
    import threading
    from types import SimpleNamespace
    from lib import eventlog, log as logger
    timeline, rows, locks, checks, clock, raw = [], [], [], [], [100.0], []
    stderr = io.StringIO()
    def record(*args, **kwargs):
        code = args[1]
        if code in _FAULT_CODES:
            available = []
            def inspect():
                for lock in locks:
                    acquired = lock.acquire(blocking=False)
                    available.append(acquired)
                    if acquired:
                        lock.release()
            worker = threading.Thread(target=inspect)
            worker.start()
            worker.join(timeout=2)
            checks.append((not worker.is_alive(), tuple(available)))
            timeline.append('fault')
            rows.append((args, kwargs))
        elif code.endswith('DrainFail'):
            timeline.append('primary')
    monkeypatch.setattr(logger.sys, 'stderr', stderr)
    monkeypatch.setattr(logger, 'time', SimpleNamespace(monotonic=lambda: clock[0], strftime=logger.time.strftime))
    monkeypatch.setattr(eventlog, 'emit', record)
    monkeypatch.setattr(logger.os, 'write', lambda fd, data: raw.append((fd, data)) or len(data))
    logger._FAULT_LOG_THROTTLE.clear()
    logger._FAULT_LOG_COUNTS.clear()
    yield SimpleNamespace(timeline=timeline, rows=rows, locks=locks, checks=checks,
                          clock=clock, raw=raw, stderr=stderr, logger=logger)
    logger._FAULT_LOG_THROTTLE.clear()
    logger._FAULT_LOG_COUNTS.clear()


def _private_backend(module, name, evidence, monkeypatch):
    from lib.process_registry import ProcessRegistry
    backend = getattr(module, name)()
    # Isolate this owner registry; use its real locking/register/unregister.
    backend._registry = ProcessRegistry(log_exception=module.log_exception)
    original = backend.unregister_handle
    def unregister(agent_id, handle):
        evidence.timeline.append('unregister')
        original(agent_id, handle)
    monkeypatch.setattr(backend, 'unregister_handle', unregister)
    return backend


def _secondary_runner(name, evidence, monkeypatch, *, callback_mode='fault', native_error=False, reported=False):
    import importlib
    import json
    module = importlib.import_module('lib.backend.' + name)
    backend = _private_backend(module, name.title() + 'Backend', evidence, monkeypatch)
    events = []
    if native_error:
        event = ({'type': 'error', 'message': 'native failure'} if name == 'grok' else
                 {'payload_type': 'run.terminal.failed', 'record_type': 'event',
                  'payload': {'kind': 'run_terminal', 'terminal': 'failed', 'reason': 'native failure'}})
        events.append(json.dumps(event))
    proc = _CleanupProc(evidence.timeline, stdout=_ScriptedStdout(events, fail=not reported),
                        returncode=1 if reported else 0)
    handle = TurnHandle(proc, None)
    if name == 'muse':
        handle = module._MuseHandle(handle)
        cancel = backend.cancel_live_text
        def observed_cancel(st):
            evidence.timeline.append('cancel')
            cancel(st)
        monkeypatch.setattr(backend, 'cancel_live_text', observed_cancel)
    backend.register_handle('fixture-agent', handle)
    evidence.locks[:] = [backend._registry._lock, proc.wait_lock]
    if name == 'muse':
        evidence.locks.extend([module._RECOVERY_LOCK, handle._replacement_lock])
    errors, results = [], []
    def on_error(message):
        evidence.timeline.append('callback')
        errors.append(message)
        if callback_mode == 'fault':
            if reported:
                # This is the PRIMARY exception for the old outer logger.
                raise RuntimeError('already-reported callback failed')
            raise CleanupFault(_CLEANUP_SECRET)
    backend._drain_stdout(
        proc=proc, agent_id='fixture-agent', session='fixture-session', trace_id='fixture-trace',
        handle=handle, backend_session_id='', minted=False, on_session_init=None,
        on_result=results.append, on_error=None if callback_mode == 'absent' else on_error,
        stream=None, enqueue=lambda **kw: 0)
    assert backend.active_handles('fixture-agent') == []
    return errors, results


@pytest.mark.parametrize('name', ['grok', 'muse'])
def test_secondary_callback_fault_is_safe_and_keeps_cleanup_order(cleanup_faults, monkeypatch, name):
    f = cleanup_faults
    errors, results = _secondary_runner(name, f, monkeypatch)
    assert errors == ['synthetic read failure'] and results == []
    assert f.timeline.count('callback') == f.timeline.count('terminate') == f.timeline.count('unregister') == 1
    assert f.timeline.count(('wait', 2)) == 1
    ordered = [item for item in f.timeline if item in ('primary', 'terminate', 'callback', 'fault', 'cancel', 'unregister')]
    assert ordered == ['primary', 'terminate', 'callback', 'fault'] + (['cancel'] if name == 'muse' else []) + ['unregister']
    assert f.rows[0][0] == ('server', name + 'ErrorCallbackFailed')
    assert f.rows[0][1]['detail']['error_type'] == 'CleanupFault'
    assert f.checks == [(True, (True,) * len(f.locks))]
    assert _CLEANUP_SECRET not in repr(f.rows) + repr(f.raw)


@pytest.mark.parametrize('name', ['grok', 'muse'])
@pytest.mark.parametrize('mode', ['healthy', 'absent', 'native_error', 'reported'])
def test_callback_latches_do_not_add_retry_or_fault(cleanup_faults, monkeypatch, name, mode):
    f = cleanup_faults
    errors, results = _secondary_runner(
        name, f, monkeypatch, callback_mode='fault' if mode == 'reported' else 'absent' if mode == 'absent' else 'healthy',
        native_error=mode == 'native_error', reported=mode == 'reported')
    assert results == [] and len(errors) == (0 if mode == 'absent' else 1)
    assert f.rows == [] and f.timeline.count('unregister') == 1
    if mode == 'reported':
        assert f.timeline.count('terminate') == 0 and f.timeline.count(('wait', None)) == 1
    else:
        assert f.timeline.count('terminate') == f.timeline.count(('wait', 2)) == 1


@pytest.mark.parametrize('terminal', [False, True])
def test_claude_wait_fault_preserves_cancel_unregister_poll_and_exit(cleanup_faults, monkeypatch, terminal):
    from types import SimpleNamespace
    from lib.backend import claude
    from lib import provider_background_jobs
    f = cleanup_faults
    backend = _private_backend(claude, 'ClaudeBackend', f, monkeypatch)
    proc = _CleanupProc(f.timeline, exited=terminal)
    proc.wait_fault = True
    handle = TurnHandle(proc, None)
    backend.register_handle('fixture-agent', handle)
    f.locks[:] = [backend._registry._lock, proc.wait_lock]
    pacer = SimpleNamespace(offer=lambda *a, **kw: f.timeline.append('offer'),
                            cancel=lambda: f.timeline.append('cancel'))
    monkeypatch.setattr(provider_background_jobs, 'turn_exited', lambda token: f.timeline.append(('exit', token)))
    backend._drain_cleanup(proc, handle, pacer, {'text': 'retained'}, False, False,
                           'fixture-agent', 'fixture-token', 'fixture-trace')
    assert f.timeline == ['offer', 'cancel', 'unregister', ('wait', 5), 'fault', 'poll'] + ([('exit', 'fixture-token')] if terminal else [])
    assert backend.active_handles('fixture-agent') == []
    assert f.rows[0][0] == ('server', 'claudeExitWaitFailed')
    assert f.checks == [(True, (True, True))]
    assert _CLEANUP_SECRET not in f.stderr.getvalue() + repr(f.rows)


@pytest.mark.parametrize('isolated,saw_result,text,offer,unregister', [
    (True, False, 'retained', False, False),
    (False, True, 'retained', False, True),
    (False, False, '', False, True),
    (False, False, 'retained', True, True),
])
def test_claude_absent_token_keeps_offer_and_no_wait_poll(cleanup_faults, monkeypatch, isolated, saw_result, text, offer, unregister):
    from types import SimpleNamespace
    from lib.backend import claude
    f = cleanup_faults
    backend = _private_backend(claude, 'ClaudeBackend', f, monkeypatch)
    proc = _CleanupProc(f.timeline)
    handle = TurnHandle(proc, None)
    pacer = SimpleNamespace(offer=lambda *a, **kw: f.timeline.append('offer'),
                            cancel=lambda: f.timeline.append('cancel'))
    backend._drain_cleanup(proc, handle, pacer, {'text': text}, isolated, saw_result,
                           'fixture-agent', '', 'fixture-trace')
    assert f.timeline == (['offer'] if offer else []) + ['cancel'] + (['unregister'] if unregister else [])
    assert f.rows == []


def _agy_cleanup_failure(evidence, monkeypatch, tmp_path, *, fail_at='terminate', kill_fails=True,
                         exited=False, gate_allows=True, pending_result=False, callback_present=True):
    import json
    import threading
    from lib.backend import agy
    f = evidence
    backend = _private_backend(agy, 'AgyBackend', f, monkeypatch)
    states, snapshots, errors, results = [], [], [], []
    real_state = agy._TurnState
    def capture_state(**kwargs):
        state = real_state(**kwargs)
        states.append(state)
        return state
    monkeypatch.setattr(agy, '_TurnState', capture_state)
    monkeypatch.setattr(agy.server_identity, 'get_server_info', lambda: {'server_id': 'fixture-host'})
    # Restoration is an external persistence boundary here; this control
    # verifies invocation/latch/cleanup, not durable restoration (owner tests remain).
    monkeypatch.setattr(backend, '_restore_failed_turn', lambda *a, **kw: f.timeline.append('restore'))
    rows = []
    if pending_result:
        rows.append(json.dumps({'event': 'result', 'result': {
            'conversation_id': '11111111-2222-4333-8444-555555555555',
            'status': 'SUCCESS', 'response': 'retained pending result'}}))
    def snapshot():
        snapshots.append((states[0].terminal, states[0].pending_result is not None))
    proc = _CleanupProc(f.timeline, stdout=_ScriptedStdout(rows, after_rows=snapshot), exited=exited)
    proc.terminate_fault = fail_at == 'terminate'
    proc.wait_fault = fail_at == 'wait'
    proc.kill_fault = kill_fails
    handle = TurnHandle(proc, None)
    agent_id = '' if pending_result else 'fixture-agent'
    backend.register_handle(agent_id, handle)
    gate_lock = threading.RLock()
    f.locks[:] = [backend._registry._lock, gate_lock, proc.wait_lock]
    def gate(action):
        with gate_lock:
            if not gate_allows:
                return False
            action()
            return True
    log_path = tmp_path / 'synthetic-drain.log'
    log_path.write_text('')
    real_unlink = agy.os.unlink
    def unlink(path, *args, **kwargs):
        if str(path) == str(log_path):
            f.timeline.append('unlink')
        return real_unlink(path, *args, **kwargs)
    monkeypatch.setattr(agy.os, 'unlink', unlink)
    def error(message):
        f.timeline.append('callback')
        errors.append(message)
    backend._drain_stream(
        proc=proc, log_path=str(log_path), agent_id=agent_id,
        session='fixture-session', trace_id='fixture-trace', handle=handle,
        on_session_init=None, on_result=results.append, on_error=error if callback_present else None,
        stream=None, enqueue=lambda **kw: 0, expected_conversation_id='', run_if_owned=gate)
    assert not log_path.exists() and backend.active_handles(agent_id) == []
    return states[0], snapshots, errors, results


@pytest.mark.parametrize('fail_at', ['terminate', 'wait'])
def test_agy_failed_force_kill_reports_without_masking_terminal_cleanup(cleanup_faults, monkeypatch, tmp_path, fail_at):
    f = cleanup_faults
    state, _snapshots, errors, results = _agy_cleanup_failure(f, monkeypatch, tmp_path, fail_at=fail_at)
    ops = [x for x in f.timeline if x in ('terminate', 'kill') or isinstance(x, tuple) and x[0] == 'wait']
    assert ops == ['terminate'] + ([('wait', 2)] if fail_at == 'wait' else []) + ['kill']
    assert len(errors) == 1 and results == [] and state.terminal == 'error' and state.callback_delivered
    assert f.timeline[-2:] == ['unlink', 'unregister'] and f.timeline.count('restore') == 1
    assert f.rows[0][0] == ('server', 'agyForceKillFailed')
    assert f.checks == [(True, (True, True, True))]
    assert _CLEANUP_SECRET not in repr(f.rows) + repr(f.raw)


@pytest.mark.parametrize('exited,kill_fails', [(True, True), (False, False)])
def test_agy_exited_or_successful_kill_adds_no_fault(cleanup_faults, monkeypatch, tmp_path, exited, kill_fails):
    f = cleanup_faults
    state, _snapshots, errors, results = _agy_cleanup_failure(
        f, monkeypatch, tmp_path, exited=exited, kill_fails=kill_fails)
    assert len(errors) == 1 and results == [] and state.callback_delivered
    assert f.rows == [] and f.timeline[-2:] == ['unlink', 'unregister']
    assert f.timeline.count('terminate') == f.timeline.count('kill') == (0 if exited else 1)
    assert not [x for x in f.timeline if isinstance(x, tuple) and x[0] == 'wait']


@pytest.mark.parametrize('gate_allows,callback_present', [(False, True), (True, False)])
def test_agy_valid_zero_callback_paths_stay_zero(cleanup_faults, monkeypatch, tmp_path, gate_allows, callback_present):
    f = cleanup_faults
    state, _snapshots, errors, results = _agy_cleanup_failure(
        f, monkeypatch, tmp_path, gate_allows=gate_allows, callback_present=callback_present)
    assert errors == results == [] and state.callback_delivered is gate_allows
    assert f.timeline.count('restore') == int(gate_allows)
    assert f.timeline[-2:] == ['unlink', 'unregister'] and len(f.rows) == 1


def test_agy_pending_result_is_demoted_by_original_drain_error(cleanup_faults, monkeypatch, tmp_path):
    f = cleanup_faults
    state, snapshots, errors, results = _agy_cleanup_failure(f, monkeypatch, tmp_path, pending_result=True)
    assert snapshots == [('result', True)]
    assert state.terminal == 'error' and state.pending_result is None and state.callback_delivered
    assert len(errors) == 1 and 'drain error after result' in errors[0] and results == []
    assert f.timeline[-1] == 'unlink'  # isolated/no-agent path never unregisters
    assert f.timeline.count('restore') == 1 and len(f.rows) == 1


def _break_new_fault_sinks(evidence, monkeypatch):
    from lib import eventlog
    f = evidence
    class SelectiveBrokenStream:
        def write(self, data):
            if any(data.startswith(code) for code in _FAULT_CODES):
                raise OSError(_CLEANUP_SECRET)
            return f.stderr.write(data)
        def flush(self):
            return f.stderr.flush()
    original_emit = eventlog.emit
    def emit(*args, **kwargs):
        if args[1] in _FAULT_CODES:
            raise OSError(_CLEANUP_SECRET)
        return original_emit(*args, **kwargs)
    def raw(*args, **kwargs):
        raise OSError(_CLEANUP_SECRET)
    monkeypatch.setattr(f.logger.sys, 'stderr', SelectiveBrokenStream())
    monkeypatch.setattr(eventlog, 'emit', emit)
    monkeypatch.setattr(f.logger.os, 'write', raw)


@pytest.mark.parametrize('name', ['grok', 'muse'])
def test_secondary_callback_total_sink_loss_keeps_cleanup(cleanup_faults, monkeypatch, name):
    f = cleanup_faults
    _break_new_fault_sinks(f, monkeypatch)
    errors, results = _secondary_runner(name, f, monkeypatch)
    assert errors == ['synthetic read failure'] and results == []
    assert f.timeline.count('callback') == f.timeline.count('unregister') == 1
    assert f.logger._FAULT_LOG_COUNTS['dropped'] == 1


def test_agy_total_sink_loss_still_restores_delivers_unlinks_unregisters(cleanup_faults, monkeypatch, tmp_path):
    f = cleanup_faults
    _break_new_fault_sinks(f, monkeypatch)
    state, _snapshots, errors, results = _agy_cleanup_failure(f, monkeypatch, tmp_path)
    assert state.callback_delivered and len(errors) == 1 and results == []
    assert f.timeline.count('restore') == 1 and f.timeline[-2:] == ['unlink', 'unregister']
    assert f.logger._FAULT_LOG_COUNTS['dropped'] == 1


def test_secondary_callback_throttle_does_not_skip_stop_callback_or_cleanup(cleanup_faults, monkeypatch):
    f = cleanup_faults
    for _ in range(3):
        _secondary_runner('grok', f, monkeypatch)
    assert len(f.rows) == 1
    assert f.timeline.count('terminate') == f.timeline.count('callback') == f.timeline.count('unregister') == 3
    f.clock[0] += 60
    _secondary_runner('grok', f, monkeypatch)
    assert len(f.rows) == 2 and f.rows[-1][1]['detail']['suppressed'] == 2
    assert f.timeline.count('terminate') == f.timeline.count('callback') == f.timeline.count('unregister') == 4
