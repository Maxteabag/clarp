#!/usr/bin/env python3
"""Two-build replay runner.

  run.py compare --base REF --head REF   replay the corpus on both, report differences
  run.py self-check [--ref REF]          the harness proof: REF against itself twice
                                         gives zero differences, and every planted
                                         change shows its specific difference
  run.py prepare REF...                  (laptop, git only) extract builds into
                                         .replay-builds/ for a runner without git
  run.py clean                           remove the cached build worktrees (clean ones only)

On the buildbox (heavy-remote copies the worktree without .git), builds come
from ``.replay-builds/<sha>/``, made beforehand by ``prepare``: ``git archive``
of server/ and static/, files read-only, and a stamp with a SHA-256 over every
file. A prepared build is used only if its digest still matches, checked
before and after the replay.

REF is a commit-ish, checked out with ``git worktree add --detach`` into the
scratch directory, made read-only and cached by SHA; a cached build is reused
only if it is still at that SHA and clean. ``--allow-path`` also accepts the
path of an existing clean checkout (development only; the report says so).

Never on laptopstudio: replay starts test Hosts (tests/replay/README.md).
Exit 0: no unexplained difference (self-check: PROOF HOLDS); 1: differences
(self-check: proof failed or diagnostic run); 2: a scenario or build could
not run.
"""
from __future__ import annotations

import argparse
import fcntl
import concurrent.futures as futures
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
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

# Short on purpose: a Host makes AF_UNIX sockets under its state dir (108-byte limit).
DEFAULT_SCRATCH = Path(os.environ.get('CLARP_REPLAY_SCRATCH', '/var/tmp/rfh'))
HARNESS_FILES = ('capture.py', 'diff.py', 'host.py', 'normalize.py', 'planted.py', 'run.py', 'scenarios.py')


class BuildError(RuntimeError):
    pass


def git(*args, cwd=HARNESS) -> str:
    return subprocess.run(['git', *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def _set_writable(root: Path, writable: bool) -> None:
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != '.git']
        for name in [*filenames, '.']:
            path = Path(dirpath) / name
            if path.is_symlink():
                continue
            mode = path.stat().st_mode
            path.chmod(mode | stat.S_IWUSR if writable else mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))


def _clean_at(path: Path, sha: str | None) -> str:
    head = git('rev-parse', 'HEAD', cwd=path)
    if sha is not None and head != sha:
        raise BuildError(f'build at {path.name} is at {head[:12]}, expected {sha[:12]}')
    if git('status', '--porcelain', '--untracked-files=all', cwd=path):
        raise BuildError(f'build at {path.name} is not clean')
    return head


PREPARED = HARNESS / '.replay-builds'
BUILD_DIRS = ('server', 'static', 'bin')  # bin/: the CLI scenarios run the build's clarp-admin
STAMP = '.replay-build.json'


def tree_digest(build: Path) -> str:
    digest = hashlib.sha256()
    for top in BUILD_DIRS:
        for path in sorted((build / top).rglob('*')):
            if '__pycache__' in path.parts or path.is_dir():
                continue
            digest.update(path.relative_to(build).as_posix().encode() + b'\0')
            digest.update(os.readlink(path).encode() if path.is_symlink() else path.read_bytes())
            digest.update(b'\0')
    return digest.hexdigest()


def has_git() -> bool:
    return (HARNESS / '.git').exists()


def prepared_build(ref: str) -> tuple[Path, str] | None:
    if not PREPARED.is_dir():
        return None
    if ref == 'HEAD':
        meta = PREPARED / 'harness.json'
        ref = json.loads(meta.read_text())['head'] if meta.is_file() else ref
    matches = [p for p in PREPARED.iterdir() if p.is_dir() and len(ref) >= 7 and p.name.startswith(ref)]
    if len(matches) != 1:
        return None
    stamp = json.loads((matches[0] / STAMP).read_text())
    if stamp.get('dirs') != list(BUILD_DIRS):
        raise BuildError(f"prepared build {stamp['sha'][:12]} was made by an older harness "
                         f"(it holds {stamp.get('dirs', ['server', 'static'])}, this one needs {list(BUILD_DIRS)}): "
                         f"run `python3 tests/replay/run.py prepare` again on the laptop")
    if tree_digest(matches[0]) != stamp['digest']:
        raise BuildError(f"prepared build {stamp['sha'][:12]} does not match its stamp")
    return matches[0], stamp['sha']


def cmd_prepare(args) -> int:
    PREPARED.mkdir(exist_ok=True)
    head = git('rev-parse', 'HEAD')
    dirty = bool(git('status', '--porcelain', '--', 'tests/replay'))
    (PREPARED / 'harness.json').write_text(json.dumps({'head': head, 'dirty': dirty}) + '\n')
    wanted = set()
    for ref in args.refs:
        sha = git('rev-parse', '--verify', f'{ref}^{{commit}}')
        wanted.add(sha)
        target = PREPARED / sha
        stamp = target / STAMP
        if stamp.is_file() and json.loads(stamp.read_text()).get('dirs') != list(BUILD_DIRS):
            # Made by an older harness with fewer directories (e.g. before bin/): make it again.
            _set_writable(target, True)
            shutil.rmtree(target)
            print(f'remade {sha[:12]} (prepared by an older harness)')
        if not target.exists():
            tmp = PREPARED / f'.{sha}.partial'
            if tmp.exists():
                _set_writable(tmp, True)
                shutil.rmtree(tmp)
            tmp.mkdir()
            archive = subprocess.run(['git', 'archive', sha, *BUILD_DIRS], cwd=HARNESS,
                                     check=True, capture_output=True).stdout
            subprocess.run(['tar', '-x', '-C', str(tmp)], input=archive, check=True)
            (tmp / STAMP).write_text(json.dumps({'sha': sha, 'dirs': list(BUILD_DIRS),
                                                 'digest': tree_digest(tmp)}) + '\n')
            _set_files_readonly(tmp)
            tmp.rename(target)
        print(f'prepared {ref} {sha}')
    if args.prune:
        for path in PREPARED.iterdir():
            if path.is_dir() and path.name not in wanted and (path / STAMP).is_file():
                _set_writable(path, True)
                shutil.rmtree(path)
                print('pruned', path.name)
    return 0


def _set_files_readonly(root: Path) -> None:
    """Files read-only, directories writable (so a sync can still delete them)."""
    for path in root.rglob('*'):
        if path.is_file() and not path.is_symlink():
            path.chmod(path.stat().st_mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))


STATIC_MTIME = 1_700_000_000


def pin_static_mtimes(build: Path) -> None:
    """The Host's server-version (and service-worker version) is the newest
    mtime under static/, which a checkout or sync sets to whenever it happened.
    Every build gets the same mtimes, so that value differs only if code does."""
    for path in (build / 'static').rglob('*'):
        if not path.is_symlink():
            os.utime(path, (STATIC_MTIME, STATIC_MTIME))


def build_for(ref: str, scratch: Path, allow_path: bool) -> tuple[Path, str]:
    """A pinned, clean, read-only build for ``ref``; returns (path, sha)."""
    build, sha = _build_for(ref, scratch, allow_path)
    pin_static_mtimes(build)
    return build, sha


def _build_for(ref: str, scratch: Path, allow_path: bool) -> tuple[Path, str]:
    prepared = prepared_build(ref)
    if prepared is not None:
        return prepared
    if not has_git():
        raise BuildError('no git here and no prepared build for this ref (run `run.py prepare` on the laptop)')
    path = Path(ref)
    if allow_path and path.is_dir() and (path / 'server/server.py').is_file():
        return path.resolve(), _clean_at(path.resolve(), None)
    sha = git('rev-parse', '--verify', f'{ref}^{{commit}}')
    target = scratch / 'builds' / sha
    target.parent.mkdir(parents=True, exist_ok=True)
    # The build cache is shared between jobs: one creates a build, the others wait.
    with open(scratch / 'builds' / '.lock', 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not target.exists():
            git('worktree', 'add', '--detach', str(target), sha)
            _set_writable(target, False)
        _clean_at(target, sha)
    return target, sha


LEASE = '.lease'


class RunRoot:
    """This run's own directory under ``<scratch>/r/``: made with an exclusive mkdir
    and held under an exclusive flock until the run ends, so no other job can
    reuse it and ``clean`` can tell a live run from a finished one. Every Host
    state dir, planted copy and raw capture of the run lives inside it; only the
    SHA build cache (``<scratch>/builds``) is shared."""

    def __init__(self, scratch: Path):
        base = scratch / 'r'
        base.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime('%m%d%H%M%S')
        for attempt in range(100):
            path = base / f'{stamp}-{os.getpid()}-{attempt}'
            try:
                path.mkdir()
                break
            except FileExistsError:
                continue
        else:
            raise BuildError('could not claim a run directory')
        self.path = path
        # A shared lease on the build cache for the whole run: clean takes it
        # exclusively, so it never removes a build while any run may be using one.
        (scratch / 'builds').mkdir(parents=True, exist_ok=True)
        self._lease = open(scratch / 'builds' / LEASE, 'w')
        fcntl.flock(self._lease, fcntl.LOCK_SH)
        self._lock = open(path / '.claim', 'w')
        fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self._lock.write(f'{os.getpid()}\n')
        self._lock.flush()
        self.labels: dict[str, str] = {}

    def dir_for(self, label: str) -> Path:
        """A short directory per build label (b, h, r1, p3, ...), recorded in labels.json."""
        if label not in self.labels:
            self.labels[label] = f'{len(self.labels):02d}'
            (self.path / self.labels[label]).mkdir()
            (self.path / 'labels.json').write_text(json.dumps(self.labels, indent=1) + '\n')
        return self.path / self.labels[label]

    def release(self, keep: bool) -> None:
        if not keep:
            _set_writable(self.path, True)
            shutil.rmtree(self.path, ignore_errors=True)
        self._lock.close()
        self._lease.close()


def live(run_dir: Path) -> bool:
    """Whether a run directory is still claimed by a running job."""
    claim = run_dir / '.claim'
    if not claim.is_file():
        return False
    with open(claim) as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        fcntl.flock(handle, fcntl.LOCK_UN)
    return False


def planted_copy(build: Path, change: dict, run: RunRoot) -> Path:
    """A fresh copy of ``build``'s server with one planted change; static is shared read-only."""
    target = run.path / f"plant-{planted.CHANGES.index(change):02d}"
    target.mkdir()
    shutil.copytree(build / 'server', target / 'server', ignore=shutil.ignore_patterns('__pycache__'))
    _set_writable(target / 'server', True)
    (target / 'static').symlink_to(build / 'static')
    (target / 'bin').symlink_to(build / 'bin')
    planted.apply(change, target)
    _set_writable(target / 'server', False)
    return target


def run_corpus(builds: dict[str, Path], chosen, run: RunRoot, jobs: int) -> dict[str, dict[str, dict]]:
    """Capture every scenario on every build; returns {label: {scenario: normalized}}."""
    out: dict[str, dict[str, dict]] = {label: {} for label in builds}
    index = {s['id']: f's{i:02d}' for i, s in enumerate(scenarios.SCENARIOS)}
    with futures.ThreadPoolExecutor(max_workers=jobs) as pool:
        tasks = {pool.submit(capture.capture, build, s, run.dir_for(label) / index[s['id']]): (label, s['id'])
                 for label, build in builds.items() for s in chosen}
        for task in futures.as_completed(tasks):
            label, sid = tasks[task]
            try:
                raw = task.result()
            except Exception as exc:  # noqa: BLE001 - reported as a run error by class only
                raw = {'scenario': sid, 'error': f'harness {type(exc).__name__}', 'root': '', 'build': '',
                       'http': [], 'sse': [], 'db': {'schema': [], 'pk': {}, 'tables': {}}, 'files': []}
            (run.dir_for(label) / f'{sid}.raw.json').write_text(json.dumps(raw, default=str))
            out[label][sid] = normalize.normalize(raw, str(HARNESS))
    return out


def verify_unchanged(builds) -> list[str]:
    """A prepared build must still match its stamp after the replay ran its code."""
    broken = []
    for label, build in builds.items():
        stamp = build / STAMP
        if stamp.is_file() and tree_digest(build) != json.loads(stamp.read_text())['digest']:
            broken.append(f'{label}: build changed during the replay')
    return broken


def diff_builds(results, a: str, b: str, chosen, calibration=()) -> diff.Report:
    report = diff.Report()
    for s in chosen:
        diff.compare(results[a][s['id']], results[b][s['id']], report,
                     [results[c][s['id']] for c in calibration] if calibration else None)
    return report


def errors(results, gating) -> list[str]:
    # Errors are harness-made strings (step index, op, what it waited for), never Host content.
    ids = {s['id'] for s in gating}
    return sorted(f"{label}/{sid}: {r['error']}" for label, runs in results.items()
                  for sid, r in runs.items() if r['error'] and sid in ids)


def split(chosen):
    """Gating scenarios count; known-bug scenarios (they pin a registered bug that
    is not fixed yet) run and are reported, never in the zero or the exit code."""
    return [s for s in chosen if not s.get('known_bug')], [s for s in chosen if s.get('known_bug')]


def known_lines(results, known, labels) -> list[str]:
    lines = []
    for s in known:
        states = ', '.join(f"{label} {'FAILED' if results[label][s['id']]['error'] else 'ok'}" for label in labels)
        lines.append(f"KNOWN BUG {s['known_bug']} ({s['id']}, not counted): {states}")
    return lines


def manifest(target: str, chosen, changes) -> list[str]:
    digest = hashlib.sha256()
    for name in HARNESS_FILES:
        digest.update((HERE / name).read_bytes())
    if has_git():
        head, dirty = git('rev-parse', 'HEAD'), bool(git('status', '--porcelain', '--', 'tests/replay'))
    else:
        meta = PREPARED / 'harness.json'
        info = json.loads(meta.read_text()) if meta.is_file() else {'head': 'unknown', 'dirty': True}
        head, dirty = f"{info['head']} (as prepared)", info['dirty']
    return [f'target: {target}',
            f"harness: {head}{' DIRTY' if dirty else ''} files-sha256 {digest.hexdigest()[:16]}",
            f"scenarios ({len(chosen)}): {','.join(s['id'] for s in chosen)}",
            f"planted ({len(changes)}): {','.join(c['id'] for c in changes)}"]


def load_intended(path: str | None) -> set[str]:
    if not path:
        return set()
    return {line.split()[0] for line in Path(path).read_text().splitlines()
            if line.strip() and not line.startswith('#')}


def select(names: str | None, table: dict, kind: str):
    if not names:
        return list(table.values())
    unknown = [n for n in names.split(',') if n not in table]
    if unknown:
        raise SystemExit(f'unknown {kind}: {len(unknown)} name(s)')
    return [table[n] for n in names.split(',')]


def cmd_compare(args) -> int:
    scratch = Path(args.scratch)
    started = time.monotonic()
    chosen = select(args.scenarios, scenarios.BY_ID, 'scenario')
    run = RunRoot(scratch)  # takes the build lease before any build is made or used
    try:
        base, base_sha = build_for(args.base, scratch, args.allow_path)
        head, head_sha = build_for(args.head, scratch, args.allow_path)
        status = _compare(args, run, chosen, base, base_sha, head, head_sha, started)
    except BaseException:
        run.release(keep=True)
        raise
    run.release(keep=args.keep or status != 0)
    print(f"run directory: {run.path}{' (kept)' if args.keep or status != 0 else ' (removed)'}")
    return status


def _compare(args, run, chosen, base, base_sha, head, head_sha, started) -> int:
    results = run_corpus({'base': base, 'base2': base, 'head': head}, chosen, run, args.jobs)
    chosen, known = split(chosen)
    # The base runs twice: a difference between its own runs means the scenario is not
    # reproducible, and its comparison with head proves nothing.
    calibration = ('base', 'base2') if args.cross_lane_order else ()
    noise = diff_builds(results, 'base', 'base2', chosen, calibration)
    report = diff_builds(results, 'base', 'head', chosen, calibration)
    intended = load_intended(args.intended)
    print('\n'.join(manifest(f'{base_sha} -> {head_sha}', chosen, [])))
    broken = errors(results, chosen) + verify_unchanged({'base': base, 'head': head})
    print('\n'.join(known_lines(results, known, ('base', 'base2', 'head'))))
    if noise.items:
        broken.append(f'base is not reproducible: {len(noise.items)} differences between its two runs')
        print('\n'.join(diff.shapes(noise)))
    print(diff.render(report, intended, f'{base_sha[:12]} -> {head_sha[:12]}', broken), end='')
    print(f'seconds: {time.monotonic() - started:.1f}')
    if broken:
        return 2
    return 1 if any(d['id'] not in intended for d in report.items) else 0


def cmd_self_check(args) -> int:
    scratch = Path(args.scratch)
    started = time.monotonic()
    diagnostic = bool(args.scenarios or args.planted or args.allow_path)
    chosen = select(args.scenarios, scenarios.BY_ID, 'scenario')
    changes = [] if args.no_planted else select(args.planted, planted.BY_ID, 'planted change')
    diagnostic = diagnostic or args.no_planted
    run = RunRoot(scratch)  # takes the build lease before any build is made or used
    try:
        build, sha = build_for(args.ref, scratch, args.allow_path)
        status = _self_check(args, run, chosen, changes, build, sha, diagnostic, started)
    except BaseException:
        run.release(keep=True)
        raise
    run.release(keep=args.keep or status != 0)
    print(f"run directory: {run.path}{' (kept)' if args.keep or status != 0 else ' (removed)'}")
    return status


def _self_check(args, run, chosen, changes, build, sha, diagnostic, started) -> int:
    builds = {'run1': build, 'run2': build, 'run3': build, 'run4': build}
    for change in changes:
        builds[f"planted-{change['id']}"] = planted_copy(build, change, run)
    results = run_corpus(builds, chosen, run, args.jobs)
    chosen, known = split(chosen)
    if args.show_sse:
        for label in ('run1', 'run2', 'run3', 'run4'):
            for s in chosen:
                sigs = [diff._signature(f) for f in results[label][s['id']]['sse']]
                print(f"SSE {label} {s['id']}: " + ' || '.join(sigs))
    print(f"# Replay harness self-check{' (DIAGNOSTIC: subset or path build, not a proof)' if diagnostic else ''}")
    print('\n'.join(manifest(sha, chosen, changes)))
    ok = True
    # run1 is the reference; run2, run3 and run4 are independent unchanged replays that
    # must show nothing (with --cross-lane-order, run1+run2 calibrate the diagnostic).
    calibration = ('run1', 'run2') if args.cross_lane_order else ()
    for a, b in (('run1', 'run2'), ('run1', 'run3'), ('run1', 'run4')):
        report = diff_builds(results, a, b, chosen, calibration)
        ok &= not report.items
        print(f"unchanged {a} vs {b}: {len(report.items)} differences  {'PASS' if not report.items else 'FAIL'}")
        if report.items:
            print('\n'.join(diff.shapes(report)))
            if args.verbose:
                print(diff.render(report, set(), f'{a} vs {b}'), end='')
    for change in changes:
        label = f"planted-{change['id']}"
        report = diff_builds(results, 'run1', label, chosen, calibration)
        hits = planted.caught(change, report.items)
        ok &= bool(hits)
        print(f"planted {change['id']}: {len(report.items)} differences, {len(hits)} specific  "
              f"{'CAUGHT' if hits else 'MISSED'}")
        if args.verbose:
            print(diff.render(report, set(), label), end='')
    broken = errors(results, chosen) + verify_unchanged({'run1': build})
    for line in known_lines(results, known, ('run1', 'run2', 'run3', 'run4')):
        print(line)
    for line in broken:
        print('RUN ERROR', line)
    print(f'seconds: {time.monotonic() - started:.1f}')
    if broken:
        print('PROOF FAILED (run errors)')
        return 2
    if diagnostic:
        print('DIAGNOSTIC ' + ('CLEAN' if ok else 'FAILED') + ' (not a proof)')
        return 1
    print('PROOF HOLDS' if ok else 'PROOF FAILED')
    return 0 if ok else 1


def cmd_clean(args) -> int:
    """Remove cached builds this harness made (registered, clean worktrees under
    scratch/builds) and finished run directories. Builds are removed only while
    holding the build lease exclusively, which no live run allows, and the
    creation lock; run directories only when no job holds their claim."""
    scratch = Path(args.scratch)
    (scratch / 'builds').mkdir(parents=True, exist_ok=True)
    with open(scratch / 'builds' / LEASE, 'w') as lease, open(scratch / 'builds' / '.lock', 'w') as lock:
        try:
            fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            print('refused to remove builds: a replay run holds the build lease')
            status = 1
        else:
            fcntl.flock(lock, fcntl.LOCK_EX)  # and no build is being created
            status = _clean_builds(scratch)
    return _clean_runs(scratch, status)


def _clean_builds(scratch: Path) -> int:
    """Called with the build lease and the creation lock held exclusively."""
    if not has_git():
        return 0
    registered = {line[len('worktree '):] for line in git('worktree', 'list', '--porcelain').splitlines()
                  if line.startswith('worktree ')}
    status = 0
    for path in sorted(p for p in (scratch / 'builds').glob('*') if not p.name.startswith('.')):
        if str(path) not in registered or path.is_symlink():
            print('refused (not a worktree this harness made):', path.name)
            status = 1
            continue
        _set_writable(path, True)
        try:
            _clean_at(path, None)
        except BuildError as exc:
            _set_writable(path, False)
            print('refused:', exc)
            status = 1
            continue
        git('worktree', 'remove', str(path))
        print('removed', path)
    git('worktree', 'prune')
    return status


def _clean_runs(scratch: Path, status: int) -> int:
    for path in sorted((scratch / 'r').glob('*')):
        if path.is_symlink() or not (path / '.claim').is_file():
            print('refused (not a run directory):', path.name)
            status = 1
        elif live(path):
            print('kept (a job is still running in it):', path.name)
        else:
            _set_writable(path, True)
            shutil.rmtree(path)
            print('removed run', path.name)
    return status


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--scratch', default=str(DEFAULT_SCRATCH))
    parser.add_argument('--jobs', type=int, default=4)
    parser.add_argument('--scenarios', help='comma-separated scenario ids (default: all; a subset is diagnostic)')
    parser.add_argument('--cross-lane-order', action='store_true',
                        help='also report cross-producer SSE order changes (diagnostic; not a contract)')
    parser.add_argument('--keep', action='store_true', help='keep the run directory (raw captures) after a clean run')
    parser.add_argument('--allow-path', action='store_true', help='accept a clean checkout path as REF (diagnostic)')
    sub = parser.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('compare')
    p.add_argument('--base', required=True)
    p.add_argument('--head', required=True)
    p.add_argument('--intended', help='file of registered difference ids, one per line')
    p = sub.add_parser('self-check')
    p.add_argument('--ref', default='HEAD')
    p.add_argument('--planted', help='comma-separated planted change ids (default: all; a subset is diagnostic)')
    p.add_argument('--verbose', action='store_true')
    p.add_argument('--no-planted', action='store_true', help='skip planted changes (diagnostic)')
    p.add_argument('--show-sse', action='store_true', help='print safe SSE signatures per run (diagnostic)')
    p = sub.add_parser('prepare')
    p.add_argument('refs', nargs='+')
    p.add_argument('--prune', action='store_true', help='remove prepared builds not named here')
    sub.add_parser('clean')
    args = parser.parse_args(argv)
    try:
        return {'compare': cmd_compare, 'self-check': cmd_self_check, 'prepare': cmd_prepare,
                'clean': cmd_clean}[args.cmd](args)
    except BuildError as exc:
        print('BUILD ERROR', exc)
        return 2


if __name__ == '__main__':
    sys.exit(main())
