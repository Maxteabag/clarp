//! Moving an agent to another backend, keeping its conversation (Rust Host
//! feature `agent_backend_transit`, contract 158; docs/protocol.md "Moving an
//! agent to another backend"). The Host moves the agent: same agent, same
//! chat, same `conversation_id`, and it writes the chat's one result row.
//! The desktop offers the choice, starts the move, shows its phase and adds
//! no row of its own. The words are the ones the iOS and Android apps use.

use chrono::{DateTime, TimeZone};
use serde_json::{Value, json};

use crate::json::{self, Object};

pub const FEATURE: &str = "agent_backend_transit";
pub const ROW_TITLE: &str = "Backend";
pub const TURN_BLOCKED: &str = "Stop the current turn first";
pub const DEFAULT_MARK: &str = "Default";
pub const CONFIRM_BODY: &str = "Keeps persona, goals and messages. Takes about a minute.";
pub const SWITCH_BODY: &str = "Keeps conversation.";
/// A Host that carries the tools too (feature `agent_backend_transit_tools`,
/// contract 161) says so in the move's confirmation.
pub const TOOLS_FEATURE: &str = "agent_backend_transit_tools";
pub const CONFIRM_BODY_TOOLS: &str = "Carries messages, tool results and attachments.";

/// What a move's confirmation says comes along, by what the Host carries.
pub fn confirm_body(carries_tools: bool) -> &'static str {
    if carries_tools { CONFIRM_BODY_TOOLS } else { CONFIRM_BODY }
}
pub const NO_ANSWER: &str = "Couldn't reach the Host. Try again.";
pub const OPTIONS_FAILED: &str = "Couldn't read the backends";

/// Whether a `/server-info` answer offers the feature.
pub fn offered_by(info: &Object) -> bool {
    has_feature(info, FEATURE)
}

/// Whether a `/server-info` answer says a move carries the tools too.
pub fn tools_offered_by(info: &Object) -> bool {
    has_feature(info, TOOLS_FEATURE)
}

fn has_feature(info: &Object, feature: &str) -> bool {
    info.get("capabilities")
        .and_then(|c| c.get("features"))
        .and_then(Value::as_array)
        .is_some_and(|features| features.iter().any(|f| f.as_str() == Some(feature)))
}

/// The row's value: "Claude · Opus 5.5" (the backend alone without a model).
pub fn value(backend: &str, model: &str) -> String {
    if model.is_empty() { backend.to_owned() } else { format!("{backend} · {model}") }
}

pub fn confirm_title(agent: &str, backend: &str, model: &str) -> String {
    format!("Move {agent} to {}?", value(backend, model))
}

pub fn switch_title(agent: &str, model: &str) -> String {
    format!("Switch {agent} to {model}?")
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Phase {
    Reading,
    Translating,
    Registering,
    Switching,
    Done,
    Failed,
}

impl Phase {
    pub fn parse(text: &str) -> Option<Self> {
        Some(match text {
            "reading" => Self::Reading,
            "translating" => Self::Translating,
            "registering" => Self::Registering,
            "switching" => Self::Switching,
            "done" => Self::Done,
            "failed" => Self::Failed,
            _ => return None,
        })
    }

    /// A running phase in plain words; empty for done and failed.
    pub fn words(self) -> &'static str {
        match self {
            Self::Reading => "reading the conversation",
            Self::Translating => "translating",
            Self::Registering => "starting the new session",
            Self::Switching => "switching",
            Self::Done | Self::Failed => "",
        }
    }

    pub fn ended(self) -> bool {
        matches!(self, Self::Done | Self::Failed)
    }
}

/// What the row says while a change runs: a move names its backend and
/// phase ("Moving to Codex… translating"); a switch of the model alone names
/// the model ("Switching to Sonnet…").
pub fn progress(backend: &str, model: &str, switch_only: bool, phase: Phase) -> String {
    if switch_only {
        return format!("Switching to {model}…");
    }
    let moving = format!("Moving to {backend}…");
    match phase.words() {
        "" => moving,
        words => format!("{moving} {words}"),
    }
}

// ---- what can be chosen (GET /agent-model-options?session=S) -------------------

#[derive(Debug, Clone, PartialEq)]
pub struct Model {
    pub id: String,
    pub label: String,
    pub default: bool,
}

#[derive(Debug, Clone, PartialEq)]
pub struct Backend {
    pub id: String,
    pub label: String,
    pub available: bool,
    /// Why it is dimmed, in plain words; empty when available.
    pub reason: String,
    pub models: Vec<Model>,
    sort_index: i64,
}

/// The tools of an `/agent-model-options` answer in the Host's order; a row
/// that cannot be read is left out, never the whole answer.
pub fn options<Tz: TimeZone>(answer: &Object, now: &DateTime<Tz>) -> Vec<Backend>
where
    Tz::Offset: std::fmt::Display,
{
    let providers = json::object(answer, "providers");
    let mut backends: Vec<Backend> = providers
        .iter()
        .filter_map(|(id, row)| {
            let row = row.as_object()?;
            if id.is_empty() || json::boolean(row, "hidden") {
                return None;
            }
            let label = crate::catalog::antigravity_label(id)
                .map(str::to_owned)
                .or_else(|| Some(json::string(row, "label")).filter(|l| !l.is_empty()))
                .unwrap_or_else(|| capitalised(id));
            let available = match row.get("available").and_then(Value::as_bool) {
                Some(explicit) => explicit,
                None if id == crate::catalog::ANTIGRAVITY_ACP => false,
                // A Host before the field: installed is available.
                None => row.get("installed").and_then(Value::as_bool) != Some(false),
            };
            let reason = if available {
                String::new()
            } else {
                unavailable_words(
                    &json::string(row, "unavailable_reason"),
                    row.get("until_ms").and_then(Value::as_i64),
                    &json::string(row, "unavailable_detail"),
                    now,
                )
            };
            let models = json::array(row, "models")
                .iter()
                .filter_map(Value::as_object)
                .filter_map(|model| {
                    let id = json::string(model, "id");
                    (!id.is_empty()).then(|| Model {
                        label: Some(json::string(model, "label")).filter(|l| !l.is_empty()).unwrap_or_else(|| id.clone()),
                        default: json::boolean(model, "default"),
                        id,
                    })
                })
                .collect();
            let sort_index = row.get("sort_index").and_then(Value::as_i64).unwrap_or(1000);
            Some(Backend { id: id.clone(), label, available, reason, models, sort_index })
        })
        .collect();
    backends.sort_by(|a, b| a.sort_index.cmp(&b.sort_index).then_with(|| a.id.cmp(&b.id)));
    backends
}

fn capitalised(text: &str) -> String {
    let mut chars = text.chars();
    chars.next().map_or_else(String::new, |first| first.to_uppercase().collect::<String>() + chars.as_str())
}

/// The Host's unavailable code in the words every app uses.
pub fn unavailable_words<Tz: TimeZone>(code: &str, until_ms: Option<i64>, detail: &str, now: &DateTime<Tz>) -> String
where
    Tz::Offset: std::fmt::Display,
{
    match code.trim().to_ascii_lowercase().as_str() {
        "not_installed" => "Not installed on this Host".into(),
        "signed_out" => "Not signed in".into(),
        "not_supported" => "Can't carry a conversation there yet".into(),
        "usage_limit" => match until_ms.and_then(|ms| now.timezone().timestamp_millis_opt(ms).single()) {
            Some(until) => format!("At its usage limit until {}", clock(&until, now)),
            None => "At its usage limit".into(),
        },
        "error" => {
            let line = detail.lines().next().unwrap_or_default().trim();
            let short: String = if line.chars().count() > 80 { line.chars().take(79).collect::<String>() + "…" } else { line.to_owned() };
            if short.is_empty() { "Couldn't check".into() } else { format!("Couldn't check: {short}") }
        }
        _ => "Not available".into(),
    }
}

/// "01:00", or "Tue 01:00" when it is not today.
fn clock<Tz: TimeZone>(at: &DateTime<Tz>, now: &DateTime<Tz>) -> String
where
    Tz::Offset: std::fmt::Display,
{
    if at.date_naive() == now.date_naive() { at.format("%H:%M").to_string() } else { at.format("%a %H:%M").to_string() }
}

/// A refused start (`POST /agent-transit`) in plain words. `unavailable` is
/// the target's own reason from the options, for `backend_unavailable`.
pub fn refusal(status: u16, code: &str, unavailable: &str) -> String {
    match code.trim().to_ascii_lowercase().as_str() {
        "turn_running" => TURN_BLOCKED.into(),
        "messages_waiting" => "Messages are waiting for this agent. Try again once they have gone.".into(),
        "transit_running" => "A move is already running".into(),
        "unknown_backend" => "This Host doesn't offer that backend".into(),
        "unknown_model" => "This Host doesn't offer that model".into(),
        "same_backend_and_model" => "It already runs on that backend and model".into(),
        "not_supported" => "Can't carry a conversation there yet".into(),
        "backend_unavailable" if !unavailable.is_empty() => unavailable.into(),
        "backend_unavailable" => "Not available".into(),
        _ if status == 0 => NO_ANSWER.into(),
        _ if status == 404 => "This Host cannot move agents".into(),
        _ => "Couldn't start the move".into(),
    }
}

/// The code of a refusal's text (`error_message` reads the body's `error`).
pub fn refusal_code(message: &str) -> String {
    let code = message.trim();
    if !code.is_empty() && code.bytes().all(|b| b.is_ascii_lowercase() || b == b'_') { code.to_owned() } else { String::new() }
}

/// The start's body: `effort` is left out, so the agent keeps its own where
/// the target takes it.
pub fn start_body(session: &str, backend: &str, model: &str, request_id: &str) -> Value {
    json!({"session": session, "backend": backend, "model": model, "request_id": request_id})
}

// ---- progress (SSE agent-transit, GET /agent-transit, the roster row) ---------

#[derive(Debug, Clone, PartialEq)]
pub struct Event {
    pub session: String,
    pub agent_id: String,
    pub transit_id: String,
    pub backend: String,
    pub model: String,
    pub phase: Phase,
    pub error: String,
}

impl Event {
    /// From the event's, the GET answer's or the roster row's `transit`
    /// fields; None without a transit id or a known phase.
    pub fn parse(fields: &Object) -> Option<Self> {
        let transit_id = json::string(fields, "transit_id");
        let phase = Phase::parse(&json::string(fields, "phase"))?;
        if transit_id.is_empty() {
            return None;
        }
        let error = match fields.get("error") {
            Some(Value::String(text)) => text.trim().to_owned(),
            Some(Value::Object(error)) => ["message", "reason", "code"].iter().map(|k| json::string(error, k)).find(|t| !t.is_empty()).unwrap_or_default(),
            _ => String::new(),
        };
        Some(Self {
            session: json::string(fields, "session"),
            agent_id: json::string(fields, "agent_id"),
            transit_id,
            backend: json::string(fields, "backend"),
            model: json::string(fields, "model"),
            phase,
            error,
        })
    }
}

// ---- the flow ---------------------------------------------------------------

/// The profile row's flow: idle → picking a backend → its model →
/// confirming → moving (with its phase) → done or failed.
#[derive(Debug, Clone, Default, PartialEq)]
pub enum Step {
    #[default]
    Idle,
    Picking,
    Model { backend: String },
    Confirming { backend: String, model: String },
    Moving { transit_id: String, backend: String, phase: Phase },
    Done { backend: String, model: String },
    Failed { backend: String, reason: String },
}

#[derive(Debug, Clone, Default, PartialEq)]
pub struct Flow {
    pub step: Step,
    /// Kept across retries of one start: the Host answers the same transit.
    pub request_id: String,
    /// The model the running change goes to.
    pub target_model: String,
    /// The last transit that ended here.
    pub ended_transit: String,
}

impl Flow {
    pub fn moving(&self) -> bool {
        matches!(self.step, Step::Moving { .. })
    }

    /// Opens the picker; never while a move runs or the agent is busy.
    pub fn open(&mut self, blocked: bool) -> bool {
        if self.moving() || blocked {
            return false;
        }
        self.step = Step::Picking;
        true
    }

    pub fn pick_backend(&mut self, backend: &Backend) {
        if backend.available && matches!(self.step, Step::Picking | Step::Model { .. } | Step::Confirming { .. }) {
            self.step = Step::Model { backend: backend.id.clone() };
        }
    }

    pub fn pick_model(&mut self, model: &str) {
        if let Step::Model { backend } = &self.step {
            self.step = Step::Confirming { backend: backend.clone(), model: model.to_owned() };
        }
    }

    /// Escape: one level back.
    pub fn back(&mut self) {
        self.step = match std::mem::take(&mut self.step) {
            Step::Confirming { backend, .. } => Step::Model { backend },
            Step::Model { .. } | Step::Picking => Step::Idle,
            other => other,
        };
    }

    /// Move: the start goes out with a request id a retry keeps.
    pub fn confirm(&mut self, new_request_id: impl FnOnce() -> String) -> Option<(String, String, String)> {
        let Step::Confirming { backend, model } = self.step.clone() else { return None };
        if self.request_id.is_empty() {
            self.request_id = new_request_id();
        }
        self.target_model = model.clone();
        self.step = Step::Moving { transit_id: String::new(), backend: backend.clone(), phase: Phase::Reading };
        Some((backend, model, self.request_id.clone()))
    }

    /// The Host took it: its transit id (an event may have come first).
    pub fn started(&mut self, id: &str) {
        if let Step::Moving { transit_id, .. } = &mut self.step
            && transit_id.is_empty()
        {
            *transit_id = id.to_owned();
        }
    }

    /// The Host refused the start: nothing changed on it.
    pub fn refused(&mut self, backend: &str, reason: &str) {
        self.request_id.clear();
        self.step = Step::Failed { backend: backend.to_owned(), reason: reason.to_owned() };
    }

    /// The held transit's id, while one is known.
    pub fn transit_id(&self) -> Option<&str> {
        match &self.step {
            Step::Moving { transit_id, .. } if !transit_id.is_empty() => Some(transit_id),
            _ => None,
        }
    }

    /// The roster row names a running move: adopt one this app did not
    /// start (or lost); a held one takes it as its event.
    pub fn adopt(&mut self, event: &Event) {
        if event.phase.ended() {
            return;
        }
        if let Step::Moving { transit_id, .. } = &self.step
            && !transit_id.is_empty()
            && *transit_id != event.transit_id
        {
            return;
        }
        self.apply(event);
    }

    /// A progress event of the transit this flow holds (or any, when it holds
    /// none yet). Events may come all within a second: the last one wins.
    pub fn apply(&mut self, event: &Event) {
        if let Step::Moving { transit_id, .. } = &self.step
            && !transit_id.is_empty()
            && *transit_id != event.transit_id
        {
            return;
        }
        // A transit that ended takes no later word of itself (a snapshot
        // read before it ended).
        if event.transit_id == self.ended_transit {
            return;
        }
        if !event.model.is_empty() {
            self.target_model = event.model.clone();
        }
        self.step = match event.phase {
            Phase::Done => {
                self.request_id.clear();
                self.ended_transit = event.transit_id.clone();
                Step::Done { backend: event.backend.clone(), model: event.model.clone() }
            }
            Phase::Failed => {
                self.request_id.clear();
                self.ended_transit = event.transit_id.clone();
                let reason = if event.error.is_empty() { "the move failed".to_owned() } else { event.error.clone() };
                Step::Failed { backend: event.backend.clone(), reason }
            }
            phase => Step::Moving { transit_id: event.transit_id.clone(), backend: event.backend.clone(), phase },
        };
    }

    /// The Host forgot the transit (a restart): nothing runs any more.
    pub fn forgotten(&mut self) {
        if self.moving() {
            self.request_id.clear();
            self.step = Step::Idle;
        }
    }

    /// Closes the picker or clears a finished result.
    pub fn reset(&mut self) {
        if !self.moving() {
            self.step = Step::Idle;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::Utc;

    fn object(value: Value) -> Object {
        value.as_object().cloned().unwrap()
    }

    #[test]
    fn options_dim_what_cannot_be_chosen_in_plain_words() {
        let now = Utc.with_ymd_and_hms(2026, 11, 23, 8, 0, 0).unwrap();
        let until = Utc.with_ymd_and_hms(2026, 11, 23, 9, 30, 0).unwrap().timestamp_millis();
        let answer = object(json!({"providers": {
            "codex": {"label": "Codex", "sort_index": 2, "available": true, "models": [
                {"id": "gpt-5.5", "label": "GPT-5.5", "default": true}, {"id": "", "label": "skipped"}]},
            "claude": {"label": "Claude", "sort_index": 1, "available": true, "models": [{"id": "opus"}]},
            "grok": {"sort_index": 3, "available": false, "unavailable_reason": "usage_limit", "until_ms": until},
            "muse": {"label": "Muse", "sort_index": 4, "available": false, "unavailable_reason": "not_supported"},
            "agy": {"label": "Antigravity", "sort_index": 5, "available": false, "unavailable_reason": "signed_out"},
            "hidden": {"hidden": true}}}));
        let backends = options(&answer, &now);
        let ids: Vec<&str> = backends.iter().map(|b| b.id.as_str()).collect();
        assert_eq!(ids, ["claude", "codex", "grok", "muse", "agy"]);
        assert_eq!(backends[0].models[0].label, "opus");
        assert_eq!(backends[1].models.len(), 1);
        assert!(backends[1].models[0].default);
        assert_eq!(backends[2].label, "Grok");
        assert_eq!(backends[2].reason, "At its usage limit until 09:30");
        assert_eq!(backends[3].reason, "Can't carry a conversation there yet");
        assert_eq!(backends[4].reason, "Not signed in");
        assert!(backends[0].reason.is_empty());
    }

    #[test]
    fn refusals_read_as_people_say_them() {
        assert_eq!(refusal(409, "turn_running", ""), TURN_BLOCKED);
        assert_eq!(refusal(400, "backend_unavailable", "Not signed in"), "Not signed in");
        assert_eq!(refusal(400, "same_backend_and_model", ""), "It already runs on that backend and model");
        assert_eq!(refusal(0, "", ""), NO_ANSWER);
        assert_eq!(refusal_code("transit_running"), "transit_running");
        assert_eq!(refusal_code("HTTP 500 Internal Server Error"), "");
    }

    fn event(id: &str, phase: &str) -> Event {
        Event::parse(&object(json!({"session": "s", "transit_id": id, "backend": "codex", "model": "gpt-5.5", "phase": phase, "error": null}))).unwrap()
    }

    #[test]
    fn a_move_lands_on_its_final_state() {
        let mut flow = Flow::default();
        assert!(!flow.open(true));
        assert!(flow.open(false));
        let codex = Backend { id: "codex".into(), label: "Codex".into(), available: true, reason: String::new(), models: vec![], sort_index: 0 };
        flow.pick_backend(&codex);
        flow.pick_model("gpt-5.5");
        let (backend, model, request) = flow.confirm(|| "r1".into()).unwrap();
        assert_eq!((backend.as_str(), model.as_str(), request.as_str()), ("codex", "gpt-5.5", "r1"));
        // Every phase may land before the 202.
        flow.apply(&event("t1", "reading"));
        flow.apply(&event("t1", "switching"));
        flow.apply(&event("t1", "done"));
        flow.started("t1");
        assert_eq!(flow.step, Step::Done { backend: "codex".into(), model: "gpt-5.5".into() });
        // Another transit's event does not move a held one.
        let mut held = Flow { step: Step::Moving { transit_id: "t2".into(), backend: "codex".into(), phase: Phase::Reading }, ..Flow::default() };
        held.apply(&event("t9", "done"));
        assert!(held.moving());
        assert!(!held.open(false));
        held.apply(&event("t2", "failed"));
        assert_eq!(held.step, Step::Failed { backend: "codex".into(), reason: "the move failed".into() });
    }

    #[test]
    fn escape_steps_back_and_a_refusal_keeps_nothing() {
        let mut flow = Flow::default();
        flow.open(false);
        let claude = Backend { id: "claude".into(), label: "Claude".into(), available: true, reason: String::new(), models: vec![], sort_index: 0 };
        let off = Backend { available: false, id: "agy".into(), ..claude.clone() };
        flow.pick_backend(&off);
        assert_eq!(flow.step, Step::Picking);
        flow.pick_backend(&claude);
        flow.pick_model("sonnet");
        flow.back();
        assert_eq!(flow.step, Step::Model { backend: "claude".into() });
        flow.back();
        flow.back();
        assert_eq!(flow.step, Step::Idle);
        flow.open(false);
        flow.pick_backend(&claude);
        flow.pick_model("sonnet");
        flow.confirm(|| "r1".into());
        flow.refused("claude", "A move is already running");
        assert!(flow.request_id.is_empty());
        assert_eq!(progress("Codex", "", false, Phase::Translating), "Moving to Codex… translating");
        assert_eq!(progress("Claude", "Sonnet", true, Phase::Switching), "Switching to Sonnet…");
    }
}

#[cfg(test)]
mod tools_tests {
    use super::*;
    use serde_json::json;

    /// A Host that carries the tools says so in the move's confirmation; an
    /// older one keeps the older text.
    #[test]
    fn the_confirmation_says_what_the_host_carries() {
        let both = json!({"capabilities": {"features": [FEATURE, TOOLS_FEATURE]}});
        let older = json!({"capabilities": {"features": [FEATURE]}});
        assert!(tools_offered_by(both.as_object().unwrap()));
        assert!(!tools_offered_by(older.as_object().unwrap()));
        assert!(offered_by(older.as_object().unwrap()));
        assert!(confirm_body(true).contains("tool results"));
        assert_eq!(confirm_body(false), CONFIRM_BODY);
    }
}
