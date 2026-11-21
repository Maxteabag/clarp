#!/usr/bin/env python3
"""Record leader decision memory and compact values from the isolated Python Host."""
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import scratch

REPO = pathlib.Path(__file__).resolve().parents[2]
OUT = REPO / "server-rs/crates/clarp-kernel/tests/fixtures/leader-memory-vectors.json"
AT = 1_795_000_000_000


@scratch.case("clarp-leader-memory-")
def run(root):
    spec = importlib.util.spec_from_file_location("replay_host", REPO / "tests/replay/host.py")
    replay = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(replay)
    (root / "scenario-config.toml").write_text("")
    replay.isolate(root)
    replay.loopback_only()
    sys.path.insert(0, str(REPO / "server"))
    from lib import db, leader_memory as memory

    db.reset_for_tests(root / "leader.sqlite")
    config = root / "config"
    config.mkdir(exist_ok=True)
    file = config / "user-values.md"
    os.environ["CLARP_USER_VALUES_DOC"] = str(file)
    documents = [
        "",
        "Plain operating note\n\n## Recent Promotions\n- hide this",
        "# Notes\n\n## Risk Posture\nKeep budgets.\n\n## Private notes\nSecret filler.\n\n"
        "## Current Value Model\nAsk once.\n\n## Recent Promotions\nDo not inject.\n\n## Anti-Goals\nAvoid repeats.",
        "## Current Value Model\n" + "å " * 1000,
    ]
    compact = []
    for document in documents:
        file.write_text(document, encoding="utf-8")
        compact.append(dict(document=document, text=memory.compact_user_values()))
    file.write_text(documents[2], encoding="utf-8")
    brief = memory.leader_context_instruction(leader_session="leader")

    db.now_ms = lambda: AT
    question = "  May  we  reuse this?  "
    decision = memory.log_decision(question=question, user_answer="  Reuse the proof.  ", tags=["Proof"])
    application = memory.log_application(decision, reason="same evidence", trace_id="trace")
    before = memory.latest_promotion_activity()
    fact = memory.merge_decision_to_user_values(decision_id=decision, tags=["Proof"])
    candidate = memory.search_memory("", tags=["proof"])
    db.now_ms = lambda: AT + 10
    assert memory.merge_decision_to_user_values(statement="reuse the proof.", promote=True) == fact
    after = memory.latest_promotion_activity()
    promoted = memory.search_memory("not the question", tags=["p%f", "missing"])
    db.now_ms = lambda: AT + 20
    memory.promote_user_value_fact(fact)
    idempotent = memory.latest_promotion_activity()

    ids = {decision: "<decision>", application: "<application>", fact: "<fact>"}
    def scrub(rows):
        return {key: [{k: ids.get(v, v) if isinstance(v, str) else v for k, v in row.items()}
                      for row in values] for key, values in rows.items()}

    OUT.write_text(json.dumps(dict(compact=compact, brief=brief, question=question,
        question_key=memory.question_hash(question), candidate=scrub(candidate),
        promoted=scrub(promoted), before=before, after=after, idempotent=idempotent,
    ), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


if __name__ == "__main__":
    run()
