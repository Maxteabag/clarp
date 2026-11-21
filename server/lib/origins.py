"""Canonical origin classifications for agent turns.

A turn's ``origin`` records what *caused* it (``user``, ``oracle``, ``agent``, ``heartbeat``,
``leader_tick``, ``dreaming``, ``janitor``, ``schedule``, ``automation``). Several subsystems
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

# Rows the server writes on its own behalf into a conversation — today only
# the "turn interrupted by server restart" marker. Not automation (the user is
# meant to see it) and not a reply (it must never be pushed as one).
MARKER_ORIGIN = "system"

# Axis A — our own scheduled automation, never a real external signal.
ROUTINE_AUTOMATION_ORIGINS = frozenset({"heartbeat", "leader_tick", "dreaming", "janitor"})

# Origins by which the user's own word reaches an agent: the user's send from
# an app (and an HTML form they submit), and the Oracle carrying what they said.
# A message with one of these is never coordination between agents
# (lib.coordination), and a send made from an agent's own turn never keeps one
# (lib.send_request): it is that agent's, whatever origin it claims.
USER_WORD_ORIGINS = frozenset({"user", "oracle"})

# A card's answer the user gave in the app (``artifact_decisions.resolved_by``
# 'user'), or that a session the owner designated answered on their behalf
# ('agent:<session>', written only after the Host checked the designation and
# the session's turn token): the Host delivers it with origin 'automation', yet
# it is the user's word. An expired or dismissed card is not an answer.
ANSWERED_CHOICES = frozenset({"accepted", "rejected", "answered"})


def is_user_word(origin: str | None, *, card_answered_by: str = "",
                 card_choice: str = "") -> bool:
    """Whether work reaching an agent carries the user's own word, from what
    the Host resolved itself: the origin it settled at admission (a send from
    an agent's own turn or a peer Host never keeps the user's origin,
    lib.send_request), or a card answered by the user or on their behalf.
    It decides a queued item's place in line (lib.turn_queue, issue 481). A
    front desk's relay is marked where its turn token is checked
    (``SendRequest.front_desk``)."""
    if (origin or "").strip() in USER_WORD_ORIGINS:
        return True
    answered_by = card_answered_by or ""
    return ((answered_by == "user" or answered_by.startswith("agent:"))
            and card_choice in ANSWERED_CHOICES)

# Origins a client may set on POST /send. ``leader_tick`` and ``janitor``
# are stamped server-side only, never accepted from a client payload.
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


# Origins by which a person addresses an agent: the user writing or speaking
# to it, and the Oracle acting on the user's spoken words. Only these move the
# user's focus (the agent an un-named hands-free message goes to). What the
# Host sends on its own does not: a peer agent's message, a schedule, a goal
# taken up again, a delivered answer, a watcher, a heartbeat. With several
# agents working toward goals, the focus otherwise changed under the user.
FOCUS_ORIGINS = frozenset({"user", "oracle"})


# The automations' acknowledgement tokens (HEARTBEAT_OK, DREAMING_OK,
# LEADER_NOOP) count only in their own automation's turns. In any other turn
# a reply is kept verbatim, unless it is nothing but the token (AUT-07).
def ack_only_token(text: str, token: str) -> bool:
    """The reply says nothing besides the token (and punctuation)."""
    import re
    return token in text and not re.sub(r"\W+", "", text.replace(token, ""))


def ack_for_origin(result: tuple[bool, str], text: str, *, token: str,
                   origin: str, own: str) -> tuple[bool, str]:
    """`result` is the automation's own (skip, text) decision for `text`; it
    stands only in a turn of that automation (`origin == own`)."""
    if origin == own:
        return result
    if ack_only_token(text, token):
        return True, ""
    return False, text
