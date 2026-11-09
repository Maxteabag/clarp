"""Early address-index publication SOURCE, UNEXECUTED, not final acceptance.

Owns files/known FDs only. No process/source/closed-writer authentication;
actual parent/collector terminal custody and final accepted index remain owed.
"""
import hashlib
import json
import os
from pathlib import Path
import socket
import stat

from .coverage_copy_read import _root, _retain, _same_capture, CHUNK, MAX_INPUT_BYTES
from .coverage_originals import _inventory, recheck_originals
from .coverage_decode_originals import collect_decoded_originals
from .coverage_graph_wire import _check
from .coverage_observe_file import observe_file
from .coverage_receipt_decode import decode_json, check_json_limits
from .coverage_receipt_index import FORMAT
from .suite_evidence import ROLES

NAME = 'index.json'


def _identity(root):
    info = _root(root).stat()
    return info.st_dev, info.st_ino, info.st_uid, info.st_mode


def _publish_index(*, root, index, index_limits, json_limits, path_bytes_limit):
    """Private fixed-name FS owner. Supplied records are STORAGE tier only."""
    if socket.gethostname() == 'laptopstudio':
        raise RuntimeError('BUILDBOX_ONLY; no index/control authority')
    check_json_limits(json_limits)
    costs = _check(index, index_limits)
    if (index_limits['wire_bytes'] > min(MAX_INPUT_BYTES, json_limits['bytes'])
            or costs['conservative_ASCII_wire_bytes'] + 1 > index_limits['wire_bytes']):
        raise ValueError('index pre-encode/decoder byte ceilings differ')
    root = _root(root)
    if (type(path_bytes_limit) is not int or path_bytes_limit <= 0
            or len(str(root / NAME).encode('utf-8')) > path_bytes_limit):
        raise ValueError('bounded index destination path required before output effects')
    _inventory(root, set(), entry_limit=1, metadata_bytes=1)
    identity = _identity(root)
    raw = (json.dumps(index, sort_keys=True, ensure_ascii=True, allow_nan=False,
                      separators=(',', ':')) + '\n').encode('ascii')
    if len(raw) > index_limits['wire_bytes']:
        raise ValueError('index encoded length exceeded conservative bound')
    directory_fd, file_fd, primary = None, None, None
    try:
        directory_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
        info = os.fstat(directory_fd)
        if ((info.st_dev, info.st_ino, info.st_uid, info.st_mode) != identity
                or not stat.S_ISDIR(info.st_mode)):
            raise ValueError('index output directory descriptor changed')
        file_fd = os.open(NAME, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                          0o600, dir_fd=directory_fd)
        info = os.fstat(file_fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_uid != os.geteuid() or info.st_mode & 0o077):
            raise ValueError('exclusive owned index file required')
        written = 0
        while written < len(raw):
            chunk = raw[written:written + CHUNK]
            if os.write(file_fd, chunk) != len(chunk):
                raise OSError('short index write; preserve partial/no retry')
            written += len(chunk)
        os.fsync(file_fd)
        os.fsync(directory_fd)
    except BaseException as error:
        primary = error
    finally:
        for fd in (file_fd, directory_fd):
            if fd is not None:
                try:
                    os.close(fd)  # every known FD independently once
                except BaseException as cleanup:
                    primary = _retain(primary, cleanup)
    if primary is not None:
        raise primary  # no optional reopen/hash/decode/index return or next phase
    if _identity(root) != identity:
        raise ValueError('index output root changed')
    actual = observe_file(str(root / NAME), byte_limit=index_limits['wire_bytes'],
                          path_bytes_limit=path_bytes_limit, retain_bytes=True)
    if (actual['raw'] != raw or actual['observation']['sha256'] != hashlib.sha256(raw).hexdigest()
            or not _same_capture(decode_json(actual['raw'], limits=json_limits), index)):
        raise ValueError('independent closed index bytes/full typed structure differ')
    _inventory(root, {NAME}, entry_limit=1, metadata_bytes=1)
    if _identity(root) != identity:
        raise ValueError('index root changed after independent reopen')
    return actual['observation']


def publish_original_index(*, paths, receipt_root, index_root, limits, json_limits,
                          index_limits, actual_exit, outer_seconds, cohort):
    # The actual collector owns nonzero-primary short circuit before optional
    # operands; no usable index accompanies a failed original pytest invocation.
    if type(actual_exit) is int and actual_exit != 0:
        return collect_decoded_originals(paths=paths, receipt_root=receipt_root, limits=limits,
                                         json_limits=json_limits, actual_exit=actual_exit,
                                         outer_seconds=outer_seconds, cohort=cohort)
    if type(actual_exit) is not int:
        raise ValueError('exact raw outer pytest exit required')
    check_json_limits(json_limits)
    _check(None, index_limits)
    if index_limits['wire_bytes'] > min(MAX_INPUT_BYTES, json_limits['bytes']):
        raise ValueError('index byte ceiling exceeds decoder/refusal ceiling')
    root = _root(index_root)
    _inventory(root, set(), entry_limit=1, metadata_bytes=1)
    identity = _identity(root)
    # Dedicated output: adding index MUST NOT mutate any original inventory.
    for text in paths.values():
        parent = _root(Path(text).parent)
        if root == parent or root.is_relative_to(parent) or parent.is_relative_to(root):
            raise ValueError('index output overlaps an original custody root')
    result = collect_decoded_originals(paths=paths, receipt_root=receipt_root, limits=limits,
                                      json_limits=json_limits, actual_exit=actual_exit,
                                      outer_seconds=outer_seconds, cohort=cohort)
    originals, decoded = result['originals'], result['decoded']
    recheck_originals(originals, limits=limits)
    index = {'format': FORMAT, 'pin': decoded['config']['pin'],
             'run_id': decoded['config']['run_id'],
             'config_sha256': originals['observations']['config']['sha256'],
             'roles': {r: {p: originals['observations'][r + '.' + p] for p in ('start', 'finish')}
                       for r in ROLES}}
    observation = _publish_index(root=root, index=index, index_limits=index_limits,
                                  json_limits=json_limits, path_bytes_limit=limits['path_bytes'])
    recheck_originals(originals, limits=limits)
    if _identity(root) != identity:
        raise ValueError('index root changed after required input cleanup/rechecks')
    actual = observe_file(observation['path'], byte_limit=index_limits['wire_bytes'],
                          path_bytes_limit=limits['path_bytes'], retain_bytes=False)
    if not _same_capture(actual['observation'], observation):
        raise ValueError('index changed after original input rechecks')
    _inventory(root, {NAME}, entry_limit=1, metadata_bytes=1)
    if _identity(root) != identity:
        raise ValueError('index output namespace changed at final boundary')
    return {'collection': result, 'index_observation': observation,
            'qualification': 'EARLY_ADDRESS_INDEX_NOT_ACCEPTED_COLLECTOR_TERMINAL_OR_FINAL_INDEX',
            'source_process_closed_writer_or_measurement_accepted': False}
