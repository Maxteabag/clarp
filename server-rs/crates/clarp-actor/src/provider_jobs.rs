//! Provider tasks share the agent's job registry. A failed append must leave
//! both the projection and the transcript position at their previous values.
use super::*;
use clarp_kernel::provider_tasks::{
    self as rules, Attempt, Event, FinalRead, JournalEntry, Observation, Session, TurnLink, Visit,
};

impl Actor {
    pub fn provider_session(&self, native: &str) -> Option<&Session> {
        self.provider_sessions.get(native)
    }
    pub fn provider_sessions(&self) -> impl Iterator<Item = (&String, &Session)> {
        self.provider_sessions.iter()
    }
    /// A runtime bound by an older Host has no provider cursor. Its first
    /// binding is already dated in the same log. Read it once and keep
    /// that time with the new cursor so pre-binding launches stay excluded.
    pub fn first_provider_binding(&self, native: &str) -> Result<Option<i64>, ActorError> {
        use clarp_kernel::agent::AgentEvent;
        use clarp_kernel::turn::TurnEvent;
        let mut after = 0;
        loop {
            let page = self.recorder.log().read_after(
                after,
                clarp_ports::log::Kinds::only(kind::AGENT_EVENT),
                WINDOW,
            )?;
            let Some(last) = page.last() else {
                return Ok(None);
            };
            after = last.seq;
            for item in page {
                let event: AgentEvent = serde_json::from_slice(&item.body)
                    .map_err(|e| ActorError::Corrupt(e.to_string()))?;
                let id = match event {
                    AgentEvent::Adopted {
                        backend_session: Some(id),
                        ..
                    }
                    | AgentEvent::TookUp {
                        backend_session: id,
                        ..
                    }
                    | AgentEvent::Turn {
                        change:
                            TurnEvent::Bound {
                                backend_session: id,
                            },
                        ..
                    } => id,
                    _ => continue,
                };
                if id == native {
                    return Ok(Some(item.ts_ms));
                }
            }
        }
    }
    pub fn provider_attempt(&self) -> Option<Attempt> {
        self.live
            .as_ref()
            .filter(|_| self.recorder.agent().live().is_some_and(Turn::has_process))
            .map(|live| Attempt {
                turn: live.turn.0,
                attempt: live.attempt,
            })
    }
    pub fn expire_provider_links(&mut self, native: &str, now: i64) -> Result<bool, ActorError> {
        let Some(saved) = self.provider_sessions.get(native) else {
            return Ok(false);
        };
        if saved.final_read.is_some() {
            return Ok(false);
        }
        if !saved.links.values().any(|link| {
            link.exited_at
                .is_some_and(|at| rules::turn_link_expired(at, now))
        }) {
            return Ok(false);
        }
        let mut session = saved.clone();
        session.links.retain(|_, link| {
            !link
                .exited_at
                .is_some_and(|at| rules::turn_link_expired(at, now))
        });
        session.revision += 1;
        self.commit_provider(
            JournalEntry {
                native: native.into(),
                session,
                records: Vec::new(),
            },
            now,
        )?;
        Ok(true)
    }
    /// An interactive resume has a runtime lifetime but owns no managed
    /// attempt. Closing its terminal can make a task unknown, never stopped.
    pub fn provider_terminal(
        &mut self,
        native: &str,
        open: bool,
        now: i64,
    ) -> Result<(), ActorError> {
        let mut session = self
            .provider_sessions
            .get(native)
            .cloned()
            .unwrap_or_default();
        if session.bound_at == 0 {
            session.bound_at = now;
            session.cwd = self.profile.cwd.to_string_lossy().into_owned();
        }
        if session.newest_owner().is_some() {
            return Ok(());
        }
        let ended = (!open).then_some(now);
        if session.ended_at == ended && session.revision > 0 {
            return Ok(());
        }
        session.ended_at = ended;
        session.revision += 1;
        self.commit_provider(
            JournalEntry {
                native: native.into(),
                session,
                records: Vec::new(),
            },
            now,
        )?;
        Ok(())
    }
    /// Pin the end of the previous attempt before its successor's shim
    /// may append. Unread older records keep their original turn ownership.
    pub fn provider_boundary(
        &mut self,
        native: &str,
        successor: Option<Attempt>,
        identity: (u64, u64, u64),
        now: i64,
    ) -> Result<(), ActorError> {
        let mut session = self
            .provider_sessions
            .get(native)
            .cloned()
            .unwrap_or_default();
        let previous = session.newest_owner();
        if previous == successor {
            return Ok(());
        }
        let (device, inode, end) = identity;
        if session.bound_at == 0 {
            session.bound_at = now;
        }
        session.cwd = self.profile.cwd.to_string_lossy().into_owned();
        let boundary = FinalRead {
            attempt: previous,
            successor,
            exited_at: now,
            device,
            inode,
            end,
        };
        if session.final_read.is_some() {
            session.following.push_back(boundary);
        } else {
            session.final_read = Some(boundary);
        }
        session.revision += 1;
        self.commit_provider(
            JournalEntry {
                native: native.into(),
                session,
                records: Vec::new(),
            },
            now,
        )?;
        Ok(())
    }
    pub fn apply_provider_visit(
        &mut self,
        visit: Visit,
        now: i64,
        revision: impl Fn() -> u64,
    ) -> Result<Outputs, ActorError> {
        let previous = self
            .provider_sessions
            .get(&visit.native)
            .cloned()
            .unwrap_or_default();
        if previous.revision != visit.expected {
            return Ok(Outputs::default());
        }
        let mut next = previous.clone();
        next.cursor = visit.cursor;
        if next.bound_at == 0 {
            next.bound_at = visit.bound_at;
        }
        if next.cwd.is_empty() {
            next.cwd = self.profile.cwd.to_string_lossy().into_owned();
        }
        next.owner = visit.owner;
        next.final_read = visit.final_read;

        let mut records = Vec::new();
        for action in &visit.actions {
            let id = rules::job_id(&self.profile.agent_id, &visit.native, &action.tool);
            if let Some(record) = self.jobs.provider_observation(
                &id,
                &records,
                Observation::Evidence {
                    native: &visit.native,
                    bound_at: next.bound_at,
                    action,
                },
                now,
                &revision,
            ) {
                if matches!(action.event, Event::Launch { .. })
                    && let Some(attempt) = visit.owner
                {
                    next.links.entry(id.clone()).or_insert(TurnLink {
                        attempt,
                        exited_at: None,
                    });
                }
                records.push(record);
            }
        }
        // Real receipts in this visit win before an inferred exit. An
        // unresolved final boundary persists across bounded reads and reopen.
        let final_exit = next.final_read.clone().filter(|end| {
            visit.eof
                || next.cursor.device != end.device
                || next.cursor.inode != end.inode
                || next.cursor.offset >= end.end
        });
        if let Some(end) = &final_exit {
            for link in next
                .links
                .values_mut()
                .filter(|link| Some(link.attempt) == end.attempt)
            {
                link.exited_at.get_or_insert(end.exited_at);
            }
        }
        let ids: std::collections::BTreeSet<_> = self
            .jobs
            .active()
            .chain(records.iter().map(|r| &r.job))
            .filter(|j| {
                j.status.is_active()
                    && j.provider_owned()
                    && j.metadata["native_session_id"] == visit.native
            })
            .map(|j| j.id.clone())
            .collect();
        let mut reconciled = true;
        for id in ids {
            if records.len() >= rules::MAX_ACTIONS {
                reconciled = false;
                break;
            }
            let link = next.links.get(&id);
            let command = if let Some(link) = link
                && let Some(exit) = link.exited_at
            {
                let turn = format!("{}:{}", link.attempt.turn, link.attempt.attempt);
                if let Some(r) = self.jobs.provider_observation(
                    &id,
                    &records,
                    Observation::Exit { turn: &turn },
                    exit,
                    &revision,
                ) {
                    records.push(r);
                }
                continue;
            } else {
                Observation::Age {
                    protected: link.is_some_and(|l| Some(l.attempt) == next.owner),
                }
            };
            if let Some(r) = self
                .jobs
                .provider_observation(&id, &records, command, now, &revision)
            {
                records.push(r);
            } else if let Some(r) = self.jobs.provider_observation(
                &id,
                &records,
                Observation::Reconcile {
                    ended: final_exit
                        .as_ref()
                        .map_or(next.ended_at.is_some(), |b| b.attempt.is_some()),
                },
                now,
                &revision,
            ) {
                records.push(r);
            }
        }
        if reconciled && let Some(end) = final_exit {
            next.ended_at = end.successor.is_none().then_some(end.exited_at);
            next.owner = end.successor;
            next.final_read = next.following.pop_front();
        }
        // An old end can require several batches on rejoin. Keep the
        // launch links until every task of that attempt has been settled.
        if next.final_read.is_none() {
            next.links.retain(|_, link| {
                !link
                    .exited_at
                    .is_some_and(|at| rules::turn_link_expired(at, now))
            });
        }
        if records.is_empty() && next == previous {
            return Ok(Outputs::default());
        }
        next.revision += 1;
        let entry = JournalEntry {
            native: visit.native,
            session: next,
            records,
        };
        self.commit_provider(entry, now)
    }
    fn commit_provider(&mut self, entry: JournalEntry, now: i64) -> Result<Outputs, ActorError> {
        let bytes =
            serde_json::to_vec(&entry).map_err(|err| ActorError::Corrupt(err.to_string()))?;
        let mut batch = self.recorder.batch();
        batch.item(NewItem::new(kind::PROVIDER_JOBS, now, bytes));
        batch.commit()?;
        let mut out = Outputs::default();
        out.changed.jobs = !entry.records.is_empty();
        for record in entry.records {
            self.jobs.apply(&record);
            self.remember_job_event(&record);
        }
        self.provider_sessions.insert(entry.native, entry.session);
        Ok(out)
    }
}
