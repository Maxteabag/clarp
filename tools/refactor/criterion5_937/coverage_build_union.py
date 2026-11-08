"""Nine-input public update owner SOURCE, UNEXECUTED; no measurement launcher.

Actual class/ABI/full run/kernel/closed-producer/custody authentication is an
external prerequisite. Raw run/capture receipts must remain retained. No report,
denominator, coverage verdict, process/compute admission or automatic retry.
"""
import os
from pathlib import Path
import socket
import stat

from .coverage_copy_read import (_root, _snapshot, _retain, _same_capture, CHUNK,
                                 capture_closed_copy, update_closed_copy)
from .coverage_union import reference_union
from .suite_evidence import ROLES


def _verify_input(capture):
    receipt = capture['read_close_receipt']
    paths, versions = [], []
    for kind in ('original_observation', 'copy_observation'):
        observed = receipt[kind]
        path = Path(observed['path'])
        _root(path.parent)
        actual = _snapshot(path)
        if not _same_capture(actual, {k: v for k, v in observed.items() if k != 'path'}):
            raise ValueError('input no longer matches captured exact source version')
        paths.append(path)
        versions.append(actual)
    if paths[0].name != paths[1].name:
        raise ValueError('source/copy basenames differ; no path rewrite')
    return paths, versions


def _exclusive_copy(source, destination, expected):
    """Owned closed main-file copy only; preserve partials on every failure.

    No sidecar checkpoint, repair, hardlink or target API open. Exclusive root
    custody and constructor/FD-assignment containment remain outer obligations.
    """
    if not _same_capture(_snapshot(source), expected):
        raise ValueError('closed target changed before copying')
    source_fd, destination_fd, directory_fd, primary = None, None, None, None
    try:
        source_fd = os.open(source, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        opened = os.fstat(source_fd)
        if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                or opened.st_dev != expected['dev'] or opened.st_ino != expected['ino']
                or opened.st_size != expected['bytes'] or opened.st_uid != os.geteuid()
                or opened.st_mtime_ns != expected['mtime_ns'] or opened.st_ctime_ns != expected['ctime_ns']):
            raise ValueError('closed target descriptor binding changed')
        destination_fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                                 | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
        copied = 0
        while True:
            raw = os.read(source_fd, min(CHUNK, expected['bytes'] - copied + 1))
            if not raw:
                break
            if copied + len(raw) > expected['bytes']:
                raise ValueError('target grew during copy')
            if os.write(destination_fd, raw) != len(raw):
                raise OSError('short exclusive copy write; preserve/no retry')
            copied += len(raw)
        if copied != expected['bytes']:
            raise ValueError('short closed target read')
        os.fsync(destination_fd)
        directory_fd = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY
                                | os.O_CLOEXEC | os.O_NOFOLLOW)
        os.fsync(directory_fd)
    except BaseException as error:
        primary = error
    finally:
        # Attempt every KNOWN owned descriptor once. No numeric fallback or
        # retry after close error; ownership then UNKNOWN to the outer owner.
        for fd in (directory_fd, destination_fd, source_fd):
            if fd is not None:
                try:
                    os.close(fd)
                except BaseException as cleanup:
                    primary = _retain(primary, cleanup)
    if primary is not None:
        raise primary  # no optional verification/publication on pending control
    if not _same_capture(_snapshot(source), expected):
        raise ValueError('target changed during copy')
    result = _snapshot(destination)
    if (result['sha256'] != expected['sha256'] or result['bytes'] != expected['bytes']
            or (result['dev'], result['ino']) == (expected['dev'], expected['ino'])):
        raise ValueError('exclusive copy bytes or inode identity mismatch')
    return result


def build_union(config_bytes, starts, finishes, *, actual_exit, outer_seconds, cohort,
                captures, limits, aggregate_root, verify_root, coverage_data_class):
    if socket.gethostname() == 'laptopstudio':
        raise RuntimeError('BUILDBOX_ONLY; source only until exact admission')
    expected = reference_union(config_bytes, starts, finishes, actual_exit=actual_exit,
                               outer_seconds=outer_seconds, cohort=cohort,
                               captures=captures, limits=limits)
    aggregate_root, verify_root = _root(aggregate_root), _root(verify_root)
    if (aggregate_root == verify_root or aggregate_root.is_relative_to(verify_root)
            or verify_root.is_relative_to(aggregate_root)
            or any(aggregate_root.iterdir()) or any(verify_root.iterdir())):
        raise ValueError('fresh separate EMPTY aggregate/verification roots required')
    actual_identities = set()
    for role in ROLES:
        (original, copy), versions = _verify_input(captures[role])
        for path, observed in zip((original, copy), versions):
            for root in (aggregate_root, verify_root):
                if root == path.parent or root.is_relative_to(path.parent) or path.parent.is_relative_to(root):
                    raise ValueError('output root overlaps input custody root')
            identity = (observed['dev'], observed['ino'])
            if identity in actual_identities:
                raise ValueError('actual input/copy inode alias before target construction')
            actual_identities.add(identity)
    target = aggregate_root / 'coverage-aggregate.data'
    # This constructor is actually owned HERE, once, after all preflight. A
    # filename alone was not proof of fresh internal API object history.
    aggregate = coverage_data_class(basename=str(target))
    primary, previous, edges = None, None, []
    try:
        for role in ROLES:  # every role, even when hashes/sets are identical
            (original, copy), _ = _verify_input(captures[role])
            declared = captures[role]['read_close_receipt']
            edge = update_closed_copy(
                original_root=original.parent, copy_root=copy.parent, filename=original.name,
                expected_sha256=declared['artifact_sha256'], expected_bytes=declared['artifact_bytes'],
                coverage_data_class=coverage_data_class, limits=limits,
                expected_sets=captures[role]['accessor_sets'], aggregate=aggregate,
                aggregate_root=aggregate_root, aggregate_before=previous)
            for kind in ('original_observation', 'copy_observation'):
                if not _same_capture(edge['read_close_receipt'][kind], declared[kind]):
                    raise ValueError('edge observation changed from authenticated source version')
            previous = edge['aggregate_after_observation']
            if (previous['dev'], previous['ino']) in actual_identities:
                raise ValueError('target aliases any original/copy input')
            edges.append({'role': role, 'target_observation_before_outer_close': previous})
    except BaseException as error:
        primary = error
    finally:
        try:
            aggregate.close()  # ONE owner attempt on every outcome
        except BaseException as cleanup:
            primary = _retain(primary, cleanup)
    if primary is not None:
        raise primary  # no next input/copy/final accessor/report on pending failure
    final_version = _snapshot(target)
    if not _same_capture(final_version, previous):
        raise ValueError('target changed through required close; preserve/PARTIAL')
    for role in ROLES:
        _verify_input(captures[role])
    _exclusive_copy(target, verify_root / target.name, final_version)
    captured = capture_closed_copy(original_root=aggregate_root, copy_root=verify_root,
                                   filename=target.name, expected_sha256=final_version['sha256'],
                                   expected_bytes=final_version['bytes'], coverage_data_class=coverage_data_class,
                                   limits=limits)
    actual = captured['accessor_sets']
    if not _same_capture({k: actual[k] for k in ('mode', 'contexts', 'files')},
                         {k: expected[k] for k in ('mode', 'contexts', 'files')}):
        raise ValueError('actual closed aggregate differs from independent full reference union')
    return {'edges': edges, 'reference_union': expected, 'closed_aggregate_capture': captured,
            'target_closed_owner_returned': True,
            'qualification': 'NINE_PUBLIC_UPDATE_STORAGE_ONLY_NOT_MEASUREMENT_ACCEPTANCE',
            'actual_run_source_producer_schema_denominator_accepted': False,
            'scope': 'conditional API union only; no report, coverage verdict or process/drain proof'}
