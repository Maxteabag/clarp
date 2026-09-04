from __future__ import annotations

from lib import agents as agents_db
from lib.runtime_events import RuntimeEventStream, RuntimeEventWatcher


class RecordingStream:
    def __init__(self):
        self.events = []

    def broadcast_ephemeral(self, event):
        self.events.append(event)


def test_runtime_events_cross_process_boundary_without_duplicate_persistence():
    runtime_stream = RuntimeEventStream()
    server_stream = RecordingStream()
    watcher = RuntimeEventWatcher(server_stream)

    runtime_stream.broadcast({
        "type": "transcript-updated",
        "session": "theo",
        "agent_id": "agent-1",
        "trace_id": "trace-1",
    })
    assert len(agents_db.events_after(0)) == 1

    watcher._poll_once()

    [event] = server_stream.events
    assert event["type"] == "transcript-updated"
    assert event["session"] == "theo"
    assert event["agent_id"] == "agent-1"
    assert event["trace_id"] == "trace-1"
    assert event["event_id"] > 0
    assert event["ts"] > 0
    assert "_clarp_runtime_event" not in event
    assert len(agents_db.events_after(0)) == 1


def test_runtime_event_watcher_ignores_events_owned_by_http_server():
    agents_db.record_sse_event({
        "type": "agent-focus", "session": "theo", "agent_id": "agent-1"})
    server_stream = RecordingStream()
    watcher = RuntimeEventWatcher(server_stream)

    watcher._poll_once()

    assert server_stream.events == []
