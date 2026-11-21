#!/usr/bin/env python3
"""Wrong rules for bookkeeping authority, recovery and its Host doors."""
import runner
K='clarp-kernel/src/goals/delegation.rs'
A='clarp-actor/src/delegation.rs'
B='clarp-actor/src/bookkeeping.rs'
F='clarp-host/src/fleet/delegations.rs'
H='clarp-host/src/'
RUNS={
    'rules':['test','--test','goal_delegation'],
    'mailbox':['test','--test','actor','the_principals_mailbox_orders_delegation'],
    'cuts':['test','-p','clarp-host','--lib','each_cut_in_the_two_store_protocol'],
    'roles':['test','-p','clarp-host','--lib','the_fleets_two_active_role_keys'],
    'port':['test','-p','clarp-host','--lib','authority_at_the_port_obeys_mailbox_order'],
    'baseline':['test','--test','actor','the_delegates_baseline_is_the_revision'],
    'startup':['test','-p','clarp-host','--lib','a_damaged_reservation_never_stops_the_host'],
    'rejoin':['test','-p','clarp-host','--lib','the_owner_rejoins_while_its_delegation_index'],
    'timeout':['test','-p','clarp-host','--lib','a_silent_principal_cannot_hold_the_fleet_mailbox'],
    'doors':['test','--test','host','a_delegation_is_reserved_started_enforced'],
}
PLANTS=[]
def put(file,run,why,old,new):
    PLANTS.append((file,run,why,old,new))

for why,old,new in [
    ('zero baseline refused','0..=2000','1..=2000'),
    ('negative baseline allowed','0..=2000','-1..=2000'),
    ('largest baseline refused','0..=2000','0..=1999'),
    ('oversized baseline allowed','0..=2000','0..=2001'),
    ('reason not stripped','python::strip(raw)','raw'),
    ('blank reason allowed','if text.is_empty()','if false'),
    ('reason loses its last character','count() > 500','count() >= 500'),
    ('reason may have one more character','count() > 500','count() > 501'),
    ('principal can be its delegate','principal.id == delegate.id','false'),
    ('direct parent unchecked','delegate.parent != Some(principal.id)','false'),
    ('delegate need not be a helper','delegate.role != "helper"','false'),
    ('archived or deleted helper accepted','if delegate.archived_or_deleted','if false'),
    ('identityless backend accepted','if !carries_identity(agent.backend)','if false'),
    ('principal backend not checked','[("principal", principal), ("delegate", delegate)]','[("delegate", delegate), ("delegate", delegate)]'),
    ('delegate backend not checked','[("principal", principal), ("delegate", delegate)]','[("principal", principal), ("principal", principal)]'),
    ('Claude identity unavailable','"claude" | "agy"','"bogus" | "agy"'),
    ('Antigravity identity unavailable','"agy" | "grok"','"bogus" | "grok"'),
    ('Grok identity unavailable','"grok" | "opencode"','"bogus" | "opencode"'),
    ('OpenCode identity unavailable','"opencode" | "deepseek"','"bogus" | "deepseek"'),
    ('DeepSeek identity unavailable','| "deepseek"','| "bogus"'),
    ('an unverified agent may edit','if !verified','if false'),
    ('wrong peer may edit','if !descendant && backend.is_some_and','if backend.is_some_and'),
    ('a verified descendant can edit','if descendant {','if false {'),
    ('a delegated write may overwrite the principal','if kind == "delegate"','if false'),
    ('the user needs a provider token','"user" | "system"','"bogus" | "system"'),
    ('the system needs a provider token','"system" | "legacy"','"bogus" | "legacy"'),
    ('legacy history needs a provider token','| "legacy"','| "bogus"'),
]:
    put(K,'rules',why,old,new)
for why,old,new in [
    ('accepted start absent after restart','Some(binding),','None,'),
    ('lifecycle admits a different delegation','active.delegation_id == id','active.delegation_id != id'),
    ('no identified caller can write','verified_caller == Some(active.delegate_agent_id.as_str())','verified_caller.is_none()'),
    ('the stopped delegation can be restarted','self.delegation.is_none() && prior.is_none()','self.delegation.is_none()'),
    ('a matching start cannot be replayed','if self.delegation.as_ref() == Some(binding) => {\n                return Ok(true);','if self.delegation.as_ref() == Some(binding) => {\n                return Ok(false);'),
    ('the active fold does not advance','self.delegation = next;','drop(next);'),
    ('the admitted operation never runs','if !self.admits_delegate(id, caller)','if true'),
]:
    put(A,'mailbox',why,old,new)
for why,old,new in [
    ('role keys held past stop','== Some(self.delegation_id.as_bytes())','!= Some(self.delegation_id.as_bytes())'),
    ('STOP loses its timestamp','let mut row = self.clone();\n            row.stopped_at = Some(at);','let mut row = self.clone();\n            row.stopped_at = Some(at+1);'),
    ('STOP loses its reason','row.stopped_reason = reason.to_owned();','row.stopped_reason.clear();'),
    ('a never-started reservation stays in history','(ROWS.into(), self.delegation_id.clone(), None)','(ROWS.into(), self.delegation_id.clone(), Some(serde_json::to_vec(self).unwrap()))'),
    ('no cleanup of unstarted credential','remove_credential(root, id)?;','let _ = (root,id);'),
    ('orphan authority is left open','!= Some(&active)','== Some(&active)'),
    ('a recovered start has a wrong map key','expected.insert(principal, entry);','expected.insert(String::new(), entry);'),
]:
    put(F,'cuts',why,old,new)
for why,old,new in [
    ('creation accepts a duplicate principal or helper','if store.get(table, key).map_err(|e| e.to_string())?.is_some()','if false'),
    ('the helper is looked up as principal','DELEGATES.into(),\n                    self.delegate_agent_id.clone(),\n                    Some(id.to_vec()),','PRINCIPALS.into(),\n                    self.delegate_agent_id.clone(),\n                    Some(id.to_vec()),'),
]:
    put(F,'roles',why,old,new)
for why,old,new in [
    ('a delegate credential is readable by other users','.mode(0o600)','.mode(0o644)'),
    ('history loses the principal session','session(&r.principal_agent_id)?','session(&r.delegate_agent_id)?'),
    ('history loses the helper session','session(&r.delegate_agent_id)?','session(&r.principal_agent_id)?'),
    ('a new relationship reports its listener unavailable','"started":true','"started":false'),
    ('an archived principal may enable','if principal.record.archived_at_ms.is_some()','if false'),
]:
    put(F,'doors',why,old,new)
for why,old,new in [
    ('a row can stop a different principal','Transition::Stop { .. } if self.delegation.as_ref() == Some(binding) => None','Transition::Stop { .. } if self.delegation.as_ref() != Some(binding) => None'),
    ('a new active relationship may never start','self.delegation.is_none() && prior.is_none()','self.delegation.is_some() && prior.is_none()'),
    ('a read after restart loses its delegation history','Some(Transition::Start { binding }) => Some(binding)','Some(Transition::Start { binding }) => None'),
]:
    put(A,'mailbox',why,old,new)
for file,why,old,new in [
    (F,'the second role is not retired on deletion','for row in &rows {\n            self.stage_delegation_stop','for row in rows.iter().take(1) {\n            self.stage_delegation_stop'),
    (F,'released relationships disappear from history','Ok(rows)\n}\n\npub(super) fn active_for_agent','rows.retain(|r|r.stopped_at.is_none());Ok(rows)\n}\n\npub(super) fn active_for_agent'),
    (F,'a created relationship leaks its token digest','"status":if r.stopped_at','"token_hash":r.token_hash,"status":if r.stopped_at'),
    (F,'a reply loses who stopped the relationship','"stopped_reason":r.stopped_reason','"stopped_reason":""'),
    (F,'stopped row claims active','{"stopped"}else{"active"}','{"active"}else{"active"}'),
    # Applied::show writes the progress fields on every successful response.
    (B,'listener is said to be in flight before any dispatch',
     'row["in_flight"] = json!(\n'
     '            row["dispatched_message_through"].as_i64().unwrap_or(0) > self.message_through\n'
     '                || row["dispatched_goal_event_through"].as_i64().unwrap_or(0)\n'
     '                    > self.goal_event_through\n'
     '        );',
     'row["in_flight"] = json!(true);'),
    (B,'unused cursor is reported spent','row["goal_event_through"] = json!(self.goal_event_through);',
     'row["goal_event_through"] = json!(self.goal_event_through.max(1));'),
    (F,'the initial reason is reset','"created_reason":r.created_reason','"created_reason":""'),
    (B,'a delegation reports a fabricated lag','.map_or(0, |since| now.saturating_sub(since))',
     '.map_or(1, |since| now.saturating_sub(since))'),
]:
    put(file,'doors',why,old,new)
for why,old,new in [
    ('a baseline returns the following distinct message','select_nth_unstable_by(offset,','select_nth_unstable_by(offset.saturating_sub(1),'),
    ('a baseline counts the oldest revisions first','|a, b| b.cmp(a)','|a, b| a.cmp(b)'),
    ('an older chunk is never read','before = first.seq;','before = 1;'),
    ('the initial cursor of a short history is nonzero','if part.len() < CHUNK {\n            return Ok(0);','if part.len() < CHUNK {\n            return Ok(1);'),
]: put('clarp-actor/src/conversation.rs','baseline',why,old,new)
put(A,'port','a STOP is admitted as a new START','Transition::Stop { .. } if self.delegation.as_ref() == Some(binding) => None','Transition::Stop { .. } if self.delegation.as_ref() == Some(binding) => Some(binding.clone())')
put(F,'doors','the fleet loses the helper creator during a fork','Request::Descendant(name.into())','Request::Descendant(String::new())')
put(H+'request_tool.rs','doors','an unverified delegate is allowed to reply to its principal','delegation["delegate_agent_id"] == replier.record.agent_id','false')
put(H+'edge.rs','doors','a delegate may send to its principal','delegation["delegate_agent_id"].as_str() == Some(sender.agent_id.as_str())','false')
for why,old,new in [
    ('a damaged START stops the Host','Err(why) => skipped(id, &why),','Err(why) => return Err(why),'),
    ('corrupt reservation omitted without its id','Err(why) => skipped(id, &why),','Err(_why) => {},'),
    ('invalid id bytes shut down the Host','skipped(&label, "non-UTF-8 reservation");','return Err(label);'),
    ('a failed principal log is silently skipped','let log = open_log(&principal)?;','let Ok(log) = open_log(&principal) else { return Ok(None); };'),
    ('a stop/cursor disagreement is trusted','&& clarp_actor::delegation::active(log.as_ref())','|| clarp_actor::delegation::active(log.as_ref())'),
    ('secondary role keys can name some other delegation','!= Some(id.as_bytes())','== Some(&[][..])'),
    ('a damaged row is reported with no count','skipped += 1;','skipped += 0;'),
]: put(F,'startup',why,old,new)
for why,old,new in [
    ('no notice about a missing START','Ok(None) => MismatchState::Missing','Ok(None) => return'),
    ('no notice about a stopped START','MismatchState::Stopped(reason, at)\n            }','return;\n            }'),
    ('no notice about inconsistent history','MismatchState::Inconsistent(\n                "delegation history disagrees with current log binding".into(),\n            )','return'),
    ('no notice about unreadable history','Err(e) => MismatchState::Inconsistent(e.to_string()),','Err(_) => return,'),
    ('log disagreement is reported without its id','let id = m.id().to_owned();','let id = String::new();'),
]: put(F if 'let id = m.id()' not in old else H+'fleet.rs','rejoin',why,old,new)
put(H+'agent.rs','timeout','a blocked agent can occupy the fleet longer than the bound','recv_timeout(Duration::from_secs(3))','recv_timeout(Duration::from_secs(9))')
put(F,'startup','history trusts a quarantined reservation','if !self.delegation_desk.expects(&row)','if false')
put(F,'cuts','a history row omits the delegate','row.principal_agent_id == id || row.delegate_agent_id == id','row.principal_agent_id == id')
put(F,'cuts','a history row omits the principal','row.principal_agent_id == id || row.delegate_agent_id == id','row.delegate_agent_id == id')
if __name__=='__main__':
    raise SystemExit(runner.main(__file__,RUNS,PLANTS))
