"""Private proc-record controls; SOURCE ONLY until admitted buildbox proof.

These do not establish real-kernel all-tree containment, missing permissions,
provider safety or historical reaping. No real process is read or signalled.
"""
from pathlib import Path

import pytest

from . import proc_samples as proc

BOOT = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'


def process(root, pid, *, parent=9, start=10, children='', extra_tasks=None):
    directory = root / str(pid)
    directory.mkdir(parents=True, exist_ok=True)
    fields = ['S'] + ['0'] * 21
    fields[1] = str(parent)
    fields[11], fields[12], fields[19] = '2', '3', str(start)
    (directory / 'stat').write_text(str(pid) + ' (synthetic proc) ' + ' '.join(fields))
    task = directory / 'task' / str(pid)
    task.mkdir(parents=True, exist_ok=True)
    (task / 'children').write_text(children)
    for tid, values in (extra_tasks or {}).items():
        task = directory / 'task' / str(tid)
        task.mkdir(parents=True, exist_ok=True)
        (task / 'children').write_text(values)
    (directory / 'smaps_rollup').write_text('0000-ffff ---p 0 0:0 0 [rollup]\nRss: 100 kB\nPss: 50 kB\n')
    (directory / 'io').write_text('read_bytes: 0\nwrite_bytes: 12\n')


@pytest.fixture
def roots(tmp_path):
    boot = tmp_path / 'sys/kernel/random/boot_id'
    boot.parent.mkdir(parents=True)
    boot.write_text(BOOT)
    process(tmp_path, 100)
    return tmp_path


def test_rollup_units_identity_and_read_brackets_are_retained(roots):
    identity = proc.bind(100, roots)
    result = proc.sample(identity, roots)
    assert result['identity'] == {'pid': 100, 'start_ticks': 10, 'boot_id': BOOT}
    assert result['rss_bytes'] == 102400
    assert result['pss_bytes'] == 51200
    assert result['before'] <= result['after']
    assert result['cpu_ticks_before'] == result['cpu_ticks_after'] == 5
    assert result['io']['write_bytes'] == 12


def test_reused_or_gone_process_never_becomes_zero_memory(roots):
    identity = proc.bind(100, roots)
    process(roots, 100, start=11)
    with pytest.raises(proc.SnapshotUnknown, match='REUSED'):
        proc.sample(identity, roots)
    (roots / '100/stat').unlink()
    with pytest.raises(proc.SnapshotUnknown, match='MISSING'):
        proc.sample(identity, roots)


def test_boot_mismatch_is_unknown(roots):
    identity = proc.bind(100, roots)
    (roots / 'sys/kernel/random/boot_id').write_text('bbbbbbbb-bbbb-cccc-dddd-eeeeeeeeeeee')
    with pytest.raises(proc.SnapshotUnknown, match='BOOT_CHANGED'):
        proc.sample(identity, roots)


@pytest.mark.parametrize('raw', ['Pss: 50 kB\n', 'Rss: 2 MB\nPss: 1 kB\n',
                                'Rss: 2 kB\nRss: 2 kB\nPss: 1 kB\n',
                                'Rss: -1 kB\nPss: 1 kB\n'])
def test_missing_duplicate_or_wrong_units_refuse(raw):
    with pytest.raises(proc.SnapshotUnknown):
        proc.rollup(raw)


def test_nonmain_task_and_nested_current_children_do_not_include_unrelated(roots):
    process(roots, 100, extra_tasks={101: '200'})
    process(roots, 200, parent=100, start=20, children='300')
    process(roots, 300, parent=200, start=30)
    process(roots, 999, parent=1, start=1)
    result = proc.owned_memory([proc.bind(100, roots)], roots)
    assert {row['identity']['pid'] for row in result['processes']} == {100, 200, 300}
    assert any(edge['task_tid'] == 101 for edge in result['edges'])
    assert result['rss_sum_bytes'] == 307200  # explicitly summed, not unique physical pages
    assert result['pss_sum_bytes'] == 153600


def test_child_parent_change_or_missing_child_is_unknown(roots):
    process(roots, 100, children='200')
    with pytest.raises(proc.SnapshotUnknown, match='MISSING'):
        proc.descendants([proc.bind(100, roots)], roots)
    process(roots, 200, parent=999, start=20)
    with pytest.raises(proc.SnapshotUnknown, match='PARENT'):
        proc.descendants([proc.bind(100, roots)], roots)
