//! The team side effects owed by a durable reply or by proven delivery of a prompt.
use serde::{Deserialize, Serialize};

use super::{Batch, JournalError, kind};
use crate::{
    agent::Command,
    turn::{Turn, TurnId},
};

#[derive(Debug, Serialize, Deserialize)]
pub enum Effect {
    LeaderNoop,
    Reply {
        id: String,
        trace: String,
        blocks: Vec<String>,
    },
    Injected {
        ids: Vec<String>,
    },
}

pub fn launch_key(turn: TurnId, attempt: u32) -> String {
    format!("team-inbox-{}-{attempt}", turn.0)
}

pub fn followup_key(turn: TurnId) -> String {
    format!("team-followup-{}", turn.0)
}

impl Batch<'_> {
    pub(super) fn team_prompt_read(&mut self, key: &str, now: i64) {
        let effect = self
            .recorder
            .log
            .find(key)
            .map_err(JournalError::from)
            .and_then(|item| {
                let Some(item) = item.filter(|item| item.kind == kind::TEAM_PROMPT) else {
                    return Ok(None);
                };
                let ids: Vec<String> =
                    serde_json::from_slice(&item.body).map_err(|error| JournalError::Corrupt {
                        seq: item.seq,
                        why: error.to_string(),
                    })?;
                let effect = serde_json::to_vec(&Effect::Injected { ids })
                    .map_err(|error| JournalError::Encode(error.to_string()))?;
                Ok(Some(effect))
            });
        match effect {
            Ok(Some(body)) => {
                self.item(clarp_ports::log::NewItem::new(kind::TEAM_EFFECT, now, body));
            }
            Ok(None) => {}
            Err(why) => {
                self.failed.get_or_insert(why);
            }
        }
    }

    pub(super) fn team_prompt_key(&self, command: &Command) -> Option<String> {
        // Only real provider input evidence reaches these edges. Startup recovery of a
        // steering echo uses the same JoinRead command as an echo read in the live spool.
        match command {
            Command::TurnStarted { turn } => match self.work.live() {
                Some(Turn::Starting(attempt)) if attempt.id == *turn => {
                    Some(launch_key(*turn, attempt.attempt))
                }
                _ => None,
            },
            Command::JoinRead { turn } => Some(followup_key(*turn)),
            _ => None,
        }
    }
}
