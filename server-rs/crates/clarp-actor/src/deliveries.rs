//! What a client is shown of the agent's user sends since the Host started (the feature
//! `send_delivery_state`): the sends whose delivery can still change, which the agent's state
//! holds, and those that changed while this Host ran, each with the client's id and the trace
//! of the message it answers. A page of the feed, a bootstrap and a row's delivery read what
//! is held here and nothing of the log; a send's names are read once, when it comes.

use std::collections::{BTreeMap, HashMap};

use clarp_kernel::delivery::{self, Feed, Send};
use serde_json::Value;

/// How long a send that can change no more is kept: the Python Host's retention of a receipt.
pub const KEPT_FOR_MS: i64 = 7 * 24 * 3_600_000;

/// A send, with the names it is shown by.
#[derive(Debug, Clone)]
struct Kept {
    send: Send,
    client_msg_id: String,
    trace_id: String,
}

/// The agent's sends, by turn, by their last revision and by trace.
#[derive(Debug, Default)]
pub struct Deliveries {
    sends: HashMap<u64, Kept>,
    by_seq: BTreeMap<u64, u64>,
    by_trace: HashMap<String, u64>,
    /// The highest revision of a send that was let go: a cursor behind it may miss one.
    watermark: u64,
    /// The newest revision a send has had: the feed's current position, as the Python Host's
    /// counter of transitions is.
    latest: u64,
}

/// The id its app gave a message, with the trace it was sent under. A message no app named
/// (its told id is empty) is shown nothing.
pub fn names(client_id: &str, trace_id: Option<&str>) -> Option<(String, String)> {
    let client = Some(client_id).filter(|id| !id.is_empty())?;
    let trace = trace_id.filter(|trace| !trace.is_empty())?;
    Some((client.to_string(), trace.to_string()))
}

impl Deliveries {
    /// A send changed: it comes, or it moves. Answers whether it moved in a way a client is
    /// told of (fenced by the user's Stop, or handed over).
    pub fn changed(&mut self, send: Send, names: Option<(String, String)>) -> bool {
        let known = self.sends.get(&send.turn).map(|kept| kept.send.clone());
        let Some((client_msg_id, trace_id)) = names.or_else(|| {
            self.sends
                .get(&send.turn)
                .map(|kept| (kept.client_msg_id.clone(), kept.trace_id.clone()))
        }) else {
            return false;
        };
        let moved = known.as_ref().is_some_and(|was| send.rev > was.rev);
        if let Some(was) = &known {
            self.by_seq.remove(&was.seq);
        }
        self.by_seq.insert(send.seq, send.turn);
        self.latest = self.latest.max(send.seq);
        self.by_trace.insert(trace_id.clone(), send.turn);
        let told = moved && (send.handed || send.fenced);
        self.sends.insert(
            send.turn,
            Kept {
                send,
                client_msg_id,
                trace_id,
            },
        );
        told
    }

    /// Lets go of the sends that can change no more and last changed before `before_ms`.
    pub fn forget_before(&mut self, before_ms: i64, waiting: &[u64]) {
        let old: Vec<u64> = self
            .sends
            .values()
            .filter(|kept| kept.send.at_ms < before_ms && !waiting.contains(&kept.send.turn))
            .map(|kept| kept.send.turn)
            .collect();
        for turn in old {
            if let Some(kept) = self.sends.remove(&turn) {
                self.by_seq.remove(&kept.send.seq);
                self.by_trace.remove(&kept.trace_id);
                self.watermark = self.watermark.max(kept.send.seq);
            }
        }
    }

    /// The message id a client knows a send by.
    pub fn message_id(&self, turn: u64) -> Option<String> {
        self.sends
            .get(&turn)
            .map(|kept| delivery::message_id(&kept.client_msg_id))
    }

    /// The receipt of the send of a trace, as the Python Host would hold it.
    pub fn receipt(&self, agent_id: &str, trace_id: &str) -> Option<Value> {
        let kept = self.sends.get(self.by_trace.get(trace_id)?)?;
        Some(
            kept.send
                .receipt(agent_id, &kept.client_msg_id, &kept.trace_id),
        )
    }

    /// The feed as a page reads it, at the agent's user Stop generation.
    pub fn feed<'a>(&'a self, agent_id: &'a str, generation: u64) -> Pages<'a> {
        Pages {
            deliveries: self,
            agent_id,
            head: self.latest,
            generation: i64::try_from(generation).unwrap_or(i64::MAX),
        }
    }

    /// How many sends are held: for a test that reads how much is kept.
    pub fn len(&self) -> usize {
        self.sends.len()
    }

    pub fn is_empty(&self) -> bool {
        self.sends.is_empty()
    }
}

/// The feed of one agent, read a page at a time.
pub struct Pages<'a> {
    deliveries: &'a Deliveries,
    agent_id: &'a str,
    head: u64,
    generation: i64,
}

impl Feed for Pages<'_> {
    fn current(&self) -> u64 {
        self.head
    }

    fn watermark(&self) -> u64 {
        self.deliveries.watermark
    }

    fn generation(&self) -> i64 {
        self.generation
    }

    fn pointers(&self, after: u64, through: u64, cap: usize) -> Vec<(u64, String, Option<Value>)> {
        if after >= through {
            return Vec::new();
        }
        let held = self.deliveries;
        held.by_seq
            .range(after + 1..=through)
            .take(cap)
            .filter_map(|(seq, turn)| {
                let kept = held.sends.get(turn)?;
                let receipt = kept
                    .send
                    .receipt(self.agent_id, &kept.client_msg_id, &kept.trace_id);
                Some((*seq, kept.trace_id.clone(), Some(receipt)))
            })
            .collect()
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used)]
mod tests {
    use super::*;

    fn send(turn: u64, rev: u64, seq: u64, fenced: bool, handed: bool) -> Send {
        Send {
            turn,
            message_seq: turn * 10,
            stops_at: 0,
            fenced,
            handed,
            rev,
            seq,
            at_ms: 1_000,
        }
    }

    #[test]
    fn a_send_is_held_by_its_last_revision_and_told_when_it_moves() {
        let mut held = Deliveries::default();
        let named = names("m1", Some("t1"));
        assert!(!held.changed(send(1, 1, 5, false, false), named));
        assert!(held.changed(send(1, 2, 9, true, false), None));
        assert!(held.changed(send(1, 3, 12, true, true), None));
        assert_eq!(held.by_seq.keys().copied().collect::<Vec<_>>(), [12]);
        assert_eq!(held.message_id(1).as_deref(), Some("u-m1"));
        assert_eq!(held.receipt("ag", "t1").unwrap()["state"], "attempted");
        // A message no app named has no delivery to show.
        assert!(names("", Some("t2")).is_none() && names("x", None).is_none());
        let feed = held.feed("ag", 1);
        assert_eq!(feed.current(), 12);
        assert_eq!(feed.pointers(0, 20, 5).len(), 1);
        assert!(feed.pointers(12, 20, 5).is_empty());
        held.forget_before(2_000, &[]);
        assert!(held.is_empty());
        assert_eq!(held.watermark, 12);
    }
}
