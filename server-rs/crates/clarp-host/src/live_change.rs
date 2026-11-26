//! The chats an open event stream follows, changed without opening it again (feature
//! `events_live_change`, issue 892).
//!
//! `live=` is fixed when an `/events` stream opens, so an app that opens a chat it does not yet
//! follow used to tear its stream down and open another: a reconnect, a replay, and through the
//! relay a new tunnel. A stream that asks (`live_change=1`) is given a random id, its first
//! event; `POST /events/{id}/live` from the token that opened it changes what it follows, and the
//! stream says `live-changed` where the change takes effect.
//!
//! A stream that also shows chat previews (`chat_previews=1`, feature `events_chat_previews`,
//! issues 905 and 913) has a second set, the chats whose panes are on screen (`visible`): a
//! change sets both at once, and its `live-changed` is where the two take effect together.

use std::collections::{HashMap, HashSet};
use std::sync::{Arc, Mutex, PoisonError, Weak};

use tokio::sync::watch;

/// The chats a stream follows, as `live=` names them: sessions or agent ids, `*` for every one,
/// none for the status lines alone.
pub type Followed = Vec<String>;

/// What a change sets: the chats followed and, on a stream that shows previews, the chats on
/// screen. Both take effect at the same `live-changed`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Change {
    pub live: Followed,
    /// `None` on a stream that shows no previews.
    pub visible: Option<Followed>,
}

struct Open {
    /// Who opened it: only they may change it.
    owner: String,
    /// The chats on screen as last set, for a stream that shows previews: a change that names
    /// none keeps them.
    visible: Option<Followed>,
    changes: watch::Sender<Option<Change>>,
}

/// The streams open now that may be changed.
#[derive(Default)]
pub struct Streams {
    open: Mutex<HashMap<String, Open>>,
}

/// Holds a stream's place among the open ones; dropped with the stream.
pub struct Held {
    pub id: String,
    pub changes: watch::Receiver<Option<Change>>,
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

/// What a change says of the chats on screen, as its body has it. Only a stream that shows
/// previews reads it; any other stream ignores it, whatever it is.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Visible {
    /// The body names none: the chats on screen stay as they were.
    Kept,
    /// The chats on screen from now on (none clears the screen).
    Set(Followed),
    /// The body names something that is no list of chats (`null` included).
    NotAList,
}

/// What a request to change a stream found.
#[derive(Debug, PartialEq, Eq)]
pub enum Changed {
    /// The stream will follow these from its `live-changed` on.
    Taken(Change),
    /// No stream of this id was opened by this owner (it ended, or it is someone else's).
    Unknown,
    /// The owner's stream shows previews and the change's `visible` is no list: nothing changed.
    NotAList,
}

impl Streams {
    /// Opens a stream for `owner` under a new random id; `visible` the chats on screen it
    /// opened with, for a stream that shows previews.
    pub fn open(self: &Arc<Self>, owner: String, visible: Option<Followed>) -> Held {
        let (changes, heard) = watch::channel(None);
        let mut open = self.open.lock().unwrap_or_else(PoisonError::into_inner);
        let id = loop {
            let id = crate::ids::hex(16);
            if !open.contains_key(&id) {
                break id;
            }
        };
        open.insert(
            id.clone(),
            Open {
                owner,
                visible,
                changes,
            },
        );
        Held {
            id,
            changes: heard,
            streams: Arc::downgrade(self),
        }
    }

    /// Has the stream `id` of `owner` follow `live` from now on and, on a stream that shows
    /// previews, show `visible` whole (one that names none keeps the chats on screen it had).
    /// Whose stream it is is judged first; `visible` only then, and only on a stream that shows
    /// previews: any other stream ignores it, malformed or not. The newest change wins: a stream
    /// that has not yet taken one takes only the last, both sets of it together.
    pub fn change(&self, id: &str, owner: &str, live: Followed, visible: Visible) -> Changed {
        let mut open = self.open.lock().unwrap_or_else(PoisonError::into_inner);
        match open.get_mut(id) {
            Some(stream) if stream.owner == owner => {
                if let Some(held) = stream.visible.as_mut() {
                    match visible {
                        Visible::Kept => {}
                        Visible::Set(visible) => *held = visible,
                        Visible::NotAList => return Changed::NotAList,
                    }
                }
                let change = Change {
                    live,
                    visible: stream.visible.clone(),
                };
                stream.changes.send_replace(Some(change.clone()));
                Changed::Taken(change)
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
        let mut held = streams.open("device-a".into(), None);
        assert_eq!(held.id.len(), 32);
        let other = streams.open("device-a".into(), None);
        assert_ne!(held.id, other.id);
        assert_eq!(
            streams.change(&held.id, "device-b", vec!["mike".into()], Visible::NotAList),
            Changed::Unknown,
            "another token's request finds no stream"
        );
        assert!(!held.changes.has_changed().unwrap());
        let live = |names: &[&str]| Change {
            live: names.iter().map(|name| (*name).to_string()).collect(),
            visible: None,
        };
        assert_eq!(
            streams.change(&held.id, "device-a", vec!["rachel".into()], Visible::Kept),
            Changed::Taken(live(&["rachel"]))
        );
        // A stream without previews has no chats on screen, whatever a change says, a malformed
        // `visible` included.
        streams.change(
            &held.id,
            "device-a",
            vec!["rachel".into()],
            Visible::Set(vec!["rachel".into()]),
        );
        streams.change(
            &held.id,
            "device-a",
            vec!["rachel".into(), "mike".into()],
            Visible::NotAList,
        );
        assert!(held.changes.has_changed().unwrap());
        assert_eq!(
            held.changes.borrow_and_update().clone(),
            Some(live(&["rachel", "mike"])),
            "the newest change wins"
        );
        let id = held.id.clone();
        drop(held);
        assert_eq!(
            streams.change(&id, "device-a", vec![], Visible::Kept),
            Changed::Unknown
        );
        assert_eq!(
            streams.change(&other.id, "device-a", vec![], Visible::NotAList),
            Changed::Taken(live(&[]))
        );
    }

    #[test]
    fn a_stream_with_previews_takes_both_sets_together_and_keeps_its_screen_when_none_is_named() {
        let streams = Arc::new(Streams::default());
        let mut held = streams.open("device-a".into(), Some(vec!["rachel".into()]));
        let both = |live: &[&str], visible: &[&str]| Change {
            live: live.iter().map(|name| (*name).to_string()).collect(),
            visible: Some(visible.iter().map(|name| (*name).to_string()).collect()),
        };
        assert_eq!(
            streams.change(&held.id, "device-a", vec!["mike".into()], Visible::Kept),
            Changed::Taken(both(&["mike"], &["rachel"])),
            "a change that names no screen keeps the one it had"
        );
        held.changes.borrow_and_update();
        // Another owner is told no such stream, before its `visible` is looked at.
        assert_eq!(
            streams.change(&held.id, "device-b", vec![], Visible::NotAList),
            Changed::Unknown
        );
        // A `visible` that is no list changes nothing.
        assert_eq!(
            streams.change(&held.id, "device-a", vec!["zed".into()], Visible::NotAList),
            Changed::NotAList
        );
        assert!(!held.changes.has_changed().unwrap(), "nothing was sent");
        assert_eq!(
            streams.change(&held.id, "device-a", vec!["mike".into()], Visible::Kept),
            Changed::Taken(both(&["mike"], &["rachel"])),
            "the refused change left the screen as it was"
        );
        streams.change(
            &held.id,
            "device-a",
            vec!["mike".into()],
            Visible::Set(vec!["mike".into()]),
        );
        streams.change(&held.id, "device-a", vec![], Visible::Set(vec![]));
        assert_eq!(
            held.changes.borrow_and_update().clone(),
            Some(both(&[], &[])),
            "rapid changes collapse to the newest, both sets of it"
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
