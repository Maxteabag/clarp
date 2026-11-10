"""Characterization of ``message_writes._store_transcript_turns_txn``.

Refactor slice S5 will split this 508-line loop into client provenance,
authority, twin resolution, upsert and cleanup (read-model review §3 step 4).
These tests pin what each branch does today, through the public entry
``message_store.store_transcript_turns`` with hand-built parsed turns, so the
split can be proven a pure move. Several pin known faults on purpose
(RM-04 non-atomic cleanup, RM-05 side effects on every row with an empty
trace); those tests say so, and a fix-lane change to them is an intended
difference to register, not a refactor.
"""
from __future__ import annotations

from tests.patching import patch_shared

import builtins
from contextlib import closing
import json
import os
import sqlite3

import pytest

from lib import (db, dreaming, heartbeat, message_store, message_writes,
                 oracle_delegations, team_leader, team_store)
from lib.heartbeat import HEARTBEAT_OK, HEARTBEAT_PROMPT
from lib.message_context import CALL_OPENING_OPEN
from lib.team_leader import LEADER_NOOP, TICK_PROMPT

AGENT = "agent-char"
SENDER = "agent-sender"
SESSION = "bs-char"
SOURCE = "/redacted/transcript.jsonl"
BASE_MS = 1_791_706_800_000          # 2026-10-11T08:20:00Z


def iso(seconds: float) -> str:
    import datetime as dt
    stamp = dt.datetime.fromtimestamp(BASE_MS / 1000 + seconds, tz=dt.timezone.utc)
    return stamp.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def u(text, at, **extra):
    return {"role": "user", "text": text, "tools": [], "timestamp": iso(at), **extra}


def a(text, at, tools=None, **extra):
    return {"role": "assistant", "text": text, "tools": tools or [], "timestamp": iso(at), **extra}


def bash(call_id, status="ok"):
    return {"name": "Bash", "id": call_id, "status": status, "input": {"command": "ls"}}


class Clock:
    def __init__(self):
        self.ms = BASE_MS

    def __call__(self):
        return self.ms


@pytest.fixture
def clock(tmp_path, monkeypatch):
    db.reset_for_tests(tmp_path / "state.sqlite")
    c = db.conn()
    for agent in (AGENT, SENDER):
        c.execute("INSERT INTO agents(agent_id, persona, voice_id, cwd, session, created_at, backend)"
                  " VALUES (?,?,?,?,?,?,'claude')", (agent, agent, "v", "/redacted", agent, 1))
    c.commit()
    clock = Clock()
    patch_shared(monkeypatch, message_writes, "now_ms", clock)
    monkeypatch.setattr(message_store, "IMPORT_COMMIT_EVERY", 0)
    monkeypatch.setattr(message_store, "IMPORT_WRITE_BUDGET_SECONDS", 1e9)
    monkeypatch.setattr(message_store, "IMPORT_WRITER_YIELD_SECONDS", 0)
    return clock


@pytest.fixture
def calls(monkeypatch):
    """The cross-area side effects the import makes, recorded (and still run)."""
    seen: dict[str, list] = {}

    def spy(module, name, key=None):
        real = getattr(module, name)

        def wrapper(*args, **kwargs):
            seen.setdefault(key or name, []).append((args, kwargs))
            return real(*args, **kwargs)
        monkeypatch.setattr(module, name, wrapper)

    spy(team_store, "capture_assistant_message")
    spy(oracle_delegations, "complete_for_trace")
    spy(team_leader, "record_leader_noop")
    spy(heartbeat, "record_heartbeat_noop_once")
    spy(heartbeat, "record_heartbeat_activity_once")
    spy(dreaming, "process_assistant_text")
    return seen


def client(client_id, text, *, at, origin="user", sender=None, trace=""):
    clock_ms = BASE_MS + int(at * 1000)
    real = message_writes.now_ms
    message_writes.now_ms = lambda: clock_ms
    try:
        message_store.record_user_message(
            agent_id=AGENT, backend_session_id=SESSION, client_msg_id=client_id, text=text,
            origin=origin, sender_agent_id=sender, trace_id=trace)
    finally:
        message_writes.now_ms = real


def turn_row(trace, *, at, outcome="completed", settled=True):
    start = BASE_MS + int(at * 1000)
    db.conn().execute(
        """INSERT INTO turns(agent_id, source, trace_id, started_at, ended_at, settled_at, outcome)
           VALUES (?, 'pwa', ?, ?, ?, ?, ?)""",
        (AGENT, trace, start, start + 6000, start + 6000 if settled else None,
         outcome if settled else None))
    db.conn().commit()


def assistant_row(message_id, source, text, *, at, trace=None, seq=-1000):
    c = db.conn()
    c.execute("""INSERT INTO messages(message_id, agent_id, backend_session_id, source_file, seq,
                 role, timestamp, text, updated_at, revision, trace_id)
                 VALUES (?, ?, ?, ?, ?, 'assistant', ?, ?, 1, ?, ?)""",
              (message_id, AGENT, SESSION, source, seq, iso(at), text,
               message_writes._next_revision(c), trace))
    c.commit()


def store(turns, source=SOURCE):
    return message_store.store_transcript_turns(
        agent_id=AGENT, backend_session_id=SESSION, source_file=source, turns=turns)


def rows():
    return [dict(r) for r in db.conn().execute(
        """SELECT message_id, source_file, seq, role, text, origin, sender_agent_id,
                  trace_id, phase, tools_json, turn_json, revision
             FROM messages WHERE agent_id = ? ORDER BY seq, message_id""", (AGENT,))]


def transcript_rows():
    return [r for r in rows() if r["source_file"] == SOURCE]


def head():
    r = db.conn().execute("SELECT revision, replace_revision FROM conversation_heads "
                          "WHERE agent_id = ? AND backend_session_id = ?", (AGENT, SESSION)).fetchone()
    return tuple(r) if r else None


def view(rs, *names):
    return [tuple(r[n] for n in names) for r in rs]


# ---- client provenance ------------------------------------------------------

def test_user_turns_link_to_client_rows_by_text_in_send_order(clock):
    client("c0", "continue", at=0)
    client("c3", "continue", at=60, origin="agent", sender=SENDER, trace="tr-3")
    store([u("continue", 0.2), a("first", 5), u("continue", 60.2), a("second", 65)])
    assert view(rows(), "source_file", "seq", "role", "text", "origin", "sender_agent_id", "trace_id") == [
        ("client:c3", -2, "user", "continue", "agent", SENDER, "tr-3"),
        ("client:c0", -1, "user", "continue", "user", None, None),
        (SOURCE, 1, "assistant", "first", "user", "", None),
        (SOURCE, 3, "assistant", "second", "agent", SENDER, "tr-3"),
    ]


def test_an_unmatched_user_turn_is_imported_and_its_reply_reads_user(clock):
    client("c0", "something else", at=0, origin="agent", sender=SENDER)
    store([u("typed in the terminal", 1), a("ok", 2)])
    assert view(transcript_rows(), "seq", "role", "origin", "sender_agent_id") == [
        (0, "user", "user", ""), (1, "assistant", "user", "")]


def test_a_retry_copy_of_the_request_is_not_imported(clock):
    client("c0", "go", at=0)
    store([u("go", 0.2), a("Usage limit", 1, failed=True), u("go", 30), a("done", 31)])
    assert view(transcript_rows(), "seq", "role", "text") == [
        (1, "assistant", "Usage limit"), (3, "assistant", "done")]


def test_a_later_client_row_with_the_same_text_is_its_own_request_not_a_retry(clock):
    client("c0", "go", at=0)
    client("c1", "go", at=30, origin="agent", sender=SENDER)
    store([u("go", 0.2), a("Usage limit", 1, failed=True), u("go", 30.2), a("done", 31)])
    assert view(transcript_rows(), "seq", "role", "origin", "sender_agent_id") == [
        (1, "assistant", "user", ""), (3, "assistant", "agent", SENDER)]


def test_a_client_linked_turn_clears_what_an_earlier_import_left_in_its_slot(clock):
    store([u("go", 0.2), a("done", 1)])
    before = head()
    client("c0", "go", at=0)
    store([u("go", 0.2), a("done", 1)])
    assert view(transcript_rows(), "seq", "role") == [(1, "assistant")]
    assert head()[1] > before[1]       # the removal bumps replace_revision


# ---- automation prompts and their replies --------------------------------

def test_heartbeat_turns_read_heartbeat_and_account_once_per_prompt(clock, calls):
    store([u(HEARTBEAT_PROMPT, 0), a(HEARTBEAT_OK, 1),
           u(HEARTBEAT_PROMPT, 60), a("Advanced the plan.", 61)])
    assert view(transcript_rows(), "seq", "role", "text", "origin") == [
        (0, "user", HEARTBEAT_PROMPT, "heartbeat"),
        (1, "assistant", "Heartbeat check: no action needed.", "heartbeat"),
        (2, "user", HEARTBEAT_PROMPT, "heartbeat"),
        (3, "assistant", "Advanced the plan.", "heartbeat")]
    ids = [r["message_id"] for r in transcript_rows()]
    assert [c[0] for c in calls["record_heartbeat_noop_once"]] == [(AGENT, ids[0])]
    assert [c[0] for c in calls["record_heartbeat_activity_once"]] == [(AGENT, ids[2])]
    store([u(HEARTBEAT_PROMPT, 0), a(HEARTBEAT_OK, 1),
           u(HEARTBEAT_PROMPT, 60), a("Advanced the plan.", 61)])
    assert len(calls["record_heartbeat_noop_once"]) == 1      # unchanged rows account nothing


def test_leader_ticks_and_dream_prompts_set_the_origin(clock, calls):
    store([u(TICK_PROMPT, 0), a(LEADER_NOOP, 1),
           u("Deep Dreaming contract: redacted", 60), a("DREAMING_OK", 61),
           u("plain", 120), a("reply", 121)])
    assert view(transcript_rows(), "role", "text", "origin") == [
        ("user", TICK_PROMPT, "leader_tick"),
        ("assistant", "Leader check: no action needed.", "leader_tick"),
        ("user", "Deep Dreaming contract: redacted", "dreaming"),
        ("assistant", "Dreaming check: no action needed.", "dreaming"),
        ("user", "plain", "user"), ("assistant", "reply", "user")]
    assert len(calls["record_leader_noop"]) == 1
    # A fresh conversation: the probe stops at the first user row, so only
    # the locked pass reaches the replies, and it records (live=False).
    assert [c[1]["live"] for c in calls["process_assistant_text"]] == [False, False, False]


def test_the_probe_pass_transforms_replies_without_recording_them(clock, calls):
    turns = [u("plain", 0), a("one", 1), u("more", 2), a("two", 3)]
    store(turns)
    calls["process_assistant_text"].clear()
    store(turns + [u("again", 4), a("three", 5)])
    assert [c[1]["live"] for c in calls["process_assistant_text"]] == [True, True, False, False, False]


def test_every_import_that_writes_records_the_leader_noop_again(clock, calls):
    """RM-05 as it stands: the locked pass records a historical noop on
    every import that writes anything, changed row or not."""
    turns = [u(TICK_PROMPT, 0), a(LEADER_NOOP, 1)]
    store(turns)
    store(turns + [u("next", 60), a("ok", 61)])
    store(turns + [u("next", 60), a("ok", 61)])          # unchanged: probe only
    assert len(calls["record_leader_noop"]) == 2


def test_a_call_opening_input_is_never_a_user_row_and_clears_its_slot(clock):
    store([u("hello", 0), a("hi", 1)])
    before = head()
    store([u(f"{CALL_OPENING_OPEN}redacted", 0), a("hi", 1)])
    assert view(transcript_rows(), "seq", "role") == [(1, "assistant")]
    assert head()[1] > before[1]


def test_a_turn_with_its_own_id_keeps_it(clock):
    store([u("ask", 0, id="native-u"), a("reply", 1, id="native-a")])
    assert view(transcript_rows(), "message_id", "seq") == [("native-u", 0), ("native-a", 1)]


# ---- twins: finalized and live rows ----------------------------------------

def test_a_reply_adopts_its_requests_final_row_when_exactly_one_matches(clock):
    client("c0", "ask", at=0, trace="tr-0")
    assistant_row("final-0", "final:tr-0", "answer <speak>answer</speak>", at=4, trace="tr-0")
    store([u("ask", 0.2), a("answer <speak>answer</speak>", 5)])
    assert view(rows(), "message_id", "source_file", "seq") == [
        ("u-c0", "client:c0", -1), ("final-0", SOURCE, 1)]


def test_two_equal_final_rows_of_one_request_are_both_left(clock):
    client("c0", "ask", at=0, trace="tr-0")
    assistant_row("final-a", "final:tr-0", "answer", at=4, trace="tr-0", seq=-1000)
    assistant_row("final-b", "final:tr-0", "answer", at=4, trace="tr-0", seq=-1001)
    store([u("ask", 0.2), a("answer", 5)])
    assert sorted(r["message_id"] for r in rows() if r["source_file"] == "final:tr-0") == [
        "final-a", "final-b"]
    assert len(transcript_rows()) == 1


def test_a_reply_adopts_the_live_row_of_its_own_turn_exact_text_first(clock):
    client("c0", "ask", at=0, trace="tr-0")
    assistant_row("live-prefix", "live:tr-0", "ans", at=3, trace="tr-0", seq=-1000)
    assistant_row("live-exact", "live:tr-0", "answer", at=4, trace="tr-0", seq=-1001)
    store([u("ask", 0.2), a("answer", 5)])
    assert view(rows(), "message_id", "source_file") == [
        ("live-exact", SOURCE), ("u-c0", "client:c0")] or \
        sorted(view(rows(), "message_id", "source_file")) == sorted([
            ("live-exact", SOURCE), ("u-c0", "client:c0")])
    assert "live-prefix" not in {r["message_id"] for r in rows()}   # cleanup removed it


@pytest.mark.parametrize("primary_fault, fallback_fault", [
    ("normal", "none"), ("sink", "none"), ("import", "none"),
    ("sink", "oserror"), ("import", "oserror"),
    ("sink", "short"), ("import", "short"),
])
def test_a_live_row_of_another_turn_is_neither_adopted_nor_removed(
        clock, monkeypatch, capfd, primary_fault, fallback_fault):
    """A failed optional diagnostic cannot change the actual import/twin result.

    Compare every stored state product with the normal path from the same
    seed. The fallback is real fd output; only genuine sink refusal/short
    writes are injected, and neither is evidence of full diagnostic delivery.
    """
    from lib import log as log_module, message_import_rows

    sensitive = "SENSITIVE-DIAGNOSTIC-SENTINEL"
    trace, live_id = f"tr-0-{sensitive}", f"live-other-{sensitive}"
    event = "transcriptLiveTwinOtherTurn"
    detail = f"agent={AGENT} trace={trace} kept={live_id}"
    fallback = b"transcriptDiagnosticFallback code=primary_log_failed count=1\n"
    client("c0", "ask", at=0, trace=trace)
    assistant_row(live_id, "live:tr-9", "answer", at=4, trace="tr-9")
    turns = [u("ask", 0.2), a("answer", 5)]
    mode = {"primary": "normal"}
    diagnoses, primary_calls, fallback_attempts = [], [], []
    real_log, real_import, real_write = log_module.log, builtins.__import__, os.write
    real_diagnose = message_import_rows._diagnose

    def observe_diagnostic(actual_event, actual_detail):
        result = real_diagnose(actual_event, actual_detail)
        diagnoses.append((actual_event, actual_detail, result))
        return result

    def primary(actual_event, actual_detail=""):
        if actual_event == event:
            primary_calls.append((actual_event, actual_detail))
            if mode["primary"] == "sink":
                raise RuntimeError(sensitive)
        return real_log(actual_event, actual_detail)

    def primary_import(name, globals=None, locals=None, fromlist=(), level=0):
        if (mode["primary"] == "import" and name == "log" and level == 1
                and "log" in fromlist
                and (globals or {}).get("__name__") == "lib.message_import_rows"):
            raise ImportError(sensitive)
        return real_import(name, globals, locals, fromlist, level)

    def fallback_write(fd, payload):
        if fd == 2 and payload.startswith(b"transcriptDiagnosticFallback"):
            fallback_attempts.append((fd, payload))
            if fallback_fault == "oserror":
                raise OSError(sensitive)
            if fallback_fault == "short":
                return real_write(fd, payload[:5])
        return real_write(fd, payload)

    patch_shared(monkeypatch, message_writes, "_diagnose", observe_diagnostic)
    monkeypatch.setattr(log_module, "log", primary)
    monkeypatch.setattr(builtins, "__import__", primary_import)
    monkeypatch.setattr(os, "write", fallback_write)
    connection = db.conn()
    with closing(sqlite3.connect(":memory:")) as seed:
        connection.backup(seed)
        store(turns)
        expected_products = tuple(connection.iterdump())
        expected_diagnoses = list(diagnoses)
        assert expected_diagnoses and all(item == (event, detail, None)
                                          for item in expected_diagnoses)
        assert primary_calls == [(event, detail)] * len(expected_diagnoses)
        assert fallback_attempts == []
        capfd.readouterr()

        seed.backup(connection)
        diagnoses.clear()
        primary_calls.clear()
        mode["primary"] = primary_fault
        store(turns)

    assert diagnoses == expected_diagnoses, "no failed diagnostic retries the import"
    assert tuple(connection.iterdump()) == expected_products
    assert not connection.in_transaction
    assert {r["message_id"]: r["source_file"] for r in rows()}[live_id] == "live:tr-9"
    assert len(transcript_rows()) == 1
    stderr = capfd.readouterr().err
    if primary_fault == "normal":
        assert primary_calls == [(event, detail)] * len(expected_diagnoses)
        assert fallback_attempts == []
        assert event in stderr and detail in stderr
    else:
        assert primary_calls == ([] if primary_fault == "import" else
                                 [(event, detail)] * len(expected_diagnoses))
        assert fallback_attempts == [(2, fallback)] * len(expected_diagnoses)
        expected_output = ("" if fallback_fault == "oserror" else
                           fallback[:5].decode() if fallback_fault == "short" else
                           fallback.decode())
        assert stderr == expected_output * len(expected_diagnoses)
        assert sensitive not in stderr and event not in stderr and AGENT not in stderr


def test_a_reply_of_unknown_turn_adopts_one_final_twin_within_ten_minutes(clock):
    assistant_row("final-near", "final:tr-x", "answer", at=300, seq=-1000)
    assistant_row("final-far", "final:tr-y", "other", at=5000, seq=-1001)
    store([u("terminal ask", 0), a("answer", 5), u("again", 6000), a("other", 6001 + 700)])
    sources = {r["message_id"]: r["source_file"] for r in rows()}
    assert sources["final-near"] == SOURCE
    assert sources["final-far"] == "final:tr-y"


def test_a_reply_already_imported_sheds_a_later_final_twin(clock):
    store([u("terminal ask", 0), a("answer", 5)])
    assistant_row("final-late", "final:tr-x", "answer", at=6)
    before = head()
    store([u("terminal ask", 0), a("answer", 5)])
    assert "final-late" not in {r["message_id"] for r in rows()}
    assert head()[1] > before[1]


# ---- the upsert ----------------------------------------------------------

def test_phase_trace_and_turn_summary_on_the_rows_of_a_settled_turn(clock):
    """A text row is commentary when a LATER row of the turn does tool work;
    its own tools do not count. kind (Codex, OpenCode) overrides."""
    turn_row("tr-0", at=0, outcome="interrupted")
    store([u("ask", 0.5), a("looking", 1, tools=[bash("b1", "running")]), a("still", 1.5),
           a("", 1.7, tools=[bash("b2")]), a("done", 2), a("said so", 2.5, kind="commentary")])
    got = transcript_rows()
    assert view(got, "seq", "phase", "trace_id") == [
        (0, None, "tr-0"), (1, "commentary", "tr-0"), (2, "commentary", "tr-0"), (3, None, "tr-0"),
        (4, "final", "tr-0"), (5, "commentary", "tr-0")]
    assert json.loads(got[1]["tools_json"])[0]["status"] == "interrupted"
    assert got[2]["turn_json"] is None
    summary = json.loads(got[5]["turn_json"])
    assert (summary["turn_id"], summary["status"], summary["tool_count"]) == ("tr-0", "interrupted", 2)
    assert [r["turn_json"] is None for r in got] == [True] * 5 + [False]


def test_an_unchanged_import_writes_nothing_and_a_changed_row_gets_a_new_revision(clock):
    turns = [u("ask", 0), a("one", 1)]
    store(turns)
    first = rows()
    clock.ms += 5000
    store(turns)
    assert rows() == first
    store([u("ask", 0), a("one, edited", 1)])
    after = {r["seq"]: r["revision"] for r in transcript_rows()}
    assert after[0] == first[0]["revision"] and after[1] > first[1]["revision"]


def test_the_returned_rows_carry_id_origin_and_revision(clock):
    out = store([u("ask", 0), a("one", 1, kind="final_answer")])
    assert [(r["role"], r["origin"], r["sender_agent_id"], r["kind"] if "kind" in r else None)
            for r in out] == [("user", "user", "", None), ("assistant", "user", "", "final_answer")]
    assert [r["id"] for r in out] == [r["message_id"] for r in transcript_rows()]
    assert [r["revision"] for r in out] == [r["revision"] for r in transcript_rows()]


# ---- cross-area side effects (gap 3) ---------------------------------------

def test_side_effects_run_for_every_assistant_row_of_every_writing_import(clock, calls):
    """RM-05 as it stands: every assistant row, changed or not, with the
    parser's trace_id (never set, so '') rather than the row's trace."""
    client("c0", "ask", at=0, trace="tr-0")
    store([u("ask", 0.2), a("one", 1), a("two", 2)])
    store([u("ask", 0.2), a("one", 1), a("two", 2), u("next", 60), a("three", 61)])
    captured = [c[1] for c in calls["capture_assistant_message"]]
    assert [(c["text"], c["trace_id"]) for c in captured] == [
        ("one", ""), ("two", ""), ("one", ""), ("two", ""), ("three", "")]
    assert [c[1]["trace_id"] for c in calls["complete_for_trace"]] == ["", "", "", "", ""]
    assert {r["trace_id"] for r in transcript_rows() if r["text"] in ("one", "two")} == {"tr-0"}


def test_an_unchanged_import_runs_no_side_effects(clock, calls):
    turns = [u("ask", 0), a("one", 1)]
    store(turns)
    count = len(calls["capture_assistant_message"])
    store(turns)
    assert len(calls["capture_assistant_message"]) == count


# ---- cleanup ---------------------------------------------------------------

def test_a_live_row_goes_when_a_related_reply_of_its_own_turn_lands(clock):
    client("c0", "ask", at=0, trace="tr-0")
    assistant_row("live-0", "live:tr-0", "partial answ", at=3, trace="tr-0", seq=-1000)
    assistant_row("final-0", "final:tr-0", "partial answer, done", at=4, trace="tr-0", seq=-1001)
    store([u("ask", 0.2), a("partial answer, done", 5)])
    ids = {r["message_id"] for r in rows()}
    assert "live-0" not in ids and "final-0" in ids


def test_a_shorter_transcript_removes_the_stale_tail_and_bumps_replace(clock):
    store([u("ask", 0), a("one", 1), u("more", 2), a("two", 3)])
    before = head()
    store([u("ask", 0), a("one", 1)])
    assert view(transcript_rows(), "seq") == [(0,), (1,)]
    assert head()[1] > before[1]


def test_a_shifted_transcript_moves_an_adopted_row_with_its_turn(clock):
    """RM-N1 (fixed; was pinned as a fault): when an earlier record
    disappears, later turns move down one slot. The adopted twin (its id is
    not the slot hash) moves with the turn it answers, as a fresh import of
    the shorter transcript would place it, instead of the next user turn's
    upsert hitting UNIQUE(agent_id, backend_session_id, seq) for ever."""
    client("c0", "ask", at=0, trace="tr-0")
    assistant_row("final-0", "final:tr-0", "answer", at=4, trace="tr-0")
    store([u("hello", -10), a("hi", -9), u("ask", 0.2), a("answer", 5), u("next", 60), a("ok", 61)])
    assert {r["message_id"]: r["seq"] for r in rows()}["final-0"] == 3
    before = head()

    store([u("hello", -10), u("ask", 0.2), a("answer", 5), u("next", 60), a("ok", 61)])

    moved = view(transcript_rows(), "seq", "message_id", "text")
    slot = lambda seq: message_writes._message_id(AGENT, SESSION, SOURCE, seq)
    assert moved == [(0, slot(0), "hello"), (2, "final-0", "answer"),
                     (3, slot(3), "next"), (4, slot(4), "ok")]
    assert head()[1] > before[1], "delta clients reload the moved slots"


def test_two_equal_replies_to_one_request_reimport_without_change(clock):
    """RM-N1 review (rf-readmodel): one request answered twice with the same
    visible text, one reply adopted. Moving adopted rows with a shift must not
    make an unchanged transcript ping-pong that row between the two slots."""
    client("c0", "ask", at=0, trace="tr-0")
    assistant_row("final-0", "final:tr-0", "OK", at=4, trace="tr-0")
    turns = [u("ask", 0.2), a("OK", 3), a("OK", 5)]
    store(turns)
    first = rows()
    clock_revision = db.conn().execute("SELECT revision FROM message_clock").fetchone()[0]
    for _ in range(3):
        store(turns)
        assert rows() == first
        assert db.conn().execute("SELECT revision FROM message_clock").fetchone()[0] == clock_revision


def test_a_shift_up_moves_an_adopted_row_with_its_turn(clock):
    """RM-N1b: a record appears earlier (a parser change un-hides one) and an
    unlinked user turn lands on the slot an adopted reply holds. The adopted
    reply moves to its turn, as a fresh import would place it, instead of the
    user turn's upsert hitting UNIQUE(agent_id, backend_session_id, seq)."""
    client("c0", "ask", at=0, trace="tr-0")
    assistant_row("final-0", "final:tr-0", "answer", at=4, trace="tr-0")
    store([u("ask", 0.2), a("answer", 5)])
    assert {r["message_id"]: r["seq"] for r in rows()}["final-0"] == 1
    before = head()

    store([u("ask", 0.2), u("typed in the terminal", 1), a("answer", 5)])

    slot = lambda seq: message_writes._message_id(AGENT, SESSION, SOURCE, seq)
    assert view(transcript_rows(), "seq", "message_id", "text") == [
        (1, slot(1), "typed in the terminal"), (2, "final-0", "answer")]
    assert head()[1] > before[1], "delta clients reload the moved slots"


def test_a_removed_request_does_not_lend_its_adopted_row_to_the_next(clock, tmp_path):
    """RM-N1c (reviewer C's control; the lead's bar): the first request and its
    reply disappear, so the next reply's turn lands on the slot holding the
    removed request's adopted row. The surviving reply keeps its own adopted
    id, the removed request's reply goes back to its final: row, and the end
    state equals a fresh import of the shortened transcript from the same seed."""
    def seed():
        client("c0", "ask0", at=0, trace="tr-0")
        client("c1", "ask1", at=60, trace="tr-1")
        assistant_row("final-0", "final:tr-0", "answer0", at=4, trace="tr-0", seq=-1000)
        assistant_row("final-1", "final:tr-1", "answer1", at=64, trace="tr-1", seq=-1001)

    def state():
        # Everything a client renders; the restored final row's seq is the
        # one live final rows take, as its seed seq is gone after adoption.
        return [(r["message_id"], r["source_file"], r["seq"] if r["seq"] >= 0 or
                 r["source_file"].startswith("client:") else "final-band",
                 r["role"], r["text"], r["origin"], r["sender_agent_id"],
                 r["trace_id"], r["phase"], r["tools_json"]) for r in rows()]

    seed()
    store([u("ask0", .2), a("answer0", 5), u("ask1", 60.2), a("answer1", 65)])
    assert view(transcript_rows(), "message_id", "seq") == [("final-0", 1), ("final-1", 3)]
    store([u("ask1", 60.2), a("answer1", 65)])
    shifted = state()
    after = rows()
    store([u("ask1", 60.2), a("answer1", 65)])
    assert rows() == after, "re-importing the shortened transcript changes nothing"

    db.reset_for_tests(tmp_path / "fresh.sqlite")
    for agent in (AGENT, SENDER):
        db.conn().execute("INSERT INTO agents(agent_id, persona, voice_id, cwd, session, created_at, backend)"
                          " VALUES (?,?,?,?,?,?,'claude')", (agent, agent, "v", "/redacted", agent, 1))
    db.conn().commit()
    seed()
    store([u("ask1", 60.2), a("answer1", 65)])

    assert shifted == state()
    assert [r[:3] for r in shifted if r[0].startswith("final-")] == [
        ("final-0", "final:tr-0", "final-band"), ("final-1", SOURCE, 1)]


def test_requests_removed_one_import_after_another_all_return_to_the_final_band(clock):
    """RM-N1c review (reviewer C's control): with no turn rows to name a seq,
    each restored final row still gets a seq of its own; the second restore
    never collides with the first."""
    for i in range(3):
        client(f"c{i}", f"ask{i}", at=60 * i, trace=f"tr-{i}")
    for i in range(3):
        assistant_row(f"final-{i}", f"final:tr-{i}", f"answer{i}", at=60 * i + 4,
                      trace=f"tr-{i}", seq=-1000 - i)

    def turns(start):
        out = []
        for i in range(start, 3):
            out.extend([u(f"ask{i}", 60 * i + .2), a(f"answer{i}", 60 * i + 5)])
        return out

    store(turns(0))
    store(turns(1))
    store(turns(2))
    kept = {r["message_id"]: r for r in rows()}
    assert (kept["final-0"]["source_file"], kept["final-1"]["source_file"]) == (
        "final:tr-0", "final:tr-1")
    assert kept["final-0"]["seq"] < 0 and kept["final-1"]["seq"] < 0
    assert kept["final-0"]["seq"] != kept["final-1"]["seq"]
    assert (kept["final-2"]["source_file"], kept["final-2"]["seq"]) == (SOURCE, 1)


# ---- agy turn authority ------------------------------------------------------

def _authority(trace, start, end, message_id):
    db.conn().execute(
        """INSERT INTO agy_turn_authority(agent_id, backend_session_id, trace_id,
               assistant_start_ordinal, assistant_end_ordinal, terminal_status,
               authoritative_message_id, updated_at) VALUES (?, ?, ?, ?, ?, 'done', ?, 1)""",
        (AGENT, SESSION, trace, start, end, message_id))
    db.conn().commit()


def test_a_stream_owned_turn_keeps_its_text_and_gains_the_transcripts_tools(clock):
    assistant_row("final-s", "final:tr-s", "stream text", at=4, trace="tr-s")
    _authority("tr-s", 0, None, "final-s")
    store([u("ask", 0), a("step", 1, tools=[bash("b1")]), a("transcript text", 2, tools=[bash("b2")]),
           u("terminal", 60), a("later", 61, tools=[bash("b3")])])
    final = {r["message_id"]: r for r in rows()}["final-s"]
    assert final["text"] == "stream text"
    assert [t["id"] for t in json.loads(final["tools_json"])] == ["b1", "b2"]
    assert view(transcript_rows(), "seq", "role") == [(0, "user"), (3, "user")]


def test_a_tombstone_authority_hides_the_turn_and_attaches_nothing(clock):
    _authority("tr-s", 0, 1, None)
    store([u("ask", 0), a("hidden", 1, tools=[bash("b1")]), a("shown", 2)])
    assert view(transcript_rows(), "seq", "role", "text") == [(0, "user", "ask"), (2, "assistant", "shown")]


# ---- batches and a failure part-way (gap 4, RM-04) ---------------------------

def test_a_failure_after_the_first_batch_leaves_that_batch_and_skips_cleanup(clock, monkeypatch):
    """RM-04 as it stands: batch 1 is committed with its head, the stale
    tail and the replace_revision bump never happen. Expected to change when
    the plan/apply split lands (register it as an intended difference)."""
    store([u("q0", 0), a("a0", 1), u("q1", 2), a("a1", 3), u("q2", 4), a("a2", 5), u("q3", 6), a("a3", 7)])
    before_head = head()
    monkeypatch.setattr(message_store, "IMPORT_COMMIT_EVERY", 2)
    real = message_writes._next_revision
    count = {"n": 0}

    def flaky(database):
        count["n"] += 1
        if count["n"] == 3:
            raise RuntimeError("injected after batch 1")
        return real(database)
    patch_shared(monkeypatch, message_writes, "_next_revision", flaky)
    with pytest.raises(RuntimeError):
        store([u("q0", 0), a("A0", 1), u("q1", 2), a("A1", 3)])     # shorter and edited
    got = {r["seq"]: r["text"] for r in transcript_rows()}
    assert got == {0: "q0", 1: "A0", 2: "q1", 3: "a1", 4: "q2", 5: "a2", 6: "q3", 7: "a3"}
    assert head()[1] == before_head[1]


# ---- 877: current-text preparation versus protected DB-candidate cleanup ----

@pytest.mark.parametrize("candidate", ["streamed", "final", "existing-slot"])
def test_current_reply_normalization_leaves_the_writer_available(clock, monkeypatch, candidate):
    """Real twin adoption must not re-clean immutable current text under BEGIN.

    Candidate rows deliberately use DIFFERENT raw markup, so the independent
    writer witness cannot mistake protected DB-row cleanup for current input.
    The cleaner and all twin SQL still run; no choice/result is mocked.
    """
    current = "<speak>Prepared λ reply.\nSecond line.</speak>"
    persisted = "Prepared λ reply. Second line."
    turn_row("trace-visible", at=0)
    client("visible-user", "question", at=0, trace="trace-visible")
    if candidate == "streamed":
        assistant_row("kept-visible", "live:trace-visible", persisted,
                      at=1, trace="trace-visible")
    elif candidate == "final":
        assistant_row("kept-visible", "final:trace-visible", persisted,
                      at=1, trace="trace-visible")
    else:
        own_id = message_writes._message_id(AGENT, SESSION, SOURCE, 1)
        assistant_row(own_id, SOURCE, persisted, at=1, trace="trace-visible", seq=1)
        assistant_row("duplicate-final", "final:trace-visible", persisted,
                      at=1, trace="trace-visible")
    connection = db.conn()
    path = connection.execute("PRAGMA database_list").fetchone()[2]
    other = sqlite3.connect(path, isolation_level=None, timeout=0)
    original = message_writes._strip_voice_markup
    admissions = []
    candidate_inputs = []

    def clean(text):
        visible = original(text)
        if text == current:
            try:
                other.execute("BEGIN IMMEDIATE")
                other.execute("INSERT OR REPLACE INTO focus(singleton,agent_id,updated_at) VALUES(0,?,1)",
                              (AGENT,))
                other.execute("COMMIT")
                admissions.append("admitted")
            except sqlite3.OperationalError as exc:
                if other.in_transaction:
                    other.execute("ROLLBACK")
                admissions.append(exc.sqlite_errorname)
        elif text == persisted:
            candidate_inputs.append(connection.in_transaction)
        return visible

    patch_shared(monkeypatch, message_writes, "_strip_voice_markup", clean)
    try:
        out = store([u("question", .1), a(current, 1)])
    finally:
        other.close()
    expected_id = own_id if candidate == "existing-slot" else "kept-visible"
    replies = [r for r in out if r["role"] == "assistant"]
    assert len(replies) == 1 and replies[0]["id"] == expected_id
    assert replies[0]["text"] == current
    assert not db.conn().execute("SELECT 1 FROM messages WHERE message_id='duplicate-final'").fetchone()
    assert candidate_inputs, "fixture must actually normalize a persisted candidate"
    assert admissions and set(admissions) == {"admitted"}, admissions


@pytest.mark.parametrize("prompt,raw,expected,origin", [
    ("ordinary", "<speak>λ\nreply</speak>", "<speak>λ\nreply</speak>", "user"),
    ("ordinary", "", "", "user"),
    ("ordinary", "<vox>um</vox>", "<vox>um</vox>", "user"),
    ("ordinary", "<environment_context>private</environment_context><speak>answer</speak>",
     "<speak>answer</speak>", "user"),
    (HEARTBEAT_PROMPT, HEARTBEAT_OK, "Heartbeat check: no action needed.", "heartbeat"),
    (TICK_PROMPT, LEADER_NOOP, "Leader check: no action needed.", "leader_tick"),
    ("Deep Dreaming contract: redacted", "DREAMING_OK", "Dreaming check: no action needed.", "dreaming"),
    ("ordinary", HEARTBEAT_OK, "Heartbeat check: no action needed.", "user"),
    ("ordinary", LEADER_NOOP, "Leader check: no action needed.", "user"),
    ("ordinary", "DREAMING_OK", "Dreaming check: no action needed.", "user"),
])
def test_prepared_visibility_preserves_effective_text_and_twin_choice(
        clock, prompt, raw, expected, origin):
    """Raw visibility is not the post-automation/hidden-block text contract."""
    visible = message_writes._strip_voice_markup(expected)
    # Plain persisted representation distinguishes candidate from raw input.
    assistant_row("final-effective", "final:unlinked", visible, at=1)
    out = store([u(prompt, 0), a(raw, 1)])
    replies = [r for r in out if r["role"] == "assistant"]
    if expected:
        assert len(replies) == 1
        assert replies[0]["text"] == expected
        assert replies[0]["origin"] == origin
        if visible:
            assert replies[0]["id"] == "final-effective"
    else:
        assert not [r for r in replies if r["text"]]


@pytest.mark.parametrize("current", ["", "<vox>um</vox>"])
def test_empty_prepared_visibility_does_not_repeat_current_cleanup(clock, monkeypatch, current):
    connection = db.conn()
    path = connection.execute("PRAGMA database_list").fetchone()[2]
    other = sqlite3.connect(path, isolation_level=None, timeout=0)
    original = message_writes._strip_voice_markup
    admissions = []

    def clean(text):
        result = original(text)
        if text == current:
            try:
                other.execute("BEGIN IMMEDIATE")
                other.execute("INSERT OR REPLACE INTO focus(singleton,agent_id,updated_at) VALUES(0,?,1)",
                              (AGENT,))
                other.execute("COMMIT")
                admissions.append("admitted")
            except sqlite3.OperationalError as exc:
                if other.in_transaction:
                    other.execute("ROLLBACK")
                admissions.append(exc.sqlite_errorname)
        return result

    patch_shared(monkeypatch, message_writes, "_strip_voice_markup", clean)
    try:
        store([u("question", 0), a(current, 1)])
    finally:
        other.close()
    assert admissions and set(admissions) == {"admitted"}, admissions
    assert [r["text"] for r in transcript_rows() if r["role"] == "assistant"] == [current]


def test_revalidated_automation_origin_uses_effective_reply_visibility(clock, monkeypatch):
    """A current DB origin, not the probe's raw ACK visibility, chooses twins."""
    from lib import message_turns
    client("origin-user", "question", at=0, origin="user")
    users = db.conn().execute(
        "SELECT message_id FROM messages WHERE agent_id=? AND backend_session_id=? AND role='user'",
        (AGENT, SESSION)).fetchall()
    assert len(users) == 1
    user_id = users[0]["message_id"]
    assistant_row("effective-status", "final:unlinked", "Heartbeat check: no action needed.", at=1)
    connection = db.conn()
    path = connection.execute("PRAGMA database_list").fetchone()[2]
    original = message_turns.TurnIndex.__init__
    changed = []

    def change_after_snapshot(self, database, agent_id):
        original(self, database, agent_id)
        if not changed:
            with sqlite3.connect(path, isolation_level=None, timeout=0) as other:
                updated = other.execute("UPDATE messages SET origin='heartbeat' WHERE message_id=?",
                                        (user_id,)).rowcount
                assert updated == 1
            changed.append(True)

    monkeypatch.setattr(message_turns.TurnIndex, "__init__", change_after_snapshot)
    out = store([u("question", .1), a(HEARTBEAT_OK, 1)])
    reply = next(row for row in out if row["role"] == "assistant")
    assert changed == [True]
    assert (reply["id"], reply["origin"], reply["text"]) == (
        "effective-status", "heartbeat", "Heartbeat check: no action needed.")
