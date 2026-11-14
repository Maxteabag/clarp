"""A long turn keeps its checkpoint (``boundary_skips_dropped_records``).

The harness injects user records into a running turn: a task notification
for every background job that ends, an account recovery, a system reminder.
The parser drops them. A full import looked for its boundary among the six
newest user records, so a turn with six of those after its prompt found
none that opens a turn, wrote no checkpoint, and every import of the
conversation was a full one (``full_no_frontier reason=bootstrap_refused``)
for as long as the turn ran. With the switch on the candidates are the
records that are turns, and the turn's own prompt is found.
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3

import pytest

from tests.readmodel import rm_n14_harness as harness
from tests.readmodel.test_rm_n14_equivalence import _clones
from tests.readmodel.test_rm_n14_restart import _decision, _frontiers

SWITCH = pytest.mark.parametrize("switch", [True, False], ids=["turns only", "as before"])
INJECTED = ("<task-notification>\n<task-id>b{n}</task-id>\n</task-notification>",
            "<clarp-account-recovery>\nCarry on ({n}).\n</clarp-account-recovery>")


def _how(switch):
    return {"settings": {"boundary_skips_dropped_records": switch}}


def _record(role, content, second):
    stamp = (dt.datetime.fromtimestamp((harness.BASE_MS + second * 1000) / 1000, dt.timezone.utc)
             .isoformat(timespec="milliseconds").replace("+00:00", "Z"))
    return json.dumps({"type": role, "timestamp": stamp,
                       "message": {"role": role, "content": content}},
                      separators=(",", ":")).encode() + b"\n"


def _step(number, second):
    call = f"long-{number}"
    return b"".join((
        _record("assistant", [{"type": "text", "text": f"long step {number}"},
                              {"type": "tool_use", "id": call, "name": "Bash",
                               "input": {"command": "true"}}], second),
        _record("user", [{"type": "tool_result", "tool_use_id": call, "content": "ok"}], second)))


def _injected(number, second):
    return _record("user", INJECTED[number % 2].format(n=number), second)


def _retentions(report):
    return [e["code"] for e in report["tail_events"] if e["event"] == "transcriptTailRetention"]


@SWITCH
@pytest.mark.parametrize("injected", [5, 8])
def test_a_long_turn_with_harness_records_after_its_prompt(tmp_path, injected, switch):
    """Three turns, then one that runs on: its prompt, and steps with
    harness records among them. With five the prompt is still among the six
    newest candidates; with eight it is not."""
    backend = "claude"
    reference, candidate = _clones(tmp_path, backend)
    transcript = tmp_path / "synthetic.jsonl"
    data = b"".join(harness.append_bytes(backend, i) for i in range(3))
    data += _record("user", "one request that takes long", 10)
    for number in range(injected):
        data += _step(number, 11 + number) + _injected(number, 11 + number)
    transcript.write_bytes(data)
    found = switch or injected < 6
    with harness.PersistentLane(reference, transcript, backend, "reference") as full:
        with harness.PersistentLane(candidate, transcript, backend, "candidate") as actual:
            full.tick(0)
            first = actual.tick(0, faults=_how(switch))
            assert _decision(first) == "full_no_frontier(missing_unknown)"
            assert _frontiers(candidate) == (1 if found else 0)
            assert ("no_safe_boundary_tail_differs" in _retentions(first)) is not found
            harness.assert_product_equal(reference, candidate)
            for tick in (1, 2, 3):
                with transcript.open("ab") as output:
                    output.write(_step(100 + tick, 40 + tick))
                    if tick == 2:
                        output.write(_injected(100, 42))
                full.tick(tick)
                report = actual.tick(tick, faults=_how(switch))
                assert _decision(report) == ("tail_applied" if found else
                                             "full_no_frontier(bootstrap_refused)")
                harness.assert_product_equal(reference, candidate)
    with sqlite3.connect(candidate) as connection:
        texts = [text for (text,) in connection.execute(
            "SELECT text FROM messages WHERE role='user'")]
    assert "one request that takes long" in texts
    assert not any(text.startswith("<") for text in texts)       # the harness records are no rows


@pytest.mark.parametrize("backend", harness.BACKENDS)
def test_an_ordinary_conversation_is_imported_as_before(tmp_path, backend):
    """The same decisions with the switch as without, Claude and Codex."""
    seen = {}
    for switch in (True, False):
        where = tmp_path / ("on" if switch else "off")
        where.mkdir()
        reference, candidate = _clones(where, backend)
        transcript = where / "synthetic.jsonl"
        transcript.write_bytes(b"".join(harness.append_bytes(backend, i) for i in range(12)))
        decisions = []
        with harness.PersistentLane(reference, transcript, backend, "reference") as full:
            with harness.PersistentLane(candidate, transcript, backend, "candidate") as actual:
                for tick in range(4):
                    if tick:
                        with transcript.open("ab") as output:
                            output.write(harness.append_bytes(backend, 11 + tick))
                    full.tick(tick)
                    decisions.append(_decision(actual.tick(tick, faults=_how(switch))))
                    harness.assert_product_equal(reference, candidate)
        with sqlite3.connect(candidate) as connection:
            seen[switch] = (decisions, connection.execute(
                "SELECT boundary_offset, boundary_seq FROM transcript_import_checkpoints").fetchall())
    assert seen[True] == seen[False]
    assert seen[True][0] == ["full_no_frontier(missing_unknown)"] + ["tail_applied"] * 3
