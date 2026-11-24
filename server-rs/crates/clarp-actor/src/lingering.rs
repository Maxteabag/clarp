//! The process of a turn that is over for the user and still runs.
//!
//! A tool says in a line of its own that its turn is over (Claude Code's `result`, Grok's end
//! line, Codex's `turn.completed`) and goes on a moment longer: Claude Code sends what it
//! counted before it exits, for up to a few seconds. The turn ends for the user at that line,
//! when nothing else of it is owed ([`ends_at_answer`]). Its process lingers until its exit is
//! in its spool, and no turn of the agent is launched before: two processes never work in one
//! conversation. A turn that waits for it is started in the log and launched at the exit, as a
//! turn that waits for the agent's fork is.
//!
//! The exit no longer changes the turn. One that fails after the tool said its answer was
//! whole leaves the answer as it is, told: the user has it whole, and trying the turn again
//! would answer it twice. The Host's own log says that it failed. What the process still says
//! after its end line is kept in the conversation, as the turn's, where the exit would have
//! kept it: a turn misjudged as over shows a late row, and loses nothing.
//!
//! A process lingers at most [`LINGER_MOST_MS`]: then it is stopped as a Stop stops it, and
//! once the Stop has had its time the next launch goes on, whether its exit came or not (a
//! process it started outside its group can hold its output open).

use clarp_backend::{Interpreter, Signal};
use clarp_kernel::agent::Agent;
use clarp_kernel::conversation::Entry;
use clarp_kernel::steering::Stage;
use clarp_kernel::turn::{Origin, RunOutcome, Turn, TurnId};
use clarp_spool::{Entry as Line, ShimEvent, SpoolReader};

use super::{Action, Actor, ActorError, LiveTurn, Outputs, WINDOW, io_error, written};

/// How long the process of a turn that is over may run before it is stopped. Claude Code
/// sends what it counted within a few seconds.
pub const LINGER_MOST_MS: i64 = 30_000;

/// How long a stopped lingering process has to be gone, beyond the Stop's own grace: an
/// interrupted Claude Code is given three seconds before SIGTERM.
const STOPPED_MOST_MS: i64 = 8_000;

/// How a turn that ends before its process does ended for the user.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(super) enum EndsAt {
    /// The tool said its answer is whole.
    Answer,
    /// The turn was stopped, and the tool said its turn is over.
    Stop,
}

pub(super) struct Lingering {
    turn: TurnId,
    attempt: u32,
    /// How far its spool has been read.
    offset: u64,
    /// What reads what it prints still, for how its process ends. None when the Host took it
    /// up after a restart: then only that it ended counts.
    interpreter: Option<Box<dyn Interpreter>>,
    /// The turn was stopped, or the process was told to end after it: how it exits says nothing.
    stopped: bool,
    /// Whose turn it was, for what the process says after its end line.
    origin: Origin,
    trace_id: Option<String>,
    /// When it began to linger, and when it was told to stop.
    since_ms: i64,
    stop_sent_ms: Option<i64>,
}

/// Whether the turn is over for the user at this line of its tool, while its process still
/// runs: the turn holds the agent and runs; it was not stopped for its account; it took no
/// message it has not answered (one given to its process and not yet read, one read and not
/// answered, or a turn of its own the process went on to); it never ran a tool that did not
/// start its input. A turn that was stopped may end so with a tool call that runs: that call
/// is cut off as the end of a stopped turn cuts it off. One that answered may not.
pub(super) fn ends_at_answer(live: &LiveTurn, agent: &Agent, at: EndsAt) -> bool {
    let joins = agent.joins().values();
    let joins_owed = match at {
        EndsAt::Answer => joins
            .into_iter()
            .any(|follow| follow.stage != Stage::Settled),
        // A message the process read and did not answer ends with the stopped turn, as it
        // does at the exit. One it may still read waits for the exit.
        EndsAt::Stop => joins
            .into_iter()
            .any(|follow| follow.stage == Stage::Pending),
    };
    let stopped = agent.stopping().is_some();
    !live.over
        && !live.for_account
        && !live.steering.input_skipped
        && live.steering.separate.is_none()
        && live.steering.parent().is_none()
        && !joins_owed
        && agent.selected_takeover().is_none()
        && match at {
            EndsAt::Answer => {
                !stopped && live.tools.is_empty() && live.steering.answered_its_prompt()
            }
            EndsAt::Stop => stopped,
        }
        && matches!(agent.live(), Some(Turn::Running(running)) if running.id == live.turn)
}

impl Actor {
    /// Whether a process of a turn that is over still runs.
    pub(super) fn lingers(&self) -> bool {
        self.lingering.is_some()
    }

    /// The live turn ended before its process: the actor follows that process to its exit.
    pub(super) fn linger(&mut self, now_ms: i64) {
        let Some(live) = self.live.take() else {
            return;
        };
        // What the turn's end says is kept for the agent's row.
        if let Ok(Some(end)) = self.turn_end(live.turn.0) {
            self.remembered.take(&end);
        }
        self.lingering = Some(Lingering {
            turn: live.turn,
            attempt: live.attempt,
            offset: live.offset,
            interpreter: Some(live.interpreter),
            stopped: live.ended_at == Some(EndsAt::Stop),
            origin: live.origin,
            trace_id: live.trace_id,
            since_ms: now_ms,
            stop_sent_ms: None,
        });
    }

    /// After a restart: the last turn that ended may have ended before its process, which may
    /// still run. Its shim's socket says so: the shim takes it away when the tool is gone.
    /// Followed to its exit, as before the restart. Called before the live turn is taken up,
    /// whose launch waits for that exit.
    pub(super) fn take_up_lingering(&mut self, out: &mut Outputs, now_ms: i64) {
        let Some(last) = self.recorder.agent().last() else {
            return;
        };
        let (turn, attempt, origin) = (last.id, last.attempts, last.origin.clone());
        if attempt == 0 {
            return;
        }
        let socket = self.profile.socket_path_of(turn, attempt);
        if !socket.exists() {
            // Its exit came while the Host was away: what it left is of no more use.
            self.forget_attempts(turn, attempt);
            return;
        }
        self.lingering = Some(Lingering {
            turn,
            attempt,
            offset: 0,
            interpreter: None,
            stopped: false,
            origin,
            trace_id: None,
            since_ms: now_ms,
            stop_sent_ms: None,
        });
        out.actions.push(Action::FollowExit {
            turn,
            attempt,
            socket,
        });
    }

    /// Reads what the lingering process printed. When its exit is there, it is let go.
    pub(super) fn pump_lingering(
        &mut self,
        out: &mut Outputs,
        now_ms: i64,
    ) -> Result<(), ActorError> {
        let Some(lingering) = self.lingering.as_mut() else {
            return Ok(());
        };
        let spool = self
            .profile
            .spool_path_of(lingering.turn, lingering.attempt);
        let mut gone = false;
        let mut late = Vec::new();
        while !gone {
            let read = SpoolReader::new(&spool)
                .read_window(lingering.offset, u64::MAX, WINDOW)
                .map_err(|error| io_error(&spool, error))?;
            if read.next_offset == lingering.offset {
                break;
            }
            lingering.offset = read.next_offset;
            let mut signals = Vec::new();
            for record in &read.records {
                gone |= matches!(record.entry, Line::Shim(ShimEvent::Exited { .. }));
                let Some(interpreter) = lingering.interpreter.as_mut() else {
                    continue;
                };
                interpreter.on_record(record, &mut signals);
                for signal in signals.drain(..) {
                    let entry = match signal {
                        Signal::Ended(outcome)
                            if outcome != RunOutcome::Completed && !lingering.stopped =>
                        {
                            eprintln!(
                                "clarp-host: agent {}: turn {} was over when its tool exited \
                                 badly; it stays as it ended: {outcome:?}",
                                self.profile.session, lingering.turn.0
                            );
                            continue;
                        }
                        Signal::Reply(mut reply) => {
                            reply.origin = lingering.origin.clone();
                            reply.trace_id.clone_from(&lingering.trace_id);
                            Entry::Reply(reply)
                        }
                        Signal::Tool(mut call) => {
                            call.origin = lingering.origin.clone();
                            call.trace_id.clone_from(&lingering.trace_id);
                            Entry::Tool(call)
                        }
                        Signal::Notice(notice) => Entry::Notice(notice),
                        _ => continue,
                    };
                    late.push((entry, record.t));
                }
            }
        }
        if !late.is_empty() {
            eprintln!(
                "clarp-host: agent {}: turn {}'s tool said {} thing(s) after its turn was over; \
                 they are kept as the turn's",
                self.profile.session,
                lingering.turn.0,
                late.len()
            );
            let mut batch = self.recorder.batch();
            for (entry, at) in &late {
                batch.item(written(entry, *at)?);
            }
            batch.commit()?;
            self.unstored_changes = 0;
            out.changed.conversation = true;
        }
        if gone {
            self.let_go(out, now_ms)?;
        }
        Ok(())
    }

    /// The shim of this attempt is gone. Answers whether it was the lingering one.
    pub(super) fn lingering_gone(
        &mut self,
        turn: TurnId,
        attempt: u32,
        out: &mut Outputs,
        now_ms: i64,
    ) -> Result<bool, ActorError> {
        let ours = self
            .lingering
            .as_ref()
            .is_some_and(|lingering| lingering.turn == turn && lingering.attempt == attempt);
        if ours {
            self.pump_lingering(out, now_ms)?;
            self.let_go(out, now_ms)?;
        }
        Ok(ours)
    }

    /// A Stop while the process lingers ends it. The turn it was is over and stays as it ended.
    pub(super) fn stop_lingering(&mut self, out: &mut Outputs, now_ms: i64) {
        let Some(lingering) = self.lingering.as_mut() else {
            return;
        };
        lingering.stopped = true;
        lingering.stop_sent_ms.get_or_insert(now_ms);
        out.actions.push(Action::Stop {
            turn: lingering.turn,
            socket: self
                .profile
                .socket_path_of(lingering.turn, lingering.attempt),
            grace_ms: self.profile.stop_grace_ms,
        });
    }

    /// When the lingering process is to be stopped, or let go of though it did not end.
    pub fn linger_due_at(&self) -> Option<i64> {
        let lingering = self.lingering.as_ref()?;
        Some(match lingering.stop_sent_ms {
            None => lingering.since_ms + LINGER_MOST_MS,
            Some(sent) => {
                sent + i64::try_from(self.profile.stop_grace_ms).unwrap_or(i64::MAX / 4)
                    + STOPPED_MOST_MS
            }
        })
    }

    /// The lingering process ran too long: it is stopped as a Stop stops it. Once that Stop has
    /// had its time, it is let go of, and the launch that waited for it is made.
    pub fn linger_due(&mut self, now_ms: i64) -> Result<Outputs, ActorError> {
        let mut out = Outputs::default();
        if self.linger_due_at().is_none_or(|due| due > now_ms) {
            return Ok(out);
        }
        self.pump_lingering(&mut out, now_ms)?;
        let Some(lingering) = self.lingering.as_ref() else {
            return Ok(out);
        };
        let (session, turn) = (&self.profile.session, lingering.turn.0);
        if lingering.stop_sent_ms.is_none() {
            eprintln!("clarp-host: agent {session}: turn {turn}'s tool ran on after it; stopped");
            self.stop_lingering(&mut out, now_ms);
        } else {
            eprintln!(
                "clarp-host: agent {session}: turn {turn}'s tool was not gone after its Stop; \
                 the next turn goes on"
            );
            self.let_go(&mut out, now_ms)?;
        }
        out.changed.status = true;
        Ok(out)
    }

    /// The lingering process is gone: what it left is cleared away, and the launch that waited
    /// for it is made.
    fn let_go(&mut self, out: &mut Outputs, now_ms: i64) -> Result<(), ActorError> {
        let Some(lingering) = self.lingering.take() else {
            return Ok(());
        };
        self.forget_attempts(lingering.turn, lingering.attempt);
        self.resume_deferred(out, now_ms)
    }

    /// The files of a turn's attempts that are over.
    pub(super) fn forget_attempts(&self, turn: TurnId, attempts: u32) {
        for attempt in 1..=attempts {
            let _ = std::fs::remove_file(self.profile.stdin_path_of(turn, attempt));
            if !self.profile.keep_spools {
                let _ = std::fs::remove_file(self.profile.spool_path_of(turn, attempt));
            }
        }
    }
}
