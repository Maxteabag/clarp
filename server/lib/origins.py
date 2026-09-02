"""Canonical origin classifications for agent turns.

A turn's ``origin`` records what *caused* it (``user``, ``oracle``, ``agent``, ``heartbeat``,
``leader_tick``, ``dreaming``, ``schedule``, ``automation``). Several subsystems
ask the same questions about an origin — "is this our own routine automation?",
"may this page the user?" — and historically each kept a private literal set. They
drifted: the dream snapshot filter forgot ``leader_tick``, so routine leader-tick
chatter ("Automated leader check" / "Leader check: no action needed.") leaked
into the read-only dream context. Centralize the definitions here so the next
origin only has to be added in one place.

Two distinct axes live here — do NOT collapse them into one set:

* ``ROUTINE_AUTOMATION_ORIGINS`` — turns the system generates on its own
  schedule (heartbeat, leader tick, dreaming). Used to suppress self-wakes,
  skip the herald ping, strip routine chatter from dream snapshots, classify
  automation display text, and gate the dream busy-check.

* ``USER_FACING_ORIGINS`` / ``SUPPRESSED_ORIGINS`` — notification policy: who a
  completed turn may page. ``leader_tick`` is routine automation BUT is the
  explicit autonomous-leader-to-User path, so it is user-facing, *not*
  suppressed. That deliberate flip is exactly why a single blob constant is
  wrong and these stay separate.

The iOS client keeps its own mirror of ``ROUTINE_AUTOMATION_ORIGINS`` in
``ClarpModels.swift`` (``isAutomated``); it can't import this module, so any
change to the routine set must be mirrored there. The coupling is guarded by
``tests/unit/test_origins.py`` and ``ios-native/CoreBehaviorTests/main.swift``.
"""
from __future__ import annotations

# Axis A — our own scheduled automation, never a real external signal.
ROUTINE_AUTOMATION_ORIGINS = frozenset({"heartbeat", "leader_tick", "dreaming"})

# Origins a client may set on POST /send. ``leader_tick`` is intentionally
# absent: it is stamped server-side only, never accepted from a client payload.
CLIENT_SETTABLE_ORIGINS = frozenset(
    {"user", "oracle", "agent", "schedule", "automation", "watcher", "heartbeat", "dreaming"}
)

# Axis B — notification policy. ``leader_tick`` flips to user-facing on purpose
# (the autonomous leader's report channel), so it is excluded from the
# suppressed set even though it is routine automation.
USER_FACING_ORIGINS = frozenset({"user", "leader_tick", "watcher"})
SUPPRESSED_ORIGINS = (
    (ROUTINE_AUTOMATION_ORIGINS - {"leader_tick"})
    | {"oracle", "agent", "schedule", "automation"}
)


def is_routine_automation(origin: str | None) -> bool:
    """True when ``origin`` is one of our own scheduled automation turns."""
    return (origin or "").strip() in ROUTINE_AUTOMATION_ORIGINS
