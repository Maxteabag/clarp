//! The hub: every change the Host tells its clients about is numbered here and fanned out.
//!
//! An event is a wake-up, not the data: it says that something changed, and the client asks what.
//! That is why the hub may forget. It keeps the last few minutes so that a client that drops for
//! a moment misses nothing; a client that was away for longer, or across a restart of the Host,
//! is told to look at everything again, and the conversations themselves tell it what is new.

use std::collections::{BTreeMap, HashSet, VecDeque};
use std::sync::{Arc, Mutex, OnceLock, PoisonError, Weak};

use serde_json::{Value, json};
use tokio::sync::{broadcast, watch};

use crate::fleet::Directory;

/// How long an event is kept for clients that reconnect.
const KEEP_MS: i64 = 5 * 60 * 1000;
const KEEP_MAX: usize = 20_000;
/// How far a listening client may fall behind before it is cut off and has to reconnect.
const BEHIND_MAX: usize = 4_096;
/// How many ended turns wait at most for the experience watcher to take them, the newest kept.
const TURN_ENDS_MAX: usize = 4_096;

/// An event as it was sent: its number, and its JSON, written once for everyone.
#[derive(Debug)]
pub struct Published {
    pub id: u64,
    pub at_ms: i64,
    pub json: String,
}

struct Recent {
    /// The first number this run of the Host gave out: a number below it is an earlier run's.
    first_id: u64,
    next_id: u64,
    events: VecDeque<Arc<Published>>,
}

/// What a client that resumes was given (feature `resume_state`, issue 882): which run of the
/// Host answered it, after which event, how many it was given, and whether some it missed
/// could not be given (`truncated`): the Host had forgotten them, or the number it named was
/// another run's.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Replay {
    pub epoch: String,
    pub after: Option<u64>,
    pub count: usize,
    pub truncated: bool,
}

impl Replay {
    pub fn to_json(&self) -> Value {
        json!({
            "epoch": self.epoch,
            "after": self.after,
            "count": self.count,
            "truncated": self.truncated,
        })
    }
}

/// What an agent's turn shows right now, as it is sent to those who asked to be shown
/// (`/events?live=`, `docs/live-items.md`). It has no number and is never kept: a client that
/// missed some asks for all of it (`GET /live`).
#[derive(Debug)]
pub struct Shown {
    pub session: String,
    pub agent_id: String,
    /// The event whole, for a connection that follows this agent.
    pub whole: String,
    /// The same with only what a list of agents shows of it: the status line and the turn.
    /// `None` when it has nothing of that: a connection that does not follow the agent is
    /// sent nothing then.
    pub summary: Option<String>,
    /// The whole event without the words of the reply and the thinking, for a connection that
    /// asked for none (`text=0`, feature `events_slim`, issue 879). `None` when nothing is
    /// left of it: then such a connection is sent nothing.
    pub textless: Option<String>,
    /// The conversation and the number of the event, for a connection that numbers what it was
    /// sent of it (`lseq_prev`).
    pub conv: String,
    pub lseq: u64,
}

impl Shown {
    /// What a connection is sent of it. `wanted` are the sessions and agents it follows,
    /// with `*` for every one; `text` whether it is sent the words of a reply as they come.
    pub fn for_one(&self, wanted: &HashSet<String>, text: bool) -> Option<&str> {
        let named = |name: &String| !name.is_empty() && wanted.contains(name);
        if wanted.contains("*") || named(&self.session) || named(&self.agent_id) {
            if text {
                Some(&self.whole)
            } else {
                self.textless.as_deref()
            }
        } else {
            self.summary.as_deref()
        }
    }
}

/// The kinds of live item whose `text` is the words of the reply or of the thinking.
const WORDS: [&str; 2] = ["message", "reasoning"];

/// The ops of a live event without the words of a reply or of thinking: no text appended,
/// and no `text` in what an op sets of a message or reasoning item. The rest of every op
/// stays as it was (issue 879).
pub fn without_text(ops: &[Value]) -> Vec<Value> {
    ops.iter()
        .filter(|op| !(op["op"] == "append" && op["field"] == "text"))
        .filter_map(|op| {
            let worded = op["kind"]
                .as_str()
                .is_some_and(|kind| WORDS.contains(&kind));
            if !worded || !op["item"].is_object() {
                return Some(op.clone());
            }
            let mut op = op.clone();
            if let Some(item) = op["item"].as_object_mut() {
                item.remove("text");
            }
            // An upsert that set nothing but the words sets nothing: it is left out.
            let empty =
                op["op"] == "upsert" && op["item"].as_object().is_some_and(|item| item.is_empty());
            (!empty).then_some(op)
        })
        .collect()
}

pub struct Hub {
    pub capture: Option<Arc<crate::diagnostics::Capture>>,
    recent: Mutex<Recent>,
    live: broadcast::Sender<Arc<Published>>,
    /// What agents' turns show as they run, for the connections that asked for it.
    shown: broadcast::Sender<Arc<Shown>>,
    /// The name of this run of the Host for those who watch a turn.
    epoch: String,
    /// How many requests of each agent wait for the user: what the apps' badge counts.
    waiting: Mutex<BTreeMap<String, usize>>,
    /// What each agent's turn shows now, for who follows every agent inside this Host (the
    /// chip a phone shows of the fleet): the status alone, never what the turn wrote.
    statuses: broadcast::Sender<Arc<Value>>,
    /// Counts the changes of what waits for the user: who watches it wakes with each.
    badge: tokio::sync::watch::Sender<u64>,
    /// What adds an agent's profile to the roster's events about it (feature
    /// `voice_profile`), once the Host has its speaker and its ears.
    pub profiles: crate::profile::Slot,
    /// A queue event resolves names when it is published. The fleet owns the roster.
    queue_roster: OnceLock<Weak<watch::Receiver<Arc<Directory>>>>,
    /// The turns that ended, as their agents judged them, until the experience watcher takes
    /// them.
    turn_ends: Mutex<VecDeque<crate::experience_watch::Ended>>,
}

impl Hub {
    /// A hub whose numbers start above every number a hub started earlier gave out: the time
    /// leads, so a restart never reuses one.
    pub fn new(now_ms: i64) -> Self {
        let (live, _) = broadcast::channel(BEHIND_MAX);
        let (shown, _) = broadcast::channel(BEHIND_MAX);
        let (statuses, _) = broadcast::channel(BEHIND_MAX);
        let (badge, _) = tokio::sync::watch::channel(0);
        let seconds = u64::try_from(now_ms / 1_000).unwrap_or(0);
        Self {
            capture: None,
            shown,
            statuses,
            badge,
            // The moment it started names a run: no two runs of a Host start in one. Twelve
            // hex signs, as the Python Host's name of a run has.
            epoch: format!("{now_ms:012x}"),
            recent: Mutex::new(Recent {
                first_id: (seconds << 20).max(1),
                next_id: (seconds << 20).max(1),
                events: VecDeque::new(),
            }),
            live,
            waiting: Mutex::new(BTreeMap::new()),
            profiles: crate::profile::Slot::new(),
            queue_roster: OnceLock::new(),
            turn_ends: Mutex::new(VecDeque::new()),
        }
    }

    /// An agent's turn ended: kept for the experience watcher. Does not wait.
    pub(crate) fn turn_ended(&self, ended: crate::experience_watch::Ended) {
        let mut ends = self
            .turn_ends
            .lock()
            .unwrap_or_else(PoisonError::into_inner);
        if ends.len() >= TURN_ENDS_MAX {
            ends.pop_front();
        }
        ends.push_back(ended);
    }

    /// The turns that ended since they were last taken.
    pub(crate) fn take_turn_ends(&self) -> Vec<crate::experience_watch::Ended> {
        let mut ends = self
            .turn_ends
            .lock()
            .unwrap_or_else(PoisonError::into_inner);
        std::mem::take(&mut *ends).into()
    }

    pub(crate) fn queue_roster(
        &self,
        roster: &Arc<watch::Receiver<Arc<Directory>>>,
    ) -> Result<(), &'static str> {
        self.queue_roster
            .set(Arc::downgrade(roster))
            .map_err(|_| "the queue roster is already attached")
    }

    pub(crate) fn queue_directory(&self) -> Option<Arc<Directory>> {
        let roster = self.queue_roster.get()?.upgrade()?;
        let directory = roster.borrow().clone();
        Some(directory)
    }

    /// An agent says how many of its requests wait for the user. Answers how many wait in
    /// all: the count the apps are told.
    pub fn waiting_for_user(&self, agent_id: &str, count: usize) -> usize {
        let mut waiting = self.waiting.lock().unwrap_or_else(PoisonError::into_inner);
        let before = waiting.get(agent_id).copied().unwrap_or(0);
        if count == 0 {
            waiting.remove(agent_id);
        } else {
            waiting.insert(agent_id.to_string(), count);
        }
        if before != count {
            self.badge.send_modify(|changes| *changes += 1);
        }
        waiting.values().sum()
    }

    /// What waits for the user changed apart from the agents' requests: what reached the owner
    /// of a janitor that fails.
    pub fn badge_moved(&self) {
        self.badge.send_modify(|changes| *changes += 1);
    }

    /// Wakes whenever what waits for the user changed.
    pub fn badge(&self) -> tokio::sync::watch::Receiver<u64> {
        self.badge.subscribe()
    }

    /// Follows what every agent's turn shows: each time a status changes, an event with
    /// that status alone, as the stream of turns would have told it.
    pub fn statuses(&self) -> broadcast::Receiver<Arc<Value>> {
        self.statuses.subscribe()
    }

    /// Numbers an event, keeps it for those who reconnect and sends it to those who listen.
    /// The event is the object a client will read; its number is added to it here.
    pub fn publish(&self, mut event: Value, now_ms: i64) -> u64 {
        if let Some(profiles) = self.profiles.get() {
            profiles.add_to(&mut event);
        }
        // What an event about one agent's row changed, as the row has it now (issue 809).
        if event["type"] == "agent-roster"
            && let Some(directory) = self.queue_directory()
        {
            crate::roster_fields::add_to(&mut event, &directory);
        }
        let mut recent = self.recent.lock().unwrap_or_else(PoisonError::into_inner);
        let id = recent.next_id;
        recent.next_id += 1;
        if let Some(fields) = event.as_object_mut() {
            fields.insert("event_id".to_string(), json!(id));
        }
        let json = event.to_string();
        let published = Arc::new(Published {
            id,
            at_ms: now_ms,
            json,
        });
        while recent.events.len() >= KEEP_MAX
            || recent
                .events
                .front()
                .is_some_and(|old| now_ms - old.at_ms > KEEP_MS)
        {
            recent.events.pop_front();
        }
        recent.events.push_back(published.clone());
        // Sent while the lock is held, so that what a new listener is given from the kept
        // events and what it then hears live join up without a gap or a repeat.
        let _ = self.live.send(published);
        id
    }

    /// A live notification without a cursor or a replay record.
    pub fn ephemeral(&self, event: Value, at_ms: i64) {
        let _ = self.live.send(Arc::new(Published {
            id: 0,
            at_ms,
            json: event.to_string(),
        }));
    }

    /// The name of this run of the Host: what a `live` event and `GET /live` say under `epoch`.
    pub fn epoch(&self) -> &str {
        &self.epoch
    }

    /// Sends a `live` event to the connections that asked to be shown agents' turns. The
    /// reading of the clock in it is this moment's: a client sets its own clock by it.
    pub fn show(
        &self,
        mut event: Value,
        now_ms: i64,
        working: Option<(&str, &crate::wire::WorkingWith)>,
    ) {
        if self.statuses.receiver_count() > 0 {
            let ops = event["ops"].as_array().into_iter().flatten();
            let mut statuses = ops.filter(|op| op["op"] == "status");
            if let Some(status) = statuses.next_back() {
                let mut told = json!({
                    "type": "live", "agent_id": event["agent_id"], "session": event["session"],
                    "ops": [status],
                });
                if let Some((trace, who)) = working
                    && status["activity"]["turn_id"] == trace
                {
                    who.tell(&mut told);
                }
                let _ = self.statuses.send(Arc::new(told));
            }
        }
        if self.shown.receiver_count() == 0 {
            return;
        }
        event["server_now_ms"] = json!(now_ms);
        let whole = event.to_string();
        let of_all: Vec<Value> = event["ops"]
            .as_array()
            .into_iter()
            .flatten()
            .filter(|op| matches!(op["op"].as_str(), Some("status" | "turn")))
            .cloned()
            .collect();
        let wordless = without_text(event["ops"].as_array().map_or(&[], Vec::as_slice));
        let textless = if event["ops"].as_array().is_some_and(|ops| *ops == wordless) {
            Some(whole.clone())
        } else {
            (!wordless.is_empty()).then(|| {
                let mut copy = event.clone();
                copy["ops"] = Value::Array(wordless);
                copy.to_string()
            })
        };
        let summary = (!of_all.is_empty()).then(|| {
            event["ops"] = Value::Array(of_all);
            event.to_string()
        });
        let text = |name: &str| event[name].as_str().unwrap_or("").to_string();
        let _ = self.shown.send(Arc::new(Shown {
            session: text("session"),
            agent_id: text("agent_id"),
            conv: text("conv"),
            lseq: event["lseq"].as_u64().unwrap_or(0),
            whole,
            summary,
            textless,
        }));
    }

    /// Starts watching what agents' turns show. Nothing from before is given.
    pub fn watch(&self) -> broadcast::Receiver<Arc<Shown>> {
        self.shown.subscribe()
    }

    /// Starts listening. Returns the kept events after `after` (all of them when the client
    /// names none), and the receiver for everything from here on.
    pub fn subscribe(
        &self,
        after: Option<u64>,
    ) -> (Vec<Arc<Published>>, broadcast::Receiver<Arc<Published>>) {
        let (missed, live, _) = self.resume(after);
        (missed, live)
    }

    /// As [`Hub::subscribe`], and what the replay was: whether every event after `after` was
    /// still kept, judged under the same lock that takes them.
    pub fn resume(
        &self,
        after: Option<u64>,
    ) -> (
        Vec<Arc<Published>>,
        broadcast::Receiver<Arc<Published>>,
        Replay,
    ) {
        let recent = self.recent.lock().unwrap_or_else(PoisonError::into_inner);
        let missed: Vec<Arc<Published>> = recent
            .events
            .iter()
            .filter(|event| after.is_none_or(|after| event.id > after))
            .cloned()
            .collect();
        let oldest = recent
            .events
            .front()
            .map_or(recent.next_id, |event| event.id);
        // A number this run never gave out (an earlier run's, or one it has not reached), or
        // one older than what is kept: what came after it is not all here.
        let truncated = after.is_some_and(|after| {
            after < recent.first_id || after >= recent.next_id || oldest > after + 1
        });
        let replay = Replay {
            epoch: self.epoch.clone(),
            after,
            count: missed.len(),
            truncated,
        };
        (missed, self.live.subscribe(), replay)
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use super::*;

    fn roster(kind: &str) -> Value {
        json!({ "type": "agent-roster", "kind": kind })
    }

    #[test]
    fn events_are_numbered_kept_and_heard_without_a_gap() {
        let hub = Hub::new(1_700_000_000_000);
        let first = hub.publish(roster("one"), 1_700_000_000_000);
        let second = hub.publish(roster("two"), 1_700_000_000_001);
        assert_eq!(second, first + 1);

        let (missed, mut live) = hub.subscribe(Some(first));
        assert_eq!(
            missed.iter().map(|e| e.id).collect::<Vec<_>>(),
            vec![second]
        );
        let third = hub.publish(roster("three"), 1_700_000_000_002);
        let heard = live.try_recv().unwrap();
        assert_eq!(heard.id, third);
        assert!(heard.json.contains("\"type\":\"agent-roster\"") && heard.json.contains("three"));
        assert!(heard.json.contains(&format!("\"event_id\":{third}")));

        let (everything, _) = hub.subscribe(None);
        assert_eq!(
            everything.len(),
            3,
            "a client that names no event is given what is kept"
        );
    }

    #[test]
    fn what_a_turn_shows_goes_whole_to_those_who_follow_it_and_in_short_to_the_rest() {
        let hub = Hub::new(1_700_000_000_000);
        assert_eq!(hub.epoch(), "018bcfe56800");
        let event = |ops: Value| json!({"type": "live", "agent_id": "a1", "session": "claire", "lseq": 4, "server_now_ms": 1, "ops": ops});
        // Nobody watches: nothing is made.
        hub.show(event(json!([{"op": "status"}])), 5, None);
        let mut watching = hub.watch();
        hub.show(
            event(json!([{"op": "upsert", "id": "cl:1"}, {"op": "status", "activity": {}}, {"op": "turn"}])),
            7,
            None,
        );
        hub.show(event(json!([{"op": "append", "id": "cl:1"}])), 8, None);
        let first = watching.try_recv().unwrap();
        let wanted = |names: &[&str]| names.iter().map(|name| (*name).to_string()).collect();
        let whole: Value =
            serde_json::from_str(first.for_one(&wanted(&["claire"]), true).unwrap()).unwrap();
        assert_eq!(whole["ops"].as_array().unwrap().len(), 3);
        assert_eq!(
            whole["server_now_ms"], 7,
            "the clock is read when it is sent"
        );
        assert_eq!(
            first.for_one(&wanted(&["a1"]), true),
            first.for_one(&wanted(&["*"]), true)
        );
        let short: Value =
            serde_json::from_str(first.for_one(&wanted(&["mike"]), true).unwrap()).unwrap();
        let ops: Vec<&str> = short["ops"]
            .as_array()
            .unwrap()
            .iter()
            .map(|op| op["op"].as_str().unwrap())
            .collect();
        assert_eq!(ops, ["status", "turn"]);
        assert_eq!(short["lseq"], 4, "under the same number");
        assert_eq!(
            first.for_one(&wanted(&[]), true),
            first.for_one(&wanted(&["mike"]), true)
        );
        // An event with nothing for a list of agents is for those who follow the agent alone.
        let second = watching.try_recv().unwrap();
        assert!(second.for_one(&wanted(&["claire"]), true).is_some());
        assert_eq!(second.for_one(&wanted(&["mike"]), true), None);
        assert!(watching.try_recv().is_err());
    }

    #[test]
    fn a_connection_that_takes_no_text_is_sent_every_op_but_the_words() {
        let hub = Hub::new(1_700_000_000_000);
        let mut watching = hub.watch();
        let wanted: HashSet<String> = ["claire".to_string()].into();
        let event = |ops: Value| json!({"type": "live", "agent_id": "a1", "session": "claire", "conv": "c1", "lseq": 9, "ops": ops});
        hub.show(
            event(json!([
                {"op": "upsert", "conv": "c1", "id": "cl:m1", "kind": "message", "rev": 1,
                 "item": {"text": "Hel", "phase": "commentary", "ordinal": 3}},
                {"op": "upsert", "conv": "c1", "id": "cl:r1", "kind": "reasoning", "rev": 2,
                 "item": {"text": "more thought"}},
                {"op": "append", "conv": "c1", "id": "cl:m1", "kind": "message", "rev": 2,
                 "field": "text", "chunk": "lo"},
                {"op": "append", "conv": "c1", "id": "cx:t1", "kind": "tool", "rev": 4,
                 "field": "tool.output", "lines": ["ok"], "total_lines": 1},
                {"op": "done", "conv": "c1", "id": "cl:m1", "kind": "message", "rev": 3,
                 "status": "completed", "item": {"text": "Hello"}},
                {"op": "status", "conv": "c1", "activity": {"state": "responding"}},
            ])),
            5,
            None,
        );
        hub.show(
            event(json!([{"op": "append", "conv": "c1", "id": "cl:m1", "kind": "message", "rev": 4, "field": "text", "chunk": "!"}])),
            6,
            None,
        );
        let first = watching.try_recv().unwrap();
        assert_eq!((first.conv.as_str(), first.lseq), ("c1", 9));
        let slim: Value = serde_json::from_str(first.for_one(&wanted, false).unwrap()).unwrap();
        let ops = slim["ops"].as_array().unwrap();
        let named: Vec<(&str, &str)> = ops
            .iter()
            .map(|op| (op["op"].as_str().unwrap(), op["id"].as_str().unwrap_or("")))
            .collect();
        assert_eq!(
            named,
            [
                ("upsert", "cl:m1"),
                ("append", "cx:t1"),
                ("done", "cl:m1"),
                ("status", "")
            ],
            "the text appends and an upsert of the words alone are left out"
        );
        assert_eq!(ops[0]["item"], json!({"phase": "commentary", "ordinal": 3}));
        assert!(ops[2]["item"].as_object().unwrap().is_empty());
        assert!(!slim.to_string().contains("Hel"));
        assert_eq!(
            first.for_one(&wanted, true).unwrap(),
            first.whole,
            "a connection that takes text is sent it whole"
        );
        // An event of nothing but the words is not sent at all to who takes none.
        let second = watching.try_recv().unwrap();
        assert_eq!(second.for_one(&wanted, false), None);
        assert!(second.for_one(&wanted, true).is_some());
        // One whose ops all stay, less their words, is sent without them.
        hub.show(
            event(
                json!([{"op": "upsert", "conv": "c1", "id": "cl:r2", "kind": "reasoning", "rev": 1,
                          "item": {"text": "", "status": "running", "title": null}}]),
            ),
            7,
            None,
        );
        let third = watching.try_recv().unwrap();
        let slim: Value = serde_json::from_str(third.for_one(&wanted, false).unwrap()).unwrap();
        assert_eq!(
            slim["ops"][0]["item"],
            json!({"status": "running", "title": null})
        );
    }

    #[test]
    fn a_resume_says_whether_its_replay_was_whole() {
        let hub = Hub::new(1_700_000_000_000);
        let first = hub.publish(roster("one"), 1_700_000_000_000);
        let second = hub.publish(roster("two"), 1_700_000_000_001);
        let (missed, _, whole) = hub.resume(Some(first));
        assert_eq!(missed.len(), 1);
        assert_eq!(
            whole,
            Replay {
                epoch: hub.epoch().to_string(),
                after: Some(first),
                count: 1,
                truncated: false
            }
        );
        assert!(!hub.resume(Some(second)).2.truncated, "nothing missed");
        assert!(
            hub.resume(Some(first - 1)).2.truncated,
            "a number before the run's first"
        );
        assert!(!hub.resume(None).2.truncated, "no resume");
        // A number of an earlier run, or one this run never gave.
        assert!(hub.resume(Some(first - 2)).2.truncated);
        assert!(hub.resume(Some(second + 1)).2.truncated);
        // Forgotten: the run has gone on past what it keeps.
        hub.publish(roster("much later"), 1_700_000_000_000 + KEEP_MS + 5);
        let (missed, _, cut) = hub.resume(Some(first));
        assert_eq!((missed.len(), cut.truncated), (1, true));
        assert_eq!(
            cut.to_json(),
            json!({"epoch": hub.epoch(), "after": first, "count": 1, "truncated": true})
        );
    }

    #[test]
    fn old_events_are_forgotten_and_a_later_hub_starts_above_an_earlier_one() {
        let hub = Hub::new(1_700_000_000_000);
        let old = hub.publish(roster("old"), 1_700_000_000_000);
        let new = hub.publish(roster("new"), 1_700_000_000_000 + KEEP_MS + 1);
        let (kept, _) = hub.subscribe(None);
        assert_eq!(kept.iter().map(|e| e.id).collect::<Vec<_>>(), vec![new]);
        assert!(new > old);

        let later = Hub::new(1_700_000_001_000);
        assert!(
            later.publish(roster("after a restart"), 1_700_000_001_000) > new,
            "a hub started a second later numbers above the one before it"
        );
    }
}
