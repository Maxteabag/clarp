#!/usr/bin/env python3
"""Summarize raw lab samples; automated flags are not semantic quality scores."""
import argparse
from collections import defaultdict
import json
from pathlib import Path
import re
import statistics


def analyze(directory):
    groups = defaultdict(list)
    rows = []
    for path in sorted(directory.glob("*.jsonl")):
        for line in path.read_text().splitlines():
            row = json.loads(line)
            rows.append(row)
            groups[(row["transport"], row["prompt"], row["batch_size"])].append(row)
    result = {"recorded_model_calls": len(rows), "format_valid": sum(bool(r.get("valid")) for r in rows), "conditions": []}
    for (transport, prompt, size), samples in groups.items():
        times = [r.get("elapsed_including_startup_ms", r["total_ms"] + (r.get("startup_ms", 0) if transport == "app-server" and r["repetition"] == 0 else 0)) for r in samples]
        answer_sets = [r.get("answers", {}) for r in samples]
        texts = [t for answers in answer_sets for t in answers.values() if isinstance(t, str)]
        unknown = [text for answers in answer_sets for key, text in answers.items() if key.startswith("unknown") and isinstance(text, str)]
        acknowledgments = sum(bool(re.search(r"unclear|unknown|not known|not clear|not evident|not specified|not shown|not .*evident", text, re.I)) for text in unknown)
        leaks = sum(bool(re.search(r"\b(?:javascript|python|typescript|json|csv|api|endpoint)\b|\b[\w-]+\.(?:js|py|sh|ts)\b", text, re.I)) for text in texts)
        startup = [r["thread_started_ms"] for r in samples if "thread_started_ms" in r]
        tails = [r["total_ms"] - event["ms"] for r in samples for event in r.get("events", []) if event["type"] == "turn.completed"]
        result["conditions"].append({"transport": transport, "prompt": prompt, "batch_size": size,
            "n": len(samples), "median_ms": round(statistics.median(times), 2), "range_ms": [min(times), max(times)],
            "session_ready_median_ms": round(statistics.median(startup), 2) if startup else None,
            "post_turn_tail_median_ms": round(statistics.median(tails), 2) if tails else None,
            "explicit_unknown": [acknowledgments, len(unknown)], "style_flags": [leaks, len(texts)],
            "median_words": statistics.median(len(t.split()) for t in texts) if texts else None})
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = analyze(args.directory)
    if args.output:
        with args.output.open("x") as output:
            json.dump(result, output, indent=2)
    print(json.dumps(result, indent=2))
