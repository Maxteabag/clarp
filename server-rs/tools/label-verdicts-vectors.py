#!/usr/bin/env python3
"""Record the label checker's decisions and documents in an isolated reference Host."""
import importlib.util
import itertools
import json
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scratch as tool_scratch

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "server-rs/crates/clarp-kernel/tests/fixtures/label-verdicts.json"


def capture():
    from lib import judgments, judgment_sites, label_audit

    cases = []
    def one(answers, ages):
        evidence = [dict(agent_id=f"id-{12-i}", name=f"Subject {i}", idle_min=age,
                         packet={"label":f"Step {i} " + chr(0x1f680) + ' "x"'}) for i, age in enumerate(ages)]
        entries = label_audit.packets(evidence)
        with patch.object(judgments, "judge", return_value=judgments.Answers(answers, {})), patch.object(judgments, "record_outcome"):
            verdicts = judgment_sites.audit_work_labels(entries)
        found = label_audit.mismatches(evidence, verdicts or {})
        prior = ""
        for attempt in range(2):
            with patch.object(label_audit.artifacts, "create", side_effect=lambda **row: row):
                doc, mark = label_audit.report({"session":"checker"}, "test-run", found, checked=len(entries), last_fingerprint=prior)
            cases.append(dict(answers=answers,evidence=evidence,found=found,previous=prior,document=doc,fingerprint=mark))
            prior = mark

    for choice, accurate, confidence in itertools.product(
        (*judgment_sites.LABEL_VERDICTS, "unknown", ""), (0.29999,0.30,0.30001), (0.49999,0.50,0.50001)):
        one({"l_a1":{"choice":choice,"probabilities":{choice:confidence,"accurate":accurate}}},[89])
    for probs in ({}, {"finished":1}, {"accurate":0.2},{"finished":"0.5","accurate":"0.1235"}):
        one({"l_a1":{"choice":"finished","probabilities":probs}},[None])
    ages=[0,89,90,91,150,210]
    one({f"l_a{i+1}":{"choice":"stalled","probabilities":{"stalled":1}} for i in range(len(ages))},ages)
    one({},[])
    return cases


if __name__ == "__main__":
    spec = importlib.util.spec_from_file_location("replay", ROOT/"tests/replay/host.py")
    replay = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(replay)
    with tool_scratch.directory() as directory:
        root = Path(directory)
        (root/"scenario-config.toml").write_text("")
        replay.isolate(root)
        replay.loopback_only()
        sys.path.insert(0,str(ROOT/"server"))
        OUT.write_text(json.dumps(capture(),ensure_ascii=True,separators=(",",":"))+"\n", encoding="ascii")
