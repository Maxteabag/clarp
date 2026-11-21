"""The goal plans' versions and the scheduler's summaries of them (lib.task_plans.plan_index).

Three triggers stamp every insert, update and delete of task_plans, whatever code wrote, so a
summary is kept exactly as long as its plan stands. These check that the summaries are what the
plans say, that nothing is read again while no plan was written, that one written plan is read
again alone, and that a transaction never leaves a summary behind.
"""
import json

import pytest

from lib import agents, db, task_plans


@pytest.fixture
def owner(tmp_path):
    aid = agents.create_agent(persona='Ivy', voice_id='', cwd=str(tmp_path), session='ivy')
    agents.bind_backend_session(aid, 'ivy-native')
    return agents.get_by_session('ivy')


def _goal(alias, **goal):
    return task_plans.create(session='ivy', plan_id=alias, title=alias, items=[{'id': 'g', 'title': 'G'}],
                             goal={'outcome': 'Done', 'criteria': ['Done'], 'limits': 'none',
                                   'enroll': True, **goal})


def _reads():
    statements = []
    db.conn().set_trace_callback(statements.append)
    return statements


def _stop():
    db.conn().set_trace_callback(None)


def _set_continuation(plan_id, **values):
    """A write to a plan's goal JSON, as task_plans.py writes it without touching
    updated_at or revision: the triggers see it all the same."""
    row = db.conn().execute("SELECT goal_json FROM task_plans WHERE plan_id=?", (plan_id,)).fetchone()
    goal = json.loads(row[0])
    goal['continuation'].update(values)
    db.conn().execute("UPDATE task_plans SET goal_json=? WHERE plan_id=?", (json.dumps(goal), plan_id))
    db.conn().commit()


def test_the_summaries_say_what_the_plans_say(owner):
    first, second = _goal('first'), _goal('second')
    _set_continuation(second['plan_id'], state='waiting', dependency_key='peer:r0123456789ab',
                      lease_until=10 ** 15)
    index = task_plans.plan_index()
    assert set(index) >= {first['plan_id'], second['plan_id']}
    waiting = index[second['plan_id']]
    assert waiting['status'] == 'active' and waiting['recovery_enabled'] == 1
    assert waiting['state'] == 'waiting' and waiting['lease_until'] == 10 ** 15
    assert waiting['peer_wait'] == 'peer:r0123456789ab'
    assert index[first['plan_id']]['peer_wait'] is None
    # Waiting on something else, or not waiting: no peer wait.
    _set_continuation(second['plan_id'], state='ready')
    assert task_plans.plan_index()[second['plan_id']]['peer_wait'] is None


def test_nothing_is_read_again_while_no_plan_was_written_and_one_written_plan_alone(owner):
    plans = [_goal(f'p{n}')['plan_id'] for n in range(4)]
    task_plans.plan_index()
    seen = _reads()
    try:
        task_plans.plan_index()
        quiet = list(seen)
        _set_continuation(plans[2], state='blocked')
        seen.clear()
        index = task_plans.plan_index()
    finally:
        _stop()
    assert quiet == ["SELECT generation FROM task_plans_clock WHERE singleton = 0"]
    plan_reads = [s for s in seen if 'FROM task_plans WHERE plan_id' in s]
    assert len(plan_reads) == 1 and plans[2] in plan_reads[0]
    assert index[plans[2]]['state'] == 'blocked'


def test_an_inserted_and_a_deleted_plan_are_seen(owner):
    kept = _goal('kept')['plan_id']
    task_plans.plan_index()
    added = _goal('added')['plan_id']
    assert added in task_plans.plan_index()
    db.conn().execute("DELETE FROM task_plans WHERE plan_id=?", (added,))
    db.conn().commit()
    index = task_plans.plan_index()
    assert added not in index and kept in index
    assert db.conn().execute("SELECT count(*) FROM task_plan_versions WHERE plan_id=?",
                             (added,)).fetchone()[0] == 0


def test_a_rolled_back_write_leaves_no_summary_behind(owner):
    plan = _goal('rolled')['plan_id']
    task_plans.plan_index()
    c = db.conn()
    c.execute("BEGIN IMMEDIATE")
    row = c.execute("SELECT goal_json FROM task_plans WHERE plan_id=?", (plan,)).fetchone()
    goal = json.loads(row[0])
    goal['continuation']['state'] = 'blocked'
    c.execute("UPDATE task_plans SET goal_json=? WHERE plan_id=?", (json.dumps(goal), plan))
    assert task_plans.plan_index()[plan]['state'] == 'blocked'       # read whole, not kept
    c.execute("ROLLBACK")
    assert task_plans.plan_index()[plan]['state'] != 'blocked'
