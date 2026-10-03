"""/log rows say which turn wrote them and how it ended (docs/live-items.md §9,
feature `log_turn_summary`), so a client can fold a settled turn without the
live channel: every row of a turn carries its `trace_id`; assistant text rows
carry `phase` (commentary before tool work, final otherwise); the turn's last
row carries `turn` shaped like GET /live's; tools an interrupt cut off read
`interrupted` instead of staying `running`."""
from __future__ import annotations

import datetime as dt

from lib import agents as agents_db
from lib import message_store, turn_lifecycle
from lib.turn_lifecycle import TurnEvent


def _iso(ms: int) -> str:
    return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _agent(session="nia", backend="claude"):
    agent_id = agents_db.create_agent(persona="Nia", voice_id="v", cwd="/tmp",
                                      session=session, backend=backend)
    agents_db.start_runtime(agent_id, session)
    return agent_id


def _start(agent_id, trace, prompt="Run the tests", client_row=True):
    agents_db.open_turn(agent_id=agent_id, source="pwa", trace_id=trace)
    started = agents_db.conn().execute(
        "SELECT started_at FROM turns WHERE trace_id = ?", (trace,)).fetchone()["started_at"]
    if client_row:
        message_store.record_user_message(
            agent_id=agent_id, backend_session_id="b-1", client_msg_id=f"c-{trace}",
            text=prompt, trace_id=trace)
    turn_lifecycle.transition(agent_id, TurnEvent.SPAWN_STARTED, {"trace_id": trace})
    return started


def _transcript(started, prompt="Run the tests", tool_status="ok", final="All 12 pass."):
    rows = [{"role": "user", "text": prompt, "timestamp": _iso(started + 10)},
            {"role": "assistant", "text": "Running them now.", "timestamp": _iso(started + 1000)},
            {"role": "assistant", "text": "", "timestamp": _iso(started + 1100),
             "tools": [{"id": "toolu_1", "name": "Bash", "status": tool_status, "summary": "npm test"}]}]
    if final:
        rows.append({"role": "assistant", "text": final, "timestamp": _iso(started + 9000)})
    return rows


def _log(agent_id, **kw):
    return message_store.list_messages(agent_id=agent_id, backend_session_id="b-1", **kw)


def test_a_settled_turn_is_stamped_on_every_row_and_summarized_on_its_last():
    agent_id = _agent()
    started = _start(agent_id, "tr-1")
    message_store.store_transcript_turns(agent_id=agent_id, backend_session_id="b-1",
                                         source_file="/x/t.jsonl", turns=_transcript(started))
    turn_lifecycle.transition(agent_id, TurnEvent.PROCESS_EXITED_OK, {"trace_id": "tr-1"})
    rows = _log(agent_id)
    assert [r["trace_id"] for r in rows] == ["tr-1"] * 4
    assistant = [r for r in rows if r["role"] == "assistant"]
    assert [r.get("phase") for r in assistant] == ["commentary", None, "final"]
    assert all("turn" not in r for r in rows[:-1])
    turn = rows[-1]["turn"]
    assert turn["turn_id"] == "tr-1" and turn["status"] == "completed"
    assert turn["started_at_ms"] == started
    assert turn["worked_ms"] == turn["ended_at_ms"] - turn["started_at_ms"] >= 0
    assert turn["tool_count"] == 1


def test_an_interrupted_turn_marks_cut_off_tools_and_reaches_delta_clients():
    agent_id = _agent()
    started = _start(agent_id, "tr-2")
    message_store.store_transcript_turns(
        agent_id=agent_id, backend_session_id="b-1", source_file="/x/t.jsonl",
        turns=_transcript(started, tool_status="recorded", final=""))
    cursor = max(r["revision"] for r in _log(agent_id))
    turn_lifecycle.try_transition(agent_id, TurnEvent.STOP_REQUESTED, {"source": "user_stop"})
    changed = _log(agent_id, after_revision=cursor)
    tool_row = next(r for r in changed if r["tools"])
    assert [t["status"] for t in tool_row["tools"]] == ["interrupted"]
    assert tool_row["turn"]["status"] == "interrupted"
    # A later re-import of the same transcript keeps it that way without churn.
    revision = max(r["revision"] for r in _log(agent_id))
    message_store.store_transcript_turns(
        agent_id=agent_id, backend_session_id="b-1", source_file="/x/t.jsonl",
        turns=_transcript(started, tool_status="recorded", final=""))
    assert _log(agent_id, after_revision=revision) == []
    assert _log(agent_id)[-1]["tools"][0]["status"] == "interrupted"


def test_a_failed_turn_reads_failed():
    agent_id = _agent()
    started = _start(agent_id, "tr-3")
    message_store.store_transcript_turns(agent_id=agent_id, backend_session_id="b-1",
                                         source_file="/x/t.jsonl", turns=_transcript(started))
    turn_lifecycle.transition(agent_id, TurnEvent.PROCESS_EXITED_FAILED, {"trace_id": "tr-3"})
    assert _log(agent_id)[-1]["turn"]["status"] == "failed"


def test_codex_phases_and_rows_without_a_client_prompt_are_attributed_by_time():
    agent_id = _agent(session="cody", backend="codex")
    started = _start(agent_id, "tr-4", client_row=False)
    message_store.store_transcript_turns(
        agent_id=agent_id, backend_session_id="b-1", source_file="/x/rollout.jsonl",
        turns=[{"role": "user", "text": "check", "timestamp": _iso(started + 5)},
               {"role": "assistant", "text": "Looking.", "kind": "commentary",
                "timestamp": _iso(started + 500)},
               {"role": "assistant", "text": "Fine.", "kind": "final_answer",
                "timestamp": _iso(started + 900)}])
    turn_lifecycle.transition(agent_id, TurnEvent.PROCESS_EXITED_OK, {"trace_id": "tr-4"})
    rows = _log(agent_id)
    assert [r["trace_id"] for r in rows] == ["tr-4"] * 3
    assert [r.get("phase") for r in rows] == [None, "commentary", "final"]
    assert rows[-1]["turn"]["status"] == "completed"


def test_a_turn_still_running_has_no_summary():
    agent_id = _agent()
    started = _start(agent_id, "tr-5")
    message_store.store_transcript_turns(agent_id=agent_id, backend_session_id="b-1",
                                         source_file="/x/t.jsonl", turns=_transcript(started))
    assert all("turn" not in r for r in _log(agent_id))
