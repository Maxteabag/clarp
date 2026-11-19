//! Provider tasks share the agent's job registry. A failed append must leave
//! both the projection and the transcript position at their previous values.
use super::*;
use clarp_kernel::provider_tasks::{self as rules,Attempt,Event,JournalEntry,Observation,Session,TurnLink,Visit};

impl Actor {
    pub fn provider_session(&self,native:&str)->Option<&Session>{self.provider_sessions.get(native)}
    pub fn provider_sessions(&self)->impl Iterator<Item=(&String,&Session)>{self.provider_sessions.iter()}
    pub fn provider_attempt(&self)->Option<Attempt>{
        self.live.as_ref().filter(|_|self.recorder.agent().live().is_some_and(Turn::has_process)).map(|live|Attempt{turn:live.turn.0,attempt:live.attempt})
    }
    pub fn apply_provider_visit(&mut self,visit:Visit,now:i64,revision:impl Fn()->u64)->Result<Outputs,ActorError>{
        let previous=self.provider_sessions.get(&visit.native).cloned().unwrap_or_default();
        if previous.revision!=visit.expected{return Ok(Outputs::default())}
        let mut next=previous.clone();
        next.cursor=visit.cursor;
        if next.bound_at==0{next.bound_at=visit.bound_at;}
        next.owner=visit.owner;
        next.final_read=visit.final_read;
        let mut staged=self.jobs.clone();
        let mut records=Vec::new();
        for action in &visit.actions{
            let id=rules::job_id(&self.profile.agent_id,&visit.native,&action.tool);
            if let Some(record)=staged.provider_observation(&id,Observation::Evidence{native:&visit.native,bound_at:next.bound_at,action},now,&revision){
                if matches!(action.event,Event::Launch{..}) && let Some(attempt)=visit.owner{
                    next.links.entry(id.clone()).or_insert(TurnLink{attempt,exited_at:None});
                }
                staged.apply(&record);
                records.push(record);
            }
        }
        // Real receipts in this visit win before an inferred exit. An
        // unresolved final boundary persists across bounded reads and reopen.
        let final_exit=next.final_read.clone().filter(|end|visit.eof || next.cursor.device!=end.device || next.cursor.inode!=end.inode || next.cursor.offset>=end.end);
        if let Some(end)=&final_exit {
            for link in next.links.values_mut().filter(|link|link.attempt==end.attempt){link.exited_at.get_or_insert(end.exited_at);}
            next.owner=None;next.final_read=None;
        }
        let ids:Vec<_>=staged.active().filter(|j|j.provider_owned()&&j.metadata["native_session_id"]==visit.native).map(|j|j.id.clone()).collect();
        for id in ids {
            let link=next.links.get(&id);
            let command=if let Some(link)=link && let Some(exit)=link.exited_at {
                let turn=format!("{}:{}",link.attempt.turn,link.attempt.attempt);
                if let Some(r)=staged.provider_observation(&id,Observation::Exit{turn:&turn},exit,&revision){staged.apply(&r);records.push(r);}
                continue;
            }else{
                Observation::Age{protected:link.is_some_and(|l|Some(l.attempt)==next.owner)}
            };
            if let Some(r)=staged.provider_observation(&id,command,now,&revision){staged.apply(&r);records.push(r);}
            else if let Some(r)=staged.provider_observation(&id,Observation::Reconcile{ended:final_exit.is_some()},now,&revision){staged.apply(&r);records.push(r);}
        }
        next.links.retain(|_,link|!link.exited_at.is_some_and(|at|rules::turn_link_expired(at,now)));
        if records.is_empty() && next==previous{return Ok(Outputs::default())}
        next.revision+=1;
        let entry=JournalEntry{native:visit.native,session:next,records};
        let bytes=serde_json::to_vec(&entry).map_err(|err|ActorError::Corrupt(err.to_string()))?;
        let mut batch=self.recorder.batch();
        batch.item(NewItem::new(kind::PROVIDER_JOBS,now,bytes));
        batch.commit()?;
        let mut out=Outputs::default();
        out.changed.jobs=!entry.records.is_empty();
        for record in entry.records{self.jobs.apply(&record);}
        self.provider_sessions.insert(entry.native,entry.session);
        Ok(out)
    }
}
