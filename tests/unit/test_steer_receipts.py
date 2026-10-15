"""Steer receipts: the per-message state machine and its durable record.

Design: /var/tmp/dagger/steer-receipts-design.md §2, §3, §11.2, §11.4, §11.8
(§11.8 supersedes §11.2 on terminal-before-ACK). Item evidence on the
expected turn always beats the ACK; a terminal state is never rewritten.
"""
from __future__ import annotations

import pytest

from lib import db, message_writes
from lib import steer_receipts as sr

AGENT = "agent-steer"
SESSION = "native-thread"


def _register(cid, *, gen="g1", turn="turn-1"):
    return sr.register(AGENT, cid, prompt_admission_id=f"adm-{cid}", trace_id=f"tr-{cid}",
                       thread_id=SESSION, expected_turn_id=turn, gen=gen)


def _row(cid):
    return sr.get(AGENT, cid)


def _full_row(cid):
    return tuple(db.conn().execute(
        "SELECT * FROM steer_receipts WHERE agent_id=? AND client_msg_id=?", (AGENT, cid)).fetchone())


# --- the decision (pure) --------------------------------------------------------------

@pytest.mark.parametrize("case,kwargs,expect", [
    ("answered", dict(consumed_seq=5, final_seqs=[9]), ("answered", "")),
    ("reopen-after-final", dict(consumed_seq=5, final_seqs=[3, 9]), ("answered", "")),
    ("earlier-final-only", dict(consumed_seq=5, final_seqs=[3]), ("no_final_response", "")),
    ("commentary-only", dict(consumed_seq=5, final_seqs=[]), ("no_final_response", "")),
    ("unphased-after", dict(consumed_seq=5, final_seqs=[], unphased_seqs=[7]), ("unknown", "no_phase")),
    ("final-beats-unphased", dict(consumed_seq=5, final_seqs=[9], unphased_seqs=[7]), ("answered", "")),
    ("coverage-gap", dict(consumed_seq=5, final_seqs=[], coverage="coverage"), ("unknown", "coverage")),
    ("rebind", dict(consumed_seq=5, final_seqs=[9], coverage="rebind"), ("unknown", "rebind")),
    ("interrupted", dict(consumed_seq=5, final_seqs=[], turn_status="interrupted"), ("interrupted", "")),
    ("failed", dict(consumed_seq=5, final_seqs=[], turn_status="failed"), ("failed", "")),
    ("unseen-ack-ok", dict(consumed_seq=None, ack="ok"), ("unknown", "accepted_not_observed")),
    ("unseen-ack-pending", dict(consumed_seq=None, ack="pending"), ("awaiting_ack", "")),
    ("unseen-ack-mismatch", dict(consumed_seq=None, ack="mismatch"), ("unknown", "ack_turn_mismatch")),
    ("unseen-ack-error", dict(consumed_seq=None, ack="error"), ("rejected", "")),
    ("unseen-ack-timeout", dict(consumed_seq=None, ack="timeout"), ("unknown", "ack_timeout")),
    ("unseen-no-client-id", dict(consumed_seq=None, ack="ok", no_client_id=True), ("unknown", "no_client_id")),
    ("unseen-acked-interrupted", dict(consumed_seq=None, ack="ok", turn_status="interrupted"), ("interrupted", "")),
])
def test_decide(case, kwargs, expect):
    args = dict(final_seqs=[], unphased_seqs=[], turn_status="completed", coverage="", ack="ok",
                no_client_id=False)
    args.update(kwargs)
    assert sr.decide(**args) == expect


# --- durable receipts: the §11.8 truth table ----------------------------------------

def test_registration_writes_one_row_before_the_rpc():
    receipt = _register("c1")
    row = _row("c1")
    assert row["state"] == "registered" and row["ack"] == "pending"
    assert row["message_id"] == "u-c1" and row["gen"] == "g1"
    assert row["prompt_admission_id"] == "adm-c1" and row["trace_id"] == "tr-c1"
    assert receipt.client_msg_id == "c1"


def test_a_second_registration_of_the_same_id_is_refused():
    assert _register("dup") is not None
    assert _register("dup") is None                         # non-terminal: no second RPC
    _register_settle("dup2", consumed=True, finals=[9])
    assert _register("dup2") is None                        # terminal: never reopened


def _register_settle(cid, *, consumed=False, finals=(), status="completed", coverage=""):
    receipt = _register(cid)
    if consumed:
        receipt.consume(5)
    receipt.settle_at_terminal(turn_status=status, final_seqs=list(finals), unphased_seqs=[],
                               coverage=coverage, no_client_id=False)
    return receipt


@pytest.mark.parametrize("late_ack", ["ok", "error", "mismatch", "timeout"])
def test_T_a_answered_then_any_late_ack_keeps_answered(late_ack):
    receipt = _register_settle("ta", consumed=True, finals=[9])
    assert _row("ta")["state"] == "answered"
    assert receipt.apply_ack(late_ack) is False             # never a re-dispatch
    row = _row("ta")
    assert (row["state"], row["ack"]) == ("answered", late_ack)


def test_T_b_no_final_response_then_late_ack_error_keeps_it():
    receipt = _register_settle("tb", consumed=True, finals=[3])
    assert _row("tb")["state"] == "no_final_response"
    assert receipt.apply_ack("error") is False
    assert (_row("tb")["state"], _row("tb")["ack"]) == ("no_final_response", "error")


def test_T_c_unseen_awaits_the_ack_then_ok_is_accepted_not_observed():
    receipt = _register_settle("tc")
    assert _row("tc")["state"] == "awaiting_ack"
    assert receipt.apply_ack("ok") is False
    row = _row("tc")
    assert (row["state"], row["reason"], row["ack"]) == ("unknown", "accepted_not_observed", "ok")


def test_T_d_unseen_then_ack_error_is_rejected_and_falls_through():
    receipt = _register_settle("td")
    assert receipt.apply_ack("error") is True               # the existing fall-through runs
    assert (_row("td")["state"], _row("td")["ack"]) == ("rejected", "error")


def test_T_e_unseen_then_ack_mismatch_is_unknown_without_fall_through():
    receipt = _register_settle("te")
    assert receipt.apply_ack("mismatch") is False
    assert (_row("te")["state"], _row("te")["reason"]) == ("unknown", "ack_turn_mismatch")


def test_T_f_unseen_then_ack_timeout_is_unknown_without_fall_through():
    """Solu §11.11, fixed: a timeout never proves rejection; no fall-through."""
    receipt = _register_settle("tf")
    assert receipt.apply_ack("timeout") is False
    assert (_row("tf")["state"], _row("tf")["reason"]) == ("unknown", "ack_timeout")
    assert not hasattr(sr, "STEER_TIMEOUT_FALLS_THROUGH")      # no production switch


def test_reader_loss_while_awaiting_the_ack_is_unknown_coverage():
    receipt = _register_settle("rl")
    assert _row("rl")["state"] == "awaiting_ack"
    assert receipt.apply_ack("reader_end") is False
    assert (_row("rl")["state"], _row("rl")["reason"]) == ("unknown", "coverage")


def test_an_ack_before_the_terminal_event():
    ok = _register("pre-ok")
    assert ok.apply_ack("ok") is False and _row("pre-ok")["state"] == "acked"
    error = _register("pre-err")
    assert error.apply_ack("error") is True and _row("pre-err")["state"] == "rejected"
    mismatch = _register("pre-mm")
    assert mismatch.apply_ack("mismatch") is False
    assert (_row("pre-mm")["state"], _row("pre-mm")["reason"]) == ("unknown", "ack_turn_mismatch")
    timeout = _register("pre-to")
    assert timeout.apply_ack("timeout") is False
    assert (_row("pre-to")["state"], _row("pre-to")["reason"]) == ("unknown", "ack_timeout")


def test_item_before_ack_consumption_wins_over_a_later_error():
    receipt = _register("ib")
    receipt.consume(4)
    assert receipt.apply_ack("error") is False              # Codex has the text: no fall-through
    row = _row("ib")
    assert (row["state"], row["ack"], row["consumed_seq"]) == ("consumed", "error", 4)
    receipt.settle_at_terminal(turn_status="completed", final_seqs=[8], unphased_seqs=[],
                               coverage="", no_client_id=False)
    assert _row("ib")["state"] == "answered"


def test_the_first_consumption_wins():
    receipt = _register("first")
    receipt.consume(4)
    receipt.consume(9)
    assert _row("first")["consumed_seq"] == 4


def test_T_g_a_new_generation_settles_open_receipts_and_leaves_terminal_ones():
    _register("open-reg")
    acked = _register("open-acked"); acked.apply_ack("ok")
    consumed = _register("open-consumed"); consumed.consume(3)
    _register_settle("open-await")                           # awaiting_ack
    _register_settle("done", consumed=True, finals=[9])
    before = _full_row("done")
    assert sr.sweep(current_gen="g2") == 4
    for cid in ("open-reg", "open-acked", "open-consumed", "open-await"):
        assert (_row(cid)["state"], _row(cid)["reason"]) == ("unknown", "coverage")
    assert _full_row("done") == before                      # terminal: byte-identical
    assert sr.sweep(current_gen="g2") == 0


def test_T_h_replayed_events_change_nothing():
    receipt = _register_settle("th", consumed=True, finals=[9])
    receipt.apply_ack("ok")
    before = _full_row("th")
    changes = db.conn().total_changes
    receipt.settle_at_terminal(turn_status="completed", final_seqs=[], unphased_seqs=[],
                               coverage="", no_client_id=False)
    receipt.apply_ack("error")
    receipt.consume(1)
    assert _full_row("th") == before
    assert db.conn().total_changes == changes                 # conditional writes: no-ops


def test_at_most_four_writes_per_steer():
    changes = db.conn().total_changes
    receipt = _register("w4")                                 # 1
    receipt.apply_ack("ok")                                   # 2
    receipt.consume(3)                                        # 3
    receipt.settle_at_terminal(turn_status="completed", final_seqs=[5], unphased_seqs=[],
                               coverage="", no_client_id=False)   # 4
    assert db.conn().total_changes - changes <= 4


# --- storage: schema 118, lifecycle, Host readers -------------------------------------

def test_schema_118_has_the_table_and_its_indexes():
    con = db.conn()
    assert con.execute("PRAGMA user_version").fetchone()[0] == 118
    names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE tbl_name='steer_receipts'")}
    assert {"steer_receipts", "idx_steer_receipts_open", "idx_steer_receipts_message"} <= names
    columns = [r[1] for r in con.execute("PRAGMA table_info(steer_receipts)")]
    assert "text" not in columns                              # metadata only


def test_older_code_reads_a_v118_database_and_118_migrates_from_117(tmp_path, monkeypatch):
    from lib import db_migrations, db_schema
    path = tmp_path / "state.sqlite"
    db.reset_for_tests(path)
    assert db.conn().execute("PRAGMA user_version").fetchone()[0] == 118
    db.conn().execute("INSERT INTO agents(agent_id, persona, voice_id, cwd, session, created_at)"
                      " VALUES ('a', 'a', 'v', '/', 's', 1)")
    db.conn().commit()
    monkeypatch.setattr(db_schema, "_SCHEMA_VERSION", 117)   # older code opens the newer file
    db.reset_for_tests(path)
    message_writes.record_user_message(agent_id="a", backend_session_id="b", client_msg_id="m",
                                       text="hi")
    assert db.conn().execute("PRAGMA user_version").fetchone()[0] == 118
    monkeypatch.undo()
    old = tmp_path / "old.sqlite"
    db.reset_for_tests(old)
    c = db.conn()
    c.execute("DROP TRIGGER steer_receipts_follow_message")
    c.execute("DROP TABLE steer_receipts")
    c.execute("PRAGMA user_version = 117")
    c.commit()
    db.reset_for_tests(old)
    c = db.conn()
    assert c.execute("PRAGMA user_version").fetchone()[0] == 118
    assert c.execute("SELECT count(*) FROM steer_receipts").fetchone()[0] == 0
    assert db_migrations._migrate_to_v118(c) is None         # idempotent


def test_a_receipt_goes_with_its_message():
    message_writes.record_user_message(agent_id=AGENT, backend_session_id=SESSION,
                                       client_msg_id="gone", text="x")
    _register_settle("gone", consumed=True, finals=[3])
    db.conn().execute("DELETE FROM messages WHERE message_id='u-gone'")
    assert _row("gone") is None


def test_responses_name_only_the_two_marked_states():
    _register_settle("r-answered", consumed=True, finals=[9])
    _register_settle("r-nofinal", consumed=True, finals=[3])
    _register_settle("r-unknown", consumed=True, finals=[], coverage="coverage")
    _register_settle("r-await")
    got = sr.responses_for(["u-r-answered", "u-r-nofinal", "u-r-unknown", "u-r-await", "u-none"])
    assert got == {
        "u-r-nofinal": {"state": "no_final_response", "reason": "", "turn_id": "turn-1"},
        "u-r-unknown": {"state": "unknown", "reason": "coverage", "turn_id": "turn-1"},
    }


def test_readers_tolerate_a_missing_table():
    db.conn().execute("DROP TRIGGER steer_receipts_follow_message")
    db.conn().execute("DROP TABLE steer_receipts")
    assert sr.responses_for(["u-x"]) == {}
    assert sr.receipt_for_message("u-x") is None


# --- §11.12 rulings ----------------------------------------------------------------------

@pytest.mark.parametrize("result,expected,outcome", [
    ({"turnId": "turn-1"}, "turn-1", "ok"),
    ({}, "turn-1", "ok"),                       # older codex: no turnId, still evidence-driven
    ({"turnId": ""}, "turn-1", "ok"),
    ({"turnId": "turn-9"}, "turn-1", "mismatch"),
])
def test_ack_outcome_from_the_steer_reply(result, expected, outcome):
    assert sr.ack_outcome(result, expected) == outcome


def test_the_steer_context_is_scoped_and_never_leaks():
    assert sr.current_context() == {"prompt_admission_id": "", "trace_id": ""}
    with sr.steer_context(prompt_admission_id="adm-1", trace_id="tr-1"):
        assert sr.current_context() == {"prompt_admission_id": "adm-1", "trace_id": "tr-1"}
    assert sr.current_context() == {"prompt_admission_id": "", "trace_id": ""}
    with pytest.raises(RuntimeError):
        with sr.steer_context(prompt_admission_id="adm-2", trace_id="tr-2"):
            raise RuntimeError("steer failed")
    assert sr.current_context() == {"prompt_admission_id": "", "trace_id": ""}


def test_older_code_deleting_a_message_on_a_118_database_removes_the_receipt(tmp_path, monkeypatch):
    from lib import db_schema
    path = tmp_path / "state.sqlite"
    db.reset_for_tests(path)
    message_writes.record_user_message(agent_id=AGENT, backend_session_id=SESSION,
                                       client_msg_id="older", text="x")
    _register_settle("older", consumed=True, finals=[3])
    db.conn().commit()
    monkeypatch.setattr(db_schema, "_SCHEMA_VERSION", 117)    # older code opens the file
    db.reset_for_tests(path)
    db.conn().execute("DELETE FROM messages WHERE message_id='u-older'")
    db.conn().commit()
    monkeypatch.undo()
    db.reset_for_tests(path)
    assert _row("older") is None


def test_a_rejected_message_steered_again_gets_a_new_attempt_and_the_old_row_stays():
    """§11.13 Q2: rejection proves the text never reached the turn and the
    fall-through queues it; a later steer of that queued message inserts
    attempt n+1. The rejected row is never rewritten; readers use the latest."""
    receipt = _register("again")
    assert receipt.apply_ack("error") is True
    rejected = tuple(db.conn().execute(
        "SELECT * FROM steer_receipts WHERE client_msg_id='again' AND attempt=1").fetchone())
    fresh = _register("again", turn="turn-2")
    assert fresh is not None and fresh.attempt == 2
    rows = db.conn().execute("SELECT attempt, state FROM steer_receipts"
                             " WHERE client_msg_id='again' ORDER BY attempt").fetchall()
    assert [tuple(r) for r in rows] == [(1, "rejected"), (2, "registered")]
    assert tuple(db.conn().execute(
        "SELECT * FROM steer_receipts WHERE client_msg_id='again' AND attempt=1").fetchone()) == rejected
    row = _row("again")
    assert (row["attempt"], row["state"], row["expected_turn_id"]) == (2, "registered", "turn-2")
    fresh.consume(4)
    fresh.settle_at_terminal(turn_status="completed", final_seqs=[2], unphased_seqs=[],
                             coverage="", no_client_id=False)
    assert sr.responses_for(["u-again"]) == {
        "u-again": {"state": "no_final_response", "reason": "", "turn_id": "turn-2"}}
    assert sr.receipt_for_message("u-again")["attempt"] == 2
    assert _register("again", turn="turn-3") is None          # attempt 2 is not rejected
    # Only the latest attempt's writes count: an old Receipt cannot touch attempt 2.
    receipt.consume(9)
    assert _row("again")["consumed_seq"] == 4


def test_the_primary_key_carries_the_attempt():
    pk = [r[1] for r in sorted(db.conn().execute("PRAGMA table_info(steer_receipts)"),
                               key=lambda r: r[5]) if r[5]]
    assert pk == ["agent_id", "client_msg_id", "attempt"]


def test_the_last_final_seq_is_kept_whatever_the_outcome():
    """§11.13 deviation 5: ordering facts persist even when nothing answered
    the message, so notifications never need updated_at for them."""
    _register_settle("fs", consumed=True, finals=[3])
    row = _row("fs")
    assert (row["state"], row["consumed_seq"], row["final_seq"]) == ("no_final_response", 5, 3)


# --- §11.13 Q3: the runtime-boot sweep ---------------------------------------------------

def test_runtime_boot_settles_every_open_receipt_before_any_dispatch():
    from types import SimpleNamespace
    from lib.runtime_startup import recover_runtime
    for cid in ("boot-reg", "boot-acked", "boot-consumed"):
        message_writes.record_user_message(agent_id=AGENT, backend_session_id=SESSION,
                                           client_msg_id=cid, text=cid)
    _register("boot-reg", gen="dead")
    acked = _register("boot-acked", gen="dead"); acked.apply_ack("ok")
    consumed = _register("boot-consumed", gen="dead"); consumed.consume(3)
    _register_settle("boot-await")
    _register_settle("boot-done", consumed=True, finals=[9])
    done = _full_row("boot-done")
    revisions = {cid: db.conn().execute("SELECT revision FROM messages WHERE message_id=?",
                                        (f"u-{cid}",)).fetchone()[0]
                 for cid in ("boot-reg", "boot-acked", "boot-consumed")}
    seen = {}

    def recover_queued():                   # dispatch starts only after the sweep
        seen.update({cid: _row(cid)["state"] for cid in
                     ("boot-reg", "boot-acked", "boot-consumed", "boot-await")})
        return 0

    result = recover_runtime(SimpleNamespace(stream=None),
                             SimpleNamespace(recover_queued=recover_queued),
                             restore_agents=lambda _ctx: None,
                             mark_interrupted=lambda stream=None: [],
                             reconcile=lambda: 0, repair_receipts=lambda: {})
    assert set(seen.values()) == {"unknown"}
    for cid in ("boot-reg", "boot-acked", "boot-consumed", "boot-await"):
        assert (_row(cid)["state"], _row(cid)["reason"]) == ("unknown", "coverage")
    assert _full_row("boot-done") == done
    assert result["steer_receipts"] == 4
    for cid, before in revisions.items():                     # clients see it without a turn
        after = db.conn().execute("SELECT revision FROM messages WHERE message_id=?",
                                  (f"u-{cid}",)).fetchone()[0]
        assert after > before


# --- §11.13 deviation 5: notification ordering from receipt seq facts ------------------

def _notification_turn(*, late_state: str):
    from lib import agents as agents_db
    aid = agents_db.create_agent(persona="Seq", voice_id="v", cwd="/tmp", session="seq")
    base = db.now_ms() - 20_000
    message_writes.record_user_message(agent_id=aid, backend_session_id="bs", client_msg_id="first",
                                       text="first", trace_id="turn-trace")
    message_writes.record_user_message(agent_id=aid, backend_session_id="bs", client_msg_id="late",
                                       text="late", trace_id="late-trace")
    db.conn().execute("INSERT INTO messages (message_id, agent_id, backend_session_id, seq, role,"
                      " text, tools_json, updated_at, origin, trace_id) VALUES"
                      " ('a-old', ?, 'bs', 1, 'assistant', 'The old answer', '[]', ?, 'user',"
                      " 'turn-trace')", (aid, base + 1000))
    db.conn().execute("UPDATE messages SET updated_at=? WHERE message_id='u-first'", (base,))
    db.conn().execute("UPDATE messages SET updated_at=? WHERE message_id='u-late'", (base + 3000,))
    receipt = sr.register(aid, "late", prompt_admission_id="", trace_id="late-trace",
                          thread_id="bs", expected_turn_id="turn-1", gen="g1")
    if late_state == "consumed-after-the-final":
        receipt.consume(5)
        receipt.settle_at_terminal(turn_status="completed", final_seqs=[3], unphased_seqs=[],
                                   coverage="coverage", no_client_id=False)
    else:                                                      # no seq facts at all
        receipt.apply_ack("ok")
        receipt.settle_at_terminal(turn_status="completed", final_seqs=[], unphased_seqs=[],
                                   coverage="", no_client_id=False)
    db.conn().execute("UPDATE steer_receipts SET registered_at=? WHERE client_msg_id='late'",
                      (base + 3000,))
    return aid, base


def _classify(aid, done_ts):
    from lib import user_notifications
    return user_notifications.classify_completed_turn(
        agent_id=aid, session="seq", persona="Seq", backend_session_id="bs", trace_id="turn-trace",
        done_ts=done_ts, settle_timeout_s=0, wait_for_message=lambda select, _t: select())


def test_seq_facts_disown_an_old_answer_whose_row_was_touched_after_the_steer():
    aid, base = _notification_turn(late_state="consumed-after-the-final")
    assert (_row_for(aid, "late")["state"], _row_for(aid, "late")["reason"]) == ("unknown", "coverage")
    # A later write (finalize, import, edit) bumps the old answer past the steer.
    db.conn().execute("UPDATE messages SET updated_at=? WHERE message_id='a-old'", (base + 5000,))
    note = _classify(aid, base + 6000)
    assert note["source_message_id"] == "a-old"
    assert note["cause_message_id"] == "u-first"              # not the steered row


def test_without_seq_facts_updated_at_still_decides():
    aid, base = _notification_turn(late_state="accepted-not-observed")
    assert _row_for(aid, "late")["reason"] == "accepted_not_observed"
    note = _classify(aid, base + 6000)                         # a-old predates the steer
    assert note["cause_message_id"] == "u-first"


def _row_for(aid, cid):
    return sr.get(aid, cid)


# --- §11.14 B1: a steer racing the reader's settle / fail_all ------------------------------

def _racing_client(monkeypatch):
    """A Codex client with one active turn that already holds one receipt
    (c1). `gate` holds the next steer() after it has fetched the active turn;
    `iterating`/`release` hold the reader inside its receipt loop."""
    import threading
    from types import SimpleNamespace
    from lib import codex_app_server as cas
    from lib.backend.codex import TurnState
    client = object.__new__(cas._Client)
    client.agent_id, client.gen, client.stream = AGENT, "g1", None
    client._lock, client._pending, client._actives = threading.Lock(), {}, {}
    sent = []

    def request(method, params, timeout=30):
        sent.append((method, params.get("clientUserMessageId")))
        return {"turnId": params["expectedTurnId"]}
    client.request = request
    handle = SimpleNamespace(is_alive=lambda: True, _done=threading.Event())
    active = cas._ActiveTurn("turn-1", SESSION, AGENT, "s", "trace",
                             TurnState(live_backend_session_id=SESSION), handle, None, None,
                             None, lambda **_k: 0)
    active.steer_ready.set()
    client._actives[AGENT], client.active = active, active
    monkeypatch.setattr(cas.agents_db, "live_backend_session", lambda _a: SESSION)
    assert client.steer("one", "c1", agent_id=AGENT) is True
    gate, iterating, release = threading.Event(), threading.Event(), threading.Event()

    class _Gated:                      # steer() waits here after fetching `active`
        def wait(self, _timeout=None):
            return gate.wait(5)
    active.steer_ready = _Gated()
    settle = sr.Receipt.settle_at_terminal

    def held_settle(self, **kwargs):
        if self.client_msg_id == "c1":
            iterating.set()
            assert release.wait(5)
        return settle(self, **kwargs)
    monkeypatch.setattr(sr.Receipt, "settle_at_terminal", held_settle)
    return client, active, sent, gate, iterating, release


def _race(client, reader_call, gate, iterating, release):
    import threading
    errors, results = [], []

    def reader():
        try:
            reader_call()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
    steerer = threading.Thread(target=lambda: results.append(
        client.steer("two", "c2", agent_id=AGENT)))
    steerer.start()                                 # parked at the gate, holding `active`
    reading = threading.Thread(target=reader)
    reading.start()
    assert iterating.wait(5)                        # the reader is inside its receipt loop
    gate.set()
    steerer.join(5)
    assert not steerer.is_alive()                   # the steer never waits on the reader
    release.set()
    reading.join(5)
    assert not reading.is_alive()
    return errors, results


def test_a_steer_arriving_while_the_reader_settles_never_breaks_the_reader(monkeypatch):
    client, active, sent, gate, iterating, release = _racing_client(monkeypatch)
    errors, results = _race(client, lambda: client._settle_receipts(active, "completed", coverage=""),
                            gate, iterating, release)
    assert errors == []
    assert results == [False]                       # the turn is over: no steer into it
    assert sr.get(AGENT, "c2") is None              # unregistered, nothing left open
    assert [cid for _m, cid in sent] == ["c1"]      # no RPC for c2
    assert _row("c1")["state"] != "registered"


def test_a_steer_arriving_while_the_reader_fails_all_never_breaks_the_reader(monkeypatch):
    client, active, sent, gate, iterating, release = _racing_client(monkeypatch)
    errors, results = _race(client, lambda: client._fail_all("codex app-server exited"),
                            gate, iterating, release)
    assert errors == []
    assert results == [False]
    assert sr.get(AGENT, "c2") is None
    assert [cid for _m, cid in sent] == ["c1"]
    assert (_row("c1")["state"], _row("c1")["reason"]) == ("unknown", "coverage")
    assert active.handle._done.is_set()             # every active still ends


# --- §11.14 N2, N3 ---------------------------------------------------------------------

def test_a_surfaced_receipt_bumps_the_row_of_a_padded_id():
    from lib import codex_app_server as cas
    message_writes.record_user_message(agent_id=AGENT, backend_session_id=SESSION,
                                       client_msg_id=" padded ", text="x")
    before = db.conn().execute("SELECT revision FROM messages WHERE message_id='u-padded'").fetchone()[0]
    cas._Client._surface_receipt(AGENT, " padded ", "unknown", None, "s")
    after = db.conn().execute("SELECT revision FROM messages WHERE message_id='u-padded'").fetchone()[0]
    assert after > before


@pytest.mark.parametrize("kwargs,expect", [
    (dict(consumed_seq=5, final_seqs=[9]), ("answered", "")),             # never blocks answered
    (dict(consumed_seq=5, final_seqs=[3]), ("unknown", "coverage")),      # only downgrades this
    (dict(consumed_seq=5, final_seqs=[], turn_status="interrupted"), ("interrupted", "")),
    (dict(consumed_seq=5, final_seqs=[], unphased_seqs=[7]), ("unknown", "no_phase")),
    (dict(consumed_seq=None, ack="ok"), ("unknown", "accepted_not_observed")),
    (dict(consumed_seq=None, ack="pending"), ("awaiting_ack", "")),
])
def test_an_empty_binding_only_downgrades_no_final_response(kwargs, expect):
    args = dict(final_seqs=[], unphased_seqs=[], turn_status="completed", coverage="unbound",
                ack="ok", no_client_id=False)
    args.update(kwargs)
    assert sr.decide(**args) == expect


def test_an_agent_unbound_mid_turn_is_reported_as_unbound(monkeypatch):
    from types import SimpleNamespace
    from lib import codex_app_server as cas
    client = object.__new__(cas._Client)
    active = SimpleNamespace(agent_id=AGENT, thread_id=SESSION)
    monkeypatch.setattr(cas.agents_db, "live_backend_session", lambda _a: "")
    assert client._coverage(active) == "unbound"
    monkeypatch.setattr(cas.agents_db, "live_backend_session", lambda _a: "other")
    assert client._coverage(active) == "rebind"
    monkeypatch.setattr(cas.agents_db, "live_backend_session", lambda _a: SESSION)
    assert client._coverage(active) == ""


# --- §11.14 L1: single-process boot sweeps before agents resume ---------------------------

def test_the_single_process_boot_sweeps_steer_receipts_before_resuming_agents(monkeypatch):
    import sys
    from types import SimpleNamespace
    from lib import runtime_startup
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[2] / "server"))
    import server
    order = []
    monkeypatch.setattr(server, "broadcast_boot_version", lambda _ctx: None)
    monkeypatch.setattr(server, "resume_persisted_agents", lambda _ctx: order.append("resume"))
    monkeypatch.setattr(runtime_startup, "_settle_dead_steer_receipts",
                        lambda _stream: order.append("sweep") or 0)

    class _Dispatch:
        def __init__(self, _ctx):
            pass

        def recover_queued(self):
            order.append("queued")
            return 0
    monkeypatch.setattr(server, "TurnDispatchService", _Dispatch)
    server._recover_after_start(SimpleNamespace(runtime_client=None, stream=None), None,
                                restart_recovery=False)
    assert order == ["sweep", "resume", "queued"]


@pytest.mark.parametrize("reader", ["settle", "fail_all"])
def test_the_reader_survives_an_insert_into_the_receipts_it_took(monkeypatch, reader):
    """The review's probe for B1: a steer preempted between loading the
    receipts dict and storing into it lands in the dict the reader is
    iterating. The reader must neither raise nor stop."""
    client, active, _sent, gate, _iterating, release = _racing_client(monkeypatch)
    gate.set()
    release.set()
    taken = active.receipts
    settle = sr.Receipt.settle_at_terminal

    def inserting_settle(self, **kwargs):
        taken["late"] = sr.Receipt(AGENT, "late", expected_turn_id="turn-1")
        return settle(self, **kwargs)
    monkeypatch.setattr(sr.Receipt, "settle_at_terminal", inserting_settle)
    if reader == "settle":
        client._settle_receipts(active, "completed", coverage="")
    else:
        client._fail_all("codex app-server exited")
        assert active.handle._done.is_set()
    assert _row("c1")["state"] != "registered"


# --- §11.15 A: only proven non-acceptance falls through --------------------------------

class _Wire:
    """The app-server's stdin as the Host sees it: records each complete line
    that reached the other side and can answer it through the client's own
    pending table, the way the reader would. Can refuse before any byte
    (closed), fail mid-write or fail the flush after delivery."""

    def __init__(self, client, *, answer=None, fail_write=None, fail_flush=None,
                 closed=False, on_write=None):
        self.client, self.answer = client, answer
        self.fail_write, self.fail_flush = fail_write, fail_flush
        self.closed, self.on_write = closed, on_write
        self.buf, self.sent = "", []

    def write(self, line):
        if self.closed:
            raise ValueError("I/O operation on closed file.")
        if self.on_write:
            self.on_write()
        if self.fail_write:
            self.buf += line[: len(line) // 2]
            raise self.fail_write
        self.buf += line

    def flush(self):
        *lines, self.buf = self.buf.split("\n")
        for raw in lines:
            message = __import__("json").loads(raw)
            self.sent.append(message)
            if self.answer:
                self.answer(self.client, message)
        if self.fail_flush:
            raise self.fail_flush

    def steers(self):
        return [m for m in self.sent if m.get("method") == "turn/steer"]


def _reply(client, message, *, result=None, error=None):
    with client._lock:
        pending = client._pending.pop(message["id"], None)
    if pending:
        done, box = pending
        box.update({"id": message["id"]},
                   **({"error": error} if error is not None else {"result": result or {}}))
        done.set()


def _ok(client, message):
    _reply(client, message, result={"turnId": message["params"]["expectedTurnId"]})


def _wire_client(monkeypatch, *, agent=AGENT, turn="turn-1", client=None, **wire):
    """A Codex client using the real request()/_send() over a _Wire."""
    import threading
    from types import SimpleNamespace
    from lib import codex_app_server as cas
    from lib.backend.codex import TurnState
    if client is None:
        client = object.__new__(cas._Client)
        client.agent_id, client.gen, client.stream = agent, "g1", None
        client._lock, client._write_lock = threading.Lock(), threading.Lock()
        client._pending, client._actives, client._next_id = {}, {}, 1
        client.proc = SimpleNamespace(stdin=_Wire(client, **wire), poll=lambda: None)
        client.active = None
        monkeypatch.setattr(cas.agents_db, "live_backend_session", lambda _a: SESSION)
    handle = SimpleNamespace(is_alive=lambda: True, _done=threading.Event())
    active = cas._ActiveTurn(turn, SESSION, agent, "s", "trace",
                             TurnState(live_backend_session_id=SESSION), handle, None, None,
                             None, lambda **_k: 0)
    active.steer_ready.set()
    client._actives[agent] = active
    client.active = active
    return client, active, client.proc.stdin


def _consume_then(reply):
    """The UserMessage echo is read before the RPC's answer."""
    def answer(client, message):
        active = client._actives[AGENT]
        active.seq += 1
        client._receipt_evidence(active, {"type": "userMessage",
                                          "clientId": message["params"]["clientUserMessageId"]})
        reply(client, message)
    return answer


def test_A1_accepted_then_the_flush_fails_is_unknown_send_failed(monkeypatch):
    client, _active, wire = _wire_client(monkeypatch, answer=_ok, fail_flush=BrokenPipeError(32, "pipe"))
    assert client.steer("aside", "a1", agent_id=AGENT) is True          # no fall-through
    row = _row("a1")
    assert (row["state"], row["reason"], row["ack"]) == ("unknown", "send_failed", "send_failed")
    assert len(wire.steers()) == 1                                       # Codex has it; never replayed
    assert client._pending == {}


@pytest.mark.parametrize("reply", ["timeout-free -32603", "-32600 after consumption"])
def test_A2_a_lost_or_failed_ack_after_consumption_keeps_the_evidence(monkeypatch, reply):
    code = -32603 if reply.startswith("timeout") else -32600
    client, active, wire = _wire_client(monkeypatch, answer=_consume_then(
        lambda c, m: _reply(c, m, error={"code": code, "message": "x"})))
    assert client.steer("aside", "a2", agent_id=AGENT) is True
    active.seq += 1
    client._receipt_evidence(active, {"type": "agentMessage", "phase": "final_answer"})
    client._settle_receipts(active, "completed", coverage="")
    assert _row("a2")["state"] == "answered"
    assert len(wire.steers()) == 1


def test_A3_a_partial_write_is_unknown_send_failed(monkeypatch):
    client, _active, _wire = _wire_client(monkeypatch, fail_write=OSError(5, "EIO"))
    assert client.steer("aside", "a3", agent_id=AGENT) is True
    assert (_row("a3")["state"], _row("a3")["reason"]) == ("unknown", "send_failed")
    assert client._pending == {}


def test_A4_a_closed_stdin_before_any_byte_is_rejected_and_falls_through(monkeypatch):
    client, _active, wire = _wire_client(monkeypatch, closed=True)
    with pytest.raises(RuntimeError):                                    # the fall-through signal
        client.steer("aside", "a4", agent_id=AGENT)
    row = _row("a4")
    assert (row["state"], row["reason"], row["ack"]) == ("rejected", "not_sent", "not_sent")
    assert wire.sent == [] and wire.buf == ""
    assert client._pending == {}


@pytest.mark.parametrize("code", [-32600, -32601, -32602])
def test_A5_a_definitive_json_rpc_refusal_is_rejected_and_falls_through(monkeypatch, code):
    client, _active, _wire = _wire_client(
        monkeypatch, answer=lambda c, m: _reply(c, m, error={"code": code, "message": "no"}))
    with pytest.raises(RuntimeError):
        client.steer("aside", "a5", agent_id=AGENT)
    assert (_row("a5")["state"], _row("a5")["ack"]) == ("rejected", "error")


@pytest.mark.parametrize("code", [-32603, -32001, -32000, 1])
def test_A6_any_other_json_rpc_error_is_unknown_without_fall_through(monkeypatch, code):
    client, _active, wire = _wire_client(
        monkeypatch, answer=lambda c, m: _reply(c, m, error={"code": code, "message": "failed to steer turn"}))
    assert client.steer("aside", "a6", agent_id=AGENT) is True
    row = _row("a6")
    assert (row["state"], row["reason"], row["ack"]) == ("unknown", "rpc_error_ambiguous",
                                                         "rpc_error_ambiguous")
    assert len(wire.steers()) == 1


@pytest.mark.parametrize("failure", ["uncoded error reply", "generic RuntimeError"])
def test_A7_an_uncoded_or_generic_error_is_unknown_without_fall_through(monkeypatch, failure):
    if failure == "uncoded error reply":
        client, _active, _wire = _wire_client(
            monkeypatch, answer=lambda c, m: _reply(c, m, error={"message": "odd"}))
    else:
        client, _active, _wire = _wire_client(monkeypatch, fail_flush=RuntimeError("odd"))
    assert client.steer("aside", "a7", agent_id=AGENT) is True
    assert _row("a7")["state"] == "unknown"
    assert _row("a7")["reason"] in ("rpc_error_ambiguous", "send_failed")
    assert client._pending == {}


def test_A8_reader_loss_while_awaiting_the_ack_is_unknown_coverage(monkeypatch):
    client, _active, _wire = _wire_client(
        monkeypatch, answer=lambda c, _m: c._fail_all("codex app-server exited"))
    assert client.steer("aside", "a8", agent_id=AGENT) is True
    assert (_row("a8")["state"], _row("a8")["reason"]) == ("unknown", "coverage")


@pytest.mark.parametrize("consumed", [True, False])
def test_A9_a_stop_racing_an_outstanding_steer_keeps_the_evidence(monkeypatch, consumed):
    def stop_then_fail(client, message):
        active = client._actives[AGENT]
        if consumed:
            active.seq += 1
            client._receipt_evidence(active, {"type": "userMessage",
                                              "clientId": message["params"]["clientUserMessageId"]})
        client._settle_receipts(active, "interrupted", coverage="")      # the Stop lands first
        _reply(client, message, error={"code": -32603, "message": "failed to steer turn"})
    client, _active, wire = _wire_client(monkeypatch, answer=stop_then_fail)
    assert client.steer("aside", "a9", agent_id=AGENT) is True          # never re-dispatched
    row = _row("a9")
    if consumed:
        assert row["state"] == "interrupted"
    else:
        assert (row["state"], row["reason"]) == ("unknown", "rpc_error_ambiguous")
    assert len(wire.steers()) == 1


# --- §11.15 B: no SQLite I/O under the module lock ---------------------------------------

def test_B1prime_a_blocked_register_never_stalls_another_turns_settle(monkeypatch):
    import sqlite3
    import threading
    import time
    from lib import codex_app_server as cas
    client, active_a, _wire = _wire_client(monkeypatch, answer=_ok)
    _client, active_b, _ = _wire_client(monkeypatch, agent="agent-other", client=client)
    entering = threading.Event()
    register = sr.register

    def slow_register(*args, **kwargs):
        entering.set()
        return register(*args, **kwargs)
    monkeypatch.setattr(sr, "register", slow_register)
    holder = sqlite3.connect(str(db.DB_PATH), isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")                         # another writer holds the DB
    steering = threading.Thread(target=lambda: _swallow(lambda: client.steer(
        "aside", "b1", agent_id=AGENT)))
    try:
        steering.start()
        assert entering.wait(5)
        started = time.monotonic()
        settling = threading.Thread(target=client._settle_receipts, args=(active_b, "completed"),
                                    kwargs={"coverage": ""})
        settling.start()
        settling.join(1.0)
        elapsed = time.monotonic() - started
        assert not settling.is_alive(), "the reader waited on a DB write behind the module lock"
        assert elapsed < 1.0                                    # busy_timeout is 5 s
        assert not cas._RECEIPTS_LOCK.locked()
    finally:
        holder.execute("ROLLBACK")
        holder.close()
        steering.join(10)
        settling.join(10)
    assert active_b.receipts_closed


def _swallow(call):
    try:
        call()
    except Exception:  # noqa: BLE001
        pass


def _close_during_register(monkeypatch, client, active, event):
    """Run the reader's `event` on its own thread while steer() registers."""
    import threading
    register = sr.register
    ran = []

    def racing_register(*args, **kwargs):
        receipt = register(*args, **kwargs)
        reader = threading.Thread(target=event)
        reader.start()
        reader.join(2)
        ran.append(not reader.is_alive())
        return receipt
    monkeypatch.setattr(sr, "register", racing_register)
    return ran


def test_B2prime_a_turn_closed_during_registration_is_rejected_before_send(monkeypatch):
    client, active, wire = _wire_client(monkeypatch, answer=_ok)
    ran = _close_during_register(monkeypatch, client, active,
                                 lambda: client._settle_receipts(active, "completed", coverage=""))
    assert client.steer("aside", "b2", agent_id=AGENT) is False          # normal dispatch runs
    assert ran == [True]                                                 # the reader never waited
    row = _row("b2")
    assert (row["attempt"], row["state"], row["reason"]) == (1, "rejected", "turn_closed_before_send")
    assert wire.steers() == []                                           # no RPC
    assert "b2" not in active.receipts
    monkeypatch.undo()
    monkeypatch.setattr(__import__("lib.codex_app_server", fromlist=["x"]).agents_db,
                        "live_backend_session", lambda _a: SESSION)
    client2, _active2, _ = _wire_client(monkeypatch, turn="turn-2", client=client)
    assert client.steer("aside", "b2", agent_id=AGENT) is True
    assert (_row("b2")["attempt"], _row("b2")["expected_turn_id"]) == (2, "turn-2")
    assert len(wire.steers()) == 1


@pytest.mark.parametrize("point", ["register", "before_send", "outstanding"])
@pytest.mark.parametrize("event", ["completed", "interrupted", "reader_loss"])
def test_B3prime_register_settle_reader_loss_and_stop_interleave_safely(monkeypatch, point, event):
    import threading
    holder = {}

    def reader_event():
        client, active = holder["client"], holder["active"]
        if event == "reader_loss":
            client._fail_all("codex app-server exited")
        else:
            client._settle_receipts(active, event, coverage="")

    def in_thread():
        t = threading.Thread(target=reader_event)
        t.start()
        t.join(5)
        assert not t.is_alive()

    answer = _ok
    on_write = None
    if point == "before_send":
        on_write = lambda: in_thread() if not holder.get("fired") and not holder.update(fired=1) else None  # noqa: E731
    if point == "outstanding":
        def answer(client, message):
            in_thread()
            _ok(client, message)
    client, active, wire = _wire_client(monkeypatch, answer=answer, on_write=on_write)
    holder.update(client=client, active=active)
    if point == "register":
        _close_during_register(monkeypatch, client, active, reader_event)
    try:
        client.steer("aside", "b3", agent_id=AGENT)
    except RuntimeError as exc:
        assert "dictionary changed size" not in str(exc)
    assert len(wire.steers()) <= 1                                       # never a duplicate RPC
    if point == "register":
        assert wire.steers() == []
    row = _row("b3")
    assert row["state"] not in sr.NON_TERMINAL                           # no orphan open row
    assert db.conn().execute(
        "SELECT count(*) FROM steer_receipts WHERE state IN"
        " ('registered','acked','consumed','awaiting_ack')").fetchone()[0] == 0
