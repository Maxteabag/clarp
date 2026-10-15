"""Steer receipts end to end: real Host dispatch, the QA Codex app-server.

Design /var/tmp/dagger/steer-receipts-design.md §8 rows 1-13 and §11.8 T-a..T-i.
The fake mirrors codex 0.159.2: steered input becomes a UserMessage item with
its clientId, drained before the final answer (answered) or left over after it
(recorded, unanswered). Its steer modes are opt-in per test (provider/steer-mode).
"""
from __future__ import annotations

import json
import sqlite3
import time
import urllib.error

import pytest

from tests.integration.test_qa_turns import QAHost


@pytest.fixture
def host(tmp_path):
    h = QAHost(tmp_path / "steer-host")
    try:
        yield h.start()
    finally:
        h.stop()


def mode(host, value):
    provider = host.root / "provider"
    provider.mkdir(parents=True, exist_ok=True)
    (provider / "steer-mode").write_text(value)


def send(host, text, cid, **extra):
    return host.request("/send", {"session": "rachel", "text": text, "client_msg_id": cid,
                                  "synthesize_audio": False, **extra})


def sql(host, query, *args):
    with sqlite3.connect(host.root / "state.sqlite") as con:
        con.row_factory = sqlite3.Row
        return [dict(r) for r in con.execute(query, args).fetchall()]


def receipt(host, cid):
    try:
        rows = sql(host, "SELECT * FROM steer_receipts WHERE client_msg_id=?", cid)
    except sqlite3.OperationalError:
        return None
    return rows[0] if rows else None


TERMINAL = {"answered", "no_final_response", "interrupted", "failed", "rejected", "unknown"}


def wait_receipt(host, cid, timeout=45.0):
    deadline = time.monotonic() + timeout
    row = None
    while time.monotonic() < deadline:
        row = receipt(host, cid)
        if row and row["state"] in TERMINAL:
            return row
        time.sleep(0.1)
    raise AssertionError(f"receipt {cid} not settled: {row}")


def provider_requests(host, method):
    path = host.root / "provider" / "turn-requests.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()
            if json.loads(line)["method"] == method]


def wait_idle(host, timeout=20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = host.request("/status?session=rachel")
        if not status.get("busy"):
            return
        time.sleep(0.1)
    raise AssertionError("rachel stayed busy")


def log_row(host, cid):
    turns = host.request("/log?session=rachel")["turns"]
    return next((t for t in turns if t["id"] == f"u-{cid}"), None)


def notifications(host):
    return sql(host, "SELECT * FROM user_notifications ORDER BY done_ts")


def wait_notification_count(host, count, timeout=20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rows = notifications(host)
        if len(rows) >= count:
            return rows
        time.sleep(0.1)
    raise AssertionError(f"expected {count} notifications: {notifications(host)}")


def late_steer(host, cid="late", text="Late note"):
    """A steer accepted after the final answer is visible, before turn/completed."""
    send(host, "First request", "first")
    host.wait_reply("rachel", "QA reply: First request")
    send(host, text, cid)
    return wait_receipt(host, cid)


# --- §8 rows ---------------------------------------------------------------------------

def test_row_1_late_steer_is_no_final_response(host):
    mode(host, "late")
    row = late_steer(host)
    assert (row["state"], row["reason"]) == ("no_final_response", "")
    wait_idle(host)
    marked = log_row(host, "late")
    assert marked["response"] == {"state": "no_final_response", "reason": "",
                                  "turn_id": row["expected_turn_id"]}
    assert len(provider_requests(host, "turn/start")) == 1            # no resend, no extra turn
    [note] = wait_notification_count(host, 1)
    assert note["cause_message_id"] != "u-late"                        # not the unanswered row
    assert note["cause_message_id"] in ("u-first", "")


def test_row_2_mid_turn_steer_is_answered(host):
    mode(host, "mid")
    send(host, "[qa-slow] typed work", "slow")
    time.sleep(0.5)
    send(host, "Spoken aside", "aside")
    row = wait_receipt(host, "aside")
    assert row["state"] == "answered"
    wait_idle(host)
    assert "response" not in log_row(host, "aside")


def test_row_3_reopen_after_final_is_answered(host):
    mode(host, "reopen")
    row = late_steer(host, "reopen")
    assert row["state"] == "answered"
    wait_idle(host)
    assert "response" not in log_row(host, "reopen")


def test_row_4_commentary_only_is_no_final_response(host):
    mode(host, "commentary-only")
    row = late_steer(host, "comment")
    assert (row["state"], row["reason"]) == ("no_final_response", "")


def test_row_5_two_steers_then_one_final_answer_both(host):
    mode(host, "mid")
    send(host, "[qa-slow] typed work", "slow")
    time.sleep(0.4)
    send(host, "Aside one", "one")
    send(host, "Aside two", "two")
    assert wait_receipt(host, "one")["state"] == "answered"
    assert wait_receipt(host, "two")["state"] == "answered"


def test_row_6_stop_with_an_acked_steer_is_interrupted(host):
    send(host, "[qa-slow] typed work", "slow")
    time.sleep(0.4)
    send(host, "Aside before stop", "stopme")
    deadline = time.monotonic() + 10
    while receipt(host, "stopme") is None or receipt(host, "stopme")["ack"] == "pending":
        assert time.monotonic() < deadline
        time.sleep(0.05)
    host.request("/stop", {"session": "rachel", "reason": "test stop"})
    row = wait_receipt(host, "stopme")
    assert row["state"] == "interrupted"
    time.sleep(1.0)
    assert len(provider_requests(host, "turn/start")) == 1            # no re-dispatch


def test_row_7_item_before_ack_records_the_consumption(host):
    mode(host, "item-before-ack,mid")
    send(host, "[qa-slow] typed work", "slow")
    time.sleep(0.4)
    send(host, "Early item", "early")
    row = wait_receipt(host, "early")
    assert row["state"] == "answered" and row["consumed_seq"] is not None and row["ack"] == "ok"


def test_row_8_no_client_id_is_unknown(host):
    mode(host, "no-client-id,mid")
    send(host, "[qa-slow] typed work", "slow")
    time.sleep(0.4)
    send(host, "Anonymous aside", "anon")
    row = wait_receipt(host, "anon")
    assert (row["state"], row["reason"]) == ("unknown", "no_client_id")


def test_row_9_app_server_restart_mid_turn_is_unknown_coverage(host):
    mode(host, "restart")
    row = late_steer(host, "restart")
    assert (row["state"], row["reason"]) == ("unknown", "coverage")


def test_row_10_rejected_steer_falls_through_to_a_normal_turn(host):
    mode(host, "ack-error")
    send(host, "[qa-slow] typed work", "slow")
    time.sleep(0.4)
    send(host, "Bounced aside", "bounced")
    row = wait_receipt(host, "bounced")
    assert (row["state"], row["ack"]) == ("rejected", "error")
    host.wait_reply("rachel", "QA reply: Bounced aside")             # delivered as its own turn
    starts = provider_requests(host, "turn/start")
    said = ["".join(i["text"] for i in s["input"]) for s in starts]
    assert len(starts) == 2 and "typed work" in said[0] and "Bounced aside" in said[1]


def test_row_11_native_rebind_mid_turn_is_unknown(host):
    mode(host, "late,hold-after-steer=1.5")
    send(host, "First request", "first")
    host.wait_reply("rachel", "QA reply: First request")
    send(host, "Rebound note", "rebound")
    time.sleep(0.4)
    with sqlite3.connect(host.root / "state.sqlite") as con:
        con.execute("UPDATE agent_runtime SET backend_session_id='rebound-thread'"
                    " WHERE agent_id=(SELECT agent_id FROM agents WHERE session='rachel')")
    row = wait_receipt(host, "rebound")
    assert (row["state"], row["reason"]) == ("unknown", "rebind")


def test_row_12_a_host_without_receipt_rows_behaves_as_today(host):
    mode(host, "late")
    late_steer(host)
    wait_idle(host)
    with sqlite3.connect(host.root / "state.sqlite") as con:          # an old runtime: no rows
        con.execute("DROP TRIGGER IF EXISTS steer_receipts_follow_message")
        con.execute("DROP TABLE steer_receipts")
    assert "response" not in log_row(host, "late")
    mode(host, "")
    send(host, "After the drop", "after")
    host.wait_reply("rachel", "QA reply: After the drop")
    rows = wait_notification_count(host, 2)
    assert rows[-1]["cause_message_id"] == "u-after"


# --- §11.8 truth table, end to end ----------------------------------------------------

@pytest.mark.parametrize("late_ack", ["ok", "error", "mismatch"])
def test_T_a_answered_then_a_late_ack(host, late_ack):
    mode(host, f"mid,ack-after-terminal={late_ack},consume")
    send(host, "[qa-slow] typed work", "slow")
    time.sleep(0.4)
    send(host, "Aside", "ta")
    row = wait_receipt(host, "ta")
    assert row["state"] == "answered"
    deadline = time.monotonic() + 10
    while receipt(host, "ta")["ack"] == "pending":
        assert time.monotonic() < deadline
        time.sleep(0.05)
    assert receipt(host, "ta")["state"] == "answered"
    assert receipt(host, "ta")["ack"] == late_ack
    time.sleep(0.5)
    assert len(provider_requests(host, "turn/start")) == 1


def test_T_b_no_final_response_then_a_late_ack_error(host):
    mode(host, "late,ack-after-terminal=error,consume")
    row = late_steer(host, "tb")
    assert row["state"] == "no_final_response"
    deadline = time.monotonic() + 10
    while receipt(host, "tb")["ack"] == "pending":
        assert time.monotonic() < deadline
        time.sleep(0.05)
    assert receipt(host, "tb")["state"] == "no_final_response"
    time.sleep(0.5)
    assert len(provider_requests(host, "turn/start")) == 1             # no re-dispatch


def test_T_c_unseen_then_ack_ok(host):
    mode(host, "ack-after-terminal=ok")
    row = late_steer(host, "tc")
    assert (row["state"], row["reason"]) == ("unknown", "accepted_not_observed")
    assert len(provider_requests(host, "turn/start")) == 1


def test_T_d_unseen_then_ack_error_queues_exactly_one_turn(host):
    mode(host, "ack-after-terminal=error")
    row = late_steer(host, "td")
    assert row["state"] == "rejected"
    host.wait_reply("rachel", "QA reply: Late note")
    starts = provider_requests(host, "turn/start")
    assert len(starts) == 2
    assert "Late note" in "".join(i["text"] for i in starts[1]["input"])


def test_T_e_unseen_then_ack_mismatch(host):
    mode(host, "ack-after-terminal=mismatch")
    row = late_steer(host, "te")
    assert (row["state"], row["reason"]) == ("unknown", "ack_turn_mismatch")
    time.sleep(0.5)
    assert len(provider_requests(host, "turn/start")) == 1


def test_T_f_unseen_then_no_ack_times_out_without_fall_through(host):
    mode(host, "ack-after-terminal=never")
    row = late_steer(host, "tf")
    assert (row["state"], row["reason"]) == ("unknown", "ack_timeout")
    time.sleep(0.5)
    assert len(provider_requests(host, "turn/start")) == 1


def test_T_g_restart_settles_open_receipts_and_keeps_terminal_ones(host):
    mode(host, "late")
    late_steer(host, "earlier")
    wait_idle(host)
    before = receipt(host, "earlier")
    mode(host, "restart")
    send(host, "Second request", "second")
    host.wait_reply("rachel", "QA reply: Second request")
    send(host, "Lost note", "lost")
    row = wait_receipt(host, "lost")
    assert (row["state"], row["reason"]) == ("unknown", "coverage")
    assert receipt(host, "earlier") == before


# --- §11.11 pinned scenarios (Solu, fixed) -------------------------------------------

def test_accepted_but_ack_lost_keeps_the_evidence_and_never_resends(host):
    mode(host, "mid,ack-after-terminal=never,consume")
    send(host, "[qa-slow] typed work", "slow")
    time.sleep(0.4)
    send(host, "Lost ack", "lostack")
    row = wait_receipt(host, "lostack")
    assert row["state"] == "answered"                                  # consumed + final
    deadline = time.monotonic() + 45
    while receipt(host, "lostack")["ack"] == "pending":                # the 30 s RPC timeout
        assert time.monotonic() < deadline
        time.sleep(0.2)
    assert (receipt(host, "lostack")["state"], receipt(host, "lostack")["ack"]) == ("answered", "timeout")
    assert len(provider_requests(host, "turn/start")) == 1


def test_reader_loss_while_awaiting_the_ack_is_unknown_without_fall_through(host):
    mode(host, "ack-after-terminal=exit")
    row = late_steer(host, "readerloss")
    assert (row["state"], row["reason"]) == ("unknown", "coverage")
    time.sleep(1.0)
    assert len(provider_requests(host, "turn/start")) == 1


def test_stop_racing_an_outstanding_steer_rpc(host):
    mode(host, "ack-delay=2")
    send(host, "[qa-slow] typed work", "slow")
    time.sleep(0.4)
    send(host, "Racing aside", "race")
    deadline = time.monotonic() + 5
    while receipt(host, "race") is None:
        assert time.monotonic() < deadline
        time.sleep(0.02)
    assert receipt(host, "race")["ack"] == "pending"                  # the RPC is outstanding
    host.request("/stop", {"session": "rachel", "reason": "racing stop"})
    row = wait_receipt(host, "race")
    assert row["state"] in {"interrupted", "unknown"}
    time.sleep(2.5)                                                    # the late answer arrives
    assert receipt(host, "race")["state"] == row["state"]
    assert len(provider_requests(host, "turn/start")) == 1             # no re-dispatch
    queue = host.request("/turn-queue?session=rachel")
    assert queue["paused"] is True                                     # the stop-hold is intact


# --- T-i: no per-turn state outlives the turn (in process) ----------------------------

def test_T_i_no_turn_structures_remain(tmp_path, monkeypatch):
    import pathlib
    import threading
    from lib import agents, codex_app_server
    from lib import steer_receipts as sr
    fake = pathlib.Path(__file__).resolve().parents[1] / "qa" / "fake_codex.py"
    provider = tmp_path / "provider"
    provider.mkdir()
    monkeypatch.setenv("CLARP_QA_PROVIDER_ROOT", str(provider))
    monkeypatch.setattr(codex_app_server._backend(), "required_binary", str(fake))
    agents.create_agent(persona="Rachel", voice_id="v", cwd=str(tmp_path), session="rachel",
                        backend="codex")
    agent = agents.get_by_session("rachel")
    agents.start_runtime(agent["agent_id"], "rachel")
    try:
        for outcome in ("ok", "error", "mismatch"):
            (provider / "steer-mode").write_text(f"ack-after-terminal={outcome}")
            done = threading.Event()
            bound = []
            handle = codex_app_server.spawn_turn(
                text="[qa-slow] hold", cwd=tmp_path, session="rachel", agent_id=agent["agent_id"],
                on_session_init=lambda t: bound.append(t) or None,
                on_result=lambda _r: done.set(), on_error=lambda _e: done.set(),
                trace_id=f"tr-{outcome}")
            assert handle is not None
            cid = f"ti-{outcome}"
            with sr.steer_context(prompt_admission_id="", trace_id=f"st-{outcome}"):
                try:
                    codex_app_server.steer(agent["agent_id"], "aside", client_msg_id=cid)
                except RuntimeError:
                    pass                                   # a definitive rejection re-raises
            assert done.wait(15)
            state = sr.get(agent["agent_id"], cid)["state"]
            assert state in {"unknown", "rejected"}
            client = codex_app_server._shared_client()
            assert getattr(client, "_actives", {}) == {} and client.active is None
    finally:
        codex_app_server.recycle_clients()
