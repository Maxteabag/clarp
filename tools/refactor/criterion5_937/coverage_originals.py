"""Early31 original-file inventory/observation SOURCE, UNEXECUTED.

No decoder, SQLite/API, index, native producer or writer-closure acceptance.
Qualified outer parent owns immutable namespace and positive writer closure.
"""
import os
from pathlib import Path
import socket
import stat

from .coverage_copy_read import _root, _retain, MAX_INPUT_BYTES
from .coverage_observe_file import observe_file
from .suite_evidence import ROLES

LABELS = ('catalog', 'archive', 'profile', 'config') + tuple(
    role + '.' + phase for role in ROLES for phase in ('start', 'finish', 'data'))
_FIELDS = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns', 'st_uid', 'st_nlink', 'st_mode')


def _epoch(info):
    return tuple(getattr(info, k) for k in _FIELDS)


def _inventory(root, expected, *, entry_limit, metadata_bytes):
    """Bound enumeration itself; preserve unexpected entries, no recursion/read."""
    iterator, primary, rows, count = None, None, {}, 0
    try:
        iterator = os.scandir(root)
        for entry in iterator:
            count += 1
            if count > entry_limit:
                raise ValueError('original directory inventory entry bound')
            if entry.name not in expected:
                raise ValueError('unexpected original entry or publication partial')
            info = entry.stat(follow_symlinks=False)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or info.st_uid != os.geteuid()):
                raise ValueError('original inventory kind/owner/alias')
            if entry.name == 'owner.json' and not 0 < info.st_size <= metadata_bytes:
                raise ValueError('owner metadata size bound')
            rows[entry.name] = _epoch(info)
    except BaseException as error:
        primary = error
    finally:
        if iterator is not None:
            try:
                iterator.close()
            except BaseException as cleanup:
                primary = _retain(primary, cleanup)
    if primary is not None:
        raise primary  # no next directory/input after pending failure/control
    if set(rows) != expected:
        raise ValueError('missing original inventory entry')
    return rows


def observe_originals(*, paths, receipt_root, limits):
    if socket.gethostname() == 'laptopstudio':
        raise RuntimeError('BUILDBOX_ONLY; no local collector/control authority')
    keys = {'file_bytes', 'total_bytes', 'path_bytes', 'inventory_entries', 'metadata_bytes'}
    if (type(paths) is not dict or set(paths) != set(LABELS)
            or type(limits) is not dict or set(limits) != keys
            or type(limits['file_bytes']) is not dict or set(limits['file_bytes']) != set(LABELS)
            or any(type(v) is not int or not 0 < v <= MAX_INPUT_BYTES for v in limits['file_bytes'].values())
            or any(type(limits[k]) is not int or limits[k] <= 0 for k in keys - {'file_bytes'})):
        raise ValueError('exact31 originals and all finite limits required')
    receipt_root = _root(receipt_root)
    roots, declared, identities, total = {}, {}, set(), 0
    for label in LABELS:
        text = paths[label]
        if (type(text) is not str or not text or '\x00' in text or '\\' in text
                or len(text.encode('utf-8')) > limits['path_bytes']):
            raise ValueError('bounded exact original path required')
        path = Path(text)
        if not path.is_absolute() or str(path) != text or '..' in path.parts:
            raise ValueError('canonical original path required')
        root = _root(path.parent)
        if '.' in label:
            role, phase = label.split('.')
            if root != receipt_root:
                raise ValueError('raw/data original outside receipt root')
            if phase != 'data' and path.name != role + '.' + phase + '.json':
                raise ValueError('raw role/phase original channel mismatch')
            if phase == 'data':
                pid = path.name.removeprefix('coverage-pid-').removesuffix('.data')
                if (path.name != 'coverage-pid-' + pid + '.data' or not pid.isascii()
                        or not pid.isdecimal() or int(pid) <= 0 or str(int(pid)) != pid):
                    raise ValueError('native PID data basename required')
        if any(os.path.lexists(text + suffix) for suffix in ('-wal', '-shm', '-journal')):
            raise ValueError('original associated sidecar before streams')
        info = path.lstat()
        identity = info.st_dev, info.st_ino
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid()
                or not 0 < info.st_size <= limits['file_bytes'][label]
                or identity in identities or text in declared):
            raise ValueError('original size/kind/owner/path/inode bound')
        identities.add(identity); declared[text] = _epoch(info)
        total += info.st_size
        names = roots.setdefault(root, {'names': set(), 'epoch': _epoch(root.stat())})['names']
        names.add(path.name)
    # owner.json is the ONLY extra name, required on the real run output.
    # Logs/JUnit/cache/tool trees must be staged outside these closed roots.
    if 'owner.json' in roots[receipt_root]['names']:
        raise ValueError('owner metadata aliases an original')
    roots[receipt_root]['names'].add('owner.json')
    owner = receipt_root / 'owner.json'
    owner_info = owner.lstat()
    if (not stat.S_ISREG(owner_info.st_mode) or owner_info.st_nlink != 1
            or owner_info.st_uid != os.geteuid() or not 0 < owner_info.st_size <= limits['metadata_bytes']
            or (owner_info.st_dev, owner_info.st_ino) in identities):
        raise ValueError('owner metadata kind/size/identity')
    total += owner_info.st_size
    if total > limits['total_bytes']:
        raise ValueError('ALL-original aggregate pre-read bound')
    inventories = {}
    for root, row in roots.items():
        inventories[root] = _inventory(root, row['names'], entry_limit=limits['inventory_entries'],
                                       metadata_bytes=limits['metadata_bytes'])
    # Every31 count and every directory enumeration completed before ANY stream.
    observed, payloads = {}, {}
    for label in LABELS:
        value = observe_file(paths[label], byte_limit=limits['file_bytes'][label],
                             path_bytes_limit=limits['path_bytes'],
                             retain_bytes=label == 'config' or label.endswith(('.start', '.finish')))
        observation = value['observation']
        expected = declared[paths[label]]
        if ((observation['dev'], observation['ino'], observation['bytes'],
             observation['mtime_ns'], observation['ctime_ns']) != expected[:5]):
            raise ValueError('original changed between all-input preflight and stream')
        observed[label] = observation
        if value['raw'] is not None:
            payloads[label] = value['raw']
    for root, row in roots.items():
        if (_epoch(root.stat()) != row['epoch']
                or _inventory(root, row['names'], entry_limit=limits['inventory_entries'],
                              metadata_bytes=limits['metadata_bytes']) != inventories[root]):
            raise ValueError('original inventory/root changed after streams')
    return {'observations': observed, 'raw': payloads,
            'qualification': 'ACTUAL31_ORIGINAL_OBSERVATIONS_NOT_INDEX_OR_PROVENANCE',
            'source_process_or_closed_writer_authenticated': False}
