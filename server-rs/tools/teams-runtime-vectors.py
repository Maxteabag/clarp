#!/usr/bin/env python3
"""Record the Python Host's Teams runtime from isolated synthetic agents."""
from __future__ import annotations

import importlib.util
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import scratch

REPO = pathlib.Path(__file__).resolve().parents[2]
OUT = REPO / "server-rs/crates/clarp-kernel/tests/fixtures/teams-runtime-vectors.json"
NOW = 1_795_000_000_000


@scratch.case("clarp-teams-runtime-")
def run(root):
    spec = importlib.util.spec_from_file_location("replay_host", REPO / "tests/replay/host.py")
    replay = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(replay)
    (root / "scenario-config.toml").write_text("")
    replay.isolate(root)
    replay.loopback_only()
    sys.path.insert(0, str(REPO / "server"))
    from lib import agents, db, team_leader, team_store, turn_command
    from lib.policies import admission

    db.reset_for_tests(root / "teams.sqlite")
    names = iter(("a-leader", "a-worker", "a-other"))
    agents._new_agent_id = lambda: next(names)
    for name in ("leader", "worker", "other"):
        agents.create_agent(persona=name.capitalize(), session=name, voice_id="", cwd=str(root), backend="claude")
    # Agent creation also records a state. Keep that earlier than the feed and receipt
    # so the fixture measures their contribution with a clock of its own.
    db.conn().execute("UPDATE state_log SET ts = ?", (NOW - 100,))
    team_ids = iter(("team-ops", "team-muted"))
    team_store._new_team_id = lambda: next(team_ids)
    team_store.now_ms = lambda: NOW
    places = [
        team_store.create_team("Ops", communication_enabled=True),
        team_store.create_team("Silent"),
    ]
    for team in places:
        for person in ("a-leader", "a-worker"):
            assert team_store.add_member(team["team_id"], person)
    team_store.set_leader("team-ops", "a-leader")
    team_store.set_nudge_enabled("team-ops", True)
    agents.latest_state = lambda agent_id: {"kind": "interrupted" if agent_id == "a-worker" else "done"}
    agents.is_busy = lambda _agent_id: False
    team_leader._recent_real_activity_reason = lambda *_args: ""

    samples = [
        "To the user <speak>hello</speak>",
        "Public <TEAM topic='work'> ready\n <speak>now</speak> <b>ship</b> </TEAM>",
        "<team>  <vox>um</vox>  </team>",
        "<team><environment_context>hidden</environment_context>done</team>",
        "<team>unclosed",
    ]
    rows = [{"text": text, "blocks": team_store.extract_team_blocks(text)} for text in samples]
    raw = "<team>Forward progress</team>" + "".join(f"<team>update {i}</team>" for i in range(1, 8))
    inserted = team_store.capture_assistant_message(
        agent_id="a-worker", source_message_id="reply-1", trace_id="trace-1", text=raw)
    again = team_store.capture_assistant_message(
        agent_id="a-worker", source_message_id="reply-1", trace_id="trace-1", text=raw)
    digest, inbox = team_store.pending_digest("a-leader")
    protocols = [
        dict(agent=agent, origin=origin, text=team_store.team_protocol_instruction(agent, turn_origin=origin))
        for agent, origin in [("a-leader", "heartbeat"), ("a-leader", "leader_tick"),
                              ("a-worker", "user"), ("a-other", "user")]
    ]
    ticks = team_leader.pending_leader_ticks(now=NOW / 1000)
    before = team_store.latest_activity_for_agent("a-leader")
    team_store.now_ms = lambda: NOW + 20
    team_store.mark_injected("a-leader", inbox)
    after = team_store.latest_activity_for_agent("a-leader")
    allowed = [admission.leader_nudge_allowed(who, team_store.list_teams())
               for who in ("a-leader", "a-worker", "a-other")]
    team_store.set_nudge_enabled("team-ops", False)
    off = team_leader.pending_leader_ticks(now=NOW / 1000)
    answer_texts = ["LEADER_NOOP", "No action. LEADER_NOOP", "word LEADER_NOOP word", "LEADER_NOOP " + "x" * 301]
    OUT.write_text(json.dumps(dict(
        blocks=rows, raw=raw, inserted=inserted, again=again, digest=digest, inbox=sorted(inbox),
        prompt=turn_command._with_team_context("User asks", digest=digest, protocol=protocols[1]["text"]),
        protocols=protocols, ticks=ticks, disabled_ticks=off, nudge_allowed=allowed,
        before=before, after=after, after_digest=team_store.pending_digest("a-leader"),
        noop=[dict(text=text, result=team_leader.strip_leader_noop(text)) for text in answer_texts],
    ), indent=2, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    run()
