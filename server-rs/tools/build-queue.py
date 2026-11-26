#!/usr/bin/env python3
"""Run one job of a checkout in its copy on the build machine, and own the copy from publishing its sources until the
command's remaining children are gone and what the caller fetches is set aside. Jobs of different checkouts run side
by side; one checkout's jobs take turns in its copy."""
from collections import Counter
from contextlib import contextmanager, nullcontext
import ctypes
import datetime
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from urllib.parse import quote

LEGACY_ROOT = Path('/var/tmp/clarp-build')
# This version's copies live in a directory of their own on the same volume, where the older tools'
# housekeeping (eviction, seeding, drop) cannot reach them: it takes every entry of LEGACY_ROOT with a
# server-rs/ in it for a copy, and .v2 has none. So no new build ever waits for, or runs into, the older tools.
ROOT = LEGACY_ROOT / '.v2'
COPY_LOCKS = ROOT / '.copy-locks'
# Each invocation's upload of a checkout, kept apart from the copy, and from every other invocation's, so that no
# upload, drop or fetch from any machine, nor a later one from the same machine after its client was lost, can
# change the sources a job compiles or what it sets aside: a job publishes its stage into the copy while it holds
# the copy, and holds its stage for as long as it lives (new_stage, job).
STAGES = ROOT / '.stage'
# <checkout>@<machine>.<8 of mkdtemp's>; the machine as tools/build gives it, without dots.
STAGE = re.compile(r'([A-Za-z0-9_-][A-Za-z0-9._-]*)@([A-Za-z0-9-]+)\.([A-Za-z0-9_]{8})')
LEGACY_SLOT = LEGACY_ROOT / 'lock'
LEGACY_HOLDER = LEGACY_ROOT / 'holder'
LEGACY_EVICTION = LEGACY_ROOT / 'eviction-lock'
CHILDREN = Path(f'/proc/self/task/{os.getpid()}/children')


def executable(pid):
    try:
        name = Path(os.readlink(f'/proc/{pid}/exe')).name
        return name if name.isprintable() else '<unnamed>'
    except OSError:
        return '<exited>'


def place_in(path):
    if path.is_relative_to(LEGACY_ROOT):
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


def recorded_holder(identity, path):
    # Some filesystems report a different device number in /proc/locks
    # than in stat. Ask the recorded owner's actual descriptor instead.
    fields = identity.split(':')
    if len(fields) != 3 or not all(field.isdecimal() for field in fields):
        return False
    pid, start, fd = fields
    proc = Path('/proc') / pid
    try:
        if process_start(pid) != start or not (proc / 'fd' / fd).samefile(path):
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


def open_lock(path):
    # Not inherited (PEP 446): a build's leftover child never keeps a copy held.
    return os.open(path, os.O_RDONLY | os.O_CREAT | os.O_NOFOLLOW, 0o644)


@contextmanager
def held(path, mode=fcntl.LOCK_EX, wait=True):
    """Lock `path` (flock) for the block and yield True, or, with wait=False, yield False at once when another has it."""
    fd = open_lock(path)
    try:
        try:
            fcntl.flock(fd, mode if wait else mode | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True
    finally:
        os.close(fd)


def lock_of(name):
    """The lock of one checkout's copy, kept beside the copies so that a drop of the copy never removes it."""
    COPY_LOCKS.mkdir(exist_ok=True)
    return COPY_LOCKS / f'{name}.lock'


def holder_of(name):
    return COPY_LOCKS / f'{name}.holder'


def holding(name):
    """(started ns, description) of the job that holds this copy now, or None."""
    try:
        identity, started, description = holder_of(name).read_text().strip().split(' ', 2)
    except (FileNotFoundError, ValueError):
        return None
    if not started.isdecimal() or not recorded_holder(identity, COPY_LOCKS / f'{name}.lock'):
        return None
    return int(started), description


@contextmanager
def own_copy(name, description):
    """Hold this checkout's copy for one job: its sources, its target and its stamps. Another job of the same
    checkout waits here; jobs of other checkouts do not (Peter, 26 Nov 2026: no queue)."""
    fd = open_lock(lock_of(name))
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            other = holding(name)
            print(f"Build copy: waiting for this checkout's running job to finish ({other[1] if other else 'starting'}).",
                  flush=True)
            fcntl.flock(fd, fcntl.LOCK_EX)
        temporary = COPY_LOCKS / f'.{name}.holder.{os.getpid()}'
        temporary.write_text(f'{os.getpid()}:{process_start(os.getpid())}:{fd} {time.time_ns()} {description}\n')
        temporary.replace(holder_of(name))
        try:
            yield
        finally:
            # Still held here, so no later job's record is removed.
            holder_of(name).unlink(missing_ok=True)
    finally:
        os.close(fd)


@contextmanager
def older_tools_idle():
    """Yield True while no build or housekeeping of the tools before 26 Nov 2026 can run, so that a target of theirs
    can be cloned as it stands. Their builds hold LEGACY_SLOT for as long as they run, and their eviction, seeding and
    drop hold LEGACY_EVICTION; both are taken shared here and only without waiting. Their files are left as they are."""
    with held(LEGACY_SLOT, fcntl.LOCK_SH, wait=False) as no_build, \
            held(LEGACY_EVICTION, fcntl.LOCK_SH, wait=False) as no_housekeeping:
        yield no_build and no_housekeeping


def drive(checkout, command):
    """A local landing driver's lease: this checkout's lock on this machine, held while the driver runs, so its
    build commands go before any other job of this checkout started here. It reserves nothing on the build machine."""
    if not command or os.getenv('CLARP_BUILD_LEASE'):
        raise SystemExit('usage: build lease COMMAND [ARG ...] (one lease per driver)')
    with open(f'/var/tmp/clarp-build-{checkout}.lock', 'a') as local:
        fcntl.flock(local, fcntl.LOCK_EX)
        pid = os.getpid()
        env = dict(os.environ, CLARP_BUILD_LEASE=f'{pid}-{process_start(pid)}', CLARP_BUILD_LEASE_CHECKOUT=checkout)
        result = subprocess.run(command, env=env).returncode
        return result if result >= 0 else 128 - result


def status():
    """The builds running now, by checkout, without taking any lock."""
    now = time.time_ns()
    running = []
    try:
        names = sorted(entry.removesuffix('.holder') for entry in os.listdir(COPY_LOCKS) if entry.endswith('.holder')
                       and not entry.startswith('.'))
    except FileNotFoundError:
        names = []
    for name in names:
        job = holding(name)
        if job:
            running.append((job[0], job[1]))
    for started, description in sorted(running):
        print(f'building: {description} (for {waited(now - started)})')
    try:
        identity, _, description = LEGACY_HOLDER.read_text().strip().partition(' ')
    except FileNotFoundError:
        identity, description = '', ''
    old = bool(description) and recorded_holder(identity, LEGACY_SLOT)
    if old:
        print(f'building with older tools: {description}')
    if not running and not old:
        print('builds: none running')
    waiting = 0
    for marker in LEGACY_ROOT.glob('wait-*-*-*'):
        _, _, pid, start = marker.name.split('-', 3)
        waiting += process_start(pid) == start
    if waiting:
        print(f'waiting in the older tools\' line: {waiting} (they take turns among themselves)')


def waited(ns):
    minutes, seconds = divmod(max(ns, 0) // 10**9, 60)
    hours, minutes = divmod(minutes, 60)
    return f'{hours}h{minutes:02d}m' if hours else f'{minutes}m{seconds:02d}s'


def cancelled(signum, frame):
    raise SystemExit(128 + signum)


# All removal is relative to open directories. Never resolve a checkout, its
# workspace or its target through a symlink.
DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
STAMP = '.clarp-last-build'
# A second name of the stamp's file, made when the build's command exited 0. Each build replaces
# the stamp with a new file, so the two are one file exactly while the last build succeeded.
GOOD = '.clarp-last-good-build'


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


def evictable(root, name):
    """(last build's time, target's stat) of another checkout's copy whose target may be removed, else None."""
    try:
        with directory(name, root) as copy, directory('server-rs', copy) as workspace:
            with directory('target', workspace) as target:
                identity = os.fstat(target)
            age = last_build(name, copy, workspace)
    except (FileNotFoundError, NotADirectoryError, PermissionError):
        return None
    return None if age is None else (age, identity)


def make_room(root, current, floor):
    # Free space is the filesystem's own count (statvfs, as df reports it), never a sum of du:
    # a target cloned from another (seed_target) shares most of its data with it, and du counts
    # that data in full for each, so du overstates what a seeded checkout costs and what
    # removing its target frees. Removal is followed by the filesystem's count again.
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
    busy = 'none'
    if free < floor:
        candidates = []
        for name in os.listdir(root):
            if name != current and not name.startswith('.') and (built := evictable(root, name)) is not None:
                candidates.append((*built, name))
        kept = []
        for age, identity, name in sorted(candidates, key=lambda c: (c[0], c[2])):
            # A copy whose own job or drop has it is in use: it is left, never waited for. Under its lock the
            # target must still be the one found, and not built since, to be removed.
            with held(lock_of(name), wait=False) as idle:
                if not idle:
                    kept.append(quote(name, safe=''))
                    continue
                now = evictable(root, name)
                if now is None or now[0] != age or not os.path.samestat(now[1], identity):
                    continue
                try:
                    with directory(name, root) as copy, directory('server-rs', copy) as workspace:
                        shutil.rmtree('target', dir_fd=workspace)
                except (FileNotFoundError, NotADirectoryError):
                    continue
            removed.append(quote(name, safe=''))
            free = free_after_removal(floor)
            if free >= floor:
                break
        busy = ','.join(kept) or 'none'
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
          f"removed targets={','.join(removed) or 'none'}, targets in use kept={busy}, incremental removed={incremental}, "
          f"free={free} bytes, {verdict}.", file=sys.stdout if free >= floor else sys.stderr, flush=True)
    return free >= floor


SEED = '.target-seed'


def newer_than_the_clone(copy):
    """Give every file of this checkout's copy, but its target, the time of now.

    Cargo decides that a workspace crate is up to date when none of its sources is newer than its
    last build. In a cloned target that build is the other checkout's, and this checkout's
    sources may have arrived before it (while waiting for the slot): a crate that differs between
    the two would be passed over, and the build would hold the other checkout's code. Newer than
    any build in the clone, each workspace crate is built again from this checkout's sources.
    """
    for parent, dirs, files in os.walk(copy):
        if Path(parent) == copy / 'server-rs':
            dirs[:] = [d for d in dirs if d not in ('target', SEED)]
        for name in files:
            os.utime(os.path.join(parent, name), follow_symlinks=False)


def last_good_build(copy):
    """The time of this checkout's last build when that build succeeded, else None."""
    try:
        stamp = os.stat(STAMP, dir_fd=copy, follow_symlinks=False)
        good = os.stat(GOOD, dir_fd=copy, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(stamp.st_mode) or not os.path.samestat(stamp, good):
        return None
    return stamp.st_mtime_ns


def host_library_built(target):
    try:
        with directory('debug', target) as debug, directory('deps', debug) as deps:
            return any(name.startswith('libclarp_host-') and name.endswith('.rlib') for name in os.listdir(deps))
    except (FileNotFoundError, NotADirectoryError):
        return False


def seed_target(root, current, workspace):
    """Give a checkout without a target a clone of the target of another checkout whose last
    build succeeded: the latest of those that hold the Host's library built (so every dependency
    of it, not only their metadata, as after a check or clippy), or else the latest of them.

    The clone shares its data with the original (a reflink), so it costs the disk little, and the
    new checkout starts with every crates.io dependency already built: cargo builds only the
    workspace's crates again, once (see newer_than_the_clone).
    One target dir cannot be shared outright: cargo gives a workspace crate the same unit name in
    every checkout, so checkouts building in turn would rebuild each other's crates every time.
    Where the filesystem cannot clone, nothing is copied and the build starts cold: one file is
    cloned first to find out, and the target is cloned only with --reflink=always, never copied.
    The caller holds its own copy; the source is held shared while it is cloned (see below).
    Returns the source checkout's name as printed, or None.
    """
    try:
        os.stat('target', dir_fd=workspace, follow_symlinks=False)
        return None
    except FileNotFoundError:
        pass
    in_use = []
    # First the copies of this version, each held shared while it is cloned: two new checkouts may clone one
    # target at once, while its own job and a drop take it alone. A copy one of them has is left, never waited for.
    sources = [found for name in os.listdir(root)
               if name != current and not name.startswith('.') and (found := seed_source(root, name))]
    source = clone_first(root, sorted(sources, reverse=True), workspace, lambda name: held(lock_of(name), fcntl.LOCK_SH, wait=False), in_use)
    source = source and quote(source, safe='')
    if source is None and os.path.isdir(LEGACY_ROOT):
        # Then, during the move to .v2, the older tools' copies, this checkout's own first: only while none of their
        # builds or housekeeping runs, held off for the moment of the clone.
        with older_tools_idle() as idle, directory(LEGACY_ROOT) as legacy:
            if idle:
                sources = [found for name in os.listdir(legacy)
                           if not name.startswith('.') and (found := seed_source(legacy, name))]
                ordered = sorted(sources, key=lambda found: (found[2] == current, found[0], found[1]), reverse=True)
                source = clone_first(legacy, ordered, workspace, lambda name: nullcontext(True), in_use)
                source = source and f"{quote(source, safe='')} (older tools)"
            else:
                in_use.append('the older tools\' copies')
    if source:
        newer_than_the_clone(ROOT / current)
    elif in_use and source is None:
        print(f"Build target: the good targets are in use ({','.join(in_use)}); building cold.", flush=True)
    return source or None


def clone_first(root, sources, workspace, hold, in_use):
    """Clone the target of the first of `sources` that `hold` gives without waiting and that is still as it was
    found (a build since may have failed). Its name, None when none was cloned, or False when cloning failed."""
    for found in sources:
        name = found[2]
        with hold(name) as idle:
            if not idle:
                in_use.append(quote(name, safe=''))
                continue
            if seed_source(root, name) != found:
                continue
            cloned = clone_target(root, name, workspace)
            if cloned is not None and cloned.returncode == 0 and stat.S_ISDIR(os.stat(SEED, dir_fd=workspace, follow_symlinks=False).st_mode):
                os.rename(SEED, 'target', src_dir_fd=workspace, dst_dir_fd=workspace)
                return name
        try:
            shutil.rmtree(SEED, dir_fd=workspace)
        except FileNotFoundError:
            pass
        if cloned is None:  # the source went meanwhile
            continue
        detail = cloned.stderr.strip().splitlines()[-1:]
        print(f"Build target: could not clone {quote(name, safe='')}'s target ({' '.join(detail)}); building cold.", flush=True)
        return False
    return None


def seed_source(root, name):
    """(Host's library built, last good build's time, name) of a copy whose target may seed another, else None."""
    try:
        with directory(name, root) as copy, directory('server-rs', copy) as other:
            with directory('target', other) as target:
                built = host_library_built(target)
            good = last_good_build(copy)
            if good is not None and last_build(name, copy, other) is not None:
                return built, good, name
    except (FileNotFoundError, NotADirectoryError, PermissionError):
        pass
    return None


def clone_target(root, name, workspace):
    """Clone `name`'s target to SEED in this workspace. The cp's result, or None when the source is gone."""
    try:
        shutil.rmtree(SEED, dir_fd=workspace)
    except FileNotFoundError:
        pass
    try:
        with directory(name, root) as copy, directory('server-rs', copy) as other:
            # A file of this checkout's own, cloned beside it, tells whether the filesystem
            # clones at all; the checkouts and their targets are on that one filesystem.
            fd = os.open(SEED, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600, dir_fd=workspace)
            with os.fdopen(fd, 'w') as probe:
                probe.write('clone probe\n')
            cloned = subprocess.run(
                ['cp', '--reflink=always', f'/proc/self/fd/{workspace}/{SEED}', f'/proc/self/fd/{workspace}/{SEED}.clone'],
                pass_fds=(workspace,), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
            for probe in (SEED, f'{SEED}.clone'):
                try:
                    os.unlink(probe, dir_fd=workspace)
                except FileNotFoundError:
                    pass
            if cloned.returncode == 0:
                cloned = subprocess.run(
                    ['cp', '-a', '--reflink=always', f'/proc/self/fd/{other}/target', f'/proc/self/fd/{workspace}/{SEED}'],
                    pass_fds=(other, workspace), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
            return cloned
    except (FileNotFoundError, NotADirectoryError):
        return None


def prepare_debug(workspace, profile):
    """Keep one dev/test profile generation in this target. The caller holds its copy."""
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
def build_space(floor, debug=False, outcome=None):
    """Prepare this checkout's target and yield whether the build may run. The caller puts the
    command's exit code in `outcome` (a dict, under 'code'): only a 0 marks the build good.
    The caller holds the copy and works in its server-rs/."""
    cwd = Path.cwd()
    if cwd.name != 'server-rs' or cwd.parent.parent != ROOT:
        raise SystemExit('Build workspace is outside the managed build directory.')
    with directory(ROOT) as root, directory(cwd.parent.name, root) as copy:
        with directory('server-rs', copy) as workspace:
            if not os.path.samestat(os.fstat(workspace), os.stat('.')):
                raise SystemExit('Build workspace changed while its copy was being taken.')
            # From here the target changes: it seeds no other checkout until this build has ended with 0, also
            # when this build is killed before its stamp is written.
            try:
                os.unlink(GOOD, dir_fd=copy)
            except FileNotFoundError:
                pass
            started = time.monotonic()
            source = seed_target(root, cwd.parent.name, workspace)
            if source:
                print(f"Build target: cloned from {source} in {time.monotonic() - started:.1f} s.", flush=True)
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
            if outcome is not None and outcome.get('code') == 0:
                os.link(temp, f'{GOOD}.{os.getpid()}', src_dir_fd=copy, dst_dir_fd=copy)
                os.replace(f'{GOOD}.{os.getpid()}', GOOD, src_dir_fd=copy, dst_dir_fd=copy)
            os.replace(temp, STAMP, src_dir_fd=copy, dst_dir_fd=copy)


# What tools/build uploads to a stage, and so what a job publishes from it into its copy, with the same rules: the
# paths below the checkout's root, whether what is no longer there goes, and what is never touched in the copy.
PUBLISHED = (
    ('server-rs/', ['--delete', '--exclude', 'target/', '--exclude', 'conformance/out/', '--exclude', 'dist/']),
    ('contract/', ['--delete']),
    ('skills/clarp-sub-agents/', ['--delete', '--exclude', '__pycache__']),
    ('scripts/clarp-media-publish.py', []),
    ('skills/clarp-background-jobs/scripts/run_detached_job.sh', []),
)


def publish(stage, copy):
    """Make the copy's sources this job's staged ones. The caller holds the copy. What changed arrives with the time
    of its arrival and what did not keeps its time, as an upload into the copy did (see tools/build)."""
    for path, rules in PUBLISHED:
        # rsync's own meaning of a trailing slash: a directory's contents into the same directory of the copy, a
        # file into its directory there. (A Path would drop the slash.)
        into = path if path.endswith('/') else os.path.dirname(path) + '/'
        (copy / into).mkdir(parents=True, exist_ok=True)
        done = subprocess.run(['rsync', '-rlpD', '--checksum', *rules, f'{stage}/src/{path}', f'{copy}/{into}'])
        if done.returncode:
            raise SystemExit(f'Build copy: could not publish the staged {path} (rsync exit {done.returncode}).')


def supervised(command):
    """Run the command, and kill and reap whatever it leaves behind once it ends or this job is cancelled."""
    # PR_SET_CHILD_SUBREAPER adopts orphaned descendants, including children
    # which start their own process group or session.
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(36, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), 'cannot supervise build children')
    for sig in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, cancelled)

    @contextmanager
    def reaping():
        try:
            yield
        finally:
            for sig in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
                signal.signal(sig, signal.SIG_IGN)
            clean_children()
    return reaping()


def run(command):
    child = subprocess.Popen(command)
    result = child.wait()
    return result if result >= 0 else 128 - result


def stages_of(name, machine):
    """The stage directories of one checkout from one machine."""
    try:
        entries = os.listdir(STAGES)
    except FileNotFoundError:
        return []
    return [entry for entry in entries
            if (found := STAGE.fullmatch(entry)) and found[1] == name and found[2] == machine]


def new_stage(name, machine, bare=False):
    """Make this invocation's stage and print its path. Unless bare (fmt), it starts as a reflink copy of the sources
    of the newest earlier stage of this checkout and machine whose upload ended (its job marked it), so the upload
    sends only what changed. Earlier stages that no job holds any more (left by a client that went, or kept as the
    last basis) are removed; a stage made after this one, and one a job still holds, are not."""
    if not re.fullmatch(r'[A-Za-z0-9_-][A-Za-z0-9._-]*', name) or not re.fullmatch(r'[A-Za-z0-9-]+', machine):
        raise SystemExit('A stage needs a plain checkout name and machine.')
    STAGES.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f'{name}@{machine}.', dir=STAGES))
    os.chmod(stage, 0o755)
    os.close(open_lock(stage / '.lock'))  # its time orders the stages
    made = os.stat(stage / '.lock').st_mtime_ns
    earlier = []
    for entry in stages_of(name, machine):
        try:
            when = os.stat(STAGES / entry / '.lock').st_mtime_ns
        except FileNotFoundError:
            continue
        if entry != stage.name and when < made:
            earlier.append((when, entry))
    uploaded = [(when, entry) for when, entry in earlier if (STAGES / entry / 'uploaded').is_file()]
    basis = max(uploaded)[1] if uploaded else None
    if basis and not bare:
        # A finished upload is never written again: only its job reads it (publish) and writes its out/.
        copied = subprocess.run(['cp', '-a', '--reflink=auto', str(STAGES / basis / 'src'), str(stage / 'src')],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if copied.returncode:
            shutil.rmtree(stage / 'src', ignore_errors=True)
    for _, entry in earlier:
        if entry == basis:
            continue
        with held(STAGES / entry / '.lock', wait=False) as idle:
            if idle:
                shutil.rmtree(STAGES / entry, ignore_errors=True)
    print(stage, flush=True)


@contextmanager
def own_stage(stage, name):
    """Hold this invocation's stage for the job's whole life, also when its client is gone, so no later invocation
    removes it. A stage removed before the job held it (its client was gone by then) ends the job here."""
    found = STAGE.fullmatch(stage.name)
    if stage.parent != STAGES or not found or found[1] != name:
        raise SystemExit('Build job is outside the managed build directory.')
    try:
        fd = open_lock(stage / '.lock')
    except FileNotFoundError:
        raise SystemExit('Build stage was removed before its job began.') from None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        if os.fstat(fd).st_nlink == 0:
            raise SystemExit('Build stage was removed before its job began.')
        yield
    finally:
        os.close(fd)


def job(name, stage, who, command, floor, debug=False):
    """One job of a checkout: hold its stage and copy, publish the stage into the copy, build, and leave what the
    caller fetches (the lockfile, and a release's binaries) in the stage's out/, all before the copy is let go."""
    stage = Path(stage)
    if not name or name.startswith('.') or '/' in name:
        raise SystemExit('Build job is outside the managed build directory.')
    since = datetime.datetime.now().strftime('%H:%M:%S')
    reaping = supervised(command)
    with own_stage(stage, name), own_copy(name, f'{who} since {since}'):
        # The upload ended before this job began: the stage may now serve a later one as its basis.
        (stage / 'uploaded').touch()
        copy = ROOT / name
        out = stage / 'out'
        try:
            with reaping:
                shutil.rmtree(out, ignore_errors=True)
                out.mkdir()
                publish(stage, copy)
                os.chdir(copy / 'server-rs')
                outcome = {}
                with build_space(floor, debug, outcome) as ready:
                    if not ready:
                        return 1
                    outcome['code'] = run(command)
                return outcome['code']
        finally:
            # The lockfile is made by the first build; the caller brings it home from here, as this job left it.
            try:
                shutil.copyfile(copy / 'server-rs' / 'Cargo.lock', out / 'Cargo.lock')
            except FileNotFoundError:
                pass


def main(argv):
    if argv[0] == '--drive':
        return drive(argv[1], argv[2:])
    if argv[0] == 'status':
        return status()
    if argv[0] == 'stage':
        # stage [--bare] NAME MACHINE
        bare = argv[1] == '--bare'
        return new_stage(*argv[1 + bare:3 + bare], bare=bare)
    if argv[0] in ('--build', '--build-debug'):
        # --build[-debug] FLOOR NAME STAGE WHO COMMAND...
        ROOT.mkdir(parents=True, exist_ok=True)
        return job(argv[2], argv[3], argv[4], argv[5:], int(argv[1]), argv[0] == '--build-debug')
    if argv[0] == '--fmt':
        # --fmt NAME STAGE WHO COMMAND...: a job without a copy, in its stage's fmt/
        with own_stage(Path(argv[2]), argv[1]), supervised(argv[4:]):
            return run(argv[4:])
    raise SystemExit('usage: build-queue.py status | stage | --build[-debug] | --fmt | --drive')


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
