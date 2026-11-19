#!/usr/bin/env python3
"""Own the remote build slot until the command and its remaining children are gone."""
import ctypes
import datetime
import fcntl
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

QUEUE = Path('/var/tmp/clarp-build')
LOCK = QUEUE / 'lock'
HOLDER = QUEUE / 'holder'
STALE_SECONDS = 15 * 60
RETRY_SECONDS = 0.2
CHILDREN = Path(f'/proc/self/task/{os.getpid()}/children')


def executable(pid):
    try:
        name = Path(os.readlink(f'/proc/{pid}/exe')).name
        return name if name.isprintable() else '<unnamed>'
    except OSError:
        return '<exited>'


def clean_children():
    # Only this supervisor waits on its children, so their PIDs cannot be reused
    # before our waitpid. Killing a child may give us its children in turn.
    names = []
    while children := CHILDREN.read_text().split():
        for pid in map(int, children):
            if os.waitpid(pid, os.WNOHANG)[0]:
                continue
            names.append(executable(pid))
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
    print(f'Build leftovers: {len(names)}' + ''.join(f' {name}' for name in names), flush=True)


def recorded_holder(identity):
    # Some filesystems report a different device number in /proc/locks
    # than in stat. Ask the recorded owner's actual descriptor instead.
    fields = identity.split(':')
    if len(fields) != 3 or not all(field.isdecimal() for field in fields):
        return False
    pid, start, fd = fields
    proc = Path('/proc') / pid
    try:
        if process_start(pid) != start or not (proc / 'fd' / fd).samefile(LOCK):
            return False
        for line in (proc / 'fdinfo' / fd).read_text().splitlines():
            words = line.split()
            if words[:1] == ['lock:'] and words[2:6] == ['FLOCK', 'ADVISORY', 'WRITE', pid]:
                return True
    except OSError:
        pass
    return False


def process_start(pid):
    try:
        # Field 22 begins after the parenthesized comm and 19 other fields.
        return Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[19]
    except (FileNotFoundError, ProcessLookupError):
        return None


def waiting_landings():
    waiting = []
    for marker in sorted(QUEUE.glob('first-*-*')):
        _, pid, start = marker.name.split('-', 2)
        try:
            age = time.time() - marker.stat().st_mtime
            if age > STALE_SECONDS and process_start(pid) != start:
                marker.unlink(missing_ok=True)
            else:
                waiting.append(marker.read_text().strip())
        except FileNotFoundError:
            continue
    return waiting


def take_slot(lock, landing, description):
    if landing:
        pid = os.getpid()
        marker = QUEUE / f'first-{pid}-{process_start(pid)}'
        try:
            marker.write_text(description + '\n')
            fcntl.flock(lock, fcntl.LOCK_EX)
        finally:
            marker.unlink(missing_ok=True)
        return
    while True:
        if not waiting_landings():
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                pass
            else:
                # A landing may have registered while we were acquiring.
                if not waiting_landings():
                    return
                fcntl.flock(lock, fcntl.LOCK_UN)
        time.sleep(RETRY_SECONDS)


def status(lock):
    for waiting in waiting_landings():
        print(f'landing waiting: {waiting}')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        try:
            identity, _, description = HOLDER.read_text().strip().partition(' ')
        except FileNotFoundError:
            identity, description = '', ''
        if description and recorded_holder(identity):
            print(f'building: {description}')
        else:
            users = subprocess.run(['fuser', str(LOCK)], capture_output=True, text=True)
            names = [executable(pid) for pid in users.stdout.split() if pid.isdecimal() and int(pid) != os.getpid()]
            print('queue: held without a current holder record; file users:', *names)
    else:
        print('queue: free')


def cancelled(signum, frame):
    raise SystemExit(128 + signum)


def run(lock, who, command):
    # PR_SET_CHILD_SUBREAPER adopts orphaned descendants, including children
    # which start their own process group or session.
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(36, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), 'cannot supervise build children')
    for sig in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, cancelled)
    landing = os.environ.get('CLARP_BUILD_FIRST') == '1'
    since = datetime.datetime.now().strftime('%H:%M:%S')
    take_slot(lock, landing, f'{who} since {since}')
    try:
        prefix = 'landing ' if landing else ''
        since = datetime.datetime.now().strftime('%H:%M:%S')
        identity = f'{os.getpid()}:{process_start(os.getpid())}:{lock}'
        HOLDER.write_text(f'{identity} {prefix}{who} since {since}\n')
        child = subprocess.Popen(command)
        result = child.wait()
        return result if result >= 0 else 128 - result
    finally:
        for sig in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, signal.SIG_IGN)
        try:
            clean_children()
        finally:
            HOLDER.write_text('')


if __name__ == '__main__':
    QUEUE.mkdir(exist_ok=True)
    # Python opens the lock close-on-exec; Popen also closes other descriptors.
    with LOCK.open('a') as slot:
        if sys.argv[1] == 'status':
            status(slot.fileno())
        else:
            sys.exit(run(slot.fileno(), sys.argv[1], sys.argv[2:]))
