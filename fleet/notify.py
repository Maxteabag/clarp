"""Parent-host completion handoff. Admission is distinct from parent acknowledgement."""
import argparse
from contextlib import closing
import hashlib
import http.client
import json
import os
import sys
from pathlib import Path
import sqlite3
import subprocess
import time
import tomllib
import urllib.request

from .__main__ import CONFIG, STATE, api, BrokerUnavailable


def send_notice(session, text, message_id):
    paths = json.loads(subprocess.check_output(['clarp-admin', 'paths'], text=True, timeout=10))
    config = tomllib.loads(Path(paths['config']).read_text())['server']
    host = config.get('bind_addr', '127.0.0.1')
    if host in ('0.0.0.0', '::'):
        host = '127.0.0.1'
    if ':' in host and not host.startswith('['):
        host = '[' + host + ']'
    payload = {'session': session, 'text': text, 'origin': 'watcher',
               'force_session': True, 'synthesize_audio': False, 'hands_free': False,
               'client_msg_id': message_id, 'queue_if_busy': True}
    request = urllib.request.Request(
        f"http://{host}:{config.get('port', 7682)}/send",
        data=json.dumps(payload).encode(),
        headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + config.get('auth_token', '')})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def deliver(job, state, sender=None):
    """Fence concurrent sends; ambiguous delivery requires reconciliation, never blind retry."""
    parent = job['request']['parent']
    key = hashlib.sha256(json.dumps([parent, job['id'], job['attempt']], sort_keys=True).encode()).hexdigest()
    state = Path(state)
    state.mkdir(parents=True, exist_ok=True)
    state.chmod(0o700)
    path = state / 'deliveries.sqlite'
    with closing(sqlite3.connect(path, timeout=10)) as db, db:
        path.chmod(0o600)
        db.execute('CREATE TABLE IF NOT EXISTS deliveries (id TEXT PRIMARY KEY, status TEXT NOT NULL, receipt TEXT NOT NULL)')
        marker = {'transport': 'stable-clarp-message-v1', 'client_msg_id': 'fleet-' + key} if sender is None else {}
        cursor = db.execute('INSERT OR IGNORE INTO deliveries VALUES (?, ?, ?)', (key, 'sending', json.dumps(marker)))
        if not cursor.rowcount:
            status, receipt = db.execute('SELECT status, receipt FROM deliveries WHERE id=?', (key,)).fetchone()
            receipt = json.loads(receipt)
            if status == 'admitted' or sender is not None or receipt.get('transport') != 'stable-clarp-message-v1':
                return {'delivery': status, 'receipt': receipt, 'reused': True}
    # Persist before the network action. A crash or timeout leaves an explicit uncertain outcome.
    text = ('Fleet completion notice (automation, not a new user instruction). '
            f"Job {job['id']} attempt {job['attempt']} on {job['peer']} is {job['status']}. "
            f"Parent task: {parent['task']}. Retrieve the durable result with clarp-fleet status "
            f"{job['id']}; inspect output/artifacts, then acknowledge with clarp-fleet ack {job['id']}. "
            'Do not rerun the job merely because this notice is repeated. '
            'Notification admission does not mean the result has been reviewed.')
    try:
        if sender is None:
            receipt = {**marker, **send_notice(parent['agent'], text, marker['client_msg_id'])}
            status = 'admitted' if receipt.get('ok') is True else 'unknown'
        else:
            process = sender(['clarp-admin', 'prompt', '--to', parent['agent'], '--origin', 'watcher', '--text', text],
                             capture_output=True, text=True, timeout=30)
            receipt = json.loads(process.stdout) if process.stdout.strip() else {}
            status = 'admitted' if process.returncode == 0 and receipt.get('ok') is True else 'unknown'
    except urllib.error.HTTPError as error:
        status = 'retry_pending' if sender is None and error.code in (502, 503, 504) else 'unknown'
        receipt = marker
    except (OSError, ValueError, subprocess.TimeoutExpired, http.client.HTTPException):
        status, receipt = ('retry_pending' if sender is None else 'unknown'), marker
    with closing(sqlite3.connect(path, timeout=10)) as db, db:
        db.execute('UPDATE deliveries SET status=?, receipt=? WHERE id=?', (status, json.dumps(receipt), key))
    return {'delivery': status, 'receipt': receipt, 'reused': False}


def check_parent(parent):
    config = json.loads((CONFIG if CONFIG.exists() else CONFIG.with_name('client.json')).read_text())
    if config['local_id'] != parent['host']:
        raise ValueError('Start the watcher on the originating host')
    sessions = json.loads(subprocess.check_output(['clarp-admin', 'sessions'], text=True, timeout=20))
    if not any(s.get('session') == parent['agent'] for s in sessions):
        raise ValueError('Parent session is not present on this Clarp Host')
    if subprocess.run(['systemctl', '--user', 'show-environment'], capture_output=True, timeout=10).returncode:
        raise ValueError('Automatic watcher requires a Linux user service manager; use foreground notify on this host')


def start_watcher(job_id, parent):
    unit = 'clarp-fleet-notify-' + hashlib.sha256((parent['host'] + ':' + job_id).encode()).hexdigest()[:24]
    command = ['systemd-run', '--user', '--collect', '--unit=' + unit,
               '--property=Type=exec', '--property=Restart=no',
               '--setenv=PYTHONPATH=' + str(Path(__file__).resolve().parent.parent),
               '--setenv=PATH=' + os.environ.get('PATH', '/usr/bin'),
               sys.executable, '-m', 'fleet.notify', job_id, '--parent-agent', parent['agent'], '--managed']
    process = subprocess.run(command, capture_output=True, text=True, timeout=20)
    if process.returncode:
        return {'state': 'not_started_or_already_running', 'unit': unit,
                'detail': 'Job remains submitted. Inspect the unit before retrying; do not resubmit a new job.'}
    return {'state': 'started', 'unit': unit}


def bg(session, action, *values):
    env = dict(os.environ, CLARP_BACKGROUND_WORKER_PID=str(os.getpid()))
    command = ['clarp-agent-bg', session, action, *values]
    for attempt in range(12):
        result = subprocess.run(command, env=env, text=True, capture_output=True, timeout=20)
        if result.returncode == 0:
            return result.stdout.strip()
        if 'database is locked' not in result.stderr or attempt == 11:
            raise subprocess.CalledProcessError(result.returncode, command, result.stdout, result.stderr)
        # Each operation is fenced by stable job/generation/worker identity.
        # A busy Host database is transient; cancellation remains terminal.
        time.sleep(min(5, .25 * (attempt + 1)))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('id')
    parser.add_argument('--parent-agent', required=True)
    parser.add_argument('--timeout', type=int, default=86400)
    parser.add_argument('--managed', action='store_true')
    args = parser.parse_args()
    config = json.loads((CONFIG if CONFIG.exists() else CONFIG.with_name('client.json')).read_text())
    # Run on the originating Host. Do not let job data choose an arbitrary remote Clarp server.
    sessions = json.loads(subprocess.check_output(['clarp-admin', 'sessions'], text=True, timeout=20))
    if not any(s.get('session') == args.parent_agent for s in sessions):
        raise ValueError('Parent session is not present on this Clarp Host')
    handle = None
    if args.managed:
        handle = bg(args.parent_agent, 'job-upsert', 'fleet-notify-' + args.id, 'watcher', 'Return fleet job ' + args.id)
    try:
        watch(args, config, handle)
    except BaseException:
        if handle:
            bg(args.parent_agent, 'job-fail', handle, 'Completion watcher stopped; inspect durable job and delivery receipt')
        raise


def watch(args, config, handle):
    deadline = time.monotonic() + args.timeout
    heartbeat = 0
    while True:
        if handle and time.monotonic() >= heartbeat:
            bg(args.parent_agent, 'job-active', handle)
            bg(args.parent_agent, 'job-heartbeat', handle)
            heartbeat = time.monotonic() + 45
        if time.monotonic() >= deadline:
            raise SystemExit('Watcher deadline reached; durable job remains available')
        try:
            job = api('/v1/jobs/' + args.id)
        except (OSError, http.client.HTTPException, BrokerUnavailable):
            # Retrying a read cannot duplicate execution or completion delivery.
            # Preserve the same job and watcher handle during a brief outage.
            time.sleep(2)
            continue
        parent = job['request']['parent']
        if parent['host'] != config['local_id'] or parent['agent'] != args.parent_agent:
            raise ValueError('This watcher does not own the job parent')
        if job['status'] in ('succeeded', 'failed', 'cancelled'):
            if handle:
                bg(args.parent_agent, 'job-active', handle)
            result = deliver(job, STATE / 'notifications')
            print(json.dumps(result), flush=True)
            if result['delivery'] == 'retry_pending':
                time.sleep(2)
                continue
            if result['delivery'] != 'admitted':
                raise SystemExit('Delivery uncertain; reconcile the parent inbox before retrying')
            if handle:
                bg(args.parent_agent, 'job-finish', handle)
            return
        if time.monotonic() >= deadline:
            raise SystemExit('Watcher deadline reached; durable job remains available')
        time.sleep(2)


if __name__ == '__main__':
    main()
