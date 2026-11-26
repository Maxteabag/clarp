//! Live items (docs/live-items.md §7) on the engine: the `live_items`
//! feature, the `/events?live=` subscription, `GET /live` snapshots and the
//! `live` events, one [`LiveView`] per chat. Without the feature nothing
//! here runs and chats keep to `/log` as before.

use clarp_core::json::{self, Object};
use clarp_core::live::{FETCH_LIVE, LiveView};
use serde_json::Value;

use crate::{Change, Engine};

/// Chats watched live besides the selected one and those shown in a pane:
/// the ones opened last. A chat further back is let go, so the Host does not
/// count it as open on the owner's device (`docs/lanes.md`); opened again,
/// it is watched again.
const MAX_WATCHED: usize = 4;
/// How many chats the order they were opened in is remembered for.
const OPENED_REMEMBERED: usize = 64;

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
    /// The chats in the order they were opened, the last at the back. Kept
    /// when the stream starts anew, so the chats are opened again in it.
    pub opened: std::collections::VecDeque<String>,
    /// A peer's engine: the chats the primary shows from this Host in its
    /// panes, never let go while shown. The primary reads its own panes.
    pub shown: std::collections::HashSet<String>,
    /// The `live=` value the event stream was last opened with.
    pub subscribed: Option<String>,
    /// The stream dropped since it last connected: events were missed.
    pub dropped: bool,
    /// Chats added to the stream by the last resubscribe: their events
    /// before it reopened were not sent, so they fetch once it has.
    pub joining: std::collections::HashSet<String>,
    /// The Host has the tool-explanation setting (`tool_explanation_setting`).
    pub explanation_setting: bool,
    /// `/log` rows carry turn summaries (`log_turn_summary`).
    pub turn_summaries: bool,
    /// A user row read into another turn names it (`read_into`, contract 189).
    pub read_into: bool,
    /// The Host packs `/events` with gzip on request (`events_gzip`, contract
    /// 198): the stream asks for it with `gzip=1`, and the HTTP client
    /// (which sends `Accept-Encoding: gzip`) unpacks it as it arrives.
    pub events_gzip: bool,
    /// The Host's clock minus this machine's, from the last
    /// `server_now_ms` any chat's live items carried (0 until one did).
    pub host_offset_ms: i64,
    /// Whether the stream's query carries `gzip=1` now.
    gzip_asked: bool,
    /// A turn that did not complete says why on its end (`turn_end_reason`).
    pub turn_reasons: bool,
    /// The Host speaks an answer only when its send asks for it
    /// (`spoken_when_asked`, contract 173).
    pub spoken_when_asked: bool,
    /// The Host's tool-explanation setting (§6), once it said.
    pub explanations: Option<Object>,
    /// Chats a live event (or a snapshot with something in it) has reached:
    /// only these are shown from live items. A Host listing the feature
    /// before its live hub serves sends neither, and those chats keep the
    /// old activity rows and typing indicator.
    pub heard: std::collections::HashSet<String>,
    /// `GET /live` requests sent and not yet answered, per chat: a catch-up
    /// round counts a chat's `/live` as read only once none is outstanding,
    /// so an answer to a request from before the round cannot end it.
    pub outstanding: std::collections::HashMap<String, u32>,
}

impl Engine {
    /// The Host sends live items (`live_items` in `/server-info`).
    pub fn live_items(&self) -> bool {
        self.live.enabled
    }

    /// The `live=` value the event stream was last opened with.
    pub fn live_subscription(&self) -> Option<&str> {
        self.live.subscribed.as_deref()
    }

    /// Whether `/log` rows carry the turn summaries finished turns fold by
    /// (`log_turn_summary`, docs/live-items.md §9).
    pub fn log_turn_summary(&self) -> bool {
        self.live.turn_summaries
    }

    /// Whether a user row read into another turn names that turn
    /// (`read_into`, Rust Host contract 189): history splits a turn at
    /// exactly those rows.
    pub fn read_into(&self) -> bool {
        self.live.read_into
    }

    /// Asks the Host to speak the answer a send starts as the voice setting
    /// says (`synthesize_audio`), where the Host offers `spoken_when_asked`:
    /// form submits, Send now, a reworded queued turn and decision answers.
    /// An older Host gets the request unchanged.
    pub(crate) fn ask_voice(&self, body: &mut Value) {
        if self.live.spoken_when_asked
            && let Some(object) = body.as_object_mut()
        {
            object.insert("synthesize_audio".into(), Value::Bool(!self.muted));
        }
    }

    /// Whether a turn that did not complete says why on its end
    /// (`turn_end_reason`, Rust Host contract 162).
    /// The chat's own Host decides, for a chat of another saved Host too.
    pub fn turn_end_reason(&self, session: &str) -> bool {
        on_peer!(self.peer_ref(session), |engine, local| engine.turn_end_reason(local));
        self.live.turn_reasons
    }

    /// Whether live items show this chat: the feature is on and the Host
    /// has actually sent it something live.
    pub fn live_active(&self, session: &str) -> bool {
        on_peer!(self.peer_ref(session), |engine, local| engine.live_active(local));
        self.live_owns(session)
    }

    /// The chat's live state, once the feature is on and it was opened.
    /// The Host clock's offset to this machine's for `session`'s Host
    /// (Host minus local, ms), as its live items last told it; 0 unknown.
    pub fn host_clock_offset_ms(&self, session: &str) -> i64 {
        on_peer!(self.peer_ref(session), |engine, local| engine.host_clock_offset_ms(local));
        self.live.host_offset_ms
    }

    pub fn live_view(&self, session: &str) -> Option<&LiveView> {
        on_peer!(self.peer_ref(session), |engine, local| engine.live_view(local));
        self.live.views.get(session)
    }

    /// Whether the Host explains tool items, as the chat's last snapshot
    /// said (on by default, as on the Host).
    pub fn tool_explanations_enabled(&self, session: &str) -> bool {
        on_peer!(self.peer_ref(session), |engine, local| engine.tool_explanations_enabled(local));
        self.live
            .explanations
            .as_ref()
            .or_else(|| self.live.views.get(session).and_then(|v| v.tool_explanations()))
            .and_then(|s| s.get("enabled"))
            .and_then(Value::as_bool)
            .unwrap_or(true)
    }

    /// Turns the Host's tool explanations on or off (§6): no explainer runs
    /// while off, and tool rows show the command under their label.
    pub fn set_tool_explanations_enabled(&mut self, enabled: bool) {
        let mut settings = self.live.explanations.clone().unwrap_or_default();
        settings.insert("enabled".into(), Value::Bool(enabled));
        self.live.explanations = Some(settings);
        self.changes.push(Change::Narrator);
        self.api.post_json("tool-explanations-settings", "/tool-explanations/settings", serde_json::json!({"enabled": enabled}), None);
    }

    /// The Host's setting, from the snapshot or its own route.
    pub(crate) fn live_explanations(&mut self, settings: Option<&Object>) {
        let Some(settings) = settings else { return };
        if self.live.explanations.as_ref() != Some(settings) {
            self.live.explanations = Some(settings.clone());
            self.changes.push(Change::Narrator);
        }
    }

    /// The Host offers its tool-explanation setting (Host contract 43).
    pub fn tool_explanation_setting(&self) -> bool {
        self.live.explanation_setting
    }

    /// Live items stand in for the old activity rows in this chat.
    pub(crate) fn live_owns(&self, session: &str) -> bool {
        self.live.enabled && self.live.views.contains_key(session) && self.live.heard.contains(session)
    }

    /// `/server-info` answered: whether the Host sends live items.
    pub(crate) fn live_server_info(&mut self, info: &Object) {
        // Feature flags live under capabilities.features
        // (contract/schemas/server-info.json).
        let has = |name: &str| {
            info.get("capabilities")
                .and_then(|c| c.get("features"))
                .and_then(Value::as_array)
                .is_some_and(|features| features.iter().any(|f| f.as_str() == Some(name)))
        };
        let enabled = has("live_items");
        self.live.explanation_setting = has("tool_explanation_setting");
        self.live.turn_summaries = has("log_turn_summary");
        self.live.read_into = has("read_into");
        self.live.events_gzip = has("events_gzip");
        self.live.turn_reasons = has(clarp_core::turn_reason::FEATURE);
        self.live.spoken_when_asked = has("spoken_when_asked");
        if enabled != self.live.enabled {
            self.live.enabled = enabled;
            self.live.views.clear();
        }
        if enabled && self.live.explanation_setting {
            self.api.get("tool-explanations-settings", "/tool-explanations/settings", &[]);
        }
        self.sync_live_subscription(false);
    }

    /// The Host or its token changed: nothing live is known any more.
    pub(crate) fn reset_live(&mut self) {
        let opened = std::mem::take(&mut self.live.opened);
        let shown = std::mem::take(&mut self.live.shown);
        self.live = Live { opened, shown, ..Live::default() };
        self.sse.set_query(Vec::new());
    }

    /// A chat opened: hold a view for it, ask for its snapshot once, and
    /// subscribe the stream to it.
    pub(crate) fn live_chat_opened(&mut self, session: &str) {
        on_peer!(self.peer_mut(session), |engine, local| engine.live_chat_opened(local));
        self.live_watch(session, true);
    }

    /// Watches a chat live. `opened_now`: the user opened it now, so it is
    /// the one opened last; a chat watched again after a reconnect keeps
    /// its place in that order.
    fn live_watch(&mut self, session: &str, opened_now: bool) {
        if !self.live.enabled || session.is_empty() || session.starts_with("pair:") {
            return;
        }
        let fresh = !self.live.views.contains_key(session);
        let view = self.live.views.entry(session.to_owned()).or_default();
        if fresh || (view.lseq().is_none() && !view.awaiting_snapshot()) {
            self.request_live(session);
        }
        if opened_now {
            self.live.opened.retain(|s| s != session);
            self.live.opened.push_back(session.to_owned());
            while self.live.opened.len() > OPENED_REMEMBERED {
                self.live.opened.pop_front();
            }
        }
        self.let_go_of_old_chats();
        self.sync_live_subscription(true);
    }

    /// Lets go of the live views of chats that are neither selected nor
    /// shown in a pane, but the few opened last.
    fn let_go_of_old_chats(&mut self) {
        let kept = self.kept_watched();
        let held: Vec<&String> = self.live.views.keys().collect();
        for session in let_go(&held, &self.live.opened, &kept, MAX_WATCHED) {
            self.live.views.remove(&session);
            self.live.heard.remove(&session);
            self.live.joining.remove(&session);
        }
    }

    /// The chats always watched live: the selected one and every one shown
    /// in a pane, on this engine's Host.
    fn kept_watched(&self) -> std::collections::HashSet<String> {
        let mut kept = self.shown_here();
        if !self.selected.is_empty() {
            kept.insert(self.selected.clone());
        }
        kept
    }

    /// The chats this engine's Host has shown in a pane: the primary's own
    /// panes, as they are now; for a peer's engine, what the primary told it.
    fn shown_here(&self) -> std::collections::HashSet<String> {
        if self.peer {
            return self.live.shown.clone();
        }
        self.panes
            .tree
            .view_layout()
            .iter()
            .filter(|pane| pane.get("shown").and_then(Value::as_bool) == Some(true))
            .filter_map(|pane| pane.get("session").and_then(Value::as_str))
            .filter(|session| clarp_core::all_hosts::split(session).is_none())
            .map(str::to_owned)
            .collect()
    }

    /// Tells each peer's engine which of its Host's chats the panes show.
    pub(crate) fn live_shown(&mut self, shown: &[String]) {
        let mut peers: std::collections::HashMap<String, std::collections::HashSet<String>> = std::collections::HashMap::new();
        for (server_id, local) in shown.iter().filter_map(|s| clarp_core::all_hosts::split(s)) {
            peers.entry(server_id.to_owned()).or_default().insert(local.to_owned());
        }
        for peer in &mut self.peers.list {
            peer.engine.live.shown = peers.remove(&peer.server_id).unwrap_or_default();
        }
    }

    /// The stream starts anew (`/server-info`): the loaded chats are
    /// watched again, those always watched first, the rest by when they were
    /// last opened, so the cap keeps the same ones as before.
    pub(crate) fn live_rewatch(&mut self) {
        let kept = self.kept_watched();
        let loaded: Vec<&String> = self.conversations.keys().collect();
        for session in rewatch_order(&loaded, &self.live.opened, &kept) {
            self.live_watch(&session, false);
        }
    }

    fn request_live(&mut self, session: &str) {
        if let Some(view) = self.live.views.get_mut(session) {
            view.expect_snapshot();
        }
        // Asked before the stream is up: events until it is are not sent,
        // so ask again once it connects.
        if !self.connected {
            self.live.joining.insert(session.to_owned());
        }
        self.get_live(session);
    }

    /// Sends `GET /live` for `session`, counted as outstanding.
    fn get_live(&mut self, session: &str) {
        *self.live.outstanding.entry(session.to_owned()).or_default() += 1;
        self.api.get(&format!("live:{session}"), "/live", &[("session", session)]);
    }

    /// One `/live` answer (or failure) for `session` came: true when none is
    /// outstanding any more.
    fn live_answered(&mut self, session: &str) -> bool {
        match self.live.outstanding.get_mut(session) {
            Some(count) if *count > 1 => {
                *count -= 1;
                false
            }
            _ => {
                self.live.outstanding.remove(session);
                true
            }
        }
    }

    /// The chats always watched live (`kept_watched`), for a catch-up round.
    pub(crate) fn live_kept_sessions(&self) -> std::collections::HashSet<String> {
        self.kept_watched()
    }

    /// A catch-up round begins: every held view asks for a fresh snapshot
    /// (events were missed); returns the `required` chats whose `/live` the
    /// round waits for.
    pub(crate) fn live_catch_up(&mut self, required: &[String]) -> std::collections::HashSet<String> {
        self.live.dropped = false;
        self.live.joining.clear();
        if !self.live.enabled {
            return Default::default();
        }
        let mut sessions: Vec<String> = self.live.views.keys().cloned().collect();
        sessions.sort();
        for session in &sessions {
            self.request_live(session);
        }
        required.iter().filter(|s| self.live.views.contains_key(*s)).cloned().collect()
    }

    /// The selected chat, the shown ones and the few opened last, as
    /// `/events?live=` names them; `live=` alone (status ops only) before
    /// any chat opened.
    fn live_sessions(&self) -> String {
        let mut sessions: Vec<&str> = self.live.views.keys().map(String::as_str).collect();
        sessions.sort_unstable();
        sessions.join(",")
    }

    /// Opens the stream with the sessions it should carry items for; a
    /// running stream reopens (resuming by event id) only when they changed.
    pub(crate) fn sync_live_subscription(&mut self, reopen: bool) {
        let wanted = self.live.enabled.then(|| self.live_sessions());
        let gzip = self.live.events_gzip;
        if wanted == self.live.subscribed && gzip == self.live.gzip_asked {
            return;
        }
        let mut query: Vec<(String, String)> = wanted.iter().map(|s| ("live".to_owned(), s.clone())).collect();
        if gzip {
            query.push(("gzip".to_owned(), "1".to_owned()));
        }
        self.sse.set_query(query);
        self.live.gzip_asked = gzip;
        let before: std::collections::HashSet<String> = self.live.subscribed.iter().flat_map(|s| s.split(',')).map(str::to_owned).collect();
        self.live.subscribed = wanted;
        if reopen && self.sse.running() {
            let added = self.live.views.keys().filter(|s| !before.contains(*s)).cloned();
            self.live.joining.extend(added);
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

    /// The reopened stream answers: chats that joined it fetch their
    /// snapshot again, so what happened before it reopened is not lost.
    pub(crate) fn live_resubscribed(&mut self) {
        let joining: Vec<String> = std::mem::take(&mut self.live.joining).into_iter().collect();
        for session in joining {
            if self.live.views.contains_key(&session) {
                self.request_live(&session);
            }
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
        // Item ops (and lseq) count only from a stream that lists the chat:
        // a summary copy (status and turn ops) must not advance it (§4).
        let listed = self.live.subscribed.as_deref().is_some_and(|s| s.split(',').any(|n| n == session));
        if !listed {
            return;
        }
        let Some(view) = self.live.views.get_mut(&session) else { return };
        let server_now = event.get("server_now_ms").and_then(Value::as_i64).unwrap_or(0);
        let before = view.generation();
        let effects = view.apply_event(event);
        if view.generation() != before {
            view.observe_clock(server_now, now_ms());
            if server_now > 0 {
                self.live.host_offset_ms = server_now - now_ms();
            }
            self.live.heard.insert(session.clone());
            self.changes.push(Change::Live(session.clone()));
        }
        if effects.contains(&FETCH_LIVE) {
            self.get_live(&session);
        }
    }

    /// Replies for `live:` tags; true when it was one.
    pub(crate) fn live_json(&mut self, tag: &str, object: &Object) -> bool {
        if tag == "tool-explanations-settings" {
            self.live_explanations(Some(object));
            return true;
        }
        let Some(session) = tag.strip_prefix("live:") else { return false };
        self.live_answered(session);
        if let Some(view) = self.live.views.get_mut(session) {
            let effects = view.apply_snapshot(object);
            if effects.contains(&FETCH_LIVE) {
                self.get_live(session);
            }
            let Some(view) = self.live.views.get_mut(session) else { return true };
            let server_now = object.get("server_now_ms").and_then(Value::as_i64).unwrap_or(0);
            view.observe_clock(server_now, now_ms());
            if server_now > 0 {
                self.live.host_offset_ms = server_now - now_ms();
            }
            // An idle, empty snapshot is what a silent Host answers too.
            let state = view.activity().get("state").and_then(Value::as_str).unwrap_or("idle");
            if !view.items().is_empty() || !matches!(state, "idle" | "") {
                self.live.heard.insert(session.to_owned());
            }
            self.changes.push(Change::Live(session.to_owned()));
        }
        if !self.live.outstanding.contains_key(session) {
            self.catch_up_live(session);
        }
        true
    }

    /// A failed `GET /live`: the chat keeps what it shows and the next event
    /// may ask again. Never a chat error (an older Host may lack the route).
    pub(crate) fn live_failure(&mut self, tag: &str, detail: &str, status: u16) -> bool {
        if tag == "tool-explanations-settings" {
            // The Host kept its setting: show what it has, and say why.
            eprintln!("Engine: tool explanations setting failed: {detail}");
            self.live.explanations = None;
            self.api.get("tool-explanations-settings", "/tool-explanations/settings", &[]);
            self.set_error(&format!("Could not change tool explanations: {detail}"));
            return true;
        }
        let Some(session) = tag.strip_prefix("live:") else { return false };
        eprintln!("Engine: GET /live for {session} failed: {detail}");
        let settled = self.live_answered(session);
        if let Some(view) = self.live.views.get_mut(session) {
            view.snapshot_failed();
        }
        if settled {
            self.catch_up_live_failed(session, status);
        }
        true
    }

    /// A catch-up round's `/live` read is asked again (its generation).
    pub(crate) fn live_retry_due(&mut self, session: &str, generation: u64) {
        if generation == self.outage.generation && self.live.views.contains_key(session) && !self.live.outstanding.contains_key(session) {
            self.request_live(session);
        }
    }
}

/// Where a chat is in the order chats were opened: 0 when it was never
/// opened (or so long ago it is forgotten), the one opened last highest.
fn opened_at(opened: &std::collections::VecDeque<String>, session: &str) -> usize {
    opened.iter().position(|s| s == session).map_or(0, |at| at + 1)
}

/// Of the chats `held` live, those to let go: never one `kept` (the selected
/// chat and those shown in a pane); of the rest, all but the `most` opened
/// last.
pub(crate) fn let_go(held: &[&String], opened: &std::collections::VecDeque<String>, kept: &std::collections::HashSet<String>, most: usize) -> Vec<String> {
    let mut others: Vec<(usize, &String)> = held.iter().filter(|s| !kept.contains(**s)).map(|s| (opened_at(opened, s), *s)).collect();
    if others.len() <= most {
        return Vec::new();
    }
    others.sort_unstable();
    let gone = others.len() - most;
    others.into_iter().take(gone).map(|(_, s)| s.clone()).collect()
}

/// The order loaded chats are watched again in: those `kept` first, then
/// the rest from the one opened last back.
pub(crate) fn rewatch_order(loaded: &[&String], opened: &std::collections::VecDeque<String>, kept: &std::collections::HashSet<String>) -> Vec<String> {
    let mut order: Vec<(bool, std::cmp::Reverse<usize>, &String)> =
        loaded.iter().map(|s| (!kept.contains(*s), std::cmp::Reverse(opened_at(opened, s)), *s)).collect();
    order.sort_unstable();
    order.into_iter().map(|(.., s)| s.clone()).collect()
}

#[cfg(test)]
mod tests {
    use super::{let_go, rewatch_order};
    use std::collections::{HashSet, VecDeque};

    fn names(list: &[&str]) -> Vec<String> {
        list.iter().map(|s| (*s).to_owned()).collect()
    }

    fn refs(list: &[String]) -> Vec<&String> {
        list.iter().collect()
    }

    #[test]
    fn the_selected_chat_and_every_shown_pane_are_never_let_go_and_the_cap_is_on_the_rest() {
        // Five panes shown (one selected), browsed past six other chats, and
        // the panes opened before all of them.
        let opened: VecDeque<String> = names(&["p1", "p2", "p3", "p4", "b1", "b2", "b3", "b4", "b5", "b6", "sel"]).into();
        let kept: HashSet<String> = names(&["p1", "p2", "p3", "p4", "sel"]).into_iter().collect();
        let held = names(&["sel", "b3", "p2", "b1", "p4", "b6", "b2", "p1", "b5", "p3", "b4"]);
        let mut gone = let_go(&refs(&held), &opened, &kept, 4);
        gone.sort();
        assert_eq!(gone, ["b1", "b2"]);
        // Within the cap nothing is let go, however many are kept.
        let few = names(&["p1", "p2", "p3", "p4", "sel", "b5", "b6"]);
        assert!(let_go(&refs(&few), &opened, &kept, 4).is_empty());
        // A chat never opened (loaded some other way) goes before any opened.
        let held = names(&["sel", "b3", "b4", "b5", "b6", "x"]);
        assert_eq!(let_go(&refs(&held), &opened, &kept, 4), ["x"]);
    }

    #[test]
    fn a_chat_let_go_and_opened_again_is_watched_again() {
        let mut opened: VecDeque<String> = names(&["a", "b", "c", "d", "e"]).into();
        let kept: HashSet<String> = HashSet::new();
        assert_eq!(let_go(&refs(&names(&["a", "b", "c", "d", "e"])), &opened, &kept, 4), ["a"]);
        // "a" opened again is the one opened last: "b" goes instead.
        opened.retain(|s| s != "a");
        opened.push_back("a".to_owned());
        assert_eq!(let_go(&refs(&names(&["b", "c", "d", "e", "a"])), &opened, &kept, 4), ["b"]);
    }

    #[test]
    fn after_a_reconnect_the_kept_chats_are_watched_first_and_the_rest_by_when_they_were_last_opened() {
        let opened: VecDeque<String> = names(&["c1", "c3", "c4", "c5", "c6", "c2"]).into();
        let kept: HashSet<String> = names(&["c2"]).into_iter().collect();
        // Stored in any order; watched again kept first, then newest first.
        let loaded = names(&["c4", "c1", "c6", "c2", "c3", "c5"]);
        let order = rewatch_order(&refs(&loaded), &opened, &kept);
        assert_eq!(order, ["c2", "c6", "c5", "c4", "c3", "c1"]);
        // Watched one by one in that order, with the cap applied after each,
        // the selected chat and the four opened last are what remain.
        let mut held: Vec<String> = Vec::new();
        for session in order {
            held.push(session);
            let gone = let_go(&refs(&held), &opened, &kept, 4);
            held.retain(|s| !gone.contains(s));
        }
        held.sort();
        assert_eq!(held, ["c2", "c3", "c4", "c5", "c6"]);
    }
}
