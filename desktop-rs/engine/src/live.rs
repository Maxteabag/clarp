//! Live items (docs/live-items.md §7) on the engine: the `live_items`
//! feature, the `/events?live=` subscription, `GET /live` snapshots and the
//! `live` events, one [`LiveView`] per chat. Without the feature nothing
//! here runs and chats keep to `/log` as before.

use clarp_core::json::{self, Object};
use clarp_core::live::{FETCH_LIVE, LiveView};
use serde_json::Value;

use crate::{Change, Engine};

pub(crate) fn now_ms() -> i64 {
    std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map_or(0, |d| d.as_millis() as i64)
}

/// The live state the engine holds across chats.
#[derive(Default)]
pub(crate) struct Live {
    /// `live_items` was in the last `/server-info`.
    pub enabled: bool,
    /// Per session: its view (created when the chat opens).
    pub views: std::collections::HashMap<String, LiveView>,
    /// The `live=` value the event stream was last opened with.
    pub subscribed: Option<String>,
    /// The stream dropped since it last connected: events were missed.
    pub dropped: bool,
}

impl Engine {
    /// The Host sends live items (`live_items` in `/server-info`).
    pub fn live_items(&self) -> bool {
        self.live.enabled
    }

    /// The chat's live state, once the feature is on and it was opened.
    pub fn live_view(&self, session: &str) -> Option<&LiveView> {
        self.live.views.get(session)
    }

    /// Live items stand in for the old activity rows in this chat.
    pub(crate) fn live_owns(&self, session: &str) -> bool {
        self.live.enabled && self.live.views.contains_key(session)
    }

    /// `/server-info` answered: whether the Host sends live items.
    pub(crate) fn live_server_info(&mut self, info: &Object) {
        let enabled = info
            .get("features")
            .and_then(Value::as_array)
            .is_some_and(|features| features.iter().any(|f| f.as_str() == Some("live_items")));
        if enabled != self.live.enabled {
            self.live.enabled = enabled;
            self.live.views.clear();
        }
        self.sync_live_subscription(false);
    }

    /// The Host or its token changed: nothing live is known any more.
    pub(crate) fn reset_live(&mut self) {
        self.live = Live::default();
        self.sse.set_query(Vec::new());
    }

    /// A chat opened: hold a view for it, ask for its snapshot once, and
    /// subscribe the stream to it.
    pub(crate) fn live_chat_opened(&mut self, session: &str) {
        if !self.live.enabled || session.is_empty() || session.starts_with("pair:") {
            return;
        }
        let fresh = !self.live.views.contains_key(session);
        let view = self.live.views.entry(session.to_owned()).or_default();
        if fresh || (view.lseq().is_none() && !view.awaiting_snapshot()) {
            self.request_live(session);
        }
        self.sync_live_subscription(true);
    }

    fn request_live(&mut self, session: &str) {
        if let Some(view) = self.live.views.get_mut(session) {
            view.expect_snapshot();
        }
        self.api.get(&format!("live:{session}"), "/live", &[("session", session)]);
    }

    /// The open and cached chats, as `/events?live=` names them; `live=`
    /// alone (status ops only) before any chat opened.
    fn live_sessions(&self) -> String {
        let mut sessions: Vec<&str> = self.live.views.keys().map(String::as_str).collect();
        sessions.sort_unstable();
        sessions.join(",")
    }

    /// Opens the stream with the sessions it should carry items for; a
    /// running stream reopens (resuming by event id) only when they changed.
    pub(crate) fn sync_live_subscription(&mut self, reopen: bool) {
        let wanted = self.live.enabled.then(|| self.live_sessions());
        if wanted == self.live.subscribed {
            return;
        }
        self.sse.set_query(wanted.iter().map(|s| ("live".to_owned(), s.clone())).collect());
        self.live.subscribed = wanted;
        if reopen && self.sse.running() {
            self.sse.resubscribe();
        }
    }

    /// The stream came back after a drop: events were missed, so every
    /// held chat asks for its snapshot again.
    pub(crate) fn live_reconnected(&mut self) {
        if !std::mem::take(&mut self.live.dropped) || !self.live.enabled {
            return;
        }
        // A view still waiting for its first snapshot needs no second one.
        let sessions: Vec<String> = self.live.views.iter().filter(|(_, v)| v.lseq().is_some()).map(|(s, _)| s.clone()).collect();
        for session in sessions {
            self.request_live(&session);
        }
    }

    pub(crate) fn live_stream_dropped(&mut self) {
        self.live.dropped = true;
    }

    /// A `live` event from the stream.
    pub(crate) fn live_event(&mut self, event: &Object) {
        if !self.live.enabled {
            return;
        }
        let session = json::string(event, "session");
        let Some(view) = self.live.views.get_mut(&session) else { return };
        let server_now = event.get("server_now_ms").and_then(Value::as_i64).unwrap_or(0);
        let before = view.generation();
        let effects = view.apply_event(event);
        if view.generation() != before {
            view.observe_clock(server_now, now_ms());
            self.changes.push(Change::Live(session.clone()));
        }
        if effects.contains(&FETCH_LIVE) {
            self.api.get(&format!("live:{session}"), "/live", &[("session", &session)]);
        }
    }

    /// Replies for `live:` tags; true when it was one.
    pub(crate) fn live_json(&mut self, tag: &str, object: &Object) -> bool {
        let Some(session) = tag.strip_prefix("live:") else { return false };
        if let Some(view) = self.live.views.get_mut(session) {
            view.apply_snapshot(object);
            view.observe_clock(object.get("server_now_ms").and_then(Value::as_i64).unwrap_or(0), now_ms());
            self.changes.push(Change::Live(session.to_owned()));
        }
        true
    }

    /// A failed `GET /live`: the chat keeps what it shows and the next event
    /// may ask again. Never a chat error (an older Host may lack the route).
    pub(crate) fn live_failure(&mut self, tag: &str, detail: &str) -> bool {
        let Some(session) = tag.strip_prefix("live:") else { return false };
        eprintln!("Engine: GET /live for {session} failed: {detail}");
        if let Some(view) = self.live.views.get_mut(session) {
            view.snapshot_failed();
        }
        true
    }
}
