"""Goal continuation inside the existing scheduler, using canonical dispatch.

Only explicitly enrolled plans participate. Claims survive process loss; a retry
of uncertain admission reuses the same client receipt, never a fresh message ID.
"""
from __future__ import annotations
import json
import secrets
from . import agents, db, task_goal_state

PREFIX='task-goal-'


def boundary(plan, goal, *, check_live=True):
    from . import artifacts, agent_goals, turn_queue, reconcile, turn_dispatch
    agent=agents.get_by_agent_id(plan['agent_id'])
    if not agent or agent.get('deleted_at') or agent.get('archived_at'):
        return 'owner_unavailable','Owner is unavailable'
    if agent['session']!=plan['session'] or goal['owner_agent_id']!=agent['agent_id']:
        return 'owner_changed','Owner binding changed; explicit re-enrollment required'
    native=agents.live_backend_session(agent['agent_id'])
    if not native or native!=goal['native_session_id']:
        return 'owner_changed','Native conversation changed; explicit re-enrollment required'
    if turn_queue.is_paused(agent['agent_id']): return 'paused','Stopped by user; queue is paused'
    if artifacts.has_pending_decision(agent['agent_id']): return 'approval','Waiting for an answer or approval'
    native_goal=agent_goals.get(agent['agent_id'])
    if native_goal and native_goal.get('native') and native_goal['status']!='complete':
        # Codex owns native continuation. Never run a competing Host goal loop.
        return 'native_owned', 'Native goal controls continuation: '+native_goal['status']
    if check_live:
        live=turn_dispatch.live_work(agent['agent_id'],session=agent['session'])
        if (reconcile.has_live_work(agent['agent_id'],agent.get('backend')) or
                live.compacting or live.terminal or live.queued):
            return 'working','Owner has live or queued work'
    return None


def validate_dispatch(agent, request_id):
    """Fence stale/paused/wrong-owner wakes at both admission and actual spawn."""
    if not request_id.startswith(PREFIX): return
    row=db.conn().execute(
        "SELECT * FROM task_plans WHERE recovery_enabled=1 AND status='active' "
        "AND json_extract(goal_json,'$.continuation.request_id')=?",(request_id,)).fetchone()
    if not row or row['agent_id']!=agent.get('agent_id'):
        raise ValueError('goal wake was superseded or owner changed')
    goal=json.loads(row['goal_json']);gate=boundary(row,goal,check_live=False)
    if gate: raise ValueError(gate[1])


def _prompt(plan,goal):
    return ('Continue your durable outcome commitment. Reassess current conditions and fresh results; '
            'choose your own next actions, collaborators and wake timing within the original authority. '
            'Earlier next-work text is provisional context, not a fixed script. Answering a side question '
            'or ending a turn does not complete this goal. Record a checkpoint with evidence and a '
            'continuation or explicit blocker before yielding. Never self-approve pending permissions.\n'
            +json.dumps(dict(plan_id=plan['plan_id'],revision=plan['revision'],outcome=goal['outcome'],
                            limits=goal['limits'],criteria=goal['criteria'],checkpoint=goal.get('checkpoint'),
                            continuation=goal['continuation']),ensure_ascii=False))


def tick(dispatch, *, now=None):
    now=db.now_ms() if now is None else now;count=0
    ids=[r[0] for r in db.conn().execute(
        "SELECT plan_id FROM task_plans WHERE recovery_enabled=1 AND status='active' ORDER BY updated_at LIMIT 100")]
    for plan_id in ids:
        claim=_claim(plan_id,now)
        if not claim: continue
        plan,goal=claim;request=goal['continuation']['request_id']
        try:
            result=dispatch(plan['session'],_prompt(plan,goal),request)
            if result is None: raise RuntimeError('dispatcher returned no admission receipt')
            queued=bool(result.get('queued')) if isinstance(result,dict) else bool(result.queued)
            _result(plan_id,request,now,'queued' if queued else 'admitted','Canonical dispatch accepted the wake')
            count+=1
        except Exception as exc:
            # Keep request ID: response loss may have occurred after admission.
            _result(plan_id,request,now,'retry',str(exc)[:500])
    return count


def _claim(plan_id,now):
    con=db.conn();con.execute('BEGIN IMMEDIATE')
    try:
        plan=con.execute('SELECT * FROM task_plans WHERE plan_id=?',(plan_id,)).fetchone()
        if not plan or plan['status']!='active' or not plan['recovery_enabled']: return None
        goal=json.loads(plan['goal_json']);state=goal['continuation']
        if state.get('state') in {'blocked','attention','not_enrolled'}: return None
        # A still-live lease belongs to another scheduler, including after a restart.
        if (state.get('lease_until') or 0)>now: return None
        gate=boundary(plan,goal)
        if gate:
            execution,reason=gate
            if state.get('observed_state')!=execution:
                state.update(observed_state=execution,observed_at=now,observed_reason=reason)
                task_goal_state._save(con,plan_id,goal)
            return None
        state.update(observed_state='idle',observed_at=now,observed_reason='Owner is idle')
        if (state.get('due_at') or 0)>now:
            task_goal_state._save(con,plan_id,goal);return None
        if state.get('state')=='waiting':
            state.update(state='ready',reason='External dependency deadline passed; inspect its actual outcome')
        if state.get('attempts',0)>=goal['recovery_limit']:
            state.update(state='attention',reason='Recovery attempt limit reached; checkpoint or resume to continue')
            task_goal_state._save(con,plan_id,goal);return None
        # Successful admission followed by an idle owner is an unfinished turn,
        # not a completed goal. A new generation gets one new receipt.
        if state.get('state') in {'admitted','queued'}:
            state.update(generation=state['generation']+1,request_id='')
        if not state.get('request_id'): state['request_id']=PREFIX+secrets.token_hex(16)
        state.update(state='dispatching',lease_until=now+60000,attempts=state.get('attempts',0)+1)
        goal['history'].append(dict(at=now,kind='wake_claim',revision=plan['revision'],
                                    detail={'request_id':state['request_id'],'generation':state['generation']}))
        task_goal_state._save(con,plan_id,goal)
        return dict(plan),goal
    finally:
        con.execute('COMMIT')


def _result(plan_id,request,now,state,reason):
    con=db.conn();con.execute('BEGIN IMMEDIATE')
    try:
        plan=con.execute('SELECT * FROM task_plans WHERE plan_id=?',(plan_id,)).fetchone()
        if not plan: return
        goal=json.loads(plan['goal_json']);wake=goal['continuation']
        if wake.get('request_id')!=request: return
        backoff=min(3600000,120000*2**min(wake.get('attempts',1)-1,5))
        wake.update(state=state,reason=reason,lease_until=None,due_at=now+backoff,last_dispatch_at=now)
        goal['history'].append(dict(at=now,kind='dispatch_'+state,revision=plan['revision'],
                                    detail={'request_id':request,'reason':reason}))
        task_goal_state._save(con,plan_id,goal)
    finally: con.execute('COMMIT')
