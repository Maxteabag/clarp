#!/usr/bin/env python3
"""Check scratch ownership and reproduce generators with this Python interpreter.

The Server (Rust) workflow has no Python step. Run this standalone with
the Python Host interpreter, from the workspace checkout.

Run --static for the source check alone. The default runs every generator in
a fresh TMPDIR. --proof runs each twice with TMPDIR and twice without it.
Run in a checkout with its reference fixtures unmodified. Stop on the first
changed output so a later generation cannot conceal the difference.
"""
import argparse
import ast
import os
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scratch

TOOLS = Path(__file__).resolve().parent
SERVER = TOOLS.parent
REPO = SERVER.parent
FORBIDDEN = {'mkdtemp', 'mkstemp', 'TemporaryDirectory'}


def fixed_scratch(tree, *, generator):
    """Recognize literal temporary paths used as directory creation/removal targets.

    A logical path in a fixture is data. It becomes a scratch hazard only
    when used to create or clear the directory itself.
    """
    names = {}

    def literal(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return Path(node.value)
        if isinstance(node, ast.Name):
            return names.get(node.id)
        if isinstance(node, ast.Call) and node.args and ast.unparse(node.func) in ('Path', 'pathlib.Path'):
            return literal(node.args[0])
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            parent, child = literal(node.left), literal(node.right)
            if parent is not None and child is not None:
                return parent / child
        return None

    for node in sorted(ast.walk(tree), key=lambda n: getattr(n, 'lineno', 0)):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names[target.id] = literal(node.value)
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == 'mkdir' and ast.unparse(func.value) != 'os':
                target = literal(func.value)
            elif ast.unparse(func) in ('os.mkdir', 'os.makedirs', 'shutil.rmtree') and node.args:
                target = literal(node.args[0])
            else:
                continue
            # A long-lived tool may own a fixed output or lock directory.
            # A generator's mkdir, or any tool clearing a fixed temp tree,
            # is different: two concurrent runs would share that work area.
            if not generator and ast.unparse(func) != 'shutil.rmtree':
                continue
            if target and any(target.is_relative_to(root) for root in ('/var/tmp', '/tmp')):
                return node.lineno
    return None


def source_check():
    for example in ("ROOT = Path('/var/tmp/clarp-shared'); ROOT.mkdir()",
                    "shutil.rmtree(pathlib.Path('/tmp') / 'shared')"):
        if not fixed_scratch(ast.parse(example), generator=True):
            raise SystemExit('scratch guard failed its fixed-directory self-check')
    if fixed_scratch(ast.parse("label = Path('/var/tmp/fixture'); print(label)"), generator=True):
        raise SystemExit('scratch guard mistook a fixture label for an allocation')
    examined = 0
    for path in sorted(SERVER.rglob('*.py')):
        relative = path.relative_to(SERVER)
        if path == TOOLS / 'scratch.py' or 'target' in relative.parts:
            continue
        tree = ast.parse(path.read_bytes(), filename=str(path))
        if line := fixed_scratch(tree, generator=path.name.endswith("-vectors.py")):
            raise SystemExit(f'{relative}:{line}: fixed scratch path; use tools/scratch.py')
        for node in ast.walk(tree):
            if ((isinstance(node, ast.Attribute) and node.attr in FORBIDDEN)
                    or (isinstance(node, ast.ImportFrom) and node.module == 'tempfile'
                        and any(n.name in FORBIDDEN or n.name == '*' for n in node.names))):
                raise SystemExit(f'{path.relative_to(REPO)}:{node.lineno}: use tools/scratch.py')
        examined += 1
    print(f'scratch guard: {examined} Python files checked; no direct allocation or fixed scratch directory; no deferred files', flush=True)


def unchanged(generator):
    # Check the repository outputs, including generated Rust tables. Tools
    # themselves can be under development when running the guard locally.
    paths = ['.', ':(exclude)server-rs/tools', ':(exclude)server-rs/conformance/conform.py']
    changed = subprocess.check_output(['git', 'diff', '--name-only', 'HEAD', '--', *paths], cwd=REPO, text=True)
    changed += subprocess.check_output(['git', 'ls-files', '--others', '--exclude-standard', '--', *paths], cwd=REPO, text=True)
    if changed.strip():
        raise SystemExit(f'{generator}: fixture changed; stopped:\n{changed.rstrip()}')


def check_scope():
    """Prove both parent selections, and cleanup as SystemExit unwinds."""
    from unittest.mock import patch

    with scratch.directory('cg-') as box:
        for env, wanted in [({}, Path('/var/tmp')), ({'TMPDIR': box}, Path(box))]:
            with patch.dict(os.environ, env, clear=True):
                try:
                    with scratch.directory() as name:
                        owned = Path(name)
                        if owned.parent != wanted.resolve():
                            raise SystemExit(f'wrong scratch parent: {owned}')
                        (owned / 'partial').write_bytes(b'fixture')
                        sys.exit(23)
                except SystemExit as exit_:
                    if exit_.code != 23:
                        raise
                if owned.exists():
                    raise SystemExit(f'scratch survived SystemExit: {owned}')


def exercise(script, private, *, failing=False):
    with scratch.directory('cg-') as box:
        area = Path(box)
        home = area / 'home'
        home.mkdir()
        parent = area / 'tmp'
        parent.mkdir()
        env = {'PATH': os.defpath, 'LANG': 'C.UTF-8', 'TZ': 'UTC', 'HOME': str(home)}
        if private:
            env['TMPDIR'] = str(parent)
        args = [sys.executable, str(script)]
        if failing:
            args += ['--case', 'missing-fixture-for-the-scratch-guard']
        result = subprocess.run(args, env=env, cwd=REPO, capture_output=True)
        unchanged(script.name)
        leftovers = sorted(p.name for p in parent.iterdir())
        if leftovers:
            raise SystemExit(f'{script.name}: TMPDIR not empty: {leftovers}')
        if failing:
            if result.returncode == 0 or b'FileNotFoundError' not in result.stderr:
                raise SystemExit(f'{script.name}: the injected missing case did not fail as expected')
        elif result.returncode:
            raise SystemExit(f'{script.name} failed ({result.returncode}):\n{result.stderr.decode(errors="replace")[-2500:]}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--static', action='store_true')
    mode.add_argument('--proof', action='store_true')
    args = parser.parse_args()
    source_check()
    if args.static:
        return
    check_scope()
    unchanged('before generation')
    exercise(TOOLS / 'seats-vectors.py', True, failing=True)
    generators = sorted([*TOOLS.glob('*-vectors.py'), TOOLS / 'casefold-table.py'])
    for script in generators:
        for private in ([True, True, False, False] if args.proof else [True]):
            exercise(script, private)
        print(f'{script.name}: unchanged', flush=True)
    print(f'scratch guard: {len(generators)} generators; fixture bytes unchanged; private TMPDIRs empty, including after failure', flush=True)


if __name__ == '__main__':
    main()
