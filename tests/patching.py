"""Patching a module-level name for a test across the module's S9 split family.

An S9 split (tools/refactor/split_apply.py) moves definitions out of a module
into parts and leaves the old module as a facade. A moved definition then reads
its names from its part's globals, so `monkeypatch.setattr(old, name, value)`
no longer reaches it. `patch_shared` sets the name on the old module and on
every part its split recipes list that exists and holds the same object, and
on nothing else: a shared helper such as `now_ms` stays untouched in modules
outside the family. Where the split is not applied (main), the family is the
module alone and this is exactly `monkeypatch.setattr(module, name, value)`.
"""
from __future__ import annotations

import importlib
import pathlib
import tomllib
import types

_REPO = pathlib.Path(__file__).resolve().parents[1]
_RECIPES = _REPO / "tools" / "refactor" / "splits"
_MISSING = object()


def split_family(module: types.ModuleType, recipes: pathlib.Path = _RECIPES) -> list[types.ModuleType]:
    """The parts the split recipes say came from `module`, as imported modules;
    parts whose file does not exist (the split is not applied) are left out."""
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
    return parts


def split_origin(path: str, recipes: pathlib.Path = _RECIPES) -> str:
    """The old module a split part came from, in the same spelling as `path`
    ("server/lib/x.py" or "lib/x.py"); `path` itself when no recipe lists it.
    Lets path-based guards keep naming the old module on integration."""
    prefix = "server/" if path.startswith("server/") else ""
    rel = path[len(prefix):]
    for recipe in sorted(recipes.glob("*.toml")) if recipes.is_dir() else ():
        for split in tomllib.loads(recipe.read_text()).get("split", []):
            if any(part.get("module") == rel for part in split.get("part", [])):
                return prefix + str(split["module"])
    return path


def patch_shared(monkeypatch, module: types.ModuleType, name: str, value,
                 recipes: pathlib.Path = _RECIPES, *, raising: bool = True) -> None:
    """monkeypatch.setattr(module, name, value, raising=raising), and the same on
    each part of the module's split family that holds the object the module holds."""
    original = getattr(module, name, _MISSING)
    monkeypatch.setattr(module, name, value, raising=raising)
    if original is _MISSING:
        return
    for part in split_family(module, recipes):
        if getattr(part, name, _MISSING) is original:
            monkeypatch.setattr(part, name, value)
