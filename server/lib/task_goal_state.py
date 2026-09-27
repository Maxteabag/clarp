"""Revisioned goal state on existing task plans; no second planner or scheduler."""
from __future__ import annotations

import json
import secrets
from . import agents, db


def check_revision(plan, revision):
    if revision is None and json.loads(plan['goal_json'] or '{}'):
        raise ValueError('revision required for durable goal')
    if revision is not None and revision != plan['revision']:
        raise ValueError('plan changed; reload before editing')


def _save(con, plan_id, goal):
    con.execute('UPDATE task_plans SET goal_json=? WHERE plan_id=?',
                (json.dumps(goal, ensure_ascii=False), plan_id))


def initialize(con, plan_id, agent, title, raw, now):
    outcome = str(raw.get('outcome') or title).strip()
    criteria = raw.get('criteria')
    if not isinstance(criteria, list) or not criteria or len(criteria) > 100:
        raise ValueError('goal requires 1–100 completion criteria')
    if not isinstance(raw.get('limits'), str) or not raw['limits'].strip():
        raise ValueError('explicit goal limits required (use "No additional limits" if applicable)')
    texts = [str(c).strip() for c in criteria]
    if any(not c for c in texts):
        raise ValueError('completion criteria must not be empty')
    native = agents.live_backend_session(agent['agent_id']) or ''
    enroll = bool(raw.get('enroll', False))
    if enroll and not native:
        raise ValueError('recovery enrollment requires a bound native conversation')
    goal = dict(outcome=outcome, limits=raw['limits'], owner_agent_id=agent['agent_id'],
                native_session_id=native,
                criteria=[dict(id=f'criterion-{i+1}', text=c, evidence='') for i,c in enumerate(texts)],
                history=[dict(at=now, kind='created', revision=0, detail={'outcome':outcome})],
                checkpoint=None, continuation=dict(generation=0, state='ready' if enroll else 'not_enrolled',
                reason='Awaiting owner checkpoint' if enroll else 'Recovery not enrolled',
                due_at=now+120000 if enroll else None, attempts=0, lease_until=None, request_id=''),
                recovery_limit=max(1,min(20,int(raw.get('recovery_limit',5)))))
    _save(con,plan_id,goal)
    con.execute('UPDATE task_plans SET recovery_enabled=? WHERE plan_id=?',(int(enroll),plan_id))


def record(con, plan, kind, detail, now):
    goal=json.loads(plan['goal_json'] or '{}')
    if not goal:
        # Legacy history begins at the first observed mutation, never retroactively.
        return
    goal['history'].append(dict(at=now,kind=kind,revision=plan['revision']+1,detail=detail))
    _save(con,plan['plan_id'],goal)


def require_completion(con, plan):
    goal=json.loads(plan['goal_json'] or '{}')
    rows=con.execute('SELECT status,required FROM task_items WHERE plan_id=?',(plan['plan_id'],)).fetchall()
    if any(r['required'] and r['status'] not in {'completed', 'removed'} for r in rows):
        raise ValueError('required work remains unresolved')
    if goal and any(not c['evidence'].strip() for c in goal['criteria']):
        raise ValueError('completion requires evidence for every acceptance criterion')
    if not goal and any(r['status'] != 'completed' for r in rows):
        raise ValueError('unfinished legacy work cannot be completed')


def mutate(plan_id, *, revision, action, data=None):
    """Single transaction for checkpoint/evidence/next work and wake registration."""
    from . import task_plans, artifacts
    data=data or {};con=db.conn();now=db.now_ms()
    con.execute('BEGIN IMMEDIATE')
    try:
        plan=con.execute('SELECT * FROM task_plans WHERE plan_id=?',(plan_id,)).fetchone()
        if not plan: raise ValueError('task plan not found')
        check_revision(plan,revision)
        goal=json.loads(plan['goal_json'] or '{}')
        if action=='enroll' and not goal:
            if plan['status']!='active': raise ValueError('only active legacy work can enroll')
            agent=agents.get_by_agent_id(plan['agent_id'])
            initialize(con,plan_id,agent,plan['title'],data,now)
            goal=json.loads(con.execute('SELECT goal_json FROM task_plans WHERE plan_id=?',(plan_id,)).fetchone()[0])
        if not goal: raise ValueError('legacy plan must be explicitly enrolled as a goal')
        if plan['status'] in {'completed','cancelled','superseded'}:
            raise ValueError('goal is closed')
        reason=str(data.get('reason') or '').strip()
        state=goal['continuation']
        if action=='enroll':
            pass
        elif action in {'pause','cancel','supersede','block','resume'}:
            if not reason: raise ValueError('goal control requires a reason')
            if action=='supersede':
                other=con.execute('SELECT agent_id FROM task_plans WHERE plan_id=?',(data.get('replacement_id',''),)).fetchone()
                if not other or other['agent_id']!=plan['agent_id'] or data['replacement_id']==plan_id:
                    raise ValueError('replacement must be another plan owned by the same agent')
                goal['superseded_by']=data['replacement_id']
            status={'pause':'paused','cancel':'cancelled','supersede':'superseded','block':'blocked','resume':'active'}[action]
            state.update(generation=state['generation']+1,state='ready' if action=='resume' else status,
                         reason=reason,due_at=now+15000 if action=='resume' else None,lease_until=None,request_id='',attempts=0)
            con.execute('UPDATE task_plans SET status=?,completed_at=? WHERE plan_id=?',
                        (status,now if action in {'cancel','supersede'} else None,plan_id))
            if action!='resume': task_plans._close_running_items(con,plan_id,'blocked' if action=='block' else 'cancelled',now)
        elif action=='checkpoint':
            if plan['status']!='active': raise ValueError('resume goal before checkpointing')
            progress=str(data.get('progress') or '').strip();next_work=str(data.get('next_work') or '').strip()
            if not progress or not next_work: raise ValueError('checkpoint requires progress and next work')
            for cid,evidence in (data.get('evidence') or {}).items():
                criterion=next((c for c in goal['criteria'] if c['id']==cid),None)
                if criterion is None or not str(evidence).strip(): raise ValueError('unknown criterion or empty evidence')
                criterion['evidence']=str(evidence)
            goal['checkpoint']=dict(at=now,progress=progress,next_work=next_work,evidence=data.get('evidence') or {})
            wake=data.get('continuation') or {};kind=wake.get('kind','timer')
            if kind not in {'timer','dependency','blocked'}: raise ValueError('invalid continuation kind')
            due=int(wake.get('due_at') or now+120000)
            if kind=='dependency' and (not wake.get('key') or not wake.get('reason') or due<=now):
                raise ValueError('dependency requires key, wait reason and future timeout')
            if kind=='blocked' and not wake.get('reason'): raise ValueError('blocker requires a reason')
            state.update(generation=state['generation']+1,state='waiting' if kind=='dependency' else ('blocked' if kind=='blocked' else 'ready'),
                         reason=str(wake.get('reason') or 'Scheduled continuation'),due_at=None if kind=='blocked' else max(now,due),
                         dependency_key=str(wake.get('key') or ''),dependency_result=None,request_id='',lease_until=None,attempts=0)
        elif action=='dependency':
            if state.get('state')!='waiting' or data.get('key')!=state.get('dependency_key'):
                raise ValueError('dependency is stale or does not belong to this goal')
            if data.get('outcome') not in {'succeeded','failed'} or not data.get('evidence'):
                raise ValueError('external result requires outcome and evidence')
            state.update(state='ready',due_at=now,reason='External work '+data['outcome'],dependency_result=data,request_id='')
        elif action=='replan':
            if not reason: raise ValueError('replanning requires a discovery or reason')
            proposed=data.get('steps')
            if not isinstance(proposed,list) or not proposed or len(proposed)>task_plans.MAX_PLAN_ITEMS:
                raise ValueError('replan requires meaningful steps')
            old=[dict(r) for r in con.execute('SELECT * FROM task_items WHERE plan_id=?',(plan_id,))]
            goal['history'].append(dict(at=now,kind='prior_steps',revision=revision+1,detail={'items':old}))
            keys=set()
            for position,raw in enumerate(proposed):
                key=task_plans.item_key(plan_id,str(raw.get('id') or ''))
                if key in keys: raise ValueError('duplicate step id')
                keys.add(key)
                existing=next((r for r in old if r['item_id']==key),None)
                if existing:
                    if not str(raw.get('title') or '').strip(): raise ValueError('step title required')
                    con.execute('UPDATE task_items SET title=?,position=?,parent_id=NULL WHERE item_id=?',
                                (raw['title'],position,key))
                else: task_plans._insert_item(con,plan_id,key,None,position,raw,now)
            for item in old:
                if item['item_id'] not in keys and item['status']!='completed':
                    elapsed=item['active_ms']+ (max(0,now-item['started_at']) if item['started_at'] else 0)
                    con.execute("UPDATE task_items SET status='removed',detail=?,started_at=NULL,active_ms=?,completed_at=? WHERE item_id=?",
                                (reason,elapsed,now,item['item_id']))
            state.update(generation=state['generation']+1,request_id='',lease_until=None)
            if state['state'] not in {'blocked','waiting'}:
                state.update(state='ready',due_at=now+120000,reason='Plan revised; reassess current results')
        elif action=='add_step':
            if not reason: raise ValueError('plan adaptation requires a reason')
            key=task_plans.item_key(plan_id,str(data.get('id') or secrets.token_hex(4)))
            position=con.execute('SELECT COUNT(*) FROM task_items WHERE plan_id=?',(plan_id,)).fetchone()[0]
            if position>=task_plans.MAX_PLAN_ITEMS: raise ValueError('too many steps')
            task_plans._insert_item(con,plan_id,key,None,position,data,now)
        else: raise ValueError('unknown goal action')
        goal['history'].append(dict(at=now,kind=action,revision=revision+1,detail=data))
        _save(con,plan_id,goal)
        con.execute('UPDATE task_plans SET revision=revision+1,updated_at=? WHERE plan_id=?',(now,plan_id))
        artifacts.sync_plan(plan_id)
        con.execute('COMMIT')
    except BaseException:
        con.execute('ROLLBACK');raise
    return task_plans.get(plan_id)
