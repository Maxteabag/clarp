//! The hub: every change the Host tells its clients about is numbered here and fanned out.
//!
//! An event is a wake-up, not the data: it says that something changed, and the client asks what.
//! That is why the hub may forget. It keeps the last few minutes so that a client that drops for
//! a moment misses nothing; a client that was away for longer, or across a restart of the Host,
//! is told to look at everything again, and the conversations themselves tell it what is new.

use std::collections::{BTreeMap, HashSet, VecDeque};
use std::sync::{Arc, Mutex, PoisonError};

use serde_json::{Value, json};
use tokio::sync::broadcast;

/// How long an event is kept for clients that reconnect.
const KEEP_MS: i64 = 5 * 60 * 1000;
const KEEP_MAX: usize = 20_000;
/// How far a listening client may fall behind before it is cut off and has to reconnect.
const BEHIND_MAX: usize = 4_096;

/// An event as it was sent: its number, and its JSON, written once for everyone.
#[derive(Debug)]
pub struct Published {
    pub id: u64,
    pub at_ms: i64,
    pub json: String,
}

struct Recent {
    next_id: u64,
    events: VecDeque<Arc<Published>>,
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
}

impl Shown {
    /// What a connection is sent of it. `wanted` are the sessions and agents it follows,
    /// with `*` for every one.
    pub fn for_one(&self, wanted: &HashSet<String>) -> Option<&str> {
        let named = |name: &String| !name.is_empty() && wanted.contains(name);
        if wanted.contains("*") || named(&self.session) || named(&self.agent_id) {
            Some(&self.whole)
        } else {
            self.summary.as_deref()
        }
    }
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
                next_id: (seconds << 20).max(1),
                events: VecDeque::new(),
            }),
            live,
            waiting: Mutex::new(BTreeMap::new()),
            profiles: crate::profile::Slot::new(),
        }
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
    pub fn show(&self, mut event: Value, now_ms: i64) {
        if self.statuses.receiver_count() > 0 {
            let ops = event["ops"].as_array().into_iter().flatten();
            let mut statuses = ops.filter(|op| op["op"] == "status");
            if let Some(status) = statuses.next_back() {
                let told = json!({
                    "type": "live", "agent_id": event["agent_id"], "session": event["session"],
                    "ops": [status],
                });
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
        let summary = (!of_all.is_empty()).then(|| {
            event["ops"] = Value::Array(of_all);
            event.to_string()
        });
        let text = |name: &str| event[name].as_str().unwrap_or("").to_string();
        let _ = self.shown.send(Arc::new(Shown {
            session: text("session"),
            agent_id: text("agent_id"),
            whole,
            summary,
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
        let recent = self.recent.lock().unwrap_or_else(PoisonError::into_inner);
        let missed = recent
            .events
            .iter()
            .filter(|event| after.is_none_or(|after| event.id > after))
            .cloned()
            .collect();
        (missed, self.live.subscribe())
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
        hub.show(event(json!([{"op": "status"}])), 5);
        let mut watching = hub.watch();
        hub.show(
            event(json!([{"op": "upsert", "id": "cl:1"}, {"op": "status", "activity": {}}, {"op": "turn"}])),
            7,
        );
        hub.show(event(json!([{"op": "append", "id": "cl:1"}])), 8);
        let first = watching.try_recv().unwrap();
        let wanted = |names: &[&str]| names.iter().map(|name| (*name).to_string()).collect();
        let whole: Value =
            serde_json::from_str(first.for_one(&wanted(&["claire"])).unwrap()).unwrap();
        assert_eq!(whole["ops"].as_array().unwrap().len(), 3);
        assert_eq!(
            whole["server_now_ms"], 7,
            "the clock is read when it is sent"
        );
        assert_eq!(
            first.for_one(&wanted(&["a1"])),
            first.for_one(&wanted(&["*"]))
        );
        let short: Value =
            serde_json::from_str(first.for_one(&wanted(&["mike"])).unwrap()).unwrap();
        let ops: Vec<&str> = short["ops"]
            .as_array()
            .unwrap()
            .iter()
            .map(|op| op["op"].as_str().unwrap())
            .collect();
        assert_eq!(ops, ["status", "turn"]);
        assert_eq!(short["lseq"], 4, "under the same number");
        assert_eq!(
            first.for_one(&wanted(&[])),
            first.for_one(&wanted(&["mike"]))
        );
        // An event with nothing for a list of agents is for those who follow the agent alone.
        let second = watching.try_recv().unwrap();
        assert!(second.for_one(&wanted(&["claire"])).is_some());
        assert_eq!(second.for_one(&wanted(&["mike"])), None);
        assert!(watching.try_recv().is_err());
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
