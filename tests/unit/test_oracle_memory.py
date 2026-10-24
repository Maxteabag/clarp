import json

import pytest

from lib import agents, db, oracle_calls, oracle_delegations, oracle_memory as memory


def test_context_resumes_across_connections_and_old_writer_cannot_overwrite():
    first = memory.open_thread("phone", "main", connection_id="socket-1")
    text = "Do not deploy. " + "Preserve this constraint. " * 450 + "Use build-42, not build-24."
    first.save({"revision": 4, "fragments": [{"role": "user", "text": text, "end_ms": 9000}]})
    resumed = memory.open_thread("phone", "main", connection_id="socket-2")
    assert resumed.thread_id == first.thread_id
    assert resumed.load()["fragments"][0]["text"] == text
    with pytest.raises(ValueError, match="Stale"):
        first.save({"revision": 1, "fragments": []})
    assert resumed.load()["revision"] == 4


def test_owner_contact_and_explicit_fresh_conversation_are_separate():
    first = memory.open_thread("phone", "main", connection_id="one")
    with pytest.raises(ValueError, match="unavailable"):
        memory.open_thread("other", "main", connection_id="other", thread_id=first.thread_id)
    with pytest.raises(ValueError, match="contact changed"):
        memory.open_thread("phone", "other", connection_id="other", thread_id=first.thread_id)
    fresh = memory.open_thread("phone", "main", connection_id="new", fresh=True)
    assert fresh.thread_id != first.thread_id


def test_observation_retries_do_not_duplicate_transcripts():
    store = memory.open_thread("phone", "main", connection_id="one")
    event = {"type": "session.input_transcript.delta", "delta": "Keep the same job running."}
    assert store.observe(event, "provider:event-1")
    assert not store.observe(event, "provider:event-1")
    assert db.conn().execute("SELECT count(*) FROM oracle_observations").fetchone()[0] == 1


def test_lost_admission_receipt_recovers_the_same_real_work_identity(tmp_path):
    agent = agents.create_agent(persona="Mira", voice_id="fixture", session="mira", cwd=str(tmp_path))
    store = memory.open_thread("phone", "mira", connection_id="one")
    args = {"agent": "mira", "request": "Audit build-42"}
    intent = store.admission(7, 0, "delegate_to_agent", args)
    ident = oracle_calls.operation_id("phone", intent["call_id"])
    oracle_delegations.begin(delegation_id=ident, trace_id="trace", client_msg_id="message", agent_id=agent,
                             session="mira", request_text=args["request"], owner_principal="phone")
    # Simulate a process exiting after durable dispatch but before returning its receipt.
    recovered = memory.open_thread("phone", "mira", connection_id="two")
    recovered.reconcile()
    assert [row["delegation_id"] for row in recovered.work()] == [ident]
    assert recovered.admission(7, 0, "delegate_to_agent", args)["status"] == "completed"
    with pytest.raises(ValueError, match="conflicting action"):
        recovered.admission(7, 0, "delegate_to_agent", {**args, "request": "Deploy build-42"})


def test_typed_context_is_exact_idempotent_and_retired_without_mutating_history():
    store = memory.open_thread("phone", "main", connection_id="one")
    text = "invoice_Ångström-42.csv; amount=550, not50"
    assert store.add_context("context-1", text=text)
    assert not store.add_context("context-1", text=text)
    with pytest.raises(ValueError, match="different content"):
        store.add_context("context-1", text="Deploy now")
    assert store.contexts()[0]["text"] == text
    assert store.remove_context("context-1")
    assert store.contexts() == []
    assert db.conn().execute("SELECT text FROM oracle_user_context").fetchone()[0] == text


def test_startup_is_a_bounded_excerpt_not_a_silent_cut_inside_a_constraint():
    store = memory.open_thread("phone", "main", connection_id="one")
    large = "Boundary: " + "evidence " * 3000 + "Never deploy."
    store.save({"revision": 2, "fragments": [{"role": "user", "text": large}, {"role": "user", "text": "Continue the audit only."}]})
    history = store.startup_history()
    text = history[0]["content"][0]["text"]
    assert len(text.encode()) < 8192
    assert "Continue the audit only." in text
    assert "Boundary:" not in text
    assert "history_is_excerpt" in text
    assert store.load()["fragments"][0]["text"] == large


def test_malformed_image_cannot_enter_context():
    store = memory.open_thread("phone", "main", connection_id="one")
    with pytest.raises(ValueError):
        store.add_context("bad-image", text="", image=b"not an image", mime_type="image/jpeg")
    assert store.contexts() == []


def test_migration_from_previous_version_keeps_podcast_history():
    con = db.conn()
    for table in ("oracle_user_context", "oracle_admissions", "oracle_thread_work", "oracle_observations", "oracle_threads"):
        con.execute("DROP TABLE " + table)
    con.execute("PRAGMA user_version=83")
    db._migrate(con)
    assert con.execute("PRAGMA user_version").fetchone()[0] == db._SCHEMA_VERSION
    assert memory.open_thread("phone", "main", connection_id="one").load()["revision"] == 0
    assert con.execute("SELECT count(*) FROM podcast_conversations").fetchone()[0] == 0


def test_current_v88_database_gets_context_notification_dedup_table():
    con = db.conn()
    con.execute("DROP TABLE oracle_context_notifications")
    con.execute("PRAGMA user_version=88")
    db._migrate(con)
    assert con.execute("PRAGMA user_version").fetchone()[0] == db._SCHEMA_VERSION
    columns = {row[1] for row in con.execute(
        "PRAGMA table_info(oracle_context_notifications)")}
    assert {"thread_id", "owner_principal", "source_kind", "source_id",
            "reference_ts", "stale", "sent_at"} <= columns


DAY = 86_400_000
NOW = 1_792_152_000_000  # 2026-10-16T12:00:00Z


def _payload(store, **kwargs):
    text = store.startup_history(roster={"agents": []}, **kwargs)[0]["content"][0]["text"]
    return json.loads(text.split("\n", 1)[1])


def _epoch_ms(iso):
    from datetime import datetime
    moment = datetime.fromisoformat(iso)
    assert moment.tzinfo is not None
    return int(moment.timestamp() * 1000)


@pytest.mark.parametrize("delta,phrase", [
    (0, "less than a minute ago"), (59_999, "less than a minute ago"), (60_000, "1 minute ago"),
    (59 * 60_000, "59 minutes ago"), (2 * 3_600_000 + 5, "2 hours ago"), (DAY, "1 day ago"),
    (13 * DAY + 3_600_000, "13 days ago"), (-5_000, "less than a minute ago")])
def test_age_phrase_is_deterministic_for_an_injected_clock(delta, phrase):
    assert memory.age_phrase(NOW - delta, NOW) == phrase


def test_startup_history_states_now_and_when_each_dated_item_happened(tmp_path):
    agent = agents.create_agent(persona="Jax", voice_id="fixture", session="jax", cwd=str(tmp_path))
    store = memory.open_thread("phone", "jax", connection_id="one")
    oracle_delegations.begin(delegation_id="work-1", trace_id="t1", client_msg_id="m1", agent_id=agent,
                             session="jax", request_text="Check the invoice", owner_principal="phone")
    db.conn().execute("UPDATE oracle_delegations SET updated_at=? WHERE delegation_id='work-1'", (NOW - 3 * DAY,))
    store.link_work("work-1")
    store.save({"revision": 3, "fragments": [
        {"role": "user", "text": "Legacy line without a time."},
        {"role": "assistant", "text": "Dated line.", "at": NOW - 2 * 3_600_000}]})
    payload = _payload(store, now_ms=NOW)
    assert _epoch_ms(payload["now"]) == NOW
    legacy, dated = payload["recent_conversation"]
    assert "at" not in legacy and "age" not in legacy
    assert _epoch_ms(dated["at"]) == NOW - 2 * 3_600_000 and dated["age"] == "2 hours ago"
    [work] = payload["work"]
    assert _epoch_ms(work["updated_at"]) == NOW - 3 * DAY and work["age"] == "3 days ago"


def test_dated_history_still_respects_the_startup_budget():
    store = memory.open_thread("phone", "main", connection_id="one")
    store.save({"revision": 1, "fragments": [
        {"role": "user", "text": "line %d " % index + "words " * 60, "at": NOW - (29 - index) * DAY} for index in range(30)]})
    payload = _payload(store, now_ms=NOW)
    del payload["history_is_excerpt"]  # added after the budgeted records
    assert len(memory._json(payload).encode()) <= 6500
    kept = [row["text"].split(" words")[0] for row in payload["recent_conversation"]]
    assert kept[-1] == "line 29" and "line 0" not in kept
    assert payload["recent_conversation"][-1]["age"] == "less than a minute ago"


def test_lab_promise_from_an_old_call_is_replayed_with_its_real_age():
    """Shape of the 2026-10-16 call: a promise made 14 days earlier, in a past call."""
    store = memory.open_thread("phone", "main", connection_id="one")
    promise = "Jeg følger med og gir deg beskjed med en gang jeg får svar fra Jax."
    store.save({"revision": 9, "fragments": [
        {"role": "user", "text": "Spør Jax om fakturaen.", "provider_session": "call-10-02", "end_ms": 4100,
         "revision": 9, "at": NOW - 14 * DAY - 9_000},
        {"role": "assistant", "text": promise, "provider_session": "call-10-02", "end_ms": 6900,
         "revision": 9, "at": NOW - 14 * DAY}]})
    payload = _payload(store, now_ms=NOW)
    replayed = next(row for row in payload["recent_conversation"] if row["text"] == promise)
    assert replayed["age"] == "14 days ago"
    assert _epoch_ms(replayed["at"]) == NOW - 14 * DAY and _epoch_ms(payload["now"]) == NOW
