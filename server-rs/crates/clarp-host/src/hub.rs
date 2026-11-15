//! The hub: every change the Host tells its clients about is numbered here and fanned out.
//!
//! An event is a wake-up, not the data: it says that something changed, and the client asks what.
//! That is why the hub may forget. It keeps the last few minutes so that a client that drops for
//! a moment misses nothing; a client that was away for longer, or across a restart of the Host,
//! is told to look at everything again, and the conversations themselves tell it what is new.

use std::collections::VecDeque;
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

pub struct Hub {
    recent: Mutex<Recent>,
    live: broadcast::Sender<Arc<Published>>,
}

impl Hub {
    /// A hub whose numbers start above every number a hub started earlier gave out: the time
    /// leads, so a restart never reuses one.
    pub fn new(now_ms: i64) -> Self {
        let (live, _) = broadcast::channel(BEHIND_MAX);
        let seconds = u64::try_from(now_ms / 1_000).unwrap_or(0);
        Self {
            recent: Mutex::new(Recent {
                next_id: (seconds << 20).max(1),
                events: VecDeque::new(),
            }),
            live,
        }
    }

    /// Numbers an event, keeps it for those who reconnect and sends it to those who listen.
    /// The event is the object a client will read; its number is added to it here.
    pub fn publish(&self, mut event: Value, now_ms: i64) -> u64 {
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
