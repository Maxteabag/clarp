"""Every SSE event type has one constructor, one protocol.md row, and the two agree."""
import json
import pathlib
import re
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))
from lib import events  # noqa: E402
from lib.protocol import SSEType  # noqa: E402

PROTOCOL_MD = ROOT / "docs" / "protocol.md"


def _documented_events() -> dict[str, set[str]]:
    """{wire type: documented field names} from the SSE table in protocol.md."""
    text = PROTOCOL_MD.read_text(encoding="utf-8")
    start = text.index("Event types and payloads:")
    end = text.index("\n\n", text.index("|---|", start))
    rows = {}
    for line in text[start:end].splitlines():
        if not line.startswith("| `") or line.startswith("| `type`"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        types = re.findall(r"`([a-z-]+)`", cells[0])
        fields = set(re.findall(r"`([a-z_]+)`", cells[1]))
        for event_type in types:
            rows[event_type] = fields
    return rows


# One call per constructor that exercises every optional field, so the
# produced key set is the full documented set.
FULL_SAMPLES = {
    SSEType.AUDIO: lambda: events.audio(
        clip_id=1, url="/audio/a.mp3", name="a.mp3", session="s",
        agent_id="a", persona="P", trace_id="t", streamable=True,
        delivery="hls", stream_url="/s", playlist_url="/p",
        complete_url="/c", audio_format={}, preview=True, herald=True,
        voice_id="v", phrase_key="k", cached_phrase=True, replay=True, ts=1),
    SSEType.SERVER_VERSION: lambda: events.server_version("1"),
    SSEType.REMOTE_ACTION: lambda: events.remote_action(
        "controller-event", 1, controller_event_id="c", button="primary",
        controller_event="down", duration_ms=0, age_ms=0, queued=False,
        controller_id="flic"),
    SSEType.AGENT_STATE: lambda: events.agent_state(
        session="s", agent_id="a", kind="thinking", persona="P", ts=1,
        detail={}, status_text="", trace_id="t", client_msg_id="m",
        queue_started=False, queue_remaining=0, origin="janitor"),
    SSEType.AGENT_ACTIVITY: lambda: events.agent_activity(
        agent_id="a", session="s", persona="P", kind="tool", phase="tool",
        status="running", tool="Read", action="read", summary="x",
        file_path="/f", ts=1),
    SSEType.AGENT_ROSTER: lambda: events.agent_roster(
        "created", session="s", persona="P", voice_id="v", backend="claude",
        name="N"),
    SSEType.AGENT_FOCUS: lambda: events.agent_focus(session="s", agent_id="a"),
    SSEType.TRANSCRIPT_UPDATED: lambda: events.transcript_updated(
        agent_id="a", session="s", backend_session_id="b"),
    SSEType.QUEUE_UPDATED: lambda: events.queue_updated(
        session="s", agent_id="a", queue_depth=0, queue_paused=False,
        queue_started=False, queue_revision=1, client_msg_id="m"),
    SSEType.ARTIFACT_UPDATED: lambda: events.artifact_updated(
        session="s", agent_id="a", artifact_id="x"),
    SSEType.ATTENTION_UPDATED: lambda: events.attention_updated(attention_count=0),
    SSEType.BACKGROUND_JOB_UPDATED: lambda: events.background_job_updated(
        change_revision=1, observed_at=1, job_id="j", session="s",
        agent_id="a", status="running", job={}),
    SSEType.PROVIDER_LIMIT: lambda: events.provider_limit(
        schema_version=1, provider_limit_event_id="e", episode_id="p",
        provider_instance_id="i", provider_id="codex", window_id="w",
        kind="hard_limit", threshold_id=None, used_percentage=100.0,
        resets_at=None, observed_at="2026-01-01T00:00:00Z",
        freshness="fresh", source={"kind": "x"}, dedupe_key=None),
    SSEType.USER_NOTIFICATION: lambda: events.user_notification(
        notification_id="n", agent_id="a", session="s", persona="P",
        done_ts=1, source_message_id="m", cause_message_id="c",
        origin="pwa", push=True, badge=True, unread=True, muted=False,
        preview="hi", reason="done"),
    SSEType.TTS_ERROR: lambda: events.tts_error(
        session="s", agent_id="a", persona="P", message="m", error="e"),
    SSEType.LOCATION_REQUEST: lambda: events.location_request(session="s"),
    SSEType.CALENDAR_REQUEST: lambda: events.calendar_request(
        request_id="r", session="s", title="t", start="x", end="y",
        time_zone="Europe/Oslo", location="", notes="", url="",
        all_day=False, calendar=""),
    SSEType.GOAL_UPDATED: lambda: events.goal_updated(
        agent_id="a", session="s", goal=None),
    SSEType.ORCHESTRATOR_DECISION: lambda: events.orchestrator_decision(
        decision_id="d", trace_id="t", action="route", kind="route",
        target_session="s", confidence=0.9, reason="r"),
}


def test_every_sse_type_has_a_constructor_a_field_set_and_a_doc_row():
    documented = _documented_events()
    for name, wire in SSEType.members().items():
        assert wire in events.CONSTRUCTORS, f"{name}: no constructor"
        assert wire in events.FIELDS, f"{name}: no field set"
        assert wire in FULL_SAMPLES, f"{name}: no test sample"
        assert wire in documented, f"{name}: no row in docs/protocol.md"
    assert set(documented) == SSEType.valid(), "protocol.md documents an unknown type"


@pytest.mark.parametrize("wire", sorted(SSEType.valid()))
def test_constructor_keys_match_documented_fields(wire):
    documented = _documented_events()[wire]
    assert documented == set(events.FIELDS[wire]), wire
    event = FULL_SAMPLES[wire]()
    assert isinstance(event, events.Event)
    assert event["type"] == wire
    assert set(event) - {"type"} == documented, wire
    json.dumps(event)  # wire-safe


def test_constructors_refuse_undocumented_fields():
    with pytest.raises(ValueError):
        events.audio(url="/a", session="s", colour="red")
    with pytest.raises(ValueError):
        events.as_event({"type": SSEType.AGENT_FOCUS, "session": "s", "extra": 1})
    with pytest.raises(ValueError):
        events.as_event({"type": "not-a-type"})
