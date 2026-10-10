"""GET /artifacts picks a page without reading every artifact's payload.

The list used to ORDER BY updated_at over `SELECT a.*`. With no index on that
column SQLite sorted every live artifact, payload included: 77.8 MB read per
poll on a copy of the live database (2026-10-10), 10-49 s on the swapping
host for a 50-row page. The page is now chosen on keys and only its rows'
payloads are read.
"""
from __future__ import annotations

import sqlite3

import pytest

from lib import agents, artifacts, db

_OLD_LIST = """SELECT a.*,g.persona AS agent_name FROM artifacts a JOIN agents g
    ON g.agent_id=a.agent_id WHERE {where}
   ORDER BY a.{order} DESC,a.artifact_id DESC LIMIT ? OFFSET ?"""


def _seed(tmp_path) -> dict[str, str]:
    ids = {session: agents.create_agent(persona=session.title(), voice_id="V", cwd=str(tmp_path),
                                        session=session) for session in ("mike", "nora")}
    con = db.conn()
    for index in range(40):
        session = "mike" if index % 3 else "nora"
        kind = ("document", "html_form", "data", "image", "event")[index % 5]
        # Shared timestamps make the artifact_id tie-break decide the order.
        stamp = 1_000 + index // 4
        con.execute(
            """INSERT INTO artifacts(artifact_id,agent_id,session,type,title,summary,status,
                   reference_id,payload_json,created_at,updated_at,deleted_at)
               VALUES (?,?,?,?,?,?,'ready','',?,?,?,?)""",
            (f"art-{index:02d}", ids[session], session, kind, f"Report {index}",
             "weekly" if index % 2 else "", '{"content":"' + "x" * 2000 + '"}',
             2_000 - index, stamp, 5 if index % 11 == 0 else None))
    return ids


def _old(where: list[str], params: list, order: str, limit: int, offset: int) -> list[str]:
    rows = db.conn().execute(_OLD_LIST.format(where=" AND ".join(where), order=order),
                             (*params, limit, offset)).fetchall()
    return [row["artifact_id"] for row in rows]


BASE = ["a.deleted_at IS NULL", "a.type NOT IN ('image','image_gallery','live_task','event')"]


@pytest.mark.parametrize("order,column", [("updated", "updated_at"), ("created", "created_at")])
@pytest.mark.parametrize("limit,offset", [(50, 0), (5, 0), (5, 5), (7, 20), (3, 100)])
def test_list_returns_the_same_page_in_the_same_order(tmp_path, order, column, limit, offset):
    _seed(tmp_path)
    got = artifacts.list_artifacts(limit=limit, offset=offset, order=order)
    assert [row["artifact_id"] for row in got] == _old(BASE, [], column, limit, offset)
    for row in got:
        assert row["payload"] == {"content": "x" * 2000}
        assert row["agent_name"] in {"Mike", "Nora"}


def test_filters_select_the_same_rows(tmp_path):
    ids = _seed(tmp_path)
    cases = [
        ({"session": "mike"}, ["a.session=?"], ["mike"]),
        ({"agent_id": ids["nora"]}, ["a.agent_id=?"], [ids["nora"]]),
        ({"type": "html_form"}, ["a.type=?"], ["html_form"]),
        ({"search": "weekly"},
         ["(LOWER(a.title) LIKE ? ESCAPE '\\' OR LOWER(a.summary) LIKE ? ESCAPE '\\')"],
         ["%weekly%", "%weekly%"]),
        ({"created_from": 1_970, "created_to": 1_990}, ["a.created_at>=?", "a.created_at<?"],
         [1_970, 1_990]),
    ]
    for kwargs, where, params in cases:
        got = [row["artifact_id"] for row in artifacts.list_artifacts(limit=8, **kwargs)]
        assert got == _old(BASE + where, params, "updated_at", 8, 0), kwargs


def test_choosing_the_page_never_reads_payloads(tmp_path):
    _seed(tmp_path)
    con = db.conn()
    pending: list[str] = []
    payload_statements: list[str] = []

    def authorize(action, table, column, *_):
        if action == sqlite3.SQLITE_READ and table == "artifacts" and column == "payload_json":
            pending.append(column)
        return sqlite3.SQLITE_OK

    def trace(sql):
        # The authorizer runs while a statement is prepared, just before it runs.
        if pending:
            payload_statements.append(" ".join(sql.split()).upper())
            pending.clear()

    con.set_authorizer(authorize)
    con.set_trace_callback(trace)
    try:
        rows = artifacts.list_artifacts(limit=5, order="updated")
    finally:
        con.set_authorizer(None)
        con.set_trace_callback(None)
    assert len(rows) == 5
    assert payload_statements, "the page's payloads were read somewhere"
    for sql in payload_statements:
        assert "LIMIT" not in sql and "ORDER BY" not in sql, \
            f"a payload-reading statement sorts or pages over artifacts: {sql}"
