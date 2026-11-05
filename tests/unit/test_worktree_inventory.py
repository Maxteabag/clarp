import importlib.util
import json
from pathlib import Path
import subprocess

import pytest

spec = importlib.util.spec_from_file_location('worktree_tool', Path(__file__).resolve().parents[2] / 'scripts/worktree.py')
worktree = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worktree)


@pytest.mark.parametrize('reference', ['mount', 'ancestor', 'compose'])
def test_stopped_restartable_container_blocks_closeout(monkeypatch, reference):
    target = Path('/tmp/worktree-inventory-fixture')
    container = {'Name': '/paused-test', 'State': {'Running': False},
                 'HostConfig': {'RestartPolicy': {'Name': 'always'}}, 'Mounts': [], 'Config': {'Labels': {}}}
    if reference == 'mount':
        container['Mounts'] = [{'Source': str(target / 'data'), 'Destination': '/data'}]
    elif reference == 'ancestor':
        container['Mounts'] = [{'Source': str(target.parent), 'Destination': '/checkouts'}]
    else:
        container['Config']['Labels']['com.docker.compose.project.config_files'] = str(target / 'compose.yml')
    def run(*args):
        if args[:3] == ('docker', 'ps', '-aq'): return 'container-id'
        if args[:2] == ('docker', 'inspect'): return json.dumps([container])
        return ''
    monkeypatch.setattr(worktree, 'run', run)
    assert 'container /paused-test' in worktree.dependencies(target)


def test_missing_workload_inventory_fails_closed(monkeypatch):
    def unavailable(*args):
        raise subprocess.CalledProcessError(1, args)
    monkeypatch.setattr(worktree, 'run', unavailable)
    blockers = worktree.dependencies(Path('/tmp/worktree-inventory-fixture'))
    assert any('cannot inspect Docker' in reason for reason in blockers)
    assert any('cannot inspect systemd' in reason for reason in blockers)


def _fake_proc(tmp_path, monkeypatch, needle, procs):
    """A fake /proc with one directory per pid: (cmdline, cwd target or None, error on
    cmdline read, error on cwd resolve). Everything else reads the real system."""
    import pathlib
    root = tmp_path / 'proc'
    for pid, (cmdline, cwd, _, _) in procs.items():
        (root / pid).mkdir(parents=True)
        (root / pid / 'cmdline').write_bytes(cmdline.encode().replace(b' ', b'\0'))
        (root / pid / 'cwd').symlink_to(cwd or tmp_path)
    real_path, real_read, real_resolve = worktree.Path, pathlib.Path.read_bytes, pathlib.Path.resolve
    class FakePath:
        home = staticmethod(real_path.home)
        def __new__(cls, *a):
            return root if a == ('/proc',) else real_path(*a)
    monkeypatch.setattr(worktree, 'Path', FakePath)
    def read_bytes(self):
        error = procs.get(self.parent.name, (None, None, None, None))[2] if self.parent.parent == root else None
        if error:
            raise error
        return real_read(self)
    def resolve(self, strict=False):
        error = procs.get(self.parent.name, (None, None, None, None))[3] if self.parent.parent == root else None
        if error:
            raise error
        return real_resolve(self, strict=strict)
    monkeypatch.setattr(pathlib.Path, 'read_bytes', read_bytes)
    monkeypatch.setattr(pathlib.Path, 'resolve', resolve)
    monkeypatch.setattr(worktree, 'run', lambda *a: '')


def test_a_process_that_exits_mid_scan_is_skipped_not_a_crash(tmp_path, monkeypatch):
    # TOOL-01: an exited pid's cmdline raises ESRCH, not ENOENT (G1, 5 Nov).
    target = tmp_path / 'tree'
    target.mkdir()
    _fake_proc(tmp_path, monkeypatch, str(target), {
        '101': ('python worker', None, ProcessLookupError(3, 'No such process'), None),
        '102': ('editor', str(target), None, None)})
    assert worktree.dependencies(target) == ['process 102']


def test_other_users_unreadable_processes_are_counted_and_their_command_lines_checked(tmp_path, monkeypatch):
    target = tmp_path / 'tree'
    denied = PermissionError(13, 'Permission denied')
    _fake_proc(tmp_path, monkeypatch, str(target), {
        '201': (f'rsync {target}/x /backup', None, None, denied),   # cwd unreadable, names the tree
        '202': ('sshd', None, None, denied),                       # cwd unreadable, unrelated
        '203': ('hidden', None, denied, None)})                    # nothing readable (hidepid)
    notes = []
    assert worktree.dependencies(target, notes) == ['process 201']
    assert notes == ['2 processes of other users: working directory not readable (their command lines were checked)',
                     '1 processes could not be inspected at all']


def test_systemd_units_whose_names_start_with_a_dash_are_inspected(monkeypatch, tmp_path):
    # '-.mount' and '-.slice' exist on every systemd host; systemctl reads an
    # argument starting with '-' before '--' as an option and exits 1.
    target = tmp_path / 'tree'
    def run(*args):
        if args[:1] != ('systemctl',):
            return ''
        if 'list-unit-files' in args:
            return '-.mount generated\napp.service enabled'
        if 'list-units' in args:
            return '-.slice loaded active active Root Slice'
        if 'show' in args:
            units = args[args.index('--') + 1:] if '--' in args else []
            if any(a.startswith('-') and not a.startswith('--') for a in args[:args.index('--') if '--' in args else len(args)]):
                raise subprocess.CalledProcessError(1, args)
            return '\n\n'.join(f'Id={u}\nWorkingDirectory={target if u == "app.service" else "/"}' for u in units)
        return ''
    monkeypatch.setattr(worktree, 'run', run)
    blockers = worktree.dependencies(target)
    assert 'systemd Id=app.service' in blockers
    assert not any('cannot inspect systemd' in b for b in blockers)
