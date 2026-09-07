"""Validation for live physical-controller events.

Controller input is deliberately ephemeral.  A button edge that is replayed
after an SSE reconnect can start or stop a recording long after the person
pressed it, so callers must broadcast the returned event with
``AudioStream.broadcast_ephemeral``.
"""
from __future__ import annotations

import secrets
import time

from .protocol import ClientAction, SSEType


class ControllerEventError(ValueError):
    """A remote controller payload is malformed or outside safe bounds."""


CONTROLLER_BUTTONS = frozenset({"primary", "secondary"})
CONTROLLER_EVENTS = frozenset({
    "down",
    "up",
    "single-click",
    "double-click",
    "hold",
    "swipe-left",
    "swipe-right",
    "swipe-up",
    "swipe-down",
    # Reserved for a future, calibrated phone-side classifier. Flic officially
    # requires a Hub for Hold & Twist, so these cannot be emitted by Clarp's
    # stock phone-direct Flic adapter.
    "rotate-clockwise",
    "rotate-counterclockwise",
})


def _bounded_string(data: dict, key: str, *, required: bool,
                    maximum: int) -> str:
    value = str(data.get(key) or "").strip()
    if required and not value:
        raise ControllerEventError(f"{key} required")
    if len(value) > maximum:
        raise ControllerEventError(f"{key} too long")
    return value


def _bounded_integer(data: dict, key: str, *, default: int,
                     maximum: int) -> int:
    value = data.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ControllerEventError(f"{key} must be an integer")
    if value < 0 or value > maximum:
        raise ControllerEventError(f"{key} out of range")
    return value


def build_controller_event(data: dict, *, now_ms: int | None = None,
                           event_id: str | None = None) -> dict:
    """Return one bounded ``remote-action/controller-event`` SSE payload."""
    if not isinstance(data, dict):
        raise ControllerEventError("object body required")
    button = _bounded_string(data, "button", required=True, maximum=16)
    if button not in CONTROLLER_BUTTONS:
        raise ControllerEventError("unknown button")
    controller_event = _bounded_string(
        data, "controller_event", required=True, maximum=32)
    if controller_event not in CONTROLLER_EVENTS:
        raise ControllerEventError("unknown controller_event")
    queued = data.get("queued", False)
    if not isinstance(queued, bool):
        raise ControllerEventError("queued must be a boolean")
    controller_id = _bounded_string(
        data, "controller_id", required=False, maximum=128)
    controller_event_id = _bounded_string(
        data, "controller_event_id", required=False, maximum=128)
    controller_event_id = (
        controller_event_id or event_id or secrets.token_urlsafe(12))
    duration_ms = _bounded_integer(
        data, "duration_ms", default=0, maximum=120_000)
    age_ms = _bounded_integer(data, "age_ms", default=0, maximum=120_000)
    timestamp = _bounded_integer(
        data,
        "ts",
        default=now_ms if now_ms is not None else int(time.time() * 1000),
        maximum=9_999_999_999_999,
    )
    event = {
        "type": SSEType.REMOTE_ACTION,
        "action": ClientAction.CONTROLLER_EVENT,
        "controller_event_id": controller_event_id,
        "button": button,
        "controller_event": controller_event,
        "duration_ms": duration_ms,
        "age_ms": age_ms,
        "queued": queued,
        "ts": timestamp,
    }
    if controller_id:
        event["controller_id"] = controller_id
    return event
