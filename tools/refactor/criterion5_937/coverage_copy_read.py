"""Strict private-copy API-read component: SOURCE ONLY, UNEXECUTED.

No coverage import or SQL decoder. The qualified outer owner supplies the exact
pinned public class and closed-producer source artifact. These checks do not
establish producer/source authenticity, valid empty data, union or readiness.
"""
import hashlib
import os
from pathlib import Path
import stat

MAX_INPUT_BYTES = 512 * 1024 * 1024  # refusal ceiling, NOT expected size or fit
CHUNK = 1024 * 1024


def _root(value):
    p = Path(value)
    if (not p.is_absolute() or p.is_symlink() or p.resolve(strict=True) != p
            or not p.is_dir()):
        raise ValueError('exact physical private root required')
    s = p.stat()
    if s.st_uid != os.geteuid() or s.st_mode & 0o077:
        raise ValueError('owned private root required; no permission repair')
    return p


def _snapshot(path):
    # The outer owner must hold exclusive closed-producer/root custody across
    # this operation. Path/FD snapshots are not an adversarial namespace fence.
    if any(os.path.lexists(str(path) + suffix) for suffix in ('-wal', '-shm', '-journal')):
        raise ValueError('unresolved coverage sidecar; preserve/PARTIAL')
    before = path.lstat()
    if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
            or before.st_uid != os.geteuid() or not 0 < before.st_size <= MAX_INPUT_BYTES):
        raise ValueError('missing/aliased/empty/oversized regular artifact')
    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as stream:
        opened = os.fstat(stream.fileno())
        if (opened.st_dev, opened.st_ino, opened.st_size) != (before.st_dev, before.st_ino, before.st_size):
            raise ValueError('artifact changed while opening')
        digest, count = hashlib.sha256(), 0
        while True:
            raw = stream.read(min(CHUNK, before.st_size - count + 1))
            if not raw:
                break
            count += len(raw)
            if count > before.st_size:
                raise ValueError('artifact grew during snapshot')
            digest.update(raw)
        after_fd = os.fstat(stream.fileno())
    after = path.lstat()
    fields = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns', 'st_nlink')
    if (count != before.st_size or any(getattr(row, k) != getattr(before, k)
                                     for row in (opened, after_fd, after) for k in fields)):
        raise ValueError('artifact changed during snapshot')
    if any(os.path.lexists(str(path) + suffix) for suffix in ('-wal', '-shm', '-journal')):
        raise ValueError('sidecar appeared during snapshot')
    return {'dev': before.st_dev, 'ino': before.st_ino, 'bytes': count,
            'mtime_ns': before.st_mtime_ns, 'ctime_ns': before.st_ctime_ns,
            'sha256': digest.hexdigest()}


def _retain(primary, secondary):
    if primary is None:
        return secondary
    if isinstance(primary, Exception) and not isinstance(secondary, Exception):
        # Export cancellation outside except Exception, while retaining BOTH
        # original objects and the read primary's untouched cause chain.
        return BaseExceptionGroup('coverage read failure and cleanup control', [primary, secondary])
    if secondary is not primary and primary.__cause__ is not secondary:
        prior = primary.__cause__
        primary.__cause__ = secondary if prior is None else BaseExceptionGroup(
            'coverage API cleanup failures', [prior, secondary])
    return primary


def _inspect_closed_copy(*, original_root, copy_root, filename, expected_sha256,
                         expected_bytes, coverage_data_class, mode, limits=None):
    """One public read/close on a private copy; no valid-empty/union verdict.

    Class/interpreter/source/producer-exit/closed-file custody must be qualified
    externally. An unchanged copy is necessary, not sufficient, data evidence.
    Every API connection for this component is owned by the supplied instance;
    outer containment covers constructor-return/assignment and unknown close.
    """
    if mode not in ('read', 'capture'):
        raise ValueError('unknown closed-copy operation')
    if mode == 'capture':
        # Tooling import/limit validation before any file/API effect. No
        # product or coverage import. Limits still need numeric qualification.
        from .coverage_sets import capture_sets, check_limits
        check_limits(limits)
    import socket
    if socket.gethostname() == 'laptopstudio':
        raise RuntimeError('BUILDBOX_ONLY; no local control or API execution authority')
    a, b = _root(original_root), _root(copy_root)
    if a == b or a.is_relative_to(b) or b.is_relative_to(a):
        raise ValueError('original/copy roots must be separate')
    if (type(filename) is not str or not filename or Path(filename).name != filename
            or filename in ('.', '..') or '\x00' in filename):
        raise ValueError('one literal artifact basename required')
    if (type(expected_bytes) is not int or not 0 < expected_bytes <= MAX_INPUT_BYTES
            or type(expected_sha256) is not str or len(expected_sha256) != 64
            or any(c not in '0123456789abcdef' for c in expected_sha256)):
        raise ValueError('exact source receipt byte/hash binding required')
    original, copy = a / filename, b / filename
    pre_a, pre_b = _snapshot(original), _snapshot(copy)
    for row in (pre_a, pre_b):
        if row['bytes'] != expected_bytes or row['sha256'] != expected_sha256:
            raise ValueError('artifact does not match one immutable source receipt')
    if (pre_a['dev'], pre_a['ino']) == (pre_b['dev'], pre_b['ino']):
        raise ValueError('API copy aliases original')
    # This is a public API construction, not source authentication. No method
    # substitution, private schema decoder, initialization/repair or fallback.
    data = coverage_data_class(basename=str(copy))
    primary, sets = None, None
    try:
        # Recheck after constructor and BEFORE read: missing read silently
        # succeeds in the pinned API. Constructor work cannot bypass custody.
        if _snapshot(copy) != pre_b or _snapshot(original) != pre_a:
            raise ValueError('artifact changed before public read')
        data.read()
        if mode == 'capture':
            # Same exact object and copy version; no detached later accessor
            # call authorized by an earlier standalone read/close receipt.
            sets = capture_sets(data, limits=limits)
    except BaseException as error:
        primary = error
    finally:
        try:
            data.close()  # ONCE; never retry an unknown/failed connection close
        except BaseException as cleanup:
            primary = _retain(primary, cleanup)
    if primary is not None:
        # Required API cleanup attempted. No optional hash/report/next input on
        # control/error; outer owner preserves roots and any unknown mutation.
        raise primary
    post_b, post_a = _snapshot(copy), _snapshot(original)
    if post_b != pre_b or post_a != pre_a:
        raise ValueError('public read mutated/reinitialized data; preserve/PARTIAL')
    receipt = {'public_read_returned': True, 'public_close_returned': True,
            'copy_bytes_unchanged': True, 'original_bytes_unchanged': True,
            'artifact_sha256': expected_sha256, 'artifact_bytes': expected_bytes,
            'schema_valid_empty_union_or_producer_accepted': False,
            'scope': 'one strict same-version copy read/accessor/close only; no update/report/process/drain proof'}

    if mode == 'capture':
        return {'read_close_receipt': receipt, 'accessor_sets': sets,
                'qualification': 'SAME_VERSION_COPY_API_CAPTURE_ONLY_NOT_INPUT_ACCEPTANCE',
                'source_producer_schema_valid_empty_or_union_accepted': False}
    return receipt


def read_closed_copy(*, original_root, copy_root, filename, expected_sha256,
                     expected_bytes, coverage_data_class):
    return _inspect_closed_copy(original_root=original_root, copy_root=copy_root, filename=filename,
                                expected_sha256=expected_sha256, expected_bytes=expected_bytes,
                                coverage_data_class=coverage_data_class, mode='read')


def capture_closed_copy(*, original_root, copy_root, filename, expected_sha256,
                        expected_bytes, coverage_data_class, limits):
    """All accessor operations INSIDE one fresh read/version/close envelope.

    No class/producer/input/schema acceptance or nine-input union is implied.
    No open library object escapes to authorize later update/report calls.
    """
    return _inspect_closed_copy(original_root=original_root, copy_root=copy_root, filename=filename,
                                expected_sha256=expected_sha256, expected_bytes=expected_bytes,
                                coverage_data_class=coverage_data_class, mode='capture', limits=limits)
