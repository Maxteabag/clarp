#!/usr/bin/env python3
"""Run reproducible checks and retain commands, exit codes and source identities."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time

ROOT = Path(__file__).resolve().parents[1]


def source_identity(root):
    def git(*args):
        return subprocess.check_output(['git', '-C', str(root), *args])
    files = set(git('diff', 'HEAD', '--name-only', '-z').split(b'\0'))
    files.update(git('ls-files', '--others', '--exclude-standard', '-z').split(b'\0'))
    changes = {}
    for name in sorted(files):
        if name:
            path = root / name.decode()
            changes[name.decode()] = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else 'deleted'
    return {'sha': git('rev-parse', 'HEAD').decode().strip(), 'changes': changes}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--full', action='store_true', help='include disposable Docker/browser checks')
    args = parser.parse_args()
    out = ROOT / '.qa' / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
    out.mkdir(parents=True)
    manifest = {'source': source_identity(ROOT), 'checks': []}
    environment = dict(os.environ)
    environment.setdefault('CLARP_TEST_IMAGE', 'clarp-qa:' + out.name.lower())
    manifest['test_image'] = environment['CLARP_TEST_IMAGE'] if args.full else None
    commands = [
        ['uv', 'run', '--frozen', '--group', 'dev', 'python', '-m', 'pytest', '-n', '2',
         f'--junitxml={out / "python.xml"}'],
        ['uv', 'run', '--frozen', '--group', 'dev', 'npm', 'test', '--',
         '--reporter=default', '--reporter=junit', f'--outputFile={out / "javascript.xml"}'],
        ['uv', 'run', '--frozen', 'python', 'scripts/benchmark_server_hotpaths.py', '--synthetic-snapshot'],
    ]
    if args.full:
        commands.extend([['scripts/test_docker_node.sh'], ['scripts/test_e2e_docker.sh']])
    status = 0
    for index, command in enumerate(commands):
        started = time.monotonic()
        log = out / f'{index + 1}.log'
        with log.open('w') as stream:
            result = subprocess.run(command, cwd=ROOT, env=environment, stdout=stream, stderr=subprocess.STDOUT)
        manifest['checks'].append({'command': command, 'exit_code': result.returncode,
                                   'seconds': round(time.monotonic() - started, 3), 'log': log.name})
        manifest['source_after'] = source_identity(ROOT)
        manifest['source_unchanged'] = manifest['source'] == manifest['source_after']
        (out / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
        print(f'{command[0]}: exit {result.returncode}; {log}', flush=True)
        if result.returncode or not manifest['source_unchanged']:
            status = result.returncode or 1
            break
    if not manifest['source_unchanged']:
        print('Source changed during verification; results do not prove the current tree.')
        status = 1
    print(f'QA evidence: {out}')
    raise SystemExit(status)


if __name__ == '__main__':
    main()
