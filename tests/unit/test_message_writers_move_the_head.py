"""Structural: every statement in server/lib that inserts, deletes or changes a message row sits in a
function that moves that conversation's head in the same transaction (R119, the invariant the import's
head check rests on: equal head = no message row of the conversation changed).

A function moves the head itself when it writes conversation_heads (an INSERT, an UPDATE or the
_row_with_its_head helper). The importer's row writers do not: they run inside the import's
transaction, whose batch head (commit_batch) and _finish_import raise the head with the import's
latest revision before every commit; they are named below with that reason. A new writer of
messages that does neither turns this red until it moves the head or is named here with its reason.

What it cannot see: SQL built at run time (an f-string table name), a statement outside server/lib,
and whether the head is moved on the same PATH as the write; the behaviour tests are
test_heads_at_every_commit (every commit of the paths it drives) and the R119 tests beside this one.
Not writers of message rows, checked: an agent's deletion is a soft delete (its rows stay), and no
maintenance pass prunes messages."""
from __future__ import annotations

import ast
import pathlib
import re

import pytest

pytestmark = pytest.mark.structural

LIB = pathlib.Path(__file__).resolve().parents[2] / "server" / "lib"
WRITES = re.compile(r"\b(INSERT\s+(?:OR\s+\w+\s+)?INTO\s+messages\b|DELETE\s+FROM\s+messages\b"
                    r"|UPDATE\s+messages\s+SET\b|REPLACE\s+INTO\s+messages\b)", re.I)
MOVES_HEAD = re.compile(r"\b(INSERT\s+(?:OR\s+\w+\s+)?INTO|UPDATE)\s+conversation_heads\b", re.I)
HEAD_HELPERS = ("_row_with_its_head",)

# (module, function): why its write moves the head although the function does not write it.
INSIDE_THE_IMPORT = {
    ("message_import_rows", "_delete_slot"): "import transaction; batch head / _finish_import",
    ("message_import_rows", "_park_adopted_occupant"): "import transaction; stamps the row, batch head",
    ("message_import_rows", "_resolve_reply_id"): "import transaction; batch head / _finish_import",
    ("message_import_rows", "_restore_unadopted_parked"): "import transaction; stamps the row, _finish_import",
    ("message_import_rows", "_upsert_row"): "import transaction; the row's revision is folded into latest_revision",
    ("message_writes", "_attach_authority_tools"): "import transaction (stamp_turn); _finish_import",
}
NOT_A_WRITE = {
    # Its text holds the write statements because it RECORDS them instead of running them: the plan
    # of an import (plan_transcript_tail), against one read snapshot, nothing written. Do not make it
    # move a head: there is no row to move one for.
    ("message_import_writer", "_PlanWriter.execute"): "records an import's writes as a plan; runs none",
}
# Writers that move the head themselves but open no transaction: their two statements (the row,
# the head) are one commit only because their caller holds BEGIN IMMEDIATE round the call. Each has
# exactly the one caller named; a second caller turns this red until it is shown to do the same.
CALLER_HOLDS_THE_TRANSACTION = {
    ("prompt_admissions", "update_for_queued_edit"): ("turn_queue", "update_text"),
}


def _functions(tree):
    out = []

    def walk(node, prefix):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = prefix + child.name
                out.append((name, child))
                walk(child, name + ".")
            elif isinstance(child, ast.ClassDef):
                walk(child, prefix + child.name + ".")
            else:
                walk(child, prefix)
    walk(tree, "")
    return out


def _sites():
    found = {}
    for path in sorted(LIB.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        module = ".".join(path.relative_to(LIB).with_suffix("").parts)
        functions = _functions(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and WRITES.search(node.value):
                owners = [(n, f) for n, f in functions if f.lineno <= node.lineno <= (f.end_lineno or f.lineno)]
                name, fn = max(owners, key=lambda item: item[1].lineno) if owners else ("<module>", None)
                found.setdefault((module, name), (fn, node.lineno))
    return found


def _moves_head(fn) -> bool:
    if fn is None:
        return False
    for node in ast.walk(fn):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and MOVES_HEAD.search(node.value):
            return True
        if isinstance(node, ast.Name) and node.id in HEAD_HELPERS:
            return True
        if isinstance(node, ast.Attribute) and node.attr in HEAD_HELPERS:
            return True
    return False


def test_every_writer_of_a_message_row_moves_its_head():
    sites = _sites()
    assert len(sites) >= 20, "the scan found too few writers: the pattern no longer matches the code"
    unlisted = [f"lib/{module.replace('.', '/')}.py:{line} {name}" for (module, name), (fn, line) in sites.items()
                if not _moves_head(fn) and (module, name) not in INSIDE_THE_IMPORT
                and (module, name) not in NOT_A_WRITE]
    assert not unlisted, ("a message row is written here and its function moves no conversation head; move "
                          "the head in the same transaction, or name it here with its reason: " + "; ".join(unlisted))


def test_every_function_named_here_still_writes_a_message_row():
    present = set(_sites())
    stale = sorted(key for key in {**INSIDE_THE_IMPORT, **NOT_A_WRITE} if key not in present)
    assert not stale, f"named here but no longer writing a message row: {stale}"


def test_the_named_importer_writers_are_called_only_from_the_import():
    """Named because the import's transaction moves the head for them: so they must not be called
    from anywhere else."""
    importers = {"message_import_rows", "message_writes", "message_import_writer", "message_import_common"}
    names = {name.split(".")[-1] for (_module, name) in INSIDE_THE_IMPORT}
    outside = []
    for path in sorted(LIB.rglob("*.py")):
        module = ".".join(path.relative_to(LIB).with_suffix("").parts)
        if module in importers:
            continue
        text = path.read_text(encoding="utf-8")
        outside += [f"{module} calls {name}" for name in names if re.search(rf"\b{name}\(", text)]
    assert not outside, outside


def _calls(name):
    found = []
    for path in sorted(LIB.rglob("*.py")):
        module = ".".join(path.relative_to(LIB).with_suffix("").parts)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        functions = _functions(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "attr", getattr(node.func, "id", None)) == name:
                owners = [(n, f) for n, f in functions if f.lineno <= node.lineno <= (f.end_lineno or f.lineno)]
                owner = max(owners, key=lambda item: item[1].lineno) if owners else ("<module>", None)
                found.append((module, owner[0], owner[1], node.lineno))
    return found


@pytest.mark.parametrize("writer", sorted(CALLER_HOLDS_THE_TRANSACTION))
def test_a_writer_without_its_own_transaction_has_one_caller_that_holds_one(writer):
    module, name = writer
    calls = _calls(name)
    assert [(m, n) for m, n, _fn, _line in calls] == [CALLER_HOLDS_THE_TRANSACTION[writer]], (
        f"{module}.{name} opens no transaction of its own; its callers: {[(m, n) for m, n, *_ in calls]}")
    _module, _name, fn, line = calls[0]
    statements = [(node.lineno, node.value) for node in ast.walk(fn)
                  if isinstance(node, ast.Constant) and isinstance(node.value, str)]
    assert any(at < line and value.strip().upper() == "BEGIN IMMEDIATE" for at, value in statements)
    assert any(at > line and value.strip().upper() == "COMMIT" for at, value in statements)
