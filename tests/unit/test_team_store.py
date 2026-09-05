from lib import agents as agents_db
from lib import db
from lib import team_store
from lib.protocol import AgentState


def test_extract_speak_blocks_strips_voice_markup():
    blocks = team_store.extract_speak_blocks(
        'plain <speak><vox>um</vox> update<br/>done</speak> tail'
    )

    assert blocks == ["Update done"]


def test_team_block_fans_out_to_teammates(tmp_path):
    mike = agents_db.create_agent(
        persona="Mike", voice_id="V", cwd=str(tmp_path), session="mike"
    )
    rachel = agents_db.create_agent(
        persona="Rachel", voice_id="V", cwd=str(tmp_path), session="rachel"
    )
    adam = agents_db.create_agent(
        persona="Adam", voice_id="V", cwd=str(tmp_path), session="adam"
    )
    team = team_store.create_team("Ops", color="moss", communication_enabled=True)
    assert team_store.add_member(team["team_id"], mike)
    assert team_store.add_member(team["team_id"], rachel)
    assert team_store.add_member(team["team_id"], adam)

    inserted = team_store.capture_assistant_message(
        agent_id=mike,
        source_message_id="msg-1",
        trace_id="trace-1",
        text="<team>Database deploy is finished.</team>",
    )

    assert inserted == 1
    digest, ids = team_store.pending_digest(rachel)
    assert "Team updates since your last turn:" in digest
    assert "[Ops] Mike: Database deploy is finished." in digest
    assert len(ids) == 1
    source_digest, source_ids = team_store.pending_digest(mike)
    assert source_digest == ""
    assert source_ids == []

    team_store.mark_injected(rachel, ids)
    digest_after, ids_after = team_store.pending_digest(rachel)
    assert digest_after == ""
    assert ids_after == []


def test_team_feed_is_decoupled_from_speak(tmp_path):
    """The team feed is driven by <team>, not <speak>: what an agent says to
    the user must NOT leak to teammates, and only <team> content fans out."""
    mike = agents_db.create_agent(
        persona="Mike", voice_id="V", cwd=str(tmp_path), session="mike")
    rachel = agents_db.create_agent(
        persona="Rachel", voice_id="V", cwd=str(tmp_path), session="rachel")
    team = team_store.create_team("Ops", communication_enabled=True)
    team_store.add_member(team["team_id"], mike)
    team_store.add_member(team["team_id"], rachel)

    # Spoken-only update: for the user's ears, never the team.
    assert team_store.capture_assistant_message(
        agent_id=mike, source_message_id="s1",
        text="<speak>Hey User, all done.</speak>") == 0
    assert team_store.pending_digest(rachel) == ("", [])

    # A turn that speaks to the user AND broadcasts: only the <team> part fans out.
    assert team_store.capture_assistant_message(
        agent_id=mike, source_message_id="s2",
        text="<speak>Telling User the secret.</speak> "
             "<team>Refactored the parser.</team>") == 1
    digest, _ = team_store.pending_digest(rachel)
    assert "Refactored the parser." in digest
    assert "secret" not in digest


def test_team_protocol_instruction_only_for_members(tmp_path):
    member = agents_db.create_agent(
        persona="Mike", voice_id="V", cwd=str(tmp_path), session="mike")
    loner = agents_db.create_agent(
        persona="Rachel", voice_id="V", cwd=str(tmp_path), session="rachel")
    team = team_store.create_team("Ops", communication_enabled=True)
    team_store.add_member(team["team_id"], member)

    brief = team_store.team_protocol_instruction(member)
    assert "Ops" in brief
    assert "<team>" in brief
    assert "Report TERMINAL results directly" not in brief
    assert team_store.team_protocol_instruction(loner) == ""


def test_extract_team_blocks_strips_markup():
    blocks = team_store.extract_team_blocks(
        'noise <team><vox>um</vox> shipped<br/>the fix</team> tail')
    assert blocks == ["Shipped the fix"]


def test_set_leader_requires_membership_and_round_trips(tmp_path):
    leader = agents_db.create_agent(
        persona="Lena", voice_id="V", cwd=str(tmp_path), session="lena")
    outsider = agents_db.create_agent(
        persona="Omar", voice_id="V", cwd=str(tmp_path), session="omar")
    team = team_store.create_team("Ops", communication_enabled=True)
    team_store.add_member(team["team_id"], leader)

    import pytest
    with pytest.raises(ValueError):
        team_store.set_leader(team["team_id"], outsider)  # not a member

    team_store.set_leader(team["team_id"], leader)
    assert team_store.get_leader(team["team_id"]) == leader
    assert team_store.get_team(team["team_id"])["leader_agent_id"] == leader

    team_store.set_leader(team["team_id"], None)           # clear
    assert team_store.get_leader(team["team_id"]) == ""


def test_team_nudging_defaults_off_and_round_trips(tmp_path):
    team = team_store.create_team("Ops", communication_enabled=True)

    assert team["nudge_enabled"] is False
    assert team_store.get_team(team["team_id"])["nudge_enabled"] is False

    updated = team_store.set_nudge_enabled(team["team_id"], False)
    assert updated["nudge_enabled"] is False
    assert team_store.get_team(team["team_id"])["nudge_enabled"] is False

    updated = team_store.set_nudge_enabled(team["team_id"], True)
    assert updated["nudge_enabled"] is True
    assert team_store.get_team(team["team_id"])["nudge_enabled"] is True
    assert team_store.set_nudge_enabled("missing", False) is None


def test_latest_activity_ignores_peer_tool_chatter(tmp_path):
    leader = agents_db.create_agent(
        persona="Leader", voice_id="V", cwd=str(tmp_path), session="leader")
    worker = agents_db.create_agent(
        persona="Worker", voice_id="V", cwd=str(tmp_path), session="worker")
    team = team_store.create_team("Ops", communication_enabled=True)
    team_store.add_member(team["team_id"], leader)
    team_store.add_member(team["team_id"], worker)
    baseline = team_store.latest_activity_for_agent(leader)

    agents_db.record_state(worker, AgentState.TOOL, {"tool": "pytest"})
    db.conn().execute(
        "UPDATE state_log SET ts = ? WHERE agent_id = ? AND kind = ?",
        (baseline + 100, worker, AgentState.TOOL),
    )
    assert team_store.latest_activity_for_agent(leader) == baseline

    agents_db.record_state(worker, AgentState.THINKING, {"trace_id": "turn-1"})
    db.conn().execute(
        "UPDATE state_log SET ts = ? WHERE agent_id = ? AND kind = ?",
        (baseline + 200, worker, AgentState.THINKING),
    )
    assert team_store.latest_activity_for_agent(leader) > baseline


def test_leader_gets_coordination_brief_others_do_not(tmp_path):
    leader = agents_db.create_agent(
        persona="Lena", voice_id="V", cwd=str(tmp_path), session="lena")
    worker = agents_db.create_agent(
        persona="Omar", voice_id="V", cwd=str(tmp_path), session="omar")
    team = team_store.create_team("Ops", communication_enabled=True)
    team_store.add_member(team["team_id"], leader)
    team_store.add_member(team["team_id"], worker)
    team_store.set_leader(team["team_id"], leader)

    leader_brief = team_store.team_protocol_instruction(leader)
    assert "Leader role: decide, delegate, track, and learn" in leader_brief
    assert "--from lena" in leader_brief          # how to prompt as itself
    assert "Omar" in leader_brief                 # member roster + state
    assert "LEADER STANDING ORDERS v2" in leader_brief
    assert "Compact User Values" in leader_brief
    assert "Do not implement work yourself" in leader_brief
    assert "Report TERMINAL results directly" not in leader_brief

    worker_brief = team_store.team_protocol_instruction(worker)
    assert "<team>" in worker_brief               # still a normal member
    assert "Report TERMINAL results directly to leader Lena" in worker_brief
    assert "self-prompt --from omar --to lena" in worker_brief
    assert "leader needs to act -> direct" in worker_brief
    assert "LEADER of this team" not in worker_brief
    assert "LEADER STANDING ORDERS v2" not in worker_brief
    assert "Compact User Values" not in worker_brief


def test_leader_heartbeat_gets_lean_protocol(tmp_path):
    leader = agents_db.create_agent(
        persona="Lena", voice_id="V", cwd=str(tmp_path), session="lena")
    worker = agents_db.create_agent(
        persona="Omar", voice_id="V", cwd=str(tmp_path), session="omar")
    team = team_store.create_team("Ops", communication_enabled=True)
    team_store.add_member(team["team_id"], leader)
    team_store.add_member(team["team_id"], worker)
    team_store.set_leader(team["team_id"], leader)
    agents_db.record_state(worker, AgentState.WAITING, {"reason": "needs input"})

    brief = team_store.team_protocol_instruction(leader, turn_origin="heartbeat")

    assert "You lead this team; decide/delegate/track" in brief
    assert "HEARTBEAT_OK" in brief
    assert "Team 'Ops' live member states" in brief
    assert "Omar (omar): waiting" in brief
    assert "LEADER STANDING ORDERS v2" not in brief
    assert "Compact User Values" not in brief
    assert "Recent Promotions" not in brief


def test_leader_tick_gets_leader_noop_reminder(tmp_path):
    leader = agents_db.create_agent(
        persona="Lena", voice_id="V", cwd=str(tmp_path), session="lena")
    worker = agents_db.create_agent(
        persona="Omar", voice_id="V", cwd=str(tmp_path), session="omar")
    team = team_store.create_team("Ops", communication_enabled=True)
    team_store.add_member(team["team_id"], leader)
    team_store.add_member(team["team_id"], worker)
    team_store.set_leader(team["team_id"], leader)

    brief = team_store.team_protocol_instruction(leader, turn_origin="leader_tick")

    assert "LEADER_NOOP" in brief
    assert "LEADER STANDING ORDERS v2" not in brief


def test_leaderless_team_does_not_get_direct_report_rule(tmp_path):
    worker = agents_db.create_agent(
        persona="Omar", voice_id="V", cwd=str(tmp_path), session="omar")
    team = team_store.create_team("Ops", communication_enabled=True)
    team_store.add_member(team["team_id"], worker)

    brief = team_store.team_protocol_instruction(worker)

    assert "<team>" in brief
    assert "Report TERMINAL results directly" not in brief


def test_digest_is_bounded_and_drains_whole_backlog(tmp_path):
    """A behind agent must not crawl through ancient backchat. The digest shows
    only the most recent `limit` updates but drains the entire unread backlog in
    one turn, so the next turn starts clean."""
    mike = agents_db.create_agent(
        persona="Mike", voice_id="V", cwd=str(tmp_path), session="mike")
    rachel = agents_db.create_agent(
        persona="Rachel", voice_id="V", cwd=str(tmp_path), session="rachel")
    team = team_store.create_team("Ops", communication_enabled=True)
    team_store.add_member(team["team_id"], mike)
    team_store.add_member(team["team_id"], rachel)

    for i in range(12):
        team_store.capture_assistant_message(
            agent_id=mike,
            source_message_id=f"msg-{i}",
            text=f"<team>Update number {i}.</team>",
        )

    digest, ids = team_store.pending_digest(rachel, limit=5)
    shown = [line for line in digest.splitlines() if line.startswith("- [Ops]")]
    assert len(shown) == 5                       # only the recent window
    assert "Update number 11." in digest         # newest is shown
    assert "Update number 7." in digest          # ...down to the 5th-newest
    assert "Update number 6." not in digest      # older ones are not
    assert "+7 earlier updates skipped" in digest
    assert len(ids) == 12                         # but the whole backlog drains

    team_store.mark_injected(rachel, ids)
    assert team_store.pending_digest(rachel) == ("", [])


def test_remove_member_clears_inbox_and_no_resurfacing(tmp_path):
    """Removing a member clears their inbox; re-adding must not resurface the
    old unread backlog (the 'still getting team context after removal' bug)."""
    mike = agents_db.create_agent(
        persona="Mike", voice_id="V", cwd=str(tmp_path), session="mike")
    rachel = agents_db.create_agent(
        persona="Rachel", voice_id="V", cwd=str(tmp_path), session="rachel")
    team = team_store.create_team("Ops", communication_enabled=True)
    team_store.add_member(team["team_id"], mike)
    team_store.add_member(team["team_id"], rachel)
    team_store.capture_assistant_message(
        agent_id=mike, source_message_id="m1",
        text="<team>Heads up.</team>")
    assert team_store.pending_digest(rachel)[1]  # rachel has unread

    team_store.remove_member(team["team_id"], rachel)
    assert team_store.pending_digest(rachel) == ("", [])

    # Re-adding gives a clean slate, not the old backlog.
    team_store.add_member(team["team_id"], rachel)
    assert team_store.pending_digest(rachel) == ("", [])


def test_delete_team_cascades(tmp_path):
    mike = agents_db.create_agent(
        persona="Mike", voice_id="V", cwd=str(tmp_path), session="mike")
    rachel = agents_db.create_agent(
        persona="Rachel", voice_id="V", cwd=str(tmp_path), session="rachel")
    team = team_store.create_team("Ops", communication_enabled=True)
    tid = team["team_id"]
    team_store.add_member(tid, mike)
    team_store.add_member(tid, rachel)
    team_store.capture_assistant_message(
        agent_id=mike, source_message_id="m1",
        text="<team>Heads up.</team>")

    assert team_store.delete_team(tid) is True
    assert team_store.get_team(tid) is None
    assert all(t["team_id"] != tid for t in team_store.list_teams(include_archived=True))
    assert team_store.pending_digest(rachel) == ("", [])
    assert team_store.list_team_messages(tid) == []
    assert team_store.delete_team(tid) is False   # already gone


def test_non_spoken_assistant_text_is_not_shared(tmp_path):
    mike = agents_db.create_agent(
        persona="Mike", voice_id="V", cwd=str(tmp_path), session="mike"
    )
    rachel = agents_db.create_agent(
        persona="Rachel", voice_id="V", cwd=str(tmp_path), session="rachel"
    )
    team = team_store.create_team("Research", communication_enabled=True)
    team_store.add_member(team["team_id"], mike)
    team_store.add_member(team["team_id"], rachel)

    inserted = team_store.capture_assistant_message(
        agent_id=mike,
        source_message_id="msg-2",
        text="This was only visible text.",
    )

    assert inserted == 0
    assert team_store.pending_digest(rachel) == ("", [])
