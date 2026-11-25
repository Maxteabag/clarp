//! Live items (docs/live-items.md §7) on the engine: the `live_items`
//! feature, the `/events?live=` subscription, `GET /live` snapshots and the
//! `live` events, one [`LiveView`] per chat. Without the feature nothing
//! here runs and chats keep to `/log` as before.

use clarp_core::json::{self, Object};
use clarp_core::live::{FETCH_LIVE, LiveView};
use serde_json::Value;

use crate::{Change, Engine};

/// Chats kept on the stream besides the selected one and those shown in a
/// pane: the ones opened last. A chat further back is let go, so the Host
/// does not count it as open on the owner's device (`docs/lanes.md`).
const MAX_OTHERS: usize = 3;
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
}

impl Engine {
    /// The Host sends live items (`live_items` in `/server-info`).
    pub fn live_items(&self) -> bool {
        self.live.enabled
    }

    /// Whether `/log` rows carry the turn summaries finished turns fold by
    /// (`log_turn_summary`, docs/live-items.md §9).
    pub fn log_turn_summary(&self) -> bool {
        self.live.turn_summaries
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
        if !self.live.enabled || session.is_empty() || session.starts_with("pair:") {
            return;
        }
        let fresh = !self.live.views.contains_key(session);
        let view = self.live.views.entry(session.to_owned()).or_default();
        if fresh || (view.lseq().is_none() && !view.awaiting_snapshot()) {
            self.request_live(session);
        }
        self.live.opened.retain(|s| s != session);
        self.live.opened.push_back(session.to_owned());
        while self.live.opened.len() > OPENED_REMEMBERED {
            self.live.opened.pop_front();
        }
        self.let_go_of_old_chats();
        self.sync_live_subscription(true);
    }

    /// Lets go of the live views of chats that are neither selected nor
    /// shown in a pane, but the few opened last.
    fn let_go_of_old_chats(&mut self) {
        let shown = self.shown_here();
        let opened_at = |session: &str| self.live.opened.iter().position(|s| s == session).map_or(0, |at| at + 1);
        let mut others: Vec<(usize, String)> = self
            .live
            .views
            .keys()
            .filter(|s| **s != self.selected && !shown.contains(*s))
            .map(|s| (opened_at(s), s.clone()))
            .collect();
        if others.len() <= MAX_OTHERS {
            return;
        }
        others.sort_unstable();
        let gone = others.len() - MAX_OTHERS;
        for (_, session) in others.into_iter().take(gone) {
            self.live.views.remove(&session);
            self.live.heard.remove(&session);
            self.live.joining.remove(&session);
        }
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

    /// The chats to open again when the stream starts anew: the oldest
    /// first and the selected one last, so the order they were opened in
    /// decides which are kept, never the order they are stored in.
    pub(crate) fn live_reopen_order(&self) -> Vec<String> {
        let shown = self.shown_here();
        let opened_at = |session: &str| self.live.opened.iter().position(|s| s == session).map_or(0, |at| at + 1);
        let mut open: Vec<(bool, bool, usize, &String)> = self
            .conversations
            .keys()
            .map(|s| (*s == self.selected, shown.contains(s), opened_at(s), s))
            .collect();
        open.sort_unstable();
        open.into_iter().map(|(.., s)| s.clone()).collect()
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
        self.api.get(&format!("live:{session}"), "/live", &[("session", session)]);
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
        if wanted == self.live.subscribed {
            return;
        }
        self.sse.set_query(wanted.iter().map(|s| ("live".to_owned(), s.clone())).collect());
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
            self.live.heard.insert(session.clone());
            self.changes.push(Change::Live(session.clone()));
        }
        if effects.contains(&FETCH_LIVE) {
            self.api.get(&format!("live:{session}"), "/live", &[("session", &session)]);
        }
    }

    /// Replies for `live:` tags; true when it was one.
    pub(crate) fn live_json(&mut self, tag: &str, object: &Object) -> bool {
        if tag == "tool-explanations-settings" {
            self.live_explanations(Some(object));
            return true;
        }
        let Some(session) = tag.strip_prefix("live:") else { return false };
        if let Some(view) = self.live.views.get_mut(session) {
            view.apply_snapshot(object);
            view.observe_clock(object.get("server_now_ms").and_then(Value::as_i64).unwrap_or(0), now_ms());
            // An idle, empty snapshot is what a silent Host answers too.
            let state = view.activity().get("state").and_then(Value::as_str).unwrap_or("idle");
            if !view.items().is_empty() || !matches!(state, "idle" | "") {
                self.live.heard.insert(session.to_owned());
            }
            self.changes.push(Change::Live(session.to_owned()));
        }
        true
    }

    /// A failed `GET /live`: the chat keeps what it shows and the next event
    /// may ask again. Never a chat error (an older Host may lack the route).
    pub(crate) fn live_failure(&mut self, tag: &str, detail: &str) -> bool {
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
        if let Some(view) = self.live.views.get_mut(session) {
            view.snapshot_failed();
        }
        true
    }
}
