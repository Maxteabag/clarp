"""Structural: every statement in server/lib that can make a message id disappear from a conversation
sits in a function that also raises conversation_heads.replace_revision (448's follow-up A).

The apps merge /log deltas by id and never remove a row; replace_revision is what tells them to
reload (message_previews.requires_replace). A statement can make an id disappear in four ways: a
DELETE FROM messages, an UPDATE that sets message_id (a rename), an UPDATE that moves a row to
another conversation (backend_session_id or agent_id), and INSERT OR REPLACE into messages. A
function that raises it through its caller is named below with its reason, and each such caller is
checked too.

What counts as the raise is a WRITE of it: a SQL string in the same function that inserts into or
updates conversation_heads and sets replace_revision. A comment, a docstring or a read that names
replace_revision is not a raise. A function named as raising through its caller must hand the
removal on after EACH of its id-removing statements: return the statement's result, or set its flag
('changed') later in the same block to True or to something read from that statement's result.

What this guard CANNOT see (it reads text, it runs nothing):
- SQL built at run time: a table name in an f-string or a format call, strings joined with +;
- a quoted or schema-qualified table name ("messages", main.messages);
- an upsert that changes a row's id (INSERT ... ON CONFLICT DO UPDATE SET message_id = ...);
- a statement outside server/lib (scripts, bin, plugin, skills, tools);
- whether the raise is on the same path as the removal, or before the transaction commits: a raise
  in the same function on another branch, or after its commit, passes; it is a text test, not a
  transaction test (the behaviour tests in test_import_raises_replace.py and
  test_live_writers_raise_replace.py are);
- R107's shape: a row that should have been retracted and was not involves no DELETE at all, so
  nothing here sees it."""
from __future__ import annotations

import ast
import pathlib
import re

import pytest

pytestmark = pytest.mark.structural

LIB = pathlib.Path(__file__).resolve().parents[2] / "server" / "lib"
REMOVES_AN_ID = re.compile(
    r"\bDELETE\s+FROM\s+messages\b"
    r"|\bUPDATE\s+messages\s+SET\s+(?:(?!\bWHERE\b).)*?\b(?:message_id|backend_session_id|agent_id)\s*="
    r"|\b(?:INSERT\s+OR\s+REPLACE|REPLACE)\s+INTO\s+messages\b",
    re.IGNORECASE | re.DOTALL)
# The raise is a write of it, not the word: an INSERT into or UPDATE of conversation_heads that sets
# replace_revision, in a SQL string of the function itself.
RAISES = re.compile(
    r"\b(?:INSERT\s+(?:OR\s+\w+\s+)?INTO|UPDATE)\s+conversation_heads\b.*?\breplace_revision\b",
    re.IGNORECASE | re.DOTALL)

# (module, qualified function): why it does not raise replace_revision itself.
THROUGH_ITS_CALLER = {
    ("message_import_rows", "_delete_slot"):
        "clears an import slot; every caller folds the count into skipped_slot_removed, which the batch "
        "head raises before the batch commits (message_writes)",
    ("message_import_rows", "_resolve_reply_id"):
        "sheds a reply's twin or a slot's earlier row; every caller folds 'changed' into "
        "skipped_slot_removed, raised by the batch head before the batch commits",
}
# Every caller of a THROUGH_ITS_CALLER function carries this in the same function; one that is itself
# named there passes the removal on in what it returns.
CALLER_CARRIES = "skipped_slot_removed"
PASSES_IT_ON = "changed"
NOT_A_WRITE = {
    ("message_import_writer", "_PlanWriter.execute"):
        "plans an import: records which ids a DELETE would remove, against a read; the import's real "
        "statements run in message_writes under the batch head",
}


def _functions(tree):
    """Every function with its qualified name (Class.method)."""
    def walk(node, prefix):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = prefix + child.name
                yield name, child
                yield from walk(child, name + ".")
            elif isinstance(child, ast.ClassDef):
                yield from walk(child, prefix + child.name + ".")
            else:
                yield from walk(child, prefix)
    return list(walk(tree, ""))


def _raises(fn) -> bool:
    """The function writes replace_revision itself: a SQL string that inserts into or updates
    conversation_heads and sets it."""
    return any(isinstance(node, ast.Constant) and isinstance(node.value, str) and RAISES.search(node.value)
               for node in ast.walk(fn))


def _sites():
    """(module, qualified function, line, the function's node) of every id-removing statement."""
    found = []
    for path in sorted(LIB.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        module = ".".join(path.relative_to(LIB).with_suffix("").parts)
        functions = _functions(tree)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and REMOVES_AN_ID.search(node.value)):
                continue
            owner = [(name, fn) for name, fn in functions
                     if fn.lineno <= node.lineno <= (fn.end_lineno or fn.lineno)]
            if not owner:
                found.append((module, "<module>", node.lineno, None))
                continue
            name, fn = max(owner, key=lambda item: item[1].lineno)     # the innermost
            found.append((module, name, node.lineno, fn))
    return found


def test_every_id_that_disappears_raises_replace_revision():
    sites = _sites()
    assert sites, "the scan found no statement at all: the pattern no longer matches the code"
    unexplained = [f"lib/{module.replace('.', '/')}.py:{line} {name}" for module, name, line, fn in sites
                   if not (fn is not None and _raises(fn)) and (module, name) not in THROUGH_ITS_CALLER
                   and (module, name) not in NOT_A_WRITE]
    assert not unexplained, (
        "a message id can disappear here without raising conversation_heads.replace_revision in the same "
        "function; raise it in the same transaction, or name the function in this test with its reason: "
        + "; ".join(unexplained))


def test_every_function_named_here_still_removes_an_id():
    present = {(module, name) for module, name, _line, _fn in _sites()}
    stale = sorted(set(THROUGH_ITS_CALLER) | set(NOT_A_WRITE))
    stale = [entry for entry in stale if entry not in present]
    assert not stale, f"named here but no longer removing an id: {stale}"


def _block_of(statement, parents):
    """The list holding `statement` and its index there, or (None, -1)."""
    holder = parents[statement]
    for field in ("body", "orelse", "finalbody", "handlers"):
        block = getattr(holder, field, None)
        if isinstance(block, list) and statement in block:
            return block, block.index(statement)
    return None, -1


def _statement_of(node, parents):
    while not isinstance(node, ast.stmt):
        node = parents[node]
    return node


def _bound(statement):
    return {node.id for target in getattr(statement, "targets", []) for node in ast.walk(target)
            if isinstance(node, ast.Name)}


def _folds(assignment, carrier, bound) -> bool:
    """`assignment` sets the carrier to True, or to something that reads what the removal bound:
    `changed = changed` or `changed = changed or other` hands nothing on."""
    if not isinstance(assignment, (ast.Assign, ast.AugAssign)):
        return False
    targets = getattr(assignment, "targets", None) or [assignment.target]
    if not any(isinstance(node, ast.Name) and node.id == carrier for target in targets for node in ast.walk(target)):
        return False
    value = assignment.value
    if isinstance(value, ast.Constant) and value.value is True:
        return True
    return bool(bound) and any(isinstance(node, ast.Name) and node.id in bound for node in ast.walk(value))


def test_a_function_that_raises_through_its_caller_hands_on_each_of_its_removals():
    """Each id-removing statement in such a function is returned, or is followed in its block by
    setting 'changed' to True or to its result: a second DELETE added without it is seen."""
    unhanded, seen = [], 0
    for path in sorted(LIB.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        module = ".".join(path.relative_to(LIB).with_suffix("").parts)
        parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
        for name, fn in _functions(tree):
            if (module, name) not in THROUGH_ITS_CALLER:
                continue
            nested = {id(node) for child in ast.walk(fn) if child is not fn
                      and isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) for node in ast.walk(child)}
            for node in ast.walk(fn):
                if id(node) in nested or not (isinstance(node, ast.Constant) and isinstance(node.value, str)
                                              and REMOVES_AN_ID.search(node.value)):
                    continue
                seen += 1
                statement = _statement_of(node, parents)
                if isinstance(statement, ast.Return):
                    continue
                block, index = _block_of(statement, parents)
                later = block[index + 1:] if block is not None else []
                if not any(_folds(following, PASSES_IT_ON, _bound(statement)) for following in later):
                    unhanded.append(f"{path.relative_to(LIB)}:{node.lineno} {name}")
    assert seen, "no removal found in the functions named here: the scan no longer matches the code"
    assert not unhanded, f"a removal here is not handed on to the caller: {unhanded}"


def _next_statement(call, parents):
    """The statement holding `call`, the names it binds, and the statement right after it in its block."""
    statement = call
    while not isinstance(statement, ast.stmt):
        statement = parents[statement]
    holder = parents[statement]
    for field in ("body", "orelse", "finalbody", "handlers"):
        block = getattr(holder, field, None)
        if isinstance(block, list) and statement in block:
            index = block.index(statement)
            following = block[index + 1] if index + 1 < len(block) else None
            break
    else:
        following = None
    bound = {node.id for target in getattr(statement, "targets", []) for node in ast.walk(target)
             if isinstance(node, ast.Name)}
    return statement, bound, following


def test_every_call_of_a_function_that_raises_through_its_caller_folds_its_result_into_the_raise():
    """Right after each call, its result is folded into the carrier: skipped_slot_removed in the
    importer, or 'changed' (what the named function returns) inside a named function."""
    helpers = {name for (_module, name) in THROUGH_ITS_CALLER}
    dropped, calls = [], 0
    for path in sorted(LIB.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
        for name, fn in _functions(tree):
            for call in ast.walk(fn):
                if not isinstance(call, ast.Call):
                    continue
                callee = call.func.id if isinstance(call.func, ast.Name) else getattr(call.func, "attr", "")
                if callee not in helpers or name == callee:
                    continue
                calls += 1
                carrier = PASSES_IT_ON if name in helpers else CALLER_CARRIES
                statement, bound, following = _next_statement(call, parents)
                if not (following is not None and _folds(following, carrier, bound)):
                    dropped.append(f"{path.relative_to(LIB)}:{call.lineno} {name} calls {callee}")
    assert calls, "no caller found: the helpers moved"
    assert not dropped, f"these calls drop the removal on the floor: {dropped}"


@pytest.mark.parametrize("body, raises", [
    ('    # this raises replace_revision, the comment says\n    db.execute("DELETE FROM messages WHERE x=?", (1,))', False),
    ('    """Raises replace_revision."""\n    db.execute("DELETE FROM messages WHERE x=?", (1,))', False),
    ('    db.execute("SELECT replace_revision FROM conversation_heads WHERE agent_id=?", (1,))', False),
    ('    db.execute("UPDATE conversation_heads SET replace_revision = ? WHERE agent_id=?", (2, 1))', True),
    ('    db.execute("""INSERT INTO conversation_heads(agent_id, backend_session_id, revision, replace_revision)\n'
     '                  VALUES(?,?,?,?) ON CONFLICT DO UPDATE SET replace_revision = excluded.replace_revision""", ())',
     True),
], ids=["a comment", "a docstring", "a read", "an update", "an upsert"])
def test_only_a_write_of_replace_revision_counts_as_the_raise(body, raises):
    fn = ast.parse("def removes(db):\n" + body + "\n").body[0]
    assert _raises(fn) is raises
