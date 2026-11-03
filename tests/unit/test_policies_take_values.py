"""Rule 6, hardened (TP-16): a policy takes values, not capabilities.

test_policies_pure.py checks what a policy imports. A policy can still do IO
through what it is handed: a module (AgentSpec.parse gets the backends module
and the personas store), a callable, or a dataclass of callbacks (RosterView).
This guard scans every module under server/lib/policies/ (rglob) for:

- callable: a parameter or dataclass field annotated Callable/Awaitable/ModuleType;
- module: an unannotated parameter named after a server/lib module (backends,
  personas, db, ...);
- untyped: any other parameter with no annotation, which can carry either.

Ratchet mode: today's cases are the baseline, keyed by module, function and
name. A new one fails; one that disappears must be removed from BASELINE.
Gather the facts in the IO code and pass them in as values.

Limits: this checks signatures, not behaviour. 'untyped' is a conservative
annotation rule that can flag an ordinary value, while Any, an annotation alias
or a typed object that carries a capability passes. It complements, and does
not replace, the import check in test_policies_pure.py and review of the call
sites.
"""
from __future__ import annotations

import ast
import pathlib

SERVER_LIB = pathlib.Path(__file__).resolve().parents[2] / "server" / "lib"
POLICIES = SERVER_LIB / "policies"
_CALLABLE = ("Callable", "Awaitable", "Coroutine", "ModuleType")
MODULE_NAMES = frozenset(
    p.stem for p in SERVER_LIB.rglob("*.py") if p.stem != "__init__") | frozenset(
    p.name for p in SERVER_LIB.iterdir() if p.is_dir() and (p / "__init__.py").exists())

BASELINE: set[tuple[str, str, str, str]] = {
    ('lib/policies/agent_spec.py', 'RosterView', 'ancestors', 'callable'),
    ('lib/policies/agent_spec.py', 'RosterView', 'cartesia_voice_for', 'callable'),
    ('lib/policies/agent_spec.py', 'RosterView', 'contact_pool', 'callable'),
    ('lib/policies/agent_spec.py', 'RosterView', 'default_model', 'callable'),
    ('lib/policies/agent_spec.py', 'RosterView', 'existing_agent', 'callable'),
    ('lib/policies/agent_spec.py', 'RosterView', 'existing_cwd', 'callable'),
    ('lib/policies/agent_spec.py', 'RosterView', 'launch_default', 'callable'),
    ('lib/policies/agent_spec.py', 'RosterView', 'random_suffix', 'callable'),
    ('lib/policies/agent_spec.py', 'RosterView', 'recorded_model', 'callable'),
    ('lib/policies/agent_spec.py', 'RosterView', 'recover_user_path', 'callable'),
    ('lib/policies/agent_spec.py', 'RosterView', 'resolve_agent', 'callable'),
    ('lib/policies/agent_spec.py', 'RosterView', 'resume_listed', 'callable'),
    ('lib/policies/agent_spec.py', 'RosterView', 'resume_owner', 'callable'),
    ('lib/policies/agent_spec.py', 'RosterView', 'session_exists', 'callable'),
    ('lib/policies/agent_spec.py', '_parse', 'backends', 'module'),
    ('lib/policies/agent_spec.py', '_parse', 'personas', 'module'),
    ('lib/policies/agent_spec.py', 'parse', 'backends', 'module'),
    ('lib/policies/agent_spec.py', 'parse', 'personas', 'module'),
    ('lib/policies/failover.py', 'decide', 'verdict', 'untyped'),
    ('lib/policies/stale_work.py', 'from_config', 'cfg', 'untyped'),
}


def capabilities(tree: ast.Module) -> set[tuple[str, str, str]]:
    """(owner, name, kind) for every capability-shaped parameter or field."""
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            owner = getattr(node, "name", "<lambda>")
            args = node.args
            params = [*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg]
            for param in filter(None, params):
                if param.arg in ("self", "cls") or isinstance(node, ast.Lambda):
                    continue
                note = ast.unparse(param.annotation) if param.annotation else ""
                if any(word in note for word in _CALLABLE):
                    found.add((owner, param.arg, "callable"))
                elif not note:
                    found.add((owner, param.arg, "module" if param.arg in MODULE_NAMES else "untyped"))
        elif isinstance(node, ast.ClassDef):
            for stmt in node.body:
                if (isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)
                        and any(word in ast.unparse(stmt.annotation) for word in _CALLABLE)):
                    found.add((node.name, stmt.target.id, "callable"))
    return found


def current() -> set[tuple[str, str, str, str]]:
    return {(path.relative_to(SERVER_LIB.parent).as_posix(), *item)
            for path in sorted(POLICIES.rglob("*.py"))
            for item in capabilities(ast.parse(path.read_text(), filename=str(path)))}


def test_policies_take_no_new_modules_or_callables():
    new = sorted(current() - BASELINE)
    assert not new, ("a policy is handed a capability; pass the facts it needs as values:\n"
                     + "\n".join(f"{f}: {owner}({name}) is {kind}" for f, owner, name, kind in new))


def test_baseline_only_shrinks():
    gone = sorted(BASELINE - current())
    assert not gone, f"remove from BASELINE (fixed or moved): {gone}"


def test_the_scanner_sees_every_form():
    tree = ast.parse(
        "from typing import Callable\n"
        "from dataclasses import dataclass\n"
        "@dataclass\n"
        "class View:\n"
        "    lookup: Callable[[str], int] = len\n"
        "    name: str = ''\n"
        "def decide(data: dict, backends, fetch: Callable[[], int], cfg, roster: View, *, n: int = 0) -> int:\n"
        "    return 0\n")
    assert capabilities(tree) == {("View", "lookup", "callable"), ("decide", "backends", "module"),
                                  ("decide", "fetch", "callable"), ("decide", "cfg", "untyped")}
