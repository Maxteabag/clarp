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

Method mode (S3 mixin moves). A MOVE of the form
`server.py::Handler=lib/routes/vocab.py::VocabRoutes[,lib/routes/x.py::XRoutes]`
moves members (methods and class-level assignments) out of a class into mixin
classes:

- every member of each listed mixin at HEAD comes from the class at BASE, by
  name, is AST-equal with imports blanked, has the same per-import accounting
  as above (shared helper `pair_imports`), and lives in exactly one listed
  mixin; a member defined twice in a class fails;
- the class at HEAD equals the class at BASE minus exactly those members, in
  order; its decorators and keywords are unchanged; its bases are the listed
  mixins first, in the listed order, then the BASE bases; and at HEAD every
  moved name resolves on the class to the mixin's object (no shadowing);
- the old file is unchanged outside the class (module-level statements);
- per destination (each mixin file and the retained old file): every name the
  old file imported that the destination uses is still imported there to the
  same object; a new import fails unless it brings a listed mixin into the
  old file from its home; compiler directives equal the old file's;
- a module-level name a moved member reads that the old file defined (not
  imported) must be bound in the mixin module to the same object (so a
  look-alike fails); an unbound name fails; a builtin may not be rebound;
- a moved member may not use zero-argument `super()` or a private `__name`
  (both change meaning when the defining class changes).

Reads, in both modes: every module-level name a moved definition or member
reads resolves in its new home. A name the old file or the destination binds
by import and also rebinds (imported from two places, imported again inside a
try/if/with/for block, assigned, defined, a loop/with/except/walrus target or
deleted at module level, or written through `global` in a function, by assignment or by an import)
fails,
because `top_imports` sees only the import and the moved reader may see
another object. In top-level mode a read of a moved definition must be that
definition in its new home (imported from it, or defined in the same file);
any other changed binding must be the same object at HEAD.

Identity is checked by importing both sides in one interpreter, in a sandboxed
copy of HEAD (HOME, XDG and CLAUDE_PWA_DB point into a temp directory).

Limits: exit 0 proves definitions, import bindings and module-level reads,
not arbitrary module-level side effects of a one-to-many split, nor rebinding
from other modules (`setattr(server, ...)`, monkeypatch, `globals()[...]`);
those need source review (s3_apply's rebinding_sites covers the S3 helpers).
An augmented assignment's reads are covered by the statement-order check of a
whole-file move, not resolved one by one.
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


def pair_imports(old: str, name: str, before: ast.AST, after: ast.AST, old_package: str,
                 path: str) -> tuple[list[str], list[tuple]]:
    """The k-th import of a definition before the move pairs with its k-th import
    after: counts must match, each pair must bind the same names, and every
    changed absolute target becomes an identity pair checked at HEAD."""
    old_imps, new_imps = imports_in(before), imports_in(after)
    if len(old_imps) != len(new_imps):
        return [f"{old}: {name} has {len(old_imps)} imports, {len(new_imps)} in {path}"], []
    problems: list[str] = []
    pairs: list[tuple] = []
    for k, (b1, b2) in enumerate(zip(old_imps, new_imps)):
        b_old, b_new = bindings(b1, old_package), bindings(b2, _package(path))
        if [b[0] for b in b_old] != [b[0] for b in b_new]:
            problems.append(f"{old}: {name} import #{k + 1} binds other names in {path}")
            continue
        for (bound, m1, a1), (_, m2, a2) in zip(b_old, b_new):
            if (m1, a1) != (m2, a2):
                pairs.append((f"{name}: {bound}", (m1, a1), (m2, a2)))
    return problems, pairs


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
        found, more = pair_imports(old, name, stmt, moved, old_package, path)
        problems += found
        pairs += more

    old_homes = {name: homes[name][0] for name in old_defs if name in homes}
    for name, stmt in old_defs.items():
        if name not in homes or isinstance(stmt, ast.AugAssign):
            continue
        path = homes[name][0]
        found, more = check_reads(old, name, homes[name][1], old_tree, old, new_trees[path], path, old_homes)
        problems += found
        pairs += more

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


def _class(tree: ast.Module, name: str) -> ast.ClassDef | None:
    return next((s for s in tree.body if isinstance(s, ast.ClassDef) and s.name == name), None)


def members(cls: ast.ClassDef) -> dict[str, ast.stmt]:
    """A class body's definitions by name (methods, nested classes, assignments)."""
    found: dict[str, ast.stmt] = {}
    for stmt in cls.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            found[stmt.name] = stmt
        elif isinstance(stmt, (ast.Assign, ast.AnnAssign)):
            targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
            found[",".join(n.id for t in targets for n in ast.walk(t)
                           if isinstance(n, ast.Name))] = stmt
    return found


def _comprehension_locals(member: ast.stmt) -> set[str]:
    """Names every load of which (outside nested function, lambda and class
    bodies) sits in a comprehension that binds that name. Python 3.12 inlines
    comprehensions (PEP 709), so symtable lists their variables in the
    enclosing scope as read; they are not module reads."""
    loads: dict[str, list[bool]] = collections.defaultdict(list)

    def visit(node, bound: frozenset[str]):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)) and node is not member:
            return
        if isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            inner = set(bound)
            for gen in node.generators:
                # The first iterable is evaluated outside the comprehension.
                visit(gen.iter, bound if gen is node.generators[0] else frozenset(inner))
                # Only plain names stored by the target are bound; a target
                # like `holder.value` or `rows[i]` loads holder and rows.
                inner |= {n.id for n in ast.walk(gen.target)
                          if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
                visit(gen.target, frozenset(inner))
                for cond in gen.ifs:
                    visit(cond, frozenset(inner))
            for part in ([node.key, node.value] if isinstance(node, ast.DictComp) else [node.elt]):
                visit(part, frozenset(inner))
            return
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            loads[node.id].append(node.id in bound)
        for child in ast.iter_child_nodes(node):
            visit(child, bound)
    visit(member, frozenset())
    return {name for name, inside in loads.items() if all(inside)}


def global_reads(member: ast.stmt) -> set[str]:
    """Names a member reads from module scope: those evaluated when it is defined
    (decorators, defaults, annotations, assigned values) and symtable's implicit
    globals in every scope nested in it; imports inside the member are local."""
    import symtable
    import textwrap
    source = textwrap.dedent(ast.unparse(member))
    table = symtable.symtable(source, "<member>", "exec")
    found: set[str] = set()

    def visit(t):
        for sym in t.get_symbols():
            # symtable can report a function's own parameter or local as
            # global too (seen on 3.12 with a generator expression in the
            # body); a name bound in the scope is not a module read unless it
            # is declared `global`.
            if (sym.is_referenced() and not sym.is_imported()
                    and (sym.is_declared_global() or (sym.is_global() and not sym.is_local()))):
                found.add(sym.get_name())
        for child in t.get_children():
            visit(child)
    # Names read when the member is defined (decorators, default values,
    # annotations, a class-level assignment's value) live in the root table.
    # A name the member also binds still counts when it is read: in a class
    # body `VALUE = VALUE` reads the module's VALUE before binding its own.
    inlined = _comprehension_locals(member)
    for sym in table.get_symbols():
        if sym.is_referenced() and not sym.is_imported() and sym.get_name() not in inlined:
            found.add(sym.get_name())
    for child in table.get_children():
        visit(child)
    # Python 3.14 lists a synthetic __conditional_annotations__ for an
    # annotated assignment; nothing in the source reads it.
    return found - {"__conditional_annotations__"}


def mixin_reads(tree: ast.Module) -> set[str]:
    """Names a mixin file uses from its module scope: the module reads of every
    member of its classes (global_reads, so a method's own local that happens to
    share an imported module's name is not one), every name a member declares
    `global` (a pure global write or delete reads nothing but changes the
    module's binding), and every name in its other module-level statements,
    imports aside."""
    found: set[str] = set()
    for stmt in tree.body:
        if isinstance(stmt, _IMPORTS):
            continue
        if isinstance(stmt, ast.ClassDef):
            found |= {n.id for part in (*stmt.bases, *stmt.keywords, *stmt.decorator_list)
                      for n in ast.walk(part) if isinstance(n, ast.Name)}
            for member in stmt.body:
                found |= global_reads(member)
                # A pure `global name` write or delete reads nothing, yet it
                # changes the module that binds the name: it counts as use.
                found |= {name for node in ast.walk(member) if isinstance(node, ast.Global)
                          for name in node.names}
        else:
            found |= used_names(stmt)
    return found


def module_binding(tree: ast.Module, path: str, name: str) -> tuple[str, str | None] | None:
    """Where a module binds `name`: an import target, or (module, name) for a
    module-level definition; None when the module does not bind it."""
    imported = top_imports(tree, _package(path))
    if name in imported:
        return imported[name]
    for stmt in tree.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and stmt.name == name:
            return (_module(path), name)
        if isinstance(stmt, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
            if any(isinstance(n, ast.Name) and n.id == name for t in targets for n in ast.walk(t)):
                return (_module(path), name)
    return None


def _module_scope_imports(tree: ast.Module, package: str) -> dict[str, set[tuple[str, str | None]]]:
    """Every import binding at module scope, nested blocks (try/if/with/for)
    included and function or class bodies excluded: bound name -> targets."""
    found: dict[str, set[tuple[str, str | None]]] = collections.defaultdict(set)

    def visit(node):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                continue
            if isinstance(child, _IMPORTS):
                if not (isinstance(child, ast.ImportFrom) and child.module == "__future__"):
                    for name, module, attr in bindings(child, package):
                        found[name].add((module, attr))
            visit(child)
    visit(tree)
    return found


def rebound_imports(tree: ast.Module, path: str) -> dict[str, str]:
    """Names a module binds by import and also binds some other way, so the
    object a reader sees at run time need not be the import's: imported from
    two different places, imported again inside a block, assigned, defined,
    used as a loop/with/except/walrus target or deleted at module level, or
    written through `global` in any function. A moved reader of such a name
    may see a different object in its new home, which top_imports alone
    cannot tell."""
    import symtable
    source = ast.unparse(tree)
    imports = _module_scope_imports(tree, _package(path))
    top = {name for s in tree.body if isinstance(s, _IMPORTS)
           and not (isinstance(s, ast.ImportFrom) and s.module == "__future__")
           for name, _, _ in bindings(s, _package(path))}
    found: dict[str, str] = {}
    for name, targets in imports.items():
        if len(targets) > 1:
            found[name] = f"imported from {len(targets)} different places"
    blocks = (ast.If, ast.Try, ast.With, ast.For, ast.While, ast.AsyncWith, ast.AsyncFor)
    for stmt in tree.body:
        if isinstance(stmt, blocks):
            for name in _module_scope_imports(ast.Module(body=[stmt], type_ignores=[]), _package(path)):
                if name in top:
                    found.setdefault(name, "imported again inside a block")
    table = symtable.symtable(source, path, "exec")
    for sym in table.get_symbols():
        if sym.get_name() in imports and sym.is_assigned():
            found.setdefault(sym.get_name(), "also bound at module level (assignment, definition, target or del)")

    def visit(t):
        for sym in t.get_symbols():
            # A `global` name rebound by assignment or by `import ... as name`.
            if sym.get_name() in imports and sym.is_declared_global() and (sym.is_assigned() or sym.is_imported()):
                found.setdefault(sym.get_name(), f"written through `global` in {t.get_name()}")
        for child in t.get_children():
            visit(child)
    for child in table.get_children():
        visit(child)
    return found


# Implicit module attributes: bound by the import system, never by a statement.
MODULE_IDENTITY = frozenset({"__file__", "__name__", "__spec__", "__package__", "__loader__",
                             "__path__", "__cached__", "__builtins__"})


def check_reads(old: str, name: str, stmt: ast.stmt, base_tree: ast.Module, old_path: str,
                tree: ast.Module, path: str, homes: dict[str, str]) -> tuple[list[str], list[tuple]]:
    """The module-level names a moved definition reads, resolved in its new
    home: a name either side rebinds after importing it fails; an unbound or
    newly bound name fails; a moved definition must be the object in its home;
    any other changed binding must be the same object at HEAD."""
    import builtins
    problems: list[str] = []
    pairs: list[tuple] = []
    rebound_old, rebound_new = rebound_imports(base_tree, old_path), rebound_imports(tree, path)
    for read in sorted(global_reads(stmt)):
        for where, rebound in ((old_path, rebound_old), (path, rebound_new)):
            if read in rebound:
                problems.append(f"{old}: {name} reads {read}, which {where} imports and rebinds "
                                f"({rebound[read]}): the moved reader may see another object")
        was = module_binding(base_tree, old_path, read)
        now = module_binding(tree, path, read)
        if read in MODULE_IDENTITY and was is None and now is None:
            # An implicit module attribute (__file__, __name__, ...): the same
            # value only while the definition stays in its own module (S9).
            if path != old_path:
                problems.append(f"{old}: {name} reads {read}, which differs in {path}")
        elif was is None and hasattr(builtins, read):
            if now is not None:
                problems.append(f"{old}: {path} binds builtin {read} that {name} reads")
        elif now is None:
            problems.append(f"{old}: {name} reads {read}, which {path} does not bind")
        elif was is None:
            problems.append(f"{old}: {name} reads {read}, which {old_path} did not bind at BASE")
        elif read in homes:
            if homes[read] != path:
                if now != (_module(homes[read]), read):
                    pairs.append((f"{path}: {read} (read by {name})", (_module(homes[read]), read), now))
            elif now != (_module(path), read):
                problems.append(f"{old}: {name} reads {read}, moved to {path}, but {path} binds it elsewhere")
        elif was != now:
            pairs.append((f"{path}: {read} (read by {name})", was, now))
    return problems, pairs


def _base_label(node: ast.expr) -> str:
    return ast.unparse(node)


def _duplicates(cls: ast.ClassDef) -> list[str]:
    names = collections.Counter()
    for stmt in cls.body:
        names.update(_one(stmt).keys())
    return sorted(n for n, count in names.items() if count > 1)


def _spec(text: str) -> tuple[str, str]:
    path, sep, name = text.partition("::")
    if not sep or not name:
        raise ValueError(f"method mode needs path.py::Class, got {text!r}")
    return path, name


def compare_methods(base: str, head: str, old: str, new: list[str]) -> tuple[list[str], list[tuple]]:
    """Method mode (`old.py::Class=new.py::Mixin[,...]`): members moved out of a
    class into mixin classes. The old file stays; everything in it outside the
    moved members must be unchanged."""
    import builtins
    old_path, old_name = _spec(old)
    old_package = _package(old_path)
    base_tree = ast.parse(_source(base, old_path))
    head_tree = ast.parse(_source(head, old_path))
    problems: list[str] = []
    pairs: list[tuple] = []
    cls_base, cls_head = _class(base_tree, old_name), _class(head_tree, old_name)
    if cls_base is None or cls_head is None:
        return [f"{old}: class {old_name} not found at {'BASE' if cls_base is None else 'HEAD'}"], []
    base_members = members(cls_base)
    old_top = top_imports(base_tree, old_package)
    rebound_old = rebound_imports(base_tree, old_path)

    mixins: list[tuple[str, str, ast.Module, ast.ClassDef]] = []
    for text in new:
        path, mixin_name = _spec(text)
        tree = ast.parse(_source(head, path))
        cls = _class(tree, mixin_name)
        if cls is None:
            problems.append(f"{old}: mixin {mixin_name} not found in {path}")
            continue
        mixins.append((path, mixin_name, tree, cls))
    mixin_names = [m[1] for m in mixins]

    # Members: each moved one verbatim, from the class at BASE, in one mixin.
    moved: dict[str, tuple[str, str]] = {}
    for path, mixin_name, tree, cls in mixins:
        for name in _duplicates(cls):
            problems.append(f"{old}: {name} is defined twice in {path}::{mixin_name}")
        for name, stmt in members(cls).items():
            if name in moved:
                problems.append(f"{old}: {name} is in both {moved[name][0]} and {path}")
                continue
            moved[name] = (path, mixin_name)
            if name not in base_members:
                problems.append(f"{old}: {path}::{mixin_name}.{name} did not exist in {old_name} at BASE")
                continue
            before = base_members[name]
            if blanked(before) != blanked(stmt):
                problems.append(f"{old}: {old_name}.{name} differs in {path}::{mixin_name}")
                continue
            for node in ast.walk(stmt):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                        and node.func.id == "super" and not node.args):
                    problems.append(f"{old}: {name} uses zero-argument super(), which changes meaning in a mixin")
                ident = (node.attr if isinstance(node, ast.Attribute)
                         else node.id if isinstance(node, ast.Name) else "")
                if ident.startswith("__") and not ident.endswith("__"):
                    problems.append(f"{old}: {name} uses private name {ident}, mangled differently in a mixin")
            found, more = pair_imports(old, name, before, stmt, old_package, path)
            problems += found
            pairs += more
            # Module-level names it reads that the old module defined (not
            # imported: the import rule below covers those) or builtins.
            for read in sorted(global_reads(stmt)):
                for where, rebound in ((old_path, rebound_old), (path, rebound_imports(tree, path))):
                    if read in rebound:
                        problems.append(f"{old}: {name} reads {read}, which {where} imports and rebinds "
                                        f"({rebound[read]}): the moved reader may see another object")
                if read in old_top:
                    continue
                was = module_binding(base_tree, old_path, read)
                now = module_binding(tree, path, read)
                if was is None and hasattr(builtins, read):
                    if now is not None:
                        problems.append(f"{old}: {path} binds builtin {read} that {name} reads")
                elif now is None:
                    problems.append(f"{old}: {name} reads {read}, which {path} does not bind")
                elif was is None:
                    problems.append(f"{old}: {name} reads {read}, which {old_path} did not bind at BASE")
                elif was != now:
                    pairs.append((f"{path}: {read} (read by {name})", was, now))
            # Resolution at HEAD: the class attribute must be the moved member.
            pairs.append((f"{old_name}.{name} resolves to {mixin_name}.{name}",
                          (_module(path), f"{mixin_name}.{name}"), (_module(old_path), f"{old_name}.{name}")))

    # The class at HEAD: BASE minus exactly the moved members, mixins first.
    for name in _duplicates(cls_head):
        problems.append(f"{old}: {name} is defined twice in {old_path}::{old_name}")
    remaining = [blanked(s) for s in cls_base.body if not (_one(s).keys() & moved.keys())]
    if remaining != [blanked(s) for s in cls_head.body]:
        problems.append(f"{old}: {old_name} at HEAD differs from {old_name} at BASE beyond the moved members")
    if ([blanked(d) for d in cls_base.decorator_list] != [blanked(d) for d in cls_head.decorator_list]
            or [blanked(k) for k in cls_base.keywords] != [blanked(k) for k in cls_head.keywords]):
        problems.append(f"{old}: {old_name} decorators or keywords changed")
    expected_bases = mixin_names + [ast.unparse(b) for b in cls_base.bases]
    head_bases = [ast.unparse(b) for b in cls_head.bases]
    if head_bases != expected_bases:
        problems.append(f"{old}: {old_name} bases {head_bases}; expected the listed mixins first, "
                        f"in order, then the BASE bases: {expected_bases}")

    # The rest of the old file: module-level statements unchanged.
    def outside(tree):
        return [("<class>" if isinstance(s, ast.ClassDef) and s.name == old_name else blanked(s))
                for s in tree.body if not isinstance(s, _IMPORTS)]
    if outside(base_tree) != outside(head_tree):
        problems.append(f"{old}: {old_path} differs outside {old_name} (module-level statements)")

    # Imports and compiler directives, per destination (mixin files and the
    # retained old file): a name the old file imported that a destination uses
    # must still be bound there to the same object; a new import fails unless
    # it brings a listed mixin into the old file from its home.
    homes = {mixin_name: path for path, mixin_name, _, _ in mixins}
    for path, tree in [(old_path, head_tree)] + [(m[0], m[2]) for m in mixins]:
        if futures(tree) != futures(base_tree):
            problems.append(f"{old}: compiler directives differ in {path}: "
                            f"{sorted(futures(base_tree))} -> {sorted(futures(tree))}")
        new_top = top_imports(tree, _package(path))
        used = used_names(tree) if path == old_path else mixin_reads(tree)
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
            if path == old_path and bound in homes:
                pairs.append((f"{path}: {bound}", (_module(homes[bound]), bound), target))
            else:
                problems.append(f"{old}: {path} adds an import of {bound} that {old_path} did not have")
    return problems, pairs


def _one(stmt: ast.stmt) -> dict[str, ast.stmt]:
    holder = ast.ClassDef(name="_", bases=[], keywords=[], body=[stmt], decorator_list=[])
    return members(holder)


_IDENTITY = """
import importlib, json, sys
sys.path.insert(0, sys.argv[1])
def get(spec):
    module, attr = spec
    try:
        obj = importlib.import_module(module)
        for part in (attr.split(".") if attr else []):
            obj = getattr(obj, part)
        return obj
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
        check = compare_methods if "::" in old else compare
        found, imports = check(base, head, old, new.split(","))
        problems += found
        pairs += imports
    problems += identical_objects(head, pairs)
    for line in problems:
        print(line)
    print(f"ast_identity: {len(moves)} moves, {len(pairs)} changed imports, {len(problems)} problems")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
