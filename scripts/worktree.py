#!/usr/bin/env python3
"""Create owned worktrees and audit all restartable dependencies before closeout."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys


def run(*args):
    return subprocess.check_output(args, text=True, stderr=subprocess.PIPE).strip()


def metadata(path):
    return Path(run('git', '-C', str(path), 'rev-parse', '--absolute-git-dir')) / 'clarp-owner.json'


def dependencies(path: Path) -> list[str]:
    """Fail closed when workload inventory cannot be inspected."""
    if sys.platform != 'linux':
        return ['automatic workload inspection currently requires Linux']
    needle = str(path)
    found = []
    ancestors = set()
    parent = os.getppid()
    while parent > 1 and parent not in ancestors:
        ancestors.add(parent)
        try:
            status = Path(f'/proc/{parent}/status').read_text()
            parent = int(next(line.split()[1] for line in status.splitlines() if line.startswith('PPid:')))
        except (OSError, StopIteration, ValueError):
            break
    for proc in Path('/proc').iterdir():
        if not proc.name.isdigit() or int(proc.name) == os.getpid():
            continue
        try:
            cwd = str((proc / 'cwd').resolve(strict=True))
            command = (proc / 'cmdline').read_bytes().replace(b'\0', b' ').decode(errors='replace')
            # Invoking shells naturally contain the target as a CLI argument.
            # Their cwd still counts; unrelated processes' arguments count too.
            if cwd == needle or cwd.startswith(needle + '/') or (needle in command and int(proc.name) not in ancestors):
                found.append(f'process {proc.name}')
        except FileNotFoundError:
            continue  # exited during inspection
        except PermissionError:
            # Other users' processes cannot be classified as safe.
            found.append(f'cannot inspect process {proc.name}')
    try:
        ids = run('docker', 'ps', '-aq').split()
        if ids:
            for container in json.loads(run('docker', 'inspect', *ids)):
                # Includes stopped containers, Compose labels and restart policy.
                mounted = False
                for mount in container.get('Mounts', []):
                    source = Path(mount.get('Source') or '')
                    if source.is_absolute():
                        source = source.resolve()
                        mounted |= source.is_relative_to(path) or path.is_relative_to(source)
                if mounted or needle in json.dumps(container.get('Config', {}).get('Labels', {})):
                    found.append(f'container {container["Name"]}')
    except (OSError, subprocess.CalledProcessError) as error:
        found.append(f'cannot inspect Docker: {type(error).__name__}')
    for scope in ([], ['--user']):
        try:
            units = {line.split()[0] for line in run(
                'systemctl', *scope, 'list-unit-files', '--no-legend', '--no-pager').splitlines() if line}
            units.update(line.split()[0] for line in run(
                'systemctl', *scope, 'list-units', '--all', '--plain', '--no-legend', '--no-pager').splitlines() if line)
            if units:
                details = run('systemctl', *scope, 'show', *sorted(unit for unit in units if '@.' not in unit), '--no-pager',
                              '--property=Id,WorkingDirectory,ExecStart,ExecStartPre,ExecStartPost,ExecStop,FragmentPath')
                for block in details.split('\n\n'):
                    if needle in block:
                        found.append('systemd ' + next((s for s in block.splitlines() if s.startswith('Id=')), 'unit'))
            # Inactive unit files can refer to the tree through EnvironmentFile,
            # scripts or a timer-triggered service, beyond the loaded properties.
            for directory in (Path('/etc/systemd/system'), Path('/usr/lib/systemd/system'),
                              Path.home() / '.config/systemd/user', Path('/etc/systemd/user')):
                if directory.exists():
                    for file in directory.rglob('*'):
                        if file.is_file() and needle in file.read_text(errors='replace'):
                            found.append(f'systemd file {file}')
        except (OSError, subprocess.CalledProcessError) as error:
            found.append(f'cannot inspect systemd {scope}: {type(error).__name__}')
    return sorted(set(found))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['create', 'check', 'remove'])
    parser.add_argument('path', type=Path)
    parser.add_argument('--owner', required=True)
    parser.add_argument('--branch')
    args = parser.parse_args()
    path = args.path.resolve()
    if args.action == 'create':
        if not args.branch:
            parser.error('--branch is required for create')
        run('git', 'fetch', 'origin', 'main')
        run('git', 'worktree', 'add', '-b', args.branch, str(path), 'origin/main')
        metadata(path).write_text(json.dumps({'owner': args.owner, 'path': str(path)}) + '\n')
        print(path)
        return
    registered = run('git', 'worktree', 'list', '--porcelain')
    if f'worktree {path}\n' not in registered + '\n':
        parser.error('path is not a registered worktree of this repository')
    marker = metadata(path)
    reasons = []
    if not marker.is_file() or json.loads(marker.read_text()).get('owner') != args.owner:
        reasons.append('ownership is not established by this tool')
    if run('git', '-C', str(path), 'status', '--porcelain'):
        reasons.append('worktree has uncommitted changes')
    run('git', 'fetch', 'origin', 'main')
    head = run('git', '-C', str(path), 'rev-parse', 'HEAD')
    if subprocess.run(['git', 'merge-base', '--is-ancestor', head, 'origin/main']).returncode:
        reasons.append('worktree commit is not contained in origin/main')
    reasons.extend(dependencies(path))
    print(json.dumps({'path': str(path), 'head': head, 'blockers': reasons}, indent=2))
    if reasons:
        raise SystemExit(1)
    if args.action == 'remove':
        # Git rechecks cleanliness at removal. No force flag is ever used.
        run('git', 'worktree', 'remove', str(path))
        run('git', 'worktree', 'prune')
        print(f'Removed {path}')


if __name__ == '__main__':
    main()
