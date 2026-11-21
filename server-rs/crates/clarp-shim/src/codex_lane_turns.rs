//! Admission within a Codex lane. Only an actor's reserved slot can own a spool.

use std::io;

use clarp_spool::{Entry, Record, ShimEvent, SpoolWriter};
use serde_json::Value;

use clarp_shim::codex_lane::{Claim, Reservation};
use clarp_shim::codex_turn::TurnSpool;

/// The lifecycle of the single live turn on a native thread.
#[derive(Default)]
pub(crate) enum Slot {
    #[default]
    Free,
    Reserved {
        binding: Reservation,
        writer: SpoolWriter,
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
        let mut writer = SpoolWriter::create(&binding.claim().spool)?;
        writer.sync()?;
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
                    writer,
                    thread.to_owned(),
                    native_turn.to_owned(),
                    pid,
                    now_ms,
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

    /// A reservation that never bound a provider turn still settles once.
    pub(crate) fn cancel(&mut self, now_ms: i64) -> io::Result<()> {
        if let Self::Reserved { mut writer, .. } = std::mem::take(self) {
            end_unbound(&mut writer, None, Some(nix::libc::SIGINT), now_ms)?;
        }
        Ok(())
    }

    pub(crate) fn lost(&mut self, now_ms: i64) -> io::Result<()> {
        match std::mem::take(self) {
            Self::Free => Ok(()),
            Self::Running { mut spool, .. } => spool.lost(now_ms),
            Self::Reserved { mut writer, .. } => end_unbound(&mut writer, Some(1), None, now_ms),
        }
    }
}

fn end_unbound(
    writer: &mut SpoolWriter,
    code: Option<i32>,
    signal: Option<i32>,
    now_ms: i64,
) -> io::Result<()> {
    writer.append(&Record {
        t: now_ms,
        entry: Entry::Shim(ShimEvent::Exited { code, signal }),
    })?;
    writer.sync()
}
