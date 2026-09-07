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
