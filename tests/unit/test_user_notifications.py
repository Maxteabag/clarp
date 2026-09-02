"""user-facing notification policy."""
from __future__ import annotations

import itertools

from lib import agents as agents_db
from lib import db, message_store, user_notifications
from lib import team_store


_IDS = itertools.count()


def _agent(*, persona: str = "Arnold", session: str = "arnold") -> str:
    return agents_db.create_agent(
        persona=persona, voice_id="v", cwd="/tmp", session=session)


def _team_leader() -> str:
    aid = _agent()
    team = team_store.create_team(f"team-{next(_IDS)}")
    assert team_store.add_member(team["team_id"], aid)
    team_store.set_leader(team["team_id"], aid)
    return aid


def _turn(agent_id: str, *, origin: str, assistant: str,
          backend_session_id: str = "bs-1", done_ts: int | None = None,
          sender_agent_id: str = ""
          ) -> int:
    now = db.now_ms()
    suffix = next(_IDS)
    message_store.record_user_message(
        agent_id=agent_id,
        backend_session_id=backend_session_id,
        client_msg_id=f"u-{origin}-{now}-{suffix}",
        text="prompt",
        origin=origin,
        sender_agent_id=sender_agent_id,
    )
    db.conn().execute(
        """INSERT INTO messages (
               message_id, agent_id, backend_session_id, seq, role, text,
               tools_json, updated_at, origin
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            f"a-{origin}-{now}-{suffix}", agent_id, backend_session_id, 1, "assistant",
            assistant, "[]", now + 1, origin,
        ),
    )
    return done_ts or now + 2


def _classify(agent_id: str, done_ts: int,
              backend_session_id: str = "bs-1") -> dict:
    return user_notifications.classify_completed_turn(
        agent_id=agent_id,
        session="arnold",
        persona="Arnold",
        backend_session_id=backend_session_id,
        done_ts=done_ts,
    )


def test_user_origin_speak_notifies(monkeypatch):
    monkeypatch.setattr(user_notifications, "SETTLE_TIMEOUT_S", 0)
    aid = _agent()
    done_ts = _turn(aid, origin="user", assistant="<speak>User should see this.</speak>")

    notification = _classify(aid, done_ts)

    assert notification["notify"] is True
    assert notification["push"] is True
    assert notification["badge"] is True
    assert notification["unread"] is True
    assert notification["preview"] == "User should see this."
    assert notification["reason"] == "speak"


def test_worker_origin_speak_is_suppressed(monkeypatch):
    user_notifications.settings_store.set_text(
        "automation_special_treatment", "true")
    monkeypatch.setattr(user_notifications, "SETTLE_TIMEOUT_S", 0)
    aid = _agent()
    done_ts = _turn(aid, origin="agent", assistant="<speak>Worker status.</speak>")

    notification = _classify(aid, done_ts)

    assert notification["notify"] is False
    assert notification["push"] is False
    assert notification["badge"] is False
    assert notification["preview"] == ""
    assert notification["reason"] == "not-user-facing-origin:agent"


def test_worker_origin_not_suppressed_when_special_treatment_is_off(monkeypatch):
    monkeypatch.setattr(user_notifications, "SETTLE_TIMEOUT_S", 0)
    user_notifications.settings_store.set_text(
        "automation_special_treatment", "false")
    aid = _agent()
    done_ts = _turn(aid, origin="agent", assistant="<speak>Worker status.</speak>")

    notification = _classify(aid, done_ts)

    assert notification["notify"] is True
    assert notification["push"] is True
    assert notification["badge"] is True
    assert notification["preview"] == "Worker status."


def test_watcher_origin_summary_notifies(monkeypatch):
    monkeypatch.setattr(user_notifications, "SETTLE_TIMEOUT_S", 0)
    aid = _agent(persona="Nadia", session="nadia")
    done_ts = _turn(
        aid,
        origin="watcher",
        assistant="Emilly replied to the message you asked me to watch.",
    )

    notification = user_notifications.classify_completed_turn(
        agent_id=aid,
        session="nadia",
        persona="Nadia",
        backend_session_id="bs-1",
        done_ts=done_ts,
    )

    assert notification["notify"] is True
    assert notification["push"] is True
    assert notification["badge"] is True
    assert notification["origin"] == "watcher"
    assert notification["preview"] == "Emilly replied to the message you asked me to watch."


def test_leader_delegated_agent_origin_is_suppressed(monkeypatch):
    user_notifications.settings_store.set_text(
        "automation_special_treatment", "true")
    monkeypatch.setattr(user_notifications, "SETTLE_TIMEOUT_S", 0)
    leader_id = _team_leader()
    aid = _agent(persona="Omar", session="omar")
    done_ts = _turn(
        aid,
        origin="agent",
        sender_agent_id=leader_id,
        assistant="<speak>Leader-directed worker output.</speak>",
    )

    notification = user_notifications.classify_completed_turn(
        agent_id=aid,
        session="omar",
        persona="Omar",
        backend_session_id="bs-1",
        done_ts=done_ts,
    )

    assert notification["notify"] is False
    assert notification["push"] is False
    assert notification["badge"] is False
    assert notification["preview"] == ""
    assert notification["reason"] == "not-user-facing-origin:agent"


def test_leader_tick_speak_is_proactive_notification(monkeypatch):
    monkeypatch.setattr(user_notifications, "SETTLE_TIMEOUT_S", 0)
    aid = _team_leader()
    done_ts = _turn(
        aid,
        origin="leader_tick",
        assistant="<speak>I found something User should review.</speak>",
    )

    notification = _classify(aid, done_ts)

    assert notification["notify"] is True
    assert notification["origin"] == "leader_tick"
    assert notification["preview"] == "I found something User should review."


def test_worker_leader_tick_speak_is_suppressed(monkeypatch):
    user_notifications.settings_store.set_text(
        "automation_special_treatment", "true")
    monkeypatch.setattr(user_notifications, "SETTLE_TIMEOUT_S", 0)
    aid = _agent(persona="Omar", session="omar")
    done_ts = _turn(
        aid,
        origin="leader_tick",
        assistant="<speak>Worker status should stay with the leader.</speak>",
    )

    notification = user_notifications.classify_completed_turn(
        agent_id=aid,
        session="omar",
        persona="Omar",
        backend_session_id="bs-1",
        done_ts=done_ts,
    )

    assert notification["notify"] is False
    assert notification["push"] is False
    assert notification["badge"] is False
    assert notification["unread"] is False
    assert notification["preview"] == ""
    assert notification["reason"] == "leader-tick-non-leader"


def test_user_origin_text_reply_notifies(monkeypatch):
    monkeypatch.setattr(user_notifications, "SETTLE_TIMEOUT_S", 0)
    aid = _agent()
    done_ts = _turn(aid, origin="user", assistant="Plain written follow-up.")

    notification = _classify(aid, done_ts)

    assert notification["notify"] is True
    assert notification["push"] is True
    assert notification["badge"] is True
    assert notification["unread"] is True
    assert notification["preview"] == "Plain written follow-up."
    assert notification["reason"] == "text-reply"


def test_long_turn_does_not_push_old_acknowledgment_then_reclassifies_final(monkeypatch):
    """Regression: DONE can precede final transcript import. An acknowledgment
    from minutes earlier must not become the completion push; the transcript
    retry should bind and push the final reply when it arrives."""
    monkeypatch.setattr(user_notifications, "SETTLE_TIMEOUT_S", 0)
    aid = _agent(persona="Josh", session="josh")
    now = db.now_ms()
    backend = "bs-long-turn"
    message_store.record_user_message(
        agent_id=aid,
        backend_session_id=backend,
        client_msg_id=f"u-long-{now}",
        text="Hide the keyboard after submitting",
        origin="user",
    )
    db.conn().execute(
        """INSERT INTO messages (
               message_id, agent_id, backend_session_id, seq, role, text,
               tools_json, updated_at, origin
           ) VALUES (?, ?, ?, ?, 'assistant', ?, '[]', ?, 'user')""",
        ("ack-long", aid, backend, 1,
         "I'll explicitly dismiss the keyboard.", now + 1),
    )
    done_ts = now + 6 * 60 * 1000

    initial = user_notifications.classify_completed_turn(
        agent_id=aid, session="josh", persona="Josh",
        backend_session_id=backend, done_ts=done_ts,
    )
    assert initial["notify"] is False
    assert initial["source_message_id"] == ""

    db.conn().execute(
        """INSERT INTO messages (
               message_id, agent_id, backend_session_id, seq, role, text,
               tools_json, updated_at, origin
           ) VALUES (?, ?, ?, ?, 'assistant', ?, '[]', ?, 'user')""",
        ("final-long", aid, backend, 2,
         "Fixed and deployed in TestFlight build 126.", done_ts + 7_000),
    )
    retried = user_notifications.reclassify_recent_suppressed(
        agent_id=aid, backend_session_id=backend, now_ms_value=done_ts + 8_000,
    )

    assert len(retried) == 1
    assert retried[0]["source_message_id"] == "final-long"
    assert retried[0]["preview"] == "Fixed and deployed in TestFlight build 126."


def test_recent_progress_acknowledgment_is_not_used_for_completion_push(monkeypatch):
    """Live regression: DONE beat the final row by 81 ms and incorrectly used
    a progress acknowledgment written nine seconds earlier as the push body.
    """
    monkeypatch.setattr(user_notifications, "SETTLE_TIMEOUT_S", 0)
    aid = _agent(persona="Arnold", session="arnold")
    now = db.now_ms()
    backend = "bs-recent-ack"
    message_store.record_user_message(
        agent_id=aid,
        backend_session_id=backend,
        client_msg_id=f"u-recent-{now}",
        text="Change the cold Action Button behavior",
        origin="user",
    )
    done_ts = now + 20_000
    db.conn().execute(
        """INSERT INTO messages (
               message_id, agent_id, backend_session_id, seq, role, text,
               tools_json, updated_at, origin
           ) VALUES (?, ?, ?, ?, 'assistant', ?, '[]', ?, 'user')""",
        ("progress-recent", aid, backend, 1,
         "Got it — I'll add that setting.", done_ts - 8_991),
    )
    db.conn().execute(
        "UPDATE messages SET kind = 'commentary' WHERE message_id = 'progress-recent'"
    )

    initial = user_notifications.classify_completed_turn(
        agent_id=aid, session="arnold", persona="Arnold",
        backend_session_id=backend, done_ts=done_ts,
    )

    assert initial["notify"] is False
    assert initial["source_message_id"] == ""

    db.conn().execute(
        """INSERT INTO messages (
               message_id, agent_id, backend_session_id, seq, role, text,
               tools_json, updated_at, origin
           ) VALUES (?, ?, ?, ?, 'assistant', ?, '[]', ?, 'user')""",
        ("final-recent", aid, backend, 2,
         "The behavior is now explicit and deployed.", done_ts + 81),
    )
    db.conn().execute(
        "UPDATE messages SET kind = 'final_answer' WHERE message_id = 'final-recent'"
    )
    retried = user_notifications.reclassify_recent_suppressed(
        agent_id=aid, backend_session_id=backend, now_ms_value=done_ts + 100,
    )

    assert len(retried) == 1
    assert retried[0]["source_message_id"] == "final-recent"
    assert retried[0]["preview"] == "The behavior is now explicit and deployed."


def test_user_origin_team_only_reply_is_suppressed(monkeypatch):
    monkeypatch.setattr(user_notifications, "SETTLE_TIMEOUT_S", 0)
    aid = _agent()
    done_ts = _turn(
        aid,
        origin="user",
        assistant="<team>I updated the team feed only.</team>",
    )

    notification = _classify(aid, done_ts)

    assert notification["notify"] is False
    assert notification["push"] is False
    assert notification["badge"] is False
    assert notification["preview"] == ""
    assert notification["reason"] == "no-user-facing-content"


def test_user_origin_leader_noop_is_suppressed(monkeypatch):
    monkeypatch.setattr(user_notifications, "SETTLE_TIMEOUT_S", 0)
    aid = _team_leader()
    done_ts = _turn(
        aid,
        origin="leader_tick",
        assistant="LEADER_NOOP",
    )

    notification = _classify(aid, done_ts)

    assert notification["notify"] is False
    assert notification["reason"] == "no-user-facing-content"


def test_leader_tick_uses_visible_prose_before_later_noop(monkeypatch):
    """Mirror Arnold's live shape: a leader tick imports several assistant rows
    together, and the newest one can be a no-op while earlier rows contain the
    actual user-facing prose.
    """
    monkeypatch.setattr(user_notifications, "SETTLE_TIMEOUT_S", 0)
    aid = _team_leader()
    now = db.now_ms()
    message_store.record_user_message(
        agent_id=aid,
        backend_session_id="bs-1",
        client_msg_id=f"u-leader-{now}",
        text="[Automated team check] Review your team's status.",
        origin="leader_tick",
    )
    rows = [
        ("empty-before", 10, "", "agent"),
        (
            "real-prose",
            11,
            "**Plan:** delegate the build check, track the result, and report the "
            "status back to the user in the morning.",
            "agent",
        ),
        ("empty-after", 12, "", "leader_tick"),
        ("noop-after", 13, "Leader check: no action needed.", "leader_tick"),
    ]
    for message_id, seq, text, origin in rows:
        db.conn().execute(
            """INSERT INTO messages (
                   message_id, agent_id, backend_session_id, seq, role, text,
                   tools_json, updated_at, origin
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                message_id,
                aid,
                "bs-1",
                seq,
                "assistant",
                text,
                "[]",
                now + 100,
                origin,
            ),
        )

    notification = _classify(aid, now + 200)

    assert notification["notify"] is True
    assert notification["push"] is True
    assert notification["badge"] is True
    assert notification["reason"] == "text-reply"
    assert notification["source_message_id"] == "real-prose"
    assert notification["preview"].startswith("**Plan:** delegate the build check")


def test_late_transcript_import_reclassifies_recent_suppressed_turn(monkeypatch):
    monkeypatch.setattr(user_notifications, "SETTLE_TIMEOUT_S", 0)
    aid = _agent()
    now = db.now_ms()
    message_store.record_user_message(
        agent_id=aid,
        backend_session_id="bs-1",
        client_msg_id=f"u-late-{now}",
        text="What is the plan?",
        origin="user",
    )
    done_ts = now + 100

    initial = _classify(aid, done_ts)
    assert initial["notify"] is False
    assert initial["reason"] == "no-user-facing-content"

    db.conn().execute(
        """INSERT INTO messages (
               message_id, agent_id, backend_session_id, seq, role, text,
               tools_json, updated_at, origin
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            "late-prose",
            aid,
            "bs-1",
            20,
            "assistant",
            "Here is the concrete plan for the user.",
            "[]",
            done_ts + 5_000,
            "user",
        ),
    )

    flipped = user_notifications.reclassify_recent_suppressed(
        agent_id=aid,
        backend_session_id="bs-1",
        now_ms_value=done_ts + 6_000,
    )

    assert len(flipped) == 1
    assert flipped[0]["notify"] is True
    assert flipped[0]["push"] is True
    assert flipped[0]["reason"] == "text-reply"
    assert flipped[0]["source_message_id"] == "late-prose"


def test_event_payload_is_coupled_for_native_and_apns(monkeypatch):
    monkeypatch.setattr(user_notifications, "SETTLE_TIMEOUT_S", 0)
    aid = _agent()
    done_ts = _turn(aid, origin="user", assistant="<speak>Ready.</speak>")

    payload = user_notifications.event_payload(_classify(aid, done_ts))

    assert payload["type"] == "user-notification"
    assert payload["push"] is True
    assert payload["badge"] is True
    assert payload["unread"] is True
    assert payload["preview"] == "Ready."


def test_muted_agent_suppresses_push_but_keeps_badge_unread(monkeypatch):
    monkeypatch.setattr(user_notifications, "SETTLE_TIMEOUT_S", 0)
    aid = _agent()
    agents_db.update_agent(aid, muted=True)
    done_ts = _turn(aid, origin="user", assistant="Quiet badge.")

    notification = _classify(aid, done_ts)
    payload = user_notifications.event_payload(notification)

    assert notification["notify"] is True
    assert notification["push"] is False
    assert notification["badge"] is True
    assert notification["unread"] is True
    assert notification["muted"] is True
    assert notification["reason"] == "text-reply-muted"
    assert payload["push"] is False
    assert payload["badge"] is True
    assert payload["unread"] is True
    assert payload["muted"] is True


def test_unmute_restores_push(monkeypatch):
    monkeypatch.setattr(user_notifications, "SETTLE_TIMEOUT_S", 0)
    aid = _agent()
    agents_db.update_agent(aid, muted=True)
    agents_db.update_agent(aid, muted=False)
    done_ts = _turn(aid, origin="user", assistant="<speak>Interrupt again.</speak>")

    notification = _classify(aid, done_ts)

    assert notification["notify"] is True
    assert notification["push"] is True
    assert notification["badge"] is True
    assert notification["unread"] is True
    assert notification["muted"] is False
    assert notification["reason"] == "speak"
