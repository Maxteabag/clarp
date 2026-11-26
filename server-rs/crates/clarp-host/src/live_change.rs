//! The chats an open event stream follows, changed without opening it again (feature
//! `events_live_change`, issue 892).
//!
//! `live=` is fixed when an `/events` stream opens, so an app that opens a chat it does not yet
//! follow used to tear its stream down and open another: a reconnect, a replay, and through the
//! relay a new tunnel. A stream that asks (`live_change=1`) is given a random id, its first
//! event; `POST /events/{id}/live` from the token that opened it changes what it follows, and the
//! stream says `live-changed` where the change takes effect.

use std::collections::{HashMap, HashSet};
use std::sync::{Arc, Mutex, PoisonError, Weak};

use tokio::sync::watch;

/// The chats a stream follows, as `live=` names them: sessions or agent ids, `*` for every one,
/// none for the status lines alone.
pub type Followed = Vec<String>;

struct Open {
    /// Who opened it: only they may change it.
    owner: String,
    changes: watch::Sender<Option<Followed>>,
}

/// The streams open now that may be changed.
#[derive(Default)]
pub struct Streams {
    open: Mutex<HashMap<String, Open>>,
}

/// Holds a stream's place among the open ones; dropped with the stream.
pub struct Held {
    pub id: String,
    pub changes: watch::Receiver<Option<Followed>>,
    streams: Weak<Streams>,
}

impl Drop for Held {
    fn drop(&mut self) {
        if let Some(streams) = self.streams.upgrade() {
            let mut open = streams.open.lock().unwrap_or_else(PoisonError::into_inner);
            open.remove(&self.id);
        }
    }
}

/// What a request to change a stream found.
#[derive(Debug, PartialEq, Eq)]
pub enum Changed {
    /// The stream will follow these from its `live-changed` on.
    Taken(Followed),
    /// No stream of this id was opened by this owner (it ended, or it is someone else's).
    Unknown,
}

impl Streams {
    /// Opens a stream for `owner` under a new random id.
    pub fn open(self: &Arc<Self>, owner: String) -> Held {
        let (changes, heard) = watch::channel(None);
        let mut open = self.open.lock().unwrap_or_else(PoisonError::into_inner);
        let id = loop {
            let id = crate::ids::hex(16);
            if !open.contains_key(&id) {
                break id;
            }
        };
        open.insert(id.clone(), Open { owner, changes });
        Held {
            id,
            changes: heard,
            streams: Arc::downgrade(self),
        }
    }

    /// Has the stream `id` of `owner` follow `live` from now on. The newest change wins: a
    /// stream that has not yet taken one takes only the last.
    pub fn change(&self, id: &str, owner: &str, live: Followed) -> Changed {
        let open = self.open.lock().unwrap_or_else(PoisonError::into_inner);
        match open.get(id) {
            Some(stream) if stream.owner == owner => {
                stream.changes.send_replace(Some(live.clone()));
                Changed::Taken(live)
            }
            _ => Changed::Unknown,
        }
    }
}

/// The chats a `live=` value or a change names, as a stream holds them.
pub fn followed<'a>(names: impl IntoIterator<Item = &'a str>) -> HashSet<String> {
    names
        .into_iter()
        .map(str::trim)
        .filter(|name| !name.is_empty())
        .map(str::to_string)
        .collect()
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use super::*;

    #[test]
    fn only_the_owner_changes_an_open_stream_and_a_closed_one_is_gone() {
        let streams = Arc::new(Streams::default());
        let mut held = streams.open("device-a".into());
        assert_eq!(held.id.len(), 32);
        let other = streams.open("device-a".into());
        assert_ne!(held.id, other.id);
        assert_eq!(
            streams.change(&held.id, "device-b", vec!["mike".into()]),
            Changed::Unknown,
            "another token's request finds no stream"
        );
        assert!(!held.changes.has_changed().unwrap());
        assert_eq!(
            streams.change(&held.id, "device-a", vec!["rachel".into()]),
            Changed::Taken(vec!["rachel".into()])
        );
        streams.change(&held.id, "device-a", vec!["rachel".into(), "mike".into()]);
        assert!(held.changes.has_changed().unwrap());
        assert_eq!(
            held.changes.borrow_and_update().clone(),
            Some(vec!["rachel".into(), "mike".into()]),
            "the newest change wins"
        );
        let id = held.id.clone();
        drop(held);
        assert_eq!(streams.change(&id, "device-a", vec![]), Changed::Unknown);
        assert_eq!(
            streams.change(&other.id, "device-a", vec![]),
            Changed::Taken(vec![])
        );
    }

    #[test]
    fn a_followed_set_is_named_as_live_names_it() {
        assert_eq!(
            followed([" rachel", "", "mike ", "rachel"]),
            ["rachel".to_string(), "mike".to_string()].into()
        );
        assert!(followed([""]).is_empty());
    }
}
