#!/usr/bin/env python3
"""Read-only/snapshot benchmarks for Clarp's real SQLite workload.

The source database is never mutated. A consistent SQLite backup is created in
a temporary directory, and write/retention experiments run only on that copy.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sqlite3
import tempfile
import time
import os
import sys


def synthetic_snapshot():
    """No live state: benchmark real query paths against an owned temporary DB."""
    with tempfile.TemporaryDirectory(prefix='clarp-snapshot-benchmark-') as directory:
        root = pathlib.Path(directory)
        for name, suffix in {
            'CLAUDE_PWA_DB': 'state.sqlite', 'CLARP_TELEMETRY_DB': 'telemetry.sqlite',
            'CLAUDE_PWA_CONFIG': 'absent.toml', 'CLAUDE_PWA_LOG_DIR': 'logs',
            'CLARP_CONFIG_DIR': 'config', 'CLARP_DATA_DIR': 'data',
            'CLARP_SHARE_DIR': 'data', 'CLARP_CACHE_DIR': 'cache',
        }.items():
            os.environ[name] = str(root / suffix)
        sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'server'))
        from lib import agents, db
        from lib.snapshot import build_agent_snapshot
        result = []
        previous = 0
        for count in (1, 10, 50, 100):
            for i in range(previous, count):
                agents.create_agent(persona=f'Bench{i}', session=f'bench-{i}', voice_id='', cwd=str(root))
            build_agent_snapshot(None)
            statements = []
            db.conn().set_trace_callback(statements.append)
            build_agent_snapshot(None)
            db.conn().set_trace_callback(None)
            result.append({'agents': count, 'sqlite_statements': len(statements),
                           **timed(lambda: build_agent_snapshot(None), iterations=20)})
            previous = count
        db.close_local()
        print(json.dumps({'workload': 'synthetic idle agents, no transcripts', 'snapshots': result}, indent=2))


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * fraction))]


def timed(call, iterations: int = 200) -> dict[str, float]:
    values = []
    for _ in range(iterations):
        started = time.perf_counter_ns()
        call()
        values.append((time.perf_counter_ns() - started) / 1_000_000)
    return {"p50_ms": percentile(values, .50), "p95_ms": percentile(values, .95),
            "max_ms": max(values)}


def connect(path: pathlib.Path) -> sqlite3.Connection:
    con = sqlite3.connect(path, timeout=10, isolation_level=None,
                          check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA busy_timeout=10000")
    return con


def main() -> int:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--database", type=pathlib.Path)
    mode.add_argument("--synthetic-snapshot", action='store_true')
    parser.add_argument("--session", default="")
    args = parser.parse_args()
    if args.synthetic_snapshot:
        synthetic_snapshot()
        return 0
    with tempfile.TemporaryDirectory(prefix="clarp-benchmark-") as directory:
        snapshot = pathlib.Path(directory) / "snapshot.sqlite"
        with sqlite3.connect(f"file:{args.database}?mode=ro", uri=True) as source, \
                sqlite3.connect(snapshot) as target:
            source.backup(target)
        con = connect(snapshot)
        agent = con.execute(
            "SELECT agent_id,session FROM agents WHERE deleted_at IS NULL "
            "AND (?='' OR session=?) ORDER BY created_at LIMIT 1",
            (args.session, args.session)).fetchone()
        if agent is None:
            raise SystemExit("no matching agent")
        runtime = con.execute(
            "SELECT backend_session_id FROM runtimes WHERE agent_id=? "
            "AND backend_session_id!='' ORDER BY runtime_id DESC LIMIT 1",
            (agent["agent_id"],)).fetchone()
        backend_session = runtime[0] if runtime else ""
        query = ("SELECT message_id,role,timestamp,text,tools_json,"
                 "display_cells_json,revision FROM messages WHERE agent_id=? "
                 "AND backend_session_id=? ORDER BY COALESCE(timestamp,'') DESC,"
                 "seq DESC LIMIT 100")
        for _ in range(20):
            con.execute(query, (agent["agent_id"], backend_session)).fetchall()
        result = {
            "source_bytes": args.database.stat().st_size,
            "rows": {name: con.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
                     for name in ("agents", "messages", "sse_events")},
            "chat_tail_100": timed(lambda: con.execute(
                query, (agent["agent_id"], backend_session)).fetchall()),
        }
        print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
