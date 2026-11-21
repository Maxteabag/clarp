//! Admission within a Codex lane. Only an actor's reserved slot can own a spool.

use std::io;

use serde_json::Value;

use clarp_shim::codex_lane::{Claim, Reservation};
use clarp_shim::codex_turn::TurnSpool;

/// The lifecycle of the single live turn on a native thread.
#[derive(Default)]
pub(crate) enum Slot {
    #[default]
    Free,
    Reserved(Reservation),
    Running {
        binding: Reservation,
        spool: TurnSpool,
    },
}

impl Slot {
    /// Exactly one claim can be held. Reconnecting with that same claim is harmless.
    pub(crate) fn reserve_goal(&mut self, claim: Claim) -> bool {
        match self {
            Self::Free => {
                *self = Self::Reserved(Reservation::Goal(claim));
                true
            }
            Self::Reserved(Reservation::Goal(held)) => *held == claim,
            _ => false,
        }
    }

    pub(crate) fn reserve_prompt(&mut self, claim: Claim, request: u64) -> bool {
        match self {
            Self::Free => {
                *self = Self::Reserved(Reservation::PromptPending { claim, request });
                true
            }
            _ => false,
        }
    }

    pub(crate) fn is_goal_reserved(&self) -> bool {
        matches!(self, Self::Reserved(Reservation::Goal(_)))
    }

    pub(crate) fn claim(&self) -> Option<&Claim> {
        match self {
            Self::Free => None,
            Self::Reserved(binding) | Self::Running { binding, .. } => Some(binding.claim()),
        }
    }

    pub(crate) fn native_turn(&self) -> Option<&str> {
        match self {
            Self::Running { binding, .. } => binding.native_turn(),
            _ => None,
        }
    }

    /// The solicited reply can arrive after its first notifications. The caller
    /// keeps those until this binding has been established.
    pub(crate) fn bind(
        &mut self,
        reply: Option<u64>,
        thread: &str,
        native_turn: &str,
        pid: u32,
        now_ms: i64,
    ) -> io::Result<bool> {
        let binding = match std::mem::take(self) {
            Self::Reserved(binding) => binding,
            other => {
                *self = other;
                return Ok(false);
            }
        };
        let bound = match reply {
            Some(id) => binding.bind_prompt(id, native_turn.to_owned()),
            None => binding.bind_goal(native_turn.to_owned()),
        };
        match bound {
            Ok(binding) => {
                let spool = TurnSpool::begin(
                    &binding.claim().spool,
                    thread.to_owned(),
                    native_turn.to_owned(),
                    pid,
                    now_ms,
                )?;
                *self = Self::Running { binding, spool };
                Ok(true)
            }
            Err(held) => {
                *self = Self::Reserved(held);
                Ok(false)
            }
        }
    }

    pub(crate) fn notification(
        &mut self,
        method: &str,
        params: &Value,
        now_ms: i64,
    ) -> io::Result<bool> {
        if let Self::Running { spool, .. } = self
            && spool.notification(method, params, now_ms)?
        {
            *self = Self::Free;
            return Ok(true);
        }
        Ok(false)
    }

    pub(crate) fn lost(&mut self, now_ms: i64) -> io::Result<()> {
        if let Self::Running { mut spool, .. } = std::mem::take(self) {
            spool.lost(now_ms)?;
        }
        Ok(())
    }
}
