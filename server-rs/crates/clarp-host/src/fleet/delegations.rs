//! Fleet-side reservation and discovery. Admission belongs to the principal's
//! actor. Every change produced here is one KeyValue transaction.
use clarp_actor::delegation::Binding;
use clarp_ports::kv::{KeyValue, Write};
use serde::{Deserialize, Serialize};

const ROWS: &str = "kept.goal_delegations";
const PRINCIPALS: &str = "kept.goal_delegation_principals";
const DELEGATES: &str = "kept.goal_delegation_delegates";

#[derive(Clone, Debug, Serialize, Deserialize)]
pub(super) struct Reservation {
    pub delegation_id: String,
    pub principal_agent_id: String,
    pub delegate_agent_id: String,
    pub token_hash: String,
    pub created_at: i64,
    pub created_reason: String,
    pub message_through: i64,
    pub stopped_at: Option<i64>,
    pub stopped_reason: String,
}

impl Reservation {
    pub fn binding(&self) -> Binding {
        Binding {
            delegation_id: self.delegation_id.clone(),
            delegate_agent_id: self.delegate_agent_id.clone(),
        }
    }
    fn row(&self) -> Result<Write, String> {
        Ok((
            ROWS.into(),
            self.delegation_id.clone(),
            Some(serde_json::to_vec(self).map_err(|e| e.to_string())?),
        ))
    }

    /// Called by the fleet's single writer. Neither uniqueness check can race
    /// with the transaction that reserves both roles.
    pub fn reserve(&self, store: &mut dyn KeyValue) -> Result<(), String> {
        for (table, key) in [
            (PRINCIPALS, &self.principal_agent_id),
            (DELEGATES, &self.delegate_agent_id),
        ] {
            if store.get(table, key).map_err(|e| e.to_string())?.is_some() {
                return Err(
                    "an active delegation already exists for this principal or delegate".into(),
                );
            }
        }
        if store
            .get(ROWS, &self.delegation_id)
            .map_err(|e| e.to_string())?
            .is_some()
        {
            return Err("delegation id already exists".into());
        }
        let id = self.delegation_id.as_bytes();
        store
            .put_together(&[
                self.row()?,
                (
                    PRINCIPALS.into(),
                    self.principal_agent_id.clone(),
                    Some(id.to_vec()),
                ),
                (
                    DELEGATES.into(),
                    self.delegate_agent_id.clone(),
                    Some(id.to_vec()),
                ),
            ])
            .map_err(|e| e.to_string())
    }

    /// Only release keys still naming this row. Reconciliation may revisit an
    /// old stop after the same principal or delegate has acquired a new role.
    fn release_keys(&self, store: &dyn KeyValue, writes: &mut Vec<Write>) -> Result<(), String> {
        for (table, key) in [
            (PRINCIPALS, &self.principal_agent_id),
            (DELEGATES, &self.delegate_agent_id),
        ] {
            if store.get(table, key).map_err(|e| e.to_string())?.as_deref()
                == Some(self.delegation_id.as_bytes())
            {
                writes.push((table.into(), key.clone(), None));
            }
        }
        Ok(())
    }

    /// The principal logged the STOP. The caller combines this with any roster
    /// update and other relationships before committing the resulting writes.
    pub fn stopped(
        &self,
        store: &dyn KeyValue,
        at: i64,
        reason: &str,
        writes: &mut Vec<Write>,
    ) -> Result<(), String> {
        if self.stopped_at.is_none() {
            let mut row = self.clone();
            row.stopped_at = Some(at);
            row.stopped_reason = reason.to_owned();
            writes.push(row.row()?);
        }
        self.release_keys(store, writes)
    }

    /// There was never a START. Such a reservation is not delegation history.
    pub fn abandon(&self, store: &mut dyn KeyValue) -> Result<(), String> {
        let mut writes = vec![(ROWS.into(), self.delegation_id.clone(), None)];
        self.release_keys(store, &mut writes)?;
        store.put_together(&writes).map_err(|e| e.to_string())
    }
}

pub(super) fn find(store: &dyn KeyValue, id: &str) -> Result<Option<Reservation>, String> {
    store
        .get(ROWS, id)
        .map_err(|e| e.to_string())?
        .map(|v| serde_json::from_slice(&v).map_err(|e| e.to_string()))
        .transpose()
}

pub(super) fn for_agent(store: &dyn KeyValue, id: &str) -> Result<Vec<Reservation>, String> {
    let mut rows = Vec::new();
    for (key, bytes) in store.list(ROWS).map_err(|e| e.to_string())? {
        let Ok(row) = serde_json::from_slice::<Reservation>(&bytes) else {
            continue;
        };
        if row.delegation_id == key && (row.principal_agent_id == id || row.delegate_agent_id == id)
        {
            rows.push(row);
        }
    }
    rows.sort_by_key(|r| std::cmp::Reverse(r.created_at));
    Ok(rows)
}

pub(super) fn active_for_agent(store: &dyn KeyValue, id: &str) -> Result<Vec<Reservation>, String> {
    let mut rows = Vec::new();
    for table in [PRINCIPALS, DELEGATES] {
        if let Some(key) = store.get(table, id).map_err(|e| e.to_string())? {
            let key = std::str::from_utf8(&key).map_err(|e| e.to_string())?;
            rows.push(
                find(store, key)?.ok_or_else(|| "missing active delegation row".to_string())?,
            );
        }
    }
    Ok(rows)
}

use super::{AGENTS, AgentRecord, Fleet, Member};
use crate::agent::{AgentHandle, DelegationCommand, Unavailable};
use clarp_actor::delegation::Transition;
use std::sync::Arc;

impl Fleet {
    fn delegation_member(&self, id: &str) -> Result<Arc<Member>, String> {
        self.place_of_any(id)
            .map(|at| self.members[at].clone())
            .ok_or_else(|| "principal or delegate not found".into())
    }

    /// Store the tombstone/archive and both possible stops in one transaction.
    /// A failed fleet write leaves the keys reserved. Admission is already
    /// closed in each log whose STOP committed; reconciliation can release it.
    pub(super) fn stop_for_roster(
        &mut self,
        record: &AgentRecord,
        reason: &str,
        removing: Option<&AgentHandle>,
    ) -> Result<(), Unavailable> {
        let rows = active_for_agent(self.store.as_ref(), &record.agent_id).map_err(Unavailable)?;
        let mut writes = if rows.is_empty() {
            Vec::new()
        } else {
            let bytes = serde_json::to_vec(record).map_err(|e| Unavailable(e.to_string()))?;
            vec![(AGENTS.into(), record.agent_id.clone(), Some(bytes))]
        };
        for row in &rows {
            self.stage_delegation_stop(row, reason, &mut writes)?;
        }
        let mut keep = || {
            if rows.is_empty() {
                self.put(record).map_err(Unavailable)
            } else {
                self.store
                    .put_together(&writes)
                    .map_err(|e| Unavailable(e.to_string()))
            }
        };
        match removing {
            Some(agent) => agent.keep_removal(keep)?,
            None => keep()?,
        }
        for row in &rows {
            self.delegation_desk.forget(row);
        }
        Ok(())
    }

    fn stage_delegation_stop(
        &self,
        row: &Reservation,
        reason: &str,
        writes: &mut Vec<Write>,
    ) -> Result<(), Unavailable> {
        let principal = self
            .delegation_member(&row.principal_agent_id)
            .map_err(Unavailable)?;
        let command = Transition::Stop {
            binding: row.binding(),
            reason: reason.to_string(),
        };
        let reply = principal
            .agent
            .delegation(DelegationCommand::Change(command))?;
        if !reply.accepted {
            return Err(Unavailable(
                "the principal has no matching delegation".into(),
            ));
        }
        let Some((Transition::Stop { reason, .. }, at)) = reply.prior else {
            return Err(Unavailable("the principal did not record a stop".into()));
        };
        row.stopped(self.store.as_ref(), at, &reason, writes)
            .map_err(Unavailable)
    }
}

use crate::edge::ApiError;
use axum::http::StatusCode;
use clarp_kernel::goals::delegation::{self as rules, Participant};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    fs,
    io::Write as _,
    os::unix::fs::{DirBuilderExt, OpenOptionsExt},
    path::Path,
};

pub(crate) enum Request {
    Listeners,
    Authorize {
        id: String,
        caller: String,
        credential: String,
    },
    Enable {
        principal: String,
        delegate: String,
        reason: String,
    },
    Stop {
        id: String,
        reason: String,
    },
    List(String),
    Current(String),
    Descendant(String),
}

fn conflict(why: impl Into<String>) -> ApiError {
    ApiError::new(StatusCode::CONFLICT, why)
}
fn unavailable(why: impl ToString) -> ApiError {
    Unavailable(why.to_string()).into()
}
fn participant(r: &AgentRecord) -> Participant<'_> {
    Participant {
        id: &r.agent_id,
        role: &r.role,
        parent: r.parent_agent_id.as_deref(),
        backend: &r.backend,
        archived_or_deleted: r.archived_at_ms.is_some() || r.deleted_at_ms.is_some(),
    }
}

/// Create the credential exclusively in the Host's private data directory.
/// Only its digest is part of the fleet's record.
fn credential(path: &Path, token: &str) -> std::io::Result<()> {
    let dir = path.parent().ok_or(std::io::ErrorKind::InvalidInput)?;
    fs::DirBuilder::new()
        .mode(0o700)
        .recursive(true)
        .create(dir)?;
    let mut file = fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(path)?;
    let write = (|| {
        file.write_all(token.as_bytes())?;
        file.sync_all()?;
        fs::File::open(dir)?.sync_all()?;
        if let Some(parent) = dir.parent() {
            fs::File::open(parent)?.sync_all()?;
        }
        Ok(())
    })();
    if write.is_err() {
        fs::remove_file(path)?;
    }
    write
}

impl Fleet {
    fn remove_abandoned_credential(&self, id: &str) -> Result<(), String> {
        remove_credential(&self.works.config.data_dir, id)
    }

    fn public_delegation(&self, r: &Reservation) -> Result<Value, Unavailable> {
        let session = |id: &str| {
            self.stored_agent(id)
                .map(|v| v.map_or_else(String::new, |a| a.session))
        };
        let mut shown = json!({
            "delegation_id":r.delegation_id,"principal_agent_id":r.principal_agent_id,"delegate_agent_id":r.delegate_agent_id,
            "status":if r.stopped_at.is_some(){"stopped"}else{"active"},
            "created_at":r.created_at,"created_reason":r.created_reason,"stopped_at":r.stopped_at,"stopped_reason":r.stopped_reason,
            "principal_session":session(&r.principal_agent_id)?,"delegate_session":session(&r.delegate_agent_id)?,
            "message_through":r.message_through,"goal_event_through":0,"dispatched_message_through":0,
            "dispatched_goal_event_through":0,"last_wake_id":"","last_wake_at":null,"last_applied_at":null,
            "last_source_at":null,"last_heartbeat_at":null,"last_error":"","job_handle":"","unapplied_since":null,
            "lag_ms":0,"in_flight":false
        });
        let progress = if let Ok(principal) = self.delegation_member(&r.principal_agent_id) {
            principal
                .agent
                .delegation(DelegationCommand::Applied(
                    r.delegation_id.clone(),
                    r.message_through,
                ))?
                .applied
                .ok_or_else(|| Unavailable("missing bookkeeping progress".into()))?
        } else if self
            .stored_agent(&r.principal_agent_id)?
            .is_some_and(|p| p.deleted_at_ms.is_some())
        {
            let log = self
                .works
                .engine
                .open(
                    &self
                        .works
                        .config
                        .data_dir
                        .join("agents")
                        .join(&r.principal_agent_id),
                    self.works.codecs.clone(),
                )
                .map_err(|e| Unavailable(e.to_string()))?;
            clarp_actor::bookkeeping::read_progress(
                log.as_ref(),
                &r.delegation_id,
                r.message_through,
            )
            .map_err(|e| Unavailable(e.to_string()))?
        } else {
            return Err(Unavailable("principal unavailable".into()));
        };
        progress.show(&mut shown, self.now_ms());
        Ok(shown)
    }
    pub(super) fn delegation_request(&mut self, req: Request) -> Result<Value, ApiError> {
        match req {
            Request::Listeners => {
                let active = self.delegation_desk.reservations.borrow();
                let ids:Vec<_>=active.iter().filter(|(_,e)|!e.starting).map(|(principal,e)|
                    json!({"principal":principal,"delegate":e.binding.delegate_agent_id,"id":e.binding.delegation_id,"baseline":e.baseline,"created":e.created})
                ).collect();
                Ok(json!(ids))
            }
            Request::Authorize {
                id,
                caller,
                credential,
            } => {
                use subtle::ConstantTimeEq;
                let denied = |s| ApiError::new(StatusCode::FORBIDDEN, s);
                let why = "unknown delegation or wrong credential";
                let row = find(self.store.as_ref(), &id)
                    .map_err(unavailable)?
                    .ok_or_else(|| denied(why))?;
                let digest = format!("{:x}", Sha256::digest(credential.as_bytes()));
                if credential.is_empty()
                    || !bool::from(digest.as_bytes().ct_eq(row.token_hash.as_bytes()))
                {
                    return Err(denied(why));
                }
                if caller != row.delegate_agent_id {
                    return Err(denied(
                        "only the delegate's own Clarp turn can keep these books",
                    ));
                }
                if row.stopped_at.is_some() {
                    return Err(denied("this delegation is stopped"));
                }
                for key in [&row.principal_agent_id, &row.delegate_agent_id] {
                    if self
                        .delegation_member(key)
                        .map_or(true, |m| m.record.archived_at_ms.is_some())
                    {
                        return Err(denied("the principal or delegate is deleted or archived"));
                    }
                }
                let reserved = self.delegation_desk.expects(&row);
                if !reserved {
                    return Err(denied(why));
                }
                Ok(self.public_delegation(&row)?)
            }
            Request::Enable {
                principal,
                delegate,
                reason,
            } => self.enable_delegation(&principal, &delegate, &reason),
            Request::Stop { id, reason } => {
                let why = rules::reason(&reason).map_err(conflict)?;
                let row = find(self.store.as_ref(), &id)
                    .map_err(unavailable)?
                    .ok_or_else(|| conflict("no such delegation"))?;
                if row.stopped_at.is_none() {
                    let mut writes = vec![];
                    self.stage_delegation_stop(&row, why, &mut writes)?;
                    self.store.put_together(&writes).map_err(unavailable)?;
                    self.delegation_desk.forget(&row);
                }
                let row = find(self.store.as_ref(), &id)
                    .map_err(unavailable)?
                    .ok_or_else(|| conflict("no such delegation"))?;
                let mut answer = self.public_delegation(&row)?;
                answer["listener"] =
                    json!({"unit":format!("clarp-bookkeeping-{id}"),"stopped":true});
                Ok(answer)
            }
            Request::Current(id) => match self.current_for(&id)? {
                Some(row) => Ok(self.public_delegation(&row)?),
                None => Ok(Value::Null),
            },
            Request::Descendant(id) => Ok(json!(self.has_delegated_ancestor(&id)?)),
            Request::List(session) => {
                let member = self
                    .delegation_member(&session)
                    .map_err(|_| ApiError::new(StatusCode::NOT_FOUND, "agent not found"))?;
                let records =
                    for_agent(self.store.as_ref(), &member.record.agent_id).map_err(unavailable)?;
                let mut shown = Vec::with_capacity(records.len());
                for mut row in records {
                    if row.stopped_at.is_none() {
                        if !self.delegation_desk.expects(&row) {
                            // Startup left this damaged reservation for diagnosis.
                            // It is neither a grant nor valid active history.
                            continue;
                        }

                        // The fleet write may have failed after a durable STOP. History
                        // must not label that binding active while the principal refuses it.
                        let actor = self
                            .delegation_member(&row.principal_agent_id)
                            .map_err(unavailable)?;
                        let state = actor.agent.delegation(DelegationCommand::Inspect(Some(
                            row.delegation_id.clone(),
                        )))?;
                        match state.prior {
                            Some((Transition::Stop { reason, .. }, at)) => {
                                let mut writes = vec![];
                                row.stopped(self.store.as_ref(), at, &reason, &mut writes)
                                    .map_err(unavailable)?;
                                self.store.put_together(&writes).map_err(unavailable)?;
                                self.delegation_desk.forget(&row);
                                row.stopped_at = Some(at);
                                row.stopped_reason = reason;
                            }
                            Some((Transition::Start { binding }, _))
                                if state.active.as_ref() == Some(&binding) => {}
                            None => {
                                self.remove_abandoned_credential(&row.delegation_id)
                                    .map_err(unavailable)?;
                                row.abandon(self.store.as_mut()).map_err(unavailable)?;
                                self.delegation_desk.forget(&row);
                                continue;
                            }
                            Some(_) => return Err(unavailable("inconsistent delegation history")),
                        }
                    }
                    shown.push(self.public_delegation(&row)?);
                }
                Ok(json!({"delegations":shown}))
            }
        }
    }
    fn enable_delegation(&mut self, p: &str, h: &str, reason: &str) -> Result<Value, ApiError> {
        let get = |key| {
            self.delegation_member(key)
                .map_err(|why| ApiError::new(StatusCode::NOT_FOUND, why))
        };
        let principal = get(p)?;
        let helper = get(h)?;
        let why = rules::enable(
            &participant(&principal.record),
            &participant(&helper.record),
            reason,
            200,
        )
        .map_err(conflict)?;
        if principal.record.archived_at_ms.is_some() {
            return Err(conflict("the principal is archived"));
        }
        helper.agent.delegation(DelegationCommand::Inspect(None))?;
        let baseline = principal
            .agent
            .delegation(DelegationCommand::Baseline(200))?
            .baseline;
        use ring::rand::{SecureRandom, SystemRandom};
        let mut bytes = [0; 32];
        SystemRandom::new()
            .fill(&mut bytes)
            .map_err(|_| unavailable("cannot create delegation credential"))?;
        let token = bytes.iter().map(|b| format!("{b:02x}")).collect::<String>();
        let id = format!("dg{}", crate::ids::hex(8));
        let row = Reservation {
            delegation_id: id.clone(),
            principal_agent_id: principal.record.agent_id.clone(),
            delegate_agent_id: helper.record.agent_id.clone(),
            token_hash: format!("{:x}", Sha256::digest(token.as_bytes())),
            created_at: self.now_ms(),
            created_reason: why.into(),
            message_through: baseline,
            stopped_at: None,
            stopped_reason: String::new(),
        };
        row.reserve(self.store.as_mut()).map_err(|s| {
            if s == "an active delegation already exists for this principal or delegate" {
                conflict(s)
            } else {
                unavailable(s)
            }
        })?;
        let path = self
            .works
            .config
            .data_dir
            .join("delegations")
            .join(format!("{id}.token"));
        if let Err(e) = credential(&path, &token) {
            row.abandon(self.store.as_mut()).map_err(unavailable)?;
            return Err(unavailable(e));
        }
        self.delegation_desk.expect(&row, true);
        let start = Transition::Start {
            binding: row.binding(),
        };
        let result = principal
            .agent
            .delegation_reply(DelegationCommand::Change(start));
        self.delegation_desk.expect(&row, false);
        match result? {
            Ok(state) if state.accepted => {}
            Ok(_) => {
                self.remove_abandoned_credential(&id).map_err(unavailable)?;
                row.abandon(self.store.as_mut()).map_err(unavailable)?;
                self.delegation_desk.forget(&row);
                return Err(conflict("the principal has another delegation"));
            }
            Err(error) => {
                // A lost mailbox answer does not prove the append failed.
                // Discard the reservation only after a successful log read.
                let state = principal
                    .agent
                    .delegation(DelegationCommand::Inspect(Some(id.clone())))?;
                if state.active.as_ref() != Some(&row.binding()) {
                    if state.prior.is_none() {
                        self.remove_abandoned_credential(&id).map_err(unavailable)?;
                        row.abandon(self.store.as_mut()).map_err(unavailable)?;
                        self.delegation_desk.forget(&row);
                    }
                    return Err(error.into());
                }
            }
        }
        let mut answer = self.public_delegation(&row)?;
        answer["baseline_message_revision"] = json!(baseline);
        answer["token_path"] = json!(path);
        answer["listener"] =
            json!({"unit":format!("clarp-bookkeeping-{id}"),"started":true,"error":""});
        Ok(answer)
    }
}

impl super::FleetHandle {
    pub(crate) async fn goal_delegation(&self, request: Request) -> Result<Value, ApiError> {
        self.ask(|answer| super::Ask::GoalDelegation { request, answer })
            .await?
    }
}

use std::collections::BTreeMap;
use tokio::sync::watch;

/// A snapshot used only to reconcile a log as it opens; it grants no right to
/// a caller. Admission always reads the actor's own fold.
#[derive(Clone)]
struct Expected {
    binding: Binding,
    baseline: i64,
    created: i64,
    starting: bool,
}

pub(crate) struct Desk {
    reservations: watch::Sender<BTreeMap<String, Expected>>,
    roster: watch::Sender<BTreeMap<String, (Option<String>, String)>>,
    fleet: std::sync::Weak<std::sync::mpsc::Sender<super::Ask>>,
}

#[derive(Clone)]
pub(crate) struct Rejoin {
    reservations: watch::Receiver<BTreeMap<String, Expected>>,
    roster: watch::Receiver<BTreeMap<String, (Option<String>, String)>>,
    fleet: std::sync::Weak<std::sync::mpsc::Sender<super::Ask>>,
}

/// Reconciliation is asynchronous. A discrepancy cannot grant authority or
/// hold up the principal’s ordinary work while the fleet repairs its index.
pub(super) struct Mismatch {
    principal: String,
    binding: Binding,
    state: MismatchState,
}
impl Mismatch {
    pub(super) fn id(&self) -> &str {
        &self.binding.delegation_id
    }
}
enum MismatchState {
    Missing,
    Stopped(String, i64),
    Inconsistent(String),
}

impl Rejoin {
    pub(crate) fn has_principal(&self, id: &str) -> bool {
        self.reservations.borrow().contains_key(id)
    }
    pub(crate) fn any(&self) -> bool {
        !self.reservations.borrow().is_empty()
    }

    pub(crate) fn reconcile(&self, actor: &mut clarp_actor::Actor, now: i64) {
        let principal = actor.profile().agent_id.clone();
        let expected = self.reservations.borrow().get(&principal).cloned();
        let notify = |binding: Binding, state: MismatchState| {
            if let Some(tx) = self.fleet.upgrade() {
                let _ = tx.send(super::Ask::DelegationMismatch(Mismatch {
                    principal: principal.clone(),
                    binding,
                    state,
                }));
            }
        };
        if let Some(active) = actor.active_delegation().cloned()
            && expected.as_ref().map(|row| &row.binding) != Some(&active)
        {
            let why = "delegation reservation missing".to_string();
            let state = match actor.close_unreserved(&active, &why, now) {
                Ok(()) => MismatchState::Stopped(why, now),
                Err(error) => MismatchState::Inconsistent(error.to_string()),
            };
            notify(active, state);
        }
        let Some(expect) = expected else { return };
        if actor.active_delegation() == Some(&expect.binding) || expect.starting {
            return;
        }
        let state = match actor.recorded_delegation(&expect.binding.delegation_id) {
            Ok(Some((Transition::Stop { binding, reason }, at))) if binding == expect.binding => {
                MismatchState::Stopped(reason, at)
            }
            Ok(None) => MismatchState::Missing,
            Ok(Some(_)) => MismatchState::Inconsistent(
                "delegation history disagrees with current log binding".into(),
            ),
            Err(e) => MismatchState::Inconsistent(e.to_string()),
        };
        notify(expect.binding, state);
    }

    pub(crate) fn descends_from(&self, agent: &str, ancestor: &str) -> bool {
        let roster = self.roster.borrow();
        let mut id = agent;
        let mut seen = std::collections::HashSet::new();
        while !id.is_empty() && seen.insert(id) {
            if id == ancestor {
                return true;
            }
            id = roster.get(id).and_then(|(p, _)| p.as_deref()).unwrap_or("");
        }
        false
    }
    pub(crate) fn backend(&self, id: &str) -> Option<String> {
        self.roster.borrow().get(id).map(|(_, b)| b.clone())
    }
}

impl Desk {
    pub(super) fn open(
        works: &super::Works,
        store: &mut dyn KeyValue,
        tx: &Arc<std::sync::mpsc::Sender<super::Ask>>,
    ) -> Result<Self, String> {
        let mut skipped = 0;
        let expected = recover(
            store,
            &works.config.data_dir,
            |principal| {
                works
                    .engine
                    .open(
                        &works.config.data_dir.join("agents").join(principal),
                        works.codecs.clone(),
                    )
                    .map_err(|e| e.to_string())
            },
            |id, why| {
                skipped += 1;
                works
                    .log
                    .line(&format!("clarp-host: delegation {id} skipped: {why}"));
            },
        )?;
        if skipped > 0 {
            works.log.line(&format!(
                "clarp-host: {skipped} reservations could not be reconciled"
            ));
        }
        Ok(Self {
            reservations: watch::channel(expected).0,
            roster: watch::channel(BTreeMap::new()).0,
            fleet: Arc::downgrade(tx),
        })
    }
    pub(super) fn for_worker(&self) -> Rejoin {
        Rejoin {
            reservations: self.reservations.subscribe(),
            roster: self.roster.subscribe(),
            fleet: self.fleet.clone(),
        }
    }
    pub(super) fn known_record(&self, r: &AgentRecord) {
        self.roster.send_modify(|known| {
            known.insert(
                r.agent_id.clone(),
                (r.parent_agent_id.clone(), r.backend.clone()),
            );
        });
    }
    fn expects(&self, row: &Reservation) -> bool {
        self.reservations
            .borrow()
            .get(&row.principal_agent_id)
            .is_some_and(|entry| entry.binding == row.binding())
    }
    fn expect(&self, row: &Reservation, starting: bool) {
        self.reservations.send_modify(|map| {
            map.insert(
                row.principal_agent_id.clone(),
                Expected {
                    binding: row.binding(),
                    baseline: row.message_through,
                    created: row.created_at,
                    starting,
                },
            );
        });
    }
    fn forget(&self, row: &Reservation) {
        self.reservations.send_modify(|map| {
            if map
                .get(&row.principal_agent_id)
                .is_some_and(|e| e.binding == row.binding())
            {
                map.remove(&row.principal_agent_id);
            }
        });
    }
}
impl Fleet {
    pub(super) fn reconcile_mismatch(&mut self, m: Mismatch) -> Result<(), String> {
        if !self
            .delegation_desk
            .reservations
            .borrow()
            .get(&m.principal)
            .is_some_and(|e| e.binding == m.binding)
        {
            // A reservation omitted during startup must stay untouched, even if
            // the actor subsequently succeeds in closing its orphan START.
            return match m.state {
                MismatchState::Inconsistent(why) => Err(why),
                _ => Ok(()),
            };
        }
        let Some(row) = find(self.store.as_ref(), &m.binding.delegation_id)? else {
            return Ok(());
        };
        if row.principal_agent_id != m.principal || row.binding() != m.binding {
            return Err("delegation reconciliation identity mismatch".into());
        }
        if row.stopped_at.is_none() {
            match m.state {
                MismatchState::Inconsistent(why) => return Err(why),
                MismatchState::Missing => {
                    self.remove_abandoned_credential(&row.delegation_id)?;
                    row.abandon(self.store.as_mut())?;
                }
                MismatchState::Stopped(reason, at) => {
                    let mut writes = vec![];
                    row.stopped(self.store.as_ref(), at, &reason, &mut writes)?;
                    self.store
                        .put_together(&writes)
                        .map_err(|e| e.to_string())?;
                }
            }
        }
        self.delegation_desk.forget(&row);
        Ok(())
    }
}

impl Fleet {
    fn current_for(&self, principal: &str) -> Result<Option<Reservation>, ApiError> {
        let Some(expected) = self
            .delegation_desk
            .reservations
            .borrow()
            .get(principal)
            .cloned()
        else {
            return Ok(None);
        };
        let actor = self.delegation_member(principal).map_err(unavailable)?;
        let state = actor.agent.delegation(DelegationCommand::Inspect(None))?;
        if state.active.as_ref() != Some(&expected.binding) {
            return Ok(None);
        }
        let row = find(self.store.as_ref(), &expected.binding.delegation_id)
            .map_err(unavailable)?
            .ok_or_else(|| unavailable("missing delegation reservation"))?;
        Ok(Some(row))
    }
    pub(super) fn has_delegated_ancestor(&self, start: &str) -> Result<bool, ApiError> {
        let mut key = match self.delegation_member(start) {
            Ok(member) => member.record.agent_id.clone(),
            Err(_) => return Ok(false),
        };
        let mut seen = std::collections::HashSet::new();
        while seen.insert(key.clone()) {
            let principal = self
                .delegation_desk
                .reservations
                .borrow()
                .iter()
                .find(|(_, e)| e.binding.delegate_agent_id == key)
                .map(|(p, _)| p.clone());
            if let Some(principal) = principal
                && self.current_for(&principal)?.is_some()
            {
                return Ok(true);
            }
            let Some(parent) = self.stored_agent(&key)?.and_then(|r| r.parent_agent_id) else {
                break;
            };
            key = parent;
        }
        Ok(false)
    }
}

pub(crate) async fn lineage(
    app: &crate::edge::App,
    claimed: &str,
    token: Option<&str>,
) -> Result<(), ApiError> {
    let caller = match token {
        Some(token) => crate::turn_identity::caller(app, token)
            .await?
            .map(|m| m.record.agent_id.clone()),
        None => None,
    };
    for name in caller
        .as_deref()
        .into_iter()
        .chain((!claimed.is_empty()).then_some(claimed))
    {
        if app
            .fleet
            .goal_delegation(Request::Descendant(name.into()))
            .await?
            == true
        {
            return Err(conflict(
                "a bookkeeping delegate does not start helpers or forks",
            ));
        }
    }
    Ok(())
}

fn remove_credential(root: &Path, id: &str) -> Result<(), String> {
    // Only Host-issued credential names can address the private file.
    if id.len() != 18 || !id.starts_with("dg") || !id[2..].bytes().all(|b| b.is_ascii_hexdigit()) {
        return Err("invalid delegation id in reservation".into());
    }
    match fs::remove_file(root.join("delegations").join(format!("{id}.token"))) {
        Ok(()) => Ok(()),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(e) => Err(e.to_string()),
    }
}

fn recover(
    store: &mut dyn KeyValue,
    root: &Path,
    mut open_log: impl FnMut(&str) -> Result<Box<dyn clarp_ports::log::AgentLog>, String>,
    mut skipped: impl FnMut(&str, &str),
) -> Result<BTreeMap<String, Expected>, String> {
    let mut expected = BTreeMap::new();
    for (principal, bytes) in store.list(PRINCIPALS).map_err(|e| e.to_string())? {
        let id = match std::str::from_utf8(&bytes) {
            Ok(id) => id,
            Err(_) => {
                let label = format!(
                    "invalid delegation ID (hex {:02x?}) for {principal}",
                    &bytes[..bytes.len().min(32)]
                );
                skipped(&label, "non-UTF-8 reservation");
                continue;
            }
        };
        let result = (|| -> Result<Option<Expected>, String> {
            let row = find(store, id)?.ok_or("missing principal reservation")?;
            if row.principal_agent_id != principal || row.delegation_id != id {
                return Err("misplaced principal reservation".into());
            }
            if store
                .get(DELEGATES, &row.delegate_agent_id)
                .map_err(|e| e.to_string())?
                .as_deref()
                != Some(id.as_bytes())
            {
                return Err("missing or mismatched delegate role key".into());
            }
            let log = open_log(&principal)?;
            match clarp_actor::delegation::recorded(log.as_ref(), id).map_err(|e| e.to_string())? {
                None => {
                    remove_credential(root, id)?;
                    row.abandon(store)?;
                    Ok(None)
                }
                Some((Transition::Stop { binding, reason }, at)) if binding == row.binding() => {
                    let mut writes = vec![];
                    row.stopped(store, at, &reason, &mut writes)?;
                    store.put_together(&writes).map_err(|e| e.to_string())?;
                    Ok(None)
                }
                Some((Transition::Start { binding }, _))
                    if binding == row.binding()
                        && row.stopped_at.is_none()
                        && clarp_actor::delegation::active(log.as_ref())
                            .map_err(|e| e.to_string())?
                            .as_ref()
                            == Some(&binding) =>
                {
                    Ok(Some(Expected {
                        binding,
                        baseline: row.message_through,
                        created: row.created_at,
                        starting: false,
                    }))
                }
                Some(_) => Err("delegation reservation disagrees with its log".into()),
            }
        })();
        match result {
            Ok(Some(entry)) => {
                expected.insert(principal, entry);
            }
            Ok(None) => {}
            Err(why) => skipped(id, &why),
        }
    }
    Ok(expected)
}

#[cfg(test)]
#[allow(clippy::unwrap_used)]
mod tests {
    use super::*;
    use clarp_ports::{kv::KeyValueEngine, log::LogEngine};
    use clarp_store_mem::{MemEngine, MemKeyValue};

    fn reservation(id: &str, principal: &str, helper: &str) -> Reservation {
        Reservation {
            delegation_id: id.into(),
            principal_agent_id: principal.into(),
            delegate_agent_id: helper.into(),
            token_hash: "fixture".into(),
            created_at: 10,
            created_reason: "fixture".into(),
            message_through: 2,
            stopped_at: None,
            stopped_reason: String::new(),
        }
    }

    #[test]
    fn each_cut_in_the_two_store_protocol_recovers_the_principals_committed_answer() {
        let id = "dg0000000000000001";
        for cut in 1..=4 {
            let root = std::env::temp_dir().join(format!("clarp-books-{}", crate::ids::hex(6)));
            let logs = MemEngine::new();
            let keys = MemKeyValue::new();
            let mut store = keys.open(&root).unwrap();
            let row = reservation(id, "a", "b");
            let log = || {
                logs.open(&root.join("a"), clarp_codec::standard_set("none").unwrap())
                    .unwrap()
            };
            let profile = clarp_actor::Profile {
                agent_id: "a".into(),
                session: "one".into(),
                conversation_id: "c".into(),
                dir: root.join("a"),
                run_dir: root.join("run"),
                cwd: root.clone(),
                model: None,
                effort: None,
                told: None,
                tool_env: vec![],
                stop_grace_ms: 1000,
                keep_spools: false,
                accounts: clarp_kernel::recovery::Accounts::None,
                epoch: "test".into(),
                hides_coordination: false,
            };
            let open = || {
                clarp_actor::Actor::open(
                    profile.clone(),
                    Arc::new(clarp_backend::scripted::Scripted::echo()),
                    log(),
                    10,
                )
                .unwrap()
                .0
            };
            let mut actor = open();
            row.reserve(store.as_mut()).unwrap();
            let token = root.join("delegations").join(format!("{id}.token"));
            credential(&token, "synthetic").unwrap();
            if cut >= 2 {
                assert!(
                    actor
                        .change_delegation(
                            Transition::Start {
                                binding: row.binding()
                            },
                            11
                        )
                        .unwrap()
                );
            }
            if cut >= 3 {
                assert!(
                    actor
                        .change_delegation(
                            Transition::Stop {
                                binding: row.binding(),
                                reason: "finished".into()
                            },
                            12
                        )
                        .unwrap()
                );
            }
            if cut == 4 {
                let mut writes = vec![];
                row.stopped(store.as_ref(), 12, "finished", &mut writes)
                    .unwrap();
                store.put_together(&writes).unwrap();
            }
            drop(actor);
            drop(store);
            let mut store = keys.open(&root).unwrap();
            let recovered = recover(
                store.as_mut(),
                &root,
                |principal| {
                    assert_eq!(principal, "a");
                    Ok(log())
                },
                |id, why| panic!("{id}: {why}"),
            )
            .unwrap();
            let actor = open();
            assert_eq!(actor.admits_delegate(id, Some("b")), cut == 2);
            assert_eq!(recovered.contains_key("a"), cut == 2);
            assert_eq!(
                active_for_agent(store.as_ref(), "a").unwrap().len(),
                usize::from(cut == 2)
            );
            assert_eq!(
                active_for_agent(store.as_ref(), "b").unwrap().len(),
                usize::from(cut == 2)
            );
            assert_eq!(token.exists(), cut != 1);
            assert_eq!(find(store.as_ref(), id).unwrap().is_some(), cut != 1);
            for who in ["a", "b"] {
                let history = for_agent(store.as_ref(), who).unwrap();
                assert_eq!(history.len(), usize::from(cut != 1));
                if let Some(r) = history.first() {
                    assert_eq!(r.stopped_at, (cut >= 3).then_some(12));
                    assert_eq!(r.stopped_reason, if cut >= 3 { "finished" } else { "" });
                }
            }
            if cut == 2 {
                // A worker opening with START but without the matching fleet reservation
                // must stop it before serving a command.
                let desk = Desk {
                    reservations: watch::channel(BTreeMap::new()).0,
                    roster: watch::channel(BTreeMap::new()).0,
                    fleet: std::sync::Weak::new(),
                };
                let mut actor = actor;
                desk.for_worker().reconcile(&mut actor, 15);
                assert!(!actor.admits_delegate(id, Some("b")));
                assert!(
                    matches!(actor.delegation_transition(id).unwrap(),Some(Transition::Stop{reason,..}) if reason=="delegation reservation missing")
                );
                drop(actor);
                assert!(!open().admits_delegate(id, Some("b")));
            } else {
                drop(actor);
            }
            std::fs::remove_dir_all(root).unwrap();
        }
    }

    #[test]
    fn the_fleets_two_active_role_keys_are_independent() {
        let mut store = MemKeyValue::new()
            .open(Path::new("delegation-roles"))
            .unwrap();
        reservation("one", "a", "b")
            .reserve(store.as_mut())
            .unwrap();
        assert!(
            reservation("two", "a", "c")
                .reserve(store.as_mut())
                .is_err()
        );
        assert!(
            reservation("two", "c", "b")
                .reserve(store.as_mut())
                .is_err()
        );
        assert!(find(store.as_ref(), "two").unwrap().is_none());
        reservation("second", "b", "c")
            .reserve(store.as_mut())
            .unwrap();
        assert_eq!(active_for_agent(store.as_ref(), "b").unwrap().len(), 2);
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used)]
mod port_tests;
