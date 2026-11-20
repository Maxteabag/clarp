#!/usr/bin/env python3
"""Own the remote build slot until the command and its remaining children are gone."""
from collections import Counter
from contextlib import contextmanager
import ctypes
import datetime
import fcntl
import json
import os
from pathlib import Path
import re
import select
import shlex
import shutil
import signal
import stat
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


def place_in(path):
    if path.is_relative_to(QUEUE):
        return None
    # Host tests usually run in "work" below a named fixture. Pytest
    # inserts a per-user parent above its fixtures; never print that parent.
    for root in (os.getenv('TMPDIR'), os.getenv('TEMP'), os.getenv('TMP'), '/tmp', '/var/tmp'):
        if not root or not path.is_relative_to(root):
            continue
        parts = path.relative_to(root).parts
        if parts and parts[0].startswith('clarp-'):
            return parts[0]
        if len(parts) >= 3 and parts[0].startswith('pytest-of-') and parts[1].startswith('pytest-'):
            return parts[2]
    return None


def test_place(pid, name):
    proc = Path('/proc') / str(pid)
    try:
        place = place_in(Path(os.readlink(proc / 'cwd').removesuffix(' (deleted)')))
    except OSError:
        place = None
    if place or name != 'clarp-shim':
        return place
    # The shim retains its turn spool even after the test removes the place.
    # Its own cwd can be the checkout. Never read or report its command line.
    try:
        for fd in (proc / 'fd').iterdir():
            try:
                path = Path(os.readlink(fd).removesuffix(' (deleted)'))
            except OSError:
                continue
            if path.parent.name == 'spool' and path.suffix == '.jsonl':
                place = place_in(path)
                if place:
                    return place
    except OSError:
        pass
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
            place = test_place(pid, name)
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
        with (QUEUE / 'eviction-lock').open('a') as guard:
            fcntl.flock(guard, fcntl.LOCK_EX)
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
    for identity, waiting in markers.items():
        if waiting.startswith('lease '):
            pid = identity.partition('-')[0]
            print(f'landing lease: {waiting[6:]} (keeper PID {pid}; ending this process releases the reservation)')
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


# All removal is relative to open directories. Never resolve a checkout, its
# workspace or its target through a symlink.
DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
STAMP = '.clarp-last-build'


@contextmanager
def directory(name, parent=None):
    fd = os.open(name, DIRECTORY, dir_fd=parent)
    try:
        yield fd
    finally:
        os.close(fd)


def regular_file(parent, name):
    return stat.S_ISREG(os.stat(name, dir_fd=parent, follow_symlinks=False).st_mode)


def last_build(name, copy, workspace):
    # Earlier dedicated build copies predate the stamp. Verify the copy
    # layout even when a stamp is present; a timestamp alone grants nothing.
    if name.endswith('-repo'):
        return None
    with directory('contract', copy) as contract, directory('schemas', contract) as schemas:
        if not regular_file(schemas, 'server-info.json'):
            return None
    with directory('scripts', copy) as scripts:
        if not regular_file(scripts, 'clarp-media-publish.py'):
            return None
    with directory('crates', workspace) as crates:
        for crate in ('clarp-host', 'clarp-kernel', 'clarp-shim'):
            with directory(crate, crates) as package:
                if not regular_file(package, 'Cargo.toml'):
                    return None
    try:
        stamp = os.stat(STAMP, dir_fd=copy, follow_symlinks=False)
        return stamp.st_mtime_ns if stat.S_ISREG(stamp.st_mode) else None
    except FileNotFoundError:
        pass
    # Cargo's latest compiler-info update is the initial age of an old copy.
    with directory('target', workspace) as target:
        try:
            info = os.stat('.rustc_info.json', dir_fd=target, follow_symlinks=False)
        except FileNotFoundError:
            info = os.fstat(target)
        return info.st_mtime_ns


def free_after_removal(floor):
    free = shutil.disk_usage('.').free
    if free < floor:
        # A CoW filesystem can defer releasing extents until its transaction commits.
        subprocess.run(['sync', '-f', '.'], check=True)
        free = shutil.disk_usage('.').free
    return free


def trim_test_executables(deps):
    files = []
    for name in os.listdir(deps):
        match = re.fullmatch(r'([A-Za-z0-9_]+)-[0-9a-f]{16}', name)
        if match:
            info = os.stat(name, dir_fd=deps, follow_symlinks=False)
            if stat.S_ISREG(info.st_mode) and info.st_mode & 0o111:
                files.append((info.st_mtime_ns, name, match[1]))
    # Break timestamp ties by filename, keeping one newest executable per crate/test name.
    files.sort()
    newest = {group: name for _, name, group in files}
    removed = 0
    for _, name, group in files:
        if name != newest[group]:
            os.unlink(name, dir_fd=deps)
            removed += 1
    return removed


def make_room(root, current, floor):
    free = shutil.disk_usage('.').free
    removed = []
    tests = 0
    incremental = 'no'
    if free < floor:
        try:
            with directory('target') as target, directory('debug', target) as debug, directory('deps', debug) as deps:
                tests = trim_test_executables(deps)
        except (FileNotFoundError, NotADirectoryError):
            pass
        if tests:
            free = free_after_removal(floor)
    if free < floor:
        # Serialize this scan with publication of a new lease. A keeper may
        # release its lease during cleanup; retaining its copy is safe.
        with (QUEUE / 'eviction-lock').open('a') as guard:
            fcntl.flock(guard, fcntl.LOCK_EX)
            leases = [description.rsplit(' since ', 1)[0] for description in waiting_landings().values()
                      if description.startswith('lease ')]
            candidates = []
            for name in os.listdir(root):
                if name == current or any(lease.endswith(' ' + name) for lease in leases):
                    continue
                try:
                    with directory(name, root) as copy, directory('server-rs', copy) as workspace:
                        with directory('target', workspace) as target:
                            identity = os.fstat(target)
                        age = last_build(name, copy, workspace)
                        if age is not None:
                            candidates.append((age, name, identity))
                except (FileNotFoundError, NotADirectoryError, PermissionError):
                    continue
            for _, name, identity in sorted(candidates):
                try:
                    with directory(name, root) as copy, directory('server-rs', copy) as workspace:
                        with directory('target', workspace) as target:
                            if not os.path.samestat(os.fstat(target), identity):
                                continue
                        shutil.rmtree('target', dir_fd=workspace)
                except (FileNotFoundError, NotADirectoryError):
                    continue
                removed.append(quote(name, safe=''))
                free = free_after_removal(floor)
                if free >= floor:
                    break
    if free < floor:
        try:
            with directory('target') as target, directory('debug', target) as debug:
                with directory('incremental', debug):
                    shutil.rmtree('incremental', dir_fd=debug)
                incremental = 'yes'
                free = free_after_removal(floor)
        except (FileNotFoundError, NotADirectoryError):
            pass
    verdict = ('ready' if free >= floor else
               f'Build stopped before cargo because less than {floor} bytes are free; {floor - free} more bytes needed')
    print(f"Build disk: for {quote(current, safe='')}, superseded tests={tests}, "
          f"removed targets={','.join(removed) or 'none'}, incremental removed={incremental}, "
          f"free={free} bytes, {verdict}.", file=sys.stdout if free >= floor else sys.stderr, flush=True)
    return free >= floor


def prepare_debug(workspace, profile):
    """Keep one dev/test profile generation in this target. The caller holds the slot."""
    mark = '.clarp-debug-profile'
    try:
        os.mkdir('target', dir_fd=workspace)
    except FileExistsError:
        pass
    with directory('target', workspace) as target:
        wanted = json.dumps(profile).encode() + b'\n'
        try:
            fd = os.open(mark, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=target)
        except FileNotFoundError:
            pass
        else:
            with os.fdopen(fd, 'rb') as previous:
                if stat.S_ISREG(os.fstat(fd).st_mode) and previous.read(len(wanted) + 1) == wanted:
                    return False
        try:
            with directory('debug', target):
                shutil.rmtree('debug', dir_fd=target)
            cleared = True
        except FileNotFoundError:
            cleared = False
        # A partial write must never bless a generation, even if the build was interrupted.
        temp = f'{mark}.{os.getpid()}.{time.monotonic_ns()}'
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=target)
        try:
            with os.fdopen(fd, 'wb') as writing:
                writing.write(wanted)
            os.replace(temp, mark, src_dir_fd=target, dst_dir_fd=target)
        finally:
            try:
                os.unlink(temp, dir_fd=target)
            except FileNotFoundError:
                pass
        return cleared


@contextmanager
def build_space(floor, debug=False):
    if floor is None:  # repo-gates does not produce a Cargo target
        yield True
        return
    cwd = Path.cwd()
    if cwd.name != 'server-rs' or cwd.parent.parent != QUEUE:
        raise SystemExit('Build workspace is outside the managed build directory.')
    with directory(QUEUE) as root, directory(cwd.parent.name, root) as copy:
        with directory('server-rs', copy) as workspace:
            if not os.path.samestat(os.fstat(workspace), os.stat('.')):
                raise SystemExit('Build workspace changed during admission.')
            if debug:
                profile = [os.environ[k] for k in ('CARGO_PROFILE_DEV_DEBUG', 'CARGO_PROFILE_TEST_DEBUG')]
                if prepare_debug(workspace, profile):
                    print('Build profile changed: cleared target/debug.', flush=True)
                    free_after_removal(floor)
        if not make_room(root, cwd.parent.name, floor):
            yield False
            return
        try:
            yield True
        finally:
            # Replace rather than touch an existing name: even an unexpected
            # hard link must not change another file's timestamp.
            temp = f'{STAMP}.{os.getpid()}'
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=copy)
            os.close(fd)
            os.replace(temp, STAMP, src_dir_fd=copy, dst_dir_fd=copy)


def run(lock, who, command, floor=None, debug=False):
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
        with build_space(floor, debug) as ready:
            if not ready:
                return 1
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
        elif sys.argv[1] in ('--build', '--build-debug'):
            sys.exit(run(slot.fileno(), sys.argv[3], sys.argv[4:], int(sys.argv[2]), sys.argv[1] == '--build-debug'))
        else:
            sys.exit(run(slot.fileno(), sys.argv[1], sys.argv[2:]))
