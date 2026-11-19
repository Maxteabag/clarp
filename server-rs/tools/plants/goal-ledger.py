#!/usr/bin/env python3
"""Wrong rules for an owning agent's ledger."""
import runner
K='clarp-kernel/src/goals/ledger.rs'
A='clarp-actor/src/goal_ledger.rs'
RUNS={'rules':['test','--test','goal_ledger'],'write':['test','--test','host','a_goal_change_keeps_its_event'],'entry':['test','--test','actor','incoming_goal_events_keep_disjoint_ids']}
PLANTS=[]
def plant(file,run,reason,old,new): PLANTS.append((file,run,reason,old,new))
for why,a,b in [
    ('an older cursor can go backwards','previous.max(requested.min(owner_high))','requested.min(owner_high)'),
    ('another agent can advance the cursor','requested.min(owner_high)','requested.max(owner_high)'),
    ('verified peer acts as the owner','if id == plan.agent_id','if id != plan.agent_id'),
    ('unverified owner gains proof','plan.agent_id.as_str(), "", false','plan.agent_id.as_str(), "", true'),
    ('prior field comes from the new goal','prior.insert(key.into(), a.clone())','prior.insert(key.into(), b.clone())'),
    ('new field comes from the prior goal','after.insert(key.into(), b.clone())','after.insert(key.into(), a.clone())'),
    ('the changed criteria disappear','"criteria",','"wrong_criteria",'),
    ('a step appears as a goal','format!("step:{s}")','format!("goal:{s}")'),
    ('source reference cap loses one','.take(20)','.take(19)'),
    ('source reference cap admits another','.take(20)','.take(21)'),
    ('reference text limit drops one character','first(s, 200)','first(s, 199)'),
    ('references keep blanks','!s.trim().is_empty()','true'),
    ('status records the new state as the old one','json!(status)','json!(to)'),
    ('user becomes owner','("user", "", "", false)','("owner", "", "", false)'),
    ('keeper loses its role','("system", id, "goal-keeper", false)','("system", id, "", false)'),
    ('automatic dependency result is claimed','kind == "unattributed"','kind == "user"'),
    ('a turn claim can acquire owner attribution','"wake_claim"','"wrong_claim"'),
    ('a blocked goal remains active','Some("blocked")','Some("active")'),
    ('a checkpoint is replayed','if index < end','if index + 1 < end'),
]:
    plant(K,'rules',why,a,b)
for why,a,b in [
    ('owner sequence starts at zero','unwrap_or(1)','unwrap_or(0)'),
    ('owner-local event is numbered ahead','event.event_id = next;','event.event_id = next+1;'),
    ('an imported event gets a new ID','previous = event.event_id;','event.event_id=next; previous=event.event_id;'),
    ('a collision overwrites a prior citation','log.find(&locator(event.event_id))?.is_some()','false'),
    ('counter ignores the imported maximum','next = next.max(','next = next.min('),
    ('a locator points at a different ordinal','&(plan, ordinal)','&(plan, ordinal+1)'),
]:
    plant(A,'entry',why,a,b)
plant(K,'entry','entry marker loses its origin','"from":source','"from":""')
if __name__=='__main__': raise SystemExit(runner.main(__file__,RUNS,PLANTS))
