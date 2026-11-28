//! Notifications per event (Host feature `notification_events`): the
//! Host's settings (which events alert, globally and per contact), read on
//! connect and kept live from `notification-settings-changed`; which
//! `user-notification` becomes a desktop popup; marking a chat read from a
//! popup (`POST /chat-read`); and answering a question from its buttons.
//! A Host without the feature keeps the old rule: a reply in a chat that
//! is not the selected one pops up.

use clarp_core::json::{self, Object};
use clarp_core::notify_events::{self, Incoming, Preset, Recent, Settings};
use serde_json::{Value, json};

use crate::{Change, Engine};

const SETTINGS_TAG: &str = "notification-settings";
const SETTINGS_PATH: &str = "/notification-settings";

#[derive(Default)]
pub(crate) struct Notifications {
    /// The Host lists `notification_events`.
    offered: bool,
    /// Its settings document, once read.
    settings: Option<Settings>,
    /// The last notification ids that popped up (or were dropped).
    recent: Recent,
}

/// One desktop popup: who, what, and what its buttons do.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Popup {
    pub title: String,
    pub body: String,
    /// The chat (qualified for another Host's).
    pub session: String,
    pub notification: Incoming,
    /// The chat is the one selected in this window: the window drops the
    /// popup while it has the focus.
    pub selected: bool,
}

impl Engine {
    // ---- queries ---------------------------------------------------------

    /// The Host keeps notification settings per event.
    pub fn notification_events_offered(&self) -> bool {
        self.notifications.offered
    }

    /// The Host's notification settings, once read.
    pub fn notification_settings(&self) -> Option<&Settings> {
        self.notifications.settings.as_ref()
    }

    /// The settings of the Host that holds this chat (another Host's for
    /// its chats), and the agent id they are keyed by there.
    pub fn notification_settings_for(&self, session: &str) -> Option<(&Settings, String)> {
        on_peer!(self.peer_ref(session), |engine, local| engine.notification_settings_for(local));
        if !self.notifications.offered {
            return None;
        }
        let agent_id = self.roster.find(session).map(|a| a.agent_id.clone()).filter(|id| !id.is_empty())?;
        Some((self.notifications.settings.as_ref()?, agent_id))
    }

    // ---- commands --------------------------------------------------------

    /// Turns an event on or off for every contact (the owner's choice).
    pub fn set_notification_event(&mut self, event: &str, on: bool) {
        let Some(settings) = self.notifications.settings.as_mut() else { return };
        let body = settings.set_global(event, on);
        self.post_notification_settings(body);
    }

    /// One contact's choice for an event: Some(on/off), or None to follow
    /// the owner's choice.
    pub fn set_agent_notification_event(&mut self, session: &str, event: &str, choice: Option<bool>) {
        on_peer!(self.peer_mut(session), |engine, local| engine.set_agent_notification_event(local, event, choice));
        let Some(agent_id) = self.roster.find(session).map(|a| a.agent_id.clone()).filter(|id| !id.is_empty()) else { return };
        let Some(settings) = self.notifications.settings.as_mut() else { return };
        let body = settings.set_agent(&agent_id, event, choice);
        self.post_notification_settings(body);
    }

    /// A contact's preset: All or Only when it needs me set its choices
    /// (and unmute it), Muted mutes it; Custom changes nothing.
    pub fn set_agent_notification_preset(&mut self, session: &str, preset: Preset) {
        on_peer!(self.peer_mut(session), |engine, local| engine.set_agent_notification_preset(local, preset));
        let Some(agent) = self.roster.find(session) else { return };
        let (agent_id, muted) = (agent.agent_id.clone(), agent.muted);
        match preset {
            Preset::Custom => {}
            Preset::Muted => self.set_agent_push_muted(session, true),
            Preset::All | Preset::NeedsMe => {
                if muted {
                    self.set_agent_push_muted(session, false);
                }
                if let Some(choices) = self.notifications.settings.as_mut().and_then(|s| s.agents.get_mut(&agent_id)) {
                    choices.muted = false;
                }
                let body = self.notifications.settings.as_mut().and_then(|s| s.set_preset(&agent_id, preset));
                if let Some(body) = body {
                    self.post_notification_settings(body);
                } else {
                    self.changes.push(Change::NotificationSettings);
                }
            }
        }
    }

    fn post_notification_settings(&mut self, body: Value) {
        self.changes.push(Change::NotificationSettings);
        self.api.post_json(SETTINGS_TAG, SETTINGS_PATH, body, None);
    }

    /// Marks a chat read without opening it (a popup's Mark read): here at
    /// once, and on the Host, which tells the other devices.
    pub fn mark_chat_read(&mut self, session: &str) {
        on_peer!(self.peer_mut(session), |engine, local| engine.mark_chat_read(local));
        if session.is_empty() {
            return;
        }
        self.mutate_roster(|r| r.clear_unread(session));
        self.changes.push(Change::NotificationRead(session.to_owned()));
        if self.notifications.offered {
            self.api.post_json(&format!("chat-read:{session}"), "/chat-read", json!({"session": session}), None);
        }
    }

    /// Answers a question from a popup's button. False when the question's
    /// revision is not known here (not in Updates): the caller opens the
    /// chat at it instead.
    pub fn answer_from_notification(&mut self, decision_id: &str, response_type: &str, option: &str) -> bool {
        let revision = self.attention_items().iter().filter_map(Value::as_object).find(|item| json::string(item, "decision_id") == decision_id).map(|item| json::integer(item, "revision"));
        let Some(revision) = revision.filter(|r| *r > 0) else { return false };
        match response_type {
            "approval" => {
                let choice = match option {
                    "yes" | "accepted" => "yes",
                    "no" | "rejected" => "no",
                    _ => return false,
                };
                self.resolve_decision(decision_id, choice, revision);
                true
            }
            "single_choice" => {
                let (artifact, body) = (self.decision_artifact(decision_id), json!({"answer": {"option_id": option}}));
                match artifact {
                    Some(artifact) => {
                        self.send_decision(&artifact, decision_id, "resolve", body, revision);
                        true
                    }
                    None => false,
                }
            }
            _ => false,
        }
    }

    /// The artifact a decision in Updates belongs to.
    fn decision_artifact(&self, decision_id: &str) -> Option<String> {
        self.attention_items()
            .iter()
            .filter_map(Value::as_object)
            .find(|item| json::string(item, "decision_id") == decision_id)
            .map(|item| json::string(item, "artifact_id"))
            .filter(|id| !id.is_empty())
    }

    // ---- the Host --------------------------------------------------------

    /// `/server-info` answered: whether the Host keeps settings per event.
    pub(crate) fn notifications_server_info(&mut self, info: &Object) {
        let offered = info
            .get("capabilities")
            .and_then(|c| c.get("features"))
            .and_then(Value::as_array)
            .is_some_and(|features| features.iter().any(|f| f.as_str() == Some(notify_events::FEATURE)));
        let was = (self.notifications.offered, self.notifications.settings.is_some());
        self.notifications.offered = offered;
        if offered {
            self.api.get(SETTINGS_TAG, SETTINGS_PATH, &[]);
        } else {
            self.notifications.settings = None;
        }
        if was != (offered, self.notifications.settings.is_some()) {
            self.changes.push(Change::NotificationSettings);
        }
    }

    /// Replies for the notification routes; true when it was one.
    pub(crate) fn notifications_json(&mut self, tag: &str, object: &Object) -> bool {
        if tag == SETTINGS_TAG {
            self.take_notification_settings(object);
            return true;
        }
        tag.starts_with("chat-read:")
    }

    pub(crate) fn notifications_failure(&mut self, tag: &str, detail: &str) -> bool {
        if tag == SETTINGS_TAG {
            // The Host kept what it had: read it again and say why.
            eprintln!("Engine: notification settings failed: {detail}");
            if self.notifications.offered {
                self.api.get(SETTINGS_TAG, SETTINGS_PATH, &[]);
            }
            self.set_error(&format!("Could not change notifications: {detail}"));
            return true;
        }
        if let Some(session) = tag.strip_prefix("chat-read:") {
            // Read here already; the other devices learn it when the chat opens.
            eprintln!("Engine: marking {session} read on the Host failed: {detail}");
            return true;
        }
        false
    }

    fn take_notification_settings(&mut self, document: &Object) {
        let settings = Settings::parse(document);
        if self.notifications.settings.as_ref() != Some(&settings) {
            self.notifications.settings = Some(settings);
            self.changes.push(Change::NotificationSettings);
        }
    }

    // ---- the event stream ------------------------------------------------

    /// `notification-settings-changed`: the whole document, from any device.
    pub(crate) fn notification_settings_event(&mut self, event: &Object) {
        if !self.notifications.offered {
            return;
        }
        match event.get("settings").and_then(Value::as_object) {
            Some(document) => self.take_notification_settings(&document.clone()),
            None => self.take_notification_settings(event),
        }
    }

    /// `agent-focus` or `chat-read`: the chat was opened or read on some
    /// device, so its popup goes.
    pub(crate) fn chat_read_event(&mut self, session: &str, read: bool) {
        if session.is_empty() {
            return;
        }
        if read {
            self.mutate_roster(|r| r.clear_unread(session));
        }
        self.changes.push(Change::NotificationRead(session.to_owned()));
    }

    /// `user-notification`: a popup when the event alerts, the contact is
    /// not quiet, it was not shown before, and (on a Host without the
    /// feature) its chat is not the selected one.
    pub(crate) fn notification_event(&mut self, event: &Object) {
        let incoming = Incoming::parse(event);
        let session = incoming.session.clone();
        let selected = session == self.selected;
        if selected {
            self.mutate_roster(|r| r.clear_unread(&session));
        }
        let quiet = self.roster.find(&session).is_some_and(|a| a.quiet);
        if !incoming.alerts(quiet) || (selected && !self.notifications.offered) {
            return;
        }
        if !self.notifications.recent.first_time(&incoming.notification_id) {
            return;
        }
        let title = Some(incoming.persona.clone()).filter(|p| !p.is_empty()).unwrap_or_else(|| session.clone());
        let mut body = clarp_core::decision_receipt::preview_line(&incoming.preview);
        if body.is_empty() {
            let key = format!("notify.body.{}", incoming.event);
            let words = notify_events::text(&key);
            body = if words == key { notify_events::text("notify.body.reply").to_owned() } else { words.to_owned() };
        }
        self.changes.push(Change::Notification(Popup { title, body, session, notification: incoming, selected }));
    }
}
