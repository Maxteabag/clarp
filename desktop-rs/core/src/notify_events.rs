//! Notifications per event (Host feature `notification_events`): the
//! Host's settings document (which events alert, globally and per contact),
//! what one `user-notification` carries, whether it pops up on this
//! desktop, and the English words for every key the desktop shows.
//!
//! The Host owns the settings, so every device of the owner agrees: `GET`
//! and `POST /notification-settings` read and change them, and the SSE
//! `notification-settings-changed` carries the same document. A Host
//! without the feature sends no `alert`, and the desktop keeps its old rule:
//! a reply in a chat that is not open pops up.

use std::collections::{HashMap, VecDeque};

use serde_json::{Value, json};

use crate::json::{self, Object};

/// The Host feature that sends `event` and `alert` and keeps the settings.
pub const FEATURE: &str = "notification_events";
/// The SSE event that carries the whole settings document.
pub const SETTINGS_CHANGED: &str = "notification-settings-changed";
/// The SSE event a chat read elsewhere (`POST /chat-read`) sends.
pub const CHAT_READ: &str = "chat-read";

/// One event of the Host's catalog and the owner's global choice for it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Event {
    /// The stable wire id (`reply`, `needs_you`, …).
    pub id: String,
    /// `notify.event.<id>`: the words for it.
    pub message_key: String,
    /// The catalog's default.
    pub default: bool,
    /// The owner's global choice (the default when not changed).
    pub on: bool,
}

/// One contact's choices: muted (no alert at all), and per event an
/// override of the global choice (absent: follow it).
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct AgentChoices {
    pub muted: bool,
    pub events: HashMap<String, bool>,
}

/// The Host's settings document.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Settings {
    pub events: Vec<Event>,
    /// Keyed by agent id.
    pub agents: HashMap<String, AgentChoices>,
    pub version: i64,
}

impl Settings {
    /// Reads the document; ids it does not know stay as the Host sent them.
    pub fn parse(document: &Object) -> Settings {
        let events = json::array(document, "events")
            .iter()
            .filter_map(Value::as_object)
            .filter_map(|e| {
                let id = json::string(e, "id");
                if id.is_empty() {
                    return None;
                }
                let default = json::boolean(e, "default");
                let on = e.get("on").and_then(Value::as_bool).unwrap_or(default);
                let key = json::string(e, "message_key");
                Some(Event { message_key: if key.is_empty() { format!("notify.event.{id}") } else { key }, id, default, on })
            })
            .collect();
        let agents = json::object(document, "agents")
            .iter()
            .filter_map(|(agent, choices)| {
                let choices = choices.as_object()?;
                let events =
                    json::object(choices, "events").iter().filter_map(|(id, on)| on.as_bool().map(|on| (id.clone(), on))).collect();
                Some((agent.clone(), AgentChoices { muted: json::boolean(choices, "muted"), events }))
            })
            .collect();
        Settings { events, agents, version: json::integer(document, "version") }
    }

    pub fn event(&self, id: &str) -> Option<&Event> {
        self.events.iter().find(|e| e.id == id)
    }

    /// The contact's own choice for the event: Some(on/off) or None for
    /// "follow the global choice".
    pub fn agent_choice(&self, agent_id: &str, event: &str) -> Option<bool> {
        self.agents.get(agent_id).and_then(|a| a.events.get(event).copied())
    }

    /// What applies for (contact, event): muted wins, then the contact's
    /// own choice, then the global one, then the catalog default.
    pub fn effective(&self, agent_id: &str, event: &str) -> bool {
        let agent = self.agents.get(agent_id);
        if agent.is_some_and(|a| a.muted) {
            return false;
        }
        agent
            .and_then(|a| a.events.get(event).copied())
            .or_else(|| self.event(event).map(|e| e.on))
            .unwrap_or(true)
    }

    /// The change as the Host takes it, applied here at once so the switch
    /// moves before the Host answers (its answer replaces the document).
    pub fn set_global(&mut self, event: &str, on: bool) -> Value {
        if let Some(e) = self.events.iter_mut().find(|e| e.id == event) {
            e.on = on;
        }
        json!({"events": {event: on}})
    }

    /// A contact's choice: Some(on/off), or None to follow the global one.
    /// `agent` is the agent id (the Host also takes the session).
    pub fn set_agent(&mut self, agent: &str, event: &str, choice: Option<bool>) -> Value {
        let choices = self.agents.entry(agent.to_owned()).or_default();
        match choice {
            Some(on) => choices.events.insert(event.to_owned(), on),
            None => choices.events.remove(event),
        };
        json!({"agent": agent, "events": {event: choice}})
    }
}

/// One answer a needs_you notification offers as a button.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Choice {
    pub id: String,
    pub label: String,
}

/// What one `user-notification` says, as the desktop uses it.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Incoming {
    /// The catalog id (`reply` on a Host without the feature).
    pub event: String,
    /// The Host's effective choice; None from a Host without the feature.
    pub alert: Option<bool>,
    pub unread: Option<bool>,
    pub notification_id: String,
    pub message_id: String,
    pub session: String,
    pub agent_id: String,
    pub persona: String,
    pub preview: String,
    /// needs_you: the answers as buttons (at most four).
    pub options: Vec<Choice>,
    /// needs_you: `approval`, `single_choice` or `text_input`.
    pub response_type: String,
    pub decision_id: String,
}

impl Incoming {
    pub fn parse(event: &Object) -> Incoming {
        let first = |keys: &[&str]| keys.iter().map(|k| json::string(event, k)).find(|v| !v.is_empty()).unwrap_or_default();
        let options = json::array(event, "options")
            .iter()
            .filter_map(Value::as_object)
            .map(|o| Choice { id: json::string(o, "id"), label: json::string(o, "label") })
            .filter(|c| !c.id.is_empty() && !c.label.is_empty())
            .take(4)
            .collect();
        let event_id = json::string(event, "event");
        Incoming {
            event: if event_id.is_empty() { "reply".into() } else { event_id },
            alert: event.get("alert").and_then(Value::as_bool),
            unread: event.get("unread").and_then(Value::as_bool),
            notification_id: json::string(event, "notification_id"),
            message_id: first(&["message_id", "source_message_id"]),
            session: json::string(event, "session"),
            agent_id: json::string(event, "agent_id"),
            persona: json::string(event, "persona"),
            preview: json::string(event, "preview"),
            options,
            response_type: json::string(event, "response_type"),
            decision_id: json::string(event, "decision_id"),
        }
    }

    /// Whether it may pop up at all: the Host said alert (or, older, said
    /// nothing), it marks the chat unread, and the contact is not quiet.
    pub fn alerts(&self, quiet: bool) -> bool {
        self.alert.unwrap_or(true) && self.unread != Some(false) && !quiet
    }

    /// Whether the popup asks for the desktop's sound, with the reader's
    /// Sound switch `on`: a question for you and a call ring, the rest is
    /// quiet. A Host without the feature names no event: every popup rings,
    /// as before.
    pub fn sound(&self, on: bool) -> bool {
        on && (self.alert.is_none() || self.event == "needs_you" || self.event == "call")
    }
}

/// The last notification ids seen, so one event pops up once even when
/// the stream sends it again (a reconnect, two Hosts' streams).
#[derive(Debug, Default)]
pub struct Recent {
    ids: VecDeque<String>,
}

impl Recent {
    const KEPT: usize = 64;

    /// True the first time the id is seen; an empty id is always new.
    pub fn first_time(&mut self, id: &str) -> bool {
        if id.is_empty() {
            return true;
        }
        if self.ids.iter().any(|seen| seen == id) {
            return false;
        }
        if self.ids.len() == Self::KEPT {
            self.ids.pop_front();
        }
        self.ids.push_back(id.to_owned());
        true
    }
}

/// The reader's own switches (`notifyreplies`, `notifysound` in `prefs`).
pub const DESKTOP_LABEL: &str = "Desktop notifications";
pub const DESKTOP_ABOUT: &str = "Show notifications on this computer";
pub const SOUND_LABEL: &str = "Sound";
pub const SOUND_ABOUT: &str = "Play the desktop's message sound when an agent asks you something or rings";

/// The events the desktop knows words for, in the Host catalog's order:
/// wire id, `notify.event.<id>`, `notify.settings.about.<id>`.
pub const EVENTS: &[(&str, &str, &str)] = &[
    ("reply", "Replies", "An agent finished and has something for you"),
    ("needs_you", "Questions for you", "An agent asks you something or waits for your answer"),
    ("failed", "Failed turns", "A turn failed and the Host stopped trying"),
    ("update", "Updates", "A note an agent sent you"),
    ("job_done", "Background jobs done", "A background job of an agent ended"),
    ("helper_report", "Helper reports", "A helper reported to the agent that started it"),
    ("call", "Calls", "An agent rings you"),
    ("account", "Account notices", "Account and usage notices"),
];

/// Every other word the desktop shows for notifications, by its stable
/// key. One place, so the localization pipeline can take them over.
pub const STRINGS: &[(&str, &str)] = &[
    ("notify.settings.desktop", DESKTOP_LABEL),
    ("notify.settings.desktop.about", DESKTOP_ABOUT),
    ("notify.settings.sound", SOUND_LABEL),
    ("notify.settings.sound.about", SOUND_ABOUT),
    ("notify.settings.default", "Default"),
    ("notify.settings.on", "On"),
    ("notify.settings.off", "Off"),
    ("notify.settings.contact", "Notifications from this contact"),
    ("notify.action.open", "Open"),
    ("notify.action.mark_read", "Mark read"),
    ("notify.body.needs_you", "Waiting for your answer"),
    ("notify.body.failed", "The turn failed"),
    ("notify.body.reply", "Done, your turn"),
];

/// The English words for a key; the key itself when none is known.
pub fn text(key: &str) -> &str {
    if let Some(id) = key.strip_prefix("notify.event.") {
        return EVENTS.iter().find(|(e, ..)| *e == id).map_or(key, |(_, label, _)| label);
    }
    if let Some(id) = key.strip_prefix("notify.settings.about.") {
        return EVENTS.iter().find(|(e, ..)| *e == id).map_or(key, |(.., about)| about);
    }
    STRINGS.iter().find(|(k, _)| *k == key).map_or(key, |(_, v)| v)
}

/// The words for an event id (`reply` → "Replies").
pub fn event_label(id: &str) -> String {
    let key = format!("notify.event.{id}");
    let words = text(&key);
    if words == key { id.replace('_', " ") } else { words.to_owned() }
}

/// The line under an event's switch.
pub fn event_about(id: &str) -> String {
    let key = format!("notify.settings.about.{id}");
    let words = text(&key);
    if words == key { String::new() } else { words.to_owned() }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn object(value: Value) -> Object {
        value.as_object().cloned().unwrap()
    }

    fn document() -> Settings {
        Settings::parse(&object(json!({
            "events": [
                {"id": "reply", "message_key": "notify.event.reply", "default": true, "on": true},
                {"id": "job_done", "message_key": "notify.event.job_done", "default": false, "on": false},
                {"id": "update", "default": true, "on": false},
            ],
            "agents": {
                "a1": {"muted": false, "events": {"job_done": true, "reply": false}},
                "a2": {"muted": true, "events": {"reply": true}},
            },
            "version": 7,
        })))
    }

    #[test]
    fn the_contact_wins_then_the_global_choice_then_on() {
        let settings = document();
        assert!(!settings.effective("a1", "reply"));
        assert!(settings.effective("a1", "job_done"));
        assert!(!settings.effective("a1", "update"));
        assert!(settings.effective("other", "reply"));
        assert!(!settings.effective("other", "job_done"));
        assert!(settings.effective("other", "unknown"));
        assert_eq!(settings.version, 7);
        assert_eq!(settings.event("update").unwrap().message_key, "notify.event.update");
    }

    #[test]
    fn muted_wins_over_everything() {
        assert!(!document().effective("a2", "reply"));
    }

    #[test]
    fn a_choice_changes_here_and_says_what_the_host_takes() {
        let mut settings = document();
        assert_eq!(settings.set_global("update", true), json!({"events": {"update": true}}));
        assert!(settings.effective("other", "update"));
        assert_eq!(settings.set_agent("a1", "reply", None), json!({"agent": "a1", "events": {"reply": null}}));
        assert_eq!(settings.agent_choice("a1", "reply"), None);
        assert!(settings.effective("a1", "reply"));
        settings.set_agent("a3", "update", Some(false));
        assert_eq!(settings.agent_choice("a3", "update"), Some(false));
    }

    #[test]
    fn an_older_host_alerts_as_before() {
        let old = Incoming::parse(&object(json!({"session": "s", "preview": "hi", "source_message_id": "m1", "unread": true})));
        assert_eq!(old.event, "reply");
        assert_eq!(old.alert, None);
        assert_eq!(old.message_id, "m1");
        assert!(old.alerts(false));
        assert!(!old.alerts(true));
    }

    #[test]
    fn alert_false_or_not_unread_stays_quiet() {
        let off = Incoming::parse(&object(json!({"event": "reply", "alert": false})));
        assert!(!off.alerts(false));
        let read = Incoming::parse(&object(json!({"event": "reply", "alert": true, "unread": false})));
        assert!(!read.alerts(false));
        let on = Incoming::parse(&object(json!({"event": "failed", "alert": true, "unread": true})));
        assert!(on.alerts(false));
    }

    #[test]
    fn a_question_brings_its_answers_and_rings() {
        let asked = Incoming::parse(&object(json!({
            "event": "needs_you", "alert": true, "decision_id": "d1", "response_type": "single_choice",
            "options": [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}, {"id": "", "label": "x"},
                        {"id": "c", "label": "C"}, {"id": "d", "label": "D"}, {"id": "e", "label": "E"}],
        })));
        assert_eq!(asked.options.iter().map(|c| c.id.as_str()).collect::<Vec<_>>(), ["a", "b", "c", "d"]);
        assert!(asked.sound(true));
        assert!(!asked.sound(false));
        assert!(!Incoming::parse(&object(json!({"event": "reply", "alert": true}))).sound(true));
        assert!(Incoming::parse(&object(json!({"preview": "older Host"}))).sound(true));
    }

    #[test]
    fn an_id_pops_up_once() {
        let mut recent = Recent::default();
        assert!(recent.first_time("pn-1"));
        assert!(!recent.first_time("pn-1"));
        assert!(recent.first_time(""));
        assert!(recent.first_time(""));
        for i in 0..Recent::KEPT {
            recent.first_time(&format!("x{i}"));
        }
        assert!(recent.first_time("pn-1"));
    }

    #[test]
    fn every_event_has_words() {
        for id in ["reply", "needs_you", "failed", "update", "job_done", "helper_report", "call", "account"] {
            assert_ne!(event_label(id), id, "{id}");
            assert!(!event_about(id).is_empty(), "{id}");
        }
        assert_eq!(event_label("new_kind"), "new kind");
    }
}
