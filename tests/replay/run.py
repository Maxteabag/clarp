#!/usr/bin/env python3
"""Two-build replay runner.

  run.py compare --base REF --head REF   replay the corpus on both, report differences
  run.py self-check [--ref REF]          the harness proof: REF against itself twice
                                         gives zero differences, every planted change
                                         is caught
  run.py clean                           remove the cached build worktrees

REF is a commit-ish (checked out with ``git worktree add --detach`` into the
scratch directory and cached by SHA) or the path of an existing checkout.
Run it through the program's heavy wrapper. Exit 0: no unexplained
difference (self-check: proof holds); 1: differences (self-check: proof
failed); 2: a scenario or build could not run.
"""
from __future__ import annotations

import argparse
import concurrent.futures as futures
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent
HARNESS = HERE.parents[1]
sys.path.insert(0, str(HERE))

import capture  # noqa: E402
import diff  # noqa: E402
import normalize  # noqa: E402
import planted  # noqa: E402
import scenarios  # noqa: E402

DEFAULT_SCRATCH = Path(os.environ.get('CLARP_REPLAY_SCRATCH', '/var/tmp/clarp-refactor-program/rf-harness/scratch'))


def git(*args, cwd=HARNESS) -> str:
    return subprocess.run(['git', *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def build_for(ref: str, scratch: Path) -> Path:
    path = Path(ref)
    if path.is_dir() and (path / 'server/server.py').is_file():
        return path.resolve()
    sha = git('rev-parse', '--verify', f'{ref}^{{commit}}')
    target = scratch / 'builds' / sha[:12]
    if not (target / 'server/server.py').is_file():
        target.parent.mkdir(parents=True, exist_ok=True)
        git('worktree', 'add', '--detach', str(target), sha)
    return target


def planted_copy(build: Path, change: dict, scratch: Path) -> Path:
    """A copy of ``build``'s server with one planted change; static is shared."""
    target = scratch / 'planted' / change['id']
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    shutil.copytree(build / 'server', target / 'server',
                    ignore=shutil.ignore_patterns('__pycache__'))
    (target / 'static').symlink_to(build / 'static')
    planted.apply(change, target)
    return target


def run_corpus(builds: dict[str, Path], chosen, scratch: Path, jobs: int) -> dict[str, dict[str, dict]]:
    """Capture every scenario on every build; returns {label: {scenario: normalized}}."""
    out: dict[str, dict[str, dict]] = {label: {} for label in builds}
    with futures.ThreadPoolExecutor(max_workers=jobs) as pool:
        tasks = {pool.submit(capture.capture, build, s, scratch / 'runs' / label): (label, s['id'])
                 for label, build in builds.items() for s in chosen}
        for task in futures.as_completed(tasks):
            label, sid = tasks[task]
            try:
                raw = task.result()
            except Exception as exc:  # noqa: BLE001 - reported as a run error, never as text
                raw = {'scenario': sid, 'error': f'{type(exc).__name__}', 'root': '', 'build': '',
                       'http': [], 'sse': [], 'db': {'schema': [], 'tables': {}}, 'files': []}
            (scratch / 'runs' / label).mkdir(parents=True, exist_ok=True)
            (scratch / 'runs' / label / f'{sid}.raw.json').write_text(json.dumps(raw, default=str))
            out[label][sid] = normalize.normalize(raw, str(HARNESS))
    return out


def diff_builds(results, a: str, b: str, chosen) -> diff.Report:
    report = diff.Report()
    for s in chosen:
        diff.compare(results[a][s['id']], results[b][s['id']], report)
    return report


def errors(results) -> list[str]:
    return sorted(f"{label}/{sid}: {r['error']}" for label, runs in results.items()
                  for sid, r in runs.items() if r['error'])


def load_intended(path: str | None) -> set[str]:
    if not path:
        return set()
    return {line.split()[0] for line in Path(path).read_text().splitlines()
            if line.strip() and not line.startswith('#')}


def select(names: str | None):
    if not names:
        return scenarios.SCENARIOS
    return [scenarios.BY_ID[n] for n in names.split(',')]


def cmd_compare(args) -> int:
    scratch = Path(args.scratch)
    started = time.monotonic()
    chosen = select(args.scenarios)
    builds = {'base': build_for(args.base, scratch), 'head': build_for(args.head, scratch)}
    results = run_corpus(builds, chosen, scratch, args.jobs)
    report = diff_builds(results, 'base', 'head', chosen)
    text = diff.render(report, load_intended(args.intended), f'{args.base} -> {args.head}')
    print(text, end='')
    print(f'scenarios: {len(chosen)}  seconds: {time.monotonic() - started:.1f}')
    broken = errors(results)
    for line in broken:
        print('RUN ERROR', line)
    if broken:
        return 2
    return 1 if any(d['id'] not in load_intended(args.intended) for d in report.items) else 0


def cmd_self_check(args) -> int:
    scratch = Path(args.scratch)
    started = time.monotonic()
    chosen = select(args.scenarios)
    build = build_for(args.ref, scratch)
    changes = [c for c in planted.CHANGES if not args.planted or c['id'] in args.planted.split(',')]
    builds = {'run1': build, 'run2': build, 'run3': build}
    for change in changes:
        builds[f"planted-{change['id']}"] = planted_copy(build, change, scratch)
    results = run_corpus(builds, chosen, scratch, args.jobs)
    ok = True
    print(f'# Replay harness self-check on {args.ref} ({git("rev-parse", "HEAD", cwd=build) if (build / ".git").exists() else build})')
    print(f'scenarios: {len(chosen)}  planted changes: {len(changes)}')
    for a, b in (('run1', 'run2'), ('run1', 'run3')):
        report = diff_builds(results, a, b, chosen)
        verdict = 'PASS' if not report.items else 'FAIL'
        ok &= not report.items
        print(f'unchanged {a} vs {b}: {len(report.items)} differences  {verdict}')
        if report.items:
            print(diff.render(report, set(), f'{a} vs {b}'), end='')
    for change in changes:
        label = f"planted-{change['id']}"
        report = diff_builds(results, 'run1', label, chosen)
        caught = [d for d in report.items if d['channel'] in change['expect_channels']]
        verdict = 'CAUGHT' if caught else 'MISSED'
        ok &= bool(caught)
        channels = sorted({d['channel'] for d in report.items})
        print(f"planted {change['id']} ({change['what']}): {len(report.items)} differences "
              f"in {','.join(channels) or '-'}  {verdict}")
        if args.verbose:
            print(diff.render(report, set(), label), end='')
    broken = errors(results)
    for line in broken:
        print('RUN ERROR', line)
    print(f'seconds: {time.monotonic() - started:.1f}')
    print('PROOF HOLDS' if ok and not broken else 'PROOF FAILED')
    if broken:
        return 2
    return 0 if ok else 1


def cmd_clean(args) -> int:
    scratch = Path(args.scratch)
    for path in sorted((scratch / 'builds').glob('*')):
        git('worktree', 'remove', '--force', str(path))
        print('removed', path)
    git('worktree', 'prune')
    for sub in ('runs', 'planted'):
        shutil.rmtree(scratch / sub, ignore_errors=True)
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--scratch', default=str(DEFAULT_SCRATCH))
    parser.add_argument('--jobs', type=int, default=4)
    parser.add_argument('--scenarios', help='comma-separated scenario ids (default: all)')
    sub = parser.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('compare')
    p.add_argument('--base', required=True)
    p.add_argument('--head', required=True)
    p.add_argument('--intended', help='file of registered difference ids, one per line')
    p = sub.add_parser('self-check')
    p.add_argument('--ref', default='HEAD')
    p.add_argument('--planted', help='comma-separated planted change ids (default: all)')
    p.add_argument('--verbose', action='store_true')
    sub.add_parser('clean')
    args = parser.parse_args(argv)
    return {'compare': cmd_compare, 'self-check': cmd_self_check, 'clean': cmd_clean}[args.cmd](args)


if __name__ == '__main__':
    sys.exit(main())
