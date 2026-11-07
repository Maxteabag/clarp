"""RM-N14 harness admission: exact byte workloads and a discriminating oracle.

These controls do not claim an incremental fast path. Until P1 lands, both
isolated lanes run the existing full importer through independent entries.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from lib import backends, db
from tests.readmodel import corpus, world
from tests.readmodel import rm_n14_harness as harness


@pytest.mark.parametrize("backend", harness.BACKENDS)
@pytest.mark.parametrize("long_turn", [False, True])
@pytest.mark.parametrize("size", [1024, 10 * 1024])
def test_fixed_byte_append_retains_provider_tool_and_prompt_shape(tmp_path, backend, long_turn, size):
    path = tmp_path / "synthetic.jsonl"
    parts = [harness.append_bytes(backend, i, size=size, long_turn=long_turn) for i in range(2)]
    assert [len(part) for part in parts] == [size, size]
    path.write_bytes(b"".join(parts))
    parsed = backends.parse_turns(backend, path)
    assert {tool["id"] for row in parsed for tool in row.get("tools", [])} == {"c0", "c1"}
    assert sum(row["role"] == "user" for row in parsed) == (1 if long_turn else 2)


@pytest.mark.parametrize("backend", harness.BACKENDS)
@pytest.mark.parametrize("mebibytes", [1, 10, 17])
def test_history_sizes_share_the_same_fixed_delta(tmp_path, backend, mebibytes):
    path = tmp_path / "synthetic.jsonl"
    next_ordinal = harness.write_history(path, backend, mebibytes=mebibytes)
    before = path.stat().st_size
    delta = harness.append_bytes(backend, next_ordinal, size=1024)
    with path.open("ab") as output:
        output.write(delta)
    assert before == mebibytes * 1024 * 1024
    assert path.stat().st_size - before == 1024


def _clones(tmp_path, backend):
    reference, candidate = tmp_path / "reference.sqlite", tmp_path / "candidate.sqlite"
    world.prepare(reference, backend)
    with sqlite3.connect(candidate) as destination:
        db.conn().backup(destination)
    return reference, candidate


@pytest.mark.parametrize("backend", harness.BACKENDS)
def test_isolated_full_reference_and_candidate_match_after_every_tick(tmp_path, backend):
    reference, candidate = _clones(tmp_path, backend)
    transcript = tmp_path / "synthetic.jsonl"
    transcript.write_bytes(b"")
    for tick in range(2):
        with transcript.open("ab") as output:
            output.write(harness.append_bytes(backend, tick))
        reports = [harness.isolated_lane(path, transcript, backend, lane, tick=tick)
                   for path, lane in ((reference, "reference"), (candidate, "candidate"))]
        harness.assert_product_equal(reference, candidate)
        assert all(report["queries"].get("SELECT", 0) > 0 for report in reports)
        assert all(report["components"]["_row_phases"]["calls"] > 0 for report in reports)
        assert all(report["total_s"] >= report["parse_s"] for report in reports)
        assert all(not report["candidate_fast_path_proven"] for report in reports)


def test_product_oracle_detects_side_effect_change_even_when_row_counts_match(tmp_path):
    reference, candidate = _clones(tmp_path, "claude")
    for path, outcome in ((reference, "noop"), (candidate, "activity")):
        with sqlite3.connect(path) as database:
            database.execute("INSERT INTO heartbeat_accounting VALUES (?,?,?,?)",
                             ("private-canary-key", corpus.AGENT, outcome, 1))
    with pytest.raises(AssertionError) as failure:
        harness.assert_product_equal(reference, candidate)
    report = json.loads(str(failure.value))
    assert report == {"code": "product_state_differs", "tables": [
        {"table": "heartbeat_accounting", "reference_rows": 1, "candidate_rows": 1}]}
    assert "private-canary" not in str(failure.value)


@pytest.mark.parametrize("backend", harness.BACKENDS)
def test_persistent_lanes_retain_distinct_processes_and_compare_each_long_turn_tick(tmp_path, backend):
    reference, candidate = _clones(tmp_path, backend)
    transcript = tmp_path / "synthetic.jsonl"
    transcript.write_bytes(b"")
    with harness.PersistentLane(reference, transcript, backend, "reference") as full:
        with harness.PersistentLane(candidate, transcript, backend, "candidate") as actual:
            assert full.process.pid != actual.process.pid
            for tick in range(3):
                with transcript.open("ab") as output:
                    output.write(harness.append_bytes(backend, tick, long_turn=True))
                a, b = full.tick(tick), actual.tick(tick)
                assert (a["process_id"], b["process_id"]) == (full.process.pid, actual.process.pid)
                harness.assert_product_equal(reference, candidate)
                assert a["writer_hold_s"] >= a["writer_max_hold_s"] >= 0
                assert a["probe_sql_s"] > 0
                assert b["process_peak_rss_kib"] > 0


@pytest.mark.parametrize("backend", harness.BACKENDS)
def test_checkpoint_lane_matches_full_through_partial_completion_and_restart(tmp_path, backend):
    """Persistent metadata is real, and survives a fresh process on the next tick.

    Existing incremental unit tests own the fallback predicates. This lane
    protects the all-product-table oracle across process lifecycle changes.
    """
    reference, candidate = _clones(tmp_path, backend)
    transcript = tmp_path / "synthetic.jsonl"
    initial = b"".join(harness.append_bytes(backend, i) for i in range(3))
    delta = harness.append_bytes(backend, 3)
    transcript.write_bytes(initial)
    with harness.PersistentLane(reference, transcript, backend, "reference") as full:
        with harness.PersistentLane(candidate, transcript, backend, "checkpoint") as actual:
            for tick, content in enumerate((initial, initial + delta[:-40], initial + delta)):
                transcript.write_bytes(content)
                full.tick(tick)
                report = actual.tick(tick)
                assert not report["shadow"].startswith("error")
                harness.assert_product_equal(reference, candidate)
                audit = harness.assert_checkpoint_parse_equal(candidate, transcript, backend)
                assert audit["complete_bytes"] <= len(content)
    # New workers keep the durable state but none of the old Python objects.
    with transcript.open("ab") as output:
        output.write(harness.append_bytes(backend, 4))
    harness.isolated_lane(reference, transcript, backend, "reference", tick=3)
    harness.isolated_lane(candidate, transcript, backend, "checkpoint", tick=3)
    harness.assert_product_equal(reference, candidate)
    harness.assert_checkpoint_parse_equal(candidate, transcript, backend)
    # Same product rows cannot excuse a metadata cursor beyond durable bytes.
    with sqlite3.connect(candidate) as connection:
        connection.execute("UPDATE transcript_import_checkpoints SET complete_size=size+1")
    harness.assert_product_equal(reference, candidate)
    with pytest.raises(AssertionError, match="checkpoint_byte_watermark"):
        harness.assert_checkpoint_parse_equal(candidate, transcript, backend)


@pytest.mark.parametrize("backend", harness.BACKENDS)
def test_tail_apply_matches_full_and_checkpoints_in_the_final_transaction(tmp_path, backend, monkeypatch):
    """P1 actual tail writes preserve every product row and durable progress."""
    from dataclasses import replace
    from lib import message_store, message_writes
    from lib import transcript_incremental as inc

    reference, candidate = _clones(tmp_path, backend)
    transcript = tmp_path / "synthetic.jsonl"
    transcript.write_bytes(b"".join(harness.append_bytes(backend, i) for i in range(3)))
    harness.isolated_lane(reference, transcript, backend, "reference", tick=0)
    harness.isolated_lane(candidate, transcript, backend, "checkpoint", tick=0)
    harness.assert_product_equal(reference, candidate)
    db.reset_for_tests(candidate)
    cp = inc.load_checkpoint(db.conn(), corpus.AGENT, corpus.SESSION, str(transcript))
    assert cp is not None and cp.boundary_seq > 0
    with transcript.open("ab") as output:
        output.write(harness.append_bytes(backend, 3))
    full = backends.parse_turns(backend, transcript)
    tail = inc.parse_range(backend, transcript, cp.boundary_offset, transcript.stat().st_size)
    assert tail == full[cp.boundary_seq:]
    monkeypatch.setattr("time.time", lambda: (harness.BASE_MS + 1000) / 1000)

    next_cp = replace(cp, size=transcript.stat().st_size,
                      complete_size=transcript.stat().st_size,
                      mtime_ns=transcript.stat().st_mtime_ns, turn_count=len(full),
                      tail_hash=inc.range_hash(transcript, cp.boundary_offset, transcript.stat().st_size))

    def publish(connection, captured):
        assert connection.in_transaction
        inc.write_checkpoint(connection, next_cp)

    message_writes.store_transcript_tail(
        agent_id=corpus.AGENT, backend_session_id=corpus.SESSION,
        source_file=str(transcript), turns=tail, start_seq=cp.boundary_seq,
        carry=cp.store_carry, on_stored=publish)
    db.reset_for_tests(reference)
    message_store.store_transcript_turns(agent_id=corpus.AGENT, backend_session_id=corpus.SESSION,
                                         source_file=str(transcript), turns=full)
    harness.assert_product_equal(reference, candidate)
    harness.assert_checkpoint_parse_equal(candidate, transcript, backend)
    # An identical replay needs no writer admission when no checkpoint is due.
    db.reset_for_tests(candidate)
    statements = []
    db.conn().set_trace_callback(statements.append)
    try:
        message_writes.store_transcript_tail(
            agent_id=corpus.AGENT, backend_session_id=corpus.SESSION,
            source_file=str(transcript), turns=tail, start_seq=cp.boundary_seq,
            carry=cp.store_carry)
    finally:
        db.conn().set_trace_callback(None)
    assert not any(sql.upper().startswith("BEGIN IMMEDIATE") for sql in statements)
    harness.assert_product_equal(reference, candidate)


@pytest.mark.parametrize("backend", harness.BACKENDS)
def test_tail_checkpoint_failure_keeps_rows_and_retries_progress(tmp_path, backend, monkeypatch):
    """A failed progress write must neither lose rows nor certify new bytes."""
    from dataclasses import replace
    from lib import message_store, message_writes
    from lib import transcript_incremental as inc

    reference, candidate = _clones(tmp_path, backend)
    transcript = tmp_path / "synthetic.jsonl"
    transcript.write_bytes(b"".join(harness.append_bytes(backend, i) for i in range(3)))
    harness.isolated_lane(reference, transcript, backend, "reference", tick=0)
    harness.isolated_lane(candidate, transcript, backend, "checkpoint", tick=0)
    db.reset_for_tests(candidate)
    previous = inc.load_checkpoint(db.conn(), corpus.AGENT, corpus.SESSION, str(transcript))
    assert previous is not None
    with transcript.open("ab") as output:
        output.write(harness.append_bytes(backend, 3))
    full = backends.parse_turns(backend, transcript)
    tail = inc.parse_range(backend, transcript, previous.boundary_offset, transcript.stat().st_size)
    updated = replace(previous, size=transcript.stat().st_size,
                      complete_size=transcript.stat().st_size,
                      mtime_ns=transcript.stat().st_mtime_ns, turn_count=len(full),
                      tail_hash=inc.range_hash(transcript, previous.boundary_offset, transcript.stat().st_size))
    failures = []
    monkeypatch.setattr("lib.log.log_exception", lambda code, error, **kw: failures.append(code))
    monkeypatch.setattr("time.time", lambda: (harness.BASE_MS + 1000) / 1000)

    def broken(connection, captured):
        inc.write_checkpoint(connection, updated)
        raise sqlite3.OperationalError("synthetic checkpoint fault after write")

    def apply(callback):
        message_writes.store_transcript_tail(
            agent_id=corpus.AGENT, backend_session_id=corpus.SESSION,
            source_file=str(transcript), turns=tail, start_seq=previous.boundary_seq,
            carry=previous.store_carry, on_stored=callback)

    apply(broken)
    assert inc.load_checkpoint(db.conn(), corpus.AGENT, corpus.SESSION, str(transcript)) == previous
    assert failures == ["transcriptCheckpointWriteSkip"]
    db.reset_for_tests(reference)
    message_store.store_transcript_turns(agent_id=corpus.AGENT, backend_session_id=corpus.SESSION,
                                         source_file=str(transcript), turns=full)
    harness.assert_product_equal(reference, candidate)
    db.reset_for_tests(candidate)
    apply(lambda connection, captured: inc.write_checkpoint(connection, updated))
    harness.assert_checkpoint_parse_equal(candidate, transcript, backend)
    harness.assert_product_equal(reference, candidate)


@pytest.mark.parametrize("during_admission", [False, True])
def test_tail_carry_refuses_prefix_edit_before_any_tail_write(tmp_path, monkeypatch, during_admission):
    """Prefix membership/payload drift invalidates carry even with equal counts."""
    from lib import message_writes
    from lib import transcript_incremental as inc

    _, candidate = _clones(tmp_path, "claude")
    transcript = tmp_path / "synthetic.jsonl"
    transcript.write_bytes(b"".join(harness.append_bytes("claude", i) for i in range(3)))
    harness.isolated_lane(candidate, transcript, "claude", "checkpoint", tick=0)
    db.reset_for_tests(candidate)
    connection = db.conn()
    previous = inc.load_checkpoint(connection, corpus.AGENT, corpus.SESSION, str(transcript))
    assert previous is not None and previous.boundary_seq > 0
    expected = message_writes._prepare_import_prelude(
        connection, corpus.AGENT, corpus.SESSION, [], phases=[], guard_carry=True)
    clock = connection.execute("SELECT revision FROM message_clock").fetchone()[0]
    count = connection.execute("SELECT count(*) FROM messages").fetchone()[0]

    def edit_prefix():
        with sqlite3.connect(candidate) as other:
            assert other.execute("UPDATE messages SET origin='agent' WHERE seq=0").rowcount == 1

    if during_admission:
        original = message_writes._revalidate_import_prelude

        def race(*args, **kwargs):
            edit_prefix()
            return original(*args, **kwargs)

        monkeypatch.setattr(message_writes, "_revalidate_import_prelude", race)
    else:
        edit_prefix()
    with transcript.open("ab") as output:
        output.write(harness.append_bytes("claude", 3))
    tail = inc.parse_range("claude", transcript, previous.boundary_offset, transcript.stat().st_size)
    with pytest.raises(message_writes._TailInvalidated, match="tail_carry_inputs_changed"):
        message_writes.store_transcript_tail(
            agent_id=corpus.AGENT, backend_session_id=corpus.SESSION,
            source_file=str(transcript), turns=tail, start_seq=previous.boundary_seq,
            carry=previous.store_carry, expected_prelude=expected)
    assert not connection.in_transaction
    assert connection.execute("SELECT revision FROM message_clock").fetchone()[0] == clock
    assert connection.execute("SELECT count(*) FROM messages").fetchone()[0] == count
    assert inc.load_checkpoint(connection, corpus.AGENT, corpus.SESSION, str(transcript)) == previous


@pytest.mark.parametrize("backend", harness.BACKENDS)
def test_running_importer_applies_only_guarded_tail_and_matches_all_product_rows(tmp_path, backend):
    reference, candidate = _clones(tmp_path, backend)
    transcript = tmp_path / "synthetic.jsonl"
    transcript.write_bytes(b"".join(harness.append_bytes(backend, i) for i in range(20)))
    with harness.PersistentLane(reference, transcript, backend, "reference") as full:
        with harness.PersistentLane(candidate, transcript, backend, "candidate") as actual:
            full.tick(0)
            actual.tick(0)
            harness.assert_product_equal(reference, candidate)
            for tick in range(1, 4):
                with transcript.open("ab") as output:
                    output.write(harness.append_bytes(backend, 19 + tick))
                full.tick(tick)
                report = actual.tick(tick)
                harness.assert_product_equal(reference, candidate)
                assert report.get("apply_mode") == "tail"
                assert 0 < report["parsed_bytes"] < report["source_bytes"] // 2
                harness.assert_checkpoint_parse_equal(candidate, transcript, backend)


@pytest.mark.parametrize("backend", harness.BACKENDS)
@pytest.mark.parametrize("change", ["prefix_edit", "truncate", "rotate", "late_result"])
def test_guarded_dispatch_falls_back_on_history_change(tmp_path, backend, change):
    reference, candidate = _clones(tmp_path, backend)
    transcript = tmp_path / "synthetic.jsonl"
    original = b"".join(harness.append_bytes(backend, i) for i in range(20))
    transcript.write_bytes(original)
    with harness.PersistentLane(reference, transcript, backend, "reference") as full:
        with harness.PersistentLane(candidate, transcript, backend, "candidate") as actual:
            full.tick(0)
            actual.tick(0)
            if change == "prefix_edit":
                for path in (reference, candidate):
                    with sqlite3.connect(path) as connection:
                        connection.execute("UPDATE messages SET origin='agent' WHERE seq=0")
                transcript.write_bytes(original + harness.append_bytes(backend, 20))
            elif change == "truncate":
                transcript.write_bytes(harness.append_bytes(backend, 0))
            elif change == "rotate":
                transcript.rename(tmp_path / "old-synthetic.jsonl")
                transcript.write_bytes(original + harness.append_bytes(backend, 20))
            else:
                if backend == "claude":
                    record = {"type": "user", "timestamp": "2026-10-11T10:00:00Z",
                              "message": {"role": "user", "content": [
                                  {"type": "tool_result", "tool_use_id": "c0", "content": "late result"}]}}
                else:
                    record = {"type": "response_item", "timestamp": "2026-10-11T10:00:00Z",
                              "payload": {"type": "function_call_output", "call_id": "c0",
                                          "turn_id": "t0", "output": "late result"}}
                with transcript.open("ab") as output:
                    output.write(json.dumps(record).encode() + b"\n")
            full.tick(1)
            report = actual.tick(1)
            harness.assert_product_equal(reference, candidate)
            assert report["apply_mode"] == "full"


@pytest.mark.parametrize("backend", harness.BACKENDS)
def test_effect_bearing_prefix_uses_full_replay_to_recover_team_fanout(tmp_path, backend):
    reference, candidate = _clones(tmp_path, backend)
    transcript = tmp_path / "synthetic.jsonl"
    data = b"".join(harness.append_bytes(backend, i) for i in range(20))
    data = data.replace(b"synthetic answer 0", b"<team>synthetic broadcast</team>", 1)
    transcript.write_bytes(data)
    for path in (reference, candidate):
        with sqlite3.connect(path) as connection:
            connection.execute("INSERT INTO teams(team_id,name,created_at,updated_at,communication_enabled) "
                               "VALUES ('synthetic-team','Synthetic',1,1,1)")
            for agent in (corpus.AGENT, corpus.SENDER):
                connection.execute("INSERT INTO team_members(team_id,agent_id,added_at) VALUES ('synthetic-team',?,1)",
                                   (agent,))
    with harness.PersistentLane(reference, transcript, backend, "reference") as full:
        with harness.PersistentLane(candidate, transcript, backend, "candidate") as actual:
            full.tick(0)
            actual.tick(0)
            harness.assert_product_equal(reference, candidate)
            # A row checkpoint must not excuse a missing delivery. A later
            # import must recover the effect of this earlier assistant row.
            for path in (reference, candidate):
                with sqlite3.connect(path) as connection:
                    assert connection.execute("SELECT count(*) FROM team_inbox").fetchone()[0] == 1
                    connection.execute("DELETE FROM team_inbox")
                    connection.execute("DELETE FROM team_messages")
            with transcript.open("ab") as output:
                output.write(harness.append_bytes(backend, 20))
            full.tick(1)
            report = actual.tick(1)
            harness.assert_product_equal(reference, candidate)
            assert report["apply_mode"] == "full"
            with sqlite3.connect(candidate) as connection:
                assert connection.execute("SELECT count(*) FROM team_messages").fetchone()[0] == 1
                assert connection.execute("SELECT count(*) FROM team_inbox").fetchone()[0] == 1


@pytest.mark.parametrize("backend", harness.BACKENDS)
@pytest.mark.parametrize("failed_tick", [1, 600])
def test_dispatch_checkpoint_failure_invalidates_frontier_and_retries_full(tmp_path, backend, failed_tick):
    reference, candidate = _clones(tmp_path, backend)
    transcript = tmp_path / "synthetic.jsonl"
    initial = b"".join(harness.append_bytes(backend, i) for i in range(20))
    transcript.write_bytes(initial)
    with harness.PersistentLane(reference, transcript, backend, "reference") as full:
        with harness.PersistentLane(candidate, transcript, backend, "candidate") as actual:
            full.tick(0)
            actual.tick(0)
            with transcript.open("ab") as output:
                output.write(harness.append_bytes(backend, 20))
            full.tick(failed_tick)
            failed_progress = actual.tick(failed_tick, faults={"checkpoint": True})
            assert failed_progress["apply_mode"] == ("tail" if failed_tick == 1 else "full")
            harness.assert_product_equal(reference, candidate)
            with sqlite3.connect(candidate) as connection:
                assert connection.execute("SELECT complete_size FROM transcript_import_checkpoints").fetchone()[0] == len(initial)
            with transcript.open("ab") as output:
                output.write(harness.append_bytes(backend, 21))
            full.tick(failed_tick + 1)
            recovered = actual.tick(failed_tick + 1)
            assert recovered["apply_mode"] == "full"
            harness.assert_product_equal(reference, candidate)
            harness.assert_checkpoint_parse_equal(candidate, transcript, backend)
            with transcript.open("ab") as output:
                output.write(harness.append_bytes(backend, 22))
            full.tick(failed_tick + 2)
            resumed = actual.tick(failed_tick + 2)
            harness.assert_product_equal(reference, candidate)
            assert resumed["apply_mode"] == "tail"


@pytest.mark.parametrize("backend", harness.BACKENDS)
def test_dispatch_recovers_failed_fanout_after_committed_earlier_rows(tmp_path, backend):
    reference, candidate = _clones(tmp_path, backend)
    transcript = tmp_path / "synthetic.jsonl"
    transcript.write_bytes(b"".join(harness.append_bytes(backend, i) for i in range(20)))
    for path in (reference, candidate):
        with sqlite3.connect(path) as connection:
            connection.execute("INSERT INTO teams(team_id,name,created_at,updated_at,communication_enabled) "
                               "VALUES ('synthetic-team','Synthetic',1,1,1)")
            for agent in (corpus.AGENT, corpus.SENDER):
                connection.execute("INSERT INTO team_members(team_id,agent_id,added_at) VALUES ('synthetic-team',?,1)",
                                   (agent,))
    with harness.PersistentLane(reference, transcript, backend, "reference") as full:
        with harness.PersistentLane(candidate, transcript, backend, "candidate") as actual:
            full.tick(0)
            actual.tick(0)
            delta = harness.append_bytes(backend, 20).replace(
                b"synthetic answer 20", b"<team>synthetic retry broadcast</team>")
            with transcript.open("ab") as output:
                output.write(delta)
            faults = {"team": True, "single_row_batches": True}
            expected, failed = full.tick(1, faults=faults), actual.tick(1, faults=faults)
            assert expected["failure_code"] == failed["failure_code"] == "OperationalError"
            harness.assert_product_equal(reference, candidate)
            with sqlite3.connect(candidate) as connection:
                assert connection.execute("SELECT count(*) FROM messages WHERE text='synthetic request 20'").fetchone()[0] == 1
                assert connection.execute("SELECT count(*) FROM team_inbox").fetchone()[0] == 0
            full.tick(2)
            recovered = actual.tick(2)
            assert recovered["apply_mode"] == "full"
            harness.assert_product_equal(reference, candidate)
            with sqlite3.connect(candidate) as connection:
                assert connection.execute("SELECT count(*) FROM team_messages").fetchone()[0] == 1
                assert connection.execute("SELECT count(*) FROM team_inbox").fetchone()[0] == 1


@pytest.mark.parametrize("backend", harness.BACKENDS)
@pytest.mark.parametrize("bound_parser", [False, True])
def test_real_background_connection_lifecycle_retains_guarded_tail(tmp_path, backend, bound_parser):
    """Worker closes SQLite per job; streamer supplies the native bound parser."""
    reference, candidate = _clones(tmp_path, backend)
    transcript = tmp_path / "synthetic.jsonl"
    transcript.write_bytes(b"".join(harness.append_bytes(backend, i) for i in range(20)))
    with harness.PersistentLane(reference, transcript, backend, "reference") as full:
        with harness.PersistentLane(candidate, transcript, backend, "candidate") as actual:
            for tick in range(3):
                if tick:
                    with transcript.open("ab") as output:
                        output.write(harness.append_bytes(backend, 19 + tick))
                full.tick(tick, close_connection=True)
                report = actual.tick(tick, close_connection=True,
                                     faults={"native_bound_parser": bound_parser})
                harness.assert_product_equal(reference, candidate)
                if tick:
                    assert report["apply_mode"] == "tail"
                    assert report["parsed_bytes"] < report["source_bytes"] // 2


@pytest.mark.parametrize("backend", harness.BACKENDS)
def test_assistant_prefix_replays_latest_user_provenance_like_full_import(tmp_path, backend):
    reference, candidate = _clones(tmp_path, backend)
    for path in (reference, candidate):
        with sqlite3.connect(path) as connection:
            connection.execute(
                "INSERT INTO messages(message_id,agent_id,backend_session_id,source_file,seq,role,"
                "timestamp,text,updated_at,revision,origin,sender_agent_id) "
                "VALUES ('u-prior',?,?, 'client:prior',-1,'user','2026-10-11T00:00:00Z',"
                "'prior peer input absent from provider file',1,1,'agent',?)",
                (corpus.AGENT, corpus.SESSION, corpus.SENDER))
            connection.execute("UPDATE message_clock SET revision=1")
            connection.execute("INSERT INTO conversation_heads VALUES (?,?,1,0)",
                               (corpus.AGENT, corpus.SESSION))
    records = [json.loads(line) for line in harness.append_bytes(backend, 0).splitlines()]
    if backend == "claude":
        records = [r for r in records if not (
            r["type"] == "user" and isinstance(r["message"]["content"], str))]
    else:
        records = [r for r in records if r.get("payload", {}).get("type") != "user_message"]
    transcript = tmp_path / "synthetic.jsonl"
    transcript.write_bytes(b"".join(json.dumps(r).encode() + b"\n" for r in records)
                           + b"".join(harness.append_bytes(backend, i) for i in range(1, 20)))
    assert backends.parse_turns(backend, transcript)[0]["role"] == "assistant"
    with harness.PersistentLane(reference, transcript, backend, "reference") as full:
        with harness.PersistentLane(candidate, transcript, backend, "candidate") as actual:
            full.tick(0)
            actual.tick(0)
            harness.assert_product_equal(reference, candidate)
            with transcript.open("ab") as output:
                output.write(harness.append_bytes(backend, 20))
            full.tick(1)
            report = actual.tick(1)
            harness.assert_product_equal(reference, candidate)
            assert report["apply_mode"] == "full"


@pytest.mark.parametrize("backend", harness.BACKENDS)
@pytest.mark.parametrize("audit_by", ["ticks", "elapsed"])
def test_unsampled_rewrite_is_repaired_by_bounded_audit(tmp_path, backend, audit_by):
    """801 accepts stale unsampled bytes only until the bounded full audit."""
    from lib import transcript_incremental as inc
    reference, candidate = _clones(tmp_path, backend)
    transcript = tmp_path / "synthetic.jsonl"
    original = b"".join(harness.append_bytes(backend, i) for i in range(300))
    transcript.write_bytes(original)
    with harness.PersistentLane(reference, transcript, backend, "reference") as full:
        with harness.PersistentLane(candidate, transcript, backend, "candidate") as actual:
            full.tick(0)
            actual.tick(0)
            with sqlite3.connect(candidate) as connection:
                offset, sampled = connection.execute(
                    "SELECT boundary_offset,prefix_hash FROM transcript_import_checkpoints").fetchone()
            changed = original.replace(b"synthetic step 8", b"synthetic task 8", 1)
            assert changed != original and len(changed) == len(original)
            transcript.write_bytes(changed + harness.append_bytes(backend, 300))
            assert inc.prefix_hash(transcript, offset) == sampled
            first = actual.tick(1)
            assert first["apply_mode"] == "tail"
            # Run to the twentieth attempted tail, or to the ten-minute limit.
            ticks = range(2, 21) if audit_by == "ticks" else [600]
            for tick in ticks:
                # Repeated unsampled edits must not postpone the audit.
                old, new = ((b"synthetic task 8", b"synthetic risk 8") if tick % 2 == 0
                            else (b"synthetic risk 8", b"synthetic task 8"))
                contents = transcript.read_bytes()
                assert old in contents
                transcript.write_bytes(contents.replace(old, new, 1))
                with transcript.open("ab") as output:
                    output.write(harness.append_bytes(backend, 300 + tick))
                due = tick == (20 if audit_by == "ticks" else 600)
                if due:
                    with sqlite3.connect(candidate) as source, sqlite3.connect(reference) as target:
                        assert source.execute("SELECT count(*) FROM messages WHERE text=?",
                                              ("synthetic step 8",)).fetchone()[0] == 1
                        # Recovery oracle starts from the SAME committed stale
                        # state. An always-fresh history necessarily allocated
                        # its rewrite revision earlier under signed801; do not
                        # hide that chronology by excluding revision columns.
                        source.backup(target)
                report = actual.tick(tick)
            full.tick(tick)
            harness.assert_product_equal(reference, candidate)
            assert report["apply_mode"] == "full"
            assert {"event": "transcriptTailDecision", "code": "full_audit_prefix_rewritten",
                    "count": 1} in report["tail_events"]
            with transcript.open("ab") as output:
                output.write(harness.append_bytes(backend, 1000))
            full.tick(tick + 1)
            recovery = actual.tick(tick + 1)
            harness.assert_product_equal(reference, candidate)
            assert recovery["apply_mode"] == "tail"


@pytest.mark.parametrize("backend", harness.BACKENDS)
def test_ordinary_append_reads_bounded_samples_and_new_bytes(tmp_path, backend):
    reference, candidate = _clones(tmp_path, backend)
    transcript = tmp_path / "synthetic.jsonl"
    next_ordinal = harness.write_history(transcript, backend, mebibytes=1)
    with harness.PersistentLane(reference, transcript, backend, "reference") as full:
        with harness.PersistentLane(candidate, transcript, backend, "candidate") as actual:
            full.tick(0)
            actual.tick(0)
            with transcript.open("ab") as output:
                output.write(harness.append_bytes(backend, next_ordinal))
            full.tick(1)
            report = actual.tick(1, close_connection=True)
            harness.assert_product_equal(reference, candidate)
            assert report["apply_mode"] == "tail"
            # All delivered source bytes counted, not an implementation flag.
            assert report["transcript_read_bytes"] < 512 * 1024


@pytest.mark.parametrize("backend", harness.BACKENDS)
@pytest.mark.parametrize("yield_at", [1, 3, 6])
def test_interleaved_prefix_client_during_yield_cannot_certify_old_carry(tmp_path, backend, yield_at):
    reference, candidate = _clones(tmp_path, backend)
    transcript = tmp_path / "synthetic.jsonl"
    transcript.write_bytes(b"".join(harness.append_bytes(backend, i) for i in range(20)))
    with harness.PersistentLane(reference, transcript, backend, "reference") as full:
        with harness.PersistentLane(candidate, transcript, backend, "candidate") as actual:
            full.tick(0)
            actual.tick(0)
            with sqlite3.connect(candidate) as connection:
                boundary_seq = connection.execute("SELECT boundary_seq FROM transcript_import_checkpoints").fetchone()[0]
            with transcript.open("ab") as output:
                output.write(harness.append_bytes(backend, 20))
            faults = {"client_on_yield": True, "single_row_batches": True, "yield_at": yield_at}
            # Align the foreign commit by canonical row sequence in both
            # lanes, not by different numbers of unchanged prefix rows.
            reference_faults = dict(faults, yield_before_seq=boundary_seq + yield_at - 1)
            a, b = full.tick(1, faults=reference_faults), actual.tick(1, faults=faults)
            assert "failure_code" not in a and "failure_code" not in b
            harness.assert_product_equal(reference, candidate)
            assert b["yield_intervention"] == yield_at and b["yield_windows"] == 6
            assert {"event": "transcriptTailRetention", "code": "checkpoint_foreign_commit_yield", "count": 1} in b["tail_events"]
            with transcript.open("ab") as output:
                output.write(harness.append_bytes(backend, 21))
            full.tick(2)
            recovered = actual.tick(2)
            assert recovered["apply_mode"] == "full"
            harness.assert_product_equal(reference, candidate)
            with transcript.open("ab") as output:
                output.write(harness.append_bytes(backend, 22))
            full.tick(3)
            assert actual.tick(3)["apply_mode"] == "tail"
            harness.assert_product_equal(reference, candidate)


@pytest.mark.parametrize("tool_name", ["Agent", "Task"])
@pytest.mark.parametrize("transition", ["appearance", "removal", "rebind"])
def test_external_subagent_prefix_is_replayed_in_full(tmp_path, tool_name, transition):
    reference, candidate = _clones(tmp_path, "claude")
    transcript = tmp_path / "parent.jsonl"
    records = [json.loads(line) for line in harness.append_bytes("claude", 0).splitlines()]
    tool = records[1]["message"]["content"][1]
    tool["name"] = tool_name
    tool["input"] = {"description": "Synthetic delegated work", "prompt": "Synthetic input"}
    first = b"".join(json.dumps(row, separators=(",", ":")).encode() + b"\n" for row in records)
    transcript.write_bytes(first + harness.append_bytes("claude", 1) + harness.append_bytes("claude", 2))
    children = tmp_path / "parent" / "subagents"
    children.mkdir(parents=True)
    child, meta = children / "agent-child.jsonl", children / "agent-child.meta.json"
    if transition != "appearance":
        child.write_text("{}\n")
        meta.write_text(json.dumps({"toolUseId": "c0"}))
    with harness.PersistentLane(reference, transcript, "claude", "reference") as full:
        with harness.PersistentLane(candidate, transcript, "claude", "candidate") as actual:
            full.tick(0)
            actual.tick(0)
            harness.assert_product_equal(reference, candidate)
            if transition == "appearance":
                child.write_text("{}\n")
                meta.write_text(json.dumps({"toolUseId": "c0"}))
            elif transition == "removal":
                child.unlink()
                meta.unlink()
            else:
                meta.write_text(json.dumps({"toolUseId": "unrelated-call"}))
            with transcript.open("ab") as output:
                output.write(harness.append_bytes("claude", 3))
            full.tick(1)
            report = actual.tick(1)
            expected_path = transition == "appearance"
            for database in (reference, candidate):
                stored = json.dumps(harness.database_view(database)["messages"])
                assert (str(child) in stored) == expected_path
            harness.assert_product_equal(reference, candidate)
            assert report["apply_mode"] == "full"


@pytest.mark.parametrize("backend", harness.BACKENDS)
@pytest.mark.parametrize("fault", ["own_only", "connection", "read_release", "read_reacquire", "read_last"])
def test_yield_fence_rejects_only_dirty_or_unverifiable_windows(tmp_path, backend, fault):
    reference, candidate = _clones(tmp_path, backend)
    transcript = tmp_path / "synthetic.jsonl"
    initial = b"".join(harness.append_bytes(backend, i) for i in range(20))
    transcript.write_bytes(initial)
    with harness.PersistentLane(reference, transcript, backend, "reference") as full:
        with harness.PersistentLane(candidate, transcript, backend, "candidate") as actual:
            full.tick(0, faults={"single_row_batches": True})
            actual.tick(0, faults={"single_row_batches": True})
            harness.assert_checkpoint_parse_equal(candidate, transcript, backend)
            harness.assert_product_equal(reference, candidate)
            with transcript.open("ab") as output:
                output.write(harness.append_bytes(backend, 20))
            faults = {"single_row_batches": True}
            expected = None
            if fault == "connection":
                faults["connection_on_yield"] = True
                faults["yield_at"] = 6
                expected = "checkpoint_connection_changed_yield"
            elif fault.startswith("read_"):
                faults["fence_read_at"] = {"read_release": 1, "read_reacquire": 2, "read_last": 12}[fault]
                expected = "checkpoint_data_version_read_failure"
            full.tick(1, faults={"single_row_batches": True})
            report = actual.tick(1, faults=faults)
            assert report["apply_mode"] == "tail"
            harness.assert_product_equal(reference, candidate)
            retained = [event["code"] for event in report["tail_events"] if event["event"] == "transcriptTailRetention"]
            if expected:
                assert expected in retained
                with sqlite3.connect(candidate) as connection:
                    assert connection.execute("SELECT complete_size FROM transcript_import_checkpoints").fetchone()[0] == len(initial)
            else:
                assert not retained
                harness.assert_checkpoint_parse_equal(candidate, transcript, backend)
            with transcript.open("ab") as output:
                output.write(harness.append_bytes(backend, 21))
            full.tick(2)
            assert actual.tick(2)["apply_mode"] == ("full" if expected else "tail")
            harness.assert_product_equal(reference, candidate)
            with transcript.open("ab") as output:
                output.write(harness.append_bytes(backend, 22))
            full.tick(3)
            assert actual.tick(3)["apply_mode"] == "tail"
            harness.assert_product_equal(reference, candidate)


@pytest.mark.parametrize("backend", harness.BACKENDS)
def test_dirty_full_bootstrap_refuses_checkpoint_then_recovers_to_tail(tmp_path, backend):
    reference, candidate = _clones(tmp_path, backend)
    transcript = tmp_path / "synthetic.jsonl"
    transcript.write_bytes(b"".join(harness.append_bytes(backend, i) for i in range(3)))
    with harness.PersistentLane(reference, transcript, backend, "reference") as full:
        with harness.PersistentLane(candidate, transcript, backend, "candidate") as actual:
            faults = {"single_row_batches": True, "client_on_yield": True}
            full.tick(0, faults=faults)
            rejected = actual.tick(0, faults=faults)
            harness.assert_product_equal(reference, candidate)
            assert {"event": "transcriptTailRetention", "code": "checkpoint_foreign_commit_yield", "count": 1} in rejected["tail_events"]
            with sqlite3.connect(candidate) as connection:
                assert connection.execute("SELECT count(*) FROM transcript_import_checkpoints").fetchone()[0] == 0
            for tick in (1, 2):
                with transcript.open("ab") as output:
                    output.write(harness.append_bytes(backend, 2 + tick))
                full.tick(tick)
                report = actual.tick(tick)
                harness.assert_product_equal(reference, candidate)
                assert report["apply_mode"] == ("full" if tick == 1 else "tail")


@pytest.mark.parametrize("backend", harness.BACKENDS)
def test_dirty_window_and_partial_fanout_failure_keep_recovery_full(tmp_path, backend):
    reference, candidate = _clones(tmp_path, backend)
    transcript = tmp_path / "synthetic.jsonl"
    transcript.write_bytes(b"".join(harness.append_bytes(backend, i) for i in range(20)))
    for path in (reference, candidate):
        with sqlite3.connect(path) as connection:
            connection.execute("INSERT INTO teams(team_id,name,created_at,updated_at,communication_enabled) "
                               "VALUES ('synthetic-team','Synthetic',1,1,1)")
            for agent in (corpus.AGENT, corpus.SENDER):
                connection.execute("INSERT INTO team_members(team_id,agent_id,added_at) VALUES ('synthetic-team',?,1)",
                                   (agent,))
    with harness.PersistentLane(reference, transcript, backend, "reference") as full:
        with harness.PersistentLane(candidate, transcript, backend, "candidate") as actual:
            full.tick(0)
            actual.tick(0)
            with sqlite3.connect(candidate) as connection:
                boundary_seq = connection.execute("SELECT boundary_seq FROM transcript_import_checkpoints").fetchone()[0]
            with transcript.open("ab") as output:
                output.write(harness.append_bytes(backend, 20).replace(
                    b"synthetic answer 20", b"<team>synthetic interrupted fanout</team>"))
            common = {"single_row_batches": True, "client_on_yield": True, "team": True}
            a = full.tick(1, faults=dict(common, yield_before_seq=boundary_seq))
            b = actual.tick(1, faults=common)
            assert a["failure_code"] == b["failure_code"] == "OperationalError"
            harness.assert_product_equal(reference, candidate)
            full.tick(2)
            assert actual.tick(2)["apply_mode"] == "full"
            harness.assert_product_equal(reference, candidate)
            with sqlite3.connect(candidate) as connection:
                assert connection.execute("SELECT count(*) FROM team_inbox").fetchone()[0] == 1
            with transcript.open("ab") as output:
                output.write(harness.append_bytes(backend, 21))
            full.tick(3)
            assert actual.tick(3)["apply_mode"] == "tail"
            harness.assert_product_equal(reference, candidate)


@pytest.mark.parametrize("backend", harness.BACKENDS)
@pytest.mark.parametrize("audit_by", ["ticks", "elapsed"])
def test_audit_deadline_is_not_renewed_by_ordinary_appends(tmp_path, backend, audit_by):
    reference, candidate = _clones(tmp_path, backend)
    transcript = tmp_path / "synthetic.jsonl"
    transcript.write_bytes(b"".join(harness.append_bytes(backend, i) for i in range(20)))
    with harness.PersistentLane(reference, transcript, backend, "reference") as full:
        with harness.PersistentLane(candidate, transcript, backend, "candidate") as actual:
            full.tick(0)
            actual.tick(0)
            ticks = range(1, 22) if audit_by == "ticks" else [599, 600, 601]
            for ordinal, tick in enumerate(ticks):
                with transcript.open("ab") as output:
                    output.write(harness.append_bytes(backend, 20 + ordinal))
                full.tick(tick)
                report = actual.tick(tick, close_connection=True)
                harness.assert_product_equal(reference, candidate)
                due = tick == (20 if audit_by == "ticks" else 600)
                assert report["apply_mode"] == ("full" if due else "tail")
                if due:
                    assert {"event": "transcriptTailDecision", "code": "full_audit",
                            "count": 1} in report["tail_events"]


@pytest.mark.parametrize("backend", harness.BACKENDS)
@pytest.mark.parametrize("anomaly", ["sample", "restart", "eviction", "clock"])
def test_digest_unknown_interval_or_sample_change_requires_full(tmp_path, backend, anomaly):
    from contextlib import ExitStack
    reference, candidate = _clones(tmp_path, backend)
    transcript = tmp_path / "synthetic.jsonl"
    original = b"".join(harness.append_bytes(backend, i) for i in range(20))
    transcript.write_bytes(original)
    with ExitStack() as stack:
        full = stack.enter_context(harness.PersistentLane(reference, transcript, backend, "reference"))
        actual = stack.enter_context(harness.PersistentLane(candidate, transcript, backend, "candidate"))
        full.tick(0)
        actual.tick(0)
        if anomaly == "sample":
            changed = original.replace(b"synthetic step 0", b"synthetic task 0", 1)
            assert changed != original
            transcript.write_bytes(changed)
        elif anomaly == "restart":
            stack.pop_all().close()
            full = stack.enter_context(harness.PersistentLane(reference, transcript, backend, "reference"))
            actual = stack.enter_context(harness.PersistentLane(candidate, transcript, backend, "candidate"))
        with transcript.open("ab") as output:
            output.write(harness.append_bytes(backend, 20))
        faults = {"evict_frontier": True} if anomaly == "eviction" else (
            {"monotonic_seconds": -1.0} if anomaly == "clock" else {})
        full.tick(1)
        report = actual.tick(1, faults=faults)
        harness.assert_product_equal(reference, candidate)
        assert report["apply_mode"] == "full"
        assert report["components"]["_read_tail_digest"]["read_bytes"] >= transcript.stat().st_size


@pytest.mark.parametrize("backend", harness.BACKENDS)
@pytest.mark.parametrize("settings", [{"audit_every": 2}, {"audit_seconds": 2.0}])
def test_tighter_audit_settings_still_force_earlier_full(tmp_path, backend, settings):
    reference, candidate = _clones(tmp_path, backend)
    transcript = tmp_path / "synthetic.jsonl"
    transcript.write_bytes(b"".join(harness.append_bytes(backend, i) for i in range(20)))
    with harness.PersistentLane(reference, transcript, backend, "reference") as full:
        with harness.PersistentLane(candidate, transcript, backend, "candidate") as actual:
            full.tick(0)
            actual.tick(0)
            for tick in [1, 2]:
                with transcript.open("ab") as output:
                    output.write(harness.append_bytes(backend, 20 + tick))
                full.tick(tick)
                report = actual.tick(tick, faults={"audit_settings": settings})
                harness.assert_product_equal(reference, candidate)
                assert report["apply_mode"] == ("tail" if tick == 1 else "full")
