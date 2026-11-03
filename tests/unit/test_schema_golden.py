"""Schema golden: the exact shape of state.sqlite, fresh and upgraded.

Dumps sqlite_master (tables, indexes, views, triggers; sorted, comments and
whitespace normalized, one column per line) of a fresh database and of a v61
database run through every migration, and compares both with the files in
tests/fixtures/schema/. Any schema change shows up here as a reviewable diff;
the two goldens differ only where an upgraded file legitimately keeps another
shape (ALTER TABLE appends columns at the end).

A schema change is a contract change: bump _SCHEMA_VERSION, add the migration
and regenerate with

    UPDATE_SCHEMA_GOLDENS=1 uv run --frozen --group dev python -m pytest \
        -p no:xdist tests/unit/test_schema_golden.py
"""
from __future__ import annotations

import os
import pathlib
import re
import sqlite3

import pytest

from lib import db
from test_db_migrations import _shape_as_v61

GOLDENS = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "schema"


def _segments(sql: str) -> list[tuple[bool, str]]:
    """Split into (quoted, text) runs; comments outside quotes become a space."""
    out: list[tuple[bool, str]] = []
    i, plain = 0, []
    while i < len(sql):
        ch = sql[i]
        if ch in "'\"`[":
            close = "]" if ch == "[" else ch
            end = sql.find(close, i + 1)
            while close != "]" and end >= 0 and sql.startswith(close * 2, end):
                end = sql.find(close, end + 2)  # doubled quote escapes itself
            end = len(sql) - 1 if end < 0 else end
            out += [(False, "".join(plain)), (True, sql[i:end + 1])]
            plain, i = [], end + 1
            continue
        if sql.startswith("--", i) or sql.startswith("/*", i):
            end = sql.find("\n", i) if ch == "-" else sql.find("*/", i) + 1
            plain.append(" ")
            i = len(sql) if end <= 0 else end + 1
            continue
        plain.append(ch)
        i += 1
    out.append((False, "".join(plain)))
    return out


def _tidy(text: str) -> str:
    text = re.sub(r"\s+", " ", text)
    return re.sub(r"\( ", "(", re.sub(r" \)", ")", re.sub(r" ?, ?", ", ", text)))


def normalize(sql: str | None) -> str:
    if sql is None:
        return "(implicit)"
    segments = [(q, t if q else _tidy(t)) for q, t in _segments(sql)]
    text = "".join(t for _, t in segments).strip()
    if not re.match(r"CREATE TABLE", text, re.I):
        return text
    # One column or constraint per line: split at commas outside quotes and parens.
    parts, depth, current, opened = [], 0, [], False
    head, tail = "", ""
    for quoted, chunk in segments:
        if quoted:
            current.append(chunk)
            continue
        for ch in chunk:
            if ch == "(":
                depth += 1
                if depth == 1 and not opened:
                    head, current, opened = "".join(current), [], True
                    continue
            elif ch == ")":
                depth -= 1
                if depth == 0 and opened and not tail:
                    parts.append("".join(current))
                    current, tail = [], ")"
                    continue
            if ch == "," and depth == 1:
                parts.append("".join(current))
                current = []
                continue
            current.append(ch)
    tail += "".join(current)
    lines = ",\n".join("    " + p.strip() for p in parts)
    return f"{head.strip()} (\n{lines}\n{tail.rstrip()}"


def dump(con: sqlite3.Connection) -> str:
    version = con.execute("PRAGMA user_version").fetchone()[0]
    rows = con.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name").fetchall()
    blocks = [f"-- user_version {version}"]
    for kind, name, table, sql in rows:
        blocks.append(f"-- {kind} {name} on {table}\n{normalize(sql)};")
    return "\n\n".join(blocks) + "\n"


def _fresh(path: pathlib.Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(path))
    db._migrate(con)
    return con


def _upgraded_from_v61(path: pathlib.Path) -> sqlite3.Connection:
    con = _fresh(path)
    _shape_as_v61(con)
    con.close()
    con = sqlite3.connect(str(path))
    db._migrate(con)
    return con


@pytest.mark.parametrize("name,build", [("fresh", _fresh), ("v61_upgraded", _upgraded_from_v61)])
def test_schema_matches_its_golden(tmp_path, name, build):
    con = build(tmp_path / f"{name}.sqlite")
    actual = dump(con)
    con.close()
    golden = GOLDENS / f"{name}.sql"
    if os.environ.get("UPDATE_SCHEMA_GOLDENS") == "1":
        golden.parent.mkdir(parents=True, exist_ok=True)
        golden.write_text(actual)
    assert golden.exists(), f"missing {golden}; regenerate with UPDATE_SCHEMA_GOLDENS=1"
    assert actual == golden.read_text(), (
        f"state.sqlite shape changed ({name}). A schema change needs a version bump, a "
        f"migration and the contract owner's review; regenerate with UPDATE_SCHEMA_GOLDENS=1 "
        f"and review the diff of {golden.relative_to(GOLDENS.parents[1])}.")


@pytest.mark.parametrize("sql,expected", [
    ("CREATE INDEX i ON t(a ,  b )", "CREATE INDEX i ON t(a, b)"),
    ("CREATE INDEX i ON t(a,b)", "CREATE INDEX i ON t(a, b)"),
    ("CREATE TABLE t (\n  a TEXT, -- note, with comma\n  b INT CHECK (b IN (1, 2))\n)",
     "CREATE TABLE t (\n    a TEXT,\n    b INT CHECK (b IN (1, 2))\n)"),
    ("CREATE TABLE t (a TEXT DEFAULT '--x,(', /* c */ b INT)",
     "CREATE TABLE t (\n    a TEXT DEFAULT '--x,(',\n    b INT\n)"),
    ("CREATE TABLE t (a TEXT DEFAULT 'it''s  two') WITHOUT ROWID",
     "CREATE TABLE t (\n    a TEXT DEFAULT 'it''s  two'\n) WITHOUT ROWID"),
    (None, "(implicit)"),
])
def test_normalize(sql, expected):
    assert normalize(sql) == expected
