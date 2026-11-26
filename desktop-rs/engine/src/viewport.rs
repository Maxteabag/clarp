//! What the screen shows, told to the Host (#905, on #892's
//! `events_live_change`): a Host that lists both `events_live_change` and
//! `events_chat_previews` streams a chat's full frames only while the chat
//! is followed live and shown in a pane, and keeps every other row of the
//! chat list current with coalesced `agent-roster` `kind:"preview"` patches.
//!
//! - The stream opens with
//!   `/events?live_change=1&chat_previews=1&live=<followed>&visible=<shown>`
//!   (an empty `visible=` asks for previews only) and first names itself
//!   with a `stream` event `{stream_id}`.
//! - When the panes change, `POST /events/{stream_id}/live` carries both
//!   whole sets, `visible: []` once the last pane closed. One is in flight
//!   at a time; changes meanwhile collapse into the next one.
//! - The stream's `live-changed` event, not the POST's answer, is where the
//!   Host applies the sets. A chat that becomes fully streamed there reads
//!   its `GET /live` (events after the boundary wait for it, by lseq) and its
//!   `/log` from its own cached revision.
//! - A chat no pane shows reads no history: every `/log` read for it waits
//!   until it is shown again (in the strongest mode asked meanwhile), except
//!   while one of its sends waits for its receipt.
//! - A preview patches the chat list row only. Its `head_revision` is the
//!   row's, never the timeline's cursor, and it never reads `/log`.
//!
//! A Host without both features keeps the stream exactly as before: `live=`
//! only, reopened when the followed chats change.

use std::collections::{BTreeSet, HashMap};

use clarp_core::json::{self, Object};
use clarp_core::roster::RowChange;
use serde_json::{Value, json};

use crate::{Change, Engine};

pub(crate) const LIVE_CHANGE: &str = "events_live_change";
pub(crate) const PREVIEWS: &str = "events_chat_previews";
const TAG: &str = "events-live:";

/// The fields a preview may carry: the snapshot's preview projection.
const PREVIEW_FIELDS: [&str; 9] = [
    "conversation_id", "head_revision", "last_message_id", "last_message", "busy", "latest_state", "latest_state_ts", "status_text",
    "last_activity",
];

/// The followed chats and the shown ones, as the wire names them.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub(crate) struct Sets {
    pub live: BTreeSet<String>,
    pub visible: BTreeSet<String>,
}

impl Sets {
    /// The chats streamed in full: followed and shown.
    pub fn full(&self) -> BTreeSet<String> {
        self.live.intersection(&self.visible).cloned().collect()
    }

    fn joined(set: &BTreeSet<String>) -> String {
        set.iter().map(String::as_str).collect::<Vec<_>>().join(",")
    }

    /// The `/events` query that opens a stream with these sets.
    pub fn query(&self, gzip: bool) -> Vec<(String, String)> {
        let mut query = vec![
            ("live_change".to_owned(), "1".to_owned()),
            ("chat_previews".to_owned(), "1".to_owned()),
            ("live".to_owned(), Self::joined(&self.live)),
            ("visible".to_owned(), Self::joined(&self.visible)),
        ];
        if gzip {
            query.push(("gzip".to_owned(), "1".to_owned()));
        }
        query
    }

    /// The sets a `live-changed` event (or a `stream` event's own fields)
    /// names; `visible` falls back to `fallback` when the event has none.
    pub fn from_event(event: &Object, fallback: &BTreeSet<String>) -> Option<Self> {
        let list = |key: &str| -> Option<BTreeSet<String>> {
            event.get(key).and_then(Value::as_array).map(|a| a.iter().filter_map(Value::as_str).filter(|s| !s.is_empty()).map(str::to_owned).collect())
        };
        Some(Self { live: list("live")?, visible: list("visible").unwrap_or_else(|| fallback.clone()) })
    }

    /// The sets an `/events` query opened the stream with.
    pub fn from_query(query: &Object) -> Self {
        let split = |key: &str| -> BTreeSet<String> {
            query.get(key).and_then(Value::as_str).unwrap_or_default().split(',').filter(|s| !s.is_empty()).map(str::to_owned).collect()
        };
        Self { live: split("live"), visible: split("visible") }
    }
}

/// How a `/log` read that waits for its chat to be shown is made.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub(crate) enum Read {
    Delta,
    Tail,
    Replace,
}

#[derive(Default)]
pub(crate) struct Viewport {
    /// The Host lists both features (and live items): this stream mode is on.
    pub on: bool,
    /// The stream now open, as its `stream` event named it.
    pub stream_id: Option<String>,
    /// The sets the Host applies now: from the stream's opening or its last
    /// `live-changed`. Kept across a drop (the next stream says anew).
    pub effective: Option<Sets>,
    /// What the Host was last asked for on this stream (opened with or
    /// posted); a change is posted only when the wanted sets differ.
    pub sent: Option<Sets>,
    /// The POST on its way (its number), if any.
    pub in_flight: Option<u64>,
    seq: u64,
    /// The query the stream is opened with from its next connection on.
    pub query: Vec<(String, String)>,
    /// Chats no pane shows whose `/log` read waits for them to be shown.
    pub deferred: HashMap<String, Read>,
}

/// Of the chats in `next`'s full set, those that just became fully
/// streamed: not in `before`'s.
pub(crate) fn promoted(before: Option<&Sets>, next: &Sets) -> Vec<String> {
    let old = before.map(Sets::full).unwrap_or_default();
    next.full().into_iter().filter(|s| !old.contains(s)).collect()
}

/// Only the preview projection's fields, and of those none older than what
/// the row holds: a state older than the row's, or a head behind its own on
/// the same conversation, is left out.
pub(crate) fn preview_fields(fields: &Object, row: Option<&clarp_core::protocol::Agent>) -> Object {
    let mut kept: Object = fields.iter().filter(|(k, _)| PREVIEW_FIELDS.contains(&k.as_str())).map(|(k, v)| (k.clone(), v.clone())).collect();
    let Some(row) = row else { return kept };
    let state_ts = kept.get("latest_state_ts").and_then(Value::as_i64);
    if state_ts.is_some_and(|ts| ts < row.latest_state_timestamp) {
        for key in ["latest_state_ts", "latest_state", "busy", "status_text"] {
            kept.remove(key);
        }
    }
    let conversation = kept.get("conversation_id").and_then(Value::as_str).unwrap_or(&row.conversation_id);
    let head = kept.get("head_revision").and_then(Value::as_i64);
    if conversation == row.conversation_id && head.is_some_and(|h| h < row.head_revision) {
        for key in ["head_revision", "last_message", "last_message_id"] {
            kept.remove(key);
        }
    }
    if kept.get("last_activity").and_then(Value::as_i64).is_some_and(|at| at < row.last_activity) {
        kept.remove("last_activity");
    }
    kept
}

fn has(info: &Object, name: &str) -> bool {
    info.get("capabilities")
        .and_then(|c| c.get("features"))
        .and_then(Value::as_array)
        .is_some_and(|features| features.iter().any(|f| f.as_str() == Some(name)))
}

impl Engine {
    /// Whether the stream follows the panes (both features, live items on).
    pub fn viewport_streaming(&self) -> bool {
        self.live.enabled && self.live.viewport.on
    }

    /// The sets the Host applies to this stream now (tests and checks).
    pub fn viewport_effective(&self) -> Option<(Vec<String>, Vec<String>)> {
        self.live.viewport.effective.as_ref().map(|s| (s.live.iter().cloned().collect(), s.visible.iter().cloned().collect()))
    }

    /// `/server-info` answered: whether this Host follows the panes.
    pub(crate) fn viewport_server_info(&mut self, info: &Object) {
        let on = has(info, LIVE_CHANGE) && has(info, PREVIEWS);
        if on != self.live.viewport.on {
            self.live.viewport = crate::viewport::Viewport { on, ..Default::default() };
        }
    }

    /// The chats a pane shows on this engine's Host, as `visible=` names
    /// them: agent chats only (a pair room is read as before).
    /// Runs once per pump, so it reads only the active workspace's panes
    /// (the ones `view_layout` marks shown), not every saved workspace.
    fn visible_chats(&self) -> BTreeSet<String> {
        let mut shown: BTreeSet<String> = if self.peer {
            self.live.shown.iter().cloned().collect()
        } else {
            let zoomed = self.panes.tree.zoomed_pane_id();
            self.panes
                .tree
                .pane_layout()
                .iter()
                .filter(|pane| zoomed.is_empty() || pane.get("id").and_then(Value::as_str) == Some(zoomed))
                .filter_map(|pane| pane.get("session").and_then(Value::as_str).map(str::to_owned))
                .collect()
        };
        if !self.selected.is_empty() {
            shown.insert(self.selected.clone());
        }
        shown.retain(|s| !s.is_empty() && !s.starts_with("pair:") && clarp_core::all_hosts::split(s).is_none());
        shown
    }

    /// The sets the screen wants now.
    pub(crate) fn wanted_sets(&self) -> Sets {
        Sets { live: self.live.views.keys().filter(|s| !s.starts_with("pair:")).cloned().collect(), visible: self.visible_chats() }
    }

    /// The chats streamed in full now.
    fn full_now(&self) -> BTreeSet<String> {
        self.live.viewport.effective.as_ref().map(Sets::full).unwrap_or_default()
    }

    /// Whether `session` is streamed in full now (a `live` event for it counts).
    pub(crate) fn viewport_full(&self, session: &str) -> bool {
        self.live.viewport.effective.as_ref().is_some_and(|s| s.live.contains(session) && s.visible.contains(session))
    }

    /// Keeps the stream's opening query on the wanted sets, so a reconnect
    /// opens with what the screen shows now. Never reopens a running stream.
    pub(crate) fn viewport_query(&mut self) {
        let query = self.wanted_sets().query(self.live.events_gzip);
        if query != self.live.viewport.query {
            self.sse.set_query(query.clone());
            self.live.viewport.query = query;
        }
    }

    /// Once per pump: posts the wanted sets when they differ from what the
    /// Host was last asked, one POST at a time.
    pub(crate) fn sync_viewport(&mut self) {
        if !self.viewport_streaming() {
            return;
        }
        self.viewport_query();
        let Some(stream) = self.live.viewport.stream_id.clone() else { return };
        let wanted = self.wanted_sets();
        let viewport = &mut self.live.viewport;
        if viewport.in_flight.is_some() || viewport.sent.as_ref() == Some(&wanted) {
            return;
        }
        viewport.seq += 1;
        viewport.in_flight = Some(viewport.seq);
        let body = json!({"live": wanted.live.iter().collect::<Vec<_>>(), "visible": wanted.visible.iter().collect::<Vec<_>>()});
        viewport.sent = Some(wanted);
        let path = format!("/events/{}/live", clarp_core::endpoint::percent_encode_segment(&stream));
        self.api.post_json(&format!("{TAG}{}:{stream}", viewport.seq), &path, body, None);
    }

    /// The POST's answer; true when the tag was one. The sets take effect at
    /// the stream's `live-changed`, so this only frees the next change.
    pub(crate) fn viewport_json(&mut self, tag: &str) -> bool {
        let Some((seq, stream)) = tag.strip_prefix(TAG).and_then(|rest| rest.split_once(':')) else { return false };
        let viewport = &mut self.live.viewport;
        if viewport.stream_id.as_deref() == Some(stream) && viewport.in_flight.is_some_and(|n| n.to_string() == seq) {
            viewport.in_flight = None;
        }
        true
    }

    /// A refused or lost POST: the stream is opened again with the wanted
    /// sets (the opener is what the Host follows then). An answer for a
    /// stream already replaced changes nothing.
    pub(crate) fn viewport_failure(&mut self, tag: &str) -> bool {
        let Some((seq, stream)) = tag.strip_prefix(TAG).and_then(|rest| rest.split_once(':')) else { return false };
        let viewport = &mut self.live.viewport;
        if viewport.stream_id.as_deref() != Some(stream) || !viewport.in_flight.is_some_and(|n| n.to_string() == seq) {
            return true;
        }
        viewport.in_flight = None;
        viewport.stream_id = None;
        viewport.sent = None;
        self.viewport_query();
        if self.sse.running() {
            self.sse.resubscribe();
        }
        true
    }

    /// The stream dropped: it is no longer the one to post to.
    pub(crate) fn viewport_dropped(&mut self) {
        let viewport = &mut self.live.viewport;
        viewport.stream_id = None;
        viewport.in_flight = None;
        viewport.sent = None;
    }

    /// A `stream` event: a new stream, applying the sets it was opened with.
    pub(crate) fn viewport_stream(&mut self, event: &Object) {
        if !self.viewport_streaming() {
            return;
        }
        let stream_id = json::string(event, "stream_id");
        if stream_id.is_empty() {
            return;
        }
        let opened = event.get("opened_query").and_then(Value::as_object).map(Sets::from_query).unwrap_or_else(|| self.wanted_sets());
        let sets = Sets::from_event(event, &opened.visible).unwrap_or(opened);
        let viewport = &mut self.live.viewport;
        viewport.stream_id = Some(stream_id);
        viewport.in_flight = None;
        viewport.sent = Some(sets.clone());
        self.viewport_boundary(sets, true);
    }

    /// A `live-changed` event on this stream: where the sets take effect.
    pub(crate) fn viewport_changed(&mut self, event: &Object) {
        if !self.viewport_streaming() {
            return;
        }
        let Some(current) = self.live.viewport.stream_id.clone() else { return };
        let named = json::string(event, "stream_id");
        if !named.is_empty() && named != current {
            return;
        }
        let fallback = self.live.viewport.sent.as_ref().map(|s| s.visible.clone()).unwrap_or_default();
        let Some(sets) = Sets::from_event(event, &fallback) else { return };
        self.viewport_boundary(sets, false);
    }

    /// The Host applies `sets` from here on. Each chat that just became
    /// fully streamed, and each shown chat whose read waited, catches up.
    fn viewport_boundary(&mut self, sets: Sets, new_stream: bool) {
        let mut promote = promoted(self.live.viewport.effective.as_ref(), &sets);
        let full = sets.full();
        for session in self.live.viewport.deferred.keys() {
            if full.contains(session) && !promote.contains(session) {
                promote.push(session.clone());
            }
        }
        self.live.viewport.effective = Some(sets);
        self.live.subscribed = Some(Sets::joined(&full));
        for session in promote {
            self.promote(&session, new_stream);
        }
    }

    /// A chat now streamed in full: its live state from `GET /live` (events
    /// after the boundary wait for it), its history from its own revision.
    fn promote(&mut self, session: &str, new_stream: bool) {
        if self.live.views.get(session).is_some_and(|v| !v.awaiting_snapshot()) {
            self.request_live(session);
        }
        let read = self.live.viewport.deferred.remove(session);
        if !self.conversations.contains_key(session) && read.is_none() {
            // Never opened here: opening it reads it.
            return;
        }
        if new_stream && read.is_none() && self.log_in_flight.contains(session) {
            // A read made for this stream's opening is on its way; the
            // snapshot read on connecting catches anything after it.
            return;
        }
        match read {
            Some(Read::Replace) => self.request_tail(session, true),
            Some(Read::Tail) => self.request_tail(session, false),
            _ => self.catch_up_shown(session),
        }
    }

    /// A shown chat reads what it missed: a new conversation (the row's
    /// conversation is not the timeline's) or nothing held reads the tail,
    /// a cached copy reads the delta after its own revision.
    fn catch_up_shown(&mut self, session: &str) {
        self.ensure_conversation(session);
        let Some(conversation) = self.conversations.get(session) else { return };
        let row = self.roster.find(session).map(|a| a.conversation_id.as_str()).unwrap_or_default();
        let other = !row.is_empty() && !conversation.conversation_id().is_empty() && row != conversation.conversation_id();
        if other || conversation.latest_revision() <= 0 {
            self.request_tail(session, false);
        } else {
            self.catching_up.insert(session.to_owned());
            self.request_delta(session);
        }
    }

    /// Whether a `/log` read of `session` is made now. Without the stream
    /// mode, always. With it, only for a chat streamed in full, a pair room
    /// a pane shows, or a chat with a send waiting for its receipt; any
    /// other read is kept (the strongest asked) for when the chat is shown.
    pub(crate) fn history_now(&mut self, session: &str, read: Read) -> bool {
        if !self.viewport_streaming() || session.is_empty() {
            return true;
        }
        let now = if session.starts_with("pair:") {
            self.kept_watched().contains(session)
        } else {
            self.viewport_full(session) || self.deliveries.values().any(|(s, _)| s == session)
        };
        if now {
            return true;
        }
        let entry = self.live.viewport.deferred.entry(session.to_owned()).or_insert(read);
        *entry = (*entry).max(read);
        false
    }

    /// A read that goes ahead takes over one that waited, in the stronger
    /// of the two modes.
    pub(crate) fn take_deferred(&mut self, session: &str) -> Option<Read> {
        self.live.viewport.deferred.remove(session)
    }

    /// A `kind:"preview"` roster patch: the chat list row changes, and
    /// nothing else (no `/log`, no timeline cursor, no unread mark).
    pub(crate) fn preview_event(&mut self, event: &Object) {
        let session = json::string(event, "session");
        let Some(fields) = event.get("fields").and_then(Value::as_object) else { return };
        if session.is_empty() {
            return;
        }
        let held = self.roster.find(&session).cloned().or_else(|| self.archived.find(&session).cloned());
        let fields = preview_fields(fields, held.as_ref());
        if fields.is_empty() {
            return;
        }
        let mut change = self.mutate_roster(|r| r.patch_row(&session, &fields));
        if change == RowChange::NotHeld {
            change = self.archived.patch_row(&session, &fields);
            if !self.archived.take_ops().is_empty() {
                self.changes.push(Change::Archive);
            }
        }
        match change {
            RowChange::Applied => {
                // Pair rooms count their own unread from their list.
                if !self.rooms.is_empty() && fields.contains_key("head_revision") {
                    self.schedule_agent_conversations();
                }
            }
            RowChange::NotHeld => self.read_row(&session),
            RowChange::Moved => self.request_snapshot(),
        }
    }

    /// A compacted catch-up that could not reach back far enough: the shown
    /// chats read what they missed (the roster reads its snapshot anyway).
    pub(crate) fn viewport_resync(&mut self, event: &Object) {
        if !self.viewport_streaming() || !json::boolean(event, "resync") || !json::boolean(event, "truncated") {
            return;
        }
        for session in self.full_now() {
            self.promote(&session, false);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn set(list: &[&str]) -> BTreeSet<String> {
        list.iter().map(|s| (*s).to_owned()).collect()
    }

    fn sets(live: &[&str], visible: &[&str]) -> Sets {
        Sets { live: set(live), visible: set(visible) }
    }

    fn object(value: Value) -> Object {
        value.as_object().cloned().unwrap()
    }

    #[test]
    fn the_query_names_both_sets_and_an_empty_visible_set_explicitly() {
        let query = sets(&["b", "a"], &[]).query(true);
        assert_eq!(
            query,
            [("live_change", "1"), ("chat_previews", "1"), ("live", "a,b"), ("visible", ""), ("gzip", "1")]
                .map(|(k, v)| (k.to_owned(), v.to_owned()))
        );
    }

    #[test]
    fn full_frames_are_the_followed_chats_a_pane_shows() {
        assert_eq!(sets(&["a", "b", "c"], &["b", "c", "d"]).full(), set(&["b", "c"]));
        assert!(sets(&["a", "b"], &[]).full().is_empty(), "list only: previews only");
    }

    #[test]
    fn a_chat_is_promoted_once_when_it_becomes_fully_streamed() {
        let before = sets(&["a", "b"], &["a"]);
        assert_eq!(promoted(Some(&before), &sets(&["a", "b"], &["a", "b"])), ["b"]);
        assert!(promoted(Some(&before), &sets(&["a", "b"], &["a"])).is_empty(), "the same sets again");
        assert!(promoted(Some(&before), &sets(&["a", "b"], &[])).is_empty(), "hiding promotes nothing");
        assert_eq!(promoted(None, &sets(&["a", "b"], &["a", "b"])), ["a", "b"], "a first stream promotes every shown chat");
    }

    #[test]
    fn sets_come_from_the_event_or_the_opening_query() {
        let event = object(json!({"type": "live-changed", "live": ["a", "b"], "visible": ["b"]}));
        assert_eq!(Sets::from_event(&event, &set(&[])), Some(sets(&["a", "b"], &["b"])));
        let old = object(json!({"type": "live-changed", "live": ["a"]}));
        assert_eq!(Sets::from_event(&old, &set(&["a"])), Some(sets(&["a"], &["a"])), "no visible: the one asked for");
        assert_eq!(Sets::from_event(&object(json!({"type": "stream", "stream_id": "s"})), &set(&[])), None);
        let query = object(json!({"live_change": "1", "live": "a,b", "visible": ""}));
        assert_eq!(Sets::from_query(&query), sets(&["a", "b"], &[]));
    }

    #[test]
    fn a_preview_carries_only_its_projection_and_never_moves_a_row_back() {
        let mut row = clarp_core::protocol::Agent::from_json(&object(json!({
            "session": "a", "conversation_id": "c1", "head_revision": 10, "latest_state_ts": 500, "last_activity": 900,
        })));
        row.last_message = "new".into();
        let fields = object(json!({"conversation_id": "c1", "head_revision": 8, "last_message": "old", "latest_state_ts": 400,
            "latest_state": "idle", "busy": false, "status_text": "x", "last_activity": 800, "persona": "Not mine", "unread": true}));
        let kept = preview_fields(&fields, Some(&row));
        assert_eq!(kept, object(json!({"conversation_id": "c1"})), "older values and other fields are left out");
        let newer = object(json!({"head_revision": 12, "last_message": "newer", "latest_state_ts": 600, "busy": true}));
        assert_eq!(preview_fields(&newer, Some(&row)), newer);
        let moved = object(json!({"conversation_id": "c2", "head_revision": 1, "last_message": "first"}));
        assert_eq!(preview_fields(&moved, Some(&row)), moved, "a new conversation starts its own count");
    }
}
