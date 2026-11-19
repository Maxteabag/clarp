#!/usr/bin/env python3
"""Own the remote build slot until the command and its remaining children are gone."""
from collections import Counter
from contextlib import contextmanager
import ctypes
import datetime
import fcntl
import os
from pathlib import Path
import select
import shlex
import signal
import subprocess
import sys
import time
from urllib.parse import quote

QUEUE = Path('/var/tmp/clarp-build')
LOCK = QUEUE / 'lock'
HOLDER = QUEUE / 'holder'
RETRY_SECONDS = 0.2
CHILDREN = Path(f'/proc/self/task/{os.getpid()}/children')


def executable(pid):
    try:
        name = Path(os.readlink(f'/proc/{pid}/exe')).name
        return name if name.isprintable() else '<unnamed>'
    except OSError:
        return '<exited>'


def test_place(pid):
    try:
        cwd = Path(os.readlink(f'/proc/{pid}/cwd').removesuffix(' (deleted)'))
    except OSError:
        return None
    if cwd.is_relative_to(QUEUE):
        return None
    # Host tests usually run in "work" below a named fixture. Pytest
    # inserts a per-user parent above its fixtures; never print that parent.
    for root in (os.getenv('TMPDIR'), os.getenv('TEMP'), os.getenv('TMP'), '/tmp', '/var/tmp'):
        if not root or not cwd.is_relative_to(root):
            continue
        parts = cwd.relative_to(root).parts
        if parts and parts[0].startswith('clarp-'):
            return parts[0]
        if len(parts) >= 3 and parts[0].startswith('pytest-of-') and parts[1].startswith('pytest-'):
            return parts[2]
    return None


def clean_children():
    # Only this supervisor waits on its children, so their PIDs cannot be reused
    # before our waitpid. Killing a child may give us its children in turn.
    counts = Counter()
    places = []
    while children := CHILDREN.read_text().split():
        for pid in map(int, children):
            if os.waitpid(pid, os.WNOHANG)[0]:
                continue
            name = quote(executable(pid), safe='')
            counts[name] += 1
            place = test_place(pid)
            if place is not None:
                places.append((name, quote(place, safe='')))
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
    for name, place in places:
        print(f'leftover={name} place={place}')
    summary = [f'{name}:{counts[name]}' for name in sorted(counts)]
    print(f'leftovers={sum(counts.values())}', *summary, flush=True)


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
        fields = Path(f'/proc/{pid}/stat').read_bytes().rsplit(b')', 1)[1].split()
        return None if fields[0] in (b'Z', b'X') else fields[19].decode()
    except (FileNotFoundError, ProcessLookupError):
        return None


def waiting_landings():
    waiting = {}
    for marker in sorted(QUEUE.glob('first-*-*')):
        _, pid, start = marker.name.split('-', 2)
        try:
            if process_start(pid) != start:
                marker.unlink(missing_ok=True)
            else:
                waiting[f'{pid}-{start}'] = marker.read_text().strip()
        except FileNotFoundError:
            continue
    return waiting


def publish_marker(marker, description):
    # Until the complete description is ready, waiters still see the old one.
    temporary = marker.with_name('.' + marker.name)
    try:
        temporary.write_text(description + '\n')
        temporary.replace(marker)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def marker_for(description):
    pid = os.getpid()
    marker = QUEUE / f'first-{pid}-{process_start(pid)}'
    try:
        publish_marker(marker, description)
        yield marker
    finally:
        marker.unlink(missing_ok=True)


def try_lock(lock):
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False


def admitted(landing, lease):
    markers = waiting_landings()
    if lease:
        if not markers.get(lease, '').startswith('lease '):
            raise SystemExit('The landing lease has ended.')
        return True
    if landing:
        return not any(text.startswith('lease ') for text in markers.values())
    return not markers


def acquire_slot(lock, landing, lease):
    while True:
        if admitted(landing, lease) and try_lock(lock):
            if admitted(landing, lease):
                return
            fcntl.flock(lock, fcntl.LOCK_UN)
        time.sleep(RETRY_SECONDS)


def take_slot(lock, landing, description):
    lease = os.getenv('CLARP_BUILD_LEASE')
    if landing and not lease:
        with marker_for(description):
            acquire_slot(lock, landing, None)
    else:
        acquire_slot(lock, landing, lease)


def reserve(description):
    # The local wrapper alone owns the writing end of SSH stdin. EOF also
    # cancels a driver that dies while waiting behind another reservation.
    with marker_for(description) as marker, (QUEUE / 'lease-lock').open('a') as serial:
        while not try_lock(serial):
            if select.select([sys.stdin], [], [], RETRY_SECONDS)[0]:
                return
        publish_marker(marker, 'lease ' + description)
        print(marker.name.removeprefix('first-'), flush=True)
        sys.stdin.buffer.read()


def drive(host, checkout, who, command):
    if not command or os.getenv('CLARP_BUILD_LEASE'):
        raise SystemExit('usage: build lease COMMAND [ARG ...] (one lease per driver)')
    # Hold the checkout lock before registering remotely. Otherwise an ordinary
    # caller could own the checkout while its remote build waits for our lease.
    with open(f'/var/tmp/clarp-build-{checkout}.lock', 'a') as local:
        fcntl.flock(local, fcntl.LOCK_EX)
        remote = shlex.join(['python3', '-c', Path(__file__).read_text(), '--reserve', who])
        with subprocess.Popen(['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=15',
                               host, remote], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              text=True) as keeper:
            try:
                identity = keeper.stdout.readline().strip()
                pid, separator, start = identity.partition('-')
                if not separator or not pid.isdecimal() or not start.isdecimal():
                    raise SystemExit('Could not acquire the landing lease.')
                env = dict(os.environ, CLARP_BUILD_LEASE=identity, CLARP_BUILD_LEASE_CHECKOUT=checkout)
                result = subprocess.run(command, env=env).returncode
                return result if result >= 0 else 128 - result
            finally:
                keeper.stdin.close()


def status(lock):
    markers = waiting_landings()
    for waiting in markers.values():
        if waiting.startswith('lease '):
            print(f'landing lease: {waiting[6:]}')
        else:
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
        print('queue: reserved' if any(m.startswith('lease ') for m in markers.values()) else 'queue: free')


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
    landing = os.environ.get('CLARP_BUILD_FIRST') == '1' or bool(os.getenv('CLARP_BUILD_LEASE'))
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
    if sys.argv[1] == '--drive':
        sys.exit(drive(*sys.argv[2:5], sys.argv[5:]))
    QUEUE.mkdir(exist_ok=True)
    if sys.argv[1] == '--reserve':
        reserve(sys.argv[2] + ' since ' + datetime.datetime.now().strftime('%H:%M:%S'))
        sys.exit(0)
    # Python opens the lock close-on-exec; Popen also closes other descriptors.
    with LOCK.open('a') as slot:
        if sys.argv[1] == 'status':
            status(slot.fileno())
        else:
            sys.exit(run(slot.fileno(), sys.argv[1], sys.argv[2:]))
