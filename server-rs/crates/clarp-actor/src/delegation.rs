//! The principal's log decides which bookkeeping delegate may act. A single
//! actor orders these transitions with its goal writes and observer reads.
use super::{Actor, ActorError, record_item};
use clarp_kernel::journal::kind;
use clarp_ports::log::AgentLog;
use serde::{Deserialize, Serialize};

const CURRENT: &str = "ledger/delegation/current";

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Binding {
    pub delegation_id: String,
    pub delegate_agent_id: String,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "transition", rename_all = "snake_case")]
pub enum Transition {
    Start { binding: Binding },
    Stop { binding: Binding, reason: String },
}

impl Transition {
    fn binding(&self) -> &Binding {
        match self {
            Self::Start { binding } | Self::Stop { binding, .. } => binding,
        }
    }
}

fn read(log: &dyn AgentLog, key: &str) -> Result<Option<Transition>, ActorError> {
    log.find(key)?
        .map(|entry| {
            serde_json::from_slice(&entry.body)
                .map_err(|e| ActorError::Corrupt(format!("invalid delegation transition: {e}")))
        })
        .transpose()
}

pub fn active(log: &dyn AgentLog) -> Result<Option<Binding>, ActorError> {
    Ok(match read(log, CURRENT)? {
        Some(Transition::Start { binding }) => Some(binding),
        _ => None,
    })
}

impl Actor {
    pub fn delegation_baseline(&self, offset: usize) -> Result<i64, ActorError> {
        super::conversation::delegation_baseline(self.recorder.log(), self.drafts(), offset)
    }
    /// Even if the STOP cannot be written, an unreserved grant is unusable
    /// during this opening. A later open tries again from its durable START.
    pub fn close_unreserved(
        &mut self,
        binding: &Binding,
        why: &str,
        now: i64,
    ) -> Result<(), ActorError> {
        let result = self.change_delegation(
            Transition::Stop {
                binding: binding.clone(),
                reason: why.into(),
            },
            now,
        );
        self.delegation = None;
        match result {
            Ok(true) => Ok(()),
            Ok(false) => Err(ActorError::Corrupt("unexpected orphan binding".into())),
            Err(e) => Err(e),
        }
    }

    pub fn active_delegation(&self) -> Option<&Binding> {
        self.delegation.as_ref()
    }

    /// The fleet distinguishes a reservation without a START from retained
    /// history whose STOP committed before the fleet could release its keys.
    pub fn delegation_transition(&self, id: &str) -> Result<Option<Transition>, ActorError> {
        read(
            self.recorder.log(),
            &format!("ledger/delegation/by-id/{id}"),
        )
    }

    /// Append the transition with both keyed views of it. Nothing changes in
    /// the fold until the append succeeds. Ok(false) means a different active
    /// delegation, a reused id, or a STOP for a binding this log never held.
    pub fn change_delegation(
        &mut self,
        transition: Transition,
        now: i64,
    ) -> Result<bool, ActorError> {
        let binding = transition.binding();
        let prior = self.delegation_transition(&binding.delegation_id)?;
        let next = match &transition {
            Transition::Start { .. } if self.delegation.as_ref() == Some(binding) => {
                return Ok(true);
            }
            Transition::Start { .. } if self.delegation.is_none() && prior.is_none() => {
                Some(binding.clone())
            }
            Transition::Stop { .. } if self.delegation.as_ref() == Some(binding) => None,
            Transition::Stop { .. } if matches!(prior, Some(Transition::Stop { binding: ref old, .. }) if old == binding) =>
            {
                return Ok(true);
            }
            _ => return Ok(false),
        };
        let key = format!("ledger/delegation/by-id/{}", binding.delegation_id);
        let mut batch = self.recorder.batch();
        for key in [CURRENT.to_string(), key] {
            batch.item(record_item(kind::GOAL_LEDGER, key, &transition, now)?);
        }
        batch.commit()?;
        self.delegation = next;
        Ok(true)
    }

    /// Called inside the same actor operation as the admitted read or append.
    pub fn admits_delegate(&self, id: &str, verified_caller: Option<&str>) -> bool {
        self.delegation.as_ref().is_some_and(|active| {
            active.delegation_id == id && verified_caller == Some(active.delegate_agent_id.as_str())
        })
    }
}

/// A read-only recovery query used before any actor opens this log. Only the
/// principal actor can append a transition.
pub fn recorded(log: &dyn AgentLog, id: &str) -> Result<Option<(Transition, i64)>, ActorError> {
    let key = format!("ledger/delegation/by-id/{id}");
    log.find(&key)?
        .map(|item| {
            let transition = serde_json::from_slice(&item.body)
                .map_err(|e| ActorError::Corrupt(format!("invalid delegation transition: {e}")))?;
            Ok((transition, item.ts_ms))
        })
        .transpose()
}

impl Actor {
    pub fn recorded_delegation(&self, id: &str) -> Result<Option<(Transition, i64)>, ActorError> {
        recorded(self.recorder.log(), id)
    }
}

impl Actor {
    /// Run an authorized operation in this mailbox turn. The caller cannot
    /// carry the permission to a later turn across a STOP.
    pub fn as_delegate<T>(
        &mut self,
        id: &str,
        caller: Option<&str>,
        run: impl FnOnce(&mut Self) -> Result<T, ActorError>,
    ) -> Result<Option<T>, ActorError> {
        if !self.admits_delegate(id, caller) {
            return Ok(None);
        }
        run(self).map(Some)
    }
}
