"""Read-only Linux process snapshots for937. Not a containment/drain primitive.

Production adapter must use real /proc and bind roots immediately after Popen.
Test fixtures may supply a private proc_root; no process is signalled here.
Missing/denied/reused/exited identities raise, never produce memory zero.
"""
from dataclasses import asdict, dataclass
from pathlib import Path
import time

MAX_TEXT = 131072
MAX_TASKS = 256
MAX_PROCESSES = 128


class SnapshotUnknown(RuntimeError):
    pass


def text(path):
    try:
        with path.open('rb') as stream:
            raw = stream.read(MAX_TEXT + 1)
        if len(raw) > MAX_TEXT:
            raise SnapshotUnknown('PROC_RECORD_OVERSIZED')
        return raw.decode('ascii')
    except (OSError, UnicodeError) as error:
        raise SnapshotUnknown('PROC_RECORD_MISSING_DENIED_OR_UNDECODABLE') from error


def boot(proc_root):
    value = text(proc_root / 'sys/kernel/random/boot_id').strip()
    if not value or any(c not in '0123456789abcdef-' for c in value):
        raise SnapshotUnknown('BOOT_ID_INVALID')
    return value


@dataclass(frozen=True)
class Process:
    pid: int
    start_ticks: int
    boot_id: str


def state(pid, proc_root=Path('/proc')):
    if type(pid) is not int or pid <= 0:
        raise SnapshotUnknown('PID_INVALID')
    raw = text(proc_root / str(pid) / 'stat')
    prefix, separator, tail = raw.rpartition(')')
    if not separator or not prefix.startswith(str(pid) + ' ('):
        raise SnapshotUnknown('STAT_ID_INVALID')
    fields = tail.split()
    try:
        if len(fields) < 22:
            raise ValueError('short stat')
        values = {'pid': pid, 'state': fields[0], 'ppid': int(fields[1]),
                  'utime_ticks': int(fields[11]), 'stime_ticks': int(fields[12]),
                  'start_ticks': int(fields[19])}
        if any(values[k] < 0 for k in ('ppid', 'utime_ticks', 'stime_ticks', 'start_ticks')):
            raise ValueError('negative stat')
    except (ValueError, IndexError) as error:
        raise SnapshotUnknown('STAT_RECORD_INVALID') from error
    return values


def bind(pid, proc_root=Path('/proc')):
    record = state(pid, proc_root)
    if record['state'] in ('Z', 'X', 'x'):
        raise SnapshotUnknown('ROOT_EXITED')
    return Process(pid, record['start_ticks'], boot(proc_root))


def validate(process, proc_root=Path('/proc')):
    record = state(process.pid, proc_root)
    if record['start_ticks'] != process.start_ticks or boot(proc_root) != process.boot_id:
        raise SnapshotUnknown('PID_REUSED_OR_BOOT_CHANGED')
    if record['state'] in ('Z', 'X', 'x'):
        raise SnapshotUnknown('OWNED_PROCESS_EXITED')
    return record


def rollup(raw):
    result = {}
    for line in raw.splitlines():
        key, separator, value = line.partition(':')
        if key not in ('Rss', 'Pss'):
            continue
        if not separator or key in result:
            raise SnapshotUnknown('ROLLUP_DUPLICATE_OR_INVALID')
        fields = value.split()
        if len(fields) != 2 or fields[1] != 'kB' or not fields[0].isdigit():
            raise SnapshotUnknown('ROLLUP_UNITS_INVALID')
        result[key] = int(fields[0]) * 1024
    if set(result) != {'Rss', 'Pss'}:
        raise SnapshotUnknown('ROLLUP_INCOMPLETE')
    return {'rss_bytes': result['Rss'], 'pss_bytes': result['Pss']}


def sample(process, proc_root=Path('/proc')):
    before = time.monotonic()
    first = validate(process, proc_root)
    memory = rollup(text(proc_root / str(process.pid) / 'smaps_rollup'))
    io = {}
    for line in text(proc_root / str(process.pid) / 'io').splitlines():
        key, separator, value = line.partition(':')
        if not separator or key in io or not value.strip().isdigit():
            raise SnapshotUnknown('IO_COUNTER_INVALID')
        io[key] = int(value.strip())
    last = validate(process, proc_root)
    after = time.monotonic()
    return {'identity': asdict(process), 'before': before, 'after': after,
            'source': 'smaps_rollup Rss/Pss kB converted to bytes; NOT VmRSS',
            **memory, 'cpu_ticks_before': first['utime_ticks'] + first['stime_ticks'],
            'cpu_ticks_after': last['utime_ticks'] + last['stime_ticks'], 'io': io,
            'qualification': 'snapshot read bracket; no one-page physical-error assertion'}


def descendants(roots, proc_root=Path('/proc')):
    """Current all-task child snapshot only; NOT historical complete reaping.

    Any observed directory/read race remains UNKNOWN. No child gets ownership
    merely from a name/path/nonce; its parent/start and root identity are checked.
    Even a complete result cannot prove absence of a previously detached child.
    """
    if not roots or len({p.pid for p in roots}) != len(roots):
        raise SnapshotUnknown('ROOT_SET_INVALID')
    found = {p.pid: p for p in roots}
    queue = list(roots)
    edges = []
    while queue:
        parent = queue.pop(0)
        validate(parent, proc_root)
        directory = proc_root / str(parent.pid) / 'task'
        try:
            tasks = sorted(directory.iterdir())
        except OSError as error:
            raise SnapshotUnknown('TASK_CENSUS_MISSING_OR_DENIED') from error
        if not tasks or len(tasks) > MAX_TASKS or any(not p.name.isdigit() for p in tasks):
            raise SnapshotUnknown('TASK_CENSUS_INVALID_OR_OVERFLOWED')
        for task in tasks:
            ids = text(task / 'children').split()
            if len(ids) > MAX_PROCESSES or any(not value.isdigit() for value in ids):
                raise SnapshotUnknown('CHILD_CENSUS_INVALID_OR_OVERFLOWED')
            for value in ids:
                pid = int(value)
                if pid in found:
                    continue
                child = bind(pid, proc_root)
                child_state = validate(child, proc_root)
                if child_state['ppid'] != parent.pid or child.start_ticks < parent.start_ticks:
                    raise SnapshotUnknown('CHILD_PARENT_OR_BIRTH_CHANGED')
                validate(parent, proc_root)
                found[pid] = child
                queue.append(child)
                edges.append({'parent': asdict(parent), 'task_tid': int(task.name),
                              'child': asdict(child)})
                if len(found) > MAX_PROCESSES:
                    raise SnapshotUnknown('PROCESS_CENSUS_OVERFLOWED')
        validate(parent, proc_root)
    return {'processes': list(found.values()), 'edges': edges,
            'qualification': 'current visible all-task child snapshot; no past detached-child/drain proof'}


def owned_memory(roots, proc_root=Path('/proc')):
    census = descendants(roots, proc_root)
    records = [sample(process, proc_root) for process in census['processes']]
    for process in census['processes']:
        validate(process, proc_root)
    return {'roots': [asdict(p) for p in roots], 'processes': records,
            'edges': census['edges'], 'rss_sum_bytes': sum(p['rss_bytes'] for p in records),
            'pss_sum_bytes': sum(p['pss_bytes'] for p in records),
            'qualification': 'summed RSS double-counts shared pages; PSS proportional; '
                             'read brackets differ; observer excluded and separately attributed; '
                             'cgroup charge/membership requires separate adapter'}
