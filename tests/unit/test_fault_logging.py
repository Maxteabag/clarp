"""Code-only fault diagnostics: sink independence, privacy and bounded work.

Legacy logger output/raise behavior stays owned by test_log. No server,
provider, live database or test-only production hook is needed here.
"""
import io
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from lib import eventlog
from lib import log as logging

SECRET = 'SYNTHETIC_FAULT_SECRET_937'


class PoisonError(Exception):
    def __str__(self):
        raise AssertionError('exception formatting is forbidden')

    def __repr__(self):
        raise AssertionError('exception representation is forbidden')


def test_eventlog_forward_failure_is_visible_without_original_detail(monkeypatch, capsys):
    def fail(*args, **kwargs):
        raise RuntimeError(SECRET)
    monkeypatch.setattr(eventlog, 'emit', fail)
    assert logging._eventlog_emit('originalEvent', 'info', SECRET) is None
    stderr = capsys.readouterr().err
    assert 'logEventlogFailed' in stderr and 'RuntimeError' in stderr
    assert SECRET not in stderr and 'originalEvent' not in stderr


def test_exception_forward_failure_keeps_legacy_line_and_reports_safe_fault(monkeypatch, capsys):
    def fail(*args, **kwargs):
        raise RuntimeError(SECRET)
    monkeypatch.setattr(eventlog, 'emit_exception', fail)
    assert logging.log_exception('legacyFail', ValueError('original legacy message')) is None
    stderr = capsys.readouterr().err
    assert 'original legacy message' in stderr
    assert 'logExceptionEventlogFailed' in stderr and 'RuntimeError' in stderr
    assert SECRET not in stderr


def test_trace_lookup_failure_preserves_original_record_and_reports_fault(tmp_path, monkeypatch, capsys):
    from lib import agents, diagnostics_settings
    diagnostics_settings.update({'enabled': True, 'categories': list(diagnostics_settings.CATEGORIES)})
    monkeypatch.setattr(eventlog, 'LOG_DIR', tmp_path)
    def fail(*args):
        raise RuntimeError(SECRET)
    monkeypatch.setattr(agents, 'get_by_session', fail)
    assert eventlog.emit('server', 'originalEvent', session='fixture-session') is None
    records = [json.loads(line) for path in tmp_path.glob('*.jsonl') for line in path.read_text().splitlines()]
    assert [r['event'] for r in records] == ['originalEvent']
    assert records[0]['session'] == 'fixture-session' and 'trace_id' not in records[0]
    stderr = capsys.readouterr().err
    assert 'eventlogTraceLookupFailed' in stderr and 'RuntimeError' in stderr
    assert SECRET not in stderr


@pytest.fixture(autouse=True)
def _isolated_fault_state():
    # The helper's process-local window must not couple tests or xdist history.
    for name in ('_FAULT_LOG_THROTTLE', '_FAULT_LOG_COUNTS'):
        state = getattr(logging, name, None)
        if state is not None:
            state.clear()
    yield
    for name in ('_FAULT_LOG_THROTTLE', '_FAULT_LOG_COUNTS'):
        state = getattr(logging, name, None)
        if state is not None:
            state.clear()


@pytest.fixture
def sinks(monkeypatch):
    stderr, structured, raw, clock = io.StringIO(), [], [], [100.0]
    # Pytest replaces process sys.stderr between fixture setup and the call.
    # Keep this helper's sink private without competing with phase capture.
    monkeypatch.setattr(logging, 'sys', SimpleNamespace(stderr=stderr))
    monkeypatch.setattr(logging.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(eventlog, 'emit', lambda *args, **kwargs: structured.append((args, kwargs)))
    monkeypatch.setattr(os, 'write', lambda fd, data: raw.append((fd, data)) or len(data))
    yield SimpleNamespace(stderr=stderr, structured=structured, raw=raw, clock=clock)



@pytest.mark.parametrize('stderr_bad,structured_bad,raw_bad,structured_mode,expected', [
    (False, False, False, True, 'attempted'),
    (True, False, True, True, 'attempted'),
    (False, True, True, True, 'attempted'),
    (True, True, False, True, 'attempted'),
    (True, True, True, True, 'dropped'),
    (False, False, False, False, 'attempted'),
    (True, False, True, False, 'dropped'),
])
def test_selected_sink_matrix(sinks, monkeypatch, stderr_bad, structured_bad, raw_bad, structured_mode, expected):
    calls = []
    class BrokenStream:
        def write(self, data):
            calls.append('stderr')
            raise OSError(SECRET)
        def flush(self):
            raise AssertionError('failed write must not flush')
    if stderr_bad:
        monkeypatch.setattr(logging.sys, 'stderr', BrokenStream())
    def structured(*args, **kwargs):
        calls.append('structured')
        if structured_bad:
            raise RuntimeError(SECRET)
        sinks.structured.append((args, kwargs))
    monkeypatch.setattr(eventlog, 'emit', structured)
    def raw(fd, data):
        calls.append('raw')
        if raw_bad:
            raise OSError(SECRET)
        sinks.raw.append((fd, data))
        return len(data)
    monkeypatch.setattr(os, 'write', raw)
    assert logging.log_fault('fixtureFault', PoisonError(SECRET), structured=structured_mode) == expected
    assert calls.count('structured') == int(structured_mode)
    assert calls.count('raw') == int(stderr_bad)
    assert calls.count('stderr') == int(stderr_bad)
    blob = sinks.stderr.getvalue() + json.dumps(sinks.structured) + repr(sinks.raw)
    assert SECRET not in blob
    if expected == 'dropped':
        assert logging._FAULT_LOG_COUNTS['dropped'] == 1


def test_stderr_only_mode_never_imports_eventlog(sinks, monkeypatch):
    import builtins
    original = builtins.__import__
    imports = []
    def guarded(name, *args, **kwargs):
        if 'eventlog' in name or 'eventlog' in (kwargs.get('fromlist') or (args[2] if len(args) > 2 else ()) or ()):
            imports.append(name)
            raise AssertionError('structured=False imported eventlog')
        return original(name, *args, **kwargs)
    # Restore even on assertion failure, before independent fixture teardown.
    with monkeypatch.context() as scoped:
        scoped.setattr(builtins, '__import__', guarded)
        assert logging.log_fault('stderrOnlyFault', PoisonError(SECRET), structured=False) == 'attempted'
    # Sink failures are contained by the helper: observe attempted imports too.
    assert imports == []
    assert sinks.structured == []
    assert 'stderrOnlyFault' in sinks.stderr.getvalue()


def test_reentry_drops_without_nested_sinks_and_guard_recovers(sinks, monkeypatch):
    nested = []
    writes = []
    class ReenteringStream:
        def write(self, data):
            writes.append(data)
            nested.append(logging.log_fault('recursiveFault', PoisonError(SECRET)))
            return len(data)
        def flush(self):
            return None
    monkeypatch.setattr(logging.sys, 'stderr', ReenteringStream())
    assert logging.log_fault('outerFault', PoisonError(SECRET)) == 'attempted'
    assert nested and set(nested) == {'dropped'}
    assert len(writes) <= 2 and len(sinks.structured) == 1 and sinks.raw == []
    monkeypatch.setattr(logging.sys, 'stderr', sinks.stderr)
    assert logging.log_fault('afterRecursiveFault', PoisonError(SECRET)) == 'attempted'
    assert len(sinks.structured) == 2


@pytest.mark.parametrize('interrupt', [KeyboardInterrupt, SystemExit])
def test_process_control_exceptions_propagate_and_guard_clears(sinks, monkeypatch, interrupt):
    def stop(*args, **kwargs):
        raise interrupt()
    monkeypatch.setattr(eventlog, 'emit', stop)
    with pytest.raises(interrupt):
        logging.log_fault('interruptedFault', RuntimeError(SECRET))
    monkeypatch.setattr(eventlog, 'emit', lambda *args, **kwargs: sinks.structured.append((args, kwargs)))
    assert logging.log_fault('afterInterruptFault', RuntimeError(SECRET)) == 'attempted'


def test_monotonic_window_and_suppression_counts(sinks):
    assert logging.log_fault('clockFault', RuntimeError(SECRET)) == 'attempted'
    for _ in range(3):
        assert logging.log_fault('clockFault', RuntimeError(SECRET)) == 'throttled'
    assert logging.log_fault('distinctFault', RuntimeError(SECRET)) == 'attempted'
    assert len(sinks.structured) == 2
    sinks.clock[0] += 59.99
    assert logging.log_fault('clockFault', RuntimeError(SECRET)) == 'throttled'
    sinks.clock[0] += .01
    assert logging.log_fault('clockFault', RuntimeError(SECRET)) == 'attempted'
    assert sinks.structured[-1][1]['detail']['suppressed'] == 4
    assert SECRET not in repr(logging._FAULT_LOG_THROTTLE)


def test_all_failed_attempt_consumes_window_then_recovers(sinks, monkeypatch):
    class BrokenStream:
        def write(self, data):
            raise OSError(SECRET)
        def flush(self):
            raise OSError(SECRET)
    def fail(*args, **kwargs):
        raise OSError(SECRET)
    monkeypatch.setattr(logging.sys, 'stderr', BrokenStream())
    monkeypatch.setattr(eventlog, 'emit', fail)
    monkeypatch.setattr(os, 'write', fail)
    assert logging.log_fault('failedFault', RuntimeError(SECRET)) == 'dropped'
    monkeypatch.setattr(logging.sys, 'stderr', sinks.stderr)
    assert logging.log_fault('failedFault', RuntimeError(SECRET)) == 'throttled'
    sinks.clock[0] += 60
    assert logging.log_fault('failedFault', RuntimeError(SECRET)) == 'attempted'


def test_overflow_never_evicts_live_keys_to_bypass_throttle(sinks):
    for i in range(200):
        logging.log_fault(f'fixtureFault{i}', RuntimeError(SECRET))
    assert len(logging._FAULT_LOG_THROTTLE) <= 128
    before = len(sinks.structured)
    for i in range(200):
        assert logging.log_fault(f'fixtureFault{i}', RuntimeError(SECRET)) == 'throttled'
    assert len(sinks.structured) == before <= 128
    sinks.clock[0] += 120
    assert logging.log_fault('freshFault', RuntimeError(SECRET)) == 'attempted'
    assert len(logging._FAULT_LOG_THROTTLE) <= 128


def test_concurrent_calls_reserve_one_attempt_without_sink_lock(sinks, monkeypatch):
    def structured(*args, **kwargs):
        assert logging._FAULT_LOG_LOCK.acquire(blocking=False), 'sink called under throttle lock'
        logging._FAULT_LOG_LOCK.release()
        sinks.structured.append((args, kwargs))
    monkeypatch.setattr(eventlog, 'emit', structured)
    barrier = threading.Barrier(8)
    def report(_):
        barrier.wait(timeout=3)
        return logging.log_fault('concurrentFault', RuntimeError(SECRET))
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(report, range(8)))
    assert results.count('attempted') == 1 and results.count('throttled') == 7
    assert len(sinks.structured) == 1


def test_invalid_labels_are_replaced_not_echoed_or_retained(sinks):
    secret_type = type(SECRET + '\ninvalid', (Exception,), {})
    assert logging.log_fault(SECRET + '\ninvalid', secret_type()) == 'attempted'
    blob = sinks.stderr.getvalue() + json.dumps(sinks.structured) + repr(logging._FAULT_LOG_THROTTLE)
    assert SECRET not in blob and 'faultLogInvalidCode' in blob and 'UnknownError' in blob


@pytest.mark.parametrize('exception_logger', [False, True])
def test_legacy_primary_print_failure_still_raises_before_forwarding(monkeypatch, exception_logger):
    class BrokenStream:
        def write(self, data):
            raise OSError('broken primary stream')
        def flush(self):
            raise AssertionError('failed write must not flush')
    def forbidden(*args, **kwargs):
        pytest.fail('legacy logger forwarded after a primary print failure')
    monkeypatch.setattr(logging.sys, 'stderr', BrokenStream())
    monkeypatch.setattr(eventlog, 'emit', forbidden)
    monkeypatch.setattr(eventlog, 'emit_exception', forbidden)
    with pytest.raises(OSError, match='broken primary stream'):
        if exception_logger:
            logging.log_exception('legacyFault', ValueError('legacy message'))
        else:
            logging.log('legacyEvent', 'legacy detail')
