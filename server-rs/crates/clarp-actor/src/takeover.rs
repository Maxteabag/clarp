//! The actor's side of takeover. The launcher only presents observations from the shim.

use std::path::PathBuf;

use clarp_kernel::agent::Command;
use clarp_kernel::takeover::{self, Input, Selection, Start};
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
    pub first_trace: Option<String>,
    /// A first turn in a fresh conversation may have no provider file yet.
    pub first_turn: bool,
}

impl Actor {
    /// Only the head of the normal user mailbox can take over, while the first turn has no
    /// stored or live output. The complete spool still has to be checked under a shim hold.
    pub fn takeover_offer(&self, into: TurnId) -> Result<Option<Offer>, ActorError> {
        let Some(live) = self.live.as_ref() else {
            return Ok(None);
        };
        let agent = self.recorder.agent();
        if !agent.can_take_over(live.turn, into, live.attempt)
            || agent.selected_takeover().is_some()
            || live.opening
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
            first_trace: first.trace_id,
            first_turn: agent.fresh_first_turn(),
        }))
    }

    /// Recheck the running attempt through the shim's exact fence. A missing or damaged
    /// record is uncertain and leaves the follow-up in Part A's queue.
    pub fn takeover_quiet(&self, offer: &Offer, until: u64) -> Result<bool, ActorError> {
        const MAX_FENCE: u64 = 4 * 1024 * 1024;
        if until > MAX_FENCE
            || self.live.as_ref().is_none_or(|live| live.offset > until)
            || self.takeover_offer(offer.into)?.is_none_or(|current| {
                (current.from, current.attempt) != (offer.from, offer.attempt)
            })
        {
            return Ok(false);
        }
        let path = self.profile.spool_path_of(offer.from, offer.attempt);
        let read = SpoolReader::new(&path)
            .read_window(0, until, usize::MAX)
            .map_err(|error| super::io_error(&path, error))?;
        if read.next_offset != until || !read.damaged.is_empty() {
            return Ok(false);
        }

        let mut spawned = false;
        for record in read.records {
            match record.entry {
                Entry::Shim(ShimEvent::Session { .. }) => {}
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
        self.carry_out(effects, &mut out)?;
        Ok(out)
    }
}
