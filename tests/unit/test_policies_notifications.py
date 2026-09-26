"""Table-driven decisions for the completed-turn notification policy."""
import pytest

from lib import origins
from lib.policies.notifications import (
    AgentCapabilities, CompletedTurn, Notify, NotificationSettings, Suppress,
    notify_decision)

ALL_ORIGINS = sorted(
    origins.ROUTINE_AUTOMATION_ORIGINS | origins.CLIENT_SETTABLE_ORIGINS
    | origins.USER_FACING_ORIGINS | origins.SUPPRESSED_ORIGINS
    | {origins.MARKER_ORIGIN, ""}
)
CHAT = AgentCapabilities(present=True)
LEADER = AgentCapabilities(present=True, is_team_leader=True)
JANITOR = AgentCapabilities(present=True, can_chat=False)
STRICT = NotificationSettings(special_automation=True)
LAX = NotificationSettings(special_automation=False)


def turn(content="speak", *, agent_id="a1", has_cause=True, interrupted=False):
    return CompletedTurn(agent_id=agent_id, has_cause=has_cause,
                         content=content, interrupted=interrupted)


@pytest.mark.parametrize("origin", ALL_ORIGINS)
@pytest.mark.parametrize("content", ["speak", "text-reply"])
def test_special_treatment_off_notifies_every_non_janitor_origin(origin, content):
    decision = notify_decision(origin, turn(content), CHAT, LAX)
    if origin == "janitor":
        assert decision == Suppress("janitor-maintenance")
    else:
        assert decision == Notify(reason=content, kind=content, push=True, muted=False)


@pytest.mark.parametrize("origin", ALL_ORIGINS)
def test_special_treatment_on_pages_only_user_facing_origins(origin):
    decision = notify_decision(origin, turn(), CHAT, STRICT)
    if origin == "janitor":
        assert decision == Suppress("janitor-maintenance")
    elif origin == "leader_tick":
        assert decision == Suppress("leader-tick-non-leader")
    elif origin in origins.USER_FACING_ORIGINS:
        assert decision == Notify(reason="speak", kind="speak", push=True, muted=False)
    else:
        assert decision == Suppress(f"not-user-facing-origin:{origin or 'unknown'}")


def test_leader_tick_pages_when_the_agent_leads_a_nudging_team():
    assert notify_decision("leader_tick", turn(), LEADER, STRICT) == Notify(
        reason="speak", kind="speak", push=True, muted=False)
    # Without special treatment the leader check is not consulted at all.
    assert isinstance(notify_decision("leader_tick", turn(), CHAT, LAX), Notify)


@pytest.mark.parametrize("origin", ALL_ORIGINS)
def test_janitor_agents_are_quiet_regardless_of_origin_or_setting(origin):
    for settings in (STRICT, LAX):
        assert notify_decision(origin, turn(), JANITOR, settings) == Suppress(
            "janitor-maintenance")


def test_janitor_origin_is_quiet_even_for_an_unknown_agent():
    assert notify_decision("janitor", turn(agent_id=""), AgentCapabilities(), LAX) == \
        Suppress("janitor-maintenance")


def test_missing_agent_precedes_missing_cause():
    absent = AgentCapabilities(present=False)
    assert notify_decision("user", turn(agent_id="", has_cause=False), absent, STRICT) == \
        Suppress("missing-agent")
    assert notify_decision("user", turn(has_cause=False), absent, STRICT) == \
        Suppress("missing-causing-row")


@pytest.mark.parametrize("content,interrupted,expected", [
    ("", False, Suppress("no-user-facing-content")),
    ("", True, Suppress("turn-interrupted")),
    ("speak", True, Notify(reason="speak", kind="speak", push=True, muted=False)),
    ("text-reply", False, Notify(reason="text-reply", kind="text-reply", push=True, muted=False)),
    ("odd-kind", False, Suppress("odd-kind")),
])
def test_content_and_interruption(content, interrupted, expected):
    assert notify_decision("user", turn(content, interrupted=interrupted), CHAT, STRICT) == expected


@pytest.mark.parametrize("content", ["speak", "text-reply"])
def test_muted_agent_keeps_badge_but_not_push(content):
    muted = AgentCapabilities(present=True, muted=True)
    assert notify_decision("user", turn(content), muted, STRICT) == Notify(
        reason=f"{content}-muted", kind=content, push=False, muted=True)
    # Mute never turns a suppressed turn into a muted-notify one.
    assert notify_decision("user", turn(""), muted, STRICT) == Suppress("no-user-facing-content")


@pytest.mark.parametrize("origin", ALL_ORIGINS)
@pytest.mark.parametrize("present", [False, True])
def test_desktop_presence_does_not_change_classification(origin, present):
    for caps in (CHAT, LEADER, JANITOR):
        for settings in (STRICT, LAX):
            assert notify_decision(origin, turn(), caps, settings, desktop_present=present) == \
                notify_decision(origin, turn(), caps, settings)


def test_origin_is_stripped():
    assert notify_decision("  agent ", turn(), CHAT, STRICT) == Suppress("not-user-facing-origin:agent")
