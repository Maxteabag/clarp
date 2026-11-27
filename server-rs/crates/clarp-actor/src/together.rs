//! The user's messages that waited behind a turn go to its tool with the turn's own message, as
//! one turn.
//!
//! The owner, on 23 Nov 2026, of an agent that answered his waiting messages one turn at a
//! time: "he should know what is going on". When a turn starts for a message of the user's
//! and more of the user's messages wait directly behind it, they are reserved for that turn
//! and read at once (`Command::Join` with `Join::together`, then `Command::JoinRead`), in the
//! same append: the same reservation a steered message has, so their rows enter the
//! conversation in order before the answer, the queue tells each one started, and they settle
//! with the turn. The prompt holds all of them, numbered in the order they were sent. Every
//! attempt of the turn is given the same prompt, rebuilt from what the log holds.

use clarp_kernel::agent::Command;
use clarp_kernel::conversation::Message;
use clarp_kernel::steering::Join;
use clarp_kernel::turn::TurnId;

use super::{Actor, ActorError, Outputs, steering};

/// The attempt a message's Claude input identity is named for when it goes into another
/// turn's prompt: an identity no attempt, steer or fork of its own ever has.
const TOGETHER_ATTEMPT: u32 = 2_001;

/// The reply to several utterances follows the one the caller most recently sent.
pub(super) fn reply_trace(carrier: &Message, latest: Option<&Message>) -> Option<String> {
    if carrier.voice_call.is_some()
        && let Some(trace) = latest.and_then(|one| one.trace_id.as_ref())
    {
        return Some(trace.clone());
    }
    carrier.trace_id.clone()
}

impl Actor {
    /// The user's messages a turn's prompt holds after its own, in the order they were sent.
    /// At the start of the turn's first attempt (`gather`) those that wait directly behind its
    /// message are taken into it first; a later attempt is given what the first took.
    pub(super) fn together(
        &mut self,
        turn: TurnId,
        carrier: Option<&Message>,
        out: &mut Outputs,
        now_ms: i64,
    ) -> Result<Vec<Message>, ActorError> {
        if let Some(carrier) = carrier {
            self.gather(turn, carrier, out, now_ms)?;
        }
        self.recorder
            .agent()
            .gathered(turn)
            .iter()
            .map(|gathered| self.message_at(gathered.message_seq))
            .collect()
    }

    fn gather(
        &mut self,
        turn: TurnId,
        carrier: &Message,
        out: &mut Outputs,
        now_ms: i64,
    ) -> Result<(), ActorError> {
        let agent = self.recorder.agent();
        let mut taken = Vec::new();
        for next in agent.waiting_together() {
            let Some(queued) = agent.waiting().find(|queued| queued.id == next) else {
                break;
            };
            // A message that cannot be read here is left to its own turn, which says so: the
            // turn that starts now is not held back by it.
            let Ok(message) = self.message_at(queued.message_seq) else {
                break;
            };
            // A different call, legacy call header or carried message keeps its
            // own turn. Stop at that boundary to preserve the order of the words.
            if !message.can_be_read_together()
                || message.voice_call != carrier.voice_call
                || !matches!(self.mark(next.0), Ok(None))
            {
                break;
            }
            taken.push((next, message));
        }
        if taken.is_empty() {
            return Ok(());
        }
        let mut batch = self.recorder.batch();
        // What the queue is told waits for the write: a batch that is not written told nothing.
        let mut told = Outputs::default();
        for (next, message) in taken {
            let uuid = clarp_kernel::takeover::attempt_prompt_uuid(
                &self.profile.agent_id,
                next.0,
                message.trace_id.as_deref(),
                TOGETHER_ATTEMPT,
            );
            let join = Join {
                into: turn,
                uuid,
                recovery: None,
                together: true,
            };
            if batch
                .command(Command::Join { turn: next, join }, now_ms)
                .is_err()
            {
                // What was taken before this one stands, and the rest waits for its own turn.
                break;
            }
            if batch
                .command(Command::JoinRead { turn: next }, now_ms)
                .is_err()
            {
                // A reservation is never written without its read: nothing of this batch is,
                // and every message waits for its own turn.
                return Ok(());
            }
            steering::queue_started(&batch, &mut told, next, &message);
        }
        if told.joined_reads.is_empty() {
            return Ok(());
        }
        batch.commit()?;
        out.joined_reads.extend(told.joined_reads);
        out.changed.conversation = true;
        out.changed.queue = true;
        Ok(())
    }
}
