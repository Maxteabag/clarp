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
use crate::turn_error::{Action, TurnError, Words};

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

/// A contact's notifications in one choice. Presets are only sets of the
/// contact's own choices on the same document: All follows the owner's
/// choice for every event, Only when it needs me turns on a question and a
/// failed turn and the rest off, Muted is the contact's mute. Anything else
/// is Custom.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Preset {
    All,
    NeedsMe,
    Muted,
    Custom,
}

impl Preset {
    /// In the order the choice lists them.
    pub const ALL: [Preset; 4] = [Preset::All, Preset::NeedsMe, Preset::Muted, Preset::Custom];

    pub fn index(self) -> usize {
        Preset::ALL.iter().position(|p| *p == self).unwrap_or(0)
    }

    pub fn from_index(index: usize) -> Preset {
        Preset::ALL.get(index).copied().unwrap_or(Preset::Custom)
    }

    /// Its words (`notify.settings.preset.<name>`).
    pub fn label(self) -> String {
        text(match self {
            Preset::All => "notify.settings.preset.all",
            Preset::NeedsMe => "notify.settings.preset.needs_me",
            Preset::Muted => "notify.settings.preset.muted",
            Preset::Custom => "notify.settings.preset.custom",
        })
    }
}

/// The events "Only when it needs me" keeps on.
pub const NEEDS_ME: &[&str] = &["needs_you", "failed"];

impl Settings {
    /// Which preset the contact's choices are (`muted` from wherever the
    /// app reads the contact's mute, which wins).
    pub fn preset(&self, agent_id: &str, muted: bool) -> Preset {
        let agent = self.agents.get(agent_id);
        if muted || agent.is_some_and(|a| a.muted) {
            return Preset::Muted;
        }
        let choices = agent.map(|a| &a.events);
        if choices.is_none_or(HashMap::is_empty) {
            return Preset::All;
        }
        let needs_me = self.events.iter().all(|e| choices.and_then(|c| c.get(&e.id)) == Some(&NEEDS_ME.contains(&e.id.as_str())));
        if needs_me { Preset::NeedsMe } else { Preset::Custom }
    }

    /// The contact's choices for All or Only when it needs me, applied here
    /// and as the Host takes them; None when nothing changes (or for Muted
    /// and Custom, which are not sets of choices).
    pub fn set_preset(&mut self, agent: &str, preset: Preset) -> Option<Value> {
        let ids: Vec<String> = self.events.iter().map(|e| e.id.clone()).collect();
        let choices = self.agents.entry(agent.to_owned()).or_default();
        let mut events = serde_json::Map::new();
        match preset {
            Preset::All => {
                for (id, _) in choices.events.drain() {
                    events.insert(id, Value::Null);
                }
            }
            Preset::NeedsMe => {
                for id in ids {
                    let on = NEEDS_ME.contains(&id.as_str());
                    if choices.events.insert(id.clone(), on) != Some(on) {
                        events.insert(id, Value::Bool(on));
                    }
                }
            }
            Preset::Muted | Preset::Custom => return None,
        }
        (!events.is_empty()).then(|| json!({"agent": agent, "events": events}))
    }
}

/// One answer a needs_you notification offers as a button.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Choice {
    pub id: String,
    pub label: String,
}

/// A read and an alert are comparable only on the same Host and generation.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct AlertOrder {
    pub epoch: String,
    pub position: (u64, u64),
}

impl AlertOrder {
    pub fn parse(event: &Object, ms: &str, seq: &str) -> Option<Self> {
        let epoch = event.get("order_epoch")?.as_str()?;
        if epoch.trim().is_empty() {
            return None;
        }
        // The desktop receives SSE, whose components are JSON integers.
        let sequence = event.get(seq)?.as_u64()?;
        if sequence == 0 {
            return None;
        }
        Some(Self { epoch: epoch.into(), position: (event.get(ms)?.as_u64()?, sequence) })
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ReadTarget {
    Through(AlertOrder),
    Decision(String),
}

impl ReadTarget {
    pub fn covers(&self, order: Option<&AlertOrder>, decision: &str) -> bool {
        match self {
            Self::Through(end) => order.is_some_and(|o| o.epoch == end.epoch && o.position <= end.position),
            Self::Decision(id) => id == decision,
        }
    }
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
    pub host: String,
    pub order: Option<AlertOrder>,
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
    /// needs_you: the card the question is on (its answers go through it).
    pub artifact_id: String,
    /// The Host's own words by key, for the app to say in its language
    /// (`title_loc_key`/`body_loc_key` and their arguments), and its English
    /// title where it made one.
    pub title: String,
    pub title_loc: Option<(String, Vec<String>)>,
    pub body_loc: Option<(String, Vec<String>)>,
    /// needs_you: the decision's revision when the push says it; else the
    /// app reads it from Updates.
    pub revision: Option<i64>,
    /// failed: what went wrong, for the words and the button.
    pub failure: Option<Failure>,
}

/// A failed turn's notification (the turn-error contract): its code, the
/// catalog key and arguments of its sentence (`error.<code>`, or
/// `error.usage_limit_until` with `time_ms` the client formats), and the
/// one action it offers beside Open.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Failure {
    pub code: String,
    pub body_loc_key: String,
    pub body_loc_args: Vec<String>,
    pub time_ms: Option<i64>,
    /// `retry`, `retry_at`, `fresh_retry`, `compact_retry`, `switch_model`
    /// or `sign_in`; empty when none.
    pub action: String,
    /// `retry_at`: when.
    pub action_at_ms: Option<i64>,
}

impl Failure {
    fn parse(event: &Object) -> Option<Failure> {
        let code = json::string(event, "error_code");
        let body_loc_key = json::string(event, "body_loc_key");
        if code.is_empty() && !body_loc_key.starts_with("error.") {
            return None;
        }
        let args = json::array(event, "body_loc_args")
            .iter()
            .map(|a| a.as_str().map_or_else(|| a.to_string(), str::to_owned))
            .collect();
        // `primary_action`: the id, or {id, at_ms}.
        let (action, at_ms) = match event.get("primary_action") {
            Some(Value::String(id)) => (id.clone(), None),
            Some(Value::Object(o)) => (json::string(o, "id"), o.get("at_ms").and_then(Value::as_i64)),
            _ => (String::new(), None),
        };
        let at_ms = at_ms.or_else(|| event.get("at_ms").and_then(Value::as_i64));
        Some(Failure { code, body_loc_key, body_loc_args: args, time_ms: event.get("time_ms").and_then(Value::as_i64), action, action_at_ms: at_ms })
    }

    /// The action sends the message again (`POST /turn-retry`); the others
    /// (switch_model, sign_in) open the chat.
    pub fn retries(&self) -> bool {
        crate::turn_error::retries(&self.action)
    }

    /// The failure as a failed turn's card has it (`turn_error`), so the
    /// popup says the same sentence and its button does the same: the
    /// arguments named by the key's English placeholders in order (`{time}`
    /// from `time_ms`), the Host's English as the fallback, and the one
    /// action. `copy_details` has no place in a popup.
    pub fn turn_error(&self, message_id: &str, fallback: &str) -> TurnError {
        let mut params = Object::new();
        let template = crate::turn_error_text::english(&self.body_loc_key).unwrap_or_default();
        let mut names: Vec<&str> = Vec::new();
        for part in template.split('{').skip(1) {
            if let Some((name, _)) = part.split_once('}')
                && !names.contains(&name)
            {
                names.push(name);
            }
        }
        for (name, arg) in names.iter().zip(&self.body_loc_args) {
            match *name {
                "time" => {
                    if let Ok(at) = arg.parse::<i64>() {
                        params.insert("time_ms".into(), Value::from(at));
                    }
                }
                _ => {
                    params.insert((*name).into(), Value::from(arg.as_str()));
                }
            }
        }
        if let Some(at) = self.time_ms {
            params.insert("time_ms".into(), Value::from(at));
        }
        let actions = Some(&self.action)
            .filter(|id| !id.is_empty() && id.as_str() != "copy_details")
            .map(|id| Action { id: id.clone(), at_ms: self.action_at_ms, ..Action::default() })
            .into_iter()
            .collect();
        TurnError {
            code: self.code.clone(),
            key: self.body_loc_key.clone(),
            params,
            text: Words { en: fallback.to_owned(), nb: String::new() },
            actions,
            message_id: message_id.to_owned(),
            resets_at_ms: self.time_ms,
            ..TurnError::default()
        }
    }
}

/// A key the Host sent and its arguments as text (numbers as digits).
fn loc(event: &Object, key: &str, args: &str) -> Option<(String, Vec<String>)> {
    let key = json::string(event, key);
    let args = json::array(event, args).iter().map(|a| a.as_str().map_or_else(|| a.to_string(), str::to_owned)).collect();
    (!key.is_empty()).then_some((key, args))
}

impl Incoming {
    /// The Host's text by key in the app's language, else its English
    /// title (empty when it made none).
    pub fn title_text(&self) -> String {
        self.title_loc.as_ref().and_then(|(key, args)| host_text(key, args)).unwrap_or_else(|| self.title.clone())
    }

    /// The Host's body by key in the app's language, when it sent one the
    /// desktop knows (a failed turn's `error.*` reads through its card).
    pub fn body_text(&self) -> Option<String> {
        self.body_loc.as_ref().and_then(|(key, args)| host_text(key, args))
    }

    pub fn parse(event: &Object) -> Incoming {
        let first = |keys: &[&str]| keys.iter().map(|k| json::string(event, k)).find(|v| !v.is_empty()).unwrap_or_default();
        let mut options: Vec<Choice> = json::array(event, "options")
            .iter()
            .filter_map(Value::as_object)
            .map(|o| Choice { id: json::string(o, "id"), label: json::string(o, "label") })
            .filter(|c| !c.id.is_empty() && !c.label.is_empty())
            .take(4)
            .collect();
        let response_type = json::string(event, "response_type");
        // An approval from a Host that sends no answers: Yes and No. The
        // Host's own Yes and No (a card without labels) in the app's words.
        if options.is_empty() && response_type == "approval" {
            options = vec![
                Choice { id: "accepted".into(), label: text("notify.action.yes") },
                Choice { id: "rejected".into(), label: text("notify.action.no") },
            ];
        } else if response_type == "approval" {
            for choice in options.iter_mut() {
                match (choice.id.as_str(), choice.label.as_str()) {
                    ("accepted", "Yes") => choice.label = text("notify.action.yes"),
                    ("rejected", "No") => choice.label = text("notify.action.no"),
                    _ => {}
                }
            }
        }
        let event_id = json::string(event, "event");
        Incoming {
            event: if event_id.is_empty() { "reply".into() } else { event_id },
            alert: event.get("alert").and_then(Value::as_bool),
            unread: event.get("unread").and_then(Value::as_bool),
            host: json::string(event, "server_instance_id"),
            order: if json::string(event, "event") == "call" { None } else { AlertOrder::parse(event, "event_at_ms", "event_seq") },
            notification_id: json::string(event, "notification_id"),
            // A failed turn's is the message it answered (`cause_message_id`).
            message_id: first(&["message_id", "source_message_id", "cause_message_id"]),
            artifact_id: json::string(event, "artifact_id"),
            title: json::string(event, "title"),
            title_loc: loc(event, "title_loc_key", "title_loc_args"),
            body_loc: loc(event, "body_loc_key", "body_loc_args"),
            session: json::string(event, "session"),
            agent_id: json::string(event, "agent_id"),
            persona: json::string(event, "persona"),
            // The Host's English fallback where it has no preview (a failure's sentence).
            preview: first(&["preview", "body"]),
            options,
            response_type,
            decision_id: json::string(event, "decision_id"),
            revision: event.get("revision").and_then(Value::as_i64).filter(|r| *r > 0),
            failure: Failure::parse(event),
        }
    }

    /// Whether it may pop up at all, the contact not quiet: the Host's
    /// `alert` decides where it says one (a question alerts without marking
    /// the chat unread); an older Host's notification pops up when it marks
    /// the chat unread.
    pub fn alerts(&self, quiet: bool) -> bool {
        !quiet && self.alert.unwrap_or(self.unread != Some(false))
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

// i18n-screen: notifications

/// The reader's own switches (`notifyreplies`, `notifysound` in `prefs`), in
/// English for the settings registry; the page shows them through [`text`].
pub const DESKTOP_LABEL: &str = "Desktop notifications";
pub const DESKTOP_ABOUT: &str = "Show notifications on this computer";
pub const SOUND_LABEL: &str = "Sound";
pub const SOUND_ABOUT: &str = "Play the desktop's message sound when an agent asks you something or rings";

/// The event ids the desktop has words for, in the Host catalog's order.
pub const EVENT_IDS: &[&str] = &["reply", "needs_you", "failed", "update", "job_done", "helper_report", "call", "account"];

/// An event's name in the app's language (`notify.event.<id>`, the Host's
/// `message_key`); an id it does not know reads as its words.
pub fn event_label(id: &str) -> String {
    use crate::tr;
    match id {
        // i18n: Settings > Notifications and a contact's profile, label of a switch (noun): an agent's finished reply
        "reply" => tr!("notify.event.reply" => "Replies"),
        // i18n: Settings > Notifications and a contact's profile, label of a switch (noun): an agent asks the user something
        "needs_you" => tr!("notify.event.needs_you" => "Questions for you"),
        // i18n: Settings > Notifications and a contact's profile, label of a switch (noun): an agent's turn that failed
        "failed" => tr!("notify.event.failed" => "Failed turns"),
        // i18n: Settings > Notifications and a contact's profile, label of a switch (noun): notes agents send the user
        "update" => tr!("notify.event.update" => "Updates"),
        // i18n: Settings > Notifications and a contact's profile, label of a switch (noun): an agent's background job ended
        "job_done" => tr!("notify.event.job_done" => "Background jobs done"),
        // i18n: Settings > Notifications and a contact's profile, label of a switch (noun): a helper agent reported to its parent
        "helper_report" => tr!("notify.event.helper_report" => "Helper reports"),
        // i18n: Settings > Notifications and a contact's profile, label of a switch (noun): an agent rings the user
        "call" => tr!("notify.event.call" => "Calls"),
        // i18n: Settings > Notifications and a contact's profile, label of a switch (noun): account and usage notices
        "account" => tr!("notify.event.account" => "Account notices"),
        other => other.replace('_', " "),
    }
}

/// The line under an event's switch (`notify.event.<id>.detail`).
pub fn event_about(id: &str) -> String {
    use crate::tr;
    match id {
        // i18n: Settings > Notifications, line under the Replies switch
        "reply" => tr!("notify.event.reply.detail" => "An agent finished and has something for you"),
        // i18n: Settings > Notifications, line under the Questions for you switch
        "needs_you" => tr!("notify.event.needs_you.detail" => "An agent asks you something or waits for your answer"),
        // i18n: Settings > Notifications, line under the Failed turns switch
        "failed" => tr!("notify.event.failed.detail" => "A turn failed and the Host stopped trying"),
        // i18n: Settings > Notifications, line under the Updates switch
        "update" => tr!("notify.event.update.detail" => "A note an agent sent you"),
        // i18n: Settings > Notifications, line under the Background jobs done switch
        "job_done" => tr!("notify.event.job_done.detail" => "A background job of an agent ended"),
        // i18n: Settings > Notifications, line under the Helper reports switch
        "helper_report" => tr!("notify.event.helper_report.detail" => "A helper reported to the agent that started it"),
        // i18n: Settings > Notifications, line under the Calls switch
        "call" => tr!("notify.event.call.detail" => "An agent rings you"),
        // i18n: Settings > Notifications, line under the Account notices switch
        "account" => tr!("notify.event.account.detail" => "Account and usage notices"),
        _ => String::new(),
    }
}

/// The desktop's own notification words by key, in the app's language; the
/// key itself for one it does not know. Words with a value in them have a
/// function of their own ([`mute_label`], [`you_answered`]).
pub fn text(key: &str) -> String {
    use crate::tr;
    match key {
        // i18n: Settings > Notifications, switch label: this computer's notification popups on or off
        "notify.settings.desktop" => tr!("notify.settings.desktop" => "Desktop notifications"),
        // i18n: Settings > Notifications, line under the Desktop notifications switch
        "notify.settings.desktop.detail" => tr!("notify.settings.desktop.detail" => "Show notifications on this computer"),
        // i18n: Settings > Notifications, switch label (noun): a sound with questions and calls
        "notify.settings.sound" => tr!("notify.settings.sound" => "Sound"),
        // i18n: Settings > Notifications, line under the Sound switch
        "notify.settings.sound.detail" => tr!("notify.settings.sound.detail" => "Play the desktop's message sound when an agent asks you something or rings"),
        // i18n: contact profile > Notifications, choice meaning "as set for everyone", shown as "Default · On"
        "notify.settings.default" => tr!("notify.settings.default" => "Default"),
        // i18n: contact profile > Notifications, choice (adjective): this event notifies
        "notify.settings.on" => tr!("notify.settings.on" => "On"),
        // i18n: contact profile > Notifications, choice (adjective): this event stays silent
        "notify.settings.off" => tr!("notify.settings.off" => "Off"),
        // i18n: contact profile > Notifications, button (verb): shows a choice per event
        "notify.settings.customize" => tr!("notify.settings.customize" => "Customize"),
        // i18n: contact profile > Notifications, preset choice: every notification as set for everyone
        "notify.settings.preset.all" => tr!("notify.settings.preset.all" => "All"),
        // i18n: contact profile > Notifications, preset choice: only questions and failed turns
        "notify.settings.preset.needs_me" => tr!("notify.settings.preset.needs_me" => "Only when it needs me"),
        // i18n: contact profile > Notifications, preset choice (adjective): nothing from this contact
        "notify.settings.preset.muted" => tr!("notify.settings.preset.muted" => "Muted"),
        // i18n: contact profile > Notifications, preset choice (adjective): its own choice per event
        "notify.settings.preset.custom" => tr!("notify.settings.preset.custom" => "Custom"),
        // i18n: desktop notification, button (verb): opens the chat at the message
        "notify.action.open" => tr!("notify.action.open" => "Open"),
        // i18n: desktop notification, button (verb): marks the chat read without opening it
        "notify.action.mark_read" => tr!("notify.action.mark_read" => "Mark read"),
        // i18n: desktop notification, button (noun): opens Settings > Notifications
        "notify.action.settings" => tr!("notify.action.settings" => "Settings"),
        // i18n: desktop notification, button that approves an agent's request when the card names no label
        "notify.action.yes" => tr!("notify.action.yes" => "Yes"),
        // i18n: desktop notification, button that declines an agent's request when the card names no label
        "notify.action.no" => tr!("notify.action.no" => "No"),
        // i18n: desktop notification, button (verb): takes back the answer just given to an agent's question
        "notify.action.undo" => tr!("notify.action.undo" => "Undo"),
        // i18n: desktop notification, body when an agent's question has no text of its own
        "notify.body.needs_you" => tr!("notify.body.needs_you" => "Waiting for your answer"),
        // i18n: desktop notification, body when a failed turn has no text of its own
        "notify.body.failed" => tr!("notify.body.failed" => "The turn failed"),
        // i18n: desktop notification, body when Undo came after the agent got the answer
        "notify.body.too_late" => tr!("notify.body.too_late" => "Too late to undo: the answer was sent"),
        _ => host_text(key, &[]).unwrap_or_else(|| key.to_owned()),
    }
}

/// "Mute <name>", a notification's button (`notify.action.mute`).
pub fn mute_label(name: &str) -> String {
    // i18n: desktop notification, button (verb): no more notifications from this contact; {} is the contact's name
    crate::tr!("notify.action.mute" => "Mute {}", name)
}

/// What was answered from a notification (`notify.body.you_answered`).
pub fn you_answered(answer: &str) -> String {
    // i18n: desktop notification, body after answering from the notification; {} is the chosen answer's label
    crate::tr!("notify.body.you_answered" => "You answered {}", answer)
}

/// A text the Host made, by the key it sent (`title_loc_key` or
/// `body_loc_key`) and its arguments, in the app's language; None for a key
/// the desktop does not know (the Host's English stands then). `error.*`
/// keys are a failed turn's and read through [`Failure::turn_error`].
pub fn host_text(key: &str, args: &[String]) -> Option<String> {
    use crate::tr;
    let a = |i: usize| args.get(i).cloned().unwrap_or_default();
    Some(match key {
        "notify.event.reply" | "notify.event.needs_you" | "notify.event.failed" | "notify.event.update" | "notify.event.job_done"
        | "notify.event.helper_report" | "notify.event.call" | "notify.event.account" => event_label(&key["notify.event.".len()..]),
        // i18n: desktop notification, body when an agent finished without words to show
        "notify.body.done" => tr!("notify.body.done" => "Done, your turn"),
        // i18n: desktop notification, title when an agent bound to privacy answered; {} is the agent's name
        "notify.body.answered" => tr!("notify.body.answered" => "{} answered", a(0)),
        // i18n: desktop notification, title when an agent bound to privacy asks something; {} is the agent's name
        "notify.body.asks" => tr!("notify.body.asks" => "{} asks", a(0)),
        // i18n: desktop notification, body of a private agent's question: its words are not shown
        "notify.body.open_to_answer" => tr!("notify.body.open_to_answer" => "Open Clarp to answer."),
        // i18n: desktop notification, title of an agent's call ringing; {} is the agent's name
        "notify.body.call_ringing" => tr!("notify.body.call_ringing" => "{} is calling", a(0)),
        // i18n: desktop notification, title of a call not answered; {} is the agent's name
        "notify.body.call_missed" => tr!("notify.body.call_missed" => "Missed call from {}", a(0)),
        // i18n: desktop notification, title of a call the user declined; {} is the agent's name
        "notify.body.call_declined" => tr!("notify.body.call_declined" => "Declined call from {}", a(0)),
        // i18n: desktop notification, title of a call going on; {} is the agent's name
        "notify.body.call_answered" => tr!("notify.body.call_answered" => "Call with {}", a(0)),
        // i18n: desktop notification, title when the calling agent hung up before it was answered; {} is the agent's name
        "notify.body.call_cancelled" => tr!("notify.body.call_cancelled" => "{} hung up", a(0)),
        // i18n: desktop notification, title of a call when the caller is not named
        "notify.body.call_incoming" => tr!("notify.body.call_incoming" => "Incoming call"),
        // i18n: desktop notification, title of a failed turn; {} is the agent's name
        "notify.body.failed_title" => tr!("notify.body.failed_title" => "{} couldn't finish", a(0)),
        // i18n: desktop notification, body when an agent's background job succeeded; {} is the job's title
        "notify.body.job_succeeded" => tr!("notify.body.job_succeeded" => "Finished: {}", a(0)),
        // i18n: desktop notification, body when an agent's background job failed; {} is the job's title
        "notify.body.job_failed" => tr!("notify.body.job_failed" => "Failed: {}", a(0)),
        // i18n: desktop notification, body when a helper agent reported to its parent; {} is the helper's name
        "notify.body.helper_reported" => tr!("notify.body.helper_reported" => "{} reported back", a(0)),
        _ => return None,
    })
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
    fn presets_are_sets_of_the_contacts_choices() {
        let mut settings = document();
        assert_eq!(settings.preset("a1", false), Preset::Custom);
        assert_eq!(settings.preset("a2", false), Preset::Muted);
        assert_eq!(settings.preset("other", false), Preset::All);
        assert_eq!(settings.preset("other", true), Preset::Muted);
        let body = settings.set_preset("other", Preset::NeedsMe).unwrap();
        assert_eq!(body["events"], json!({"reply": false, "job_done": false, "update": false}));
        // The document here names no needs_you or failed: only its events count.
        assert_eq!(settings.preset("other", false), Preset::NeedsMe);
        assert_eq!(settings.set_preset("other", Preset::NeedsMe), None);
        let body = settings.set_preset("a1", Preset::All).unwrap();
        assert_eq!(body["events"]["job_done"], Value::Null);
        assert_eq!(settings.preset("a1", false), Preset::All);
        assert_eq!(settings.set_preset("a1", Preset::All), None);
        assert_eq!(settings.set_preset("a1", Preset::Muted), None);
        for preset in Preset::ALL {
            assert_eq!(Preset::from_index(preset.index()), preset);
            assert!(!preset.label().starts_with("notify."));
        }
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
    fn alert_decides_and_an_older_hosts_read_stays_quiet() {
        let off = Incoming::parse(&object(json!({"event": "reply", "alert": false})));
        assert!(!off.alerts(false));
        // A question alerts without marking the chat unread.
        let asked = Incoming::parse(&object(json!({"event": "needs_you", "alert": true, "unread": false})));
        assert!(asked.alerts(false));
        assert!(!asked.alerts(true), "never from a quiet contact");
        let read = Incoming::parse(&object(json!({"preview": "older Host", "unread": false})));
        assert!(!read.alerts(false));
        let on = Incoming::parse(&object(json!({"event": "failed", "alert": true, "unread": true, "body": "You hit the usage limit."})));
        assert!(on.alerts(false));
        assert_eq!(on.preview, "You hit the usage limit.", "the Host's English fallback when there is no preview");
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
    fn a_failure_brings_its_key_time_and_action() {
        let failed = Incoming::parse(&object(json!({
            "event": "failed", "alert": true, "error_code": "usage_limit", "body_loc_key": "error.usage_limit_until",
            "body_loc_args": ["Claude", 1795800000000i64], "time_ms": 1795800000000i64, "body": "Your Claude account is out of usage.",
            "primary_action": {"id": "retry_at", "at_ms": 1795800000000i64}, "message_id": "m9",
        })));
        let failure = failed.failure.clone().unwrap();
        assert_eq!(failure.body_loc_key, "error.usage_limit_until");
        assert_eq!(failure.body_loc_args, ["Claude", "1795800000000"]);
        assert_eq!((failure.time_ms, failure.action.as_str(), failure.action_at_ms), (Some(1795800000000), "retry_at", Some(1795800000000)));
        assert!(failure.retries());
        assert_eq!(failed.preview, "Your Claude account is out of usage.");
        let plain = Incoming::parse(&object(json!({"event": "failed", "error_code": "auth", "primary_action": "sign_in"})));
        assert!(!plain.failure.unwrap().retries());
        assert_eq!(Incoming::parse(&object(json!({"event": "reply"}))).failure, None);
    }

    #[test]
    fn a_failure_reads_as_its_card_does() {
        let failure = Incoming::parse(&object(json!({
            "event": "failed", "error_code": "usage_limit", "body_loc_key": "error.usage_limit_until",
            "body_loc_args": ["Claude", "1795800000000"], "time_ms": 1795800000000i64, "primary_action": "retry_at",
            "body": "Your Claude account is out of usage.",
        })))
        .failure
        .unwrap();
        let error = failure.turn_error("m9", "Your Claude account is out of usage.");
        assert_eq!(error.params.get("backend"), Some(&json!("Claude")));
        assert_eq!(error.params.get("time_ms"), Some(&json!(1795800000000i64)));
        assert_eq!((error.message_id.as_str(), error.actions.len()), ("m9", 1));
        // retry_at with no time of its own retries at the reset.
        assert_eq!(error.retry_body("s", &error.actions[0]), Some(json!({"session": "s", "message_id": "m9", "at_ms": 1795800000000i64})));
        let sentence = error.sentence_in("en", 1795790000000);
        assert!(sentence.starts_with("Your Claude account is out of usage until ") && !sentence.contains('{'), "{sentence}");
        // A key the catalog lacks: the Host's English.
        let unknown = Failure { body_loc_key: "error.brand_new".into(), action: "copy_details".into(), ..Failure::default() };
        let error = unknown.turn_error("m1", "Something new broke.");
        assert_eq!(error.sentence_in("en", 0), "Something new broke.");
        assert!(error.actions.is_empty(), "no Copy diagnostics in a popup");
    }

    #[test]
    fn an_approval_answers_yes_or_no_and_keeps_its_revision() {
        let named = Incoming::parse(&object(json!({
            "event": "needs_you", "decision_id": "d2", "response_type": "approval", "revision": 5,
            "options": [{"id": "accepted", "label": "Ship it"}, {"id": "rejected", "label": "Hold"}],
        })));
        assert_eq!(named.options.iter().map(|c| c.label.as_str()).collect::<Vec<_>>(), ["Ship it", "Hold"]);
        assert_eq!(named.revision, Some(5));
        let older = Incoming::parse(&object(json!({"event": "needs_you", "decision_id": "d3", "response_type": "approval"})));
        assert_eq!(older.options, [Choice { id: "accepted".into(), label: "Yes".into() }, Choice { id: "rejected".into(), label: "No".into() }]);
        assert_eq!(older.revision, None);
        let plain = Incoming::parse(&object(json!({"event": "needs_you", "decision_id": "d5", "response_type": "approval",
            "options": [{"id": "accepted", "label": "Yes"}, {"id": "rejected", "label": "No"}]})));
        assert_eq!(plain.options[0].label, text("notify.action.yes"), "the Host's default Yes in the app's words");
        let typed = Incoming::parse(&object(json!({"event": "needs_you", "decision_id": "d4", "response_type": "text_input"})));
        assert!(typed.options.is_empty(), "a text answer has no buttons");
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
        for id in EVENT_IDS {
            assert_ne!(event_label(id), *id, "{id}");
            assert!(!event_about(id).is_empty(), "{id}");
        }
        assert_eq!(event_label("new_kind"), "new kind");
        assert_eq!(host_text("notify.event.reply", &[]).as_deref(), Some("Replies"), "the Host's message_key reads as the title");
    }

    #[test]
    fn a_notification_reads_the_hosts_keys() {
        let failed = Incoming::parse(&object(json!({
            "event": "failed", "title": "Vera couldn't finish", "title_loc_key": "notify.body.failed_title", "title_loc_args": ["Vera"],
            "body_loc_key": "error.auth", "body_loc_args": ["Claude"], "cause_message_id": "m7", "source_message_id": "",
        })));
        assert_eq!(failed.title_text(), "Vera couldn't finish");
        assert_eq!(failed.body_text(), None, "the card's sentence says it");
        assert_eq!(failed.message_id, "m7", "the message the failed turn answered");
        let job = Incoming::parse(&object(json!({"event": "job_done", "body_loc_key": "notify.body.job_failed", "body_loc_args": ["Backup"], "title": "Nightly"})));
        assert_eq!(job.body_text().as_deref(), Some("Failed: Backup"));
        assert_eq!(job.title_text(), "Nightly", "no title key: the Host's English");
        let asked = Incoming::parse(&object(json!({"event": "needs_you", "artifact_id": "q1", "decision_id": "d1"})));
        assert_eq!(asked.artifact_id, "q1");
    }

    #[test]
    fn the_desktop_says_the_hosts_texts_by_key() {
        let args = ["Vera".to_owned()];
        assert_eq!(host_text("notify.body.failed_title", &args).as_deref(), Some("Vera couldn't finish"));
        assert_eq!(host_text("notify.body.job_succeeded", &["Nightly build".to_owned()]).as_deref(), Some("Finished: Nightly build"));
        assert_eq!(host_text("notify.body.done", &[]).as_deref(), Some("Done, your turn"));
        assert_eq!(host_text("notify.body.brand_new", &args), None, "the Host's English stands");
        assert_eq!(host_text("error.auth", &args), None, "a failed turn's sentence is the card's");
        assert_eq!(text("notify.body.done"), "Done, your turn");
        assert_eq!(text("notify.unknown"), "notify.unknown");
        assert_eq!(mute_label("Vera"), "Mute Vera");
        assert_eq!(you_answered("Now"), "You answered Now");
    }
}
