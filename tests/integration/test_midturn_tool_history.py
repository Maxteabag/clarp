"""RM-N12: a new send must preserve the running turn's transcript tools."""
import json
import datetime as dt
import time
import threading
from contextlib import contextmanager

import pytest

from lib import agents, backends, live_hub, message_store, message_writes
from lib import transcript_import_cache, transcript_incremental
from lib import turn_lifecycle
from lib.turn_lifecycle import TurnEvent
from tests.readmodel import corpus
from tests.integration.test_server_di import (  # noqa: F401
    _get, _post, _model_catalog_never_probes_installed_clis,
    fake_ctx, running_server,
)


def _active_turn(running_server, monkeypatch, tmp_path, backend):
    from lib import tool_explanation_settings
    tool_explanation_settings.update({"enabled": False})
    base, _ctx, _srv = running_server
    agent = agents.get_by_session("rachel")
    agents.conn().execute("UPDATE agents SET backend=? WHERE agent_id=?",
                          (backend, agent["agent_id"]))
    agents.start_runtime(agent["agent_id"], "rachel")
    agents.bind_backend_session(agent["agent_id"], "synthetic-native")
    transcript = tmp_path / "synthetic.jsonl"
    source = getattr(corpus, backend)()
    spawned, steered = [], []
    monkeypatch.setattr(backends, "spawn_turn", lambda *a, **kw: spawned.append(kw))
    monkeypatch.setattr(backends, "active_handles", lambda *a: ["synthetic-handle"] if spawned else [])
    monkeypatch.setattr(backends, "steer_turn", lambda *a, **kw: steered.append(kw) or True)
    monkeypatch.setattr(backends.by_id(backend), "resume_target", lambda *a: str(transcript))
    status, _raw = _post(base + "/send", {
        "session": "rachel", "force_session": True,
        "text": corpus.request_text(0), "client_msg_id": "initial-request",
    })
    assert status == 200 and len(spawned) == 1
    active_trace = agents.get_trace(agent["agent_id"])
    monkeypatch.setattr(backends, "find_session_jsonl", lambda *a: transcript)
    return base, agent, transcript, source, spawned, steered, active_trace


def _log(base, after=0):
    status, raw = _get(base + "/log?session=rachel&limit=100&include_automated=0"
                      + f"&after_revision={after}")
    assert status == 200
    return json.loads(raw)


@pytest.mark.parametrize("backend,queue", [("claude", True), ("codex", True), ("codex", False)])
def test_midturn_send_keeps_tools_in_snapshot_and_delta(
        running_server, monkeypatch, tmp_path, backend, queue):
    base, agent, transcript, source, spawned, steered, active_trace = _active_turn(
        running_server, monkeypatch, tmp_path, backend)
    # Leave the first turn running after a tool result, before its final reply.
    records = source.header + source.groups[0][:3 if backend == "claude" else 6]
    transcript.write_text("".join(json.dumps(r) + "\n" for r in records))
    before = _log(base)
    tool_ids = {r["id"] for r in before["turns"] if r["tools"] or r["display_cells"]}
    assert tool_ids, "the running transcript must expose tool activity before send"
    status, _raw = _post(base + "/send", {
        "session": "rachel", "force_session": True,
        "text": corpus.request_text(1), "client_msg_id": "midturn-steer",
        "queue_if_busy": queue,
    })
    assert status == 200
    assert len(spawned) == 1, "the follow-up must reach the busy path"
    assert len(steered) == (0 if queue else 1)
    assert agents.get_trace(agent["agent_id"]) == active_trace
    # The provider keeps writing after the send. A queued prompt has not
    # reached it; an accepted steer appears inside the same provider turn.
    continued = source.groups[1][:3 if backend == "claude" else 6]
    if queue:
        continued = continued[1:] if backend == "claude" else continued[3:]
    else:
        for record in continued:
            if "turn_id" in record.get("payload", {}):
                record["payload"]["turn_id"] = "turn-0"
    with transcript.open("a") as output:
        output.write("".join(json.dumps(r) + "\n" for r in continued))
    # /log refreshes already stored history in the background. Run its real
    # importer to completion before checking the post-refresh response.
    transcript_incremental.import_transcript(
        backend=backend, path=transcript, agent_id=agent["agent_id"],
        backend_session_id="synthetic-native")
    delta = _log(base, before["latest_revision"])
    # Follow the native replacement flag, otherwise upsert changed rows by id.
    merged = {r["id"]: r for r in before["turns"]}
    if delta["replace_required"]:
        merged = {r["id"]: r for r in _log(base)["turns"]}
    else:
        merged.update({r["id"]: r for r in delta["turns"]})
    reload = _log(base)
    for rows in (list(merged.values()), reload["turns"]):
        kept = {r["id"] for r in rows if r["tools"] or r["display_cells"]}
        assert tool_ids <= kept


@contextmanager
def _held_import(monkeypatch, transcript):
    entered, release = threading.Event(), threading.Event()
    original = transcript_incremental.import_transcript

    def delayed(**kwargs):
        if kwargs["path"] == transcript:
            entered.set()
            assert release.wait(5), "fixture importer was not released"
        return original(**kwargs)

    monkeypatch.setattr(transcript_incremental, "import_transcript", delayed)
    try:
        yield entered, release
    finally:
        release.set()
        assert transcript_import_cache.wait_for_background(5)


@pytest.mark.parametrize("backend,queue", [("claude", True), ("codex", True), ("codex", False)])
def test_replacement_after_send_keeps_live_tools_awaiting_import(
        running_server, monkeypatch, tmp_path, backend, queue):
    base, agent, transcript, source, spawned, steered, trace = _active_turn(
        running_server, monkeypatch, tmp_path, backend)
    first = source.groups[0]
    if backend == "claude":
        commentary = json.loads(json.dumps(first[1]))
        commentary["message"]["content"] = commentary["message"]["content"][:1]
        initial = [first[0], commentary]
        tool_line = json.loads(json.dumps(first[1]))
        tool_line["uuid"] = "synthetic-tool-only"
        tool_line["message"]["content"] = tool_line["message"]["content"][1:]
        later = [tool_line, first[2]]
        call = "toolu_0"
    else:
        initial, later, call = first[:4], first[4:6], "call_0"
        # Exploration commands coalesce under a display-cell group id;
        # this probe needs the tool's exact identity for the recovery check.
        later[0]["payload"]["arguments"] = json.dumps({"cmd": "printf synthetic"})
    transcript.write_text("".join(json.dumps(r) + "\n" for r in source.header + initial))
    before = _log(base)
    assert before["turns"] and not any(r["tools"] or r["display_cells"] for r in before["turns"])

    # A streamed commentary row can overlap commentary already imported.
    # The next real import retracts that duplicate and raises the replace
    # watermark, even though this provider turn is still running.
    streamed = message_store.upsert_live_assistant_message(
        agent_id=agent["agent_id"], backend_session_id="synthetic-native",
        trace_id=trace, text="Looking at 0.", item_key="overlapping-commentary")
    assert streamed is not None
    transcript_incremental.import_transcript(
        backend=backend, path=transcript, agent_id=agent["agent_id"],
        backend_session_id="synthetic-native")
    head = agents.conn().execute(
        "SELECT replace_revision FROM conversation_heads WHERE agent_id=? AND backend_session_id=?",
        (agent["agent_id"], "synthetic-native")).fetchone()[0]
    assert head > before["latest_revision"]

    with _held_import(monkeypatch, transcript) as (entered, release):
        # Hold the real background import queue: live activity can reach the
        # client before the worker takes this transcript version (lock pressure).
        with transcript.open("a") as output:
            output.write("".join(json.dumps(r) + "\n" for r in later))
        turn_lifecycle.transition(agent["agent_id"], TurnEvent.TOOL_STARTED, {
            "trace_id": trace, "call_id": call, "tool": "Bash",
            "input": {"command": "printf synthetic"}, "dispatch": backend,
            "phase": "tool_started", "status": "running",
        })
        hub = live_hub.current()
        assert hub is not None
        assert any(i.get("tool", {}).get("call_id") == call
                   for i in hub.snapshot(agent_id=agent["agent_id"])["items"])
        transcript_import_cache.schedule_import(
            transcript, lambda: transcript_incremental.import_transcript(
                backend=backend, path=transcript, agent_id=agent["agent_id"],
                backend_session_id="synthetic-native"), owner=agent["agent_id"])
        assert entered.wait(2)
        status, _raw = _post(base + "/send", {
            "session": "rachel", "force_session": True,
            "text": corpus.request_text(1), "client_msg_id": "late-import-followup",
            "queue_if_busy": queue,
        })
        assert status == 200 and len(spawned) == 1
        assert len(steered) == (0 if queue else 1)
        assert agents.get_trace(agent["agent_id"]) == trace
        delta = _log(base, before["latest_revision"])
        assert delta["replace_required"]
        assert delta["conversation_id"] == before["conversation_id"]
        assert not delta["has_more"]
        assert agents.conn().execute(
            "SELECT replace_revision FROM conversation_heads WHERE agent_id=? AND backend_session_id=?",
            (agent["agent_id"], "synthetic-native")).fetchone()[0] == head
        timer = threading.Timer(0.1, release.set)
        timer.start()
        try:
            replacement = _log(base)
        finally:
            release.set()
            timer.cancel()
            timer.join(1)
        assert any(i.get("tool", {}).get("call_id") == call
                   for i in hub.snapshot(agent_id=agent["agent_id"])["items"])
        calls = [t.get("id") for r in replacement["turns"]
                 for t in r["tools"] + r["display_cells"]]
        # Let the queued production importer finish. The same call then appears
        # without another provider event: the gap is delivery timing, not deletion.
        assert transcript_import_cache.wait_for_background(5)
        refreshed = _log(base)
        imported_calls = [t.get("id") for r in refreshed["turns"]
                          for t in r["tools"] + r["display_cells"]]
        assert call in imported_calls
        assert call in calls, "a replacement snapshot must retain the tool already shown live"


@pytest.mark.parametrize("queue", [True, False])
def test_long_tool_turn_only_loses_a_tail_row_at_the_page_boundary(
        running_server, monkeypatch, tmp_path, queue):
    clock = [int(time.time() * 1000)]
    monkeypatch.setattr(message_writes, "now_ms", lambda: clock[0])
    base, agent, transcript, source, spawned, steered, trace = _active_turn(
        running_server, monkeypatch, tmp_path, "codex")
    records = source.header + source.groups[0][:3]
    for index in range(101):
        timestamp = (dt.datetime.fromtimestamp(
            (clock[0] + index + 1) / 1000, dt.timezone.utc)
            .isoformat(timespec="milliseconds").replace("+00:00", "Z"))
        # Codex groups consecutive calls onto the last assistant row. A
        # distinct commentary message makes this a 101-row tool history.
        records.append({"timestamp": timestamp, "type": "event_msg", "payload": {
            "type": "agent_message", "message": f"Synthetic step {index}",
            "phase": "commentary", "turn_id": "turn-0"}})
        records.append({"timestamp": timestamp, "type": "response_item", "payload": {
            "type": "function_call", "name": "exec_command",
            "arguments": json.dumps({"cmd": "ls /redacted"}),
            "call_id": f"long-{index}", "turn_id": "turn-0"}})
    transcript.write_text("".join(json.dumps(r) + "\n" for r in records))
    before = _log(base)
    assert len(before["turns"]) == 100 and before["has_more"]
    before_tools = {r["id"] for r in before["turns"] if r["tools"] or r["display_cells"]}
    assert len(before_tools) == 100  # the original prompt is already on the older page
    clock[0] += 1000
    status, _raw = _post(base + "/send", {
        "session": "rachel", "force_session": True, "text": corpus.request_text(1),
        "client_msg_id": "long-turn-followup", "queue_if_busy": queue,
    })
    assert status == 200 and len(spawned) == 1
    assert len(steered) == (0 if queue else 1)
    assert agents.get_trace(agent["agent_id"]) == trace
    delta = _log(base, before["latest_revision"])
    assert not delta["replace_required"]
    assert not any(r["tools"] or r["display_cells"] for r in delta["turns"])
    tail = _log(base)
    assert len(tail["turns"]) == 100 and tail["has_more"]
    after_tools = {r["id"] for r in tail["turns"] if r["tools"] or r["display_cells"]}
    assert len(before_tools - after_tools) == (0 if queue else 1)
