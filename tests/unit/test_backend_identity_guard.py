"""The backend identity guard, widened (BACKENDS-10).

test_backend_capabilities.py bans `backend == X` lines in the top level of
server/lib. This AST guard covers what that one cannot see, over every
subpackage (rglob) and server.py:

- comparisons (==, !=, in, not in) against an id literal, a backends.X /
  AgentBackend.X reference, a module-local constant bound to an id, or a
  literal set/list/tuple holding one;
- dicts keyed by an id; literal sets, lists and tuples holding an id;
- by_id("codex")-style calls with a literal id;
- SQL literals comparing a column with an id ('codex').

Ratchet mode: today's counts per module and kind are the baseline. A new kind
or a higher count fails (give the Backend class in server/lib/backend/ a
method or attribute instead); a lower count fails until BASELINE is lowered.
Only backend/registry.py, the one module that lists the ids, is exempt.

Limits: a volume ratchet per (module, kind), so removing one use and adding
another of the same kind in the same module passes; review catches such
relocations. It is syntactic: an id reached through an alias or computed at
run time is not seen.
"""
from __future__ import annotations

import ast
import collections
import pathlib
import re

SERVER = pathlib.Path(__file__).resolve().parents[2] / "server"
IDS = frozenset({"claude", "codex", "agy", "antigravity", "grok", "opencode", "deepseek", "muse"})
_REFS = frozenset(i.upper() for i in IDS)
_SQL = re.compile(r"(?:=|!=|<>|\bIN\b)\s*\(?\s*'(?:%s)'" % "|".join(sorted(IDS)), re.I)
EXEMPT = {"lib/backend/registry.py"}

BASELINE: dict[tuple[str, str], int] = {
    ('lib/avatar_urls.py', 'dict_key'): 1,
    ('lib/backend/agy.py', 'collection'): 1,
    ('lib/backend/claude.py', 'collection'): 2,
    ('lib/backend_auth.py', 'dict_key'): 2,
    ('lib/backend_usage.py', 'collection'): 1,
    ('lib/backend_usage.py', 'compare'): 8,
    ('lib/backend_usage.py', 'constant'): 3,
    ('lib/codex_app_server.py', 'by_id'): 1,
    ('lib/harness_providers.py', 'collection'): 1,
    ('lib/instance_backup.py', 'compare'): 2,
    ('lib/janitor_autonomy.py', 'compare'): 2,
    ('lib/janitor_hotseat.py', 'collection'): 1,
    ('lib/janitor_hotseat.py', 'dict_key'): 1,
    ('lib/janitors.py', 'collection'): 3,
    ('lib/live_hub.py', 'dict_key'): 1,
    ('lib/managed_skills.py', 'collection'): 4,
    ('lib/managed_skills.py', 'dict_key'): 1,
    ('lib/model_avatars.py', 'collection'): 3,
    ('lib/oracle_codex_session.py', 'collection'): 1,
    ('lib/oracle_live.py', 'compare'): 1,
    ('lib/oracle_realtime.py', 'compare'): 1,
    ('lib/oracle_router.py', 'collection'): 3,
    ('lib/oracle_router.py', 'compare'): 2,
    ('lib/personal_skills.py', 'dict_key'): 1,
    ('lib/policies/agent_spec.py', 'dict_key'): 1,
    ('lib/provider_background_jobs.py', 'by_id'): 1,
    ('lib/provider_background_jobs.py', 'sql'): 1,
    ('lib/provider_capabilities.py', 'compare'): 15,
    ('lib/provider_capabilities.py', 'dict_key'): 1,
    ('lib/roster.py', 'dict_key'): 2,
    ('lib/tool_explanations.py', 'collection'): 1,
    ('lib/tool_explanations.py', 'compare'): 1,
    ('lib/transcript_incremental.py', 'collection'): 1,
    ('lib/transcript_incremental.py', 'dict_key'): 4,
    ('lib/turn_identity.py', 'collection'): 1,
    ('lib/viz_native.py', 'collection'): 1,
    ('lib/viz_rule_author.py', 'collection'): 2,
}


def _is_id(node) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in IDS


def _is_ref(node) -> bool:
    return (isinstance(node, ast.Attribute) and node.attr in _REFS
            and isinstance(node.value, ast.Name) and node.value.id in ("backends", "AgentBackend"))


def identity_uses(tree: ast.Module) -> collections.Counter[str]:
    constants = {target.id for stmt in tree.body if isinstance(stmt, ast.Assign) and _is_id(stmt.value)
                 for target in stmt.targets if isinstance(target, ast.Name)}
    found: collections.Counter[str] = collections.Counter({"constant": len(constants)})
    in_compare: set[int] = set()

    def idish(node) -> bool:
        if isinstance(node, (ast.Set, ast.List, ast.Tuple)) and any(_is_id(e) or _is_ref(e) for e in node.elts):
            in_compare.add(id(node))
            return True
        return _is_id(node) or _is_ref(node) or (isinstance(node, ast.Name) and node.id in constants)

    nodes = list(ast.walk(tree))
    for node in nodes:
        if isinstance(node, ast.Compare) and any(idish(side) for side in [node.left, *node.comparators]):
            found["compare"] += 1
    for node in nodes:
        if isinstance(node, ast.Dict) and any(k is not None and (_is_id(k) or _is_ref(k)) for k in node.keys):
            found["dict_key"] += 1
        elif (isinstance(node, (ast.Set, ast.List, ast.Tuple)) and id(node) not in in_compare
              and any(_is_id(e) or _is_ref(e) for e in node.elts)):
            found["collection"] += 1
        elif (isinstance(node, ast.Call) and getattr(node.func, "attr", getattr(node.func, "id", "")) == "by_id"
              and node.args and _is_id(node.args[0])):
            found["by_id"] += 1
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and _SQL.search(node.value):
            found["sql"] += 1
    return +found


def current() -> dict[tuple[str, str], int]:
    out = {}
    for path in sorted((SERVER / "lib").rglob("*.py")) + [SERVER / "server.py"]:
        rel = path.relative_to(SERVER).as_posix()
        if rel in EXEMPT:
            continue
        for kind, count in identity_uses(ast.parse(path.read_text(), filename=str(path))).items():
            out[(rel, kind)] = count
    return out


def test_no_new_backend_identity_logic():
    now = current()
    grew = [f"{rel}: {kind} x{count} (baseline {BASELINE.get((rel, kind), 0)})"
            for (rel, kind), count in sorted(now.items()) if count > BASELINE.get((rel, kind), 0)]
    assert not grew, ("backend identity logic grew; give the Backend class in server/lib/backend/ "
                      "a method or attribute and call it instead:\n" + "\n".join(grew))


def test_baseline_only_shrinks():
    now = current()
    lower = [f"{rel}: {kind} baseline {count}, now {now.get((rel, kind), 0)}"
             for (rel, kind), count in sorted(BASELINE.items()) if now.get((rel, kind), 0) < count]
    assert not lower, "lower BASELINE to match (the ratchet only goes down):\n" + "\n".join(lower)


def test_the_scanner_sees_every_form():
    tree = ast.parse(
        'CLAUDE = "claude"\n'
        'from lib import backends\n'
        'def f(b, row):\n'
        '    if b in {"codex", "grok"}: pass\n'
        '    if b == CLAUDE: pass\n'
        '    if backends.CODEX != b: pass\n'
        '    limits = {"codex": 5}\n'
        '    order = ["claude", "agy"]\n'
        '    registry.by_id("codex")\n'
        '    sql = "SELECT 1 FROM t WHERE backend = \'codex\'"\n'
        '    if b == "other": pass\n')
    assert identity_uses(tree) == {"constant": 1, "compare": 3, "dict_key": 1, "collection": 1,
                                   "by_id": 1, "sql": 1}
