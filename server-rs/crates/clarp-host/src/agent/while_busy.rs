//! What the owner's send does while the agent works (feature `send_while_busy`, issue 920):
//! read against the agent's state on its own thread, before the send is committed, so that what
//! is decided and what is done are the same step.

use clarp_kernel::conversation::Message;
use clarp_kernel::turn::Admission;
use clarp_kernel::while_busy::{Done, Reason, WhileBusy};

use super::Worker;

/// A send's `while_busy`, before and after it is read against the agent.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum Busy {
    /// Not asked: a busy send does what it always did.
    #[default]
    Unasked,
    /// Asked, not yet read against the agent.
    Asked(WhileBusy),
    /// Read: what to do with it.
    Planned(Plan),
}

/// What a send of the owner's that named `while_busy` does.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Plan {
    /// Nothing runs, or the send is not the owner's own: it is taken as any other, and its
    /// answer says nothing of this.
    AsAlways,
    /// Read into the running turn as always; where the turn cannot take it yet, it waits and
    /// is read in when it can ([`Worker::steer_when_can`]).
    Steer,
    /// Waits for the running turn and runs next.
    Wait(Done),
    /// Said direct: the running turn makes way now and it runs next, as a take-over.
    Direct(Done),
}

impl Worker {
    /// Reads a send's `while_busy` against the agent's state: whether a turn runs, whether its
    /// tool can take words during a turn at all, and whether it can be cut. Only the owner's own
    /// send said to the agent (origin user or oracle, not another agent's words) acts on it.
    pub(super) fn plan_while_busy(
        &self,
        asked: WhileBusy,
        message: &Message,
        admission: Admission,
        coordination: bool,
    ) -> Plan {
        let owners =
            admission == Admission::Queue && message.origin.is_user_send() && !coordination;
        if !owners {
            return Plan::AsAlways;
        }
        let text = message.text.trim();
        // A send the tool would not take as words (a file it names, a `/` command) waits for
        // its own turn, as it always has.
        let words = !text.starts_with('/') && clarp_backend::agy_acp::attached(text).is_empty();
        self.plan_for(asked, words)
    }

    /// What an owner's own send said to the agent now does while a turn runs, by what it asks
    /// and whether it is words the tool could take: [`Worker::plan_while_busy`] for a send, and
    /// [`Worker::owner_waits`] for the row that says whether such a send waits.
    fn plan_for(&self, asked: WhileBusy, words: bool) -> Plan {
        let Some(actor) = self.actor.as_ref() else {
            return Plan::AsAlways;
        };
        if actor.transit_holds_queue() || actor.live_turn().is_none() {
            return Plan::AsAlways;
        }
        let cuttable = actor.live_cuttable();
        // A turn that cannot be cut (the tool's compaction, a native goal's own turn) and cannot
        // take words now is waited for, whatever was asked: as a take-over of it is refused, and
        // the answer says so (`turn_not_cuttable`).
        let take_over = |done: Done| {
            if cuttable {
                Plan::Direct(done)
            } else {
                Plan::Wait(Done::fell_back(
                    asked,
                    WhileBusy::AfterTurn,
                    Reason::NotCuttable,
                ))
            }
        };
        match asked {
            WhileBusy::TakeOver => take_over(Done::as_asked(WhileBusy::TakeOver)),
            WhileBusy::AfterTurn => Plan::Wait(Done::as_asked(WhileBusy::AfterTurn)),
            WhileBusy::Steer if !words => Plan::Wait(Done::fell_back(
                asked,
                WhileBusy::AfterTurn,
                Reason::NotWords,
            )),
            WhileBusy::Steer => {
                if self.never_takes_words() || (!cuttable && !actor.steerable()) {
                    take_over(Done::fell_back(
                        asked,
                        WhileBusy::TakeOver,
                        Reason::NotSteerable,
                    ))
                } else {
                    Plan::Steer
                }
            }
        }
    }

    /// What keeps a message of the owner's said now waiting, and how many waiting messages run
    /// before it (issue 918, the agent's `availability`). The message is the one his apps send:
    /// words, with the Host's `while_busy` setting (feature `send_while_busy`), read by the same
    /// [`Worker::plan_for`] that admits it. It waits where a backend move holds the queue, or
    /// where the running turn can neither take it now nor be cut (`turn_not_cuttable`: the
    /// tool's compaction, a summary for a move, a native goal's own turn). It does not wait
    /// where it is read into the turn, or the turn makes way for it (a turn that takes no words,
    /// his own included, is stopped for it under `steer` and `take_over`). Under `after_turn` it
    /// waits for the turn because he chose that order: ordinary work, which the chat's preview
    /// shows, not a reason the agent cannot answer. A send without `while_busy` (an app before
    /// feature `send_while_busy`) keeps its older admission, which this does not describe.
    pub(super) fn owner_waits(&self) -> crate::availability::OwnerWaits {
        use crate::availability::{OwnerWaits, Why};
        let Some(actor) = self.actor.as_ref() else {
            return OwnerWaits::default();
        };
        let why = if actor.transit_holds_queue() {
            Some(Why::Moving)
        } else {
            let directory = self.setup.hub.queue_directory();
            let setting = directory
                .as_deref()
                .and_then(|directory| directory.setting(clarp_kernel::while_busy::KEY));
            match self.plan_for(WhileBusy::stored(setting), true) {
                Plan::Wait(Done {
                    fallback: Some((_, Reason::NotCuttable)),
                    ..
                }) => actor.live_origin().and_then(Why::uncut),
                Plan::AsAlways | Plan::Steer | Plan::Direct(_) | Plan::Wait(_) => None,
            }
        };
        OwnerWaits {
            why,
            ahead: actor.ahead_of_owner(),
        }
    }

    /// Whether the running turn's tool cannot take words during this turn at all: its backend
    /// has no steering, its lane is off by the owner's setting, a terminal is open on its tool,
    /// the turn runs in a process of its own beside the lane (an exec turn, a turn on its own
    /// server), or its input has ended. A turn that may still take them later ("not yet": its
    /// process or lane not known yet) is not this.
    fn never_takes_words(&self) -> bool {
        let Some(actor) = self.actor.as_ref() else {
            return false;
        };
        let offered = match self.setup.backend.steers() {
            clarp_backend::Steers::Stdin => true,
            clarp_backend::Steers::Words => self.chat_lane(),
            clarp_backend::Steers::No => false,
        };
        let live = actor.live_turn();
        !offered
            || self.terminals > 0
            || self.behind_terminal.is_some()
            || (live.is_some() && self.exec_turn == live)
            || actor.input_ended()
    }

    /// The owner's messages that asked to be read in and found the turn not ready for them yet
    /// are read in once it is (`steerable`): each in the order sent, as the chip's own steer
    /// does. One that started as a turn of its own, or that the turn still cannot take, leaves
    /// or stays. Kept on this thread only: after a restart such a message waits for its turn,
    /// as any message that could not be read in.
    pub(super) fn steer_when_can(&mut self) {
        if self.steer_pending.is_empty() {
            return;
        }
        let now = self.now_ms();
        let Some(actor) = self.actor.as_mut() else {
            self.steer_pending.clear();
            return;
        };
        let live = actor.live_turn();
        self.steer_pending.retain(|(into, _)| Some(*into) == live);
        if actor.transit_holds_queue() || !actor.steerable() {
            return;
        }
        let mut still = Vec::new();
        let mut done = clarp_actor::Outputs::default();
        for (into, turn) in std::mem::take(&mut self.steer_pending) {
            match actor.steer_waiting(turn, true, now) {
                Ok((clarp_actor::Steered::NotSteerable, out)) => {
                    still.push((into, turn));
                    done.absorb(out);
                }
                Ok((_, out)) => done.absorb(out),
                Err(error) => eprintln!(
                    "clarp-host: agent {}: a waiting message was not read into the turn: {error}",
                    self.setup.record.session
                ),
            }
        }
        self.steer_pending = still;
        self.after(done);
    }
}
