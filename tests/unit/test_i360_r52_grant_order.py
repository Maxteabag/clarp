"""Issue 360, R52: when one sentence lets several held agents speak, their
clips play in the order the agents asked to speak (their heralds fired), the
same on every Host start, not in a set's hash order (which changes with each
process's hash seed)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

UNIT = Path(__file__).resolve().parent
SERVER = UNIT.parents[1] / "server"

SCENARIO = """
import json, sys
from test_herald import make_manager
out = []
for arrival in [("rachel", "bella"), ("bella", "rachel")]:
    hm, stream, _ = make_manager()
    hm.set_focus("claude")
    for ts, sid in enumerate(arrival, start=10):
        hm.ingest_clip(sid, url=f"/audio/{sid}.mp3", ts=ts)
    res = hm.on_user_text("Rachel, Bella, go ahead")
    out.append([list(arrival), list(res.granted), stream.audio_urls[-2:]])
print(json.dumps(out))
"""


def test_one_grant_for_several_plays_them_in_the_order_they_asked_whatever_the_hash_seed(tmp_path):
    for seed in range(6):                   # one process per Host start
        env = {**os.environ, "PYTHONHASHSEED": str(seed),
               "PYTHONPATH": os.pathsep.join([str(SERVER), str(UNIT)]),
               "CLAUDE_PWA_DB": str(tmp_path / "unused.sqlite")}
        done = subprocess.run([sys.executable, "-c", SCENARIO], env=env, cwd=tmp_path,
                              capture_output=True, text=True, timeout=60, check=True)
        for arrival, granted, played in json.loads(done.stdout.strip().splitlines()[-1]):
            assert granted == arrival, (seed, granted)
            assert played == [f"/audio/{sid}.mp3" for sid in arrival], (seed, played)
