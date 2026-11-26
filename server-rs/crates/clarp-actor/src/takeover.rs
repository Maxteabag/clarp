//! The actor's side of takeover. The launcher only presents observations from the shim.

use std::path::PathBuf;

use clarp_kernel::agent::{Command, TransitCheckpoint};
use clarp_kernel::takeover::{self, ClaudeCheckpointProgress, Input, Selection, Start};
use clarp_kernel::turn::{Turn, TurnId};
use clarp_spool::{Entry, ShimEvent, SpoolReader};

use super::{Actor, ActorError, Changed, Facts, Outputs, closing, written};

/// The live attempt and the waiting turn selected before the launcher asks for a hold.
pub struct Offer {
    pub from: TurnId,
    pub into: TurnId,
    pub attempt: u32,
    pub socket: PathBuf,
    pub session: Option<String>,
    pub first: String,
    pub first_prompt_uuid: Option<String>,
    /// A first turn in a fresh conversation may have no provider file yet.
    pub first_turn: bool,
}

/// The active attempt that may be checkpointed once the shim proves its hold fence.
#[derive(Clone)]
pub struct TransitOffer {
    pub turn: TurnId,
    pub attempt: u32,
    pub socket: PathBuf,
    pub session: Option<String>,
    pub prompt_uuid: String,
    pub first_turn: bool,
}

impl Actor {
    pub fn transit_checkpoint_offer(&self) -> Option<TransitOffer> {
        let live = self.live.as_ref()?;
        let agent = self.recorder.agent();
        if !self.recorder.transit_held()
            || agent.expected_transit_checkpoint().is_some()
            || agent.selected_takeover().is_some()
            || !agent.joins().is_empty()
            || !agent.gathered(live.turn).is_empty()
            || live.steering.input_skipped
            || self.fork_runs()
            || live.in_call
            || live.opening
            || live.origin == clarp_kernel::turn::Origin::NativeGoal
            || matches!(&live.origin, clarp_kernel::turn::Origin::Trigger { name } if name == clarp_kernel::turn::COMPACTION)
            || live.over
            || !live.drafts.is_empty()
            || !live.tools.is_empty()
            || !live.calls.is_empty()
            || self.backend.id() != "claude"
        {
            return None;
        }
        Some(TransitOffer {
            turn: live.turn,
            attempt: live.attempt,
            socket: self.profile.socket_path_of(live.turn, live.attempt),
            session: agent.backend_session().map(str::to_owned),
            prompt_uuid: live.steering.prompt_uuid()?.to_owned(),
            first_turn: agent.fresh_first_turn(),
        })
    }

    /// The held spool must be complete and contain no provider work or tool side effects.
    pub fn expect_transit_checkpoint(
        &mut self,
        offer: &TransitOffer,
        fence: u64,
        native: &ClaudeCheckpointProgress,
        now_ms: i64,
    ) -> Result<bool, ActorError> {
        if self
            .transit_checkpoint_offer()
            .is_none_or(|current| (current.turn, current.attempt) != (offer.turn, offer.attempt))
        {
            return Ok(false);
        }
        let recorded_work = native.has_work();
        let safe = if recorded_work {
            self.recorded_transit_spool(offer, fence, native)?
        } else {
            self.quiet_spool(offer.turn, offer.attempt, fence)?
        };
        if !safe {
            return Ok(false);
        }
        let checkpoint = TransitCheckpoint {
            turn: offer.turn,
            attempt: offer.attempt,
            fence,
        };
        match self.recorder.handle(
            Command::ExpectTransitCheckpoint {
                checkpoint,
                recorded_work,
            },
            now_ms,
        ) {
            Ok(_) => Ok(true),
            Err(clarp_kernel::journal::JournalError::Refused(_)) => Ok(false),
            Err(error) => Err(error.into()),
        }
    }

    /// The completed results must agree with the native log and must already be journaled.
    /// A behind actor, a half-written frame or a tool that may still make a change defers.
    fn recorded_transit_spool(
        &self,
        offer: &TransitOffer,
        fence: u64,
        native: &ClaudeCheckpointProgress,
    ) -> Result<bool, ActorError> {
        if fence > 4 * 1024 * 1024
            || self.live.as_ref().is_none_or(|live| live.offset != fence)
            || self.recorder.spool_offset(offer.turn) != fence
        {
            return Ok(false);
        }
        let path = self.profile.spool_path_of(offer.turn, offer.attempt);
        let read = SpoolReader::new(&path)
            .read_window(0, fence, usize::MAX)
            .map_err(|error| super::io_error(&path, error))?;
        if read.next_offset != fence || !read.damaged.is_empty() {
            return Ok(false);
        }
        let mut progress = ClaudeCheckpointProgress::default();
        let mut spawned = false;
        for record in &read.records {
            match &record.entry {
                Entry::Shim(ShimEvent::Session { .. } | ShimEvent::InputOpen { .. }) => {}
                Entry::Shim(ShimEvent::Spawned { .. }) => spawned = true,
                Entry::Out(line) if line.trim().is_empty() => {}
                Entry::Out(line) => {
                    let Ok(event) = serde_json::from_str(line) else {
                        return Ok(false);
                    };
                    if progress.record(&event).is_none() {
                        return Ok(false);
                    }
                }
                Entry::Hook { name, data } if native.matches_hook(name, data) => {}
                _ => return Ok(false),
            }
        }
        Ok(spawned && progress.finished().as_ref() == Some(native))
    }

    /// A durable intent alone cannot resume a turn. Require the exact checkpoint followed by
    /// proof that the source process is gone, regardless of the earlier socket response.
    pub(super) fn recover_transit_checkpoint(&mut self, now_ms: i64) -> Result<bool, ActorError> {
        let Some(checkpoint) = self.recorder.agent().expected_transit_checkpoint() else {
            return Ok(false);
        };
        let spool = self
            .profile
            .spool_path_of(checkpoint.turn, checkpoint.attempt);
        let read = SpoolReader::new(&spool)
            .read_window(checkpoint.fence, u64::MAX, 3)
            .map_err(|error| super::io_error(&spool, error))?;
        if !read.damaged.is_empty()
            || !matches!(read.records.as_slice(), [marker, exit]
                if marker.entry == Entry::Shim(ShimEvent::TransitCheckpoint { fence: checkpoint.fence })
                    && matches!(exit.entry, Entry::Shim(ShimEvent::Exited { signal: Some(9), code: None })))
        {
            return Ok(false);
        }
        let mut batch = self.recorder.batch();
        if batch
            .command(Command::ParkTransitCheckpoint { checkpoint }, now_ms)
            .is_err()
        {
            return Ok(false);
        }
        batch.cursor(checkpoint.turn, read.next_offset, now_ms);
        batch.commit()?;
        if let Some(live) = self.live.as_mut() {
            live.over = true;
        }
        Ok(true)
    }

    /// Only the head of the normal user mailbox can take over, while the first turn has no
    /// stored or live output. The complete spool still has to be checked under a shim hold.
    pub fn takeover_offer(&self, into: TurnId) -> Result<Option<Offer>, ActorError> {
        let Some(live) = self.live.as_ref() else {
            return Ok(None);
        };
        let agent = self.recorder.agent();
        if self.transit_holds_queue()
            || !agent.can_take_over(live.turn, into, live.attempt)
            || agent.selected_takeover().is_some()
            || live.opening
            || live.steering.input_skipped
            || live.over
            || live.message_id != live.last_row
            || !live.drafts.is_empty()
            || !live.tools.is_empty()
            || !live.calls.is_empty()
        {
            return Ok(None);
        }
        let seq = match agent.live() {
            Some(Turn::Starting(turn)) => turn.message_seq,
            Some(Turn::Running(turn)) => turn.message_seq,
            _ => return Ok(None),
        };
        let first = self.message_at(seq)?;
        if first.sender.is_some() || first.text.trim().is_empty() {
            return Ok(None);
        }
        Ok(Some(Offer {
            from: live.turn,
            into,
            attempt: live.attempt,
            socket: self.profile.socket_path_of(live.turn, live.attempt),
            session: agent.backend_session().map(str::to_owned),
            first: first.text,
            first_prompt_uuid: live.steering.prompt_uuid().map(str::to_owned),
            first_turn: agent.fresh_first_turn(),
        }))
    }

    /// A caller may ask the shim to hold only after this intent is durable.
    pub fn request_takeover_hold(
        &mut self,
        into: TurnId,
        now_ms: i64,
    ) -> Result<Option<Offer>, ActorError> {
        let Some(offer) = self.takeover_offer(into)? else {
            return Ok(None);
        };
        self.recorder.handle(
            Command::OfferTakeover {
                from: offer.from,
                into: offer.into,
                attempt: offer.attempt,
            },
            now_ms,
        )?;
        Ok(Some(offer))
    }

    /// Recheck the running attempt through the shim's exact fence. A missing or damaged
    /// record is uncertain and leaves the follow-up in Part A's queue.
    pub fn takeover_quiet(&self, offer: &Offer, until: u64) -> Result<bool, ActorError> {
        if self
            .takeover_offer(offer.into)?
            .is_none_or(|current| (current.from, current.attempt) != (offer.from, offer.attempt))
        {
            return Ok(false);
        }
        self.quiet_spool(offer.from, offer.attempt, until)
    }

    fn quiet_spool(&self, turn: TurnId, attempt: u32, until: u64) -> Result<bool, ActorError> {
        if until > 4 * 1024 * 1024 || self.live.as_ref().is_none_or(|live| live.offset > until) {
            return Ok(false);
        }
        let path = self.profile.spool_path_of(turn, attempt);
        let read = SpoolReader::new(&path)
            .read_window(0, until, usize::MAX)
            .map_err(|error| super::io_error(&path, error))?;
        if read.next_offset != until || !read.damaged.is_empty() {
            return Ok(false);
        }

        let mut spawned = false;
        for record in read.records {
            match record.entry {
                Entry::Shim(ShimEvent::Session { .. } | ShimEvent::InputOpen { .. }) => {}
                Entry::Shim(ShimEvent::Spawned { .. }) => spawned = true,
                Entry::Out(line) if line.trim().is_empty() => {}
                Entry::Out(line) => {
                    let Ok(serde_json::Value::Object(fields)) = serde_json::from_str(&line) else {
                        return Ok(false);
                    };
                    let event = serde_json::Value::Object(fields);
                    let output = match self.backend.id() {
                        "claude" => event["type"] == "result" || takeover::claude_output(&event),
                        "agy" => event["event"] == "result" || takeover::agy_output(&event),
                        _ => true,
                    };
                    if output {
                        return Ok(false);
                    }
                }
                // An observed hook did work. Unknown stderr or shim records fail closed.
                _ => return Ok(false),
            }
        }
        Ok(spawned)
    }

    /// Keep the launch and the two turn identities before asking the shim to end the old
    /// tool. The caller revalidates this offer with the same actor after reading the fence.
    pub fn select_takeover(
        &mut self,
        offer: &Offer,
        fence: u64,
        session: String,
        input: Input,
        now_ms: i64,
    ) -> Result<bool, ActorError> {
        if !self.takeover_quiet(offer, fence)? {
            return Ok(false);
        }
        let sequence = self
            .recorder
            .agent()
            .waiting()
            .find(|next| next.id == offer.into)
            .map(|next| next.message_seq)
            .ok_or_else(|| ActorError::Corrupt("the takeover's queued message is gone".into()))?;
        let trace = self.message_at(sequence)?.trace_id;
        self.recorder.handle(
            Command::SelectTakeover {
                selected: Selection {
                    into: offer.into,
                    attempt: offer.attempt,
                    fence,
                    trace,
                    start: Start {
                        from: offer.from,
                        session,
                        input,
                    },
                },
            },
            now_ms,
        )?;
        Ok(true)
    }

    pub fn selected_takeover(&self) -> Option<&Selection> {
        self.recorder.agent().selected_takeover()
    }

    pub fn selected_takeover_socket(&self) -> Option<(TurnId, PathBuf)> {
        let selection = self.selected_takeover()?;
        let from = selection.start.from;
        Some((from, self.profile.socket_path_of(from, selection.attempt)))
    }

    pub fn selected_takeover_has_marker(&self) -> Result<bool, ActorError> {
        let Some(selection) = self.selected_takeover() else {
            return Ok(false);
        };
        let spool = self
            .profile
            .spool_path_of(selection.start.from, selection.attempt);
        let read = SpoolReader::new(&spool)
            .read_window(selection.fence, u64::MAX, 1)
            .map_err(|error| super::io_error(&spool, error))?;
        Ok(read.damaged.is_empty()
            && read.records.first().is_some_and(|record| {
                record.entry
                    == Entry::Shim(ShimEvent::Takeover {
                        fence: selection.fence,
                        replaced_by: selection.into.0,
                    })
            }))
    }

    pub fn clear_takeover(&mut self, from: TurnId, now_ms: i64) -> Result<(), ActorError> {
        self.recorder
            .handle(Command::ClearTakeover { from }, now_ms)?;
        Ok(())
    }

    /// Recovery uses the old attempt's spool rather than a possibly lost socket reply.
    /// Only the exact marker and a terminal SIGKILL confirm consumption.
    pub(super) fn recover_takeover(&mut self, now_ms: i64) -> Result<Option<Outputs>, ActorError> {
        let Some(selected) = self.selected_takeover() else {
            return Ok(None);
        };
        let (from, attempt, fence, into) = (
            selected.start.from,
            selected.attempt,
            selected.fence,
            selected.into.0,
        );
        let path = self.profile.spool_path_of(from, attempt);
        let read = SpoolReader::new(&path)
            .read_window(fence, u64::MAX, 8)
            .map_err(|error| super::io_error(&path, error))?;
        let Some(first) = read.records.first() else {
            return Ok(None);
        };
        if read.damaged.is_empty()
            && first.entry
                == Entry::Shim(ShimEvent::Takeover {
                    fence,
                    replaced_by: into,
                })
            && read.records.iter().any(|record| {
                matches!(
                    record.entry,
                    Entry::Shim(ShimEvent::Exited {
                        signal: Some(9),
                        ..
                    })
                )
            })
        {
            return self.finish_takeover(from, now_ms).map(Some);
        }
        Ok(None)
    }

    /// A confirmed takeover ends on the original user row. The first end, the second row and
    /// the second start commit together, including its launch plan for recovery.
    pub fn finish_takeover(&mut self, from: TurnId, now_ms: i64) -> Result<Outputs, ActorError> {
        let sequence = match self.recorder.agent().live() {
            Some(Turn::Starting(turn)) if turn.id == from => turn.message_seq,
            Some(Turn::Running(turn)) if turn.id == from => turn.message_seq,
            _ => return Ok(Outputs::default()),
        };
        let first = self.message_at(sequence)?;
        let facts = Facts {
            trace_id: first.trace_id,
            ..Facts::default()
        };
        let mut batch = self.recorder.batch();
        batch
            .command(Command::FinishTakeover { from }, now_ms)
            .map_err(clarp_kernel::journal::JournalError::Refused)?;
        if let Some(end) = closing(&mut batch, from, &first.id, facts) {
            batch.item(written(&end, now_ms)?);
        }
        let effects = batch.commit()?.effects;

        self.unstored_changes = 0;
        self.forget_live();
        let mut out = Outputs {
            changed: Changed {
                conversation: true,
                status: true,
                queue: true,
                ..Changed::default()
            },
            ..Outputs::default()
        };
        self.carry_out(effects, &mut out, now_ms)?;
        Ok(out)
    }
}
