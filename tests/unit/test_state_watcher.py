import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "server"))
from lib import agents as agents_db  # noqa: E402
from lib import events  # noqa: E402
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


def _done_agent(session):
    agent_id = agents_db.create_agent(
        persona=session.title(), voice_id="V", cwd="/tmp", session=session)
    agents_db.record_state(agent_id, "done", {"backend_session_id": "bs"})
    return agent_id


def test_poll_once_returns_while_completed_turn_classification_blocks(monkeypatch):
    import threading
    import time
    from lib import apns, user_notifications

    release = threading.Event()
    classified = []

    def slow_classify(**kw):
        assert release.wait(5), "test released the classifier"
        classified.append(kw["agent_id"])
        return {"notify": True, "push": True, "session": kw["session"],
                "agent_id": kw["agent_id"], "preview": "done",
                "notification_id": "pn-1", "persona": kw["persona"]}

    pushed = []
    monkeypatch.setattr(user_notifications, "classify_completed_turn", slow_classify)
    monkeypatch.setattr(user_notifications, "event_payload",
                        lambda n: {"type": SSEType.USER_NOTIFICATION, **{
                            k: v for k, v in n.items()
                            if k in events.FIELDS[SSEType.USER_NOTIFICATION]}})
    monkeypatch.setattr(apns, "on_user_notification", pushed.append)

    agent_id = _done_agent("rachel")
    stream = FakeStream()
    watcher = StateLogWatcher(stream)
    try:
        started = time.monotonic()
        watcher._poll_once()
        elapsed = time.monotonic() - started
        # The classifier is still parked; the state stream already moved on.
        assert elapsed < 0.5, f"poll blocked for {elapsed:.2f}s on classification"
        assert [e["type"] for e in stream.events][:1] == [SSEType.AGENT_STATE]
        assert not classified
        assert not any(e["type"] == SSEType.USER_NOTIFICATION for e in stream.events)

        release.set()
        assert watcher.wait_for_notifications(5)
        assert classified == [agent_id]
        assert pushed and pushed[0]["agent_id"] == agent_id
        assert any(e["type"] == SSEType.USER_NOTIFICATION for e in stream.events)
    finally:
        release.set()
        watcher.stop()


def test_completed_turn_classification_keeps_row_order(monkeypatch):
    from lib import apns, user_notifications

    order = []

    def classify(**kw):
        order.append(kw["agent_id"])
        return {"notify": False}

    monkeypatch.setattr(user_notifications, "classify_completed_turn", classify)
    monkeypatch.setattr(apns, "on_user_notification", lambda n: None)

    first = _done_agent("first")
    second = _done_agent("second")
    third = _done_agent("third")
    watcher = StateLogWatcher(FakeStream())
    try:
        watcher._poll_once()
        assert watcher.wait_for_notifications(5)
        assert order == [first, second, third]
    finally:
        watcher.stop()


def test_state_watcher_stop_joins_notification_worker():
    watcher = StateLogWatcher(FakeStream())
    watcher.INTERVAL_SEC = 10
    watcher.start()
    worker = watcher._notify_thread
    assert worker is not None and worker.is_alive()

    watcher.stop()

    assert not worker.is_alive()
