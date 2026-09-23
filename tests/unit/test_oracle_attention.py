"""Oracle sees current native decisions without inventing delivery evidence."""
import json
import threading
from types import SimpleNamespace

from lib import db, oracle_attention, oracle_live_stable, oracle_memory


def _decision(**overrides):
    row = {
        "decision_id": "decision-current",
        "artifact_id": "artifact-current",
        "agent_name": "Vesper",
        "session": "vesper",
        "kind": "question",
        "title": "Workspace edits",
        "question": "Which workspace should I keep?",
        "context": "The current task is blocked on this choice.",
        "response_type": "single_choice",
        "options": [
            {"id": "current", "label": "Keep current"},
            {"id": "compact", "label": "Use compact"},
        ],
        "allow_custom_text": True,
        "recommended_option_id": "current",
        "blocks_progress": True,
        "priority": 200,
        "created_at": 10,
        "updated_at": 11,
        "deadline_at": None,
        "expires_at": None,
        "status": "pending",
    }
    row.update(overrides)
    return row


def test_pending_decisions_uses_only_current_attention_projection(monkeypatch):
    calls = []

    def attention(**kwargs):
        calls.append(kwargs)
        return [_decision(), _decision(decision_id="answered", status="answered"),
                _decision(decision_id="expired", status="expired")]

    monkeypatch.setattr(oracle_attention.artifacts, "attention", attention)
    rows = oracle_attention.pending_decisions()

    assert calls == [{"include_questions": True}]
    assert [row["decision_id"] for row in rows] == ["decision-current"]
    assert "answer" not in rows[0]
    assert "push" not in rows[0]
    assert "resolved_at" not in rows[0]


def test_context_text_preserves_pending_and_unobserved_delivery_state():
    payload = oracle_attention.context_text(_decision())

    assert "still pending" in payload
    assert "sent_to_oracle_context_not_heard" in payload
    assert '"acknowledgement":"unobserved"' in payload
    assert "push delivery" in payload.lower()
    encoded = json.loads(payload.split(": ", 1)[1])
    assert encoded["decision_id"] == "decision-current"
    assert encoded["status"] == "pending"
    assert "answer" not in encoded


def test_context_text_never_cuts_an_oversized_record_mid_json():
    row = _decision(
        decision_id="d" * 180,
        artifact_id="a" * 180,
        title="t" * 160,
        question="q" * 700,
        context="x" * 400,
        options=[
            {"id": "i" * 80, "label": "l" * 120, "description": "z" * 180}
        ] * 3,
    )
    payload = oracle_attention.context_text(row)
    encoded = json.loads(payload.split(": ", 1)[1])
    assert len(payload.encode("utf-8")) <= 1500
    assert encoded["status"] == "pending"
    assert encoded["decision_id"] == "d" * 180
    assert encoded["options_complete"] is False
    assert encoded["question_excerpt_truncated"] is True
    assert "full pending decision card" in encoded["reference"]


def test_notification_claim_is_durable_per_owner_and_thread():
    first = oracle_memory.open_thread("phone", "primary", connection_id="one")
    assert oracle_attention.claim_notification(first, "decision-current") is True

    resumed = oracle_memory.open_thread(
        "phone", "primary", connection_id="two", thread_id=first.thread_id)
    assert oracle_attention.claim_notification(resumed, "decision-current") is False


def _insert_completion(*, done_ts, source_text="The requested change is complete."):
    backend = "backend-vesper"
    db.conn().executemany(
        """INSERT INTO messages(
               message_id,agent_id,backend_session_id,seq,role,text,tools_json,
               updated_at,origin
           ) VALUES(?,?,?,?,?,?,?,?,?)""",
        [
            ("cause-user", "agent-vesper", backend, -1, "user",
             "Please check the workspace.", "[]", done_ts - 100, "user"),
            ("source-answer", "agent-vesper", backend, 1, "assistant",
             source_text, "[]", done_ts, "user"),
        ],
    )
    db.conn().execute(
        """INSERT INTO user_notifications(
               notification_id,agent_id,session,persona,backend_session_id,trace_id,
               done_ts,source_message_id,cause_message_id,origin,notify,push,badge,
               unread,preview,reason,created_at,updated_at,muted
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        ("notification-answer", "agent-vesper", "vesper", "Vesper", backend,
         "trace-answer", done_ts, "source-answer", "cause-user", "user", 1, 1,
         1, 1, source_text[:180], "text-reply", done_ts, done_ts, 0),
    )


def test_unread_completion_bridge_quotes_exact_source_and_filters_provenance():
    now = db.now_ms()
    _insert_completion(done_ts=now - 1000)
    rows = oracle_attention.pending_completion_notifications(now_ms_value=now)

    assert len(rows) == 1
    assert rows[0]["notification_id"] == "notification-answer"
    assert rows[0]["source_message_id"] == "source-answer"
    assert rows[0]["reference_ts"] == now - 1000
    assert rows[0]["stale"] is False
    assert rows[0]["source_text"] == "The requested change is complete."
    payload = oracle_attention.completion_context_text(rows[0])
    assert "not a formal decision" in payload
    assert "The requested change is complete." in payload


def test_unread_completion_bridge_suppresses_newer_reply_and_stale_rows():
    now = db.now_ms()
    _insert_completion(done_ts=now - 1000)
    db.conn().execute(
        """INSERT INTO messages(
               message_id,agent_id,backend_session_id,seq,role,text,tools_json,
               updated_at,origin
           ) VALUES(?,?,?,?,?,?,?,?,?)""",
        ("newer-user", "agent-vesper", "backend-vesper", 2, "user",
         "Thanks, I have the answer.", "[]", now, "user"),
    )
    assert oracle_attention.pending_completion_notifications(now_ms_value=now) == []

    db.conn().execute("DELETE FROM messages WHERE message_id='newer-user'")
    assert oracle_attention.pending_completion_notifications(
        now_ms_value=now + oracle_attention.ORACLE_NOTIFICATION_MAX_AGE_MS + 1
    ) == []


def test_stable_oracle_notifies_unrelated_agent_decision_once(monkeypatch):
    sent, downstream = [], []
    pending = [_decision()]
    monkeypatch.setattr(oracle_live_stable.oracle_attention, "pending_decisions",
                        lambda: list(pending))
    tools = SimpleNamespace(lock=threading.Lock(), delegations=set(), results=lambda: [])
    conversation = oracle_live_stable.Conversation(
        SimpleNamespace(send=sent.append), downstream.append, tools, "unused",
        clock=lambda: 100.0)
    try:
        conversation.tick()
        conversation.tick()
        events = [json.loads(raw) for raw in sent]
        assert len(events) == 1
        assert events[0]["type"] == "session.thinking.append"
        assert "decision-current" in events[0]["content"]
        assert downstream == []
        assert conversation.context_notifications_sent == {"decision:decision-current"}
    finally:
        conversation.stop.set()
        conversation.pool.shutdown()


def test_stable_oracle_presents_one_decision_per_quiet_opportunity(monkeypatch):
    sent = []
    now = [100.0]
    pending = [_decision(), _decision(decision_id="decision-next", title="Second")]
    monkeypatch.setattr(oracle_live_stable.oracle_attention, "pending_decisions",
                        lambda: list(pending))
    tools = SimpleNamespace(lock=threading.Lock(), delegations=set(), results=lambda: [])
    conversation = oracle_live_stable.Conversation(
        SimpleNamespace(send=sent.append), lambda _event: None, tools, "unused",
        clock=lambda: now[0])
    try:
        conversation.tick()
        now[0] = 101.0
        conversation.tick()
        assert len(sent) == 1
        now[0] = 105.0
        conversation.tick()
        assert len(sent) == 2
        assert "decision-current" in json.loads(sent[0])["content"]
        assert "decision-next" in json.loads(sent[1])["content"]
    finally:
        conversation.stop.set()
        conversation.pool.shutdown()


def test_stable_oracle_bridges_unread_completion_without_decision_semantics(monkeypatch):
    sent = []
    monkeypatch.setattr(oracle_live_stable.oracle_attention, "pending_decisions", lambda: [])
    monkeypatch.setattr(
        oracle_live_stable.oracle_attention,
        "pending_completion_notifications",
        lambda: [{
            "notification_id": "notification-answer",
            "source_message_id": "source-answer",
            "cause_message_id": "cause-user",
            "agent": "Vesper",
            "session": "vesper",
            "origin": "user",
            "reference_ts": 100,
            "stale": False,
            "source_text": "The requested change is complete.",
        }],
    )
    tools = SimpleNamespace(lock=threading.Lock(), delegations=set(), results=lambda: [])
    conversation = oracle_live_stable.Conversation(
        SimpleNamespace(send=sent.append), lambda _event: None, tools, "unused",
        clock=lambda: 100.0)
    try:
        conversation.tick()
        payload = json.loads(sent[0])["content"]
        assert "The requested change is complete." in payload
        assert "not a formal decision" in payload
        assert conversation.context_notifications_sent == {"completion:source-answer"}
    finally:
        conversation.stop.set()
        conversation.pool.shutdown()


def test_stable_oracle_does_not_replay_when_decision_resolves(monkeypatch):
    sent, downstream = [], []
    current = [_decision()]
    monkeypatch.setattr(oracle_live_stable.oracle_attention, "pending_decisions",
                        lambda: list(current))
    tools = SimpleNamespace(lock=threading.Lock(), delegations=set(), results=lambda: [])
    conversation = oracle_live_stable.Conversation(
        SimpleNamespace(send=sent.append), downstream.append, tools, "unused",
        clock=lambda: 100.0)
    try:
        conversation.tick()
        current.clear()
        conversation.tick()
        assert len(sent) == 1
        assert downstream == []
    finally:
        conversation.stop.set()
        conversation.pool.shutdown()


def test_attention_read_failure_does_not_stop_live_oracle(monkeypatch):
    monkeypatch.setattr(
        oracle_live_stable.oracle_attention,
        "pending_decisions",
        lambda: (_ for _ in ()).throw(RuntimeError("database busy")),
    )
    tools = SimpleNamespace(lock=threading.Lock(), delegations=set(), results=lambda: [])
    conversation = oracle_live_stable.Conversation(
        SimpleNamespace(send=lambda _raw: None), lambda _event: None, tools, "unused",
        clock=lambda: 0.0)
    try:
        conversation.tick()
        assert not conversation.stop.is_set()
    finally:
        conversation.stop.set()
        conversation.pool.shutdown()


def test_transcript_reimport_is_not_a_new_user_reply():
    from datetime import datetime, timezone
    now = db.now_ms()
    _insert_completion(done_ts=now-1000)
    old = datetime.fromtimestamp((now-5000)/1000, timezone.utc).isoformat()
    db.conn().execute("UPDATE messages SET timestamp=?,updated_at=? WHERE message_id='cause-user'", (old,now))
    assert len(oracle_attention.pending_completion_notifications(now_ms_value=now)) == 1


def test_notification_cannot_quote_another_conversation_source():
    now=db.now_ms()
    _insert_completion(done_ts=now-1000)
    db.conn().execute("UPDATE messages SET backend_session_id='other-thread' WHERE message_id='source-answer'")
    assert oracle_attention.pending_completion_notifications(now_ms_value=now) == []
