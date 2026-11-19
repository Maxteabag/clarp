#!/usr/bin/env python3
"""The reference's complete batch request, captured at its HTTP boundary."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
KERNEL = ROOT / "server-rs/crates/clarp-kernel"


def capture():
    from lib import judgments, judgment_sites
    scenarios = []
    for size in (0, 1, 2, 12):
        entries = {f"a{i+1}": {"label": f"Example {i}"} for i in range(size)}
        requests = []
        def post(body, key, seconds):
            requests.append({"body": body, "seconds": seconds})
            return {"answers": {k: {"choice":"accurate","probabilities":{"accurate":1}} for k in body["questions"]}}
        with patch.object(judgments,"site_enabled",return_value=True), patch.object(judgments,"api_key",return_value="example"), patch.object(judgments,"_post",post), patch.object(judgments,"_log_decision"), patch.object(judgments,"record_outcome"):
            judgment_sites.audit_work_labels(entries)
        scenarios.append({"entries":list(entries.values()),"requests":requests})
    return scenarios


if __name__=="__main__":
    spec=importlib.util.spec_from_file_location("replay",ROOT/"tests/replay/host.py")
    replay=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(replay)
    with tempfile.TemporaryDirectory() as directory:
        root=Path(directory)
        (root/"scenario-config.toml").write_text("")
        replay.isolate(root)
        replay.loopback_only()
        sys.path.insert(0,str(ROOT/"server"))
        cases=capture()
        (KERNEL/"tests/fixtures/label-site.json").write_text(json.dumps(cases,indent=1)+"\n")
        sample=cases[1]["requests"][0]["body"]["questions"]["l_a1"]
        (KERNEL/"src/janitors/label-question.json").write_text(json.dumps(sample,indent=1)+"\n")
