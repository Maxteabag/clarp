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
