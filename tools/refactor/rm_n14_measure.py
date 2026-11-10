"""Resource-bounded synthetic RM-N14 baseline/continuation measurement.

Run through heavy-remote. Default is one 1 MiB Claude case, ten 1 KiB warm
appends and two isolated persistent lanes. Repeat explicitly for 10/17 MiB,
10 KiB deltas and --long-turn. This does not enable an incremental importer.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import datetime as dt
import json
import math
import pathlib
import platform
import sqlite3
import statistics
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "server")]
from tests.readmodel import corpus, world
from tests.readmodel import rm_n14_harness as harness


def seed_inputs(path, backend, turn_rows, client_rows):
    """Identical historical raw inputs; no provider, runtime or Host starts."""
    from lib import db
    world.prepare(path, backend)
    connection = db.conn()
    for i in range(turn_rows):
        began = harness.BASE_MS - (turn_rows - i) * 1000
        connection.execute(
            "INSERT INTO turns(agent_id,source,trace_id,started_at,ended_at,settled_at,outcome) "
            "VALUES (?,'pwa',?,?,?,?, 'completed')",
            (corpus.AGENT, f"seed-{i}", began, began + 500, began + 500))
    for i in range(client_rows):
        at = harness.BASE_MS - (client_rows - i) * 1000
        stamp = dt.datetime.fromtimestamp(at / 1000, dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        connection.execute(
            "INSERT INTO messages(message_id,agent_id,backend_session_id,source_file,seq,role,timestamp,text,updated_at,revision) "
            "VALUES (?,?,?,?,?,'user',?,?,?,?)",
            (f"u-seed-{i}", corpus.AGENT, corpus.SESSION, f"client:seed-{i}", -i - 1,
             stamp, f"synthetic historical input {i}", at, i + 1))
    connection.execute("UPDATE message_clock SET revision=?", (client_rows,))
    connection.execute("INSERT INTO conversation_heads VALUES (?,?,?,0)",
                       (corpus.AGENT, corpus.SESSION, client_rows))
    connection.commit()
    db.close_local()


def percentile95(values):
    return sorted(values)[max(0, math.ceil(len(values) * 0.95) - 1)]


def measure(args):
    started = time.monotonic()
    deadline = started + args.wall_budget
    with tempfile.TemporaryDirectory(prefix="rm-n14-measure-") as directory:
        root = pathlib.Path(directory)
        reference, candidate, transcript = root / "reference.sqlite", root / "candidate.sqlite", root / "synthetic.jsonl"
        next_ordinal = harness.write_history(transcript, args.backend, mebibytes=args.mebibytes,
                                             long_turn=args.long_turn)
        seed_inputs(reference, args.backend, args.turn_rows, args.client_rows)
        with sqlite3.connect(reference) as original, sqlite3.connect(candidate) as clone:
            original.backup(clone)
        observations = []
        with ExitStack() as stack:
            full = stack.enter_context(harness.PersistentLane(reference, transcript, args.backend, "reference"))
            actual = stack.enter_context(harness.PersistentLane(candidate, transcript, args.backend, "candidate"))
            for tick in range(args.ticks + 1):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("measurement wall budget exhausted")
                full.timeout = actual.timeout = min(120, remaining)
                if tick:
                    with transcript.open("ab") as output:
                        output.write(harness.append_bytes(args.backend, next_ordinal + tick - 1,
                                                          size=args.append_bytes, long_turn=args.long_turn))
                a, b = full.tick(tick), actual.tick(tick)
                comparison_at = time.perf_counter()
                harness.assert_product_equal(reference, candidate)
                comparison_s = time.perf_counter() - comparison_at
                observations.append({"tick": tick, "bootstrap": tick == 0,
                                     "product_equal": True, "oracle_s": comparison_s,
                                     "reference": a, "candidate": b})
        warm = [r["candidate"]["total_s"] for r in observations if not r["bootstrap"]]
        return {"status": "tail_apply_measured" if all(
                    r["candidate"].get("apply_mode") == "tail" for r in observations[1:]) else "full_or_mixed",
                "candidate_fast_path_proven": all(
                    r["candidate"].get("apply_mode") == "tail" for r in observations[1:]),
                "backend": args.backend, "history_mib": args.mebibytes,
                "long_turn": args.long_turn, "append_bytes": args.append_bytes,
                "warm_ticks": args.ticks, "turn_input_rows": args.turn_rows,
                "client_input_rows": args.client_rows,
                "median_candidate_s": statistics.median(warm), "p95_candidate_s": percentile95(warm),
                "p95_method": "nearest_rank", "wall_s": time.monotonic() - started,
                "host": {"os": platform.system(), "machine": platform.machine(), "python": platform.python_version()},
                "limits": ["shared buildbox", "filesystem cache warm from generation",
                           "profiling overhead included", "product oracle excluded from import timings",
                           "store carry and external effects require separate controls",
                           "P1 effect-bearing prefixes use full replay", "P2 long-turn frontier not implemented"],
                "observations": observations}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", choices=harness.BACKENDS, default="claude")
    parser.add_argument("--mebibytes", choices=(1, 10, 17), type=int, default=1)
    parser.add_argument("--append-bytes", choices=(1024, 10240), type=int, default=1024)
    parser.add_argument("--ticks", type=int, default=10)
    parser.add_argument("--turn-rows", type=int, default=1000)
    parser.add_argument("--client-rows", type=int, default=200)
    parser.add_argument("--wall-budget", type=float, default=600)
    parser.add_argument("--long-turn", action="store_true")
    parser.add_argument("--output", type=pathlib.Path, required=True)
    args = parser.parse_args()
    if args.ticks < 1 or args.wall_budget <= 0 or min(args.turn_rows, args.client_rows) < 0:
        parser.error("positive ticks/budget and nonnegative input counts required")
    result = measure(args)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({k: v for k, v in result.items() if k != "observations"}, sort_keys=True))
