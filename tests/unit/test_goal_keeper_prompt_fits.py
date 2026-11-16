"""The keeper's prompt is built in bounded time whatever the goal holds.

On 16 Nov 2026 a goal whose recorded part and fact sheet were 100,051 bytes
(51 over the budget) with three characters of new activity kept the keeper's
thread in `prompt_for` for ever: three characters cut by a quarter are three
characters. The thread held the interpreter and the agent list took 10 to 30 s.
"""
from __future__ import annotations

import threading

import pytest

from lib import goal_keeper

MARK = "NEW ACTIVITY SINCE LAST UPDATE (untrusted):\n"


def _plan(progress: str) -> dict:
    return {"plan_id": "a:b:c", "title": "t", "status": "active", "items": [],
            "goal": {"outcome": "o", "criteria": [],
                     "checkpoint": {"progress": progress, "next_work": ""}}}


def _built(plan, text):
    """The prompt, or a failure when building it does not end."""
    out = {}
    worker = threading.Thread(
        target=lambda: out.setdefault("prompt", goal_keeper.prompt_for(plan, {}, text)),
        daemon=True)
    worker.start()
    worker.join(20)
    assert "prompt" in out, "prompt_for did not return"
    return out["prompt"]


def _head(plan) -> int:
    return len(_built(plan, "x").split(MARK)[0].encode()) + len(MARK)


def _plan_with_room(room: int) -> dict:
    """A goal whose head leaves `room` bytes of the budget (negative: over it)."""
    plan = _plan("p" * (goal_keeper.PROMPT_BYTES - _head(_plan("")) - room))
    assert _head(plan) == goal_keeper.PROMPT_BYTES - room
    return plan


@pytest.mark.parametrize("text", ["sh.", "a", "ab", "åå", "x" * 5000, ""])
@pytest.mark.parametrize("over", [51, 1, 0, 40_000])
def test_a_head_at_or_over_the_budget_still_gives_a_prompt(text, over):
    prompt = _built(_plan_with_room(-over), text)
    # No room: nothing of the activity is carried, and the head is whole.
    assert prompt.endswith(MARK)
    assert len(prompt.encode()) == goal_keeper.PROMPT_BYTES + over


@pytest.mark.parametrize("room", [1, 2, 3, 7])
def test_a_few_bytes_of_room_take_the_end_of_the_activity(room):
    text = "0123456789" * 30
    prompt = _built(_plan_with_room(room), text)
    body = prompt.split(MARK, 1)[1]
    assert len(prompt.encode()) <= goal_keeper.PROMPT_BYTES
    assert len(body) <= room and text.endswith(body)


def test_activity_that_fits_is_carried_whole():
    assert _built(_plan("short"), "what happened").endswith(MARK + "what happened")
    assert _built(_plan("short"), "").endswith(MARK + "(nothing new)")
