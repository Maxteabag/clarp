"""Patching a module-level name for a test across the module's S9 split family.

An S9 split (tools/refactor/split_apply.py) moves definitions out of a module
into parts and leaves the old module as a facade. A moved definition then reads
its names from its part's globals, so `monkeypatch.setattr(old, name, value)`
no longer reaches it. `patch_shared` sets the name on the old module and on
every part its split and method recipes list that exists and holds the same object, and
on nothing else: a shared helper such as `now_ms` stays untouched in modules
outside the family. Where the split is not applied (main), the family is the
module alone and this is exactly `monkeypatch.setattr(module, name, value)`.
"""
from __future__ import annotations

import ast
import importlib
import pathlib
import tomllib
import types
from collections.abc import Iterable, Iterator

_REPO = pathlib.Path(__file__).resolve().parents[1]
_RECIPES = _REPO / "tools" / "refactor" / "splits"
_MISSING = object()


def split_family(module: types.ModuleType, recipes: pathlib.Path | None = None, *,
                 methods: pathlib.Path | None = None) -> list[types.ModuleType]:
    """The parts the split recipes say came from `module`, as imported modules;
    parts whose file does not exist (the split is not applied) are left out."""
    recipes = recipes or _RECIPES
    name = module.__name__
    if not name.startswith("lib."):
        return []
    old = name.replace(".", "/") + ".py"
    parts: list[types.ModuleType] = []
    for path in sorted(recipes.glob("*.toml")) if recipes.is_dir() else ():
        for split in tomllib.loads(path.read_text()).get("split", []):
            if split.get("module") != old:
                continue
            for part in split.get("part", []):
                rel = str(part.get("module", ""))
                if not (_REPO / "server" / rel).exists():
                    continue
                parts.append(importlib.import_module(rel[:-3].replace("/", ".")))
    # Named-class moves copy module imports into mixins. Include method
    # consumers of both the original facade and its already applied parts.
    reachable = {old, *(part.__name__.replace(".", "/") + ".py" for part in parts)}
    methods = methods if methods is not None else recipes.parent / "methods"
    for path in sorted(methods.glob("*.toml")) if methods.is_dir() else ():
        data = tomllib.loads(path.read_text())
        if data.get("source_module") not in reachable:
            continue
        for group in data.get("group", []):
            rel = group["module"]
            if (_REPO / "server" / rel).exists():
                part = importlib.import_module(rel[:-3].replace("/", "."))
                if part not in parts:
                    parts.append(part)
    return parts


def split_origin(path: str, name: str | None = None, recipes: pathlib.Path | None = None, *,
                 methods: pathlib.Path | None = None) -> str:
    """The old module a split part came from, in the same spelling as `path`
    ("server/lib/x.py" or "lib/x.py"); `path` itself when no recipe lists it.
    With `name`, only when the recipe moved exactly that name into this part:
    anything else defined in the part keeps its own path (new state is new).
    Lets path-based guards keep naming the old module on integration."""
    recipes = recipes or _RECIPES
    prefix = "server/" if path.startswith("server/") else ""
    rel = path[len(prefix):]
    methods = methods if methods is not None else recipes.parent / "methods"
    for recipe in sorted(methods.glob("*.toml")) if methods.is_dir() else ():
        data = tomllib.loads(recipe.read_text())
        for group in data.get("group", []):
            if group["module"] == rel and (name is None or name in group.get("members", [])):
                rel = data["source_module"]
    for recipe in sorted(recipes.glob("*.toml")) if recipes.is_dir() else ():
        for split in tomllib.loads(recipe.read_text()).get("split", []):
            for part in split.get("part", []):
                if part.get("module") == rel and (name is None or name in part.get("names", [])):
                    return prefix + str(split["module"])
    return prefix + rel


def patch_shared(monkeypatch, module: types.ModuleType, name: str, value,
                 recipes: pathlib.Path | None = None, *, raising: bool = True,
                 methods: pathlib.Path | None = None) -> None:
    """monkeypatch.setattr(module, name, value, raising=raising), and the same on
    each part of the module's split family that holds the object the module holds."""
    original = getattr(module, name, _MISSING)
    monkeypatch.setattr(module, name, value, raising=raising)
    if original is _MISSING:
        return
    for part in split_family(module, recipes, methods=methods):
        if getattr(part, name, _MISSING) is original:
            monkeypatch.setattr(part, name, value)


def _bound_name(stmt: ast.stmt) -> str | None:
    """The one name a top-level statement defines, or None."""
    if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return stmt.name
    if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
        return getattr(stmt.targets[0], "id", None)
    if isinstance(stmt, ast.AnnAssign):
        return getattr(stmt.target, "id", None)
    return None


def logical_split_trees(rel: str, tree: ast.Module, recipes: pathlib.Path | None = None
                        ) -> Iterator[tuple[str, ast.Module]]:
    """(origin, module) pairs for one parsed file, for source guards.

    A file no split recipe lists yields itself: exactly (rel, tree), the same
    Module object. In an S9 split part, a top-level definition the recipe
    moved there by name counts as its old module, so the facade keeps owning
    what it owned before the split; anything else in the part (new code,
    imports, unnamed statements) keeps the part's own path. The part's
    generated docstring is skipped, and each group starts with `pass` so its
    first statement is never read as a docstring.
    """
    if split_origin(rel, recipes=recipes) == rel:
        yield rel, tree
        return
    groups: dict[str, list[ast.stmt]] = {}
    for index, stmt in enumerate(tree.body):
        if index == 0 and isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
            continue  # the generated "Moved out of ..." docstring
        name = _bound_name(stmt)
        groups.setdefault(split_origin(rel, name, recipes) if name else rel, []).append(stmt)
    for origin, body in groups.items():
        yield origin, ast.Module(body=[ast.Pass(), *body], type_ignores=[])


def logical_split_tree_map(paths: Iterable[pathlib.Path], root: pathlib.Path,
                           recipes: pathlib.Path | None = None) -> dict[str, ast.Module]:
    """logical_split_trees over many files, merged per origin: every path's own
    key is present, and a file that is its origin's only contributor keeps its
    own Module object. The original module's constants and their moved
    consumers share one tree, while new code in a part stays a separate owner."""
    merged: dict[str, list[ast.Module]] = {}
    for path in paths:
        rel = path.relative_to(root).as_posix()
        merged.setdefault(rel, [])
        for origin, module in logical_split_trees(rel, ast.parse(path.read_text(), filename=str(path)), recipes):
            merged.setdefault(origin, []).append(module)
    return {rel: modules[0] if len(modules) == 1 else
            ast.Module(body=[stmt for module in modules for stmt in module.body], type_ignores=[])
            for rel, modules in merged.items()}   # no modules: an empty Module
