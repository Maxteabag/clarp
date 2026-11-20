"""Wrong rules, planted one at a time: whether the tests of the Host in Rust can fail.

A test that cannot fail holds nothing. Beside this program lie lists of wrong rules, one list for a part of the
Host. A wrong rule is a small change to the source that makes the Host do otherwise than it is to do: an id may be
of any signs, a call that is ending is not waited for. For each a list names the file, the tests that should notice
(a run of the list), what the wrong rule says in words, the text as it is, and the text of the wrong rule. This
program writes each wrong rule into the source in its turn, runs the tests of its run, puts the source back, and
writes down how that ended:

    CAUGHT           the tests failed: they hold the rule
    MISSED           the tests passed with the wrong rule in place: nothing holds the rule
    DID NOT COMPILE  the wrong rule is no program: it is to be written again
    HUNG             the tests did not end

What is MISSED gets a test, or the code that nothing can observe goes, or the wrong rule is taken off the list with
the reason written there (`NOT PLANTED`). A list is done when every rule of it is CAUGHT.

    server-rs/tools/plants/<list>.py [--check] [--root DIR] [--shard I/N] [numbers...]

`--check` only says whether every wrong rule still has its one place in the source. `--root` plants into another
checkout than the one the list is in. `--shard I/N` takes every Nth rule, beginning with rule I: so several machines
share a list. Numbers name the rules to plant; without them, all, after the tests were run as they are: tests that
fail by themselves show nothing.

Where it compiles: through `server-rs/tools/build` on the build machine, or with `cargo` itself where `PLANTS_CARGO`
is set, which is how the workflow "Planted rules (Rust)" runs it on GitHub's machines. What it writes down goes to
`PLANTS_OUT` (a temporary directory removed on return when unset): `<list>.result`, and the tests' own words for every
rule in `<list>.<number>.log`.

No wrong rule may make a test reach beyond the machine it runs on: where the rule would be one, it is not planted,
and the list says so.
"""
from __future__ import annotations

import os
import pathlib
import subprocess
import sys

VERDICTS = ('CAUGHT', 'MISSED', 'DID NOT COMPILE', 'HUNG')
# How long the tests of one rule may take, compiling included.
ENDS_WITHIN = 1500


sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import scratch as tool_scratch


def main(list_file: str, runs: dict, plants: list) -> int:
    args = sys.argv[1:]
    here = pathlib.Path(list_file).resolve()
    name, root = here.stem, here.parents[3]
    if '--root' in args:
        at = args.index('--root')
        root = pathlib.Path(args[at + 1]).resolve()
        del args[at:at + 2]
    shard = None
    if '--shard' in args:
        at = args.index('--shard')
        first, every = (int(part) for part in args[at + 1].split('/'))
        shard = (first, every)
        del args[at:at + 2]
    crates = root / 'server-rs' / 'crates'
    kept = {file: (crates / file).read_text() for file in {plant[0] for plant in plants}}
    lost = [f'{number}: {what}' for number, (file, _, what, old, _) in enumerate(plants) if kept[file].count(old) != 1]
    unknown = sorted({run for _, run, _, _, _ in plants} - set(runs))
    if lost or unknown:
        print('NOT RUN. No one place for:', *lost, sep='\n  ')
        print('No such run:', *unknown)
        return 2
    if '--check' in args:
        print(f'{name}: every wrong rule has its one place: {len(plants)}')
        return 0
    only = [int(arg) for arg in args if arg.isdigit()]
    chosen = [number for number in range(len(plants))
              if (not only or number in only) and (shard is None or number % shard[1] == shard[0])]
    if 'PLANTS_OUT' in os.environ:
        out = pathlib.Path(os.environ['PLANTS_OUT'])
        out.mkdir(parents=True, exist_ok=True)
        return run(out, root, crates, kept, runs, plants, chosen, shard, name, not only)
    with tool_scratch.directory('clarp-plants-') as folder:
        return run(pathlib.Path(folder), root, crates, kept, runs, plants, chosen, shard, name, not only)


def run(out, root, crates, kept, runs, plants, chosen, shard, name, check_baseline):
    tag = name if shard is None else f'{name}.shard-{shard[0]}'

    def tested(run: str, number: str) -> tuple[str, str]:
        """Runs the tests of a run. Says how they ended (`passed`, `failed`, `no program`, `hung`) and where."""
        if os.environ.get('PLANTS_CARGO'):
            command, where = ['cargo', runs[run][0], '--workspace', '--locked', *runs[run][1:]], root / 'server-rs'
        else:
            command, where = ['server-rs/tools/build', *runs[run]], root
        try:
            done = subprocess.run(command, cwd=where, capture_output=True, text=True, timeout=ENDS_WITHIN, check=False)
        except subprocess.TimeoutExpired:
            return 'hung', ''
        said = done.stdout + done.stderr
        (out / f'{tag}.{number}.log').write_text(said)
        lines = said.splitlines()
        failed = [line.split()[1] for line in lines if line.startswith('test ') and line.endswith('FAILED')]
        first = next((line.strip() for line in lines if 'panicked at' in line or line.startswith('error')), '')
        told = (', '.join(test[:60] for test in failed) or first)[:170]
        if done.returncode == 0:
            return 'passed', told
        return ('failed' if 'test result:' in said else 'no program'), told

    lines: list[str] = []

    def write(line: str) -> None:
        lines.append(line)
        print(line, flush=True)
        (out / f'{tag}.result').write_text('\n'.join(lines) + '\n')

    if check_baseline:
        for run in sorted({plants[number][1] for number in chosen}):
            ended, told = tested(run, f'as-it-is-{run}')
            write(f'as it is ({run}): {ended} {told}'.rstrip())
            if ended != 'passed':
                write(f'{name}: NOT RUN: the tests of {run} do not pass as they are')
                return 2
    counted = dict.fromkeys(VERDICTS, 0)
    try:
        for number in chosen:
            file, run, what, old, new = plants[number]
            (crates / file).write_text(kept[file].replace(old, new))
            ended, told = tested(run, str(number))
            (crates / file).write_text(kept[file])
            verdict = {'failed': 'CAUGHT', 'passed': 'MISSED', 'no program': 'DID NOT COMPILE', 'hung': 'HUNG'}[ended]
            counted[verdict] += 1
            write(f'{verdict} {number} ({run}): {what} | {told}')
            if ended == 'hung':
                # Whatever did not end may still hold the one slot of the build machine.
                break
    finally:
        for file, text in kept.items():
            (crates / file).write_text(text)
    others = ', '.join(f'{count} {verdict}' for verdict, count in counted.items() if count and verdict != 'CAUGHT')
    write(f'{tag}: {len(chosen)} wrong rules: {counted["CAUGHT"]} CAUGHT' + (f', {others}' if others else ''))
    return 0 if counted['CAUGHT'] == len(chosen) else 1
