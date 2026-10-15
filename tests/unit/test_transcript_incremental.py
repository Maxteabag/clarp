"""Incremental transcript import in shadow mode (lib.transcript_incremental).

Transcripts here are synthetic and redacted, shaped like recorded Claude,
Codex and agy files. The equivalence tests replay a transcript as it grows,
cut at every record boundary and inside records: database A runs the real
import (the full import plus the shadow, which writes checkpoints); database B
applies the incremental import from A's checkpoint wherever ``decide`` allows
it and a full import otherwise. After every cut both databases must hold the
same rows, heads, revisions and clock.
"""
from __future__ import annotations

import json
import os
import pathlib
import sqlite3
import threading

import pytest

from lib import db, eventlog, message_store, message_writes
from lib import transcript_incremental as inc
from lib.heartbeat import HEARTBEAT_PROMPT

AGENT = "agent-incr"
SESSION = "bs-incr"
SENDER = "agent-sender"


def _line(record: dict) -> bytes:
    return json.dumps(record, separators=(",", ":")).encode() + b"\n"


def request_text(backend: str, r: int) -> str:
    """Requests 0 and 3 send the same words, as a repeated "continue" does:
    only the send-order cursor tells which client row each copy belongs to."""
    if r in (0, 3):
        return "continue"
    return f"request {r} about [redacted]" if backend == "claude" else f"request {r}"


def _ts(i: int) -> str:
    return f"2026-10-11T08:{i // 60:02d}:{i % 60:02d}.000Z"


# ---- transcripts shaped like the recorded ones ------------------------------

def claude_lines(requests=5, *, background_result_at=None) -> list[bytes]:
    records: list[dict] = []
    parent = [None]

    def add(record):
        record["uuid"] = f"uuid-{len(records)}"
        record["parentUuid"] = parent[0]
        record.setdefault("timestamp", _ts(len(records)))
        record.setdefault("sessionId", "redacted")
        parent[0] = record["uuid"]
        records.append(record)

    for r in range(requests):
        text = HEARTBEAT_PROMPT if r == 2 else request_text("claude", r)
        add({"type": "user", "message": {"role": "user", "content": text}})
        tools = [{"type": "tool_use", "id": f"toolu_{r}", "name": "Bash",
                  "input": {"command": f"ls /redacted/{r}"}}]
        if r == 0:
            tools.append({"type": "tool_use", "id": "toolu_bg", "name": "Bash",
                          "input": {"command": "sleep 600", "run_in_background": True}})
        add({"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": f"Looking at {r}."}, *tools]}})
        add({"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": f"toolu_{r}", "content": "a\nb"}]}})
        if background_result_at == r:
            add({"type": "user", "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_bg", "content": "late"}]}})
        add({"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": f"Done with {r}. <speak>Done {r}.</speak>"}]}})
    return [_line(r) for r in records]


def codex_lines(requests=5, *, late_output_at=None) -> list[bytes]:
    out = [{"timestamp": _ts(0), "type": "session_meta",
            "payload": {"id": "redacted", "cwd": "/redacted"}}]
    for r in range(requests):
        turn = f"turn-{r}"
        out += [
            {"timestamp": _ts(r * 10 + 1), "type": "event_msg",
             "payload": {"type": "task_started", "turn_id": turn}},
            {"timestamp": _ts(r * 10 + 1), "type": "turn_context",
             "payload": {"turn_id": turn, "cwd": "/redacted"}},
            {"timestamp": _ts(r * 10 + 2), "type": "response_item",
             "payload": {"type": "message", "role": "user",
                         "content": [{"type": "input_text", "text": request_text("codex", r)}]}},
            {"timestamp": _ts(r * 10 + 2), "type": "event_msg",
             "payload": {"type": "user_message", "message": request_text("codex", r)}},
            {"timestamp": _ts(r * 10 + 3), "type": "response_item",
             "payload": {"type": "reasoning", "summary": [], "turn_id": turn}},
            {"timestamp": _ts(r * 10 + 3), "type": "event_msg",
             "payload": {"type": "agent_message", "message": f"Checking {r}.",
                         "phase": "commentary", "turn_id": turn}},
            {"timestamp": _ts(r * 10 + 4), "type": "response_item",
             "payload": {"type": "function_call", "name": "exec_command",
                         "arguments": json.dumps({"cmd": f"ls /redacted/{r}"}),
                         "call_id": f"call_{r}", "turn_id": turn}},
            {"timestamp": _ts(r * 10 + 5), "type": "response_item",
             "payload": {"type": "function_call_output", "call_id": f"call_{r}",
                         "output": "Process exited with code 0\nOutput:\na\nb",
                         "turn_id": turn}},
        ]
        if late_output_at == r:
            out.append({"timestamp": _ts(r * 10 + 5), "type": "response_item",
                        "payload": {"type": "function_call_output", "call_id": "call_0",
                                    "output": "late", "turn_id": turn}})
        out += [
            {"timestamp": _ts(r * 10 + 6), "type": "event_msg",
             "payload": {"type": "agent_message", "message": f"Done with {r}.",
                         "phase": "final_answer", "turn_id": turn}},
            {"timestamp": _ts(r * 10 + 6), "type": "response_item",
             "payload": {"type": "message", "role": "assistant", "phase": "final_answer",
                         "content": [{"type": "output_text", "text": f"Done with {r}."}],
                         "turn_id": turn}},
            {"timestamp": _ts(r * 10 + 7), "type": "event_msg",
             "payload": {"type": "task_complete", "turn_id": turn}},
        ]
    return [_line(r) for r in out]


def agy_lines(requests=5, *, out_of_order_at=None, background_end_at=None) -> list[bytes]:
    rows = []
    step = 0
    for r in range(requests):
        rows.append({"step_index": step, "type": "USER_INPUT", "created_at": _ts(step),
                     "content": f"<USER_REQUEST>\n{request_text('agy', r)}\n</USER_REQUEST>"})
        rows.append({"step_index": step + 1, "type": "PLANNER_RESPONSE",
                     "created_at": _ts(step + 1), "content": f"Looking at {r}.",
                     "thinking": "Plan the listing.",
                     "tool_calls": [{"name": "run_command",
                                     "args": {"command": f"ls /redacted/{r}",
                                              "toolSummary": "list files"}}]})
        answer = ("Created At: x\nOutput:\nrunning in background tasks/task-7.log"
                  if r == 0 else "Created At: x\nCompleted At: y\nOutput:\na\nb")
        rows.append({"step_index": step + 2, "type": "GENERIC", "created_at": _ts(step + 2),
                     "content": answer, "status": "RUNNING" if r == 0 else "DONE"})
        rows.append({"step_index": step + 3, "type": "PLANNER_RESPONSE",
                     "created_at": _ts(step + 3), "content": f"Done with {r}."})
        if background_end_at == r:
            rows.append({"step_index": step + 4, "type": "SYSTEM_MESSAGE",
                         "created_at": _ts(step + 4),
                         "content": "Task /tasks/task-7 finished with result: ok"})
        if out_of_order_at == r:
            rows.append({"step_index": 2, "type": "PLANNER_RESPONSE",
                         "created_at": _ts(step + 4), "content": "Filed late."})
        step += 5
    return [_line(r) for r in rows]


BUILDERS = {"claude": claude_lines, "codex": codex_lines, "agy": agy_lines}


def cut_points(lines: list[bytes]) -> list[int]:
    cuts, offset = [], 0
    for index, line in enumerate(lines):
        if index % 3 == 1:
            cuts.append(offset + len(line) // 2)      # inside a record
        offset += len(line)
        cuts.append(offset)
    return cuts


# ---- databases ----------------------------------------------------------------

def _prepare(path: pathlib.Path, backend: str) -> None:
    db.reset_for_tests(path)
    c = db.conn()
    for agent in (AGENT, SENDER):
        c.execute("INSERT INTO agents(agent_id, persona, voice_id, cwd, session, created_at, backend)"
                  " VALUES (?,?,?,?,?,?,?)", (agent, agent, "v", "/redacted", agent, 1, backend))
    c.commit()


def _seed(backend: str, clock: dict) -> None:
    """Rows other paths write before the transcript has them: client sends
    (one from another agent), a streamed reply to adopt, an agy turn whose
    final row the stream owns."""
    message_store.record_user_message(
        agent_id=AGENT, backend_session_id=SESSION, client_msg_id="c0",
        text=request_text(backend, 0))
    clock["ms"] += 1
    message_store.record_user_message(
        agent_id=AGENT, backend_session_id=SESSION, client_msg_id="c1",
        text=request_text(backend, 1), origin="agent", sender_agent_id=SENDER)
    clock["ms"] += 1
    message_store.record_user_message(
        agent_id=AGENT, backend_session_id=SESSION, client_msg_id="c3",
        text=request_text(backend, 3), origin="agent", sender_agent_id=SENDER)
    c = db.conn()
    rev = message_writes._next_revision(c)
    c.execute("""INSERT INTO messages(message_id, agent_id, backend_session_id, source_file,
                 seq, role, timestamp, text, updated_at, revision)
                 VALUES ('live-3', ?, ?, 'live:t3', -1000, 'assistant', ?, ?, 1, ?)""",
              (AGENT, SESSION, _ts(3), "Done with 3.", rev))
    if backend == "agy":
        c.execute("""INSERT INTO messages(message_id, agent_id, backend_session_id, source_file,
                     seq, role, timestamp, text, updated_at, revision)
                     VALUES ('final-agy', ?, ?, 'final:tr-agy', -2000, 'assistant', ?,
                             'Stream answer 1.', 1, ?)""",
                  (AGENT, SESSION, _ts(1), message_writes._next_revision(c)))
        c.execute("""INSERT INTO agy_turn_authority(agent_id, backend_session_id, trace_id,
                     assistant_start_ordinal, assistant_end_ordinal, terminal_status,
                     authoritative_message_id, updated_at)
                     VALUES (?, ?, 'tr-agy', 2, 4, 'done', 'final-agy', 1)""",
                  (AGENT, SESSION))
    c.commit()


def snapshot() -> dict:
    c = db.conn()
    return {
        "messages": [tuple(r) for r in c.execute("SELECT * FROM messages ORDER BY message_id")],
        "heads": [tuple(r) for r in c.execute("SELECT * FROM conversation_heads ORDER BY 1, 2")],
        "clock": c.execute("SELECT revision FROM message_clock").fetchone()[0],
    }


@pytest.fixture
def shadow_on(monkeypatch):
    conf = dict(inc._DEFAULTS, enabled=True, sample_every=1,
                max_cpu_seconds_per_hour=1e9, max_load_per_cpu=1e9,
                max_io_pressure=1e9, max_memory_pressure=1e9)
    monkeypatch.setattr(inc, "settings", lambda: dict(conf))
    monkeypatch.setattr(inc, "BUDGET", inc.ShadowBudget())
    events = []
    real_emit = eventlog.emit

    def emit(source, event, **kw):
        if event == "import_shadow":
            events.append(kw["detail"])
        return real_emit(source, event, **kw)
    monkeypatch.setattr(eventlog, "emit", emit)
    return events


@pytest.fixture
def clock(monkeypatch):
    state = {"ms": 1_791_700_000_000}
    monkeypatch.setattr(message_writes, "now_ms", lambda: state["ms"])
    return state


def _decide(cp, backend, path):
    return inc.decide(cp, backend, path, inc.identity(path), now_ms=cp.audited_at if cp else 0,
                      audit_every=10**9, audit_seconds=1e12, max_tail_bytes=1 << 30)


def _replay(tmp_path, backend, lines, clock, cuts=None):
    """Grow the transcript cut by cut; A imports (with the shadow), B imports
    incrementally from A's checkpoint. Returns per-cut (decision, verdict)."""
    a, b = tmp_path / "a.sqlite", tmp_path / "b.sqlite"
    for path in (a, b):
        _prepare(path, backend)
        _seed(backend, dict(clock))
    transcript = tmp_path / f"{backend}.jsonl"
    data = b"".join(lines)
    transcript.write_bytes(b"")
    results = []
    for index, cut in enumerate(cuts or cut_points(lines)):
        with open(transcript, "r+b") as f:     # append in place: same inode
            f.seek(0, os.SEEK_END)
            f.write(data[f.tell():cut])
        clock["ms"] += 1000
        db.reset_for_tests(a)
        cp = inc.load_checkpoint(db.conn(), AGENT, SESSION, str(transcript))
        inc.import_transcript(backend=backend, path=transcript, agent_id=AGENT,
                              backend_session_id=SESSION)
        expected = snapshot()
        db.reset_for_tests(b)
        decision = _decide(cp, backend, transcript)
        if decision:
            message_store.store_transcript_turns(
                agent_id=AGENT, backend_session_id=SESSION, source_file=str(transcript),
                turns=inc.parse_range(backend, transcript, 0, inc.identity(transcript).size))
        else:
            tail = inc.parse_range(backend, transcript, cp.boundary_offset,
                                   inc.identity(transcript).size)
            message_store_tail(transcript, tail, cp)
        assert snapshot() == expected, f"{backend} cut {index} at byte {cut} ({decision or 'incremental'})"
        results.append(decision)
    return results


def message_store_tail(transcript, tail, cp):
    message_writes.store_transcript_tail(
        agent_id=AGENT, backend_session_id=SESSION, source_file=str(transcript),
        turns=tail, start_seq=cp.boundary_seq, carry=cp.store_carry)


# ---- equivalence ------------------------------------------------------------

@pytest.mark.parametrize("backend", ["claude", "codex", "agy"])
def test_incremental_matches_full_import_at_every_cut(tmp_path, backend, clock, shadow_on):
    decisions = _replay(tmp_path, backend, BUILDERS[backend](), clock)
    incremental = decisions.count("")
    assert incremental >= len(decisions) // 2, decisions
    verdicts = [e["verdict"] for e in shadow_on]
    assert not [v for v in verdicts if v.startswith(("diverge", "error"))], verdicts
    assert "match" in verdicts
    assert all("shadow_cpu_ms" in e and "hour_cpu_s" in e for e in shadow_on)


def test_late_completion_of_the_last_turn_is_reparsed(tmp_path, clock, shadow_on):
    """The current turn's tool result lands a tick after its call: the
    boundary turn is parsed again, so the call settles incrementally."""
    lines = claude_lines(3)
    ends = [sum(len(l) for l in lines[:i + 1]) for i in range(len(lines))]
    # Ticks at the end of request 1's tool call, then after its result.
    decisions = _replay(tmp_path, "claude", lines, clock, cuts=ends[:6] + [ends[6], ends[7]])
    assert decisions[-1] == "" and decisions[-2] == ""


# ---- full fallback ------------------------------------------------------------

def _checkpointed(tmp_path, backend, lines, upto, clock):
    _prepare(tmp_path / "a.sqlite", backend)
    transcript = tmp_path / f"{backend}.jsonl"
    transcript.write_bytes(b"".join(lines[:upto]))
    inc.import_transcript(backend=backend, path=transcript, agent_id=AGENT,
                          backend_session_id=SESSION)
    cp = inc.load_checkpoint(db.conn(), AGENT, SESSION, str(transcript))
    assert cp is not None
    return transcript, cp


def _append(path, lines):
    with open(path, "ab") as f:
        f.write(b"".join(lines))


@pytest.mark.parametrize("backend,builder,reason", [
    ("claude", lambda: claude_lines(4, background_result_at=2), "claude_late_tool_result"),
    ("codex", lambda: codex_lines(4, late_output_at=2), "codex_output_before_boundary"),
    ("agy", lambda: agy_lines(4, background_end_at=2), "agy_background_before_boundary"),
    ("agy", lambda: agy_lines(4, out_of_order_at=2), "agy_step_out_of_order"),
])
def test_a_record_reaching_before_the_boundary_forces_a_full_import(
        tmp_path, backend, builder, reason, clock, shadow_on):
    lines = builder()
    # Checkpoint after request 1, then append the rest including the late record.
    starts = [i for i, l in enumerate(lines) if inc._BOUNDARY_MARKERS[backend](l)]
    transcript, cp = _checkpointed(tmp_path, backend, lines, starts[2], clock)
    _append(transcript, lines[starts[2]:])
    assert _decide(cp, backend, transcript) == reason
    # And the replay stays equal: B falls back to the full import there.
    (tmp_path / "replay").mkdir()
    decisions = _replay(tmp_path / "replay", backend, lines, clock)
    assert reason in decisions


def test_edits_and_rewrites_of_earlier_records_force_a_full_import(tmp_path, clock, shadow_on):
    lines = claude_lines(6)
    starts = [i for i, l in enumerate(lines) if inc._BOUNDARY_MARKERS["claude"](l)]
    transcript, cp = _checkpointed(tmp_path, "claude", lines, starts[4] + 3, clock)
    data = transcript.read_bytes()

    def edited(at: int, new: bytes) -> str:
        transcript.write_bytes(data)                       # same inode, same bytes
        with open(transcript, "r+b") as f:
            f.seek(at)
            f.write(new)
        return _decide(cp, "claude", transcript)

    # Same-length metadata edit just before the boundary (the tail window).
    window = cp.boundary_offset - 40
    assert edited(window, b"X") == "prefix_rewritten"
    # Same-length edit at the very start (the head block).
    assert edited(10, b"Y") == "prefix_rewritten"
    # Edit inside the region already parsed after the boundary.
    assert edited(cp.boundary_offset + 20, b"Z") == "tail_rewritten"
    # Truncated (compaction to a shorter file).
    transcript.write_bytes(data[:cp.boundary_offset])
    assert _decide(cp, "claude", transcript) == "shrink"
    # Rewritten through a new file renamed over the old one.
    replacement = tmp_path / "new.jsonl"
    replacement.write_bytes(data + b"\n")
    os.replace(replacement, transcript)
    assert _decide(cp, "claude", transcript) == "inode"


def test_a_checkpoint_from_another_parser_version_is_refused(tmp_path, clock, shadow_on, monkeypatch):
    lines = codex_lines(3)
    transcript, cp = _checkpointed(tmp_path, "codex", lines, len(lines) - 2, clock)
    _append(transcript, lines[-2:])
    assert _decide(cp, "codex", transcript) == ""
    monkeypatch.setattr(inc, "parser_version", lambda backend: "a-newer-parser")
    assert _decide(cp, "codex", transcript) == "parser_version"


def test_the_audit_and_the_tail_budget_force_a_full_import(tmp_path, clock, shadow_on):
    lines = claude_lines(3)
    transcript, cp = _checkpointed(tmp_path, "claude", lines, len(lines) - 1, clock)
    _append(transcript, lines[-1:])
    ident = inc.identity(transcript)
    assert inc.decide(cp, "claude", transcript, ident, now_ms=cp.audited_at + 3_600_000,
                      audit_every=50, audit_seconds=3600, max_tail_bytes=1 << 30) == "audit"
    cp.imports_since_audit = 50
    assert inc.decide(cp, "claude", transcript, ident, now_ms=cp.audited_at,
                      audit_every=50, audit_seconds=3600, max_tail_bytes=1 << 30) == "audit"
    cp.imports_since_audit = 0
    assert inc.decide(cp, "claude", transcript, ident, now_ms=cp.audited_at,
                      audit_every=50, audit_seconds=3600, max_tail_bytes=10) == "tail_too_large"


# ---- the shadow compares, never serves ----------------------------------------

def test_the_shadow_reports_a_divergence_and_the_full_import_still_wins(tmp_path, clock, shadow_on):
    lines = claude_lines(4)
    starts = [i for i, l in enumerate(lines) if inc._BOUNDARY_MARKERS["claude"](l)]
    transcript, cp = _checkpointed(tmp_path, "claude", lines, starts[2] + 2, clock)
    message_store.record_user_message(
        agent_id=AGENT, backend_session_id=SESSION, client_msg_id="c3",
        text=request_text("claude", 3))
    c = db.conn()
    # A corrupted provenance cursor: it claims the client row of request 3
    # was already linked, so the tail would import a second copy.
    carry = dict(cp.store_carry, consumed_client_ids=[*cp.store_carry["consumed_client_ids"], "u-c3"])
    c.execute("UPDATE transcript_import_checkpoints SET store_carry_json=?",
              (json.dumps(carry),))
    c.commit()
    _append(transcript, lines[starts[2] + 2:])
    inc.import_transcript(backend="claude", path=transcript, agent_id=AGENT,
                          backend_session_id=SESSION)
    assert shadow_on[-1]["verdict"].startswith("diverge:"), shadow_on[-1]
    rows = message_store.list_messages(agent_id=AGENT, backend_session_id=SESSION)
    # The full import's own answer: the earliest "continue" takes the new
    # client row, the other stays a transcript row. The bad carry changed nothing.
    continued = [r["id"] for r in rows if r["text"] == "continue"]
    assert len(continued) == 2 and continued.count("u-c3") == 1


def test_without_the_shadow_the_import_is_the_plain_full_import(tmp_path, clock, monkeypatch):
    monkeypatch.setattr(inc, "BUDGET", inc.ShadowBudget())
    _prepare(tmp_path / "a.sqlite", "claude")
    transcript = tmp_path / "t.jsonl"
    transcript.write_bytes(b"".join(claude_lines(3)))
    calls = []
    real = message_store.store_transcript_turns
    monkeypatch.setattr(message_store, "store_transcript_turns",
                        lambda **kw: calls.append(sorted(kw)) or real(**kw))
    result = inc.import_transcript(backend="claude", path=transcript, agent_id=AGENT,
                                   backend_session_id=SESSION)
    assert result["shadow"] == "disabled"
    assert calls == [["agent_id", "backend_session_id", "source_file", "turns"]]
    assert db.conn().execute("SELECT count(*) FROM transcript_import_checkpoints").fetchone()[0] == 0


def test_the_kill_switch_outranks_the_config(monkeypatch):
    from lib import config
    monkeypatch.setattr(config, "load", lambda: type("C", (), {
        "transcript_shadow": {"enabled": True, "sample_every": 5}})())
    assert inc.settings()["enabled"] is True and inc.settings()["sample_every"] == 5
    monkeypatch.setenv("CLARP_TRANSCRIPT_SHADOW", "0")
    assert inc.settings()["enabled"] is False


def test_the_budget_samples_skips_under_pressure_and_caps_cpu(monkeypatch):
    conf = dict(inc._DEFAULTS, enabled=True, sample_every=3, max_cpu_seconds_per_hour=1.0)
    budget = inc.ShadowBudget()
    monkeypatch.setattr(os, "getloadavg", lambda: (0.0, 0.0, 0.0))
    monkeypatch.setattr(inc, "_pressure", lambda resource: 0.0)
    assert [budget.admit(conf) for _ in range(2)] == ["not_sampled"] * 2
    assert budget.admit(conf) == ""
    # Never two at once.
    for _ in range(2):
        budget.admit(conf)
    assert budget.admit(conf) == "concurrent"
    spent = budget.release(cpu_s=1.5, wall_s=2.0)
    assert spent["hour_cpu_s"] == 1.5 and spent["skipped"] == {"concurrent": 1}
    for _ in range(2):
        budget.admit(conf)
    assert budget.admit(conf) == "hourly_cpu_cap"
    budget = inc.ShadowBudget()
    monkeypatch.setattr(os, "getloadavg", lambda: (os.cpu_count() * 2.0, 0.0, 0.0))
    for _ in range(2):
        budget.admit(conf)
    assert budget.admit(conf) == "load"
    monkeypatch.setattr(os, "getloadavg", lambda: (0.0, 0.0, 0.0))
    monkeypatch.setattr(inc, "_pressure", lambda resource: 50.0 if resource == "io" else 0.0)
    for _ in range(2):
        budget.admit(conf)
    assert budget.admit(conf) == "io_pressure"


# ---- crashes, races, rollback --------------------------------------------------

def test_a_crash_at_each_step_leaves_no_checkpoint_ahead_of_the_rows(tmp_path, clock, shadow_on, monkeypatch):
    lines = claude_lines(3)
    _prepare(tmp_path / "a.sqlite", "claude")
    transcript = tmp_path / "t.jsonl"
    transcript.write_bytes(b"".join(lines[:5]))

    def count():
        c = db.conn()
        return (c.execute("SELECT count(*) FROM transcript_import_checkpoints").fetchone()[0],
                c.execute("SELECT count(*) FROM messages WHERE source_file=?",
                          (str(transcript),)).fetchone()[0])

    class Crash(BaseException):
        pass

    # 1. During the parse: nothing is stored.
    def crash_parse(path):
        raise Crash
    with pytest.raises(Crash):
        inc.import_transcript(backend="claude", path=transcript, agent_id=AGENT,
                              backend_session_id=SESSION, parse=crash_parse)
    assert count() == (0, 0)
    # 2. During the store: the rows' last batch and the checkpoint go together.
    real_write = inc.write_checkpoint
    monkeypatch.setattr(inc, "write_checkpoint", lambda *a: (_ for _ in ()).throw(Crash()))
    with pytest.raises(Crash):
        inc.import_transcript(backend="claude", path=transcript, agent_id=AGENT,
                              backend_session_id=SESSION)
    db.conn().rollback()          # the process died: SQLite drops the open batch
    assert count() == (0, 0)
    # 3. The checkpoint write itself fails: the import commits without it.
    monkeypatch.setattr(inc, "write_checkpoint",
                        lambda *a: (_ for _ in ()).throw(sqlite3.OperationalError("disk I/O")))
    inc.import_transcript(backend="claude", path=transcript, agent_id=AGENT,
                          backend_session_id=SESSION)
    checkpoints, rows = count()
    assert checkpoints == 0 and rows > 0
    # 4. Healthy again: the checkpoint is written with the rows it describes.
    monkeypatch.setattr(inc, "write_checkpoint", real_write)
    _append(transcript, lines[5:])
    inc.import_transcript(backend="claude", path=transcript, agent_id=AGENT,
                          backend_session_id=SESSION)
    cp = inc.load_checkpoint(db.conn(), AGENT, SESSION, str(transcript))
    assert cp.turn_count == len(inc.parse_range("claude", transcript, 0, transcript.stat().st_size))


def test_two_importers_racing_keep_the_checkpoint_that_covers_more(tmp_path, clock, shadow_on):
    lines = codex_lines(4)
    transcript, older = _checkpointed(tmp_path, "codex", lines, len(lines) - 6, clock)
    _append(transcript, lines[len(lines) - 6:])
    inc.import_transcript(backend="codex", path=transcript, agent_id=AGENT,
                          backend_session_id=SESSION)
    newer = inc.load_checkpoint(db.conn(), AGENT, SESSION, str(transcript))
    assert newer.complete_size > older.complete_size
    inc.write_checkpoint(db.conn(), older)     # the slower importer commits last
    db.conn().commit()
    kept = inc.load_checkpoint(db.conn(), AGENT, SESSION, str(transcript))
    assert kept.complete_size == newer.complete_size
    # Real threads: one shadow at a time, every import succeeds.
    errors, barrier = [], threading.Barrier(4)

    def run():
        try:
            barrier.wait()
            inc.import_transcript(backend="codex", path=transcript, agent_id=AGENT,
                                  backend_session_id=SESSION)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            db.close_local()
    threads = [threading.Thread(target=run) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert not errors
    assert not [e for e in shadow_on if e["verdict"].startswith(("diverge", "error"))]


def test_older_code_reads_a_v117_database_and_117_migrates_from_116(tmp_path, monkeypatch):
    from lib import db_schema, db_migrations
    # Pinned to the 117 release explicitly: this is about 117 against 116,
    # whatever the current schema version is.
    monkeypatch.setattr(db_schema, "_SCHEMA_VERSION", 117)
    path = tmp_path / "state.sqlite"
    db.reset_for_tests(path)
    c = db.conn()
    assert c.execute("PRAGMA user_version").fetchone()[0] == 117
    c.execute("INSERT INTO agents(agent_id, persona, voice_id, cwd, session, created_at)"
              " VALUES ('a', 'a', 'v', '/', 's', 1)")
    c.commit()
    # Older code (schema 116) opens the newer file: no migration, and its own
    # tables work; the checkpoint table is simply never read.
    monkeypatch.setattr(db_schema, "_SCHEMA_VERSION", 116)
    db.reset_for_tests(path)
    message_store.store_transcript_turns(agent_id="a", backend_session_id="b", source_file="f",
                                         turns=[{"role": "user", "text": "hi", "timestamp": _ts(1)}])
    assert db.conn().execute("PRAGMA user_version").fetchone()[0] == 117
    monkeypatch.setattr(db_schema, "_SCHEMA_VERSION", 117)
    # A v116 database gains the table when the 117 release opens it.
    old = tmp_path / "old.sqlite"
    db.reset_for_tests(old)
    c = db.conn()
    c.execute("DROP TABLE transcript_import_checkpoints")
    c.execute("PRAGMA user_version = 116")
    c.commit()
    db.reset_for_tests(old)
    c = db.conn()
    assert c.execute("PRAGMA user_version").fetchone()[0] == 117
    assert c.execute("SELECT count(*) FROM transcript_import_checkpoints").fetchone()[0] == 0
    assert db_migrations._migrate_to_v117(c) is None     # idempotent
    monkeypatch.undo()


# ---- the store's carried state at every split ---------------------------------

def _at(seconds: float) -> str:
    return message_writes._iso_from_ms(1_790_000_000_000 + int(seconds * 1000))


STORE_TURNS = [
    {"role": "user", "text": "check it", "timestamp": _at(0.3)},
    {"role": "assistant", "text": "Endpoint is unavailable.", "timestamp": _at(1), "failed": True},
    {"role": "user", "text": "check it", "timestamp": _at(75)},           # a retry's echo
    {"role": "assistant", "text": "Endpoint is unavailable.", "timestamp": _at(76), "failed": True},
    {"role": "user", "text": "other thing", "timestamp": _at(100)},
    {"role": "assistant", "text": "Reply other.", "timestamp": _at(101), "kind": "final_answer",
     "tools": [{"id": "t1", "name": "Bash", "status": "ok"}]},
    {"role": "user", "text": "check it", "timestamp": _at(300.4)},
    {"role": "assistant", "text": "Working.", "timestamp": _at(310),
     "tools": [{"id": "t2", "name": "Bash", "status": "running"}]},
    {"role": "assistant", "text": "Done.", "timestamp": _at(320), "kind": "final_answer"},
    {"role": "user", "text": "from the terminal", "timestamp": _at(400)},
    {"role": "assistant", "text": "Terminal answer.", "timestamp": _at(401),
     "tools": [{"id": "t3", "name": "Read", "status": "ok"}]},
]


def _store_world(path, clock, upto: int) -> None:
    _prepare(path, "agy")
    clock["ms"] = 1_790_000_000_000
    message_store.record_user_message(
        agent_id=AGENT, backend_session_id=SESSION, client_msg_id="c1",
        text="check it", origin="agent", sender_agent_id=SENDER, trace_id="tr-c1")
    c = db.conn()
    # A finalized live row twin of "Reply other." (no request trace links it),
    # and an agy turn whose range stays open until a later user message.
    c.execute("""INSERT INTO messages(message_id, agent_id, backend_session_id, source_file,
                 seq, role, timestamp, text, updated_at, revision)
                 VALUES ('final-other', ?, ?, 'final:tr-other', -10, 'assistant', ?,
                         'Reply other.', 1, ?)""",
              (AGENT, SESSION, _at(102), message_writes._next_revision(c)))
    c.execute("""INSERT INTO messages(message_id, agent_id, backend_session_id, source_file,
                 seq, role, timestamp, text, updated_at, revision)
                 VALUES ('final-done', ?, ?, 'final:tr-done', -11, 'assistant', ?,
                         'Done (stream).', 1, ?)""",
              (AGENT, SESSION, _at(321), message_writes._next_revision(c)))
    c.execute("""INSERT INTO agy_turn_authority(agent_id, backend_session_id, trace_id,
                 assistant_start_ordinal, assistant_end_ordinal, terminal_status,
                 authoritative_message_id, updated_at)
                 VALUES (?, ?, 'tr-done', 3, NULL, 'done', 'final-done', 1)""", (AGENT, SESSION))
    c.commit()
    clock["ms"] += 300_000
    message_store.record_user_message(
        agent_id=AGENT, backend_session_id=SESSION, client_msg_id="c2", text="check it",
        trace_id="tr-c2")


def test_the_carried_store_state_reproduces_the_full_import_at_every_split(tmp_path, clock):
    """Previous import saw ``turns[:seen]``; the next one sees all turns.
    Resuming at every user turn ``k`` with the state captured there must
    leave exactly the rows, heads and clock of the full re-import."""
    users = [i for i, t in enumerate(STORE_TURNS) if t["role"] == "user"]
    checked = 0
    for seen in range(1, len(STORE_TURNS)):
        for k in [u for u in users if u < seen]:
            a, b = tmp_path / f"a-{seen}-{k}.sqlite", tmp_path / f"b-{seen}-{k}.sqlite"
            _store_world(a, clock, seen)
            message_store.store_transcript_turns(agent_id=AGENT, backend_session_id=SESSION,
                                                 source_file="f", turns=STORE_TURNS[:seen])
            clock["ms"] += 1000
            message_store.store_transcript_turns(agent_id=AGENT, backend_session_id=SESSION,
                                                 source_file="f", turns=STORE_TURNS)
            expected = snapshot()
            _store_world(b, clock, seen)
            carry: dict = {}
            message_store.store_transcript_turns(agent_id=AGENT, backend_session_id=SESSION,
                                                 source_file="f", turns=STORE_TURNS[:seen],
                                                 capture_at=k, captured=carry)
            clock["ms"] += 1000
            message_writes.store_transcript_tail(
                agent_id=AGENT, backend_session_id=SESSION, source_file="f",
                turns=STORE_TURNS[k:], start_seq=k, carry=carry)
            assert snapshot() == expected, f"seen {seen}, resumed at {k}"
            checked += 1
    assert checked > 20
