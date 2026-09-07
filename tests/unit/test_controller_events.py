import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "server"))

from lib.controller_events import (  # noqa: E402
    CONTROLLER_EVENTS,
    ControllerEventError,
    build_controller_event,
)


def test_build_controller_event_keeps_only_bounded_contract_fields():
    event = build_controller_event({
        "action": "controller-event",
        "controller_id": "flic-duo-1",
        "controller_event_id": "press-42",
        "button": "secondary",
        "controller_event": "swipe-left",
        "duration_ms": 640,
        "age_ms": 0,
        "queued": False,
        "ignored": "never broadcast",
    }, now_ms=1234)

    assert event == {
        "type": "remote-action",
        "action": "controller-event",
        "controller_id": "flic-duo-1",
        "controller_event_id": "press-42",
        "button": "secondary",
        "controller_event": "swipe-left",
        "duration_ms": 640,
        "age_ms": 0,
        "queued": False,
        "ts": 1234,
    }


@pytest.mark.parametrize("controller_event", sorted(CONTROLLER_EVENTS))
def test_every_controller_event_is_accepted(controller_event):
    event = build_controller_event({
        "button": "primary",
        "controller_event": controller_event,
    }, now_ms=1234, event_id="generated-id")
    assert event["controller_event"] == controller_event
    assert event["controller_event_id"] == "generated-id"


@pytest.mark.parametrize("payload,error", [
    ({"controller_event": "down"}, "button required"),
    ({"button": "third", "controller_event": "down"}, "unknown button"),
    ({"button": "primary"}, "controller_event required"),
    ({"button": "primary", "controller_event": "shake"},
     "unknown controller_event"),
    ({"button": "primary", "controller_event": "down", "queued": 1},
     "queued must be a boolean"),
    ({"button": "primary", "controller_event": "down", "age_ms": -1},
     "age_ms out of range"),
    ({"button": "primary", "controller_event": "down",
      "duration_ms": 120_001}, "duration_ms out of range"),
])
def test_controller_event_rejects_malformed_input(payload, error):
    with pytest.raises(ControllerEventError, match=error):
        build_controller_event(payload, now_ms=1234)
