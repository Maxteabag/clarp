#!/usr/bin/env python3
"""AST identity gate for move-only commits (docs/architecture/s1-layering.md, section 5).

    python scripts/refactor/ast_identity.py [--root REPO] BASE HEAD MOVE [MOVE ...]

Each MOVE is `old/path.py=new/path.py[,new/other.py]` (server-relative, for
example `lib/db_schema.py=lib/persistence/schema.py`).

Definitions. Every top-level function, class and assignment of the old file at
BASE must exist under the same name in exactly one new file at HEAD, and its
AST must be equal with every import statement inside it blanked out: no
renames, nothing else normalized. A repeated augmented assignment
(`_SCHEMA_SQL += ...`) is matched by its occurrence number. A whole-file move
(one new file) must also keep the order of its non-import statements.

Imports, the only thing allowed to differ, are checked one occurrence at a time:

- Inside a definition, the k-th import of the old definition pairs with the
  k-th import of the new one (imports are blanked to a slot no source can
  contain, the blanked ASTs must be equal and the counts must match, so the
  positions correspond). Each pair must bind the same names; where a binding's absolute
  target changed (`from .dependency import VALUE` moved one package deeper to
  `from ..dependency import VALUE` resolves to the same target and passes), the
  two targets must be the same object at HEAD.
- At module level, every name the old file bound by import must still be bound
  by import in each new file that uses it (a dropped required import fails),
  and to the same object. A new module-level import is allowed only when it
  brings in a moved definition from its new home (also checked by identity).
- A definition that left the old file and is imported back there must be the
  object defined in its new home. A retained old file (listed among the new
  files) gets the same required-import checks as any other.
- The `from __future__` directives of every file that receives a moved
  definition must equal the old file's: they change how an unchanged
  definition executes (annotations, for example).

Identity is checked by importing both sides in one interpreter, in a sandboxed
copy of HEAD (HOME, XDG and CLAUDE_PWA_DB point into a temp directory).

Limits: exit 0 proves definitions and import bindings, not arbitrary
module-level side effects of a one-to-many split; those need source review.
"""
from __future__ import annotations

import ast
import collections
import copy
import json
import os
import pathlib
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[2]
_IMPORTS = (ast.Import, ast.ImportFrom)


def _source(ref: str, path: str) -> str:
    return subprocess.run(["git", "-C", str(ROOT), "show", f"{ref}:server/{path}"],
                          check=True, capture_output=True, text=True).stdout


def _module(path: str) -> str:
    return path[:-3].replace("/", ".").removesuffix(".__init__")


def _package(path: str) -> str:
    module = _module(path)
    return module if path.endswith("__init__.py") else module.rpartition(".")[0]


# What a blanked import becomes: a name no source can contain, so no real
# statement (`pass` included) can stand in for a removed import.
_SLOT = "\x00import slot"


class _Blank(ast.NodeTransformer):
    def visit_Import(self, node):
        return ast.Expr(ast.Name(id=_SLOT, ctx=ast.Load()))

    visit_ImportFrom = visit_Import


def blanked(node: ast.AST) -> str:
    """ast.dump with every import statement replaced by an unforgeable slot."""
    return ast.dump(_Blank().visit(copy.deepcopy(node)))


def imports_in(node: ast.AST) -> list[ast.stmt]:
    """Import statements in a definition, in source order (depth first)."""
    found = []

    def visit(n):
        if isinstance(n, _IMPORTS):
            found.append(n)
        for child in ast.iter_child_nodes(n):
            visit(child)
    visit(node)
    return found


def bindings(node: ast.stmt, package: str) -> list[tuple[str, str, str | None]]:
    """(bound name, absolute module, attribute or None) for one import statement."""
    if isinstance(node, ast.Import):
        return [(a.asname or a.name.split(".")[0], a.asname and a.name or a.name.split(".")[0], None)
                for a in node.names]
    if node.level:
        parts = package.split(".") if package else []
        parts = parts[:len(parts) - (node.level - 1)] if node.level > 1 else parts
        module = ".".join([*parts, *([node.module] if node.module else [])])
    else:
        module = node.module or ""
    return [(a.asname or a.name, module, a.name) for a in node.names]


def definitions(tree: ast.Module) -> dict[str, ast.stmt]:
    found: dict[str, ast.stmt] = {}
    seen: collections.Counter[str] = collections.Counter()
    for stmt in tree.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            found[stmt.name] = stmt
        elif isinstance(stmt, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
            name = ",".join(n.id for t in targets for n in ast.walk(t) if isinstance(n, ast.Name))
            if isinstance(stmt, ast.AugAssign):
                seen[name] += 1
                name = f"{name} (augmented #{seen[name]})"
            found[name] = stmt
    return found


def futures(tree: ast.Module) -> frozenset[str]:
    """Compiler directives (`from __future__ import ...`): they change how an
    unchanged definition executes, so they are compared, not import-accounted."""
    return frozenset(a.name for s in tree.body if isinstance(s, ast.ImportFrom)
                     and s.module == "__future__" for a in s.names)


def top_imports(tree: ast.Module, package: str) -> dict[str, tuple[str, str | None]]:
    out = {}
    for stmt in tree.body:
        if isinstance(stmt, ast.ImportFrom) and stmt.module == "__future__":
            continue
        if isinstance(stmt, _IMPORTS):
            for name, module, attr in bindings(stmt, package):
                out[name] = (module, attr)
    return out


def used_names(tree: ast.AST) -> set[str]:
    return {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}


def compare(base: str, head: str, old: str, new: list[str]) -> tuple[list[str], list[tuple]]:
    """Problems found, and (label, before, after) target pairs to check for identity at HEAD."""
    old_tree = ast.parse(_source(base, old))
    new_trees = {path: ast.parse(_source(head, path)) for path in new}
    old_defs = definitions(old_tree)
    problems: list[str] = []
    pairs: list[tuple] = []
    homes: dict[str, tuple[str, ast.stmt]] = {}
    for path, tree in new_trees.items():
        for name, stmt in definitions(tree).items():
            if name in old_defs and name in homes:
                problems.append(f"{old}: {name} is defined in both {homes[name][0]} and {path}")
            homes.setdefault(name, (path, stmt))

    old_package = _package(old)
    for name, stmt in old_defs.items():
        if name not in homes:
            problems.append(f"{old}: {name} is missing from {', '.join(new)}")
            continue
        path, moved = homes[name]
        if blanked(stmt) != blanked(moved):
            problems.append(f"{old}: {name} differs in {path}")
            continue
        old_imps, new_imps = imports_in(stmt), imports_in(moved)
        if len(old_imps) != len(new_imps):
            problems.append(f"{old}: {name} has {len(old_imps)} imports, {len(new_imps)} in {path}")
            continue
        for k, (before, after) in enumerate(zip(old_imps, new_imps)):
            b_old, b_new = bindings(before, old_package), bindings(after, _package(path))
            if [b[0] for b in b_old] != [b[0] for b in b_new]:
                problems.append(f"{old}: {name} import #{k + 1} binds other names in {path}")
                continue
            for (bound, m1, a1), (_, m2, a2) in zip(b_old, b_new):
                if (m1, a1) != (m2, a2):
                    pairs.append((f"{name}: {bound}", (m1, a1), (m2, a2)))

    if len(new_trees) == 1:
        (path, tree), = new_trees.items()
        keep = [blanked(s) for s in old_tree.body if not isinstance(s, _IMPORTS)]
        if keep != [blanked(s) for s in tree.body if not isinstance(s, _IMPORTS)]:
            problems.append(f"{old}: module-level statement order or content differs in {path}")

    hosts = {homes[name][0] for name in old_defs if name in homes}
    for path, tree in new_trees.items():
        if path in hosts and futures(tree) != futures(old_tree):
            problems.append(f"{old}: compiler directives differ in {path}: "
                            f"{sorted(futures(old_tree))} -> {sorted(futures(tree))}")

    old_top = top_imports(old_tree, old_package)
    for path, tree in new_trees.items():
        new_top = top_imports(tree, _package(path))
        used = used_names(tree)
        for bound, target in old_top.items():
            if bound not in used:
                continue
            if bound not in new_top:
                problems.append(f"{old}: {path} uses {bound} but no longer imports it")
            elif new_top[bound] != target:
                pairs.append((f"{path}: {bound}", target, new_top[bound]))
        for bound, target in new_top.items():
            if bound in old_top:
                continue
            if bound in homes and homes[bound][0] != path:
                # A moved definition imported from its new home (also back into a
                # retained old file): it must be the very object defined there.
                pairs.append((f"{path}: {bound}", (_module(homes[bound][0]), bound), target))
            else:
                problems.append(f"{old}: {path} adds an import of {bound} that the old file did not have")
    return problems, pairs


_IDENTITY = """
import importlib, json, sys
sys.path.insert(0, sys.argv[1])
def get(spec):
    module, attr = spec
    try:
        obj = importlib.import_module(module)
        return getattr(obj, attr) if attr else obj
    except Exception as exc:
        return exc
bad = []
for label, before, after in json.loads(sys.argv[2]):
    a, b = get(before), get(after)
    if isinstance(a, Exception) or isinstance(b, Exception) or a is not b:
        bad.append(label)
print(json.dumps(bad))
"""


def identical_objects(head: str, pairs: list[tuple]) -> list[str]:
    """Labels of target pairs that are not the same object at HEAD."""
    if not pairs:
        return []
    with tempfile.TemporaryDirectory(prefix="ast-identity-", dir="/var/tmp") as work:
        tree = pathlib.Path(work)
        archive = subprocess.run(["git", "-C", str(ROOT), "archive", head, "server"],
                                 check=True, capture_output=True).stdout
        subprocess.run(["tar", "-x", "-C", str(tree)], input=archive, check=True)
        # Importing server modules must never reach the live data or config.
        env = {**os.environ, "HOME": str(tree / "home"), "CLAUDE_PWA_DB": str(tree / "state.sqlite"),
               **{k: str(tree / "home" / k.lower()) for k in
                  ("XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME")}}
        out = subprocess.run([sys.executable, "-c", _IDENTITY, str(tree / "server"), json.dumps(pairs)],
                             check=True, capture_output=True, text=True, env=env, cwd=tree).stdout
        return [f"import {label}: not the same object at {head}" for label in json.loads(out)]


def main(argv: list[str]) -> int:
    global ROOT
    if argv[:1] == ["--root"]:
        ROOT, argv = pathlib.Path(argv[1]), argv[2:]
    if len(argv) < 3:
        print(__doc__, file=sys.stderr)
        return 2
    base, head, *moves = argv
    problems, pairs = [], []
    for move in moves:
        old, _, new = move.partition("=")
        found, imports = compare(base, head, old, new.split(","))
        problems += found
        pairs += imports
    problems += identical_objects(head, pairs)
    for line in problems:
        print(line)
    print(f"ast_identity: {len(moves)} moves, {len(pairs)} changed imports, {len(problems)} problems")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
