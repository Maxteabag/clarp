//! Admission within a Codex lane. Only an actor's reserved slot can own a spool; the spool is
//! admitted as every lane admits one (`lane_spool`).

use std::io;

use serde_json::Value;

use clarp_shim::codex_lane::{Claim, Reservation};
use clarp_shim::codex_turn::TurnSpool;

use crate::lane_spool::ClaimSpool;

/// The lifecycle of the single live turn on a native thread.
#[derive(Default)]
pub(crate) enum Slot {
    #[default]
    Free,
    Reserved {
        binding: Reservation,
        writer: ClaimSpool,
    },
    Running {
        binding: Reservation,
        spool: TurnSpool,
    },
}

impl Slot {
    /// Create the attempt's spool before any write that can hand work to the
    /// provider. A recovered actor can follow that claim instead of relaunching.
    pub(crate) fn reserve(&mut self, binding: Reservation) -> io::Result<()> {
        if !matches!(self, Self::Free) {
            return Err(io::Error::other("the Codex lane is already reserved"));
        }
        let writer = ClaimSpool::admit(binding.claim())?;
        *self = Self::Reserved { binding, writer };
        Ok(())
    }

    pub(crate) fn is_goal_reserved(&self) -> bool {
        matches!(
            self,
            Self::Reserved {
                binding: Reservation::Goal(_),
                ..
            }
        )
    }

    pub(crate) fn claim(&self) -> Option<&Claim> {
        match self {
            Self::Free => None,
            Self::Reserved { binding, .. } | Self::Running { binding, .. } => Some(binding.claim()),
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
        started: bool,
    ) -> io::Result<bool> {
        let (binding, writer) = match std::mem::take(self) {
            Self::Reserved { binding, writer } => (binding, writer),
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
                let spool = TurnSpool::bind(
                    writer.into_writer(),
                    thread.to_owned(),
                    native_turn.to_owned(),
                    pid,
                    now_ms,
                    started,
                )?;
                *self = Self::Running { binding, spool };
                Ok(true)
            }
            Err(held) => {
                *self = Self::Reserved {
                    binding: held,
                    writer,
                };
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

    /// Codex accepted steered words for the running turn.
    pub(crate) fn steered(&mut self, id: &str) {
        if let Self::Running { spool, .. } = self {
            spool.steered(id);
        }
    }

    /// The accepted steered ids of the running turn that Codex has not said it read.
    pub(crate) fn unread(&self) -> Vec<String> {
        match self {
            Self::Running { spool, .. } => spool.unread(),
            _ => Vec::new(),
        }
    }

    pub(crate) fn receipt(&mut self, id: &str, now_ms: i64) -> io::Result<()> {
        match self {
            Self::Running { spool, .. } => spool.receipt(id, now_ms),
            _ => Ok(()),
        }
    }

    /// Codex was ended where it stood after it did not answer a Stop.
    pub(crate) fn killed(&mut self, now_ms: i64) -> io::Result<()> {
        match std::mem::take(self) {
            Self::Free => Ok(()),
            Self::Running { mut spool, .. } => spool.killed(now_ms),
            Self::Reserved { writer, .. } => writer.end(None, Some(nix::libc::SIGKILL), now_ms),
        }
    }

    /// A reservation that never bound a provider turn still settles once.
    pub(crate) fn cancel(&mut self, now_ms: i64) -> io::Result<()> {
        if let Self::Reserved { writer, .. } = std::mem::take(self) {
            writer.end(None, Some(nix::libc::SIGINT), now_ms)?;
        }
        Ok(())
    }

    /// Codex ran on another provider than the lane's: the claimed turn, bound or not yet,
    /// fails with these words, once.
    pub(crate) fn refused(&mut self, why: &str, now_ms: i64) -> io::Result<()> {
        match std::mem::take(self) {
            Self::Free => Ok(()),
            Self::Running { mut spool, .. } => spool.refused(why, now_ms),
            Self::Reserved { mut writer, .. } => {
                let failed = serde_json::json!({"type": "turn.failed", "error": why});
                writer.out(failed.to_string(), now_ms)?;
                writer.end(Some(1), None, now_ms)
            }
        }
    }

    pub(crate) fn lost(&mut self, now_ms: i64) -> io::Result<()> {
        match std::mem::take(self) {
            Self::Free => Ok(()),
            Self::Running { mut spool, .. } => spool.lost(now_ms),
            Self::Reserved { writer, .. } => writer.end(Some(1), None, now_ms),
        }
    }
}
