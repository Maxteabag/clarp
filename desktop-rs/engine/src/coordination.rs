//! Coordination between agents on the engine (feature
//! `hidden_coordination`, docs/protocol.md "Hidden coordination"): the
//! Host's setting (`GET`/`POST /coordination-settings`), a chat's own
//! choice (`POST /agent-coordination`), what every `/log` read of a chat
//! sends, the visit that shows a chat's hidden runs, and the reload when
//! the setting changes. Without the feature nothing here runs and `/log`
//! reads are as before. The Host's value is the truth: the desktop writes
//! it only when the reader flips the switch, and never seeds it.

use std::collections::HashSet;

use clarp_core::coordination;
use clarp_core::json::Object;
use serde_json::{Value, json};

use crate::{Change, Engine};

#[derive(Default)]
pub(crate) struct Coordination {
    /// `hidden_coordination` was in the last `/server-info`.
    pub feature: bool,
    /// The Host's own value (`show`, `hide`, or one this client does not
    /// know), empty until it said.
    pub visibility: String,
    /// Chats read with every run shown for this visit (`coordination=show`).
    pub shown: HashSet<String>,
}

impl Engine {
    /// The Host can leave coordination between agents out of a chat.
    pub fn hidden_coordination(&self) -> bool {
        self.coordination.feature
    }

    /// The chat's own Host can leave coordination between agents out of it.
    pub fn chat_hidden_coordination(&self, session: &str) -> bool {
        on_peer!(self.peer_ref(session), |engine, _local| engine.hidden_coordination());
        self.coordination.feature
    }

    /// A chat's own choice for messages between agents: "show", "hide", or
    /// "default" (it follows its Host), as the snapshot says.
    pub fn chat_coordination_choice(&self, session: &str) -> String {
        let over = self.roster().find(session).map(|a| a.coordination_override.trim().to_ascii_lowercase()).unwrap_or_default();
        match over.as_str() {
            "show" | "hide" => over,
            _ => "default".to_owned(),
        }
    }

    /// The Host's setting as it said (empty before it did).
    pub fn coordination_visibility(&self) -> &str {
        &self.coordination.visibility
    }

    /// Whether the Host shows coordination between agents (an unknown value
    /// reads as show; nothing known yet as the Host's default, hide).
    pub fn coordination_shown(&self) -> bool {
        self.coordination.feature && !self.coordination.visibility.is_empty() && !coordination::hides(&self.coordination.visibility)
    }

    /// The reader flipped "Show messages between agents": the Host's value.
    pub fn set_coordination_shown(&mut self, show: bool) {
        if !self.coordination.feature {
            return;
        }
        let value = if show { coordination::SHOW } else { coordination::HIDE };
        self.coordination.visibility = value.to_owned();
        self.changes.push(Change::Preferences);
        self.api.post_json("coordination-settings", "/coordination-settings", json!({"coordination_visibility": value}), None);
    }

    /// A chat's own choice (`show`, `hide`, or `default` to follow the Host).
    pub fn set_chat_coordination(&mut self, session: &str, visibility: &str) {
        on_peer!(self.peer_mut(session), |engine, local| engine.set_chat_coordination(local, visibility));
        if !self.coordination.feature || session.is_empty() {
            return;
        }
        self.api.post_json(&format!("agent-coordination:{session}"), "/agent-coordination", json!({"session": session, "visibility": visibility}), None);
    }

    /// The chat is read with every run shown for this visit.
    pub fn coordination_visit(&self, session: &str) -> bool {
        on_peer!(self.peer_ref(session), |engine, local| engine.coordination_visit(local));
        self.coordination.shown.contains(session)
    }

    /// The quiet line was opened: read the chat again with every run shown,
    /// for this visit only (never cached).
    pub fn show_coordination(&mut self, session: &str) {
        on_peer!(self.peer_mut(session), |engine, local| engine.show_coordination(local));
        if !self.coordination.feature || session.is_empty() || !self.coordination.shown.insert(session.to_owned()) {
            return;
        }
        self.request_tail(session, true);
    }

    /// Leaving the chat ends its visit: it is read as the setting says.
    pub(crate) fn end_coordination_visits(&mut self, keep: &str) {
        let ended: Vec<String> = self.coordination.shown.iter().filter(|s| s.as_str() != keep).cloned().collect();
        for session in ended {
            self.coordination.shown.remove(&session);
            self.request_tail(&session, true);
        }
    }

    /// What a `/log` read of `session` adds.
    pub(crate) fn coordination_query(&self, session: &str, delta: bool) -> Vec<(&'static str, &'static str)> {
        let echo = self.conversations.get(session).and_then(|c| c.coordination());
        coordination::query(self.coordination.feature, self.coordination_visit(session), delta, echo)
    }

    /// Whether a chat's rows may be cached: not a page read for one visit.
    pub(crate) fn coordination_cacheable(&self, session: &str) -> bool {
        !self.coordination_visit(session) && !coordination::requested(self.conversations.get(session).and_then(|c| c.coordination()))
    }

    /// `/server-info` answered.
    pub(crate) fn coordination_server_info(&mut self, info: &Object) {
        let has = info
            .get("capabilities")
            .and_then(|c| c.get("features"))
            .and_then(Value::as_array)
            .is_some_and(|features| features.iter().any(|f| f.as_str() == Some(coordination::FEATURE)));
        if has != self.coordination.feature {
            self.coordination.feature = has;
            self.coordination.shown.clear();
            self.changes.push(Change::Preferences);
        }
        if has {
            self.api.get("coordination-settings", "/coordination-settings", &[]);
        } else {
            self.coordination.visibility.clear();
        }
    }

    /// The snapshot changed agents' settings: an open chat read under
    /// another one is read again.
    pub(crate) fn coordination_reloads(&mut self) {
        if !self.coordination.feature {
            return;
        }
        let due: Vec<String> = self
            .conversations
            .iter()
            .filter(|(session, conversation)| {
                let agent = self.roster.find(session).map(|a| a.coordination_visibility.as_str()).unwrap_or_default();
                conversation.latest_revision() > 0
                    && !self.log_in_flight.contains(session.as_str())
                    && coordination::needs_reload(true, self.coordination_visit(session), conversation.coordination(), agent)
            })
            .map(|(session, _)| session.clone())
            .collect();
        for session in due {
            self.request_tail(&session, true);
        }
    }

    /// Replies for the coordination tags; true when it was one.
    pub(crate) fn coordination_json(&mut self, tag: &str, object: &Object) -> bool {
        if tag == "coordination-settings" || tag == "coordination-settings-read" {
            let value = object.get("coordination_visibility").and_then(Value::as_str).unwrap_or_default();
            if !value.is_empty() && self.coordination.visibility != value {
                self.coordination.visibility = value.to_owned();
                self.changes.push(Change::Preferences);
            }
            return true;
        }
        if !tag.starts_with("agent-coordination:") {
            return false;
        }
        // The roster event follows; the snapshot reloads the chat.
        self.request_snapshot();
        true
    }

    /// A refused or failed write: read the Host's value again, so the
    /// switch shows what it holds, and say why.
    pub(crate) fn coordination_failure(&mut self, tag: &str, detail: &str) -> bool {
        if tag == "coordination-settings" {
            eprintln!("Engine: coordination setting failed: {detail}");
            self.api.get("coordination-settings-read", "/coordination-settings", &[]);
            return true;
        }
        if tag == "coordination-settings-read" {
            eprintln!("Engine: GET /coordination-settings failed: {detail}");
            return true;
        }
        if tag.starts_with("agent-coordination:") {
            // i18n: error banner: the chat's messages-between-agents choice was not saved; {} is the Host's reason
            self.set_error(&clarp_core::tr!("Could not change messages between agents: {}", detail));
            self.request_snapshot();
            return true;
        }
        false
    }
}
