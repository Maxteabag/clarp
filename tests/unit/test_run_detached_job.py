"""The clarp-background-jobs worker: it owns the job and writes the receipt.

Runs the shipped script with the real agent_bg helper on the test database.
"""
import os
import pathlib
import shlex
import signal
import subprocess
import sys
import time

import pytest

from lib import agents, background_jobs as jobs, db

ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = ROOT / 'skills/clarp-background-jobs/scripts/run_detached_job.sh'


@pytest.fixture
def run(tmp_path):
    agents.create_agent(persona='Ziggy fixture', voice_id='', cwd=str(tmp_path), session='ziggy-fixture')
    env = {**os.environ, 'CLAUDE_PWA_DB': str(db.DB_PATH), 'CLARP_CODE_ROOT': str(ROOT / 'server'),
           'CLARP_AGENT_BG': shlex.join([sys.executable, str(ROOT / 'scripts/agent_bg.py')])}
    env.pop('CLARP_BACKGROUND_WORKER_PID', None)

    def start(job_id, *command, log_name=None, expect=0):
        log = tmp_path / 'out' / (log_name or f'{job_id}.log')   # directory made by the script
        out = subprocess.run([str(SCRIPT), 'ziggy-fixture', job_id, 'render', 'Render fixture', str(log), '--', *command],
                             env=env, capture_output=True, text=True, timeout=40)
        assert out.returncode == expect, out.stderr
        return (out.stdout.strip() if expect == 0 else out.stderr), log
    return start


def wait_for(predicate, timeout=20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return False


@pytest.mark.parametrize('code,status', [(0, 'succeeded'), (3, 'failed')])
def test_worker_reports_the_commands_own_exit_and_keeps_its_log(run, code, status):
    handle, log = run(f'exit-{code}', 'sh', '-c', f'echo rendering; sleep 1; exit {code}')
    assert handle == f'bg1:1:exit-{code}'
    assert jobs.get(f'exit-{code}', reconcile=False)['status'] == 'running'  # returned while the work runs
    assert wait_for(lambda: jobs.get(f'exit-{code}', reconcile=False)['status'] == status)
    job = jobs.get(f'exit-{code}', reconcile=False)
    assert pathlib.Path(f'{log}.exit').read_text().strip() == str(code)
    assert log.read_text().strip() == 'rendering' and job['log_path'] == str(log)
    assert job['progress_text'] == f'Command exited {code}; log {log}'
    assert job['terminal_reason'] == ('' if code == 0 else f'exit {code}')


def test_cancelling_the_job_stops_the_workers_own_command(run, tmp_path):
    handle, log = run('cancel-me', 'sleep', '30')
    jobs.cancel('cancel-me')
    assert wait_for(lambda: pathlib.Path(f'{log}.exit').exists(), timeout=30)
    assert jobs.get('cancel-me', reconcile=False)['status'] == 'cancelled'


def test_a_cancelled_job_id_is_not_reused_for_a_new_run(run, tmp_path):
    handle, log = run('one-run', 'sleep', '30')
    jobs.cancel('one-run')
    assert wait_for(lambda: pathlib.Path(f'{log}.exit').exists(), timeout=30)
    marker = tmp_path / 'second-run-started'
    error, _ = run('one-run', 'touch', str(marker), log_name='again.log', expect=1)
    assert 'job-upsert failed' in error
    time.sleep(1)
    assert not marker.exists() and jobs.get('one-run', reconcile=False)['status'] == 'cancelled'


def test_a_worker_killed_before_its_receipt_is_never_reported_as_success(run, tmp_path):
    handle, log = run('lost-worker', 'sleep', '5')   # its command ends by itself
    pid = jobs.get('lost-worker', reconcile=False)['worker_pid']
    os.killpg(os.getpgid(pid), signal.SIGKILL)   # the worker's own group: its command lives on in its own session
    assert wait_for(lambda: jobs.get('lost-worker')['status'] == 'failed')
    job = jobs.get('lost-worker')
    assert job['terminal_reason'] == 'worker_vanished' and job['outcome_state'] == 'unknown'
    assert not pathlib.Path(f'{log}.exit').exists()
