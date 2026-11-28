//! What the user can be notified of, one event at a time, and whether they are.
//!
//! This is the one list of notification events. Each has a wire id that is never renamed, a
//! message key the apps translate (`notify.event.<id>`), whether it is on when the owner chose
//! nothing, whether it makes a sound, how much it may interrupt and how relevant it is beside
//! others on a phone's lock screen.
//!
//! The owner's choices are kept on the Host, so every device of theirs agrees: one choice per
//! event for all agents, and per agent an override of any event. An agent's override wins,
//! then the global choice, then the catalog's default. An agent the owner muted pushes nothing
//! at all, whatever the choices say. Off means no push and no desktop popup; the badge and the
//! unread mark are the conversation's state and stay.
//!
//! The global choice of `call` is not kept here: it is the ring switch, which the rings keep
//! (`rings.enabled`). This module is handed it wherever it decides.

use std::collections::BTreeMap;

use serde::{Deserialize, Serialize};
use serde_json::{Value, json};

/// A notification event.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Default)]
pub enum Event {
    /// A turn ended with something for the user: a written reply or speech.
    #[default]
    Reply,
    /// An agent asked the user something: a decision or question card, a text request, or a
    /// reply while a decision waits.
    NeedsYou,
    /// A turn failed and the Host has stopped trying.
    Failed,
    /// An agent's note to the user (`tell-user`, `POST /agent-notes`).
    Update,
    /// A background job of an agent ended, well or not.
    JobDone,
    /// A helper reported to the agent that made it.
    HelperReport,
    /// An agent rings.
    Call,
    /// An account or quota notice.
    Account,
}

/// What the catalog says of an event.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Spec {
    pub event: Event,
    /// The wire id: never renamed.
    pub id: &'static str,
    /// The key the apps translate the event's name by.
    pub message_key: &'static str,
    /// On where the owner chose nothing.
    pub default_on: bool,
    /// The push makes a sound.
    pub sound: bool,
    /// How much its push may interrupt (`interruption-level`): `active` respects Focus and Do
    /// Not Disturb. A request that blocks the agent and cannot wait is raised to
    /// `time-sensitive` where it is made ([`crate::push::needs_interruption`]).
    pub level: &'static str,
    /// `aps.relevance-score`: what the lock screen puts first.
    pub relevance: f64,
}

/// Every event, in the order the apps list them.
pub const CATALOG: [Spec; 8] = [
    Spec {
        event: Event::Reply,
        id: "reply",
        message_key: "notify.event.reply",
        default_on: true,
        sound: false,
        level: "active",
        relevance: 0.5,
    },
    Spec {
        event: Event::NeedsYou,
        id: "needs_you",
        message_key: "notify.event.needs_you",
        default_on: true,
        sound: true,
        level: "active",
        relevance: 1.0,
    },
    Spec {
        event: Event::Failed,
        id: "failed",
        message_key: "notify.event.failed",
        default_on: true,
        sound: false,
        level: "active",
        relevance: 0.8,
    },
    Spec {
        event: Event::Update,
        id: "update",
        message_key: "notify.event.update",
        default_on: true,
        sound: false,
        level: "active",
        relevance: 0.3,
    },
    // The agent usually says itself that its job ended: off unless the owner wants both.
    Spec {
        event: Event::JobDone,
        id: "job_done",
        message_key: "notify.event.job_done",
        default_on: false,
        sound: false,
        level: "active",
        relevance: 0.3,
    },
    // The parent relays what its helper reported: off unless the owner wants both.
    Spec {
        event: Event::HelperReport,
        id: "helper_report",
        message_key: "notify.event.helper_report",
        default_on: false,
        sound: false,
        level: "active",
        relevance: 0.3,
    },
    Spec {
        event: Event::Call,
        id: "call",
        message_key: "notify.event.call",
        default_on: true,
        sound: true,
        level: "time-sensitive",
        relevance: 1.0,
    },
    Spec {
        event: Event::Account,
        id: "account",
        message_key: "notify.event.account",
        default_on: true,
        sound: false,
        level: "active",
        relevance: 0.3,
    },
];

impl Event {
    pub fn spec(self) -> &'static Spec {
        // Every event is in the catalog: the test below holds it.
        CATALOG
            .iter()
            .find(|spec| spec.event == self)
            .unwrap_or(&CATALOG[0])
    }

    pub fn id(self) -> &'static str {
        self.spec().id
    }

    /// The event a wire id names. `None` for an id nobody knows.
    pub fn named(id: &str) -> Option<Self> {
        CATALOG
            .iter()
            .find(|spec| spec.id == id)
            .map(|spec| spec.event)
    }
}

/// Whether an event is on for an agent: its override, else the global choice, else the
/// catalog's default.
pub fn effective(event: Event, agent_override: Option<bool>, global: Option<bool>) -> bool {
    agent_override.or(global).unwrap_or(event.spec().default_on)
}

/// The key a setting is kept under among the Host's settings.
pub const KEY: &str = "notify.events";

/// The owner's choices, as they are kept. The global choice of `call` is never in `events`:
/// the ring switch is that fact.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct Settings {
    /// The global choice of each event the owner made one for, by wire id.
    #[serde(default)]
    pub events: BTreeMap<String, bool>,
    /// Each agent's overrides, by its id, by wire id.
    #[serde(default)]
    pub agents: BTreeMap<String, BTreeMap<String, bool>>,
    /// Grows with every change: an app that holds a newer one ignores an older.
    #[serde(default)]
    pub version: u64,
}

/// A change refused, with the code a client is told.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Refused {
    pub code: &'static str,
    pub message: String,
}

fn refused(code: &'static str, message: impl Into<String>) -> Refused {
    Refused {
        code,
        message: message.into(),
    }
}

/// What a change asked of the one fact this module does not keep.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct Changed {
    /// The ring switch is to be set to this.
    pub call: Option<bool>,
}

impl Settings {
    /// From what is kept. What cannot be read, and an id nobody knows, is no choice.
    pub fn stored(text: Option<&str>) -> Self {
        let mut settings: Self = text
            .and_then(|text| serde_json::from_str(text).ok())
            .unwrap_or_default();
        let known = |id: &String, _: &mut bool| Event::named(id).is_some();
        settings.events.retain(known);
        settings.events.remove(Event::Call.id());
        for overrides in settings.agents.values_mut() {
            overrides.retain(known);
        }
        settings.agents.retain(|_, overrides| !overrides.is_empty());
        settings
    }

    /// As it is kept.
    pub fn kept(&self) -> String {
        serde_json::to_string(self).unwrap_or_default()
    }

    pub fn global(&self, event: Event) -> Option<bool> {
        self.events.get(event.id()).copied()
    }

    pub fn of_agent(&self, agent_id: &str, event: Event) -> Option<bool> {
        self.agents.get(agent_id)?.get(event.id()).copied()
    }

    /// Whether the owner is alerted of this event of this agent: never of a muted one.
    /// `rings_on`: the ring switch, the global choice of `call`.
    pub fn alerts(&self, event: Event, agent_id: &str, muted: bool, rings_on: bool) -> bool {
        let global = if event == Event::Call {
            Some(rings_on)
        } else {
            self.global(event)
        };
        !muted && effective(event, self.of_agent(agent_id, event), global)
    }

    /// Applies what `POST /notification-settings` asked: `{events: {<id>: bool}}` for every
    /// agent, or, with `agent` (the agent's id, resolved by the caller), `{<id>: bool|null}`
    /// for that agent, `null` following the global choice again. Nothing is changed when any
    /// part is refused. The version grows by one.
    pub fn change(&mut self, asked: &Value, agent: Option<&str>) -> Result<Changed, Refused> {
        let Some(events) = asked.get("events").and_then(Value::as_object) else {
            return Err(refused(
                "events_required",
                "events must be an object of event ids",
            ));
        };
        let mut wanted = Vec::with_capacity(events.len());
        for (id, value) in events {
            let Some(event) = Event::named(id) else {
                return Err(refused(
                    "unknown_event",
                    format!("unknown notification event: {id}"),
                ));
            };
            let choice = match value {
                Value::Bool(on) => Some(*on),
                Value::Null if agent.is_some() => None,
                _ => {
                    let allowed = if agent.is_some() {
                        "true, false or null"
                    } else {
                        "true or false"
                    };
                    return Err(refused("invalid_choice", format!("{id} must be {allowed}")));
                }
            };
            wanted.push((event, choice));
        }
        let mut changed = Changed::default();
        match agent {
            Some(agent) => {
                let overrides = self.agents.entry(agent.to_string()).or_default();
                for (event, choice) in wanted {
                    match choice {
                        Some(on) => overrides.insert(event.id().to_string(), on),
                        None => overrides.remove(event.id()),
                    };
                }
                if overrides.is_empty() {
                    self.agents.remove(agent);
                }
            }
            None => {
                for (event, choice) in wanted {
                    let on = choice.unwrap_or(event.spec().default_on);
                    if event == Event::Call {
                        changed.call = Some(on);
                    } else {
                        self.events.insert(event.id().to_string(), on);
                    }
                }
            }
        }
        self.version += 1;
        Ok(changed)
    }

    /// A change made elsewhere (the ring switch, an agent's mute) is a change of the document:
    /// its version grows by one.
    pub fn touch(&mut self) {
        self.version += 1;
    }

    /// An agent that is gone takes its overrides along. Answers whether it had any.
    pub fn forget(&mut self, agent_id: &str) -> bool {
        self.agents.remove(agent_id).is_some()
    }

    /// The document `GET /notification-settings` answers and `notification-settings-changed`
    /// carries. `agents`: every agent there is, with its session and whether it is muted;
    /// those with an override or a mute are listed.
    pub fn document<'a>(
        &self,
        rings_on: bool,
        agents: impl IntoIterator<Item = (&'a str, &'a str, bool)>,
    ) -> Value {
        let events: Vec<Value> = CATALOG
            .iter()
            .map(|spec| {
                let global = if spec.event == Event::Call {
                    Some(rings_on)
                } else {
                    self.global(spec.event)
                };
                json!({
                    "id": spec.id,
                    "message_key": spec.message_key,
                    "default": spec.default_on,
                    "on": effective(spec.event, None, global),
                })
            })
            .collect();
        let mut listed = serde_json::Map::new();
        for (agent_id, session, muted) in agents {
            let overrides = self.agents.get(agent_id);
            if !muted && overrides.is_none() {
                continue;
            }
            listed.insert(
                agent_id.to_string(),
                json!({
                    "session": session,
                    "muted": muted,
                    "events": overrides.cloned().unwrap_or_default(),
                }),
            );
        }
        json!({ "events": events, "agents": listed, "version": self.version })
    }
}

/// The type of the event every app is told a change by.
pub const CHANGED: &str = "notification-settings-changed";

/// A text the Host makes for a notification: a key the apps translate and its arguments.
/// The English text goes beside it, for an app that has no translation of the key.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct Loc {
    pub key: String,
    /// Texts and numbers, in order: a count is a number, so each app picks its own plural
    /// form; a moment would be epoch milliseconds. Never prose the Host formatted.
    pub args: Vec<Value>,
}

impl Loc {
    pub fn new(key: &str, args: &[&str]) -> Self {
        Self {
            key: key.to_string(),
            args: args.iter().map(|arg| json!(arg)).collect(),
        }
    }

    /// The English text: the template of its key with the arguments in place of `{1}`,
    /// `{2}`, ...
    pub fn english(&self) -> String {
        let mut text = english(&self.key).to_string();
        for (at, arg) in self.args.iter().enumerate() {
            let arg = match arg {
                Value::String(said) => said.clone(),
                other => other.to_string(),
            };
            text = text.replace(&format!("{{{}}}", at + 1), &arg);
        }
        text
    }
}

/// The English text of every key the Host sends, with `{1}`, `{2}` for the arguments in
/// order. The apps keep the same keys in their own string catalogs.
pub const STRINGS: [(&str, &str); 23] = [
    ("notify.event.reply", "Replies"),
    ("notify.event.needs_you", "Questions for you"),
    ("notify.event.failed", "Failed turns"),
    ("notify.event.update", "Updates"),
    ("notify.event.job_done", "Finished jobs"),
    ("notify.event.helper_report", "Helper reports"),
    ("notify.event.call", "Calls"),
    ("notify.event.account", "Accounts"),
    ("notify.body.done", "Done — your turn 👋"),
    ("notify.body.answered", "{1} answered"),
    ("notify.body.asks", "{1} asks"),
    ("notify.body.open_to_answer", "Open Clarp to answer."),
    ("notify.body.call_ringing", "{1} is calling"),
    ("notify.body.call_missed", "Missed call from {1}"),
    ("notify.body.call_declined", "Declined call from {1}"),
    ("notify.body.call_answered", "Call with {1}"),
    ("notify.body.call_cancelled", "{1} hung up"),
    ("notify.body.call_incoming", "Incoming call"),
    ("notify.body.failed_title", "{1} couldn't finish"),
    ("notify.body.failed_withheld", "Open Clarp to see why."),
    ("notify.body.job_succeeded", "Finished: {1}"),
    ("notify.body.job_failed", "Failed: {1}"),
    ("notify.body.helper_reported", "{1} reported back"),
];

/// The key a chat's preview line is translated by, from what the Host's preview formatter
/// found the line to be (`preview_parts`: `{kind, ...}`). The keys are the chat list's own
/// (`preview.*`), so the apps keep each string once. `None` for plain text, which is the
/// agent's words, and for anything nobody knows: then the English body stands alone.
pub fn preview_loc(parts: &Value) -> Option<Loc> {
    let text = |key: &str| json!(parts[key].as_str().unwrap_or(""));
    let count = |key: &str| json!(parts[key].as_u64().unwrap_or(0));
    let loc = |key: &str, args: Vec<Value>| {
        Some(Loc {
            key: key.to_string(),
            args,
        })
    };
    match parts["kind"].as_str()? {
        "image" if parts["caption"].as_str().unwrap_or("").is_empty() => {
            loc("preview.image", Vec::new())
        }
        "image" => loc("preview.image_caption", vec![text("caption")]),
        kind @ ("images" | "files" | "attachments") => {
            loc(&format!("preview.{kind}"), vec![count("count")])
        }
        "file" => loc("preview.file", vec![text("name")]),
        "code" => loc("preview.code", Vec::new()),
        "decision" => loc("preview.decision", vec![text("question"), text("outcome")]),
        "tool" => {
            let category = parts["category"].as_str().unwrap_or("");
            let well_formed = !category.is_empty()
                && category
                    .chars()
                    .all(|c| c.is_ascii_alphanumeric() || c == '_');
            if !well_formed {
                return None;
            }
            loc(&format!("preview.tool.{category}"), vec![text("label")])
        }
        _ => None,
    }
}

/// The English template of a key; the key itself for one nobody knows.
pub fn english(key: &str) -> &str {
    STRINGS
        .iter()
        .find(|(known, _)| *known == key)
        .map_or(key, |(_, text)| *text)
}

/// The id a failed turn is told under: once per turn, however often it was tried.
pub fn failed_id(agent_id: &str, turn: u64) -> String {
    format!(
        "failed-{}",
        crate::notify::id(agent_id, turn as i64).trim_start_matches("pn-")
    )
}

/// The id a job's end is told under: once per run and state.
pub fn job_id(job_id: &str, generation: u64, state: &str) -> String {
    format!("job-{job_id}-{generation}-{state}")
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use super::*;

    #[test]
    fn every_event_is_in_the_catalog_once_with_its_key() {
        let all = [
            Event::Reply,
            Event::NeedsYou,
            Event::Failed,
            Event::Update,
            Event::JobDone,
            Event::HelperReport,
            Event::Call,
            Event::Account,
        ];
        assert_eq!(all.len(), CATALOG.len());
        for event in all {
            let spec = event.spec();
            assert_eq!(spec.event, event);
            assert_eq!(Event::named(spec.id), Some(event));
            assert_eq!(spec.message_key, format!("notify.event.{}", spec.id));
            assert_ne!(english(spec.message_key), spec.message_key);
        }
        assert_eq!(Event::named("replies"), None);
        // Off by default: what the agent relays itself.
        let off: Vec<&str> = CATALOG
            .iter()
            .filter(|spec| !spec.default_on)
            .map(|spec| spec.id)
            .collect();
        assert_eq!(off, ["job_done", "helper_report"]);
        // Sound only where someone waits for the user.
        let loud: Vec<&str> = CATALOG
            .iter()
            .filter(|spec| spec.sound)
            .map(|spec| spec.id)
            .collect();
        assert_eq!(loud, ["needs_you", "call"]);
    }

    #[test]
    fn an_agents_override_wins_then_the_global_choice_then_the_default() {
        assert!(effective(Event::Reply, None, None));
        assert!(!effective(Event::JobDone, None, None));
        assert!(!effective(Event::Reply, None, Some(false)));
        assert!(effective(Event::Reply, Some(true), Some(false)));
        assert!(!effective(Event::Reply, Some(false), Some(true)));
        assert!(effective(Event::JobDone, None, Some(true)));
    }

    #[test]
    fn a_muted_agent_alerts_of_nothing_and_rings_follow_the_ring_switch() {
        let mut settings = Settings::default();
        settings
            .change(&json!({"events": {"needs_you": true}}), Some("a1"))
            .unwrap();
        assert!(!settings.alerts(Event::NeedsYou, "a1", true, true));
        assert!(settings.alerts(Event::NeedsYou, "a1", false, true));
        assert!(!settings.alerts(Event::Call, "a2", false, false));
        assert!(settings.alerts(Event::Call, "a2", false, true));
    }

    #[test]
    fn a_change_refuses_unknown_ids_and_wrong_values_and_changes_nothing() {
        let mut settings = Settings::default();
        let before = settings.clone();
        let unknown = settings.change(&json!({"events": {"reply": false, "nope": true}}), None);
        assert_eq!(unknown.unwrap_err().code, "unknown_event");
        let null = settings.change(&json!({"events": {"reply": null}}), None);
        assert_eq!(null.unwrap_err().code, "invalid_choice");
        let text = settings.change(&json!({"events": {"reply": "off"}}), Some("a1"));
        assert_eq!(text.unwrap_err().code, "invalid_choice");
        let missing = settings.change(&json!({"agent": "a1"}), Some("a1"));
        assert_eq!(missing.unwrap_err().code, "events_required");
        assert_eq!(settings, before);
    }

    #[test]
    fn the_global_call_choice_is_the_ring_switch_and_not_kept_here() {
        let mut settings = Settings::default();
        let changed = settings
            .change(&json!({"events": {"call": false, "failed": false}}), None)
            .unwrap();
        assert_eq!(changed.call, Some(false));
        assert_eq!(settings.global(Event::Call), None);
        assert_eq!(settings.global(Event::Failed), Some(false));
        assert_eq!(settings.version, 1);
        let document = settings.document(false, []);
        let call = &document["events"][6];
        assert_eq!(call["id"], "call");
        assert_eq!(call["on"], false);
        // A kept `call` (written by hand) is not read: the rings own it.
        let read = Settings::stored(Some(r#"{"events":{"call":true,"x":true},"version":4}"#));
        assert!(read.events.is_empty());
        assert_eq!(read.version, 4);
    }

    #[test]
    fn null_lets_an_agent_follow_the_global_choice_again() {
        let mut settings = Settings::default();
        settings
            .change(
                &json!({"events": {"reply": false, "job_done": true}}),
                Some("a1"),
            )
            .unwrap();
        assert_eq!(settings.of_agent("a1", Event::Reply), Some(false));
        let document = settings.document(true, [("a1", "vera", false), ("a2", "ola", true)]);
        assert_eq!(
            document["agents"],
            json!({
                "a1": {"session": "vera", "muted": false, "events": {"job_done": true, "reply": false}},
                "a2": {"session": "ola", "muted": true, "events": {}},
            })
        );
        settings
            .change(
                &json!({"events": {"reply": null, "job_done": null}}),
                Some("a1"),
            )
            .unwrap();
        assert!(settings.agents.is_empty());
        assert_eq!(settings.version, 2);
        let kept = Settings::stored(Some(&settings.kept()));
        assert_eq!(kept, settings);
    }

    #[test]
    fn host_texts_have_english_once_each() {
        let answered = Loc::new("notify.body.answered", &["Vera"]);
        assert_eq!(answered.english(), "Vera answered");
        let mut keys: Vec<&str> = STRINGS.iter().map(|(key, _)| *key).collect();
        keys.sort_unstable();
        keys.dedup();
        assert_eq!(keys.len(), STRINGS.len());
    }

    #[test]
    fn a_preview_line_maps_to_the_chat_lists_keys_and_plain_text_to_none() {
        let key = |parts: Value| preview_loc(&parts).map(|loc| (loc.key, loc.args));
        let args = |list: &[&str]| list.iter().map(|arg| json!(arg)).collect::<Vec<Value>>();
        assert_eq!(key(json!({"kind": "text", "text": "hi"})), None);
        assert_eq!(
            key(json!({"kind": "image", "caption": ""})),
            Some(("preview.image".into(), Vec::<Value>::new()))
        );
        assert_eq!(
            key(json!({"kind": "image", "caption": "A cat"})),
            Some(("preview.image_caption".into(), args(&["A cat"])))
        );
        assert_eq!(
            key(json!({"kind": "files", "count": 3})),
            Some(("preview.files".into(), vec![json!(3)]))
        );
        assert_eq!(
            key(json!({"kind": "file", "name": "a.pdf"})),
            Some(("preview.file".into(), args(&["a.pdf"])))
        );
        assert_eq!(
            key(json!({"kind": "code"})),
            Some(("preview.code".into(), Vec::<Value>::new()))
        );
        assert_eq!(
            key(
                json!({"kind": "decision", "question": "Ship?", "outcome": "accepted", "answer": ""})
            ),
            Some(("preview.decision".into(), args(&["Ship?", "accepted"])))
        );
        assert_eq!(
            key(json!({"kind": "tool", "category": "exec", "name": "Bash", "label": "ls"})),
            Some(("preview.tool.exec".into(), args(&["ls"])))
        );
        assert_eq!(
            key(json!({"kind": "tool", "category": "a.b", "label": "x"})),
            None
        );
        assert_eq!(key(Value::Null), None);
        assert_eq!(key(json!({"kind": "hologram"})), None);
    }

    #[test]
    fn dedup_ids_are_stable_per_turn_and_per_job_state() {
        assert_eq!(failed_id("a1", 7), failed_id("a1", 7));
        assert_ne!(failed_id("a1", 7), failed_id("a1", 8));
        assert!(failed_id("a1", 7).starts_with("failed-"));
        assert_eq!(job_id("j1", 2, "failed"), "job-j1-2-failed");
    }
}
