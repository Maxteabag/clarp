"""Typed SSE events: one constructor per ``SSEType`` member.

Every event that reaches the wire is built here. A constructor returns an
``Event`` (a ``dict`` whose class is the proof it was built by this module)
with exactly the keys, in exactly the order, that the emit sites produced
before the constructors existed; ``docs/protocol.md`` documents the same
keys and a schema test cross-checks the two.

``broadcast(stream, event)`` and ``broadcast_ephemeral(stream, event)`` are
the only way an owned emit site pushes to the stream hub; both refuse a
plain dict. Payloads that crossed a serialisation boundary (rows read back
from SQLite, events carried inside another response) come back through
``as_event`` which re-validates the type and key set.

Optional fields use the ``OMIT`` sentinel so a caller can still send an
explicit ``None`` (the wire has both "absent" and ``null``; ``agent-focus``
sends ``null`` when no agent owns the session, for example).
"""
from __future__ import annotations

from typing import Any, Callable, Mapping

from .protocol import SSEType

OMIT: Any = object()


class Event(dict):
    """A wire-ready SSE payload. Only instances built here reach ``broadcast``."""
    __slots__ = ()


# The closed key set (``type`` excluded) for each event type. Order is the
# canonical documentation order; constructors decide the wire order.
FIELDS: dict[str, tuple[str, ...]] = {
    SSEType.AUDIO: (
        "clip_id", "url", "name", "session", "agent_id", "persona",
        "trace_id", "streamable", "delivery", "stream_url", "playlist_url",
        "complete_url", "audio_format", "preview", "herald", "voice_id",
        "phrase_key", "cached_phrase", "replay", "ts",
    ),
    SSEType.SERVER_VERSION: ("version",),
    SSEType.REMOTE_ACTION: (
        "action", "ts", "controller_event_id", "button", "controller_event",
        "duration_ms", "age_ms", "queued", "controller_id",
    ),
    SSEType.AGENT_STATE: (
        "agent_id", "session", "persona", "kind", "ts", "detail",
        "status_text", "trace_id", "client_msg_id", "queue_started",
        "queue_remaining", "origin",
    ),
    SSEType.AGENT_ACTIVITY: (
        "agent_id", "session", "persona", "kind", "phase", "status", "tool",
        "action", "summary", "file_path", "ts",
    ),
    SSEType.AGENT_ROSTER: (
        "kind", "session", "persona", "voice_id", "backend", "name",
    ),
    SSEType.AGENT_FOCUS: ("session", "agent_id"),
    SSEType.TRANSCRIPT_UPDATED: ("agent_id", "session", "backend_session_id"),
    SSEType.QUEUE_UPDATED: (
        "agent_id", "session", "queue_depth", "queue_paused", "queue_started",
        "queue_revision", "client_msg_id",
    ),
    SSEType.ARTIFACT_UPDATED: ("session", "agent_id", "artifact_id"),
    SSEType.ATTENTION_UPDATED: ("attention_count",),
    SSEType.BACKGROUND_JOB_UPDATED: (
        "change_revision", "observed_at", "job_id", "session", "agent_id",
        "status", "job",
    ),
    SSEType.PROVIDER_LIMIT: (
        "schema_version", "provider_limit_event_id", "episode_id",
        "provider_instance_id", "provider_id", "window_id", "kind",
        "threshold_id", "used_percentage", "resets_at", "observed_at",
        "freshness", "source", "dedupe_key",
    ),
    SSEType.USER_NOTIFICATION: (
        "notification_id", "agent_id", "session", "persona", "done_ts",
        "source_message_id", "cause_message_id", "origin", "push", "badge",
        "unread", "muted", "preview", "reason",
    ),
    SSEType.TTS_ERROR: ("session", "agent_id", "persona", "message", "error"),
    SSEType.LOCATION_REQUEST: ("session",),
    SSEType.CALENDAR_REQUEST: (
        "request_id", "session", "title", "start", "end", "time_zone",
        "location", "notes", "url", "all_day", "calendar",
    ),
    SSEType.GOAL_UPDATED: ("agent_id", "session", "goal"),
    SSEType.ORCHESTRATOR_DECISION: (
        "decision_id", "trace_id", "action", "kind", "target_session",
        "confidence", "reason",
    ),
}

# Keys the stream hub adds after construction; tolerated on re-validation.
_HUB_KEYS = frozenset({"event_id"})


def _build(event_type: str, pairs: list[tuple[str, Any]], *,
           type_first: bool = True) -> Event:
    allowed = FIELDS[event_type]
    event = Event()
    if type_first:
        event["type"] = event_type
    for key, value in pairs:
        if value is OMIT:
            continue
        if key == "type":
            event["type"] = event_type
            continue
        if key not in allowed:
            raise ValueError(f"{event_type}: unknown field {key!r}")
        event[key] = value
    return event


# ---- constructors ------------------------------------------------------

def audio(**fields: Any) -> Event:
    """A voice clip is ready. ``url`` and ``session`` are required.

    Audio has five producers with five different field orders (the herald,
    the TTS worker, replay, cached phrases, previews). Keyword order is kept
    so each producer's payload stays byte-identical; the key set is closed.
    """
    for required in ("url", "session"):
        if required not in fields:
            raise ValueError(f"audio: {required} is required")
    return _build(SSEType.AUDIO, list(fields.items()))


def server_version(version: str) -> Event:
    return _build(SSEType.SERVER_VERSION, [("version", version)])


def remote_action(action: str, ts: int, *,
                  controller_event_id: Any = OMIT, button: Any = OMIT,
                  controller_event: Any = OMIT, duration_ms: Any = OMIT,
                  age_ms: Any = OMIT, queued: Any = OMIT,
                  controller_id: Any = OMIT) -> Event:
    return _build(SSEType.REMOTE_ACTION, [
        ("action", action),
        ("controller_event_id", controller_event_id),
        ("button", button),
        ("controller_event", controller_event),
        ("duration_ms", duration_ms),
        ("age_ms", age_ms),
        ("queued", queued),
        ("ts", ts),
        ("controller_id", controller_id),
    ])


def remote_action_from(payload: Mapping[str, Any]) -> Event:
    """Adopt a controller event built by ``controller_events``."""
    fields = dict(payload)
    if fields.pop("type", SSEType.REMOTE_ACTION) != SSEType.REMOTE_ACTION:
        raise ValueError("remote_action_from: not a remote-action payload")
    return remote_action(fields.pop("action"), fields.pop("ts"), **fields)


def agent_state(*, session: str, agent_id: str, kind: str,
                persona: Any = OMIT, ts: Any = OMIT, detail: Any = OMIT,
                status_text: Any = OMIT, trace_id: Any = OMIT,
                client_msg_id: Any = OMIT, queue_started: Any = OMIT,
                queue_remaining: Any = OMIT, origin: Any = OMIT) -> Event:
    return _build(SSEType.AGENT_STATE, [
        ("session", session),
        ("agent_id", agent_id),
        ("kind", kind),
        ("persona", persona),
        ("ts", ts),
        ("detail", detail),
        ("status_text", status_text),
        ("trace_id", trace_id),
        ("client_msg_id", client_msg_id),
        ("queue_started", queue_started),
        ("queue_remaining", queue_remaining),
        ("origin", origin),
    ])


def agent_activity(*, agent_id: str, session: str, persona: str, kind: str,
                   phase: str, status: str, tool: str, action: str,
                   summary: str, file_path: str, ts: int) -> Event:
    return _build(SSEType.AGENT_ACTIVITY, [
        ("agent_id", agent_id),
        ("session", session),
        ("persona", persona),
        ("kind", kind),
        ("phase", phase),
        ("status", status),
        ("tool", tool),
        ("action", action),
        ("summary", summary),
        ("file_path", file_path),
        ("ts", ts),
    ])


def agent_roster(kind: Any = OMIT, *, session: Any = OMIT,
                 persona: Any = OMIT, voice_id: Any = OMIT,
                 backend: Any = OMIT, name: Any = OMIT) -> Event:
    """Refetch the snapshot. Without ``kind`` it is the reconnect nudge."""
    return _build(SSEType.AGENT_ROSTER, [
        ("kind", kind),
        ("session", session),
        ("persona", persona),
        ("voice_id", voice_id),
        ("backend", backend),
        ("name", name),
    ])


def agent_focus(*, session: str, agent_id: str | None) -> Event:
    return _build(SSEType.AGENT_FOCUS, [
        ("session", session), ("agent_id", agent_id),
    ])


def transcript_updated(*, agent_id: str, session: str | None,
                       backend_session_id: Any = OMIT) -> Event:
    return _build(SSEType.TRANSCRIPT_UPDATED, [
        ("agent_id", agent_id),
        ("session", session),
        ("backend_session_id", backend_session_id),
    ])


def queue_updated(*, session: str, agent_id: str, queue_depth: int,
                  queue_paused: bool, queue_started: bool,
                  queue_revision: int, client_msg_id: Any = OMIT) -> Event:
    return _build(SSEType.QUEUE_UPDATED, [
        ("session", session),
        ("agent_id", agent_id),
        ("client_msg_id", client_msg_id),
        ("queue_depth", queue_depth),
        ("queue_paused", queue_paused),
        ("queue_started", queue_started),
        ("queue_revision", queue_revision),
    ])


def artifact_updated(*, session: str, agent_id: str,
                     artifact_id: str) -> Event:
    return _build(SSEType.ARTIFACT_UPDATED, [
        ("session", session),
        ("agent_id", agent_id),
        ("artifact_id", artifact_id),
    ])


def attention_updated(*, attention_count: int) -> Event:
    return _build(SSEType.ATTENTION_UPDATED, [
        ("attention_count", attention_count),
    ])


def background_job_updated(*, change_revision: int, observed_at: int,
                           job_id: str, session: str, agent_id: str,
                           status: str, job: dict[str, Any]) -> Event:
    return _build(SSEType.BACKGROUND_JOB_UPDATED, [
        ("change_revision", change_revision),
        ("observed_at", observed_at),
        ("job_id", job_id),
        ("session", session),
        ("agent_id", agent_id),
        ("status", status),
        ("job", job),
    ])


def provider_limit(*, schema_version: int, provider_limit_event_id: str,
                   episode_id: str, provider_instance_id: str,
                   provider_id: str, window_id: str, kind: str,
                   threshold_id: Any, used_percentage: Any, resets_at: Any,
                   observed_at: Any, freshness: str, source: dict[str, Any],
                   dedupe_key: Any) -> Event:
    # ``schema_version`` precedes ``type`` on this wire payload; keep it.
    return _build(SSEType.PROVIDER_LIMIT, [
        ("schema_version", schema_version),
        ("type", SSEType.PROVIDER_LIMIT),
        ("provider_limit_event_id", provider_limit_event_id),
        ("episode_id", episode_id),
        ("provider_instance_id", provider_instance_id),
        ("provider_id", provider_id),
        ("window_id", window_id),
        ("kind", kind),
        ("threshold_id", threshold_id),
        ("used_percentage", used_percentage),
        ("resets_at", resets_at),
        ("observed_at", observed_at),
        ("freshness", freshness),
        ("source", source),
        ("dedupe_key", dedupe_key),
    ], type_first=False)


def user_notification(*, notification_id: str, agent_id: str, session: str,
                      persona: str, done_ts: int, source_message_id: Any,
                      cause_message_id: Any, origin: str, push: bool,
                      badge: bool, unread: bool, muted: bool, preview: Any,
                      reason: Any) -> Event:
    return _build(SSEType.USER_NOTIFICATION, [
        ("notification_id", notification_id),
        ("agent_id", agent_id),
        ("session", session),
        ("persona", persona),
        ("done_ts", done_ts),
        ("source_message_id", source_message_id),
        ("cause_message_id", cause_message_id),
        ("origin", origin),
        ("push", bool(push)),
        ("badge", bool(badge)),
        ("unread", bool(unread)),
        ("muted", bool(muted)),
        ("preview", preview),
        ("reason", reason),
    ])


def tts_error(*, session: Any, agent_id: Any, persona: Any, message: str,
              error: str) -> Event:
    return _build(SSEType.TTS_ERROR, [
        ("session", session),
        ("agent_id", agent_id),
        ("persona", persona),
        ("message", message),
        ("error", error),
    ])


def location_request(*, session: str) -> Event:
    return _build(SSEType.LOCATION_REQUEST, [("session", session)])


def calendar_request(*, request_id: str, session: str, title: str,
                     start: str, end: str, time_zone: str, location: str,
                     notes: str, url: str, all_day: bool,
                     calendar: str) -> Event:
    return _build(SSEType.CALENDAR_REQUEST, [
        ("request_id", request_id),
        ("session", session),
        ("title", title),
        ("start", start),
        ("end", end),
        ("time_zone", time_zone),
        ("location", location),
        ("notes", notes),
        ("url", url),
        ("all_day", all_day),
        ("calendar", calendar),
    ])


def goal_updated(*, agent_id: str, session: str,
                 goal: dict[str, Any] | None) -> Event:
    return _build(SSEType.GOAL_UPDATED, [
        ("agent_id", agent_id),
        ("session", session),
        ("goal", goal),
    ])


def orchestrator_decision(*, decision_id: str, trace_id: str, action: str,
                          kind: str, target_session: str,
                          confidence: Any, reason: Any) -> Event:
    return _build(SSEType.ORCHESTRATOR_DECISION, [
        ("decision_id", decision_id),
        ("trace_id", trace_id),
        ("action", action),
        ("kind", kind),
        ("target_session", target_session),
        ("confidence", confidence),
        ("reason", reason),
    ])


CONSTRUCTORS: dict[str, Callable[..., Event]] = {
    SSEType.AUDIO: audio,
    SSEType.SERVER_VERSION: server_version,
    SSEType.REMOTE_ACTION: remote_action,
    SSEType.AGENT_STATE: agent_state,
    SSEType.AGENT_ACTIVITY: agent_activity,
    SSEType.AGENT_ROSTER: agent_roster,
    SSEType.AGENT_FOCUS: agent_focus,
    SSEType.TRANSCRIPT_UPDATED: transcript_updated,
    SSEType.QUEUE_UPDATED: queue_updated,
    SSEType.ARTIFACT_UPDATED: artifact_updated,
    SSEType.ATTENTION_UPDATED: attention_updated,
    SSEType.BACKGROUND_JOB_UPDATED: background_job_updated,
    SSEType.PROVIDER_LIMIT: provider_limit,
    SSEType.USER_NOTIFICATION: user_notification,
    SSEType.TTS_ERROR: tts_error,
    SSEType.LOCATION_REQUEST: location_request,
    SSEType.CALENDAR_REQUEST: calendar_request,
    SSEType.GOAL_UPDATED: goal_updated,
    SSEType.ORCHESTRATOR_DECISION: orchestrator_decision,
}


# ---- validation and delivery ------------------------------------------

def as_event(payload: Mapping[str, Any]) -> Event:
    """Re-admit a payload that crossed a serialisation boundary.

    An ``Event`` passes through. Any other mapping must name a known type
    and use only that type's documented keys; order is preserved.
    """
    if isinstance(payload, Event):
        return payload
    event_type = payload.get("type")
    if event_type not in FIELDS:
        raise ValueError(f"unknown SSE event type {event_type!r}")
    allowed = set(FIELDS[event_type]) | _HUB_KEYS | {"type"}
    unknown = [key for key in payload if key not in allowed]
    if unknown:
        raise ValueError(f"{event_type}: unknown field(s) {unknown!r}")
    event = Event()
    for key, value in payload.items():
        event[key] = value
    return event


def require(event: object) -> Event:
    if not isinstance(event, Event):
        raise TypeError(
            "broadcast() accepts only events built by lib.events; got "
            f"{type(event).__name__}")
    return event


def broadcast(stream: Any, event: Event) -> None:
    """Push a typed event through the stream hub (durable, replayed)."""
    stream.broadcast(require(event))


def broadcast_ephemeral(stream: Any, event: Event) -> None:
    """Push a typed event that must never be replayed after reconnect."""
    stream.broadcast_ephemeral(require(event))
