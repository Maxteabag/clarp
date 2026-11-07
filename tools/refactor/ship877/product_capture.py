"""Buildbox pytest plugin: independent OLD/NEW writer product artifacts.

Use the SAME bytes at both source pins, with -n0 and SHIP877_PRODUCTS pointing
at separate artifact roots. It never chooses a twin or rewrites a product.
The companion runner must compare every per-node JSON byte after BOTH complete;
this plugin alone makes no old/new equivalence or passing-test claim.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import time

import pytest


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_protocol(item, nextitem):
    # Pin the common clock BEFORE fixture construction (SQLite fixture fields
    # included), not by deleting/rounding volatile columns from the products.
    previous = time.time
    time.time = lambda: 1791706800.0
    try:
        yield
    finally:
        time.time = previous


def _value(value):
    if isinstance(value, bytes):
        return {"sqlite_blob_base64": base64.b64encode(value).decode("ascii")}
    return value


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    yield
    # Capture after real writer/effects and assertions, before fixture teardown.
    # A test failure is retained by pytest; an artifact cannot make it green.
    from lib import db
    root = Path(os.environ["SHIP877_PRODUCTS"])
    root.mkdir(parents=True, exist_ok=True)
    connection = db.conn()
    if connection.in_transaction:
        raise RuntimeError("product_capture_requires_completed_test_transaction")
    connection.execute("BEGIN")
    try:
        schema = [list(row) for row in connection.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_schema ORDER BY type,name")]
        tables = {}
        for row in connection.execute("SELECT name FROM sqlite_schema WHERE type='table' ORDER BY name"):
            name = row[0]
            quoted = '"' + name.replace('"', '""') + '"'
            columns = [list(value) for value in connection.execute(f"PRAGMA table_info({quoted})")]
            data = [[_value(value) for value in record]
                    for record in connection.execute(f"SELECT * FROM {quoted}")]
            data.sort(key=lambda value: json.dumps(value, ensure_ascii=True, separators=(",", ":")))
            tables[name] = {"columns": columns, "rows": data}
    finally:
        connection.execute("ROLLBACK")
    filename = hashlib.sha256(item.nodeid.encode()).hexdigest() + ".json"
    artifact = {"nodeid": item.nodeid, "schema": schema, "tables": tables,
                "excluded_tables": [], "excluded_columns": [], "normalized_values": []}
    (root / filename).write_text(json.dumps(artifact, ensure_ascii=True, sort_keys=True,
                                           separators=(",", ":")) + "\n")
