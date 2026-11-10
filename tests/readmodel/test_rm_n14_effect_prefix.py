"""Effect-bearing prefixes: settled effects admit a frontier, open ones stay full.

Settled: heartbeat and leader-tick turns. Open: sub-agent cells, <team>
blocks and dream turns.

Every tick runs a full-replay reference lane next to the incremental
candidate and compares every product table, side-effect tables included, so
a lost, duplicated or unretried effect shows up as a product difference.
"""
from __future__ import annotations

import json
import pathlib
import sqlite3

import pytest

from lib.dreaming_settings import DREAMING_OK
from lib.heartbeat import HEARTBEAT_OK, HEARTBEAT_PROMPT
from lib.team_leader import LEADER_NOOP, TICK_PROMPT
from tests.readmodel import corpus
from tests.readmodel import rm_n14_harness as harness
from tests.readmodel.test_rm_n14_equivalence import _clones


def _escaped(text: str) -> bytes:
    return json.dumps(text)[1:-1].encode()


_AUTOMATIONS = {
    "heartbeat": (HEARTBEAT_PROMPT, HEARTBEAT_OK),
    "leader_tick": (TICK_PROMPT, LEADER_NOOP),
    "dreaming": ("Deep Dreaming contract: synthetic round", DREAMING_OK),
}
# The kinds a frontier is admitted over; a dream turn keeps the full path.
_SETTLED = ("heartbeat", "leader_tick")


def _automation_history(backend: str, kind: str = "heartbeat", count: int = 20) -> bytes:
    """A long history whose first turn is an answered automation turn."""
    prompt, ack = _AUTOMATIONS[kind]
    return (b"".join(harness.append_bytes(backend, i) for i in range(count))
            .replace(b"synthetic request 0", _escaped(prompt), 1)
            .replace(b"synthetic answer 0", _escaped(ack), 1))


def _claude_plain_turn(prompt: str, answer: str) -> bytes:
    """Turn 0 is one prompt and one reply, nothing in between."""
    stamp = "2026-10-11T00:00:00.000Z"
    records = [{"type": role, "timestamp": stamp, "message": {"role": role, "content": text}}
               for role, text in (("user", prompt), ("assistant", answer))]
    return b"".join(json.dumps(r, separators=(",", ":")).encode() + b"\n" for r in records)


def _claude_agent_turn() -> bytes:
    """Turn 0 calls Claude's Agent tool (call c0, child agent a0)."""
    stamp = "2026-10-11T00:00:00.000Z"

    def record(role, content, **extra):
        return {"type": role, "timestamp": stamp,
                "message": {"role": role, "content": content}, **extra}
    result = {"status": "completed", "agentId": "a0", "totalToolUseCount": 2}
    records = [
        record("user", "synthetic request 0"),
        record("assistant", [{"type": "tool_use", "id": "c0", "name": "Agent",
                              "input": {"description": "look around", "prompt": "look"}}]),
        record("user", [{"type": "tool_result", "tool_use_id": "c0", "content": "launched"}],
               toolUseResult=result),
        record("assistant", [{"type": "text", "text": "synthetic answer 0"}]),
    ]
    return b"".join(json.dumps(r, separators=(",", ":")).encode() + b"\n" for r in records)


def _discover_child(transcript: pathlib.Path) -> None:
    directory = transcript.parent / transcript.stem / "subagents"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "agent-a0.jsonl").write_text("{}\n", encoding="utf-8")
    (directory / "agent-a0.meta.json").write_text('{"toolUseId": "c0"}', encoding="utf-8")


def _codes(report, event):
    return [e["code"] for e in report["tail_events"] if e["event"] == event]


def _decision(report):
    decisions = [e for e in report["tail_events"] if e["event"] == "transcriptTailDecision"]
    assert len(decisions) == 1
    return decisions[0]


def _append(transcript, data):
    with transcript.open("ab") as output:
        output.write(data)


def _drop_checkpoint(database):
    with sqlite3.connect(database) as connection:
        connection.execute("DELETE FROM transcript_import_checkpoints")


def _transcript_rows(database):
    with sqlite3.connect(database) as connection:
        return connection.execute(
            "SELECT count(*), count(DISTINCT seq) FROM messages "
            "WHERE source_file NOT LIKE 'client:%' AND source_file NOT LIKE 'live:%' "
            "AND source_file NOT LIKE 'final:%'").fetchone()


@pytest.mark.parametrize("backend", harness.BACKENDS)
@pytest.mark.parametrize("kind", _SETTLED)
def test_old_settled_automation_admits_a_frontier_and_full_tail_full_keeps_rows(
        tmp_path, backend, kind):
    reference, candidate = _clones(tmp_path, backend)
    transcript = tmp_path / "synthetic.jsonl"
    transcript.write_bytes(_automation_history(backend, kind))
    with harness.PersistentLane(reference, transcript, backend, "reference") as full:
        with harness.PersistentLane(candidate, transcript, backend, "candidate") as actual:
            full.tick(0)
            bootstrap = actual.tick(0)
            harness.assert_product_equal(reference, candidate)
            retention = _codes(bootstrap, "transcriptTailRetention")
            assert "checkpoint_closed_effects" in retention
            assert "effect_bearing_prefix" not in retention
            harness.assert_checkpoint_parse_equal(candidate, transcript, backend)
            for tick in (1, 2):
                _append(transcript, harness.append_bytes(backend, 19 + tick))
                full.tick(tick)
                report = actual.tick(tick)
                assert report["apply_mode"] == "tail"
                assert _decision(report)["code"] == "tail_applied"
                harness.assert_product_equal(reference, candidate)
            # Back to a full import over the tails: nothing lost or doubled.
            _drop_checkpoint(candidate)
            _append(transcript, harness.append_bytes(backend, 22))
            full.tick(3)
            report = actual.tick(3)
            assert report["apply_mode"] == "full"
            harness.assert_product_equal(reference, candidate)
            harness.assert_checkpoint_parse_equal(candidate, transcript, backend)
            rows, seqs = _transcript_rows(candidate)
            assert rows == seqs
            _append(transcript, harness.append_bytes(backend, 23))
            full.tick(4)
            assert actual.tick(4)["apply_mode"] == "tail"
            harness.assert_product_equal(reference, candidate)
            if kind == "heartbeat":
                # Accounted once by the import that stored the turn (its first
                # reply counts as activity), never again by a later import.
                with sqlite3.connect(candidate) as connection:
                    assert connection.execute(
                        "SELECT outcome FROM heartbeat_accounting").fetchall() == [("activity",)]


def test_heartbeat_noop_is_counted_once_across_full_tail_full(tmp_path):
    backend = "claude"
    reference, candidate = _clones(tmp_path, backend)
    transcript = tmp_path / "synthetic.jsonl"
    transcript.write_bytes(_claude_plain_turn(HEARTBEAT_PROMPT, HEARTBEAT_OK) + b"".join(
        harness.append_bytes(backend, i) for i in range(1, 20)))
    with harness.PersistentLane(reference, transcript, backend, "reference") as full:
        with harness.PersistentLane(candidate, transcript, backend, "candidate") as actual:
            full.tick(0)
            assert "checkpoint_closed_effects" in _codes(actual.tick(0), "transcriptTailRetention")
            _append(transcript, harness.append_bytes(backend, 20))
            full.tick(1)
            assert actual.tick(1)["apply_mode"] == "tail"
            _drop_checkpoint(candidate)
            _append(transcript, harness.append_bytes(backend, 21))
            full.tick(2)
            assert actual.tick(2)["apply_mode"] == "full"
            _append(transcript, harness.append_bytes(backend, 22))
            full.tick(3)
            assert actual.tick(3)["apply_mode"] == "tail"
            harness.assert_product_equal(reference, candidate)
            for database in (reference, candidate):
                with sqlite3.connect(database) as connection:
                    assert connection.execute(
                        "SELECT outcome FROM heartbeat_accounting").fetchall() == [("noop",)]
                    assert connection.execute(
                        "SELECT noop_streak FROM heartbeat_state").fetchall() == [(1,)]


@pytest.mark.parametrize("backend", harness.BACKENDS)
@pytest.mark.parametrize("newer_run", [False, True])
def test_dream_prefix_stays_full_so_a_replay_acts_as_the_full_import_does(
        tmp_path, backend, newer_run):
    reference, candidate = _clones(tmp_path, backend)
    transcript = tmp_path / "synthetic.jsonl"
    transcript.write_bytes(_automation_history(backend, "dreaming"))
    with harness.PersistentLane(reference, transcript, backend, "reference") as full:
        with harness.PersistentLane(candidate, transcript, backend, "candidate") as actual:
            full.tick(0)
            retention = _codes(actual.tick(0), "transcriptTailRetention")
            assert "effect_bearing_prefix" in retention
            assert "checkpoint_closed_effects" not in retention
            harness.assert_product_equal(reference, candidate)
            if newer_run:
                # A later night is running when the old DREAMING_OK is replayed.
                # What the replay does to it is not this rule's to change.
                for database in (reference, candidate):
                    with sqlite3.connect(database) as connection:
                        connection.execute(
                            "INSERT INTO dream_runs(run_id, agent_id, session, local_date, timezone,"
                            " timezone_source, status, stage, min_directions, planned_directions,"
                            " planned_rounds, target_tokens, target_minutes, started_at, updated_at)"
                            " VALUES ('run-later', ?, ?, '2026-10-12', 'UTC', 'test', 'active',"
                            " 'fanout', 1, 1, 1, 1, 1, 2, 2)", (corpus.AGENT, corpus.AGENT))
            _append(transcript, harness.append_bytes(backend, 20))
            full.tick(1)
            report = actual.tick(1)
            assert report["apply_mode"] == "full"
            assert _decision(report).get("reason") == "bootstrap_effect"
            harness.assert_product_equal(reference, candidate)
            if newer_run:
                statuses = []
                for database in (reference, candidate):
                    with sqlite3.connect(database) as connection:
                        statuses.append(connection.execute(
                            "SELECT status FROM dream_runs WHERE run_id='run-later'").fetchone()[0])
                assert statuses[0] == statuses[1]


@pytest.mark.parametrize("child_found", [True, False])
def test_claude_subagent_prefix_stays_full_and_a_later_child_is_replayed(tmp_path, child_found):
    backend = "claude"
    reference, candidate = _clones(tmp_path, backend)
    transcript = tmp_path / "synthetic.jsonl"
    transcript.write_bytes(_claude_agent_turn() + b"".join(
        harness.append_bytes(backend, i) for i in range(1, 20)))
    child = transcript.parent / "synthetic" / "subagents" / "agent-a0.jsonl"
    if child_found:
        _discover_child(transcript)
    with harness.PersistentLane(reference, transcript, backend, "reference") as full:
        with harness.PersistentLane(candidate, transcript, backend, "candidate") as actual:
            full.tick(0)
            retention = _codes(actual.tick(0), "transcriptTailRetention")
            assert "effect_bearing_prefix" in retention
            assert "checkpoint_closed_effects" not in retention
            # The directory changes with no change to the parent's bytes.
            if child_found:
                child.unlink()
                child.with_name("agent-a0.meta.json").unlink()
            else:
                _discover_child(transcript)
            _append(transcript, harness.append_bytes(backend, 20))
            full.tick(1)
            report = actual.tick(1)
            assert report["apply_mode"] == "full"
            assert _decision(report).get("reason") == "bootstrap_effect"
            harness.assert_product_equal(reference, candidate)
            with sqlite3.connect(candidate) as connection:
                stored = [json.loads(cells) for (cells,) in connection.execute(
                    "SELECT display_cells_json FROM messages WHERE display_cells_json LIKE '%subagents%'")]
            assert [cell["transcript_path"] for row in stored for cell in row] == [
                "" if child_found else str(child)]


@pytest.mark.parametrize("backend", harness.BACKENDS)
def test_settled_heartbeat_does_not_excuse_a_team_block(tmp_path, backend):
    reference, candidate = _clones(tmp_path, backend)
    transcript = tmp_path / "synthetic.jsonl"
    transcript.write_bytes(_automation_history(backend).replace(
        b"synthetic answer 1", b"<team>synthetic broadcast</team>", 1))
    with harness.PersistentLane(reference, transcript, backend, "reference") as full:
        with harness.PersistentLane(candidate, transcript, backend, "candidate") as actual:
            full.tick(0)
            bootstrap = actual.tick(0)
            assert "effect_bearing_prefix" in _codes(bootstrap, "transcriptTailRetention")
            _append(transcript, harness.append_bytes(backend, 20))
            full.tick(1)
            report = actual.tick(1)
            assert report["apply_mode"] == "full"
            assert _decision(report).get("reason") == "bootstrap_effect"
            harness.assert_product_equal(reference, candidate)
