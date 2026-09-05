"""Corpus paging must neither skip boundaries nor chase concurrent writes."""
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "server"))
from lib.viz_corpus import tool_rows
from lib.viz_normalize import unmatched_clusters


def test_paged_scan_is_fenced_and_filters_without_skipping():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute("CREATE TABLE state_log (state_id INTEGER PRIMARY KEY, "
                "agent_id TEXT, ts INTEGER, kind TEXT, detail TEXT)")
    detail = json.dumps({"tool": "Bash", "input": {"command": "frobnicate x"}})
    con.executemany("INSERT INTO state_log VALUES (?, 'a', ?, ?, ?)",
                    [(i, i, "tool" if i % 2 else "text", detail) for i in range(1, 10)])
    rows = tool_rows(con, since=3, until=20, page_size=2)
    first = next(rows)
    con.execute("INSERT INTO state_log VALUES (10, 'a', 10, 'tool', ?)", (detail,))
    assert [r["state_id"] for r in [first, *rows]] == [3, 5, 7, 9]
    clusters = unmatched_clusters(tool_rows(con, page_size=1))
    assert clusters[0]["count"] == 6


def test_queries_never_fetch_more_than_a_page():
    class Cursor:
        def fetchone(self):
            return [500]

        def fetchall(self):
            return []

        def close(self):
            pass

    class Connection:
        def execute(self, sql, params=()):
            if "detail" in sql:
                assert "LIMIT ?" in sql and "OFFSET" not in sql
                assert params[-1] == 7
            return Cursor()

    assert list(tool_rows(Connection(), page_size=7)) == []
