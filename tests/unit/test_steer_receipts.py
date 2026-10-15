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
