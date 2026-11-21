//! The agent log's native goal mirror and lane ownership fence.

use clarp_kernel::agent_goal::Goal;
use clarp_kernel::journal::kind;
use serde::{Deserialize, Serialize};

use super::{Actor, ActorError, record_item};

const KEY: &str = "native-codex-goal";

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct NativeGoal {
    pub thread: String,
    pub goal: Option<Goal>,
    pub lane: Ownership,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Ownership {
    /// The lane may own the provider thread even if its activation reply was lost.
    Acquiring,
    Owned,
    /// A Stop's pause may not have landed. A reconnect must fence then pause.
    Stopping,
    /// The account pool paused execution. Its next allowed claim can resume the native goal.
    AccountHold,
    /// A provider continuation tried to run before the actor could grant a claim.
    SchedulerHold,
    /// The provider child has exited and released the thread.
    Exec,
}

impl Actor {
    pub fn native_goal(&self) -> Result<Option<NativeGoal>, ActorError> {
        let Some(item) = self.recorder.log().find(KEY)? else {
            return Ok(None);
        };
        serde_json::from_slice(&item.body).map_err(|error| {
            ActorError::Corrupt(format!(
                "invalid native goal record at {}: {error}",
                item.seq
            ))
        })
    }

    /// Commit ownership before asking the provider to start using a thread.
    /// Commit a new public goal before sending the event that advertises it.
    pub fn keep_native_goal(&mut self, goal: &NativeGoal, now_ms: i64) -> Result<(), ActorError> {
        let record = item(goal, now_ms)?;
        let mut batch = self.recorder.batch();
        batch.item(record);
        batch.commit()?;
        Ok(())
    }
}

/// Also used by admission, which records the lane fence in the same append
/// as its first reserved native turn.
pub(super) fn item(
    goal: &NativeGoal,
    now_ms: i64,
) -> Result<clarp_ports::log::NewItem, ActorError> {
    record_item(kind::NATIVE_GOAL, KEY.into(), goal, now_ms)
}
