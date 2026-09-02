"""Invariants for the canonical origin classification module.

These lock the two-axis design described in ``server/lib/origins.py`` so the
busy-gate / snapshot / notification drift that produced the "every dream skips
busy" and "leader-tick chatter leaks into the dream snapshot" bugs cannot
silently reappear when a new origin is added.
"""
from lib import origins


def test_routine_set_is_exactly_the_three_self_scheduled_origins():
    assert origins.ROUTINE_AUTOMATION_ORIGINS == {
        "heartbeat",
        "leader_tick",
        "dreaming",
    }


def test_is_routine_automation_normalizes_and_rejects_unknown():
    assert origins.is_routine_automation("heartbeat")
    assert origins.is_routine_automation("  dreaming  ")  # stripped
    assert not origins.is_routine_automation("user")
    assert not origins.is_routine_automation(None)
    assert not origins.is_routine_automation("")
    # Callers lowercase upstream; the module does not, so this documents that
    # the contract is "feed me a normalized origin".
    assert not origins.is_routine_automation("Heartbeat")


def test_leader_tick_is_routine_but_user_facing_and_not_suppressed():
    # The deliberate axis flip: routine automation, yet the explicit
    # autonomous-leader-to-User report channel.
    assert "leader_tick" in origins.ROUTINE_AUTOMATION_ORIGINS
    assert "leader_tick" in origins.USER_FACING_ORIGINS
    assert "leader_tick" not in origins.SUPPRESSED_ORIGINS


def test_user_facing_and_suppressed_are_disjoint():
    # No origin may both page the user and be silenced.
    assert origins.USER_FACING_ORIGINS & origins.SUPPRESSED_ORIGINS == set()


def test_watcher_is_client_settable_and_user_facing_but_not_routine_automation():
    assert "watcher" in origins.CLIENT_SETTABLE_ORIGINS
    assert "watcher" in origins.USER_FACING_ORIGINS
    assert "watcher" not in origins.SUPPRESSED_ORIGINS
    assert "watcher" not in origins.ROUTINE_AUTOMATION_ORIGINS


def test_oracle_is_authenticated_user_work_but_does_not_page_or_speak_twice():
    assert "oracle" in origins.CLIENT_SETTABLE_ORIGINS
    assert "oracle" in origins.SUPPRESSED_ORIGINS
    assert "oracle" not in origins.USER_FACING_ORIGINS
    assert "oracle" not in origins.ROUTINE_AUTOMATION_ORIGINS


def test_every_routine_origin_except_leader_tick_is_suppressed():
    assert (origins.ROUTINE_AUTOMATION_ORIGINS - {"leader_tick"}) <= (
        origins.SUPPRESSED_ORIGINS
    )


def test_leader_tick_is_never_client_settable():
    # Stamped server-side only; accepting it from a client payload would let any
    # caller forge the autonomous-leader report channel.
    assert "leader_tick" not in origins.CLIENT_SETTABLE_ORIGINS
    for ok in ("user", "oracle", "agent", "schedule", "automation", "watcher", "heartbeat", "dreaming"):
        assert ok in origins.CLIENT_SETTABLE_ORIGINS


def test_regression_busy_gate_and_snapshot_use_the_same_full_set():
    # origin/main shipped two disagreeing 2-element sets:
    #   busy gate     == {"heartbeat", "leader_tick"}   (missing "dreaming")
    #   snapshot strip == {"dreaming", "heartbeat"}      (missing "leader_tick")
    # The second omission leaked leader-tick chatter into the dream snapshot.
    # Both now route through ROUTINE_AUTOMATION_ORIGINS, so the union is whole.
    assert "dreaming" in origins.ROUTINE_AUTOMATION_ORIGINS
    assert "leader_tick" in origins.ROUTINE_AUTOMATION_ORIGINS
    assert "heartbeat" in origins.ROUTINE_AUTOMATION_ORIGINS
