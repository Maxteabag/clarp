#!/usr/bin/env python3
"""Apply S3 group moves: Handler members out of server.py into route mixins.

    python tools/refactor/s3_apply.py [--root REPO] [--recipe FILE] [--check] --helpers
    python tools/refactor/s3_apply.py [--root REPO] [--recipe FILE] [--check] (--all | --group NAME ...)

Helpers step (`[helpers]` in the recipe; `--helpers` applies only it, `--all`
and `--group` apply it first): the listed module-level functions and values of
server.py move verbatim (source text, server.py order) into
server/lib/routes/helpers.py, and server.py imports them back at the place of
the first one, so server.<name> is lib.routes.helpers.<name> for every moved
name and hooks, skills and scripts that import them from server keep working.
Verified by ast_identity's top-level mode
(`server.py=server.py,lib/routes/helpers.py`) before any group. It refuses a
listed name that is not a plain module-level definition, a helper that reads a
name server.py defines unless that name is listed too, and any name that is
rebound anywhere: a second module-level binding, `global` in a server.py
function, `del`, and any attribute assignment, setattr/delattr or monkeypatch
of the name in any tracked Python file (all of server/, scripts/, tests/,
plugin/, skills/, bin/, tools/) unless its target expression names `helpers`.
Each such site is listed; after the move a patch through the server module
would silently stop affecting the mixins.

A group move is data (tools/refactor/s3_moves.toml) plus this generator; it is
re-applied to whatever server.py is in the tree, never merged by hand
(docs/architecture/route-registry.md, section 11). For each group, in recipe
order, it:

- cuts each listed member from `class Handler` in server/server.py as source
  text: from its first decorator (with the comment lines directly above it)
  to its last line, so bodies and comments travel byte for byte;
- writes server/<module> with a generated header, server.py's own
  `from __future__` line, server.py's own import statement for every
  module-level name the members read (narrowed to the names they use, in
  server.py's order), and `class <Class>:` holding the members in recipe order;
- imports the mixin in server.py directly above `class Handler` and makes the
  applied mixins Handler's first bases, in recipe order.

It refuses, naming the member, when a listed member is not in Handler, when a
member reads a module-level name server.py defines rather than imports
(composition-root state: it needs a reviewed preparatory edit first), when
server.py binds a read name only in a non-plain way (inside try/if), or when a
member uses zero-argument super() or a private __name. The read analysis is
ast_identity.py's own (global_reads, module_binding), so the generator refuses
what the method-mode gate would reject.

Deterministic: the same tree and recipe give the same bytes. A group whose
mixin is already among Handler's bases is skipped, so re-running is a no-op.
--check writes nothing and reports what would change (exit 1 if anything would).
Exit 0 on success, 1 on --check with pending changes, 2 on a refusal or usage.
"""
from __future__ import annotations

import argparse
import ast
import builtins
import importlib.util
import pathlib
import sys
import tomllib

ROOT = pathlib.Path(__file__).resolve().parents[2]
SERVER = "server/server.py"
HANDLER = "Handler"
HEADER = ('"""{cls}: Handler routes moved out of server.py by tools/refactor/s3_apply.py\n'
          '(group "{group}"). Generated: change tools/refactor/s3_moves.toml, not this file."""\n')


class Refusal(Exception):
    pass


def _ast_identity():
    path = ROOT / "scripts" / "refactor" / "ast_identity.py"
    spec = importlib.util.spec_from_file_location("ast_identity_for_s3_apply", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_IDENT = __import__("re").compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _check_module(group: str, module: str, cls: str) -> None:
    """A group's module is exactly lib/routes/<identifier>.py (no traversal, no
    package or helpers module) and its class an identifier."""
    parts = pathlib.PurePosixPath(module).parts
    stem = pathlib.PurePosixPath(module).stem
    if (len(parts) != 3 or parts[:2] != ("lib", "routes") or not module.endswith(".py")
            or not _IDENT.match(stem) or stem in ("__init__", "helpers", "registry")):
        raise Refusal(f"recipe: group {group} module must be lib/routes/<name>.py (got {module!r})")
    if not _IDENT.match(str(cls)):
        raise Refusal(f"recipe: group {group} class must be an identifier (got {cls!r})")


def load_recipe(path: pathlib.Path) -> list[dict]:
    data = tomllib.loads(path.read_text())
    groups = data.get("group", [])
    seen = set()
    for g in groups:
        for key in ("name", "module", "class", "members"):
            if key not in g:
                raise Refusal(f"recipe: group {g.get('name', '?')} lacks {key}")
        if g["name"] in seen:
            raise Refusal(f"recipe: group {g['name']} listed twice")
        seen.add(g["name"])
        _check_module(g["name"], g["module"], g["class"])
        _acknowledged(g, f"group {g['name']}")
    names = [m for g in groups for m in g["members"]]
    dup = sorted({m for m in names if names.count(m) > 1})
    if dup:
        raise Refusal(f"recipe: members listed in more than one place: {dup}")
    return groups


def _handler(tree: ast.Module) -> ast.ClassDef:
    for stmt in tree.body:
        if isinstance(stmt, ast.ClassDef) and stmt.name == HANDLER:
            return stmt
    raise Refusal(f"{SERVER}: class {HANDLER} not found")


def _member_nodes(cls: ast.ClassDef) -> dict[str, ast.stmt]:
    found: dict[str, ast.stmt] = {}
    for stmt in cls.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            found[stmt.name] = stmt
        elif isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
            found[stmt.targets[0].id] = stmt
        elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            found[stmt.target.id] = stmt
    return found


def _span(lines: list[str], node: ast.stmt) -> tuple[int, int]:
    """0-based [start, end) line range of a member: decorators, and the comment
    lines directly above it at the same indentation, included."""
    start = min([node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])]) - 1
    indent = lines[start][:len(lines[start]) - len(lines[start].lstrip())]
    while start > 0 and lines[start - 1].startswith(indent + "#"):
        start -= 1
    return start, node.end_lineno


class _ModuleBindings(ast.NodeVisitor):
    """Every statically visible binding of a name in module scope, with its kind
    and line: imports, assignments and deletes (any target form), for/with
    targets, except-handler names, match captures, named expressions (walrus,
    also inside comprehensions), def/class names at any module-level nesting,
    and `global` declarations in any function or class at any depth. Function,
    lambda and class bodies are other scopes; only their evaluated parts
    (decorators, defaults, annotations, bases) and `global` statements count."""

    def __init__(self):
        self.found: dict[str, list[tuple[str, int]]] = {}

    def add(self, name, kind, node):
        self.found.setdefault(name, []).append((kind, getattr(node, "lineno", 0)))

    def visit_Import(self, node):
        for alias in node.names:
            self.add(alias.asname or alias.name.split(".")[0], "import", node)

    def visit_ImportFrom(self, node):
        if node.module == "__future__":
            return
        for alias in node.names:
            self.add(alias.asname or alias.name, "import", node)

    def visit_Name(self, node):
        if isinstance(node.ctx, ast.Store):
            self.add(node.id, "assignment", node)
        elif isinstance(node.ctx, ast.Del):
            self.add(node.id, "del", node)

    def visit_NamedExpr(self, node):
        self.add(node.target.id, "named expression", node)
        self.visit(node.value)

    def _globals(self, body):
        for sub in ast.walk(ast.Module(body=list(body), type_ignores=[])):
            if isinstance(sub, ast.Global):
                for name in sub.names:
                    self.add(name, "global write", sub)

    def _function_header(self, node):
        for d in node.decorator_list:
            self.visit(d)
        args = node.args
        for d in [*args.defaults, *[k for k in args.kw_defaults if k is not None]]:
            self.visit(d)
        for a in [*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg]:
            if a is not None and getattr(a, "annotation", None) is not None:
                self.visit(a.annotation)
        if getattr(node, "returns", None) is not None:
            self.visit(node.returns)

    def visit_FunctionDef(self, node):
        self.add(node.name, "def", node)
        self._function_header(node)
        self._globals(node.body)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Lambda(self, node):
        for d in [*node.args.defaults, *[k for k in node.args.kw_defaults if k is not None]]:
            self.visit(d)

    def visit_ClassDef(self, node):
        self.add(node.name, "class", node)
        for part in [*node.decorator_list, *node.bases, *[k.value for k in node.keywords]]:
            self.visit(part)
        self._globals(node.body)

    def _comprehension(self, node):
        # Loop targets are the comprehension's own; a walrus binds the enclosing scope.
        for sub in ast.walk(node):
            if isinstance(sub, ast.NamedExpr):
                self.add(sub.target.id, "named expression", sub)
            elif isinstance(sub, ast.Lambda):
                self.visit_Lambda(sub)

    visit_ListComp = visit_SetComp = visit_DictComp = visit_GeneratorExp = _comprehension

    def visit_ExceptHandler(self, node):
        if node.name:
            self.add(node.name, "except alias", node)
        self.generic_visit(node)

    def visit_MatchAs(self, node):
        if node.name:
            self.add(node.name, "match capture", node)
        self.generic_visit(node)

    def visit_MatchStar(self, node):
        if node.name:
            self.add(node.name, "match capture", node)

    def visit_MatchMapping(self, node):
        if node.rest:
            self.add(node.rest, "match capture", node)
        self.generic_visit(node)


def module_bindings(tree: ast.Module) -> dict[str, list[tuple[str, int]]]:
    collector = _ModuleBindings()
    for stmt in tree.body:
        collector.visit(stmt)
    return collector.found


_DYNAMIC = __import__("re").compile(r"\bglobals\s*\(|\bvars\s*\(\s*\)|\bexec\s*\(|sys\.modules\s*\[\s*__name__\s*\]|__dict__\s*\[")


def refuse_dynamic_globals(text: str) -> None:
    """A module whose globals can be written dynamically has no statically
    provable owner for any name: refuse rather than guess."""
    match = _DYNAMIC.search(text)
    if match:
        line = text.count("\n", 0, match.start()) + 1
        raise Refusal(f"{SERVER}:{line}: dynamic access to module globals ({match.group(0).strip()}); "
                      "the binding audit cannot prove ownership")


def refuse_wildcards(tree: ast.Module) -> None:
    """A `from x import *` anywhere in server.py binds names that cannot be
    listed statically (it can shadow a builtin a moved member reads, and the
    destination would not inherit it): refuse rather than guess."""
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and any(a.name == "*" for a in node.names):
            raise Refusal(f"{SERVER}:{node.lineno}: wildcard import from {node.module or '.'}; "
                          "its names cannot be proved, remove it first")


def rebound_imports(tree: ast.Module, tool=None) -> dict[str, str]:
    """Names server.py binds by an import and also binds any other way (or by a
    second import): their owner is ambiguous."""
    found: dict[str, str] = {}
    for name, sites in module_bindings(tree).items():
        if any(kind == "import" for kind, _ in sites) and len(sites) > 1:
            first = sites[0]
            other = next(site for site in sites if site is not first)
            found[name] = f"{other[0]} on line {other[1]}"
    return found


def _refuse_member(name: str, node: ast.stmt, tree: ast.Module, tool) -> list[tuple[str, tuple]]:
    """The module-level reads of a member, as (name, import binding); refuses
    what cannot move verbatim."""
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name) and sub.func.id == "super" and not sub.args:
            raise Refusal(f"{name}: uses zero-argument super()")
        ident = sub.attr if isinstance(sub, ast.Attribute) else sub.id if isinstance(sub, ast.Name) else ""
        if ident.startswith("__") and not ident.endswith("__"):
            raise Refusal(f"{name}: uses private name {ident}")
    imports = tool.top_imports(tree, "")
    rebound = rebound_imports(tree, tool)
    reads = []
    for read in sorted(tool.global_reads(node)):
        if read in rebound:
            raise Refusal(f"{name}: reads {read}, which server.py imports and also rebinds ({rebound[read]}); "
                          "the owner is ambiguous, prepare it first")
        if read in imports:
            reads.append((read, imports[read]))
            continue
        binding = tool.module_binding(tree, "server.py", read)
        if binding is not None:
            raise Refusal(f"{name}: reads {read}, which server.py defines (composition-root state; "
                          "needs a reviewed preparatory edit first)")
        if hasattr(builtins, read):
            continue
        raise Refusal(f"{name}: reads {read}, which server.py does not bind with a plain top-level import")
    return reads


def _import_lines(tree: ast.Module, needed: set[str]) -> list[str]:
    """server.py's own top-level import statements, narrowed to `needed`, in
    server.py's order; `from __future__` excluded."""
    out = []
    for stmt in tree.body:
        if isinstance(stmt, ast.ImportFrom) and stmt.module == "__future__":
            continue
        if isinstance(stmt, ast.Import):
            for alias in stmt.names:
                bound = alias.asname or alias.name.split(".")[0]
                if bound in needed:
                    out.append(f"import {alias.name}" + (f" as {alias.asname}" if alias.asname else ""))
        elif isinstance(stmt, ast.ImportFrom):
            kept = [a for a in stmt.names if (a.asname or a.name) in needed]
            if kept:
                names = ", ".join(a.name + (f" as {a.asname}" if a.asname else "") for a in kept)
                out.append(f"from {'.' * stmt.level}{stmt.module or ''} import {names}")
    return out


def _futures(tree: ast.Module, lines: list[str]) -> list[str]:
    return [lines[s.lineno - 1] for s in tree.body
            if isinstance(s, ast.ImportFrom) and s.module == "__future__"]


def _bases_line(line: str, mixins: list[str]) -> str:
    stripped = line.rstrip()
    indent = line[:len(line) - len(line.lstrip())]
    if stripped == f"{indent}class {HANDLER}:":
        bases: list[str] = []
    else:
        head, sep, rest = line.partition(f"class {HANDLER}(")
        if not sep or not rest.rstrip().endswith("):"):
            raise Refusal(f"{SERVER}: class {HANDLER} header must be one line ending in ':' or '):'")
        bases = [b.strip() for b in rest.rstrip()[:-2].split(",") if b.strip()]
    kept = [b for b in bases if b not in mixins]
    return f"{indent}class {HANDLER}({', '.join(mixins + kept)}):\n"


HELPERS_MODULE = "lib/routes/helpers.py"


def load_helpers(path: pathlib.Path) -> list[str]:
    names = tomllib.loads(path.read_text()).get("helpers", {}).get("names", [])
    if len(names) != len(set(names)):
        raise Refusal("recipe: [helpers] lists a name twice")
    return list(names)


def _top_definitions(tree: ast.Module) -> dict[str, list[ast.stmt]]:
    """Every module-level statement that binds a name, by name (imports excluded)."""
    found: dict[str, list[ast.stmt]] = {}
    for stmt in tree.body:
        names: list[str] = []
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names = [stmt.name]
        elif isinstance(stmt, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
            names = [n.id for t in targets for n in ast.walk(t) if isinstance(n, ast.Name)]
        elif isinstance(stmt, ast.Delete):
            names = [n.id for t in stmt.targets for n in ast.walk(t) if isinstance(n, ast.Name)]
        elif isinstance(stmt, (ast.For, ast.With, ast.If, ast.Try, ast.While)):
            names = [n.id for n in ast.walk(stmt) if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del))]
        for name in names:
            found.setdefault(name, []).append(stmt)
    return found


def _tracked_python(root: pathlib.Path) -> list[pathlib.Path]:
    import subprocess
    try:
        out = subprocess.run(["git", "-C", str(root), "ls-files", "*.py"], check=True,
                             capture_output=True, text=True).stdout.split()
        return [root / p for p in out]
    except (subprocess.CalledProcessError, FileNotFoundError):
        return sorted(p for p in root.rglob("*.py") if ".venv" not in p.parts and ".git" not in p.parts)


def _file_bindings(module: ast.Module) -> dict[str, list[tuple[ast.AST, bool]]]:
    """Every binding of every name anywhere in a file, any scope: (node, is a
    top-level statement of the module)."""
    top = set(map(id, module.body))
    found: dict[str, list[tuple[ast.AST, bool]]] = {}

    def add(name, node, at_top):
        found.setdefault(name, []).append((node, at_top))

    for parent in ast.walk(module):
        at_top = id(parent) in top
        if isinstance(parent, (ast.Import, ast.ImportFrom)):
            for alias in parent.names:
                add(alias.asname or alias.name.split(".")[0], parent, at_top)
        elif isinstance(parent, ast.Name) and isinstance(parent.ctx, (ast.Store, ast.Del)):
            add(parent.id, parent, False)
        elif isinstance(parent, ast.arg):
            add(parent.arg, parent, False)
        elif isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            add(parent.name, parent, at_top)
        elif isinstance(parent, ast.ExceptHandler) and parent.name:
            add(parent.name, parent, False)
        elif isinstance(parent, (ast.MatchAs, ast.MatchStar)) and parent.name:
            add(parent.name, parent, False)
        elif isinstance(parent, ast.MatchMapping) and parent.rest:
            add(parent.rest, parent, False)
        elif isinstance(parent, (ast.Global, ast.Nonlocal)):
            for name in parent.names:
                add(name, parent, False)
    return found


def _server_module_names(module: ast.Module, bindings: dict) -> set[str]:
    """Names that may refer to the server module in a file (fail closed): any
    name containing 'server', a name bound by `import server`, and a name
    assigned from module_from_spec(...) of a spec whose path mentions server.py."""
    names = {name for name in bindings if "server" in name.lower()}
    specs = set()
    for node in ast.walk(module):
        if isinstance(node, ast.Import):
            names |= {a.asname or a.name for a in node.names if a.name == "server"}
        elif isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            call = ast.unparse(node.value.func)
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if call.endswith("spec_from_file_location") and "server.py" in ast.unparse(node.value):
                specs |= set(targets)
            elif call.endswith("module_from_spec") and any(
                    isinstance(a, ast.Name) and a.id in specs for a in node.value.args):
                names |= set(targets)
    return names


def _acknowledged(entry: dict, where: str) -> list[tuple[str, str]]:
    """A recipe entry's acknowledged_patches: [{path, name, reason, evidence}].
    Retargeting the patch to where the moved code reads the name is the default;
    an acknowledgement states, with evidence (for example a trace showing the
    moved code is never reached in that test, or identical results with the
    patch also applied at the moved code's import site), that the test does not
    depend on the moved code seeing the patch. G3 checks every one."""
    out = []
    for item in entry.get("acknowledged_patches", []):
        if (not isinstance(item, dict) or not item.get("path") or not item.get("name")
                or not str(item.get("reason", "")).strip() or not str(item.get("evidence", "")).strip()):
            raise Refusal(f"recipe: {where} acknowledged_patches entries need path, name, reason and evidence")
        out.append((str(item["path"]), str(item["name"])))
    return out


def _unacknowledged(records: list[tuple[str, int, str, str]],
                    acknowledged: list[tuple[str, str]]) -> list[str]:
    """Sites not waived: an acknowledgement matches exactly its file and the
    exact patched name, never a substring of the site text."""
    return [text for path, _, name, text in records if (path, name) not in set(acknowledged)]


def _call_arg(call: ast.Call, position: int, *keywords: str):
    if len(call.args) > position and not any(isinstance(a, ast.Starred) for a in call.args[:position + 1]):
        return call.args[position]
    for kw in call.keywords:
        if kw.arg in keywords:
            return kw.value
    return None


def rebinding_records(root: pathlib.Path, names: set[str], *, server_targets_only: bool = False) -> list[tuple[str, int, str, str]]:
    """Every place that could rebind one of `names` on the server module after it
    moved: in server.py any second binding at module scope (module_bindings);
    in any tracked Python file an attribute assignment or delete, a
    setattr/delattr/monkeypatch.setattr/delattr, patch.object or patch.multiple
    naming it (positional or keyword arguments), or a patch target string ending
    in `.<name>`. A target is exempt only when it is unambiguously
    lib.routes.helpers: a name bound exactly once in the whole file, by a
    module-level import of lib.routes.helpers, or the dotted lib.routes.helpers
    where `lib` is only ever bound by plain `import lib...`; a string exempt only
    when it starts with `lib.routes.helpers.`."""
    sites: list[tuple[str, int, str, str]] = []
    server = root / SERVER
    text = server.read_text()
    tree = ast.parse(text)
    for name, found in module_bindings(tree).items():
        if not server_targets_only and name in names and len(found) > 1:
            other = found[1]
            sites.append((SERVER, other[1], name, f"{SERVER}:{other[1]}: {name} is also bound by a {other[0]}"))
    helpers_module = "lib.routes." + pathlib.PurePosixPath(HELPERS_MODULE).stem

    for path in _tracked_python(root):
        rel = path.relative_to(root).as_posix()
        if rel == "server/" + HELPERS_MODULE:
            continue
        try:
            module = ast.parse(path.read_text())
        except (SyntaxError, UnicodeDecodeError, OSError):
            continue
        bindings = _file_bindings(module)

        def helpers_alias(name: str) -> bool:
            found = bindings.get(name, [])
            if len(found) != 1:
                return False
            node, at_top = found[0]
            if not at_top:
                return False
            if isinstance(node, ast.ImportFrom):
                return (node.module == "lib.routes" and not node.level
                        and any(a.name == "helpers" and (a.asname or a.name) == name for a in node.names))
            if isinstance(node, ast.Import):
                return any(a.name == helpers_module and a.asname == name for a in node.names)
            return False

        def lib_is_the_package() -> bool:
            found = bindings.get("lib", [])
            return bool(found) and all(isinstance(n, ast.Import) and all(
                a.asname is None or a.asname != "lib" for a in n.names) for n, _ in found)

        server_names = _server_module_names(module, bindings)

        def target_ok(expr) -> bool:
            if server_targets_only:
                # Only a patch through the server module matters here.
                return not (isinstance(expr, ast.Name) and expr.id in server_names)
            if isinstance(expr, ast.Name):
                return helpers_alias(expr.id)
            return ast.unparse(expr) == helpers_module and lib_is_the_package()

        def string_ok(value: str) -> bool:
            if server_targets_only:
                return "server" not in value.split(".", 1)[0].lower()
            return value.startswith(helpers_module + ".")

        def named(value) -> str | None:
            return value.value if isinstance(value, ast.Constant) and isinstance(value.value, str) else None

        def obj_ok(obj) -> bool:
            # A string target ("server", "server.x") is judged by its string,
            # never treated as an object expression.
            if named(obj) is not None:
                return string_ok(named(obj) + ".")
            return obj is not None and target_ok(obj)

        def add(lineno, name, text):
            sites.append((rel, lineno, name, f"{rel}:{lineno}: {text}"))

        for node in ast.walk(module):
            if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign, ast.Delete)):
                targets = (node.targets if isinstance(node, (ast.Assign, ast.Delete)) else [node.target])
                for t in targets:
                    for sub in ast.walk(t):
                        if isinstance(sub, ast.Attribute) and sub.attr in names and not target_ok(sub.value):
                            add(node.lineno, sub.attr, f"assigns or deletes .{sub.attr}")
            elif isinstance(node, ast.Call):
                func = ast.unparse(node.func)
                if func.endswith("patch.multiple"):
                    obj = _call_arg(node, 0, "target")
                    for attr in [kw.arg for kw in node.keywords if kw.arg in names]:
                        if not obj_ok(obj):
                            add(node.lineno, attr, f"{func}({ast.unparse(obj) if obj else '?'}, {attr}=...)")
                elif func.endswith(("setattr", "delattr", "patch.object")):
                    obj = _call_arg(node, 0, "target")
                    attr = _call_arg(node, 1, "attribute" if func.endswith("patch.object") else "name")
                    if named(obj) is not None and func.endswith(("setattr", "delattr")):
                        # monkeypatch.setattr("module.attr", value) form
                        target = named(obj)
                        name = target.rpartition(".")[2]
                        if name in names and "." in target and not string_ok(target):
                            add(node.lineno, name, f"{func}({target!r})")
                    elif named(attr) in names and not obj_ok(obj):
                        add(node.lineno, named(attr), f"{func}({ast.unparse(obj) if obj else '?'}, {named(attr)!r})")
                elif func.endswith("patch"):
                    target = named(_call_arg(node, 0, "target"))
                    if target and "." in target and target.rpartition(".")[2] in names and not string_ok(target):
                        add(node.lineno, target.rpartition(".")[2], f"{func}({target!r})")
    return sorted(set(sites))


def rebinding_sites(root: pathlib.Path, names: set[str], *, server_targets_only: bool = False) -> list[str]:
    """rebinding_records as printable lines."""
    return [text for *_, text in rebinding_records(root, names, server_targets_only=server_targets_only)]


def apply_helpers(root: pathlib.Path, names: list[str],
                  acknowledged: list[tuple[str, str]] = ()) -> dict[str, str]:
    """The [helpers] step; {} when there is nothing to do or it is applied."""
    if not names:
        return {}
    tool = _ast_identity()
    server_path = root / SERVER
    text = server_path.read_text()
    lines = text.splitlines(keepends=True)
    refuse_dynamic_globals(text)
    tree = ast.parse(text)
    refuse_wildcards(tree)
    imports = tool.top_imports(tree, "")
    module = "lib.routes." + pathlib.PurePosixPath(HELPERS_MODULE).stem
    if all(imports.get(n) == (module, n) for n in names):
        return {}
    if any(imports.get(n) == (module, n) for n in names) or (root / "server" / HELPERS_MODULE).exists():
        raise Refusal(f"{HELPERS_MODULE} is partly applied; re-apply from a clean server.py")
    if not (root / "server" / "lib" / "routes" / "__init__.py").exists():
        raise Refusal("server/lib/routes/__init__.py is missing: the route seam must be applied first")
    defs = _top_definitions(tree)
    binding_map = module_bindings(tree)
    wanted = set(names)
    nodes = []
    for name in names:
        stmts = defs.get(name, [])
        if (len(stmts) != 1 or len(binding_map.get(name, [])) != 1
                or not isinstance(stmts[0], (ast.FunctionDef, ast.AsyncFunctionDef, ast.Assign, ast.AnnAssign))):
            raise Refusal(f"helpers: {name} is not a single plain module-level definition in server.py "
                          f"(bindings: {binding_map.get(name, [])})")
        stmt = stmts[0]
        if isinstance(stmt, (ast.Assign, ast.AnnAssign)):
            targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
            bound = {n.id for t in targets for n in ast.walk(t) if isinstance(n, ast.Name)}
            if bound != {name}:
                raise Refusal(f"helpers: {name} is assigned together with {sorted(bound - {name})}")
        nodes.append((name, stmt))
    rebound = rebound_imports(tree, tool)
    sites = rebinding_sites(root, wanted)
    if sites:
        raise Refusal("helpers: names rebound outside their definition (retarget or keep them):\n  "
                      + "\n  ".join(sites))
    needed: set[str] = set()
    for name, stmt in nodes:
        reads = tool.global_reads(stmt) if not isinstance(stmt, (ast.Assign, ast.AnnAssign)) else {
            n.id for n in ast.walk(stmt.value if stmt.value is not None else stmt)
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
        for read in sorted(reads):
            if read in wanted:
                continue
            if read in rebound:
                raise Refusal(f"helpers: {name} reads {read}, which server.py imports and also rebinds "
                              f"({rebound[read]}); the owner is ambiguous, prepare it first")
            if read in imports:
                needed.add(read)
            elif read in defs:
                raise Refusal(f"helpers: {name} reads {read}, which server.py defines; list {read} too or keep {name}")
            elif not hasattr(builtins, read):
                raise Refusal(f"helpers: {name} reads {read}, which server.py does not bind with a plain top-level import")
    patched = _unacknowledged(rebinding_records(root, needed, server_targets_only=True), list(acknowledged))
    if patched:
        raise Refusal("helpers: imported names the moved helpers read are patched through the server "
                      "module; after the move the patch would not reach them:\n  " + "\n  ".join(patched))
    nodes.sort(key=lambda item: item[1].lineno)
    spans = [_span(lines, stmt) for _, stmt in nodes]
    bodies = ["".join(lines[a:b]).rstrip("\n") + "\n" for a, b in spans]
    parts = ['"""Module-level helpers moved out of server.py by tools/refactor/s3_apply.py.\n'
             'server.py imports them back. Generated: change tools/refactor/s3_moves.toml, not this file."""\n']
    if _futures(tree, lines):
        parts.append("".join(_futures(tree, lines)))
    if _import_lines(tree, needed):
        parts.append("\n".join(_import_lines(tree, needed)) + "\n")
    helpers_text = "\n".join(p.rstrip("\n") + "\n" for p in parts) + "\n\n" + "\n\n".join(bodies)
    drop = set()
    for a, b in spans:
        drop.update(range(a, b))
        nxt = b
        while nxt < len(lines) and not lines[nxt].strip() and nxt - b < 2:
            drop.add(nxt)
            nxt += 1
    first = spans[0][0]
    back = f"from {module} import {', '.join(n for n, _ in nodes)}  # noqa: E402  (s3_apply)\n"
    out_lines = []
    for i, line in enumerate(lines):
        if i == first:
            out_lines.append(back)
        if i in drop:
            continue
        out_lines.append(line)
    return {"server/" + HELPERS_MODULE: helpers_text, SERVER: "".join(out_lines)}


def apply(root: pathlib.Path, groups: list[dict], scan_root: pathlib.Path | None = None) -> dict[str, str]:
    """New text per changed file (paths relative to root); refuses on any problem."""
    tool = _ast_identity()
    server_path = root / SERVER
    text = server_path.read_text()
    refuse_dynamic_globals(text)
    lines = text.splitlines(keepends=True)
    tree = ast.parse(text)
    refuse_wildcards(tree)
    cls = _handler(tree)
    current_bases = [ast.unparse(b) for b in cls.bases]
    members = _member_nodes(cls)
    out: dict[str, str] = {}
    cut: list[tuple[int, int]] = []
    applied: list[str] = [g["class"] for g in groups if g["class"] in current_bases]
    new_groups = [g for g in groups if g["class"] not in current_bases]
    if new_groups and not (root / "server" / "lib" / "routes" / "__init__.py").exists():
        raise Refusal("server/lib/routes/__init__.py is missing: the route seam must be applied first")
    for g in new_groups:
        module_path = root / "server" / g["module"]
        if module_path.exists():
            raise Refusal(f"group {g['name']}: {g['module']} exists but {g['class']} is not a Handler base")
        reads: dict[str, tuple] = {}
        bodies = []
        for name in g["members"]:
            node = members.get(name)
            if node is None:
                raise Refusal(f"group {g['name']}: {name} is not a member of {HANDLER}")
            reads.update(dict(_refuse_member(name, node, tree, tool)))
            start, end = _span(lines, node)
            cut.append((start, end))
            bodies.append("".join(lines[start:end]))
        patched = _unacknowledged(rebinding_records(scan_root or root, set(reads), server_targets_only=True),
                                  _acknowledged(g, f"group {g['name']}"))
        if patched:
            raise Refusal(f"group {g['name']}: imported names its members read are patched through the "
                          "server module; after the move the patch would not reach them:\n  "
                          + "\n  ".join(patched))
        body = "\n".join(b if b.endswith("\n") else b + "\n" for b in bodies)
        parts = [HEADER.format(cls=g["class"], group=g["name"])]
        futures = _futures(tree, lines)
        if futures:
            parts.append("".join(futures))
        imports = _import_lines(tree, set(reads))
        if imports:
            parts.append("\n".join(imports) + "\n")
        parts.append(f"\n\nclass {g['class']}:\n{body}")
        out["server/" + g["module"]] = "\n".join(p.rstrip("\n") + "\n" for p in parts[:-1]) + parts[-1]
    if not new_groups:
        return out
    # server.py: cut members (with one following blank line), import the mixins
    # above Handler, put the new mixins first in Handler's bases (recipe order),
    # ahead of the mixins already applied: the order ast_identity's method mode
    # checks for each stage, so a group applied alone is verifiable on its own.
    # Mixins share no member, so their order in the MRO changes no lookup.
    drop = set()
    for start, end in cut:
        drop.update(range(start, end))
        if end < len(lines) and not lines[end].strip():
            drop.add(end)
    header_index = cls.lineno - 1
    mixins = [g["class"] for g in new_groups] + [b for b in current_bases if b in applied]
    new_lines = []
    for i, line in enumerate(lines):
        if i in drop:
            continue
        if i == header_index:
            for g in new_groups:
                module = "lib.routes." + pathlib.PurePosixPath(g["module"]).stem
                new_lines.append(f"from {module} import {g['class']}  # noqa: E402  (s3_apply)\n")
            if new_groups:
                new_lines.append("\n\n")
            new_lines.append(_bases_line(line, mixins))
            continue
        new_lines.append(line)
    out[SERVER] = "".join(new_lines)
    return out


def main(argv: list[str]) -> int:
    global ROOT
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=pathlib.Path, default=None)
    parser.add_argument("--recipe", type=pathlib.Path, default=None)
    parser.add_argument("--check", action="store_true")
    which = parser.add_mutually_exclusive_group(required=True)
    which.add_argument("--all", action="store_true")
    which.add_argument("--helpers", action="store_true")
    which.add_argument("--group", action="append")
    args = parser.parse_args(argv)
    root = (args.root or ROOT).resolve()
    recipe = args.recipe or (root / "tools" / "refactor" / "s3_moves.toml")
    try:
        groups = load_recipe(recipe)
        helpers = load_helpers(recipe)
        helpers_ack = _acknowledged(tomllib.loads(recipe.read_text()).get("helpers", {}), "[helpers]")
        if args.group:
            unknown = sorted(set(args.group) - {g["name"] for g in groups})
            if unknown:
                raise Refusal(f"recipe has no group {unknown}")
            # A group is applied with every group before it in recipe order
            # (those already applied are left as they are).
            last = max(i for i, g in enumerate(groups) if g["name"] in args.group)
            groups = groups[:last + 1]
        changes = apply_helpers(root, helpers, helpers_ack)
        if not args.helpers:
            if changes:
                # Groups see server.py as the helpers step leaves it.
                import shutil
                import tempfile
                # A directory this invocation creates and owns; nothing pre-existing is touched.
                staged = pathlib.Path(tempfile.mkdtemp(prefix="s3-apply-stage-"))
                shutil.copytree(root / "server", staged / "server")
                for path, text in changes.items():
                    (staged / path).parent.mkdir(parents=True, exist_ok=True)
                    (staged / path).write_text(text)
                try:
                    changes.update(apply(staged, groups, scan_root=root))
                finally:
                    shutil.rmtree(staged)
            else:
                changes = apply(root, groups)
    except Refusal as exc:
        print(f"s3_apply: REFUSED: {exc}")
        return 2
    for path in sorted(changes):
        print(f"s3_apply: {'would write' if args.check else 'wrote'} {path}")
        if not args.check:
            (root / path).parent.mkdir(parents=True, exist_ok=True)
            (root / path).write_text(changes[path])
    if not changes:
        print("s3_apply: nothing to do")
    return 1 if (args.check and changes) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
