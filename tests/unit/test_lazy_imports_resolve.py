"""Every function-level import under server/ resolves (PT-14, slice S1).

About 1,400 imports in server/ sit inside functions, mostly to break import
cycles. Python only runs them when the function runs, so a move in slice S1
that renames or splits a module can break one silently until that code path
runs in production. This test finds each one with the AST, resolves it with
importlib.util.find_spec (relative imports against the importing module's
package), and for `from X import name` also checks that `name` is a submodule
of X or bound in X's module scope (a module-level __getattr__ counts as
binding everything).

Server-internal targets (lib.*) must always resolve. A third-party import may
be missing only when it is optional: in the body of a try whose handler
catches ImportError, ModuleNotFoundError or Exception (whisper and torch in
stt.py). Its except, else and finally suites are not protected by that
handler, and a function defined inside the try runs later, outside it.
"""
from __future__ import annotations

import ast
import functools
import importlib
import importlib.util
import pathlib

SERVER = pathlib.Path(__file__).resolve().parents[2] / "server"
INTERNAL = ("lib", "server")
# A third-party import in a try body guarded by one of these is optional.
_OPTIONAL_HANDLERS = frozenset({"ImportError", "ModuleNotFoundError", "Exception", "BaseException"})
_DEFERRED = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
_TRY = tuple(t for t in (getattr(ast, "Try", None), getattr(ast, "TryStar", None)) if t)


def _module_name(path: pathlib.Path, root: pathlib.Path) -> str:
    parts = list(path.relative_to(root).with_suffix("").parts)
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def _caught(handler: ast.ExceptHandler) -> set[str]:
    """Exception class names a handler catches; a bare except catches everything."""
    if handler.type is None:
        return {"BaseException"}
    nodes = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    return {n.id if isinstance(n, ast.Name) else n.attr for n in nodes if isinstance(n, (ast.Name, ast.Attribute))}


def lazy_imports(tree: ast.AST) -> list[tuple[ast.stmt, bool]]:
    """(import node, optional) for every import inside a function body.

    `optional` holds only while execution is inside the body of a try whose
    handlers catch an import failure; a nested function body starts unprotected.
    """
    found = []

    def visit(node: ast.AST, in_function: bool, protected: bool) -> None:
        if isinstance(node, _DEFERRED):
            in_function, protected = True, False
        if isinstance(node, _TRY):
            guards = any(_caught(h) & _OPTIONAL_HANDLERS for h in node.handlers)
            for stmt in node.body:
                visit(stmt, in_function, protected or guards)
            for part in (*node.handlers, *node.orelse, *node.finalbody):
                visit(part, in_function, protected)
            return
        if in_function and isinstance(node, (ast.Import, ast.ImportFrom)):
            found.append((node, protected))
        for child in ast.iter_child_nodes(node):
            visit(child, in_function, protected)

    visit(tree, False, False)
    return found


def _bound_names(statements: list[ast.stmt]) -> tuple[set[str], bool]:
    """Names bound in module scope, entering control flow but never a def or class body."""
    names: set[str] = set()
    dynamic = False

    def targets(node: ast.AST) -> None:
        names.update(n.id for n in ast.walk(node) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store))

    for stmt in statements:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(stmt.name)
            dynamic |= stmt.name == "__getattr__"
        elif isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                targets(target)
        elif isinstance(stmt, (ast.AnnAssign, ast.AugAssign)):
            targets(stmt.target)
        elif isinstance(stmt, (ast.Import, ast.ImportFrom)):
            names.update((a.asname or a.name).split(".")[0] for a in stmt.names)
        elif isinstance(stmt, (ast.For, ast.AsyncFor)):
            targets(stmt.target)
        elif isinstance(stmt, (ast.With, ast.AsyncWith)):
            for item in stmt.items:
                if item.optional_vars is not None:
                    targets(item.optional_vars)
        if isinstance(stmt, _TRY):
            for handler in stmt.handlers:
                if handler.name:
                    names.add(handler.name)
        for suite in ("body", "orelse", "finalbody") if not isinstance(
                stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) else ():
            inner, more = _bound_names(getattr(stmt, suite, []) or [])
            names |= inner
            dynamic |= more
        nested = [h.body for h in stmt.handlers] if isinstance(stmt, _TRY) else []
        nested += [case.body for case in getattr(stmt, "cases", [])] if isinstance(stmt, ast.Match) else []
        for body in nested:
            inner, more = _bound_names(body)
            names |= inner
            dynamic |= more
    return names, dynamic


@functools.cache
def _module_scope(origin: str) -> tuple[frozenset[str], bool]:
    names, dynamic = _bound_names(ast.parse(pathlib.Path(origin).read_text()).body)
    return frozenset(names), dynamic


def unresolved(path: pathlib.Path, root: pathlib.Path | None = None,
               internal: tuple[str, ...] = INTERNAL) -> list[str]:
    root = SERVER if root is None else root
    module = _module_name(path, root)
    package = module if path.name == "__init__.py" else module.rpartition(".")[0]
    problems = []
    for node, optional in lazy_imports(ast.parse(path.read_text(), filename=str(path))):
        if isinstance(node, ast.Import):
            targets = [(alias.name, None) for alias in node.names]
        else:
            base = (importlib.util.resolve_name("." * node.level + (node.module or ""), package)
                    if node.level else node.module)
            targets = [(base, alias.name) for alias in node.names]
        for base, name in targets:
            is_internal = base.split(".")[0] in internal
            try:
                spec = importlib.util.find_spec(base)
            except (ImportError, ValueError):
                spec = None
            where = f"{path.relative_to(root)}:{node.lineno}"
            if spec is None:
                if is_internal or not optional:
                    problems.append(f"{where}: no module {base}")
                continue
            if not name or name == "*" or not spec.origin or not spec.origin.endswith(".py"):
                continue
            if spec.submodule_search_locations and importlib.util.find_spec(f"{base}.{name}") is not None:
                continue
            if pathlib.Path(spec.origin).is_relative_to(root):
                names, dynamic = _module_scope(spec.origin)
                if name not in names and not dynamic:
                    problems.append(f"{where}: {base} has no module-level {name}")
    return problems


def test_every_function_level_import_resolves():
    problems = [p for path in sorted(SERVER.rglob("*.py")) if "tests" not in path.parts
                for p in unresolved(path)]
    assert not problems, "function-level imports that would fail when their code runs:\n" + "\n".join(problems)


def _package(tmp_path, monkeypatch, files: dict[str, str]) -> pathlib.Path:
    package = tmp_path / "probepkg"
    package.mkdir()
    (package / "__init__.py").write_text("")
    for name, text in files.items():
        (package / name).write_text(text)
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    return package


def _problems(tmp_path, package, name):
    return [p.split(": ", 1)[1] for p in unresolved(package / name, root=tmp_path, internal=("probepkg",))]


def test_only_a_protected_try_body_makes_a_third_party_import_optional(tmp_path, monkeypatch):
    package = _package(tmp_path, monkeypatch, {"importer.py": (
        "def body():\n"
        "    try:\n"
        "        import nomod_body\n"
        "        try:\n"
        "            import nomod_nested\n"
        "        except OSError:\n"
        "            pass\n"
        "    except ImportError:\n"
        "        pass\n"
        "def tuple_handler():\n"
        "    try:\n"
        "        import nomod_tuple\n"
        "    except (OSError, ModuleNotFoundError):\n"
        "        pass\n"
        "def in_except():\n"
        "    try:\n"
        "        pass\n"
        "    except ImportError:\n"
        "        import nomod_except\n"
        "def in_else():\n"
        "    try:\n"
        "        pass\n"
        "    except ImportError:\n"
        "        pass\n"
        "    else:\n"
        "        import nomod_else\n"
        "def in_finally():\n"
        "    try:\n"
        "        pass\n"
        "    except ImportError:\n"
        "        pass\n"
        "    finally:\n"
        "        import nomod_finally\n"
        "def deferred():\n"
        "    try:\n"
        "        def later():\n"
        "            import nomod_deferred\n"
        "        run = lambda: __import__('x')\n"
        "    except ImportError:\n"
        "        pass\n"
        "def unguarded():\n"
        "    import nomod_plain\n")})
    assert _problems(tmp_path, package, "importer.py") == [
        "no module nomod_except", "no module nomod_else", "no module nomod_finally",
        "no module nomod_deferred", "no module nomod_plain"]


def test_module_scope_stops_at_function_and_class_bodies(tmp_path, monkeypatch):
    package = _package(tmp_path, monkeypatch, {
        "target.py": (
            "if True:\n"
            "    def helper():\n"
            "        hidden = 1\n"
            "    visible = 2\n"
            "try:\n"
            "    from os import sep as optional_sep\n"
            "except ImportError:\n"
            "    optional_sep = '/'\n"
            "class Holder:\n"
            "    member = 1\n"
            "table = {}\n"
            "table[subscript_key] = 1\n"
            "match 1:\n"
            "    case 1:\n"
            "        matched = True\n"),
        "importer.py": (
            "def f():\n"
            "    from .target import helper, visible, optional_sep, Holder, matched\n"
            "    from .target import subscript_key\n"
            "    from .target import hidden\n"
            "    from .target import member\n"
            "    from .nomodule import x\n"
            "    from . import target\n")})
    assert _problems(tmp_path, package, "importer.py") == [
        "probepkg.target has no module-level subscript_key",
        "probepkg.target has no module-level hidden", "probepkg.target has no module-level member",
        "no module probepkg.nomodule"]
