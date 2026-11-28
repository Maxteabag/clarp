#!/usr/bin/env python3
"""Record each turn and wait for its release when the prompt ends in hold or hold-TAG."""
import json
from pathlib import Path
import re
import sys
import time

root = Path(__file__).resolve().parent
if "-p" not in sys.argv:
    sys.exit(0)
prompt = sys.argv[sys.argv.index("-p") + 1]
prompts = root / "prompts"
with prompts.open("a") as out:
    print(json.dumps(prompt), file=out)
held = re.search(r"hold(?:-(\w+))?$", prompt)
tag = held.group(1) if held else None
if held:
    suffix = "-" + tag if tag else ""
    (root / ("holding" + suffix)).touch()
    deadline = time.monotonic() + 60
    while not (root / ("release" + suffix)).exists():
        if time.monotonic() >= deadline:
            sys.exit("stand-in release expired")
        time.sleep(0.02)
if (root / "spoken").exists():
    answer = f"<speak>Answer {tag or 'none'}.</speak>"
else:
    answer = f"grok answer {len(prompts.read_text().splitlines())}"
print(json.dumps({"type": "text", "data": answer}))
print(json.dumps({"type": "end"}))
