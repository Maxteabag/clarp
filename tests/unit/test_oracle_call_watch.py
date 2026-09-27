"""Disposable journals and admissions: never send a real Oracle wake."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import time

import pytest

SPEC = importlib.util.spec_from_file_location('oracle_watch', Path(__file__).parents[2] / 'scripts/oracle_call_watch.py')
watch = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(watch)


@pytest.fixture
def worker(tmp_path, monkeypatch):
    monkeypatch.setattr(watch, 'DIR', tmp_path)
    monkeypatch.setattr(watch, 'SEEN', tmp_path / 'seen.txt')
    monkeypatch.setattr(watch, 'LOG', tmp_path / 'watch.log')
    monkeypatch.setattr(watch, 'bg', lambda *a: subprocess.CompletedProcess(a, 0, '', ''))
    path = tmp_path / 'real-call-fixture.jsonl'
    path.write_text(json.dumps({'event': 'session.summary', 'fields': {'seconds': 40}}))
    os.utime(path, (time.time()-100, time.time()-100))
    return path


def test_failed_admission_retains_pending_and_retries_once(worker, monkeypatch):
    seen = set()
    delivered = set()
    attempts = []
    def admission(path, fields):
        attempts.append(path.stem)
        delivered.add(path.stem)  # accepted, but first response is lost
        if len(attempts) == 1:
            raise TimeoutError('lost response')
        return {'ok': True, 'deduplicated': True}
    monkeypatch.setattr(watch, 'send_review', admission)
    assert watch.scan('handle', seen, 0) == 2
    assert not seen and not watch.SEEN.exists()
    assert watch.scan('handle', seen, 0) == 0
    assert seen == {worker.stem} == delivered
    assert watch.scan('handle', seen, 0) == 0
    assert len(attempts) == 2


@pytest.mark.parametrize('status', [1, 2, 127, -9])
def test_uncertain_or_cancelled_never_delivers(worker, monkeypatch, status):
    monkeypatch.setattr(watch, 'bg', lambda *a: subprocess.CompletedProcess(a, status, '', ''))
    monkeypatch.setattr(watch, 'send_review', lambda *a: pytest.fail('must not deliver'))
    seen = set()
    assert watch.scan('handle', seen, 0) == status
    assert not seen and not watch.SEEN.exists()


def test_restart_uses_fixed_boundary_and_retained_seen(worker, monkeypatch):
    # Journal predates this process, but is inside the mission boundary.
    calls = []
    monkeypatch.setattr(watch, 'send_review', lambda *a: calls.append(a) or {'ok': True})
    assert watch.scan('handle', set(), time.time()-200) == 0
    restored = set(watch.SEEN.read_text().split())
    assert watch.scan('new-generation', restored, time.time()-200) == 0
    assert len(calls) == 1


def test_backoff_recovery_and_cancel_exit(worker, monkeypatch):
    monkeypatch.setattr(watch, 'MISSION', worker.parent)
    boundary = worker.parent / 'since.txt'
    boundary.write_text('0')
    monkeypatch.setattr(watch, 'BOUNDARY', boundary)
    active = iter([2, 2, 0, 1])
    sleeps, scans = [], []
    def bg(*args):
        return subprocess.CompletedProcess(args, next(active) if args[0] == 'job-active' else 0,
                                           'bg1:3:fixture', '')
    monkeypatch.setattr(watch, 'bg', bg)
    monkeypatch.setattr(watch.time, 'sleep', sleeps.append)
    monkeypatch.setattr(watch, 'scan', lambda *a: scans.append(a) or 0)
    assert watch.main() == 0
    assert sleeps == [5, 10, 30]
    assert len(scans) == 1
    assert 'Ownership confirmed again' in watch.LOG.read_text()


def test_review_uses_stable_host_admission_key(worker, monkeypatch, tmp_path):
    code = tmp_path / 'installed' / 'bin'
    code.mkdir(parents=True)
    (code / 'clarp-admin.py').write_text('def api_request(method, path, payload, **kwargs):\n    return payload\n')
    monkeypatch.setenv('CLARP_CODE_ROOT', str(code.parent))
    first = watch.send_review(worker, {'seconds': 40})
    second = watch.send_review(worker, {'seconds': 40})
    assert first['client_msg_id'] == second['client_msg_id']
    assert first['client_msg_id'].endswith(worker.stem)
    assert first['queue_if_busy'] is True
    assert first['origin'] == 'watcher'
