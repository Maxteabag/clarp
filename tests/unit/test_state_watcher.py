import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "server"))
from lib import agents as agents_db  # noqa: E402
from lib.protocol import SSEType  # noqa: E402
from lib.state_watcher import StateLogWatcher  # noqa: E402


class FakeStream:
    def __init__(self):
        self.events = []

    def broadcast(self, event):
        self.events.append(event)


def test_state_watcher_broadcasts_protocol_event():
    agent_id = agents_db.create_agent(
        persona="Rachel", voice_id="V", cwd="/tmp", session="rachel")
    agents_db.set_custom_status(agent_id, "Building")
    agents_db.record_state(agent_id, "thinking", {"source": "pwa"})
    stream = FakeStream()
    watcher = StateLogWatcher(stream)

    watcher._poll_once()

    assert stream.events
    assert stream.events[0]["type"] == SSEType.AGENT_STATE
    assert stream.events[0]["agent_id"] == agent_id
    assert stream.events[0]["session"] == "rachel"
    assert stream.events[0]["status_text"] == "Building"


def test_state_watcher_broadcasts_status_clear():
    agent_id = agents_db.create_agent(
        persona="Rachel", voice_id="V", cwd="/tmp", session="rachel")
    stream = FakeStream()
    watcher = StateLogWatcher(stream)

    agents_db.set_custom_status(agent_id, "Building")
    agents_db.record_state(agent_id, "background", {"label": "Building"})
    watcher._poll_once()

    agents_db.set_custom_status(agent_id, "")
    agents_db.record_state(agent_id, "idle")
    watcher._poll_once()

    state_events = [e for e in stream.events if e["type"] == SSEType.AGENT_STATE]
    assert state_events[0]["status_text"] == "Building"
    assert state_events[-1]["kind"] == "idle"
    assert state_events[-1]["status_text"] == ""


def test_state_watcher_stop_joins_thread_quickly():
    watcher = StateLogWatcher(FakeStream())
    watcher.INTERVAL_SEC = 10

    watcher.start()
    watcher.stop()

    assert watcher._thread is not None
    assert not watcher._thread.is_alive()
