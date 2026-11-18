"""The rule that decides whether a routing CLI's failure keeps its own words (issue 360, R85);
its behaviour in every sink is pinned in tests/unit/test_routing_failure_text.py."""
from __future__ import annotations

import time

from lib import model_fallbacks


def test_a_prompt_shorter_than_the_run_is_compared_whole():
    assert model_fallbacks.echoes("error near: say hello now", "say hello")
    assert not model_fallbacks.echoes("error near: say goodbye", "say hello")


def test_the_comparison_costs_little_on_a_100_kb_prompt():
    prompt = ("word " * 20_000)[:100_000]
    tail = "".join(chr(65 + n % 26) for n in range(1000))
    started = time.perf_counter()
    for _ in range(10):
        model_fallbacks.cli_failure("claude", tail, prompt=prompt, returncode=3)
    took = (time.perf_counter() - started) / 10
    print(f"R85 COST {took * 1000:.2f} ms per failure (100 KB prompt, 1,000-character tail)")
    assert took < 0.5
