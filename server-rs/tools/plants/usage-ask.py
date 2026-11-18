#!/usr/bin/env python3
"""Refresh age, projection, explicit refresh and the fallback, each independently falsified."""
import runner

K = "clarp-kernel/src/usage_ask.rs"
P = "clarp-host/src/usage_provider.rs"
RUNS = {
    "vectors": ["test", "--test", "usage_ask_vectors"],
    "host": ["test", "--test", "host", "--", "asking_for_usage_reads_each_provider"],
}
# Do not mutate the destination, credential boundary or opt-in default into a real provider call.
PLANTS = [
    (K, "vectors", "refresh at the five-minute boundary", "> 300_000", ">= 300_000"),
    (K, "vectors", "discard freshness on the hour", "<= crate::standing::FRESH_MS", "< crate::standing::FRESH_MS"),
    (K, "vectors", "the reset instant is still fresh", "now >= end", "now > end"),
    (K, "vectors", "weekly buckets have five hours", '("seven_day", Some(10080))', '("seven_day", Some(300))'),
    (K, "vectors", "do not count remaining fractions", "(1.0 - fraction) * 100.0", "fraction * 100.0"),
    (P, "host", "skip Codex fallback", 'direct.is_some() || tool != "codex"', 'direct.is_some() || tool == "codex"'),
]


if __name__ == "__main__":
    raise SystemExit(runner.main(__file__, RUNS, PLANTS))
