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
    ('a step appears as a goal','"goal".into(), |s| format!("step:{s}")','"goal".into(), |s| format!("goal:{s}")'),
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
RUNS['page']=['test','-p','clarp-actor','--lib','a_page_seeks_only_in_its_plan']
RUNS['route']=['test','--test','host','ledger_pages_and_the_goal_tool_read_the_same_attributed_events_after_a_restart']
for why,old,new in [
    ('cursor includes the event at after','event_id <= after','event_id < after'),
    ('an empty page returns a fabricated row count','remaining > size','remaining >= size'),
    ('page limit reduced','limit.clamp(1, 500)','limit.clamp(1, 1)'),
    ('skip begins after the next ordinal','low + offset','low + offset + 1'),
    ('a page looks at another plan tail','get(log, &tail(plan))?.unwrap_or(0);\n    let mut low','get(log, &tail("wrong"))?.unwrap_or(0);\n    let mut low'),
]: plant(A,'page',why,old,new)
for why,old,new in [
    ('an empty page loses the supplied cursor','map_or(after, |e| e.event_id)','map_or(0, |e| e.event_id)'),
    ('no actor session enrichment','row["actor_session"] = json!(name)','row["actor_session"] = json!("")'),
    ('default page skips first event','integer("after", 0)','integer("after", 1)'),
    ('first limit is refused','limit.clamp(1, 500)','limit.clamp(2, 500)'),
]: plant('clarp-host/src/plans.rs','route',why,old,new)
for why,old,new in [
    ('a reason disappears','detail["reason"].as_str().unwrap_or(""), 4000','detail["other"].as_str().unwrap_or(""), 4000'),
    ('verified flag lost','json!(verified)','json!(false)'),
    ('goal changes lose delegation identity','if system { "" } else { delegation }','if system { delegation } else { "" }'),
    ('the timestamp of a change is its revision','entry["at"].as_i64().unwrap_or(plan.updated_at)','entry["revision"].as_i64().unwrap_or(plan.updated_at)'),
    ('a field removed from the goal stays in after','new["goal"] = json!(after)','new["goal"] = json!(before)'),
    ('a resume leaves the goal paused','"resume" => Some("active")','"resume" => Some("paused")'),
    ('wake failure is attributed to the owner','| "wake_failed"','| "other_wake"'),
    ('a dispatch retry acquires owner attribution','| "dispatch_retry"','| "other_retry"'),
    ('an ended execution is attributed to the owner','| "execution_observed"','| "other_execution"'),
    ('unadmitted wake acquires owner attribution','| "wake_undelivered"','| "other_undelivered"'),
    ('a queued dispatch acquires owner attribution','| "dispatch_queued"','| "other_queued"'),
    ('an admitted dispatch acquires owner attribution','| "dispatch_admitted"','| "other_admitted"'),
]:
    plant(K,'rules',why,old,new)
for why,old,new,run in [
    ('page cap admits the 501st record','limit.clamp(1, 500)','limit.clamp(1, 501)','page'),
    ('page cap loses the 500th record','limit.clamp(1, 500)','limit.clamp(1, 499)','page'),
    ('the applied cursor skips the most recent event','next - 1','next - 2','write'),
    ('a receipt is counted as a new event','next - 1','next','write'),
    ('applied cursor ignores events in its own append','Some(i) => serde_json::from_slice::<i64>(&i.body).map_err(|e| invalid(&e.to_string()))?,','Some(_) => next_id(log)?,','entry'),
    ('a deleted owner loses the cancellation reason','e.reason = "Owner agent deleted".into()','e.reason.clear()','route'),
    ('a deleted owner claims its goal succeeded','e.new = serde_json::json!({"status":"cancelled"})','e.new = serde_json::json!({"status":"completed"})','route'),
    ('a closed goal is cancelled as well','if plan.status == "active"','if plan.status != "active"','route'),
]:
    plant(A,run,why,old,new)
# Migration, turn identities at the boundaries, and deletion after the tombstone commits.
RUNS['keeper']=['test','--test','host','the_keeper_follows_the_next_event_after_a_trimmed_goal']
RUNS['doors']=['test','--test','host','the_goal_ledger_attributes_each_host_door']
RUNS['retire']=['test','-p','clarp-host','--lib','a_durable_removal_reports_success']
H='clarp-host/src/'
for file,why,old,new in [
    ('clarp-actor/src/lib.rs','upgrading keeps the old ordinal cursor','record.import_keeper_cursor(plan, None, baseline);','let _ = baseline;'),
    ('clarp-kernel/src/goals.rs','the last historic entry is news to the keeper','None => baseline,','None => baseline - 1,'),
    ('clarp-kernel/src/goals.rs','a valid moved cursor skips forward to the tail','.clamp(0, events.last().map_or(0, |event| event.event_id))','.max(events.last().map_or(0, |event| event.event_id))'),
    ('clarp-kernel/src/goals.rs','a moved cursor can exceed its tail','.clamp(0, events.last().map_or(0, |event| event.event_id))','.max(0)'),
    (H+'agent.rs','the first keeper page can start beyond its owner','after.clamp(0, next - 1)','after.max(0)'),
    (H+'agent.rs','the keeper includes the next unused ID in its starting cap','after.clamp(0, next - 1)','after.clamp(0, next)'),
    (H+'keeper.rs','gather uses the uncorrected document cursor','let since = (since_ms, start);','let since = (since_ms, requested);'),
]: plant(file,'keeper',why,old,new)
for file,why,old,new in [
    ('request_tool.rs','arming trusts a different named requester','Some(id) if id == owner.record.agent_id','Some(id) if !id.is_empty()'),
    ('request_tool.rs','arming discards the identified owner',
     '                deadline,\n                request_owner(turn.as_deref(), &requester),',
     '                deadline,\n                crate::agent::GoalCaller::Default,'),
    ('request_tool.rs','abandoning a definitely refused request loses the caller',
     '                        &format!("HTTP {status}"),\n                        request_owner(turn.as_deref(), &requester),',
     '                        &format!("HTTP {status}"),\n                        crate::agent::GoalCaller::Default,'),
    ('request_tool.rs','a verified answer claims the named replier','Peer(who.record.agent_id.clone(), true)','Peer(replier.record.agent_id.clone(), true)'),
    ('request_tool.rs','an answer with no token becomes verified','Peer(replier.record.agent_id.clone(), false)','Peer(replier.record.agent_id.clone(), true)'),
    ('tools.rs','the goal CLI drops its turn','Ok(Some(who)) => crate::agent::GoalCaller::Verified(who.record.agent_id.clone())','Ok(Some(_)) => crate::agent::GoalCaller::Default'),
    ('plans.rs','an action discards a presented identity','crate::agent::GoalCaller::Verified(who.record.agent_id.clone())','crate::agent::GoalCaller::Default'),
    ('agent.rs','a tokenless user action is claimed by its owner','(_, goaltool::Ask::UserAct { .. }) => Author::User','(_, goaltool::Ask::UserAct { .. }) => Author::Owner(None)'),
    ('agent.rs','the keeper writes without its identity','Author::Keeper(&written.janitor_agent_id)','Author::System'),
    ('agent.rs','a user Stop claims a system actor','clarp_kernel::goals::ledger::Author::User,','clarp_kernel::goals::ledger::Author::System,'),
    ('agent.rs','a non-user Stop changes the ledger author','clarp_kernel::goals::ledger::Author::User,','if !reason.starts_with("Stopped by") { clarp_kernel::goals::ledger::Author::User } else { clarp_kernel::goals::ledger::Author::System },'),
    ('agent.rs','the reason loses who stopped the goal','clarp_kernel::control::paused_by(&by)','"User stopped this agent".to_string()'),
    ('agent.rs','a conversation carried over is claimed by the user','clarp_kernel::goals::ledger::Author::System,\n                    now,','clarp_kernel::goals::ledger::Author::User,\n                    now,'),
]: plant(H+file,'doors',why,old,new)
for why,old,new in [
    ('a durable removal returns a failure when only retirement failed','self.works.log.unkept("goal ledger retirement", &why);','return Err(Unavailable::new(why));'),
    ('a failed retirement goes unreported','self.works.log.unkept("goal ledger retirement", &why);','drop(why);'),
    ('reading a tombstoned ledger before retirement is allowed','if !record.ledger_retired {\n            return Err','if false {\n            return Err'),
]: plant(H+'fleet.rs','retire',why,old,new)

if __name__=='__main__': raise SystemExit(runner.main(__file__,RUNS,PLANTS))
