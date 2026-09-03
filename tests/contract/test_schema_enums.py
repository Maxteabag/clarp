"""Schema enums stay pinned to server/lib/protocol.py.

The schemas are hand-written, so this test is the drift guard: any new
wire value added in Python without updating contract/schemas (or vice
versa) fails here.
"""
from __future__ import annotations

import json
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "server"))

from lib.protocol import (  # noqa: E402
    ActivityStatus,
    AgentState,
    ClientAction,
    ClipStatus,
    SSEType,
)

SCHEMAS = REPO / "contract" / "schemas"

#: The core event types a third client must implement (docs/protocol.md
#: compatibility policy). Everything else on the wire is an extension
#: surface the client may ignore.
CORE_EVENTS = {
    "transcript-updated",
    "agent-state",
    "agent-activity",
    "agent-roster",
    "agent-focus",
    "queue-updated",
    "user-notification",
    "audio",
    "tts-error",
    "server-version",
    "remote-action",
}


def _sse_defs() -> dict:
    return json.loads((SCHEMAS / "sse.json").read_text())["$defs"]


def test_core_events_are_all_known_wire_types():
    assert CORE_EVENTS <= set(SSEType.__dict__.values()), (
        "a core event is not a known SSEType; fix protocol.py or the list")


def test_sse_defs_cover_every_core_event():
    defs = _sse_defs()
    assert CORE_EVENTS <= set(defs), (
        f"sse.json misses core events: {sorted(CORE_EVENTS - set(defs))}")


def test_agent_state_enum_matches():
    assert set(_sse_defs()["agent-state-kind"]["enum"]) == AgentState.valid()


def test_activity_status_enum_matches():
    assert set(_sse_defs()["activity-status"]["enum"]) == ActivityStatus.valid()


def test_client_action_enum_matches():
    assert set(_sse_defs()["client-action"]["enum"]) == ClientAction.valid()


def test_clip_status_enum_matches():
    ack = json.loads((SCHEMAS / "clips-ack.json").read_text())
    assert set(ack["$defs"]["clip-status"]["enum"]) == ClipStatus.valid()
