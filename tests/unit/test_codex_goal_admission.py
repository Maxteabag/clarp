# Reviewer A's probe on 0dabadfb (WIP f0d6105b), adapted (see below) as the round-9 regression.
"""Coarse goal-call admission shares the retirement fence for every action."""
import threading

from lib import agents, codex_app_server as cas
from tests.unit.test_codex_goal_coarse import _loaded_goal
from lib import agent_goals


def test_a_goal_call_picked_before_retirement_cannot_enter_after_its_mark(tmp_path, monkeypatch):
    client, agent = _loaded_goal(tmp_path, monkeypatch, agent_goals.ACTIVE)
    monkeypatch.setattr(cas, '_auth_fingerprint', lambda: b'old')
    # Round 9 adaptation (disclosed): the user-paused state is set in the
    # mirror directly; a goal() pause's own answer end now re-triggers the
    # recycle (round 8), which would retire the client before this probe.
    agent_goals.upsert(agent['agent_id'], session=agent['session'], backend='codex',
        goal=agent_goals.from_codex({'objective': 'synthetic', 'status': 'paused'}))
    looked_up = threading.Event()
    begin = threading.Event()
    request_or_done = threading.Event()
    reply = threading.Event()
    outstanding = threading.Event()
    errors, killed_during_answer = [], []
    goal = client.goal

    def after_pool_lookup(*args, **kwargs):
        looked_up.set()
        assert begin.wait(5)
        return goal(*args, **kwargs)

    monkeypatch.setattr(client, 'goal', after_pool_lookup)

    def provider_reply(method, params, timeout=30):
        assert method == 'thread/goal/get'
        outstanding.set()
        request_or_done.set()
        assert reply.wait(5)
        outstanding.clear()
        return {'goal': {'objective': 'synthetic', 'status': 'paused'}}

    client.request = provider_reply
    client.proc.kill.side_effect = lambda: killed_during_answer.append(outstanding.is_set())
    may_continue = client._goal_may_continue

    def admit_after_quiet_snapshot():
        result = may_continue()
        begin.set()
        assert request_or_done.wait(5)
        return result

    monkeypatch.setattr(client, '_goal_may_continue', admit_after_quiet_snapshot)

    def public_goal_call():
        try:
            cas.goal(agent['agent_id'], 'get')  # real pool lookup, then actual goal method
        except Exception as exc:
            errors.append(exc)
        finally:
            request_or_done.set()

    worker = threading.Thread(target=public_goal_call)
    worker.start()
    try:
        assert looked_up.wait(5), 'control: caller obtained the shared client before retirement'
        client.recycle_if_quiet()
    finally:
        begin.set()
        reply.set()
        worker.join(5)
    assert not worker.is_alive()
    assert all(isinstance(error, RuntimeError) for error in errors), errors
    assert not any(killed_during_answer), 'a goal answer began after the retirement mark and was killed outstanding'
