#!/usr/bin/env python3
"""Apply S9 module splits: top-level definitions out of a large module into new ones.

    python tools/refactor/split_apply.py [--root REPO] [--recipes DIR] [--check] (--all | --split NAME ...)
    python tools/refactor/split_apply.py [--root REPO] [--recipes DIR] --list

A split is data (one TOML file per lane in tools/refactor/splits/, applied in
file-name order, entries in file order) plus this generator. Like the S3 group
moves it is re-applied to whatever module text is in the tree, never merged by
hand: the merge script takes main's old module, removes the generated parts and
runs this again, so fixes to the old module on main keep flowing.

    [[split]]
    name = "agent-live-settings"           # unique across all recipe files
    module = "lib/agent_live_tools.py"      # server-relative old module
    [[split.part]]
    module = "lib/agent_live_settings.py"   # new module, same directory as the old one
    names = ["PROFILE_KEY", "frozen_config"]

    [[repoint]]
    name = "s7-wav-to-mp3-import"
    file = "lib/custom_tts_adapters.py"     # where an import names a moved definition
    from_module = ".tts_providers"
    to_module = ".audio_codec"
    names = ["_wav_to_mp3"]

For each split it:

- cuts every listed definition from the old module as source text (decorators
  and the comment lines directly above it travel with it), so bodies and
  comments move byte for byte;
- writes each part with a generated header, the old module's own
  `from __future__` line, the old module's own import statements narrowed to
  the names the part reads (in the old module's order), an import of every
  name it reads from an earlier part, and the definitions in old-module order;
- leaves the old module as a facade: at the place of each part's first
  definition it imports that part's names back, so `lib.<old>.<name>` is the
  moved object and hooks, scripts, skills and tests that import from the old
  module keep working.

It refuses, naming the definition, when a listed name is not one plain
top-level definition, when two entries list it, when a part is not in the old
module's directory, when a moved definition reads a name the old module keeps
defining (that would import the facade back: a cycle) or a name of a later
part, and when anything rebinds a moved name or an import a moved definition
reads through the old module (`setattr`, `monkeypatch.setattr`, `patch(...)`,
attribute assignment), because after the move that patch would no longer
reach the moved code. Retarget the patch to the new module in the same commit,
or acknowledge it with evidence (`acknowledged_patches`, as in s3_moves.toml).

Verification is separate (the merge script runs it per commit):
`scripts/refactor/ast_identity.py BASE HEAD <old>=<old>,<part>,...` proves the
facade plus the parts equal the old module. A repoint is an import-only change
of one file, verified with `<file>=<file>`.

`--check` exits 1 when applying would change a file (CI drift check). Exit codes:
0 applied or already applied, 1 drift (--check), 2 refused or bad recipe.
"""
from __future__ import annotations

import argparse
import ast
import builtins
import pathlib
import sys
import tomllib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from s3_apply import (  # noqa: E402  - shared S3 machinery, unchanged
    Refusal, _acknowledged, _ast_identity, _call_arg, _file_bindings, _futures,
    _import_lines, _span, _top_definitions, _tracked_python, _unacknowledged,
    module_bindings, rebound_imports, refuse_dynamic_globals, refuse_wildcards,
)

RECIPES = "tools/refactor/splits"
# Module attributes whose value depends on the module a definition lives in.
_MODULE_IDENTITY = frozenset({"__file__", "__name__", "__spec__", "__package__", "__loader__", "__path__"})
HEADER = ('"""Moved out of {old} by tools/refactor/split_apply.py (split {split!r}, recipe {recipe}).\n'
          '{old_name} imports these names back. Generated: change the recipe, not this file."""\n')


# ---- recipes ------------------------------------------------------------------

def load_recipes(directory: pathlib.Path) -> tuple[list[dict], list[dict]]:
    """Every [[split]] and [[repoint]] of every *.toml in `directory`, files in
    name order, entries in file order; refuses duplicates across files."""
    splits: list[dict] = []
    repoints: list[dict] = []
    seen_names: dict[str, str] = {}
    claimed: dict[tuple[str, str], str] = {}
    for path in sorted(directory.glob("*.toml")):
        data = tomllib.loads(path.read_text())
        unknown = set(data) - {"split", "repoint"}
        if unknown:
            raise Refusal(f"{path.name}: unknown top-level keys {sorted(unknown)}")
        for kind, bucket in (("split", splits), ("repoint", repoints)):
            for entry in data.get(kind, []):
                name = str(entry.get("name") or "")
                if not name:
                    raise Refusal(f"{path.name}: a [[{kind}]] entry has no name")
                if name in seen_names:
                    raise Refusal(f"{path.name}: entry {name!r} is also defined in {seen_names[name]}")
                seen_names[name] = path.name
                entry = {**entry, "recipe": f"{RECIPES}/{path.name}"}
                if kind == "split":
                    _check_split(entry)
                    for part in entry["part"]:
                        for moved in part["names"]:
                            key = (entry["module"], moved)
                            if key in claimed:
                                raise Refusal(f"{path.name}: {entry['module']}:{moved} is listed by "
                                              f"{claimed[key]!r} and {name!r}")
                            claimed[key] = name
                else:
                    _check_repoint(entry)
                bucket.append(entry)
    return splits, repoints


def _check_split(entry: dict) -> None:
    name, module, parts = entry["name"], entry.get("module"), entry.get("part")
    if not isinstance(module, str) or not module.endswith(".py") or not module.startswith("lib/"):
        raise Refusal(f"split {name!r}: module must be a server-relative lib/... .py path")
    if not isinstance(parts, list) or not parts:
        raise Refusal(f"split {name!r}: needs at least one [[split.part]]")
    old_dir = pathlib.PurePosixPath(module).parent
    targets = set()
    for part in parts:
        target = part.get("module")
        names = part.get("names")
        if (not isinstance(target, str) or not target.endswith(".py")
                or pathlib.PurePosixPath(target).parent != old_dir or target == module):
            raise Refusal(f"split {name!r}: part module {target!r} must be a new .py file in {old_dir}/")
        if target in targets:
            raise Refusal(f"split {name!r}: part {target} listed twice")
        targets.add(target)
        if not isinstance(names, list) or not names or not all(isinstance(n, str) and n for n in names):
            raise Refusal(f"split {name!r}: part {target} needs a non-empty names list")
        if len(set(names)) != len(names):
            raise Refusal(f"split {name!r}: part {target} lists a name twice")


def _check_repoint(entry: dict) -> None:
    name = entry["name"]
    for key in ("file", "from_module", "to_module"):
        if not isinstance(entry.get(key), str) or not entry[key]:
            raise Refusal(f"repoint {name!r}: {key} is required")
    names = entry.get("names")
    if not isinstance(names, list) or not names or not all(isinstance(n, str) and n for n in names):
        raise Refusal(f"repoint {name!r}: needs a non-empty names list")


def _dotted(module_path: str) -> str:
    return pathlib.PurePosixPath(module_path).with_suffix("").as_posix().replace("/", ".")


# ---- rebinding through the old module ----------------------------------------

def old_module_rebinds(root: pathlib.Path, old: str, names: set[str], skip: set[str]) -> list[tuple[str, int, str, str]]:
    """Every place in a tracked Python file that rebinds one of `names` through the
    OLD module (where, after the split, it no longer reaches the moved code):
    attribute assignment or delete, setattr/delattr/monkeypatch.setattr,
    patch.object/patch.multiple on an object bound to the old module, and a patch
    target string `lib.<old>.<name>`. Fail closed: a name is the old module when
    any binding of it in the file imports the old module, or when it is the
    dotted `lib.<old>`."""
    dotted = _dotted(old)
    stem = dotted.rpartition(".")[2]
    sites: list[tuple[str, int, str, str]] = []
    for path in _tracked_python(root):
        rel = path.relative_to(root).as_posix()
        if rel in skip:
            continue
        try:
            module = ast.parse(path.read_text())
        except (SyntaxError, UnicodeDecodeError, OSError):
            continue
        bindings = _file_bindings(module)
        aliases = set()
        for bound, found in bindings.items():
            for node, _ in found:
                if isinstance(node, ast.ImportFrom) and any(
                        a.name == stem and (a.asname or a.name) == bound for a in node.names) and (
                        node.module in ("lib", None) or (node.module or "").endswith(".lib")
                        or (node.level and not node.module)):
                    aliases.add(bound)
                if isinstance(node, ast.Import) and any(
                        a.name == dotted and a.asname == bound for a in node.names):
                    aliases.add(bound)

        def is_old(expr) -> bool:
            if isinstance(expr, ast.Name):
                return expr.id in aliases
            return ast.unparse(expr) == dotted

        def named(value):
            return value.value if isinstance(value, ast.Constant) and isinstance(value.value, str) else None

        def add(lineno, name, text):
            sites.append((rel, lineno, name, f"{rel}:{lineno}: {text}"))

        for node in ast.walk(module):
            if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign, ast.Delete)):
                targets = node.targets if isinstance(node, (ast.Assign, ast.Delete)) else [node.target]
                for t in targets:
                    for sub in ast.walk(t):
                        # Only an attribute that is itself the store target rebinds
                        # (`old.X = v`); `d[old.X] = v` merely reads it.
                        if (isinstance(sub, ast.Attribute) and isinstance(sub.ctx, (ast.Store, ast.Del))
                                and sub.attr in names and is_old(sub.value)):
                            add(node.lineno, sub.attr, f"assigns or deletes {dotted}.{sub.attr}")
            elif isinstance(node, ast.Call):
                func = ast.unparse(node.func)
                if func.endswith("patch.multiple"):
                    obj = _call_arg(node, 0, "target")
                    old_target = obj is not None and (is_old(obj) or named(obj) == dotted)
                    for attr in [kw.arg for kw in node.keywords if kw.arg in names]:
                        if old_target:
                            add(node.lineno, attr, f"{func}({dotted}, {attr}=...)")
                elif func.endswith(("setattr", "delattr", "patch.object")):
                    obj = _call_arg(node, 0, "target")
                    attr = _call_arg(node, 1, "attribute" if func.endswith("patch.object") else "name")
                    if named(obj) is not None and func.endswith(("setattr", "delattr")):
                        target = named(obj)
                        head, _, name = target.rpartition(".")
                        if name in names and head == dotted:
                            add(node.lineno, name, f"{func}({target!r})")
                    elif named(attr) in names and obj is not None and is_old(obj):
                        add(node.lineno, named(attr), f"{func}({ast.unparse(obj)}, {named(attr)!r})")
                elif func.endswith("patch"):
                    target = named(_call_arg(node, 0, "target"))
                    if target:
                        head, _, name = target.rpartition(".")
                        if name in names and head == dotted:
                            add(node.lineno, name, f"{func}({target!r})")
    return sorted(set(sites))


# ---- one split -----------------------------------------------------------------

def _stmt_reads(tool, stmt: ast.stmt) -> set[str]:
    if isinstance(stmt, (ast.Assign, ast.AnnAssign)):
        value = stmt.value if stmt.value is not None else stmt
        reads = {n.id for n in ast.walk(value) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
        if isinstance(stmt, ast.AnnAssign):
            reads |= {n.id for n in ast.walk(stmt.annotation) if isinstance(n, ast.Name)}
        return reads
    return tool.global_reads(stmt)


def apply_split(root: pathlib.Path, split: dict) -> dict[str, str]:
    """New text per changed file (paths relative to root); {} when already
    applied; refuses on any problem."""
    tool = _ast_identity()
    old = split["module"]
    old_path = root / "server" / old
    if not old_path.exists():
        raise Refusal(f"split {split['name']!r}: {old} does not exist")
    text = old_path.read_text()
    lines = text.splitlines(keepends=True)
    refuse_dynamic_globals(text)
    tree = ast.parse(text)
    refuse_wildcards(tree)
    package = _dotted(old).rpartition(".")[0]
    imports = tool.top_imports(tree, package)
    part_of: dict[str, int] = {}
    for index, part in enumerate(split["part"]):
        for name in part["names"]:
            part_of[name] = index
    homes = [_dotted(p["module"]) for p in split["part"]]
    # Already applied: every name imported back from its part, and every part exists.
    if all(imports.get(n) == (homes[i], n) for n, i in part_of.items()):
        if all((root / "server" / p["module"]).exists() for p in split["part"]):
            return {}
    if any(imports.get(n) == (homes[i], n) for n, i in part_of.items()) or any(
            (root / "server" / p["module"]).exists() for p in split["part"]):
        raise Refusal(f"split {split['name']!r} is partly applied; re-apply from main's {old}")
    defs = _top_definitions(tree)
    binding_map = module_bindings(tree)
    nodes: dict[str, ast.stmt] = {}
    for name in part_of:
        stmts = defs.get(name, [])
        if (len(stmts) != 1 or len(binding_map.get(name, [])) != 1 or not isinstance(
                stmts[0], (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Assign, ast.AnnAssign))):
            raise Refusal(f"split {split['name']!r}: {name} is not a single plain top-level definition "
                          f"of {old} (bindings: {binding_map.get(name, [])})")
        stmt = stmts[0]
        if isinstance(stmt, (ast.Assign, ast.AnnAssign)):
            # ast_identity matches one name per assignment; a multi-name
            # assignment (`A, B = 1, 2`) cannot be proved, so it stays.
            targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
            bound = {n.id for t in targets for n in ast.walk(t) if isinstance(n, ast.Name)}
            if bound != {name}:
                raise Refusal(f"split {split['name']!r}: {name} is assigned together with "
                              f"{sorted(bound - {name})}; ast_identity cannot prove a multi-name assignment")
        identity = sorted({n.id for n in ast.walk(stmt) if isinstance(n, ast.Name)} & _MODULE_IDENTITY)
        if identity:
            raise Refusal(f"split {split['name']!r}: {name} reads {', '.join(identity)}, which differ in its "
                          "new module; keep it in the old module")
        nodes[name] = stmt
    rebound = rebound_imports(tree, tool)
    needed: list[set[str]] = [set() for _ in split["part"]]
    sibling: list[dict[int, set[str]]] = [{} for _ in split["part"]]
    for name, stmt in nodes.items():
        index = part_of[name]
        for read in sorted(_stmt_reads(tool, stmt)):
            if read == name:
                continue
            if read in part_of:
                other = part_of[read]
                if other > index:
                    raise Refusal(f"split {split['name']!r}: {name} reads {read}, which moves to a later part "
                                  f"({split['part'][other]['module']}); order the parts so reads go backwards")
                if other < index:
                    sibling[index].setdefault(other, set()).add(read)
                continue
            if read in rebound:
                raise Refusal(f"split {split['name']!r}: {name} reads {read}, which {old} imports and also "
                              f"rebinds ({rebound[read]}); prepare it first")
            if read in imports:
                needed[index].add(read)
            elif read in defs:
                raise Refusal(f"split {split['name']!r}: {name} reads {read}, which stays in {old}; "
                              f"move {read} too (into this or an earlier part) or keep {name}")
            elif not hasattr(builtins, read):
                raise Refusal(f"split {split['name']!r}: {name} reads {read}, which {old} does not bind "
                              f"with a plain top-level import")
    skip = {f"server/{p['module']}" for p in split["part"]}
    acknowledged = _acknowledged(split, f"split {split['name']!r}")
    # A patch through the old module still reaches code that stays there (it
    # reads the facade's name); it misses only moved code, so watch exactly the
    # names moved definitions read: moved names and the imports they need.
    watched = set().union(*needed)
    for name, stmt in nodes.items():
        watched |= {read for read in _stmt_reads(tool, stmt) if read in part_of and read != name}
    sites = _unacknowledged(old_module_rebinds(root, old, watched, skip), acknowledged)
    if sites:
        raise Refusal(f"split {split['name']!r}: names rebound through {old}; after the split the patch "
                      "would not reach the moved code (retarget it to the new module in the same commit, "
                      "or acknowledge it with evidence):\n  " + "\n  ".join(sites))
    futures = _futures(tree, lines)
    out: dict[str, str] = {}
    drop: set[int] = set()
    first_line: dict[int, int] = {}
    for index, part in enumerate(split["part"]):
        members = sorted(((name, nodes[name]) for name in part["names"]), key=lambda item: item[1].lineno)
        spans = [_span(lines, stmt) for _, stmt in members]
        # Definitions that were neighbours (only blank lines between) keep their
        # original spacing; others are separated by two blank lines.
        blocks: list[list[int]] = []
        for a, b in spans:
            if blocks and all(not lines[i].strip() for i in range(blocks[-1][1], a)) and a - blocks[-1][1] <= 2:
                blocks[-1][1] = b
            else:
                blocks.append([a, b])
        bodies = ["".join(lines[a:b]).rstrip("\n") + "\n" for a, b in blocks]
        first_line[index] = spans[0][0]
        for a, b in spans:
            drop.update(range(a, b))
            nxt = b
            while nxt < len(lines) and not lines[nxt].strip() and nxt - b < 2:
                drop.add(nxt)
                nxt += 1
        chunks = [HEADER.format(old=f"server/{old}", split=split["name"], recipe=split["recipe"],
                                old_name=pathlib.PurePosixPath(old).name)]
        if futures:
            chunks.append("".join(futures))
        import_lines = _import_lines(tree, needed[index])
        for other in sorted(sibling[index]):
            stem = pathlib.PurePosixPath(split["part"][other]["module"]).stem
            import_lines.append(f"from .{stem} import {', '.join(sorted(sibling[index][other]))}")
        if import_lines:
            chunks.append("\n".join(import_lines) + "\n")
        body = "\n".join(c.rstrip("\n") + "\n" for c in chunks) + "\n\n" + "\n\n".join(bodies)
        out["server/" + part["module"]] = body
    back = {first_line[i]: _facade_import(p, nodes, split["name"]) for i, p in enumerate(split["part"])}
    new_lines = []
    for i, line in enumerate(lines):
        if i in back:
            new_lines.append(back[i])
        if i in drop:
            continue
        new_lines.append(line)
    out["server/" + old] = "".join(new_lines)
    return out


def _facade_import(part: dict, nodes: dict[str, ast.stmt], split_name: str) -> str:
    """The old module's import of one part's names, in old-module order, wrapped."""
    stem = pathlib.PurePosixPath(part["module"]).stem
    names = [n for n, _ in sorted(((n, nodes[n]) for n in part["names"]), key=lambda x: (x[1].lineno, x[0]))]
    rows, row = [], "   "
    for name in names:
        if len(row) + len(name) + 2 > 100:
            rows.append(row.rstrip())
            row = "   "
        row += f" {name},"
    rows.append(row.rstrip())
    return (f"from .{stem} import (  # noqa: E402,F401  (split_apply: {split_name})\n"
            + "\n".join(rows) + "\n)\n")


# ---- one repoint ----------------------------------------------------------------

def apply_repoint(root: pathlib.Path, entry: dict) -> dict[str, str]:
    """Rewrite `from <from_module> import <names>` to `<to_module>` in one file, at
    every scope; only statements importing exactly a subset of `names` change."""
    path = root / "server" / entry["file"]
    if not path.exists():
        raise Refusal(f"repoint {entry['name']!r}: {entry['file']} does not exist")
    text = path.read_text()
    tree = ast.parse(text)
    lines = text.splitlines(keepends=True)
    wanted = set(entry["names"])

    def spelled(node: ast.ImportFrom) -> str:
        return "." * node.level + (node.module or "")

    hits, done = [], []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported = {a.name for a in node.names}
            if spelled(node) == entry["from_module"] and imported <= wanted:
                hits.append(node)
            elif spelled(node) == entry["to_module"] and imported <= wanted:
                done.append(node)
    if not hits:
        if done:
            return {}
        raise Refusal(f"repoint {entry['name']!r}: no `from {entry['from_module']} import ...` of "
                      f"{sorted(wanted)} in {entry['file']}")
    for node in hits:
        if node.lineno != node.end_lineno:
            raise Refusal(f"repoint {entry['name']!r}: {entry['file']}:{node.lineno} spans several lines")
        line = lines[node.lineno - 1]
        old_spelling = f"from {entry['from_module']} import"
        if line.count(old_spelling) != 1:
            raise Refusal(f"repoint {entry['name']!r}: {entry['file']}:{node.lineno} is not one plain import")
        lines[node.lineno - 1] = line.replace(old_spelling, f"from {entry['to_module']} import")
    return {"server/" + entry["file"]: "".join(lines)}


# ---- command line ------------------------------------------------------------------

def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--root", default=str(pathlib.Path(__file__).resolve().parents[2]))
    parser.add_argument("--recipes", default=None)
    parser.add_argument("--check", action="store_true")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--all", action="store_true")
    group.add_argument("--split", action="append", default=[])
    group.add_argument("--repoint", action="append", default=[])
    group.add_argument("--list", action="store_true")
    args = parser.parse_args(argv)
    root = pathlib.Path(args.root).resolve()
    recipes = pathlib.Path(args.recipes) if args.recipes else root / RECIPES
    try:
        splits, repoints = load_recipes(recipes)
        if args.list:
            for entry in splits:
                print(f"split {entry['name']} {entry['module']}="
                      + ",".join([entry["module"]] + [p["module"] for p in entry["part"]]))
            for entry in repoints:
                print(f"repoint {entry['name']} {entry['file']}={entry['file']}")
            return 0
        chosen: list[tuple[str, dict]] = []
        if args.all:
            chosen = [("split", e) for e in splits] + [("repoint", e) for e in repoints]
        else:
            by_name = {e["name"]: e for e in splits}
            by_repoint = {e["name"]: e for e in repoints}
            for name in args.split:
                if name not in by_name:
                    raise Refusal(f"no split named {name!r}")
                chosen.append(("split", by_name[name]))
            for name in args.repoint:
                if name not in by_repoint:
                    raise Refusal(f"no repoint named {name!r}")
                chosen.append(("repoint", by_repoint[name]))
        drift = False
        for kind, entry in chosen:
            changes = apply_split(root, entry) if kind == "split" else apply_repoint(root, entry)
            if not changes:
                print(f"split_apply: {kind} {entry['name']} already applied")
                continue
            drift = True
            if args.check:
                print(f"split_apply: {kind} {entry['name']} would change {', '.join(sorted(changes))}")
                continue
            for rel, new_text in changes.items():
                (root / rel).write_text(new_text)
            print(f"split_apply: {kind} {entry['name']} applied: {', '.join(sorted(changes))}")
        return 1 if (args.check and drift) else 0
    except Refusal as refusal:
        print(f"split_apply: REFUSED: {refusal}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
