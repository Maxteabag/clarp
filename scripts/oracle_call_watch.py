#!/usr/bin/env python3
"""Mission-owned Oracle call observer. No inference; sends deduplicated review wakes.

The mission supplies its paths and fixed observation boundary through environment
variables. Preserve that boundary and reported_calls.txt across worker restarts.
"""
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time

OWNER = os.environ.get('ORACLE_WATCH_OWNER', 'opus-a5a4')
JOB_ID = os.environ.get('ORACLE_WATCH_JOB', 'oracle-call-watch')
MISSION = Path(os.environ.get('ORACLE_WATCH_MISSION', str(Path.home() / 'Documents/oracle-mission')))
DIR = Path(os.environ.get('ORACLE_WATCH_JOURNALS', str(Path.home() / '.local/share/clarp/oracle-diagnostics')))
LOG = Path(os.environ.get('ORACLE_WATCH_LOG', '/var/tmp/oracle-call-watch.log'))
SEEN = MISSION / 'reported_calls.txt'
BOUNDARY = MISSION / 'watch_since.txt'
POLL_SECONDS = 30


def log(message):
    with LOG.open('a') as out:
        out.write(time.strftime('%Y-%m-%dT%H:%M:%SZ ', time.gmtime()) + message + '\n')


def bg(*args):
    # Optional pinned helper permits a CLI-only repair without a Host restart.
    helper = os.environ.get('ORACLE_WATCH_BG_SCRIPT')
    command = [sys.executable, helper] if helper else ['clarp-agent-bg']
    try:
        result = subprocess.run(command + [OWNER, *args], capture_output=True,
                                text=True, timeout=20,
                                env=dict(os.environ, CLARP_BACKGROUND_WORKER_PID=str(os.getpid())))
    except (OSError, subprocess.TimeoutExpired) as exc:
        result = subprocess.CompletedProcess(command, 2, '', str(exc))
    if args[0] == 'job-active' and result.returncode == 1 and result.stderr.strip():
        # Defensive compatibility: an older helper/import failure can also
        # exit 1. A traceback is never affirmative cancellation evidence.
        result.returncode = 2
    if result.returncode:
        log(f'{args[0]} rc={result.returncode}: {result.stderr.strip()[:500]}')
    return result


def save_seen(seen):
    temporary = SEEN.with_suffix('.tmp')
    with temporary.open('w') as out:
        out.write('\n'.join(sorted(seen)) + '\n')
        out.flush()
        os.fsync(out.fileno())
    temporary.replace(SEEN)


def summary(path):
    try:
        for line in reversed(path.read_text().splitlines()[-6:]):
            row = json.loads(line)
            if row.get('event') == 'session.summary':
                return row['fields']
    except (OSError, ValueError, KeyError) as exc:
        log(f'Cannot read {path.name}: {exc}')
    return None


def send_review(path, fields):
    # Retain the same request id on timeout, retry and worker restart. Host
    # admission deduplication closes the accepted-but-response-lost window.
    root = Path(os.environ.get('CLARP_CODE_ROOT', str(Path.home() / '.local/share/clarp/current')))
    spec = importlib.util.spec_from_file_location('watch_admin', root / 'bin/clarp-admin.py')
    admin = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(admin)
    return admin.api_request('POST', '/send', {
        'session': OWNER, 'origin': 'watcher', 'force_session': True,
        'queue_if_busy': True, 'synthesize_audio': False,
        'client_msg_id': f'{JOB_ID}:{OWNER}:{path.stem}',
        'text': f'[oracle-call-watch] An Oracle call ended: {path} ({fields.get("seconds")} s). '
                f'Review it against {MISSION / "MISSION.md"} and record the evidence.',
    }, timeout=15)


def scan(handle, seen, since):
    for path in sorted(DIR.glob('*.jsonl'), key=lambda p: p.stat().st_mtime):
        modified = path.stat().st_mtime
        if path.stem in seen or modified < since or time.time() - modified < 20:
            continue
        fields = summary(path)
        if fields is None:
            continue
        seconds = float(fields.get('seconds') or 0)
        if seconds <= 15:
            seen.add(path.stem)
            save_seen(seen)
            log(f'Ignored short call {path.stem} ({seconds:.1f}s)')
            continue
        # A fresh ownership check immediately before each delivery, not only
        # once before a potentially long scan.
        status = bg('job-active', handle).returncode
        if status != 0:
            return status
        try:
            receipt = send_review(path, fields)
            if receipt.get('ok') is not True:
                raise RuntimeError(f'Admission not confirmed: {receipt}')
        except Exception as exc:
            log(f'Review admission uncertain for {path.stem}; retaining pending: {exc}')
            return 2
        # Never acknowledge locally before an accepted or deduplicated receipt.
        seen.add(path.stem)
        save_seen(seen)
        log(f'Review accepted {path.stem}: {json.dumps(receipt, sort_keys=True)}')
        bg('job-progress', handle, f'Waiting for Oracle calls >15s; last accepted: {path.stem}')
    return 0


def main():
    MISSION.mkdir(parents=True, exist_ok=True)
    # A second launch cannot steal the generation or send duplicate wakes.
    lock = (MISSION / 'watch_calls.lock').open('a')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log('Existing watcher holds lock; duplicate launch stopped')
        return 0
    # The owner establishes the boundary once, including any outage backlog.
    # Refuse to guess it from process start and silently lose pending calls.
    if not BOUNDARY.exists():
        log(f'Missing fixed observation boundary: {BOUNDARY}')
        return 1
    since = float(BOUNDARY.read_text().strip())
    registered = bg('job-upsert', JOB_ID, 'watcher', 'Oracle call watcher: review each real call')
    if registered.returncode:
        # Cancelled upsert returns a handle with 1; never restart cancellation.
        if registered.returncode == 1 and registered.stdout.strip().startswith('bg1:'):
            log('Registration affirmatively terminal; stopping')
            return 0
        return 1
    handle = registered.stdout.strip()
    log(f'Registered handle={handle} pid={os.getpid()} since={since}')
    bg('job-log', handle, str(LOG))
    bg('job-progress', handle, 'Waiting for real Oracle calls >15s; no inference performed by watcher')
    seen = set(SEEN.read_text().split()) if SEEN.exists() else set()
    delay = 5
    uncertain = False
    while True:
        status = bg('job-active', handle).returncode
        if status == 0:
            if uncertain:
                log('Ownership confirmed again; resuming pending review checks')
                bg('job-progress', handle, 'Ownership recovered; waiting for real Oracle calls >15s')
                uncertain = False
            status = scan(handle, seen, since)
        if status == 1:
            log('Ownership affirmatively terminal or superseded; stopping without restart')
            return 0
        if status != 0:
            uncertain = True
            log(f'Ownership or admission uncertain; no new deliveries; retry in {delay}s')
            time.sleep(delay)
            delay = min(delay * 2, 60)
        else:
            delay = 5
            time.sleep(POLL_SECONDS)


if __name__ == '__main__':
    raise SystemExit(main())
