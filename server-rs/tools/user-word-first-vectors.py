#!/usr/bin/env python3
"""Record PR 506's queue position and user-word classifier in an isolated home."""
import importlib.util
from itertools import product
import json
from pathlib import Path
import sys

import scratch

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "server-rs/crates/clarp-kernel/tests/fixtures/user-word-first-vectors.json"


@scratch.case("clarp-user-word-first-vectors-")
def reference(root):
    spec = importlib.util.spec_from_file_location("replay_host", ROOT / "tests/replay/host.py")
    sandbox = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sandbox)
    (root / "scenario-config.toml").write_text("")
    sandbox.isolate(root)
    sandbox.loopback_only()
    sys.path.insert(0, str(ROOT / "server"))
    from lib import origins, turn_queue

    classify = []
    for origin, actor, choice in product(
        (None, "", " user ", "oracle", "agent", "automation", "schedule", "heartbeat", "janitor"),
        ("", "user", "agent", "agent:desk", "watcher"),
        ("accepted", "rejected", "answered", "expired", "dismissed", "withdrawn"),
    ):
        word = origins.is_user_word(origin, card_answered_by=actor, card_choice=choice)
        classify.append([origin, actor, choice, word])

    slots = []
    states = ((False, False), (True, False), (False, True), (True, True))
    for length in range(4):
        for items in product(states, repeat=length):
            slot = turn_queue.user_word_slot(items, is_user=lambda item: item[0],
                                             started=lambda item: item[1])
            slots.append([items, slot])
    return {"classification": classify, "slots": slots}


if __name__ == "__main__":
    OUT.write_text(json.dumps(reference(), ensure_ascii=True, indent=2) + "\n")
