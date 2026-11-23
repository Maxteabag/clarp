//! Conversation and Session views on the engine (feature `chat_view`,
//! issue 702; `clarp_core::chat_view`): whether a chat's Host offers them,
//! the Host's value (each snapshot agent's `chat_view`), the chat's own
//! choice on this device, Clean chat, and the write `POST /agent-chat-view`
//! to the chat's own Host (a peer chat's to its peer). Without the feature
//! nothing here runs and every chat is drawn as before.
//!
//! This device's state is the primary engine's, kept by chat key
//! (`@<server_id>/<session>`, as the chat's Tool activity is): its own
//! choices in the settings, writes in flight and refusals in memory. The
//! engine that reads the chat's Host (a peer's for a peer chat) only sends
//! the write and hands back what came of it (`outcomes`), which the
//! primary settles when it pumps.

use std::collections::HashMap;

use clarp_core::all_hosts;
use clarp_core::chat_view::{self, Choice, Setting};
use clarp_core::json::Object;
use serde_json::{Value, json};

use crate::{Change, Engine};

/// What came of a write, or a new Host value, for a chat of the engine
/// that read it (its own session name).
#[derive(Debug, Clone)]
pub(crate) enum Outcome {
    Accepted { session: String, generation: u64, echo: Option<Choice> },
    Refused { session: String, generation: u64, detail: String },
    HostRead { session: String, value: Option<Choice> },
}

impl Outcome {
    fn session(&self) -> &str {
        match self {
            Outcome::Accepted { session, .. } | Outcome::Refused { session, .. } | Outcome::HostRead { session, .. } => session,
        }
    }

    fn qualified(self, server_id: &str) -> Outcome {
        let q = |s: String| if s.is_empty() || server_id.is_empty() { s } else { all_hosts::qualify(server_id, &s) };
        match self {
            Outcome::Accepted { session, generation, echo } => Outcome::Accepted { session: q(session), generation, echo },
            Outcome::Refused { session, generation, detail } => Outcome::Refused { session: q(session), generation, detail },
            Outcome::HostRead { session, value } => Outcome::HostRead { session: q(session), value },
        }
    }
}

#[derive(Default)]
pub(crate) struct ChatViews {
    /// `chat_view` was in the last `/server-info`.
    pub feature: bool,
    /// Writes in flight, refusals and generations by chat key (the
    /// primary's); the chat's own choice is in the settings.
    states: HashMap<String, Setting>,
    /// What this engine's Host said, for the primary to settle.
    pub outcomes: Vec<Outcome>,
}

const TAG: &str = "agent-chat-view:";

impl Engine {
    /// The chat's own Host offers Conversation and Session views.
    pub fn chat_view_feature(&self, session: &str) -> bool {
        on_peer!(self.peer_ref(session), |engine, _local| engine.chat_view.feature);
        self.chat_view.feature
    }

    /// Clean chat shows in Settings while any Host read offers the feature.
    pub fn chat_view_offered(&self) -> bool {
        self.chat_view.feature || self.peers.list.iter().any(|p| p.engine.chat_view.feature)
    }

    /// Settings › Chats › Clean chat: every chat without its own choice in
    /// Conversation view, on this device.
    pub fn clean_chat(&self) -> bool {
        self.settings.boolean(chat_view::CLEAN_KEY, false)
    }

    pub fn set_clean_chat(&mut self, on: bool) {
        if self.clean_chat() == on {
            return;
        }
        self.settings.set(chat_view::CLEAN_KEY, on);
        self.changes.push(Change::Preferences);
        let open: Vec<String> = self.conversations.keys().cloned().collect();
        self.changes.extend(open.into_iter().map(Change::Conversation));
        self.changes.push(Change::Roster);
    }

    /// The chat's toggle: the Host's value as last read, this device's own
    /// choice and any write in flight.
    pub fn chat_view_setting(&self, session: &str) -> Setting {
        let key = self.chat_key(session);
        let mut setting = self.chat_view.states.get(&key).cloned().unwrap_or_default();
        setting.local = self
            .settings
            .get(chat_view::LOCAL_KEY)
            .and_then(|m| m.get(&key))
            .and_then(Value::as_str)
            .and_then(Choice::decoded);
        setting.host = self.roster().find(session).and_then(|a| Choice::decoded(&a.chat_view));
        setting
    }

    /// What the chat draws: None when its Host lacks the feature (then
    /// nothing changes and no switch shows).
    pub fn chat_view(&self, session: &str) -> Option<Choice> {
        if session.is_empty() || !self.chat_view_feature(session) {
            return None;
        }
        Some(self.chat_view_setting(session).effective(self.clean_chat()))
    }

    /// The chat draws the Conversation view.
    pub fn chat_in_conversation(&self, session: &str) -> bool {
        self.chat_view(session) == Some(Choice::Conversation)
    }

    /// The entry the chat's Chat view menu has checked ("default",
    /// "conversation", "session").
    pub fn chat_view_menu_value(&self, session: &str) -> &'static str {
        chat_view::menu_value(self.chat_view_setting(session).own())
    }

    /// A Chat view menu entry: a view becomes the chat's own (and is
    /// written to its Host); Follow Settings clears the chat's own choice
    /// on this device.
    pub fn choose_chat_view_menu(&mut self, session: &str, value: &str) {
        match chat_view::menu_choice(value) {
            Some(choice) => self.choose_chat_view(session, choice),
            None => {
                let mut setting = self.chat_view_setting(session);
                if setting.clear_own() {
                    self.store_chat_view(session, setting);
                }
            }
        }
    }

    /// The reader picks a view: it shows at once; the Host is told after.
    pub fn choose_chat_view(&mut self, session: &str, choice: Choice) {
        if !self.chat_view_feature(session) || self.chat_key(session).is_empty() {
            return;
        }
        let mut setting = self.chat_view_setting(session);
        let Some(generation) = setting.choose(choice) else { return };
        self.store_chat_view(session, setting);
        self.send_chat_view(session, generation, choice);
    }

    /// C in the chat: the other view, as the chat's own choice.
    pub fn toggle_chat_view(&mut self, session: &str) -> bool {
        let Some(now) = self.chat_view(session) else { return false };
        self.choose_chat_view(session, now.other());
        true
    }

    /// The last write was refused: the quiet line's words.
    pub fn chat_view_refusal(&self, session: &str) -> Option<String> {
        if !self.chat_view_feature(session) || !self.chat_view_setting(session).refused {
            return None;
        }
        self.chat_view_refusal_words(session)
    }

    /// Keeps `setting` for the chat: its own choice in the settings, the
    /// rest in memory.
    fn store_chat_view(&mut self, session: &str, setting: Setting) {
        let key = self.chat_key(session);
        if key.is_empty() {
            return;
        }
        let mut map = self.settings.get(chat_view::LOCAL_KEY).and_then(Value::as_object).cloned().unwrap_or_default();
        let before = map.get(&key).cloned();
        match setting.local {
            Some(choice) => {
                map.insert(key.clone(), json!(choice.wire()));
            }
            None => {
                map.remove(&key);
            }
        }
        if map.get(&key) != before.as_ref() {
            if map.is_empty() {
                self.settings.remove(chat_view::LOCAL_KEY);
            } else {
                self.settings.set(chat_view::LOCAL_KEY, Value::Object(map));
            }
            self.changes.push(Change::Preferences);
        }
        self.chat_view.states.insert(key, Setting { local: None, host: None, ..setting });
        self.changes.push(Change::Conversation(session.to_owned()));
        self.changes.push(Change::Roster);
    }

    /// `POST /agent-chat-view` to the chat's own Host.
    fn send_chat_view(&mut self, session: &str, generation: u64, choice: Choice) {
        on_peer!(self.peer_mut(session), |engine, local| engine.send_chat_view(local, generation, choice));
        self.api.post_json(
            &format!("{TAG}{generation}:{session}"),
            chat_view::WRITE_PATH,
            json!({"session": session, "chat_view": choice.wire()}),
            None,
        );
    }

    /// `/server-info` answered.
    pub(crate) fn chat_view_server_info(&mut self, info: &Object) {
        let has = info
            .get("capabilities")
            .and_then(|c| c.get("features"))
            .and_then(Value::as_array)
            .is_some_and(|features| features.iter().any(|f| f.as_str() == Some(chat_view::FEATURE)));
        if has != self.chat_view.feature {
            self.chat_view.feature = has;
            self.changes.push(Change::Preferences);
            let open: Vec<String> = self.conversations.keys().cloned().collect();
            for session in open {
                // Rows read before carry no tags: read the chat again, so
                // its view can split it.
                if has {
                    self.request_tail(&session, true);
                }
                self.changes.push(Change::Conversation(session));
            }
        }
    }

    /// `agent-roster` kind `chat_view`: the chat's new Host value.
    pub(crate) fn chat_view_roster(&mut self, event: &Object) {
        if event.get("kind").and_then(Value::as_str) != Some(chat_view::ROSTER_KIND) {
            return;
        }
        let session = event.get("session").and_then(Value::as_str).unwrap_or_default();
        let value = event.get("chat_view").and_then(Value::as_str).unwrap_or_default();
        if session.is_empty() || value.is_empty() {
            return;
        }
        self.chat_view.outcomes.push(Outcome::HostRead { session: session.to_owned(), value: Choice::decoded(value) });
    }

    /// The answer to a write; true when it was one.
    pub(crate) fn chat_view_json(&mut self, tag: &str, object: &Object) -> bool {
        let Some((generation, session)) = parse_tag(tag) else { return false };
        let echo = object.get("chat_view").and_then(Value::as_str).and_then(Choice::decoded);
        self.chat_view.outcomes.push(Outcome::Accepted { session, generation, echo });
        true
    }

    /// A refused or failed write: the choice stays on this device.
    pub(crate) fn chat_view_failure(&mut self, tag: &str, detail: &str) -> bool {
        let Some((generation, session)) = parse_tag(tag) else { return false };
        eprintln!("Engine: chat view not saved: {detail}");
        self.chat_view.outcomes.push(Outcome::Refused { session, generation, detail: detail.to_owned() });
        true
    }

    /// The primary settles what its Host and its peers said.
    pub(crate) fn settle_chat_views(&mut self, outcomes: Vec<Outcome>) {
        for outcome in outcomes {
            let session = outcome.session().to_owned();
            let mut setting = self.chat_view_setting(&session);
            match outcome {
                Outcome::Accepted { generation, echo, .. } => setting.accepted(generation, echo),
                Outcome::Refused { generation, .. } => {
                    setting.refused_write(generation);
                    if setting.refused {
                        if let Some(words) = self.chat_view_refusal_words(&session) {
                            self.set_error(&words);
                        }
                    }
                }
                Outcome::HostRead { value, .. } => setting.host_read(value),
            }
            self.store_chat_view(&session, setting);
        }
    }

    fn chat_view_refusal_words(&self, session: &str) -> Option<String> {
        let host = match all_hosts::split(session) {
            Some((server_id, _)) => self.peers.list.iter().find(|p| p.server_id == server_id).map(|p| p.label.clone()).unwrap_or_default(),
            None => self.server_name.clone(),
        };
        Some(chat_view::refused_words(&host))
    }

    /// This engine's outcomes, for the primary.
    pub(crate) fn take_chat_view_outcomes(&mut self) -> Vec<Outcome> {
        std::mem::take(&mut self.chat_view.outcomes)
    }

    /// A peer's outcomes, its chats named as the primary names them.
    pub(crate) fn qualify_chat_view_outcomes(outcomes: Vec<Outcome>, server_id: &str) -> Vec<Outcome> {
        outcomes.into_iter().map(|o| o.qualified(server_id)).collect()
    }
}

/// `agent-chat-view:<generation>:<session>`.
fn parse_tag(tag: &str) -> Option<(u64, String)> {
    let rest = tag.strip_prefix(TAG)?;
    let (generation, session) = rest.split_once(':')?;
    Some((generation.parse().ok()?, session.to_owned()))
}
