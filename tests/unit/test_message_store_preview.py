"""Regression coverage for the chat-list preview (`last_message_preview`).

The preview must show the genuinely-latest message. Message timestamps arrive
with different fractional precision, and updated_at is cache-maintenance time,
so ordering must use SQLite's parsed semantic timestamp with updated_at only as
a fallback for legacy rows that have no valid timestamp.
"""
from __future__ import annotations

from lib import db, message_store


def _agent(agent_id="a1", session="mike"):
    db.conn().execute(
        "INSERT INTO agents (agent_id, persona, voice_id, cwd, session, created_at)"
        " VALUES (?,?,?,?,?,?)", (agent_id, "Mike", "v", "/tmp", session, db.now_ms()))


def _msg(mid, seq, text, timestamp, updated_at, role="assistant", agent_id="a1",
         origin="user"):
    db.conn().execute(
        "INSERT INTO messages (message_id, agent_id, seq, role, timestamp, text,"
        " tools_json, updated_at, origin) VALUES (?,?,?,?,?,?,?,?,?)",
        (mid, agent_id, seq, role, timestamp, text, "[]", updated_at, origin))


def test_preview_orders_by_parsed_timestamp_not_lexical_timestamp():
    """The bug: newer transcript reply (microsecond stamp) sorts lexically below
    the older message (millisecond stamp), so the string-ordered query returns the
    OLDER one. Must pick by recency instead."""
    _agent()
    # The newer microsecond timestamp sorts lexically below the older
    # millisecond timestamp, and its maintenance timestamp is deliberately old.
    _msg("m1", 1, "older reply", "2026-06-21T17:17:44.453Z", 9000)
    _msg("m2", 2, "newer reply", "2026-06-21T17:17:44.453123Z", 1000)
    assert message_store.last_message_preview(agent_id="a1") == "newer reply"


def test_message_activity_ignores_cache_refresh_time():
    _agent()
    _msg("m1", 1, "old reply", "2026-06-08T15:43:06.040Z",
         1_784_000_000_000)
    assert message_store.last_message_activity(agent_id="a1") == 1_780_933_386_040


def test_real_message_activity_ignores_automation_origins():
    _agent()
    _msg("m1", 1, "user reply", "", 1_000, origin="user")
    _msg("m2", 2, "heartbeat reply", "", 2_000, origin="heartbeat")
    _msg("m3", 3, "agent reply", "", 3_000, origin="agent")
    assert message_store.last_message_activity(agent_id="a1") == 3_000
    assert message_store.last_real_message_activity(agent_id="a1") == 1_000


def test_preview_strips_markup_and_picks_latest():
    _agent()
    _msg("m1", 1, "first", "2026-06-21T10:00:00.000Z", 1000)
    _msg("m2", 2, "<speak>All <vox>um</vox> done. <break time=\"350ms\"/>"
         "<speed ratio=\"0.85\">Ship it</speed>.</speak>",
         "2026-06-21T10:00:01.000Z", 2000)
    assert message_store.last_message_preview(agent_id="a1") == "All done. Ship it."


def test_preview_drops_team_blocks_wholesale():
    _agent()
    _msg("m1", 1, "first", "2026-06-21T10:00:00.000Z", 1000)
    _msg("m2", 2, "Done. <team>private coordination update</team> Next.",
         "2026-06-21T10:00:01.000Z", 2000)
    assert message_store.last_message_preview(agent_id="a1") == "Done. Next."


def test_preview_prefixes_user_messages():
    _agent()
    _msg("m1", 1, "agent reply", "2026-06-21T10:00:00.000Z", 1000)
    _msg("u1", -1, "my question", "2026-06-21T10:00:05.000Z", 5000, role="user")
    assert message_store.last_message_preview(agent_id="a1") == "You: my question"


def test_preview_ignores_newer_routine_automation_rows():
    _agent()
    _msg("m1", 1, "real reply", "2026-06-21T10:00:00.000Z", 1000)
    _msg("h1", 2, "HEARTBEAT_OK", "2026-06-21T10:01:00.000Z", 2000,
         origin="heartbeat")
    _msg("l1", 3, "LEADER_NOOP", "2026-06-21T10:02:00.000Z", 3000,
         origin="leader_tick")
    _msg("d1", 4, "DREAMING_OK", "2026-06-21T10:03:00.000Z", 4000,
         origin="dreaming")
    assert message_store.last_message_preview(agent_id="a1") == "real reply"


def test_preview_empty_when_only_routine_automation_rows():
    _agent()
    _msg("h1", 1, "HEARTBEAT_OK", "2026-06-21T10:01:00.000Z", 2000,
         origin="heartbeat")
    _msg("d1", 2, "DREAMING_OK", "2026-06-21T10:02:00.000Z", 3000,
         origin="dreaming")
    assert message_store.last_message_preview(agent_id="a1") == ""


def test_preview_empty_when_no_messages():
    _agent()
    assert message_store.last_message_preview(agent_id="a1") == ""
