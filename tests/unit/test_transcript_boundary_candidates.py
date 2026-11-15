"""Where a full import looks for its boundary (``boundary_skips_dropped_records``).

A Claude user record that is not a tool result may open a turn. The harness
injects such records into a running turn (a task notification, an account
recovery, a system reminder) and the parser drops them, so a tail that
starts at one does not start with a user turn. A full import tries only the
six newest candidates: a turn with six of them after its prompt got no
checkpoint, and every import of its conversation was a full one.
"""
from __future__ import annotations

import json

import pytest

from lib import backends, transcript_incremental as inc

MAX = 16 * 1024 * 1024
INJECTED = ("<task-notification>\n<task-id>b1</task-id>\n</task-notification>",
            "<clarp-account-recovery>\nCarry on.\n</clarp-account-recovery>",
            "<system-reminder>\nnothing new\n</system-reminder>")


def _record(role, content, second=0):
    return json.dumps({"type": role, "timestamp": f"2026-11-14T10:00:{second:02d}.000Z",
                       "message": {"role": role, "content": content}},
                      separators=(",", ":")).encode() + b"\n"


def _step(number):
    call = f"c{number}"
    return b"".join((
        _record("assistant", [{"type": "text", "text": f"step {number}"},
                              {"type": "tool_use", "id": call, "name": "Bash",
                               "input": {"command": "true"}}]),
        _record("user", [{"type": "tool_result", "tool_use_id": call, "content": "ok"}])))


def _transcript(tmp_path, injected, *, prompts=("the one request",)):
    """Each prompt, then steps with ``injected`` harness records among them."""
    data = b""
    for prompt in prompts:
        data += _record("user", prompt)
        for number in range(injected + 2):
            data += _step(number)
            if number < injected:
                data += _record("user", INJECTED[number % len(INJECTED)])
    path = tmp_path / "transcript.jsonl"
    path.write_bytes(data)
    return path


def _chosen(path, turns_only):
    turns = backends.parse_turns("claude", path)
    ident = inc.identity(path)
    return turns, inc.choose_boundary("claude", path, turns, inc.turn_digests(turns), ident, MAX,
                                      turns_only=turns_only)


@pytest.mark.parametrize("injected", [0, 5, 6, 7, 40])
def test_a_turn_with_harness_records_keeps_its_boundary(tmp_path, injected):
    path = _transcript(tmp_path, injected)
    turns, (boundary, why) = _chosen(path, True)
    assert [turn["role"] for turn in turns].count("user") == 1
    assert boundary is not None and (boundary.seq, boundary.offset, why) == (0, 0, "")
    turns, (boundary, why) = _chosen(path, False)
    if injected < inc._CANDIDATES:
        assert boundary is not None and boundary.seq == 0
    else:
        assert (boundary, why) == (None, "tail_differs")


def test_the_candidates_are_the_newest_records_that_are_turns(tmp_path):
    path = _transcript(tmp_path, 9, prompts=[f"request {n}" for n in range(8)])
    end = path.stat().st_size
    every = inc.boundary_candidates("claude", path, end, MAX)
    assert len(every) == inc._CANDIDATES
    assert all(str(record["message"]["content"]).startswith("<") for _offset, record in every)
    real = inc.boundary_candidates("claude", path, end, MAX, turns_only=True)
    assert [record["message"]["content"] for _offset, record in real] \
        == [f"request {n}" for n in (7, 6, 5, 4, 3, 2)]
    offsets = [offset for offset, _record in real]
    assert offsets == sorted(offsets, reverse=True)
    for offset, _record in real:
        assert inc.parse_range("claude", path, offset, end)[0]["role"] == "user"
    turns, (boundary, why) = _chosen(path, True)
    assert (boundary.offset, why) == (offsets[0], "")
    assert turns[boundary.seq]["text"] == "request 7"
    assert _chosen(path, False)[1] == (None, "tail_differs")


def test_a_boundary_is_still_decided_by_the_tail_parsed_alone(tmp_path):
    """The filter only chooses which records are tried. A record that is a
    turn when parsed alone and whose tail does not reproduce the full parse
    is refused as before."""
    path = _transcript(tmp_path, 2)
    turns = backends.parse_turns("claude", path)
    wrong = turns[:-1] + [{**turns[-1], "text": "not what the file says"}]
    boundary, why = inc.choose_boundary("claude", path, wrong, inc.turn_digests(wrong),
                                        inc.identity(path), MAX, turns_only=True)
    assert (boundary, why) == (None, "tail_differs")


def test_codex_candidates_are_what_they_were(tmp_path):
    """A Codex turn opens at task_started, which parsed alone is no turn."""
    lines = []
    for number in range(3):
        for kind, payload in (("event_msg", {"type": "task_started", "turn_id": f"t{number}"}),
                              ("event_msg", {"type": "user_message", "message": f"request {number}"}),
                              ("event_msg", {"type": "agent_message", "message": f"answer {number}",
                                             "phase": "final_answer"})):
            lines.append(json.dumps({"timestamp": f"2026-11-14T10:00:0{number}.000Z", "type": kind,
                                     "payload": payload}, separators=(",", ":")).encode() + b"\n")
    path = tmp_path / "rollout.jsonl"
    path.write_bytes(b"".join(lines))
    end = path.stat().st_size
    assert inc.boundary_candidates("codex", path, end, MAX, turns_only=True) \
        == inc.boundary_candidates("codex", path, end, MAX)
    assert len(inc.boundary_candidates("codex", path, end, MAX)) == 3


def test_the_switch_is_off_until_switched_on():
    assert inc._DEFAULTS["boundary_skips_dropped_records"] is False
