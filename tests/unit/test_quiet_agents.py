"""A Janitor's work never reaches the owner by itself (docs/notification-policy.md,
"Quiet agents"): no push, no unread, nothing in Updates, Ctrl+J or the "agents
working" Live Activity. Its janitor_failure alert is the one exception."""
from __future__ import annotations

import pytest

from lib import (agents, artifacts, attention_index, backends, db, janitors,
                 janitor_attention, live_activity, message_store, user_notifications)
from lib.audio_stream import AudioStream
from lib.context import ServerContext, StubSTT
from lib.janitor_context import build_context_from_connection
from lib.snapshot import build_agent_snapshot
from lib.tts_engine import FakeTTSEngine


@pytest.fixture
def fleet(monkeypatch):
    """Sam is a Janitor watching Hugo; Mike is an ordinary agent."""
    monkeypatch.setattr(backends, "active_handles", lambda *a: [])
    sam = agents.create_agent(persona="Sam", voice_id="", cwd="/tmp", session="sam", backend="codex")
    hugo = agents.create_agent(persona="Hugo", voice_id="", cwd="/tmp", session="hugo", backend="codex")
    mike = agents.create_agent(persona="Mike", voice_id="", cwd="/tmp", session="mike")
    agents.record_state(hugo, "done", {"origin": "user", "trace_id": "work"})
    config = janitors.create("sam", attachments=[{"trigger_id": "agent-work-completed"}])
    config = janitors.set_enabled("sam", config["revision"], True)
    return {"sam": sam, "hugo": hugo, "mike": mike, "config": config}


def _reply(agent_id: str, session: str, text: str) -> dict:
    """A user-origin turn that ends in a spoken reply, classified at DONE."""
    message_store.record_user_message(
        agent_id=agent_id, backend_session_id="bs", client_msg_id=f"u-{session}",
        text="prompt", origin="user")
    now = db.now_ms()
    db.conn().execute(
        """INSERT INTO messages (message_id, agent_id, backend_session_id, seq, role, text,
               tools_json, updated_at, origin) VALUES (?, ?, 'bs', 1, 'assistant', ?, '[]', ?, 'user')""",
        (f"a-{session}", agent_id, text, now + 1))
    return user_notifications.classify_completed_turn(
        agent_id=agent_id, session=session, persona=session, backend_session_id="bs",
        done_ts=now + 2, settle_timeout_s=0)


def _fail_twice(fleet) -> dict:
    config = fleet["config"]
    for _ in range(2):
        run = janitors.create_run(config["attachments"][0]["attachment_id"], config["generation"],
                                  [build_context_from_connection(db.conn(), "hugo")])
        janitors.finish_run(run["run_id"], "error", "Model limit reached")
    assert janitor_attention.reconcile() == 1
    return janitor_attention.pending()[0]


def test_a_janitor_reply_is_neither_pushed_nor_unread_but_an_ordinary_one_is(fleet):
    janitor = _reply(fleet["sam"], "sam", "<speak>Relabelled three tasks.</speak>")
    ordinary = _reply(fleet["mike"], "mike", "<speak>Done with the build.</speak>")
    assert janitor["reason"] == "janitor-maintenance"
    assert not any(janitor[key] for key in ("notify", "push", "badge", "unread"))
    assert all(ordinary[key] for key in ("notify", "push", "badge", "unread"))


def test_a_janitors_decisions_and_artifacts_stay_out_of_updates(fleet):
    artifacts.create_decision(session="sam", title="Relabel?", question="Rename Hugo's task?")
    artifacts.create(session="sam", type="document", title="Label review", status="failed",
                     payload={"content": "Reviewed"})
    asked = artifacts.create_decision(session="mike", title="Deploy?", question="Now?")
    report = artifacts.create(session="mike", type="document", title="Report", status="failed",
                              payload={"content": "Ready"})

    assert [d["artifact_id"] for d in artifacts.attention(include_questions=True)] == \
        [asked["artifact_id"]]
    assert [a["artifact_id"] for a in attention_index.page()["artifacts"]] == [report["artifact_id"]]
    badge = live_activity.badge_attention()
    assert badge == {"count": 2, "agent_ids": {fleet["mike"]}}
    # The Janitor's own chat still has them.
    assert len(artifacts.list_artifacts(session="sam")) == 2


def test_a_janitor_failure_still_reaches_updates(fleet):
    item = _fail_twice(fleet)
    assert item["attention_kind"] == "janitor_failure" and item["agent_id"] == fleet["sam"]
    badge = live_activity.badge_attention()
    assert badge == {"count": 1, "agent_ids": {fleet["sam"]}}


def _status(agent_id, session, state):
    return {"type": "live", "agent_id": agent_id, "session": session,
            "ops": [{"op": "status", "activity": {"state": state, "turn_started_ms": 1}}]}


def test_a_working_janitor_is_not_in_the_agents_working_live_activity(fleet):
    sent = []
    pusher = live_activity.LiveActivityPusher(
        lambda payload, priority: sent.append(payload["aps"]["content-state"]),
        clock=lambda: 1000.0, schedule=lambda *a: None)
    pusher.observe(_status(fleet["sam"], "sam", "tool"))
    pusher.observe(_status(fleet["sam"], "sam", "waiting"))
    assert sent == []
    pusher.observe(_status(fleet["mike"], "mike", "thinking"))
    assert [(s["working"], s["needs_you"]) for s in sent] == [(1, 0)]
    assert [row["agent_id"] for row in sent[-1]["agents"]] == [fleet["mike"]]


def test_the_snapshot_marks_janitors_quiet(fleet, tmp_path):
    ctx = ServerContext(
        root=tmp_path, static=tmp_path, audio_dir=tmp_path / "audio",
        agents_path=tmp_path / "agents.json", default_session="mike",
        tts=FakeTTSEngine(tmp_path / "audio"), stream=AudioStream(tmp_path / "audio"),
        stt=StubSTT(), roster_names=())
    quiet = {row["session"]: row["quiet"] for row in build_agent_snapshot(ctx)["agents"]}
    assert quiet == {"sam": True, "hugo": False, "mike": False}
