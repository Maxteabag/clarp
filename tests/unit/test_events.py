"""Wire shape of each typed SSE event and the typed broadcast gate."""
import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "server"))
from lib import events  # noqa: E402
from lib.controller_events import build_controller_event  # noqa: E402
from lib.protocol import AgentState, SSEType  # noqa: E402


class FakeStream:
    def __init__(self):
        self.durable = []
        self.ephemeral = []

    def broadcast(self, event):
        self.durable.append(event)

    def broadcast_ephemeral(self, event):
        self.ephemeral.append(event)


def wire(event) -> str:
    return json.dumps(event)


def test_roster_nudge_is_the_exact_reconnect_bytes():
    assert wire(events.agent_roster()) == '{"type": "agent-roster"}'


def test_roster_kinds_keep_site_order():
    assert wire(events.agent_roster("contact-assigned", session="s", name="N")) == (
        '{"type": "agent-roster", "kind": "contact-assigned", "session": "s", "name": "N"}')
    assert wire(events.agent_roster("persona-created")) == (
        '{"type": "agent-roster", "kind": "persona-created"}')
    assert wire(events.agent_roster("created", session="s", persona="P",
                                    voice_id="v", backend="claude")) == (
        '{"type": "agent-roster", "kind": "created", "session": "s", '
        '"persona": "P", "voice_id": "v", "backend": "claude"}')


def test_agent_focus_keeps_null_agent_id():
    assert wire(events.agent_focus(session="s", agent_id=None)) == (
        '{"type": "agent-focus", "session": "s", "agent_id": null}')


def test_agent_state_stop_shape():
    assert wire(events.agent_state(session="s", agent_id="a",
                                   kind=AgentState.INTERRUPTED)) == (
        '{"type": "agent-state", "session": "s", "agent_id": "a", "kind": "interrupted"}')


def test_queue_updated_shape_with_and_without_client_msg_id():
    base = dict(session="s", agent_id="a", queue_depth=1, queue_paused=False,
                queue_started=False, queue_revision=7)
    assert wire(events.queue_updated(**base)) == (
        '{"type": "queue-updated", "session": "s", "agent_id": "a", "queue_depth": 1, '
        '"queue_paused": false, "queue_started": false, "queue_revision": 7}')
    assert list(events.queue_updated(client_msg_id="m", **base)) == [
        "type", "session", "agent_id", "client_msg_id", "queue_depth",
        "queue_paused", "queue_started", "queue_revision"]


def test_audio_preserves_each_producers_order_and_requires_url_and_session():
    herald = events.audio(url="/audio/x.mp3", name="x.mp3", session="s",
                          herald=True, clip_id=3, trace_id="t")
    assert list(herald) == ["type", "url", "name", "session", "herald", "clip_id", "trace_id"]
    replay = events.audio(clip_id=3, session="s", agent_id="a", persona="P",
                          trace_id="t", ts=1, url="/audio/x.mp3")
    assert list(replay) == ["type", "clip_id", "session", "agent_id", "persona",
                            "trace_id", "ts", "url"]
    preview = events.audio(url="/audio/p.mp3", name="p.mp3", session="s", preview=True)
    assert wire(preview) == (
        '{"type": "audio", "url": "/audio/p.mp3", "name": "p.mp3", "session": "s", "preview": true}')
    with pytest.raises(ValueError):
        events.audio(url="/audio/x.mp3")
    with pytest.raises(ValueError):
        events.audio(session="s")


def test_provider_limit_puts_schema_version_before_type():
    event = events.provider_limit(
        schema_version=1, provider_limit_event_id="e", episode_id="p",
        provider_instance_id="i", provider_id="codex", window_id="w",
        kind="hard_limit", threshold_id=None, used_percentage=None,
        resets_at=None, observed_at="x", freshness="fresh",
        source={"kind": "s"}, dedupe_key=None)
    assert list(event)[:3] == ["schema_version", "type", "provider_limit_event_id"]
    assert event["type"] == SSEType.PROVIDER_LIMIT


def test_remote_action_plain_and_controller_shapes():
    assert wire(events.remote_action("record", 5)) == (
        '{"type": "remote-action", "action": "record", "ts": 5}')
    built = build_controller_event({
        "action": "controller-event", "controller_event_id": "c1",
        "button": "primary", "controller_event": "down", "controller_id": "flic",
    }, now_ms=9)
    adopted = events.remote_action_from(built)
    assert isinstance(adopted, events.Event)
    assert wire(adopted) == wire(built)


def test_orchestrator_decision_shape():
    assert wire(events.orchestrator_decision(
        decision_id="d", trace_id="t", action="route", kind="route",
        target_session="s", confidence=0.5, reason="r")) == (
        '{"type": "orchestrator-decision", "decision_id": "d", "trace_id": "t", '
        '"action": "route", "kind": "route", "target_session": "s", '
        '"confidence": 0.5, "reason": "r"}')


def test_location_calendar_artifact_attention_shapes():
    assert wire(events.location_request(session="s")) == (
        '{"type": "location-request", "session": "s"}')
    assert list(events.calendar_request(
        request_id="r", session="s", title="t", start="a", end="b",
        time_zone="", location="", notes="", url="", all_day=False,
        calendar="")) == [
        "type", "request_id", "session", "title", "start", "end", "time_zone",
        "location", "notes", "url", "all_day", "calendar"]
    assert wire(events.artifact_updated(session="s", agent_id="a", artifact_id="x")) == (
        '{"type": "artifact-updated", "session": "s", "agent_id": "a", "artifact_id": "x"}')
    assert wire(events.attention_updated(attention_count=2)) == (
        '{"type": "attention-updated", "attention_count": 2}')
    assert wire(events.server_version("v9")) == '{"type": "server-version", "version": "v9"}'


def test_event_is_a_dict_that_compares_and_serialises_like_one():
    event = events.agent_focus(session="s", agent_id="a")
    assert event == {"type": "agent-focus", "session": "s", "agent_id": "a"}
    assert dict(event) == event
    assert json.loads(json.dumps(event)) == event


def test_broadcast_accepts_only_typed_events():
    stream = FakeStream()
    typed = events.agent_roster("deleted", session="s")
    events.broadcast(stream, typed)
    events.broadcast_ephemeral(stream, events.remote_action("record", 1))
    assert stream.durable == [typed]
    assert stream.ephemeral[0]["action"] == "record"
    with pytest.raises(TypeError):
        events.broadcast(stream, {"type": "agent-roster", "kind": "deleted"})
    with pytest.raises(TypeError):
        events.broadcast_ephemeral(stream, {"type": "remote-action", "action": "record"})
    assert len(stream.durable) == 1 and len(stream.ephemeral) == 1


def test_as_event_readmits_a_round_tripped_payload_in_order():
    original = events.agent_state(session="s", agent_id="a", kind="done", trace_id="t")
    original["event_id"] = 12  # added by the stream hub after recording
    restored = events.as_event(json.loads(json.dumps(original)))
    assert isinstance(restored, events.Event)
    assert list(restored) == list(original)
    assert events.as_event(original) is original
