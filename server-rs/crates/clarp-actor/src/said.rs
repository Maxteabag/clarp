//! What a running turn has said that the work after it decides on: whether a reply was the
//! answer or was said along the way. The live turn and the replay of its spool after a Host
//! restart apply these same rules, record by record, so that a restart changes nothing of which
//! replies are answers, neither in the log nor for those who watch the turn. The replay only
//! does not write again what the log holds already.

use clarp_kernel::live::Hub;
use serde_json::Value;

use super::ActorError;

/// Those who watch the turn: its live view, the time of the record that decides, and where what
/// the view sends goes. The live turn sends it; the replay after a restart sends it to nobody.
pub(super) struct Shown<'a> {
    pub(super) hub: &'a mut Hub,
    pub(super) now: i64,
    pub(super) events: &'a mut Vec<Value>,
}

#[derive(Default)]
pub(super) struct Unfollowed {
    /// The replies of this turn that said something and that no tool work has followed yet,
    /// oldest first. Tool work that comes makes them commentary.
    replies: Vec<String>,
    /// The newest of `replies` carried tool calls: it announced work, and is no answer.
    worked: bool,
    /// The steps of the plan the tool last told in this turn. A plan told again unchanged (Codex
    /// closes its list so when the task is complete, after its last words) is no work.
    plan: Option<Value>,
    /// What this attempt said or did decided what was said before it: from here on it does
    /// not matter what an attempt before this one left.
    decided: bool,
}

impl Unfollowed {
    /// What the log says the turn has said that no work followed, as an attempt begins: an
    /// attempt before it, or an actor before this one, may have said it.
    pub(super) fn from_log((replies, worked): (Vec<String>, bool)) -> Self {
        Self {
            replies,
            worked,
            ..Self::default()
        }
    }

    /// The state the replay of an attempt's spool came to. Where that attempt decided nothing
    /// of what was said before it, what was said before it is the log's, as when it began.
    pub(super) fn or_log(
        self,
        log: impl FnOnce() -> Result<(Vec<String>, bool), ActorError>,
    ) -> Result<Self, ActorError> {
        if self.decided {
            return Ok(self);
        }
        let (replies, worked) = log()?;
        Ok(Self {
            replies,
            worked,
            ..self
        })
    }

    pub(super) fn is_empty(&self) -> bool {
        self.replies.is_empty()
    }

    /// A reply was written: `words` whether it says something, `calls` whether it calls tools.
    /// Returns the replies its calls make said along the way.
    pub(super) fn reply(
        &mut self,
        id: &str,
        words: bool,
        calls: bool,
        shown: Shown<'_>,
    ) -> Vec<String> {
        // The tools it calls are work after what the turn said before.
        let followed = if calls {
            self.work()
        } else {
            if words {
                // Words after words: those before were an answer, and stay one whatever work
                // follows these. The conversation view only grows. Words whose own message
                // called tools were said along the way, and are no answer.
                if self.worked {
                    self.replies.clear();
                }
                for answer in self.replies.drain(..) {
                    let events = shown.hub.close_answer(&answer, shown.now);
                    shown.events.extend(events);
                }
                self.decided = true;
            }
            Vec::new()
        };
        if words {
            self.replies.push(id.to_owned());
            self.worked = calls;
            self.decided = true;
        }
        followed
    }

    /// Work that no reply carries began: a call that is a row by itself. Returns the replies it
    /// makes said along the way.
    pub(super) fn work(&mut self) -> Vec<String> {
        self.worked = false;
        self.decided = true;
        std::mem::take(&mut self.replies)
    }

    /// The tool told its plan. A plan whose steps changed (Codex's `update_plan`) is work,
    /// though no row of the conversation says so: returns the replies it makes said along the
    /// way. One told again with the same steps is no new work.
    pub(super) fn plan(&mut self, steps: &Value) -> Option<Vec<String>> {
        if self.plan.as_ref() == Some(steps) {
            return None;
        }
        self.plan = Some(steps.clone());
        Some(self.work())
    }

    /// A message was read into the turn. It closes what the turn said before it: an answer
    /// given there answered what came before, and the work that follows is for the new
    /// message. Tool work must never make it commentary, which the conversation does not show:
    /// a reply the user was shown stays shown. That holds where the message begins a turn of
    /// its own (`own_turn`: the tool had already ended its answer), and where the newest words
    /// carried no call. Words that announced a call are said along the way, and the work after
    /// them still makes them so.
    pub(super) fn read(&mut self, own_turn: bool, shown: Shown<'_>) {
        if own_turn || !self.worked {
            self.replies.clear();
            self.worked = false;
            self.decided = true;
            // Those who watch the turn are shown them as answers too.
            let events = shown.hub.close_answers(shown.now);
            shown.events.extend(events);
        }
    }
}
