"""Requests between agents that come back to the requester (peer_requests)."""
import pytest

from lib import agents, message_store, peer_requests as pr, task_plans
from lib.db import now_ms


@pytest.fixture
def pair(tmp_path):
    made = {}
    for session in ('pip', 'avana'):
        aid = agents.create_agent(persona=session.title(), voice_id='', cwd=str(tmp_path), session=session)
        agents.bind_backend_session(aid, f'{session}-native')
        made[session] = agents.get_by_session(session)
    return made['pip'], made['avana']


def _goal(agent, alias='game', **goal):
    return task_plans.create(session=agent['session'], plan_id=alias, title='Game',
                             items=[{'id': 'g', 'title': 'Game'}],
                             goal={'outcome': 'Ship the game', 'criteria': ['Shipped'], 'limits': 'none',
                                   'enroll': True, **goal})


def _sent(pip, avana, request_id):
    """What /send records for the request: the recipient's row, sender attached."""
    message_store.record_user_message(
        agent_id=avana['agent_id'], backend_session_id='avana-native',
        client_msg_id=pr.request_client_id(request_id), text=pr.request_text(request_id, pip, 'Add a log'),
        origin='agent', sender_agent_id=pip['agent_id'])


def test_the_requesters_goal_waits_and_is_never_taken_from_another_wait(pair):
    pip, avana = pair
    plan = _goal(pip)
    first, second = pr.new_id(), pr.new_id()
    pr.wait_on(pip, plan['plan_id'], first, avana, deadline_s=3600)
    cont = task_plans.get(plan['plan_id'])['goal']['continuation']
    assert (cont['state'], cont['dependency_key']) == ('waiting', f'peer:{first}')
    assert cont['due_at'] > now_ms() + 3500_000
    with pytest.raises(pr.RequestError, match=f'already waits on peer:{first}'):
        pr.wait_on(pip, plan['plan_id'], second, avana)
    with pytest.raises(pr.RequestError, match='belongs to pip'):
        pr.wait_on(avana, plan['plan_id'], second, pip)
    manual = _goal(pip, alias='manual', enroll=False)
    with pytest.raises(pr.RequestError, match='not enrolled'):
        pr.wait_on(pip, manual['plan_id'], second, avana)


def test_only_the_addressee_can_answer_a_request(pair, tmp_path):
    pip, avana = pair
    request_id = pr.new_id()
    _sent(pip, avana, request_id)
    assert pr.find_request(avana, request_id)['session'] == 'pip'
    with pytest.raises(pr.RequestError, match='was not sent to pip'):
        pr.find_request(pip, request_id)
    with pytest.raises(pr.RequestError, match='not a request id'):
        pr.find_request(avana, 'r123')
    agents.set_archived(pip['agent_id'], True)
    with pytest.raises(pr.RequestError, match='is archived'):
        pr.find_request(avana, request_id)


def test_a_result_wakes_the_waiting_goal_once_and_a_duplicate_changes_nothing(pair):
    pip, avana = pair
    plan = _goal(pip)
    request_id = pr.new_id()
    pr.wait_on(pip, plan['plan_id'], request_id, avana)
    _sent(pip, avana, request_id)
    decision = pr.plan_reply(avana, request_id, 'result', 'Shipped as clarpForm.log()')
    assert decision['action'] == 'dependency'
    pr.record_result(decision['plan'], decision['data'])
    cont = task_plans.get(plan['plan_id'])['goal']['continuation']
    assert cont['state'] == 'ready' and 'clarpForm.log()' in cont['dependency_result']['evidence']
    assert pr.plan_reply(avana, request_id, 'result', 'Shipped as clarpForm.log()')['action'] == 'delivered'


def test_without_a_waiting_goal_replies_are_stable_messages(pair):
    pip, avana = pair
    request_id = pr.new_id()
    _sent(pip, avana, request_id)
    blocked = pr.plan_reply(avana, request_id, 'blocked', 'Waiting for Peter to approve activation')
    again = pr.plan_reply(avana, request_id, 'blocked', 'Waiting for Peter to approve activation')
    result = pr.plan_reply(avana, request_id, 'result', 'Live now')
    assert blocked['action'] == result['action'] == 'message'
    assert blocked['payload']['client_msg_id'] == again['payload']['client_msg_id']
    assert result['payload']['client_msg_id'] == f'peer-res-{request_id}'
    assert (result['payload']['session'], result['payload']['sender']) == ('pip', 'avana')
    assert 'still open' in blocked['payload']['text'] and 'still open' not in result['payload']['text']


def test_a_result_for_a_paused_goal_waits_for_the_resume(pair):
    pip, avana = pair
    plan = _goal(pip)
    request_id = pr.new_id()
    pr.wait_on(pip, plan['plan_id'], request_id, avana)
    _sent(pip, avana, request_id)
    p = task_plans.get(plan['plan_id'])
    p = task_plans._goal_mutate(p['plan_id'], revision=p['revision'], action='pause', data={'reason': 'User paused'})
    decision = pr.plan_reply(avana, request_id, 'result', 'Live now')
    assert decision['action'] == 'dependency'   # never a message around the pause
    pr.record_result(decision['plan'], decision['data'])
    p = task_plans.get(plan['plan_id'])
    assert p['status'] == 'paused' and p['goal']['continuation']['state'] == 'paused'
    p = task_plans._goal_mutate(p['plan_id'], revision=p['revision'], action='resume', data={'reason': 'User resumed'})
    cont = p['goal']['continuation']
    assert cont['state'] == 'ready' and cont['dependency_result']['outcome'] == 'succeeded'


def _waiting(pip, avana):
    plan = _goal(pip)
    request_id = pr.new_id()
    pr.wait_on(pip, plan['plan_id'], request_id, avana)
    _sent(pip, avana, request_id)
    return task_plans.get(plan['plan_id']), request_id


def test_a_user_block_holds_the_result_and_quiets_updates(pair):
    pip, avana = pair
    p, request_id = _waiting(pip, avana)
    task_plans._goal_mutate(p['plan_id'], revision=p['revision'], action='block', data={'reason': 'Needs Peter'})
    with pytest.raises(pr.RequestError, match='blocked by the user'):
        pr.plan_reply(avana, request_id, 'progress', 'Halfway')
    decision = pr.plan_reply(avana, request_id, 'result', 'Live now')
    assert decision['action'] == 'dependency'
    pr.record_result(decision['plan'], decision['data'])
    assert task_plans.get(p['plan_id'])['goal']['continuation']['dependency_result']['outcome'] == 'succeeded'


def test_a_result_after_the_goal_stopped_waiting_still_arrives(pair):
    """The deadline woke Pip, then Peter paused the goal: the goal no longer
    takes the result, and the answer must not be lost."""
    pip, avana = pair
    p, request_id = _waiting(pip, avana)
    from lib import db  # recovery's deadline handling, as task_goal_recovery._claim writes it
    goal = p['goal']
    goal['continuation'].update(state='ready', reason='External dependency deadline passed; inspect its actual outcome')
    db.conn().execute('UPDATE task_plans SET goal_json=? WHERE plan_id=?', (__import__('json').dumps(goal), p['plan_id']))
    p = task_plans.get(p['plan_id'])
    task_plans._goal_mutate(p['plan_id'], revision=p['revision'], action='pause', data={'reason': 'User paused'})
    decision = pr.plan_reply(avana, request_id, 'result', 'Live now')
    assert decision['action'] == 'message' and decision['payload']['client_msg_id'] == f'peer-res-{request_id}'


def test_a_real_answer_after_an_undelivered_record_is_still_delivered(pair):
    pip, avana = pair
    p, request_id = _waiting(pip, avana)
    assert pr.abandon_wait(p['plan_id'], request_id, 'HTTP 409')
    decision = pr.plan_reply(avana, request_id, 'result', 'It did arrive; live now')
    assert decision['action'] == 'message'   # not swallowed as "already recorded"


def test_a_helper_answering_its_parent_is_reported(pair, tmp_path):
    pip, avana = pair
    helper = agents.create_agent(persona='Pip helper', voice_id='', cwd=str(tmp_path), session='pip-helper')
    agents.set_lineage(helper, parent_agent_id=pip['agent_id'], role='helper')
    agents.bind_backend_session(helper, 'pip-helper-native')
    helper_row = agents.get_by_session('pip-helper')
    p, request_id = _waiting(pip, helper_row)
    decision = pr.plan_reply(helper_row, request_id, 'result', 'Done')
    pr.record_result(decision['plan'], decision['data'], replier=helper_row)
    assert agents.get_by_session('pip-helper')['helper_state'] == 'reported'


def test_updates_for_one_request_are_capped(pair):
    pip, avana = pair
    request_id = pr.new_id()
    _sent(pip, avana, request_id)
    for n in range(pr.MAX_UPDATES):
        payload = pr.plan_reply(avana, request_id, 'progress', f'Step {n}')['payload']
        message_store.record_user_message(agent_id=pip['agent_id'], backend_session_id='pip-native',
                                          client_msg_id=payload['client_msg_id'], text=payload['text'],
                                          origin='agent', sender_agent_id=avana['agent_id'])
    with pytest.raises(pr.RequestError, match='updates already went'):
        pr.plan_reply(avana, request_id, 'progress', 'Step again')
    # A retry of the fifth update, whose send result was lost, is that message again.
    retried = pr.plan_reply(avana, request_id, 'progress', f'Step {pr.MAX_UPDATES - 1}')
    assert retried['action'] == 'message' and retried['payload']['client_msg_id'] == payload['client_msg_id']
    assert pr.plan_reply(avana, request_id, 'result', 'Done')['action'] == 'message'


# ---- R136: the requester hears when its request ends unrun, within one tick ---------------------

def _queued(pip, avana, request_id):
    from lib import turn_queue
    queue_id = f'q-{request_id}'
    assert turn_queue.enqueue(queue_id=queue_id, agent_id=avana['agent_id'], session='avana', text='Add a log',
                              trace_id=f't-{request_id}', client_msg_id=pr.request_client_id(request_id),
                              synthesize_audio=False, origin='agent', sender_agent_id=pip['agent_id'])
    from lib import db
    return db.conn().execute('SELECT * FROM queued_turns WHERE queue_id=?', (queue_id,)).fetchone()


def _waiting_on_queue(pip, avana):
    plan = _goal(pip)
    request_id = pr.new_id()
    pr.wait_on(pip, plan['plan_id'], request_id, avana)
    return plan['plan_id'], request_id, _queued(pip, avana, request_id)


def _result(plan_id):
    return (task_plans.get(plan_id)['goal']['continuation'] or {}).get('dependency_result') or {}


def test_a_request_refused_before_it_was_sent_wakes_its_requester_at_the_next_tick(pair):
    from lib import turn_queue
    pip, avana = pair
    plan_id, request_id, row = _waiting_on_queue(pip, avana)
    assert turn_queue.fail_refused_launch(row, failures=8, error='no seat for the model')
    assert pr.settle_failed_waits() == 1
    result = _result(plan_id)
    assert result['outcome'] == 'failed'
    assert pr.NOT_DELIVERED in result['evidence'] and 'no seat for the model' in result['evidence']
    assert pr.settle_failed_waits() == 0                       # once: the goal no longer waits


def test_a_delivered_or_still_queued_request_is_left_alone(pair):
    pip, avana = pair
    queued_plan, _queued_id, _row = _waiting_on_queue(pip, avana)
    delivered = _goal(pip, alias='other')
    request_id = pr.new_id()
    pr.wait_on(pip, delivered['plan_id'], request_id, avana)
    _sent(pip, avana, request_id)                              # delivered at once, no queue row
    assert pr.settle_failed_waits() == 0
    for plan_id in (queued_plan, delivered['plan_id']):
        assert task_plans.get(plan_id)['goal']['continuation']['state'] == 'waiting'


def test_a_request_that_fails_after_the_deadline_keeps_the_deadlines_answer(pair):
    """The deadline woke the requester first: the row failing afterwards wakes nothing again."""
    import json
    from lib import db, turn_queue
    pip, avana = pair
    plan_id, request_id, row = _waiting_on_queue(pip, avana)
    plan = task_plans.get(plan_id)
    goal = plan['goal']
    goal['continuation'].update(state='ready', reason='External dependency deadline passed; inspect its actual outcome')
    db.conn().execute('UPDATE task_plans SET goal_json=? WHERE plan_id=?', (json.dumps(goal), plan_id))
    assert turn_queue.fail_refused_launch(row, failures=8, error='no seat')
    assert pr.settle_failed_waits() == 0
    assert task_plans.get(plan_id)['goal']['continuation']['state'] == 'ready'
    assert not _result(plan_id)


def test_the_agent_schedulers_tick_settles_a_failed_peer_wait(pair):
    """The tick that does it: the agent scheduler's, every pass, beside the goal wakes."""
    from lib import scheduler, turn_queue
    pip, avana = pair
    plan_id, request_id, row = _waiting_on_queue(pip, avana)
    assert turn_queue.fail_refused_launch(row, failures=8, error='no seat')
    runner = scheduler.AgentScheduleRunner(dispatch_turn=lambda *a, **k: None)
    runner.tick()
    assert _result(plan_id)['outcome'] == 'failed'


def test_a_request_whose_turn_ran_and_failed_is_not_settled_as_undelivered(pair):
    """A queue row ends 'failed' also when the message ran and the recipient's turn failed
    (settle_receipt maps a failed outcome to 'failed'): the request was delivered, so the wait
    stays open for its answer or its deadline (reviewer 3 on PR 535)."""
    from lib import db
    pip, avana = pair
    plan_id, request_id, row = _waiting_on_queue(pip, avana)
    db.conn().execute("UPDATE queued_turns SET status='failed', started_at=? WHERE queue_id=?",
                      (now_ms(), row['queue_id']))
    assert pr.unrun_reason(request_id) == ''
    assert pr.settle_failed_waits() == 0
    assert task_plans.get(plan_id)['goal']['continuation']['state'] == 'waiting'


def test_a_tick_racing_the_answer_gives_one_result_and_one_wake(pair, monkeypatch):
    """The tick reads the waiting goal, then the answer (or the deadline) lands before its write:
    the tick writes through the same conditional mutation (the goal's revision it read, and a
    dependency that still names this key), so its write is refused, it reads again, finds the
    goal no longer waiting and settles nothing. One result: the answer's."""
    from lib import turn_queue
    pip, avana = pair
    plan_id, request_id, row = _waiting_on_queue(pip, avana)
    assert turn_queue.fail_refused_launch(row, failures=8, error='no seat')
    real = task_plans._goal_mutate
    raced = []

    def mutate(target, *, revision, action, data=None):
        if action == 'dependency' and not raced:
            raced.append(1)                                    # the answer lands first
            current = task_plans.get(target)
            real(target, revision=current['revision'], action='dependency',
                 data={'key': pr.dependency_key(request_id), 'outcome': 'succeeded', 'evidence': 'answered'})
        return real(target, revision=revision, action=action, data=data)
    monkeypatch.setattr(task_plans, '_goal_mutate', mutate)
    assert pr.settle_failed_waits() == 0
    result = _result(plan_id)
    assert result['outcome'] == 'succeeded' and result['evidence'] == 'answered'
