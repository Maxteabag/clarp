"""Rule 6 of docs/architecture/state-and-boundaries.md: policies are pure.

A module under server/lib/policies/ may import the standard library's value
modules and a short list of constant-only server modules. Anything that can
reach a database, the clock, the filesystem or a subprocess fails here; the IO
code gathers those facts and passes them in.
"""
from __future__ import annotations

import ast
import pathlib

POLICIES = pathlib.Path(__file__).resolve().parents[2] / "server" / "lib" / "policies"

STDLIB = frozenset({"__future__", "dataclasses", "typing", "enum", "base64", "re",
                    "collections", "functools", "itertools", "secrets"})
# Server modules that hold constants and pure helpers only.
PURE_LIB = frozenset({"origins", "roster", "voice"})
# secrets is allowed for one reason: agent_spec's injectable random_suffix
# default. A policy never calls it on its own path.
BANNED_CALLS = frozenset({"open", "print", "input"})


def _imports(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield 0, alias.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                yield 0, (node.module or "").split(".")[0]
            elif node.module:
                yield min(node.level, 2), node.module.split(".")[0]
            else:
                for alias in node.names:
                    yield min(node.level, 2), alias.name


def test_policies_import_only_pure_modules():
    problems = []
    for path in sorted(POLICIES.glob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for level, name in _imports(tree):
            ok = (name in STDLIB if level == 0
                  else True if level == 1  # sibling policy modules
                  else name in PURE_LIB)
            if not ok:
                problems.append(f"{path.name}: imports {'.' * level}{name}")
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id in BANNED_CALLS):
                problems.append(f"{path.name}:{node.lineno}: calls {node.func.id}()")
    assert not problems, "\n".join(problems)


def test_the_scanner_catches_an_io_import(tmp_path):
    tree = ast.parse("from .. import db\nimport time\nfrom . import origins\n")
    assert sorted(_imports(tree)) == [(0, "time"), (1, "origins"), (2, "db")]
