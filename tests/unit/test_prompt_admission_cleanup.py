"""QUEUE-N2: removing an unmaterialized prompt admission (on every queue
delete, inside BEGIN IMMEDIATE) decided 'materialized' with an unindexed
scan of every message, holding the writer lock for seconds on a large Host.
It now looks up the admission's own message by key."""
from __future__ import annotations

from lib import agents, db, message_store, prompt_admissions


def _admission(agent_id, client_id):
    admission = prompt_admissions.create(
        authenticated_at_admission=True, origin="user", sender_agent_id="",
        channel="pwa", observed_at=db.now_ms(), client_admission_id=client_id,
        trace_id=f"t-{client_id}", original_text="synthetic")
    return prompt_admissions.record(admission, agent_id=agent_id, session="mike")


def _exists(admission_id):
    return db.conn().execute("SELECT 1 FROM prompt_admissions WHERE admission_id = ?",
                             (admission_id,)).fetchone() is not None


def test_cleanup_keeps_materialized_admissions_and_never_scans_messages(tmp_path, monkeypatch):
    agent_id = agents.create_agent(persona="Mike", voice_id="v", cwd=str(tmp_path), session="mike")
    kept = _admission(agent_id, "c-kept")
    message_store.record_user_message(
        agent_id=agent_id, backend_session_id="bs-1", client_msg_id="c-kept",
        text="synthetic", origin="user", sender_agent_id=None,
        prompt_admission_id=kept, trace_id="t-c-kept")
    dropped = _admission(agent_id, "c-dropped")

    connection = db.conn()
    plans = []

    class Observed:
        def __getattr__(self, name):
            return getattr(connection, name)

        def execute(self, sql, parameters=()):
            if " ".join(sql.split()).upper().startswith("DELETE FROM PROMPT_ADMISSIONS"):
                plans.extend(row[3] for row in connection.execute(
                    "EXPLAIN QUERY PLAN " + sql, parameters))
            return connection.execute(sql, parameters)

    monkeypatch.setattr(db, "conn", lambda: Observed())
    prompt_admissions.delete_unmaterialized(kept)
    prompt_admissions.delete_unmaterialized(dropped)

    assert _exists(kept) and not _exists(dropped)
    assert plans and not [p for p in plans if p.upper().startswith("SCAN MESSAGES")], plans


def test_every_admission_query_on_messages_finds_its_row_by_key(tmp_path, monkeypatch):
    """QUEUE-N2, widened: the queued-edit update and prompt history also look
    up an admission's own user row by key; none of them scans messages."""
    from test_prompt_history import _admit, _agent as _history_agent, _history
    agent_id = _history_agent(tmp_path)
    first = _admit(agent_id, client_id="u-one", text="one", observed_at=1000, channel="pwa")
    _admit(agent_id, client_id="u-two", text="two", observed_at=2000, channel="pwa")

    connection = db.conn()
    plans = []

    class Observed:
        def __getattr__(self, name):
            return getattr(connection, name)

        def execute(self, sql, parameters=()):
            flat = " ".join(sql.split()).upper()
            if "MESSAGES" in flat and "PROMPT_ADMISSION_ID" in flat:
                plans.append((flat[:60], [row[3] for row in connection.execute(
                    "EXPLAIN QUERY PLAN " + sql, parameters)]))
            return connection.execute(sql, parameters)

    monkeypatch.setattr(db, "conn", lambda: Observed())
    page = _history(limit=1)
    _history(limit=1, before=page["page"]["next_before"])
    prompt_admissions.update_for_queued_edit(first, "one, edited")

    edited = connection.execute("SELECT text FROM messages WHERE prompt_admission_id = ?",
                                (first,)).fetchone()
    assert edited["text"] == "one, edited"
    scanned = [query for query, plan in plans
               if any(step.upper().startswith("SCAN MESSAGES") or step.upper().startswith("SCAN M ")
                      for step in plan)]
    assert len(plans) >= 4 and not scanned, plans
