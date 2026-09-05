#!/usr/bin/env python3
"""Deterministic queue experiments: fake model, real shipping scheduler."""
import json
import argparse
from pathlib import Path
import sys
import threading
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "server"))
from lib import tool_explanations as shipping
ToolExplanations = shipping.ToolExplanations


def item(identity):
    return {"id": identity, "activity": {"command": identity}}


def contention(priority=False):
    entered = threading.Event()
    release = threading.Event()
    batches = []
    def model(level, requests):
        batches.append([r["activity"]["command"] for r in requests])
        entered.set()
        if not release.wait(2):
            raise TimeoutError("lab gate")
        time.sleep(.02)  # Synthetic; not a real model latency claim.
        return {r["id"]: "Read the requested file." for r in requests}
    with patch.object(shipping, "log", lambda *_: None), ToolExplanations(translate=model, debounce=.02) as service:
        for offset in range(0, 64, 8):
            service.request(3, [item(f"history-{i}") for i in range(offset, offset + 8)])
        assert entered.wait(2)
        started = time.perf_counter()
        service.request(3, [item("focused-live")])
        if priority:
            # LAB ONLY: promote the newly enqueued focused row, leaving the
            # running batch alone. Production has no priority parameter yet.
            with service._condition:
                key = next(reversed(service._queue))
                service._queue.move_to_end(key, last=False)
        release.set()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            response = service.request(3, [item("focused-live")])["items"][0]
            if response["status"] == "ready":
                break
            time.sleep(.001)
        else:
            raise TimeoutError("scheduler did not finish")
        elapsed = (time.perf_counter() - started) * 1000
        batch_number = next(i + 1 for i, values in enumerate(batches) if "focused-live" in values)
        cached_started = time.perf_counter()
        assert service.request(3, [item("focused-live")])["items"][0]["status"] == "ready"
        cache_ms = (time.perf_counter() - cached_started) * 1000
    return {"priority_prototype": priority, "live_batch_number": batch_number,
            "synthetic_live_wait_ms": round(elapsed, 2), "cache_ms": round(cache_ms, 3),
            "note": "Fake model takes 20ms; live batch position is the deterministic result."}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = [contention(False), contention(True)]
    if args.output:
        with args.output.open("x") as output:
            json.dump(result, output, indent=2)
    print(json.dumps(result, indent=2))
