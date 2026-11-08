"""Closed synthetic main-file binding/copy; source only, NOT EXECUTED.

Reaping below certifies ONLY the actual Popen root. A whole native capsule also
needs the separately source-bound initial guard/terminal/raw-table validation.
No generic all-descendant/drain or remote backend claim is made. Unknown or
partial outputs are preserved; these helpers never remove/quarantine by deletion.
"""
import hashlib
import os
from pathlib import Path
import stat
import subprocess

from .raw_products import capture, exact_diff, private_database

MAX_DB_BYTES = 512 * 1024 * 1024


def main_binding(path):
    """Streaming physical identity/hash/version, no SQLite open or checkpoint."""
    path = Path(path)
    if not path.is_absolute() or path.is_symlink() or path.resolve(strict=True) != path:
        raise ValueError('main file must be exact plain physical path')
    def facts(value):
        return [value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns]
    def wal_empty():
        wal = Path(str(path) + '-wal')
        try:
            value = wal.lstat()
        except FileNotFoundError:
            return
        if not stat.S_ISREG(value.st_mode) or value.st_nlink != 1 or value.st_size != 0:
            raise ValueError('WAL is linked/nonregular or has uncheckpointed bytes')
    wal_empty()
    digest = hashlib.sha256()
    count, header = 0, b''
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        first = os.fstat(descriptor)
        if (not stat.S_ISREG(first.st_mode) or first.st_nlink != 1
                or first.st_size < 100 or first.st_size > MAX_DB_BYTES):
            raise ValueError('main-file type/link/size unknown')
        while raw := os.read(descriptor, 1024 * 1024):
            if not header:
                header = raw[:100]
            count += len(raw)
            if count > MAX_DB_BYTES:
                raise ValueError('main-file grew beyond bound')
            digest.update(raw)
        last = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if facts(first) != facts(last) or facts(last) != facts(path.stat()) or count != last.st_size:
        raise ValueError('main-file changed during streaming binding')
    wal_empty()
    if header[:16] != b'SQLite format 3\0':
        raise ValueError('unknown SQLite main header')
    return {'path': str(path), 'identity': facts(last), 'bytes': count,
            'sha256': digest.hexdigest(), 'WAL_tail_bytes': 0,
            'header_versions': {'schema_version': int.from_bytes(header[40:44], 'big'),
                                'user_version': int.from_bytes(header[60:64], 'big')},
            'scope': 'physical main-file only; not logical integrity or process drain'}


def require_root_reaped(child, *, identity, expected_argv):
    if not isinstance(child, subprocess.Popen) or child.pid != identity['pid'] or child.args != expected_argv:
        raise ValueError('owned actual Popen/identity/argv mismatch')
    try:
        code = child.wait(timeout=0)
    except subprocess.TimeoutExpired as error:
        raise ValueError('producer still running/unknown; preserve roots, do not copy') from error
    if type(code) is not int or code != 0 or child.returncode != code:
        raise ValueError('producer did not exit successfully; no fixture')
    # The Popen object and its birth binding must have been retained by the
    # qualified owner at launch. This cannot establish birth after the fact.
    if type(identity['start_ticks']) is not int or identity['start_ticks'] <= 0 or not identity['boot_id']:
        raise ValueError('missing launch birth/boot binding')
    return {'pid': child.pid, 'start_ticks': identity['start_ticks'], 'boot_id': identity['boot_id'],
            'actual_Popen_wait_exit': code, 'scope': 'root reaped ONLY; guard-based child exclusion separately required'}


def qualify_initial(child, *, expected_argv, launch_identity, expected, terminal, root, guard_failure_file):
    """Source/attempt/guard/raw snapshot and real root-reap prerequisite.

    Terminal bytes/hash/publication/launch ownership are outer-runner duties.
    A caller's True flag or plain JSON exit code is not used instead of wait().
    """
    root_reaped = require_root_reaped(child, identity=launch_identity, expected_argv=expected_argv)
    if terminal['producer'] != launch_identity or any(terminal[key] != expected[key]
            for key in ('run_id', 'pin', 'mode', 'source_manifest_sha256', 'guard_failure_file')):
        raise ValueError('initial native terminal source/attempt/birth mismatch')
    if terminal['mode'] != 'initial_seed' or terminal['checkpoint'] != [0, 0, 0]:
        raise ValueError('no successful native INITIAL checkpoint')
    counts = terminal['guard_counts']
    if (str(guard_failure_file) != terminal['guard_failure_file']
            or set(counts) - {'qualified_usage_attempt', 'native_usage_enoent'}
            or any(type(value) is not int or value < 0 for value in counts.values())
            or counts.get('qualified_usage_attempt', 0) != counts.get('native_usage_enoent', 0)
            or Path(guard_failure_file).exists() or Path(guard_failure_file).is_symlink()):
        raise ValueError('native guard failure/returned child/uncertain ENOENT; preserve roots')
    path = private_database(Path(root) / 'state.sqlite', root)
    first = main_binding(path)
    if first['header_versions'] != terminal['native_versions']:
        raise ValueError('native checkpoint/version header mismatch')
    # Also catch logical writes after the constructor snapshot, even when they
    # were flushed/checkpointed before process exit. No table or clock masks.
    closed_products = capture(path, owned_root=root)
    diff = exact_diff(terminal['products'], closed_products)
    if diff['changes']:
        raise ValueError('logical seed changed after constructor snapshot; no usable fixture')
    if main_binding(path) != first:
        raise ValueError('closed seed changed during raw validation')
    return {'root_reaped': root_reaped, 'closed_main': first,
            'closed_products_sha256': diff['after_products_sha256'],
            'logical_changes_after_constructor': 0,
            'scope': 'initial fixture only, with qualified pinned guard; not measured root reaping'}


def physical_copy(source, destination, *, expected_binding):
    """Exclusive full byte copy; no hardlink, schema patch, cleanup or retry."""
    source, destination = Path(source), Path(destination)
    if (not destination.is_absolute() or destination.is_symlink() or destination.exists()
            or destination.parent.resolve(strict=True) != destination.parent):
        raise ValueError('new destination not exact/private/exclusive')
    before = main_binding(source)
    if before != expected_binding:
        raise ValueError('closed source changed before copy')
    output_fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        input_fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            info = os.fstat(input_fd)
            if [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns] != before['identity']:
                raise ValueError('closed source replaced before copy stream')
            copied_bytes = 0
            while raw := os.read(input_fd, 1024 * 1024):
                copied_bytes += len(raw)
                if copied_bytes > before['bytes']:
                    raise ValueError('closed source grew; preserve partial copy')
                remaining = memoryview(raw)
                while remaining:
                    written = os.write(output_fd, remaining)
                    if written <= 0:
                        raise OSError('copy short/zero write')
                    remaining = remaining[written:]
        finally:
            os.close(input_fd)
        os.fsync(output_fd)
    finally:
        os.close(output_fd)
    copied = main_binding(destination)
    if (main_binding(source) != before
            or any(copied[key] != before[key] for key in ('sha256', 'bytes', 'header_versions'))
            or copied['identity'][:2] == before['identity'][:2]):
        raise ValueError('copy unqualified; PRESERVE source and partial destination, do not retry')
    directory = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    return {'source': before, 'physical_copy': copied, 'same_bytes_distinct_inode': True,
            'scope': 'closed main-file only; private ancillary/corpus restore remains separate'}
