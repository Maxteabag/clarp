#!/usr/bin/env python3
"""Owner subgoal commands and committed events, read from the Python Host."""
import json
import runpy
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scratch as tool_scratch

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'server-rs/crates/clarp-kernel/tests/fixtures/goal-subgoals.json'


def reference(root):
    harness = runpy.run_path(str(ROOT / 'tests/replay/host.py'))
    harness['isolate'](root)
    harness['loopback_only']()
    sys.path.insert(0, str(ROOT / 'server'))
    from lib import agents, db, goal_ledger, task_plans

    assert db.DB_PATH.resolve().is_relative_to(root)
    with patch.object(agents, '_new_agent_id', return_value='a'):
        agents.create_agent(persona='Owner', session='owner', cwd=str(root), voice_id='')
    tokens = iter(range(100))
    with patch('secrets.token_hex', side_effect=lambda size: f'{next(tokens):0{2*size}x}'), patch.object(db, 'now_ms', return_value=100):
        plan = task_plans.create(session='owner', title='Fixture', items=[],
                                 goal={'outcome':'Deliver', 'criteria':['Done'], 'limits':'Local'})
        plain = task_plans.create(session='owner', plan_id='plain', title='Plain', items=[])
    pid = plan['plan_id']
    cases = []

    def call(action, data, actor=None, target=pid, at=200):
        before = goal_ledger.events(target)['next_after']
        row = dict(plan=target, action=action, data=data, actor=actor, at=at)
        with patch.object(db, 'now_ms', return_value=at):
            try:
                answer = goal_ledger.owner_subgoal(target, action, data, actor=actor)
            except (ValueError, AttributeError, OverflowError) as error:
                row['error'] = str(error)
                row['exit'] = 4 if isinstance(error, goal_ledger.DelegationDenied) else 1
            else:
                row['answer'] = answer
        page = goal_ledger.events(target, after=before)
        row['events'] = page['events']
        if 'error' in row:
            assert not row['events']
        cases.append(row)

    add = {'subgoal_id':'z','title':' Write ','criteria':[' first ', 3], 'owner':' Owner '}
    call('add', add, target='missing')
    call('add', add, target=plain['plan_id'])
    call('retire', {})
    for data in [None, True, 3, 1.5, 'title', []]:
        call('add', data)
    call('add', dict(add, subgoal_id='fallback', title={'key': True}, criteria=0, source_refs=''))
    for data in ({}, {'subgoal_id':'   '}, {'subgoal_id':'s'*81}, {'subgoal_id':'z'},
                 dict(add,title='t'*301), dict(add,owner='o'*121), dict(add,intent='i'*4001),
                 dict(add,criteria='x'), dict(add,criteria=['x']*21), dict(add,criteria=[' ']),
                 dict(add,criteria=['c'*501]), dict(add,reason='r'*4001)):
        call('add',data)
    for refs in (None, '', [], [' ref ', 'x'*301], ['x']*20, ['x']*21, [' '], [5], 'text'):
        call('add',dict(add,subgoal_id=f'refs-{len(cases)}',source_refs=refs))
    call('add',add)
    call('add',add)
    call('add',dict(add,subgoal_id='a',title='A'),{'kind':'owner','agent_id':'b','verified':True})
    call('add',dict(add,subgoal_id='later'),{'kind':'owner'},at=201)
    call('update',{'subgoal_id':'absent'})
    def update(fields, **extra):
        current = next(s for s in goal_ledger.subgoals(pid) if s['subgoal_id']=='z')
        call('update',dict({'subgoal_id':'z','expected_revision':current['revision'],'fields':fields},**extra))
    for expected in (None,0,2,'1',1.0):
        update({'title':'bad'},expected_revision=expected)
    update({},expected_revision=True)
    update({'title':'same revision via true'},expected_revision=True)
    for fields in [None, [], False, '', 1, True, 1.5, 'word', ['one']]:
        update(fields)
    for name,limit in [('title',300),('owner',4000),('intent',4000),('next_dependency',4000),('current_action',4000)]:
        update({name:'é'*(limit+1)})
    update({'evidence':[{'text':'é'*4001}]})
    update({'evidence':[{'text':'ok','source_refs':['x']*21}]})
    update({'criteria':['x']*20})
    update({'last_observed_at':-2})
    update({'status':'retired'},reason='   ')
    for fields in ({'z':1,'a':2},{'status':'other'},{'status':'retired'},
                   {'criteria':'bad'},{'criteria':['x']*21},{'criteria':['c'*501]},
                   {'evidence':None},{'evidence':['item']},{'evidence':[{}]},
                   {'last_observed_at':'now'},{'current_action':'c'*4001}):
        update(fields)
    update({'title':'Changed'},source_refs=[' '])
    update({'status':'retired'},reason='r'*4001)
    update({'title':'Changed','z':'bad','a':'bad'},source_refs=[' '])
    update({'title':'Clean','intent':'Intent','owner':'o'*121,'criteria':['yes'],
            'current_action':' Doing ','next_dependency':17,'last_observed_at':111})
    for basis in ('observed','claimed','inferred','other'):
        update({'evidence':[{'text':' Proof ','basis':basis,'source_refs':[' example ']}]})
    for status in goal_ledger.SUBGOAL_STATUSES:
        update({'status':status},reason='reason')
    update({'title':'','last_observed_at':True,'status':'active'},reason='reopen')
    call('add',dict(add,subgoal_id='s'*80,title='é'*300,owner='o'*120,intent='i'*4000,
                    criteria=['c'*500]+['x']*19,reason='r'*4000))
    return dict(plan=pid,plain=plain['plan_id'],first_event=plan['ledger']['event_count'],
                cases=cases,written_by=f'goal-subgoals-vectors.py with Python {sys.version_info.major}.{sys.version_info.minor}')


if __name__ == '__main__':
    with tool_scratch.directory() as place:
        OUT.write_text(json.dumps(reference(Path(place)),ensure_ascii=True,separators=(',',':'))+'\n')
