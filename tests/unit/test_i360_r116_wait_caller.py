"""Issue 360, R116: arming a peer wait, and settling one as not delivered, name
the verified caller as the actor, as recording a reply does. A verified agent
acting on another agent's goal was recorded as that goal's owner, unverified,
and who really acted was lost."""
from __future__ import annotations

from lib import db, peer_requests as pr, turn_identity

from test_peer_requests import _goal, pair  # noqa: F401 - fixture


def _last_actor(plan_id):
    row = db.conn().execute(
        "SELECT actor_kind, actor_agent_id FROM goal_events WHERE plan_id=? ORDER BY event_id DESC LIMIT 1",
        (plan_id,)).fetchone()
    return row["actor_kind"], row["actor_agent_id"]


def test_arming_a_wait_names_the_verified_caller(pair, monkeypatch):
    pip, avana = pair
    plan = _goal(pip)
    monkeypatch.setattr(turn_identity, "caller_agent_id", lambda environ=None: avana["agent_id"])
    pr.wait_on(pip, plan["plan_id"], pr.new_id(), avana, deadline_s=3600)
    assert _last_actor(plan["plan_id"]) == ("peer", avana["agent_id"])


def test_settling_a_wait_as_undelivered_names_the_verified_caller(pair, monkeypatch):
    pip, avana = pair
    plan = _goal(pip)
    request_id = pr.new_id()
    pr.wait_on(pip, plan["plan_id"], request_id, avana, deadline_s=3600)
    monkeypatch.setattr(turn_identity, "caller_agent_id", lambda environ=None: avana["agent_id"])
    assert pr.abandon_wait(plan["plan_id"], request_id, "refused") is True
    assert _last_actor(plan["plan_id"]) == ("peer", avana["agent_id"])


def test_the_owner_itself_is_still_the_owner(pair, monkeypatch):
    pip, avana = pair
    plan = _goal(pip)
    monkeypatch.setattr(turn_identity, "caller_agent_id", lambda environ=None: pip["agent_id"])
    pr.wait_on(pip, plan["plan_id"], pr.new_id(), avana, deadline_s=3600)
    assert _last_actor(plan["plan_id"]) == ("owner", pip["agent_id"])
