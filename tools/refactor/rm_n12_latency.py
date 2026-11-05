"""Measure bounded snapshot waits on synthetic, already imported histories.

Run on heavy-remote, never against a live database or transcript. Reports
counts/timings only; the generated inputs and database disappear on exit.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import statistics
import sys
import tempfile
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "server"))

from lib import db, telemetry, eventlog
from lib import transcript_import_cache as cache, transcript_incremental as inc
from lib.conversation import ACTIVE_SNAPSHOT_IMPORT_TIMEOUT_SECONDS
from tests.readmodel import corpus, world


def measure(backend: str, requests: int, samples: int) -> dict:
    with tempfile.TemporaryDirectory(prefix="rm-n12-latency-") as directory:
        root = pathlib.Path(directory)
        assert cache.reset_for_tests()
        world.prepare(root / "synthetic.sqlite", backend)
        telemetry.reset_for_tests(root / "synthetic-telemetry.sqlite")
        eventlog.reset_for_tests(root / "logs")
        os.environ["CLARP_TRANSCRIPT_SHADOW"] = "0"
        # Measure import cost rather than the normal five-second refresh window.
        cache._BACKGROUND_MIN_INTERVAL = 0
        corpus.REQUESTS = requests + samples
        source = getattr(corpus, backend)()
        path = root / f"{backend}.jsonl"
        timings = []

        def write(records, mode):
            with path.open(mode) as output:
                for record in records:
                    output.write(json.dumps(record) + "\n")

        write(source.header, "w")
        for group in source.groups[:requests]:
            write(group, "a")

        def importer():
            timings.append(inc.import_transcript(
                backend=backend, path=path, agent_id=corpus.AGENT,
                backend_session_id=corpus.SESSION))

        started = time.perf_counter()
        cache.import_if_changed(path, importer, owner=corpus.AGENT)
        cold_s = time.perf_counter() - started
        observations = []
        for group in source.groups[requests:]:
            write(group, "a")
            started = time.perf_counter()
            fresh = cache.import_for_snapshot(
                path, importer, owner=corpus.AGENT,
                timeout=ACTIVE_SNAPSHOT_IMPORT_TIMEOUT_SECONDS)
            wait_s = time.perf_counter() - started
            if not cache.wait_for_background(180):
                raise TimeoutError("synthetic benchmark importer did not finish")
            complete_s = time.perf_counter() - started
            observations.append({
                "fresh_within_bound": fresh, "wait_s": wait_s, "complete_s": complete_s,
                "source_bytes": path.stat().st_size, **timings[-1],
            })
        result = {
            "backend": backend, "history_requests": requests, "samples": observations,
            "bound_s": ACTIVE_SNAPSHOT_IMPORT_TIMEOUT_SECONDS,
            "cold_import_s": cold_s,
            "median_complete_s": statistics.median(r["complete_s"] for r in observations),
            "max_complete_s": max(r["complete_s"] for r in observations),
            "stale_returns": sum(not r["fresh_within_bound"] for r in observations),
        }
        assert cache.reset_for_tests()
        db.close_local()
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requests", nargs="+", type=int, default=[100, 1000, 10000])
    parser.add_argument("--backends", nargs="+", choices=["claude", "codex"], default=["claude", "codex"])
    parser.add_argument("--samples", type=int, default=3)
    parser.add_argument("--output", type=pathlib.Path)
    args = parser.parse_args()
    results = []
    for backend in args.backends:
        for count in args.requests:
            result = measure(backend, count, args.samples)
            results.append(result)
            print(json.dumps(result, sort_keys=True), flush=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(results, indent=2) + "\n")


if __name__ == "__main__":
    main()
