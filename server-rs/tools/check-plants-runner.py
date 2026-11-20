#!/usr/bin/env python3
"""Checks that the runner of wrong rules (`tools/plants/runner.py`) runs nothing on words it does not know.

A list of wrong rules plants into the source and compiles. Given an argument it does not know (`--help`, a word
mistyped, a shard that is none), it must say so in one line with its usage and end, having planted, compiled and
written nothing; `--help` and `-h` print the usage and end well; `--check` still says whether every rule has its one
place. Run beside the lists' own `--check`:

    python3 server-rs/tools/check-plants-runner.py

It starts no compiler: every case ends in the runner's reading of its arguments, before anything is run; a runner that
went on would find a stand-in for `cargo` that only leaves a mark. Nothing is reached outside the machine.
"""
from __future__ import annotations

import os
import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import scratch as tool_scratch

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parents[1]
# A list that is on main, so that this check stands with the runner alone.
LIST = HERE / 'plants' / 'agent-portraits.py'

REFUSED = [
    ['--bogus'], ['--help-me'], ['--check', '--verbose'], ['3', 'x'], ['--shard'], ['--shard', '8/8'],
    ['--shard', 'a/b'], ['--shard', '1'], ['--root'], ['--root', '--check'], ['-v'], ['٣'],
]
HELP = [['--help'], ['-h'], ['--check', '--help']]


def source_state() -> str:
    """What git says of the sources a list plants into: the same before and after every case."""
    said = subprocess.run(['git', 'status', '--porcelain', '--', 'server-rs/crates'], cwd=ROOT,
                          capture_output=True, text=True, check=True)
    return said.stdout


def ask(args: list[str], out: pathlib.Path, tools: pathlib.Path) -> subprocess.CompletedProcess[str]:
    # Were the arguments taken, the runner would go on to compile: it is given a stand-in for `cargo` that only
    # leaves a mark, so that a runner that is wrong is seen and reaches no compiler and no build machine.
    world = dict(os.environ, PLANTS_CARGO='1', PLANTS_OUT=str(out))
    world['PATH'] = f'{tools}{os.pathsep}{world.get("PATH", "")}'
    return subprocess.run([sys.executable, str(LIST), *args], cwd=ROOT, env=world, capture_output=True, text=True,
                          timeout=60, check=False)


def main() -> int:
    failures = []
    before = source_state()
    with tool_scratch.directory('clarp-runner-check-') as folder:
        tools = pathlib.Path(folder) / 'bin'
        tools.mkdir()
        mark = pathlib.Path(folder) / 'compiled'
        (tools / 'cargo').write_text(f'#!/bin/sh\necho "$*" >> "{mark}"\nexit 0\n')
        (tools / 'cargo').chmod(0o755)
        for number, args in enumerate(REFUSED + HELP + [['--check']]):
            out = pathlib.Path(folder) / str(number)
            try:
                done = ask(args, out, tools)
            except subprocess.TimeoutExpired:
                failures.append(f'{args}: did not end at its arguments')
                continue
            lines = done.stdout.strip().splitlines()
            if args in REFUSED:
                good = (done.returncode == 2 and len(lines) == 1 and 'Nothing was run.' in lines[0]
                        and 'usage:' in lines[0])
            elif args in HELP:
                good = done.returncode == 0 and len(lines) == 1 and lines[0].startswith('usage:')
            else:
                good = done.returncode == 0 and 'every wrong rule has its one place' in done.stdout
            if not good:
                failures.append(f'{args}: exit {done.returncode}: {done.stdout.strip()[:200]!r}')
            if out.exists():
                failures.append(f'{args}: wrote {out}')
            if mark.exists():
                failures.append(f'{args}: compiled: {mark.read_text().strip()[:120]!r}')
                mark.unlink()
    if source_state() != before:
        failures.append('the sources changed')
    for failure in failures:
        print(failure)
    print(f'runner: {len(REFUSED)} refused, {len(HELP)} usage, 1 check: '
          + ('every case as it must be' if not failures else f'{len(failures)} wrong'))
    return 1 if failures else 0


if __name__ == '__main__':
    raise SystemExit(main())
