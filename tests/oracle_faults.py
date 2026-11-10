"""Synthetic sinks for real Oracle consumer fault reporting; no logger mock."""
import io
import json
import threading
from types import SimpleNamespace

import pytest

from lib import eventlog, log as logging

SECRET = 'ORACLE_FAULT_PRIVATE_PAYLOAD_937'


class FaultSinks:
    def __init__(self):
        self.failed = False
        self.clock = 100.0
        self.lock = None
        self.lock_observations = []
        self.writes = []
        self.structured = []
        self.text = io.StringIO()

    def observe_lock(self):
        if self.lock is None:
            return
        acquired = []

        def probe():
            held = self.lock.acquire(blocking=False)
            acquired.append(held)
            if held:
                self.lock.release()
        worker = threading.Thread(target=probe)
        worker.start()
        worker.join(timeout=2)
        self.lock_observations.append(not worker.is_alive() and acquired == [True])

    def write(self, text):
        self.observe_lock()
        self.writes.append(text)
        if self.failed:
            raise OSError('synthetic stderr failure')
        return self.text.write(text)

    def flush(self):
        pass

    def emit(self, source, code, *, level, detail):
        self.observe_lock()
        self.structured.append((source, code, level, detail))
        if self.failed:
            raise OSError('synthetic structured failure')

    def raw(self, fd, data):
        self.observe_lock()
        self.writes.append(data.decode('ascii'))
        raise OSError('synthetic raw fallback failure')

    def assert_fault(self, code, error_type='OSError'):
        assert self.structured == [('server', code, 'warning', {
            'error_type': error_type, 'suppressed': 0})]
        evidence = ''.join(self.writes) + json.dumps(self.structured)
        assert code in evidence and error_type in evidence
        assert SECRET not in evidence
        if self.lock is not None:
            assert self.lock_observations and all(self.lock_observations)


@pytest.fixture
def fault_sinks(monkeypatch):
    sinks = FaultSinks()
    # Replace module references, not the process-wide clock/stderr/os module.
    monkeypatch.setattr(logging, 'time', SimpleNamespace(monotonic=lambda: sinks.clock))
    monkeypatch.setattr(logging, 'sys', SimpleNamespace(stderr=sinks))
    monkeypatch.setattr(logging, 'os', SimpleNamespace(write=sinks.raw))
    monkeypatch.setattr(logging, '_FAULT_LOG_THROTTLE', {})
    monkeypatch.setattr(logging, '_FAULT_LOG_COUNTS', {})
    monkeypatch.setattr(logging, '_FAULT_LOG_LOCAL', threading.local())
    monkeypatch.setattr(eventlog, 'emit', sinks.emit)
    return sinks
