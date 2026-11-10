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
            decision = [e for e in report["tail_events"] if e["event"] == "transcriptTailDecision"]
            assert decision == [{"event": "transcriptTailDecision", "code": "full_no_frontier",
                                 "count": 1, "reason": "bootstrap_effect"}]
            with sqlite3.connect(candidate) as connection:
                assert connection.execute("SELECT count(*) FROM team_messages").fetchone()[0] == 1
                assert connection.execute("SELECT count(*) FROM team_inbox").fetchone()[0] == 1


@pytest.mark.parametrize("backend", harness.BACKENDS)
def test_dispatch_checkpoint_failure_invalidates_frontier_and_retries_full(tmp_path, backend):
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
            full.tick(1)
            failed_progress = actual.tick(1, faults={"checkpoint": True})
            assert failed_progress["apply_mode"] == "tail"
            harness.assert_product_equal(reference, candidate)
            with sqlite3.connect(candidate) as connection:
                assert connection.execute("SELECT complete_size FROM transcript_import_checkpoints").fetchone()[0] == len(initial)
            with transcript.open("ab") as output:
                output.write(harness.append_bytes(backend, 21))
            full.tick(2)
            recovered = actual.tick(2)
            assert recovered["apply_mode"] == "full"
            harness.assert_product_equal(reference, candidate)
            harness.assert_checkpoint_parse_equal(candidate, transcript, backend)


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
def test_rewrite_outside_durable_sample_windows_cannot_skip_changed_prefix(tmp_path, backend):
    """Shadow's sampled prefix hash is insufficient for authoritative writes."""
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
            # The mutation deliberately survives the existing durable sample
            # guard, so only authoritative prefix certification can reject it.
            assert inc.prefix_hash(transcript, offset) == sampled
            full.tick(1)
            report = actual.tick(1)
            harness.assert_product_equal(reference, candidate)
            assert report["apply_mode"] == "full"


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
                if tick == 1:
                    decision = [e for e in report["tail_events"] if e["event"] == "transcriptTailDecision"]
                    assert decision == [{"event": "transcriptTailDecision", "code": "full_no_frontier",
                                         "count": 1, "reason": "publication_foreign_commit_yield"}]


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
@pytest.mark.parametrize("cause", ["initial", "evicted", "overweight", "consumed", "revision", "reset", "removed", "history_lost", "metadata_failure"])
def test_no_frontier_reason_reports_only_proven_cache_history(tmp_path, backend, cause, monkeypatch):
    """Operational codes explain actual misses without changing native products."""
    from lib import transcript_incremental as inc

    reference, candidate = _clones(tmp_path, backend)
    transcript = tmp_path / "private-source-canary.jsonl"
    transcript.write_bytes(b"".join(harness.append_bytes(backend, i) for i in range(3)))
    harness.run_lane(reference, transcript, backend, "reference", tick=0)
    inc._TAIL_STATES.reset()
    first = harness.run_lane(candidate, transcript, backend, "candidate", tick=0)
    assert first["apply_mode"] == "full"
    first_decision = [e for e in first["tail_events"] if e["event"] == "transcriptTailDecision"]
    assert first_decision == [{"event": "transcriptTailDecision", "code": "full_no_frontier",
                               "count": 1, "reason": "unknown_reset"}]
    key = inc._tail_key(transcript, corpus.AGENT, corpus.SESSION)
    revision = inc.parser_version(backend)
    found, state = inc._TAIL_STATES.get(key, revision)
    assert found and state is not None, "fixture must really retain a native frontier"
    expected = None
    if cause == "evicted":
        monkeypatch.setattr(inc._TAIL_STATES, "max_entries", 1)
        inc._TAIL_STATES.put(("other-private-source", "other-agent", "other-session"), revision, None)
        expected = "evicted"
    elif cause == "overweight":
        monkeypatch.setattr(inc._TAIL_STATES, "max_weight", 1)
        inc._TAIL_STATES.put(key, revision, state)
        expected = "overweight"
    elif cause == "consumed":
        inc._TAIL_STATES.consume(key, revision)
        expected = "consumed"
    elif cause == "revision":
        inc._TAIL_STATES.put(key, "older-parser-revision", state)
        expected = "revision"
    elif cause == "reset":
        inc._TAIL_STATES.reset()
        expected = "unknown_reset"
    elif cause == "removed":
        inc._TAIL_STATES.pop(key)
        expected = "unknown_removal"
    elif cause == "metadata_failure":
        # Retain a genuinely observed OLD overweight reason when the next
        # consumption's metadata replacement fails, while native cache work succeeds.
        cap = inc._TAIL_STATES.max_weight
        monkeypatch.setattr(inc._TAIL_STATES, "max_weight", 1)
        inc._TAIL_STATES.put(key, revision, state)
        monkeypatch.setattr(inc._TAIL_STATES, "max_weight", cap)
        old = inc._TAIL_REASONS.peek(key)
        assert old is not None and old[1].code == "overweight"
        put = inc._TAIL_REASONS.put
        failed = False

        def fail_selected_replacement(actual_key, actual_revision, value, **kwargs):
            nonlocal failed
            if actual_key == key and not failed:
                failed = True
                raise MemoryError("synthetic metadata replacement failure")
            return put(actual_key, actual_revision, value, **kwargs)

        monkeypatch.setattr(inc._TAIL_REASONS, "put", fail_selected_replacement)
        inc._TAIL_STATES.consume(key, revision)
        assert failed and inc._TAIL_REASONS.peek(key) == old
        expected = "unknown_metadata"
    elif cause == "history_lost":
        inc._TAIL_STATES.pop(key)
        inc._TAIL_REASONS.reset()
        expected = "unknown_reset"  # absent bounded history never invents eviction
    with transcript.open("ab") as output:
        output.write(harness.append_bytes(backend, 3))
    report = harness.run_lane(candidate, transcript, backend, "candidate", tick=1)
    decision = [e for e in report["tail_events"] if e["event"] == "transcriptTailDecision"]
    if expected is None:
        assert report["apply_mode"] == "tail"
        assert decision == [{"event": "transcriptTailDecision", "code": "tail_applied", "count": 1}]
    else:
        assert report["apply_mode"] == "full"
        assert decision == [{"event": "transcriptTailDecision", "code": "full_no_frontier",
                             "count": 1, "reason": expected}]
    harness.run_lane(reference, transcript, backend, "reference", tick=1)
    harness.assert_product_equal(reference, candidate)
    if cause == "metadata_failure":
        inc._TAIL_STATES.reset()  # successful explicit reset retires poisoned history
        reset = harness.run_lane(candidate, transcript, backend, "candidate", tick=2)
        observed = [e for e in reset["tail_events"] if e["event"] == "transcriptTailDecision"]
        assert observed == [{"event": "transcriptTailDecision", "code": "full_no_frontier",
                             "count": 1, "reason": "unknown_reset"}]
        harness.run_lane(reference, transcript, backend, "reference", tick=2)
        harness.assert_product_equal(reference, candidate)


@pytest.mark.parametrize("backend", harness.BACKENDS)
@pytest.mark.parametrize("intervention", ["unrelated", "prefix_before_probe", "prefix_before_writer", "checkpoint_failure", "writer_rollback_failure",
                                           "primary_keyboard_interrupt", "primary_system_exit",
                                           "cleanup_keyboard_interrupt", "cleanup_system_exit",
                                           "caught_caller_control", "diagnostic_control"])
def test_full_bootstrap_recertifies_after_unrelated_foreign_yield(tmp_path, backend, intervention, monkeypatch):
    """Fresh carry can publish only if the read-only probe and final writer agree."""
    from lib import log as host_log
    from lib import message_store, message_writes
    from lib import transcript_incremental as inc

    reference, candidate = _clones(tmp_path, backend)
    transcript = tmp_path / "synthetic.jsonl"
    transcript.write_bytes(b"".join(harness.append_bytes(backend, i) for i in range(20)))
    observations = []
    real_yield = message_writes._ImportWriter.yield_writer
    real_prepare = message_writes._prepare_import_prelude
    real_execute = message_writes._ImportWriter.execute
    real_checkpoint = inc.write_checkpoint
    real_log_exception = host_log.log_exception
    diagnostic_control = KeyboardInterrupt("synthetic local diagnostic control")
    caller_control = KeyboardInterrupt("synthetic already handled caller control")
    primary_error = sqlite3.DatabaseError("synthetic current-writer proof read failure")
    cleanup_error = sqlite3.OperationalError("database is locked")
    rollback_control = intervention in ("writer_rollback_failure", "primary_keyboard_interrupt",
                                        "primary_system_exit", "cleanup_keyboard_interrupt", "cleanup_system_exit",
                                        "caught_caller_control", "diagnostic_control")
    if intervention == "primary_keyboard_interrupt":
        primary_error = KeyboardInterrupt("synthetic primary control")
    elif intervention == "primary_system_exit":
        primary_error = SystemExit(73)
    elif intervention == "cleanup_keyboard_interrupt":
        cleanup_error = KeyboardInterrupt("synthetic cleanup control")
    elif intervention == "cleanup_system_exit":
        cleanup_error = SystemExit(74)
    expected_control = (diagnostic_control if intervention == "diagnostic_control"
                        else primary_error if not isinstance(primary_error, Exception)
                        else cleanup_error if not isinstance(cleanup_error, Exception) else None)
    native_holder, proxy_holder = [], []
    cleanup_attempts = operation_calls = 0

    class FailingWriterRollback:
        def __init__(self, native):
            self.native = native
            self.primary_raised = False

        def __getattr__(self, name):
            return getattr(self.native, name)

        def execute(self, sql, parameters=()):
            nonlocal cleanup_attempts
            statement = str(sql).strip().upper()
            if final_writer and self.native.in_transaction:
                if statement.startswith("SELECT TRACE_ID, STARTED_AT") and not self.primary_raised:
                    self.primary_raised = True
                    raise primary_error
                if statement == "ROLLBACK":
                    cleanup_attempts += 1
                    assert self.native.in_transaction
                    raise cleanup_error  # leave the REAL writer transaction active
            return self.native.execute(sql, parameters)

    def provided_connection():
        native = db.conn()
        if not native_holder:
            native_holder.append(native)
            proxy_holder.append(FailingWriterRollback(native))
        assert native_holder[0] is native
        return proxy_holder[0]

    def edit_prefix(path):
        with sqlite3.connect(path) as other:
            cursor = other.execute("UPDATE messages SET text=text || ' foreign' WHERE agent_id=? "
                                   "AND backend_session_id=? AND seq=0", (corpus.AGENT, corpus.SESSION))
            assert cursor.rowcount == 1

    for database, lane in ((reference, "reference"), (candidate, "candidate")):
        injected = probe_seen = prefix_changed = final_writer = False
        checkpoint_attempts = 0

        def unrelated_commit(writer):
            nonlocal injected
            real_yield(writer)
            if injected:
                return
            assert not writer.in_transaction
            before = real_prepare(writer._database, corpus.AGENT, corpus.SESSION, [], raw_only=True)
            version = writer._database.execute("PRAGMA data_version").fetchone()[0]
            with sqlite3.connect(database) as other:
                other.execute("INSERT INTO sse_events(ts,type,session,agent_id,payload) VALUES (?,?,?,?,?)",
                              (harness.BASE_MS, "synthetic-unrelated", "other-native", "other-agent", "{}"))
            after = real_prepare(writer._database, corpus.AGENT, corpus.SESSION, [], raw_only=True)
            assert writer._database.execute("PRAGMA data_version").fetchone()[0] != version
            assert before == after
            observations.append(lane)
            injected = True

        def prepare(writer, *args, **kwargs):
            nonlocal probe_seen, prefix_changed
            if lane == "candidate" and kwargs.get("guard_carry"):
                probe_seen = True
                if intervention == "prefix_before_probe":
                    assert not writer.in_transaction
                    edit_prefix(database)
                    prefix_changed = True
            return real_prepare(writer, *args, **kwargs)

        def execute(writer, *args, **kwargs):
            nonlocal prefix_changed, final_writer
            if (lane == "candidate" and rollback_control and probe_seen
                    and writer.locked and writer.began_at is None):
                final_writer = True
            if (lane == "candidate" and intervention == "prefix_before_writer" and probe_seen
                    and writer.locked and writer.began_at is None and not prefix_changed):
                assert not writer.in_transaction  # real commit BEFORE target BEGIN, never after lock
                edit_prefix(database)
                prefix_changed = True
            return real_execute(writer, *args, **kwargs)

        def checkpoint(*args, **kwargs):
            nonlocal checkpoint_attempts
            checkpoint_attempts += 1
            result = real_checkpoint(*args, **kwargs)
            if intervention == "checkpoint_failure":
                raise sqlite3.OperationalError("synthetic checkpoint failure after actual write")
            return result

        def diagnostic(event, exc, *args, **kwargs):
            if intervention == "diagnostic_control" and event == "transcriptCheckpointRecertifySkip":
                raise diagnostic_control
            return real_log_exception(event, exc, *args, **kwargs)

        with monkeypatch.context() as selected:
            selected.setattr(message_writes._ImportWriter, "yield_writer", unrelated_commit)
            selected.setattr(message_writes, "_prepare_import_prelude", prepare)
            selected.setattr(message_writes._ImportWriter, "execute", execute)
            selected.setattr(inc, "write_checkpoint", checkpoint)
            if lane == "candidate" and rollback_control:
                selected.setattr(message_store, "conn", provided_connection)
                selected.setattr(host_log, "log_exception", diagnostic)

                def operation():
                    nonlocal operation_calls
                    operation_calls += 1
                    return harness.run_lane(database, transcript, backend, lane, tick=0,
                                            faults={"single_row_batches": True}, close=False)

                try:
                    expected_type = (BaseException if intervention == "caught_caller_control"
                                     else type(expected_control) if expected_control is not None else RuntimeError)
                    with pytest.raises(expected_type) as failure:
                        if intervention == "caught_caller_control":
                            try:
                                raise caller_control
                            except KeyboardInterrupt as already_handled:
                                assert already_handled is caller_control
                                db.retry_locked(operation, retries=2, sleep_sec=0)
                        else:
                            db.retry_locked(operation, retries=2, sleep_sec=0)
                    if expected_control is diagnostic_control:
                        assert failure.value is diagnostic_control
                        assert failure.value.__cause__ is cleanup_error
                    elif expected_control is primary_error:
                        assert failure.value is primary_error
                        assert failure.value.__cause__ is cleanup_error
                    elif expected_control is cleanup_error:
                        assert failure.value is cleanup_error
                        assert failure.value.__cause__ is primary_error
                    else:
                        assert isinstance(failure.value, RuntimeError)
                        assert failure.value is not caller_control
                        assert str(failure.value) == "checkpoint recertification cleanup uncertain"
                        assert failure.value.primary_error is primary_error
                        assert failure.value.cleanup_error is cleanup_error
                        assert failure.value.__cause__ is cleanup_error
                    assert operation_calls == cleanup_attempts == 1
                    assert native_holder[0].in_transaction
                    assert checkpoint_attempts == 0
                finally:
                    try:
                        if native_holder:
                            native_holder[0].rollback()  # independent real terminal cleanup, never proxy
                            assert not native_holder[0].in_transaction
                    finally:
                        db.close_local()
            else:
                report = harness.run_lane(database, transcript, backend, lane, tick=0,
                                          faults={"single_row_batches": True})
        assert injected
        if lane == "candidate" and not rollback_control:
            assert report["apply_mode"] == "full"
    assert observations == ["reference", "candidate"]
    if intervention.startswith("prefix_"):
        assert probe_seen and prefix_changed, "new read-only/writer boundary was not exercised"
        edit_prefix(reference)  # independent reference preserves the same legitimate foreign edit
    harness.assert_product_equal(reference, candidate)
    db.reset_for_tests(candidate)
    cp = inc.load_checkpoint(db.conn(), corpus.AGENT, corpus.SESSION, str(transcript))
    if intervention != "unrelated":
        assert cp is None, "failed recertification published a stale or rolled-back checkpoint"
        if intervention == "checkpoint_failure":
            assert probe_seen and checkpoint_attempts == 1, "checkpoint failure was retried or never reached"
        elif rollback_control:
            assert probe_seen and final_writer and operation_calls == 1
        return
    assert cp is not None, "unrelated foreign commit stranded a completed full-import frontier"
    assert cp.complete_size == transcript.stat().st_size
    with transcript.open("ab") as output:
        output.write(harness.append_bytes(backend, 20))
    harness.run_lane(reference, transcript, backend, "reference", tick=1)
    next_tick = harness.run_lane(candidate, transcript, backend, "candidate", tick=1)
    assert next_tick["apply_mode"] == "tail"
    harness.assert_product_equal(reference, candidate)


@pytest.mark.parametrize("backend", harness.BACKENDS)
@pytest.mark.parametrize("change", ["append", "prefix_rewrite", "same_mtime_rewrite",
                                     "midread_same_mtime_rewrite", "rotation", "partial_append",
                                     "rotation_same_size", "midread_rotation", "midread_rotation_same_size",
                                     "publication_rotation", "publication_rotation_same_size",
                                     "postcheckpoint_rotation", "postcheckpoint_rotation_same_size"])
def test_full_bootstrap_certifies_only_the_actual_consumed_prefix(tmp_path, backend, change, monkeypatch):
    """Growth certifies consumed bytes; earlier-byte edits and replacement do not."""
    import os
    import pathlib
    from lib import transcript_incremental as inc, message_writes

    reference, candidate = _clones(tmp_path, backend)
    transcript = tmp_path / "synthetic.jsonl"
    initial = b"".join(harness.append_bytes(backend, i) for i in range(20))
    delta = harness.append_bytes(backend, 20)
    transcript.write_bytes(initial)
    harness.run_lane(reference, transcript, backend, "reference", tick=0)
    real_parse = backends.parse_turns
    consumed = []
    changed = False

    def mutate():
        nonlocal changed
        stat = transcript.stat()
        changed = True
        if change in ("append", "partial_append"):
            with transcript.open("ab") as output:
                output.write(delta if change == "append" else delta[:-40])
        elif "rotation" in change:
            rewritten = initial.replace(b"synthetic request 0", b"synthetic request X", 1)
            assert rewritten != initial and len(rewritten) == len(initial)
            staged = tmp_path / "canonical.staged.jsonl"
            staged.write_bytes(rewritten if change.endswith("same_size") else rewritten + delta)
            os.utime(staged, ns=(stat.st_atime_ns, stat.st_mtime_ns))
            os.replace(staged, transcript)  # native migration's rename OVER same path
            assert transcript.stat().st_ino != stat.st_ino
            if change.endswith("same_size"):
                assert transcript.stat().st_size == stat.st_size
                assert transcript.stat().st_mtime_ns == stat.st_mtime_ns
        else:
            rewritten = initial.replace(b"synthetic request 0", b"synthetic request X", 1)
            assert len(rewritten) == len(initial) and rewritten != initial
            transcript.write_bytes(rewritten)
            if change in ("same_mtime_rewrite", "midread_same_mtime_rewrite"):
                os.utime(transcript, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        if "rotation" not in change:
            assert transcript.stat().st_ino == stat.st_ino

    class AfterUserLine:
        def __init__(self, native):
            self.native = native

        def __getattr__(self, name):
            return getattr(self.native, name)

        def __enter__(self):
            self.native.__enter__()
            return self

        def __exit__(self, *args):
            return self.native.__exit__(*args)

        def __iter__(self):
            return self

        def __next__(self):
            line = next(self.native)
            if not changed and "synthetic request 0" in line:
                mutate()  # actual original user line has already been consumed
            return line

    def intercept_open(original):
        def opened(path, *args, **kwargs):
            native = original(path, *args, **kwargs)
            return AfterUserLine(native) if os.fspath(path) == str(transcript) else native
        return opened

    def mutate_after_real_parse(selected_backend, path):
        if not consumed and os.fspath(path) == str(transcript):
            if change.startswith("midread_"):
                with monkeypatch.context() as reading:
                    reading.setattr(pathlib.Path, "open", intercept_open(pathlib.Path.open))
                    reading.setattr(inc._RangePath, "open", intercept_open(inc._RangePath.open))
                    turns = real_parse(selected_backend, path)
                assert changed, "native reader did not pass its real user line"
            else:
                turns = real_parse(selected_backend, path)
                if not change.startswith(("publication_", "postcheckpoint_")):
                    mutate()
            consumed.append(len(turns))
            return turns
        return real_parse(selected_backend, path)

    real_yield, real_prepare = message_writes._ImportWriter.yield_writer, message_writes._prepare_import_prelude
    real_execute, real_checkpoint = message_writes._ImportWriter.execute, inc.write_checkpoint
    yielded = probe_seen = False

    def unrelated_yield(writer):
        nonlocal yielded
        real_yield(writer)
        if not yielded:
            assert not writer.in_transaction
            before_raw = real_prepare(writer._database, corpus.AGENT, corpus.SESSION, [], raw_only=True)
            with sqlite3.connect(candidate) as other:
                other.execute("INSERT INTO sse_events(ts,type,session,agent_id,payload) VALUES (?,?,?,?,?)",
                              (harness.BASE_MS, "synthetic-unrelated", "other-native", "other-agent", "{}"))
            assert before_raw == real_prepare(writer._database, corpus.AGENT, corpus.SESSION, [], raw_only=True)
            yielded = True

    def prepared(writer, *args, **kwargs):
        nonlocal probe_seen
        if kwargs.get("guard_carry"):
            probe_seen = True
        return real_prepare(writer, *args, **kwargs)

    def execute(writer, *args, **kwargs):
        if probe_seen and writer.locked and writer.began_at is None and not changed:
            assert not writer.in_transaction  # after probe/hash, BEFORE actual BEGIN
            mutate()
        return real_execute(writer, *args, **kwargs)

    def checkpoint(*args, **kwargs):
        result = real_checkpoint(*args, **kwargs)
        if not changed:
            mutate()  # checkpoint written, but actual transaction not committed
        return result

    with monkeypatch.context() as selected:
        selected.setattr(backends, "parse_turns", mutate_after_real_parse)
        faults = None
        if change.startswith("publication_"):
            selected.setattr(message_writes._ImportWriter, "yield_writer", unrelated_yield)
            selected.setattr(message_writes, "_prepare_import_prelude", prepared)
            selected.setattr(message_writes._ImportWriter, "execute", execute)
            faults = {"single_row_batches": True}
        if change.startswith("postcheckpoint_"):
            selected.setattr(inc, "write_checkpoint", checkpoint)
        actual = harness.run_lane(candidate, transcript, backend, "candidate", tick=0, faults=faults)
    if change.startswith("publication_"):
        assert yielded and probe_seen, "actual fresh-probe/writer replacement phase was not reached"
    assert consumed and changed and actual["apply_mode"] == "full"
    if change.startswith("publication_"):
        # The unrelated commit is an input intervention, not an importer
        # product: give the independent full reference that same external row.
        with sqlite3.connect(reference) as other:
            other.execute("INSERT INTO sse_events(ts,type,session,agent_id,payload) VALUES (?,?,?,?,?)",
                          (harness.BASE_MS, "synthetic-unrelated", "other-native", "other-agent", "{}"))
    harness.assert_product_equal(reference, candidate)
    db.reset_for_tests(candidate)
    checkpoint = inc.load_checkpoint(db.conn(), corpus.AGENT, corpus.SESSION, str(transcript))
    if change in ("append", "partial_append"):
        assert transcript.stat().st_size > len(initial)
        assert checkpoint is not None, "pure growth discarded the actual parsed-prefix frontier"
        assert checkpoint.complete_size == len(initial)
        assert checkpoint.turn_count == consumed[0]
    else:
        if change.startswith("postcheckpoint_"):
            assert checkpoint is None or checkpoint.inode != transcript.stat().st_ino
        else:
            assert checkpoint is None, "an earlier-byte rewrite or replacement was falsely certified"
        retained = inc._TAIL_STATES.peek((str(candidate), corpus.AGENT, corpus.SESSION, str(transcript)))
        assert retained is None or retained[1] is None, "replaced source retained old process carry"
        assert actual.get("_clarp_transcript_completion_v1") == "deferred", "known invalid bytes became fresh"
    if change == "partial_append":
        with transcript.open("ab") as output:
            output.write(delta[-40:])
    harness.run_lane(reference, transcript, backend, "reference", tick=1)
    following = harness.run_lane(candidate, transcript, backend, "candidate", tick=1)
    assert following["apply_mode"] == ("tail" if change in ("append", "partial_append") else "full")
    harness.assert_product_equal(reference, candidate)


@pytest.mark.parametrize("backend", harness.BACKENDS)
@pytest.mark.parametrize("same_size", [False, True])
def test_staged_rollout_replacement_after_real_checkpoint_reparses_all_products(tmp_path, backend, same_size):
    """Same pathname/native conversation is not the checkpoint's file identity."""
    import os
    from lib import transcript_incremental as inc

    reference, candidate = _clones(tmp_path, backend)
    transcript = tmp_path / "synthetic.jsonl"
    initial = b"".join(harness.append_bytes(backend, i) for i in range(20))
    transcript.write_bytes(initial)
    try:
        harness.isolated_lane(reference, transcript, backend, "reference", tick=0)
        first = harness.run_lane(candidate, transcript, backend, "candidate", tick=0, close=False)
        assert first["apply_mode"] == "full"
        harness.assert_product_equal(reference, candidate)
        harness.assert_checkpoint_parse_equal(candidate, transcript, backend)
        old = inc.load_checkpoint(db.conn(), corpus.AGENT, corpus.SESSION, str(transcript))
        assert old is not None
        key = (str(candidate), corpus.AGENT, corpus.SESSION, str(transcript))
        retained = inc._TAIL_STATES.peek(key)
        assert retained is not None and retained[1] is not None, "real native frontier prerequisite failed"
        stat = transcript.stat()
        rewritten = initial.replace(b"synthetic request 0", b"synthetic request X", 1)
        assert rewritten != initial and len(rewritten) == len(initial)
        staged = tmp_path / "canonical.staged.jsonl"
        staged.write_bytes(rewritten if same_size else rewritten + harness.append_bytes(backend, 20))
        os.utime(staged, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        os.replace(staged, transcript)
        assert transcript.stat().st_ino != old.inode
        if same_size:
            assert transcript.stat().st_size == old.size and transcript.stat().st_mtime_ns == old.mtime_ns
        harness.isolated_lane(reference, transcript, backend, "reference", tick=1)
        actual = harness.run_lane(candidate, transcript, backend, "candidate", tick=1,
                                  initialize=False, close=False)
        assert actual["apply_mode"] == "full"
        assert {"event": "transcriptTailDecision", "code": "full_inode", "count": 1} in actual["tail_events"]
        assert actual.get("_clarp_transcript_completion_v1") != "deferred"
        harness.assert_product_equal(reference, candidate)
        current = inc.load_checkpoint(db.conn(), corpus.AGENT, corpus.SESSION, str(transcript))
        assert current is not None and current.inode == transcript.stat().st_ino
        assert current.inode != old.inode
        retained = inc._TAIL_STATES.peek(key)
        assert retained is not None and retained[1].checkpoint.inode == current.inode
    finally:
        db.close_local()
