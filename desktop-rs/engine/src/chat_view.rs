//! Conversation and Session views on the engine (feature `chat_view`,
//! issue 702): whether a chat's Host offers them, the Host's agent setting
//! (the snapshot's `chat_view`, written with `POST /agent-chat-view`), each
//! chat's own choice on this device and Clean chat (`clarp_core::chat_view`
//! says how they combine). Without the feature nothing here changes a chat.

use clarp_core::chat_view;
use clarp_core::json::Object;
use serde_json::{Value, json};

use crate::{Change, Engine};

#[derive(Default)]
pub(crate) struct ChatView {
    /// `chat_view` was in the last `/server-info`.
    pub feature: bool,
    /// `conversation_fork` was in it.
    pub fork: bool,
}

fn has(info: &Object, name: &str) -> bool {
    info.get("capabilities")
        .and_then(|c| c.get("features"))
        .and_then(Value::as_array)
        .is_some_and(|features| features.iter().any(|f| f.as_str() == Some(name)))
}

impl Engine {
    /// This Host offers Conversation and Session views.
    pub fn chat_view_feature(&self) -> bool {
        self.chat_view.feature
    }

    /// The chat's own Host offers them.
    pub fn chat_view_offered(&self, session: &str) -> bool {
        on_peer!(self.peer_ref(session), |engine, _local| engine.chat_view_feature());
        self.chat_view.feature
    }

    /// Any Host this desktop reads offers them (Clean chat is listed then).
    pub fn chat_view_anywhere(&self) -> bool {
        self.chat_view.feature || self.peers.list.iter().any(|p| p.engine.chat_view.feature)
    }

    /// Clean chat: every chat in Conversation view (this device's switch).
    pub fn clean_chat(&self) -> bool {
        self.settings.boolean(chat_view::CLEAN_KEY, false)
    }

    pub fn set_clean_chat(&mut self, on: bool) {
        if self.clean_chat() == on {
            return;
        }
        self.settings.set(chat_view::CLEAN_KEY, on);
        self.changes.push(Change::Preferences);
    }

    /// The chat's own choice on this device: "conversation", "session", or
    /// None (it follows Settings).
    pub fn chat_view_own(&self, session: &str) -> Option<&'static str> {
        chat_view::own(&self.settings, &self.chat_key(session))
    }

    /// The Host's value for the chat's agent ("" when it says none).
    pub fn chat_view_host(&self, session: &str) -> String {
        self.roster().find(session).map(|a| a.chat_view.clone()).unwrap_or_default()
    }

    /// The view the chat draws: "conversation" or "session"; always
    /// "session" when its Host lacks the feature.
    pub fn chat_view(&self, session: &str) -> &'static str {
        if !self.chat_view_offered(session) {
            return chat_view::SESSION;
        }
        chat_view::effective(self.chat_view_own(session), self.clean_chat(), &self.chat_view_host(session))
    }

    /// Whether the chat draws its Conversation view.
    pub fn chat_view_conversation(&self, session: &str) -> bool {
        self.chat_view(session) == chat_view::CONVERSATION
    }

    /// The chat menu's choice: "default" (Follow Settings), or its own.
    pub fn chat_view_menu_value(&self, session: &str) -> &'static str {
        self.chat_view_own(session).unwrap_or("default")
    }

    /// The reader picks a view for the chat ("default" clears its own):
    /// it shows at once and the Host is told after; a refusal keeps the
    /// choice on this device and says so.
    pub fn set_chat_view(&mut self, session: &str, value: &str) {
        let key = self.chat_key(session);
        if key.is_empty() || !self.chat_view_offered(session) {
            return;
        }
        let choice = chat_view::decoded(value);
        if self.chat_view_own(session) != choice {
            chat_view::set_own(&mut self.settings, &key, choice);
            self.changes.push(Change::Preferences);
            self.changes.push(Change::Conversation(session.to_owned()));
        }
        if let Some(view) = choice {
            self.write_chat_view(session, view);
        }
    }

    fn write_chat_view(&mut self, session: &str, view: &str) {
        on_peer!(self.peer_mut(session), |engine, local| engine.write_chat_view(local, view));
        if !self.chat_view.feature || session.is_empty() {
            return;
        }
        self.api.post_json(&format!("agent-chat-view:{session}"), chat_view::WRITE_PATH, json!({"session": session, "chat_view": view}), None);
    }

    /// The agent's own turn beside a conversation fork's, as one quiet
    /// line ("Also working · Running a command"), when both gates allow it
    /// and that turn runs; None otherwise.
    pub fn chat_main_turn_line(&self, session: &str) -> Option<String> {
        on_peer!(self.peer_ref(session), |engine, local| engine.chat_main_turn_line(local));
        if !self.chat_view.fork || !self.roster.find(session).is_some_and(|a| a.conversation_fork) {
            return None;
        }
        self.live.views.get(session).and_then(|v| v.main_turn()).and_then(chat_view::main_turn_line)
    }

    /// `/server-info` answered.
    pub(crate) fn chat_view_server_info(&mut self, info: &Object) {
        let (feature, fork) = (has(info, chat_view::FEATURE), has(info, chat_view::FORK_FEATURE));
        if feature != self.chat_view.feature || fork != self.chat_view.fork {
            self.chat_view = ChatView { feature, fork };
            self.changes.push(Change::Preferences);
        }
    }

    /// `agent-roster` kind `chat_view`: another device's different choice
    /// replaces this device's own for that chat; the echo of this
    /// device's own write keeps it.
    pub(crate) fn chat_view_roster(&mut self, event: &Object) {
        let kind = event.get("kind").and_then(Value::as_str).unwrap_or_default();
        let session = event.get("session").and_then(Value::as_str).unwrap_or_default();
        let Some(value) = event.get("chat_view").and_then(Value::as_str).and_then(chat_view::decoded) else { return };
        if kind != chat_view::ROSTER_KIND || session.is_empty() {
            return;
        }
        if self.chat_view_own(session).is_some_and(|own| own != value) {
            let key = self.chat_key(session);
            chat_view::set_own(&mut self.settings, &key, None);
            self.changes.push(Change::Preferences);
            self.changes.push(Change::Conversation(session.to_owned()));
        }
    }

    /// Replies for the write; true when it was one.
    pub(crate) fn chat_view_json(&mut self, tag: &str) -> bool {
        if !tag.starts_with("agent-chat-view:") {
            return false;
        }
        // The roster event follows; the snapshot brings the Host's value.
        self.request_snapshot();
        true
    }

    /// A refused or failed write: the choice stays on this device.
    pub(crate) fn chat_view_failure(&mut self, tag: &str) -> bool {
        if !tag.starts_with("agent-chat-view:") {
            return false;
        }
        let host = self.server_name.clone();
        self.set_error(&chat_view::refused(&host));
        true
    }
}
