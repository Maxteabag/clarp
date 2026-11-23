//! Changing an agent's backend from its profile (Maxteabag/clarp#720; Host
//! feature `agent_backend_transit`, Rust Host contract 158). The Host moves
//! the agent (same agent, chat and conversation id) and writes the one result
//! row in the chat; the app offers the choice, starts the move and shows its
//! phases. The words are the ones every app shares (clarp-ios#140,
//! `BackendTransit.swift`).

use std::fmt::Display;

use chrono::{DateTime, TimeZone, Utc};
use serde_json::Value;

use crate::json::{self, Object};

/// The Host feature in `/server-info` that offers it.
pub const FEATURE: &str = "agent_backend_transit";

pub const ROW_TITLE: &str = "Backend";
pub const TURN_BLOCKED: &str = "Finish or stop the current turn first";
pub const MESSAGES_WAITING: &str = "Messages are waiting for this agent. Try again once they have gone.";
pub const DEFAULT_MARK: &str = "Default";
/// Only the conversation's messages are carried (addendum 2).
pub const CONFIRM_BODY: &str =
    "It keeps its persona and goals and takes the conversation's messages along (not tool output, thinking or attachments). This takes about a minute.";
/// Same backend, another model: no move, a switch.
pub const SWITCH_BODY: &str = "Its conversation stays as it is.";
/// The start got no answer from the Host.
pub const NO_ANSWER: &str = "Couldn't reach the Host. Try again.";

/// The row's value: "Claude · Opus 5.5".
pub fn value(backend: &str, model: &str) -> String {
    if model.is_empty() { backend.to_owned() } else { format!("{backend} · {model}") }
}

pub fn confirm_title(agent: &str, backend: &str, model: &str) -> String {
    format!("Move {agent} to {}?", value(backend, model))
}

pub fn switch_title(agent: &str, model: &str) -> String {
    format!("Switch {agent} to {model}?")
}

/// The row while a move runs: "Moving to Codex… translating".
pub fn moving_line(backend: &str, phase: Phase) -> String {
    let words = phase.words();
    if words.is_empty() { format!("Moving to {backend}…") } else { format!("Moving to {backend}… {words}") }
}

/// A model-only switch names the model alone.
pub fn switching_line(model: &str) -> String {
    format!("Switching to {model}…")
}

/// The result once done, in the Host's words (addendum 3).
pub fn done_line(backend: &str, model: &str, switch_only: bool, omitted: i64) -> String {
    if switch_only {
        return format!("Now on {model}.");
    }
    match omitted {
        0 => format!("Now on {}. The conversation came along.", value(backend, model)),
        n => format!("Now on {}. The conversation came along, except its oldest {n} turns.", value(backend, model)),
    }
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

    /// The plain words of a running phase; empty for done and failed.
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

// ---- options (GET /agent-model-options?session=S) ------------------------------

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Model {
    pub id: String,
    pub label: String,
    pub default: bool,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Backend {
    pub id: String,
    pub label: String,
    pub available: bool,
    /// Why it is dimmed, in words; None when available.
    pub reason: Option<String>,
    pub models: Vec<Model>,
}

/// The Host's offer for one agent in its order (`sort_index`, then id). A
/// provider that cannot be read is left out, never the whole answer.
pub fn options<Tz: TimeZone>(catalog: &Object, now: &DateTime<Tz>) -> Vec<Backend>
where
    Tz::Offset: Display,
{
    let mut rows: Vec<(i64, Backend)> = json::object(catalog, "providers")
        .iter()
        .filter_map(|(id, row)| {
            let row = row.as_object()?;
            if id.is_empty() || json::boolean(row, "hidden") {
                return None;
            }
            let label = row.get("label").and_then(Value::as_str).filter(|l| !l.is_empty()).map_or_else(|| capitalized(id), str::to_owned);
            let available = match row.get("available").and_then(Value::as_bool) {
                Some(said) => said,
                // A Host before the field: installed is available.
                None => row.get("installed").and_then(Value::as_bool).unwrap_or(true),
            };
            let reason = (!available).then(|| {
                unavailable_words(
                    row.get("unavailable_reason").and_then(Value::as_str),
                    row.get("until_ms").and_then(Value::as_i64),
                    row.get("unavailable_detail").and_then(Value::as_str),
                    now,
                )
            });
            let models = json::array(row, "models")
                .iter()
                .filter_map(Value::as_object)
                .filter_map(|model| {
                    let id = json::string(model, "id");
                    if id.is_empty() {
                        return None;
                    }
                    let label = model.get("label").and_then(Value::as_str).filter(|l| !l.is_empty()).unwrap_or(&id).to_owned();
                    Some(Model { label, default: json::boolean(model, "default"), id })
                })
                .collect();
            let index = row.get("sort_index").and_then(Value::as_i64).unwrap_or(1000);
            Some((index, Backend { id: id.clone(), label, available, reason, models }))
        })
        .collect();
    rows.sort_by(|a, b| a.0.cmp(&b.0).then_with(|| a.1.id.cmp(&b.1.id)));
    rows.into_iter().map(|(_, backend)| backend).collect()
}

fn capitalized(text: &str) -> String {
    let mut chars = text.chars();
    chars.next().map_or_else(String::new, |first| first.to_uppercase().chain(chars).collect())
}

/// The Host's unavailable code in the shared words.
pub fn unavailable_words<Tz: TimeZone>(code: Option<&str>, until_ms: Option<i64>, detail: Option<&str>, now: &DateTime<Tz>) -> String
where
    Tz::Offset: Display,
{
    match code.map(|c| c.trim().to_ascii_lowercase()).as_deref() {
        Some("not_installed") => "Not installed on this Host".into(),
        Some("signed_out") => "Not signed in".into(),
        Some("not_supported") => "Can't carry a conversation there yet".into(),
        Some("usage_limit") => match until_ms {
            Some(ms) => format!("At its usage limit until {}", clock(ms, now)),
            None => "At its usage limit".into(),
        },
        Some("error") => {
            let line = detail.and_then(|d| d.lines().next()).unwrap_or_default().trim();
            if line.is_empty() {
                "Couldn't check".into()
            } else if line.chars().count() > 80 {
                format!("Couldn't check: {}…", line.chars().take(79).collect::<String>())
            } else {
                format!("Couldn't check: {line}")
            }
        }
        _ => "Not available".into(),
    }
}

/// "01:00", or "Tue 01:00" when it is not today (in `now`'s zone).
pub fn clock<Tz: TimeZone>(until_ms: i64, now: &DateTime<Tz>) -> String
where
    Tz::Offset: Display,
{
    let Some(until) = Utc.timestamp_millis_opt(until_ms).single() else { return String::new() };
    let until = until.with_timezone(&now.timezone());
    if until.date_naive() == now.date_naive() { until.format("%H:%M").to_string() } else { until.format("%a %H:%M").to_string() }
}

// ---- refusals (POST /agent-transit) --------------------------------------------

/// A refused start in plain words. `unavailable` is the picker's words for
/// the target (a `backend_unavailable` refusal's reason as the app holds it).
pub fn refusal(status: u16, code: &str, unavailable: Option<&str>) -> String {
    match code.trim().to_ascii_lowercase().as_str() {
        "turn_running" => TURN_BLOCKED.into(),
        "messages_waiting" => MESSAGES_WAITING.into(),
        "transit_running" => "A move is already running".into(),
        "unknown_backend" => "This Host doesn't offer that backend".into(),
        "unknown_model" => "This Host doesn't offer that model".into(),
        "same_backend_and_model" => "It already runs on that backend and model".into(),
        "not_supported" => "Can't carry a conversation there yet".into(),
        "backend_unavailable" => unavailable.unwrap_or("Not available").into(),
        _ if status == 0 => NO_ANSWER.into(),
        _ if status == 404 => "This Host cannot move agents".into(),
        _ => "Couldn't start the move".into(),
    }
}

// ---- progress (SSE agent-transit, GET /agent-transit, the roster row) -----------

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Event {
    pub session: String,
    pub transit_id: String,
    pub backend: String,
    pub model: String,
    pub phase: Phase,
    pub error: Option<String>,
    /// On done: the oldest turns the size budget left out.
    pub omitted: i64,
}

impl Event {
    /// From the event's, the GET answer's or the roster row's `transit`
    /// fields; None without a transit id or a known phase.
    pub fn from_json(object: &Object) -> Option<Self> {
        let transit_id = json::string(object, "transit_id");
        let phase = Phase::parse(&json::string(object, "phase"))?;
        if transit_id.is_empty() {
            return None;
        }
        let error = match object.get("error") {
            Some(Value::String(text)) => Some(text.clone()),
            Some(Value::Object(error)) => ["message", "reason", "code"].iter().find_map(|k| error.get(*k).and_then(Value::as_str)).map(str::to_owned),
            _ => None,
        }
        .map(|e| e.trim().to_owned())
        .filter(|e| !e.is_empty());
        Some(Self {
            session: json::string(object, "session"),
            transit_id,
            backend: json::string(object, "backend"),
            model: json::string(object, "model"),
            phase,
            error,
            omitted: json::integer(&json::object(object, "carried"), "omitted"),
        })
    }
}

// ---- the flow ------------------------------------------------------------------

/// The profile row's flow: idle → picking a backend → its model →
/// confirming → moving (with its phase) → done or failed.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub enum Step {
    #[default]
    Idle,
    Picking,
    Model { backend: String },
    Confirming { backend: String, model: String },
    Moving { transit_id: Option<String>, backend: String, phase: Phase },
    Done { backend: String, model: String, omitted: i64 },
    Failed { backend: String, reason: String },
}

#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct Flow {
    pub step: Step,
    /// Kept across retries of one start: the Host answers the same transit.
    pub request_id: Option<String>,
    /// The model the running change goes to.
    pub target_model: String,
    /// The backend the agent ran on when the change started (a model-only
    /// switch keeps it); empty when unknown.
    pub from_backend: String,
}

impl Flow {
    pub fn moving(&self) -> bool {
        matches!(self.step, Step::Moving { .. })
    }

    /// The picker shows (any of its steps).
    pub fn picking(&self) -> bool {
        matches!(self.step, Step::Picking | Step::Model { .. } | Step::Confirming { .. })
    }

    /// Opens the picker; never while a move runs or the agent is busy.
    pub fn open(&mut self, blocked: bool) -> bool {
        if self.moving() || blocked {
            return false;
        }
        self.step = Step::Picking;
        true
    }

    /// An available backend: on to its models.
    pub fn pick_backend(&mut self, backend: &Backend) {
        if backend.available && self.picking() {
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
            Step::Model { .. } => Step::Picking,
            Step::Picking => Step::Idle,
            other => other,
        };
    }

    /// Move: the start goes with a request id a retry keeps.
    pub fn confirm(&mut self, new_request_id: impl FnOnce() -> String) -> Option<(String, String, String)> {
        let Step::Confirming { backend, model } = &self.step else { return None };
        let (backend, model) = (backend.clone(), model.clone());
        let id = self.request_id.get_or_insert_with(new_request_id).clone();
        self.target_model = model.clone();
        self.step = Step::Moving { transit_id: None, backend: backend.clone(), phase: Phase::Reading };
        Some((backend, model, id))
    }

    /// The Host took it: its transit id.
    pub fn started(&mut self, id: &str) {
        if let Step::Moving { transit_id, .. } = &mut self.step {
            *transit_id = Some(id.to_owned());
        }
    }

    /// The Host refused the start: the agent is unchanged.
    pub fn refused(&mut self, reason: &str) {
        let Step::Moving { backend, .. } = &self.step else { return };
        let backend = backend.clone();
        self.request_id = None;
        self.step = Step::Failed { backend, reason: reason.to_owned() };
    }

    /// The roster row's running move after a (re)connect: one this app did
    /// not see start is adopted.
    pub fn adopt(&mut self, event: &Event) {
        if event.phase.ended() {
            return;
        }
        if let Step::Moving { transit_id: held, .. } = &self.step
            && held.as_deref().is_some_and(|held| held != event.transit_id)
        {
            return;
        }
        self.apply(event);
    }

    /// A progress event of the transit this flow holds (or any while it
    /// holds none yet). Ended events of another transit are ignored.
    pub fn apply(&mut self, event: &Event) {
        match &self.step {
            Step::Moving { transit_id: Some(held), .. } if *held != event.transit_id => return,
            Step::Moving { .. } => {}
            // Not moving here: only a running move is taken up.
            _ if event.phase.ended() => return,
            _ => {}
        }
        if !event.model.is_empty() {
            self.target_model = event.model.clone();
        }
        self.step = match event.phase {
            Phase::Done => {
                self.request_id = None;
                Step::Done { backend: event.backend.clone(), model: event.model.clone(), omitted: event.omitted }
            }
            Phase::Failed => {
                self.request_id = None;
                Step::Failed { backend: event.backend.clone(), reason: event.error.clone().unwrap_or_else(|| "the move failed".into()) }
            }
            phase => Step::Moving { transit_id: Some(event.transit_id.clone()), backend: event.backend.clone(), phase },
        };
    }

    /// Closes the picker or clears a finished result; never a running move.
    pub fn reset(&mut self) {
        if !self.moving() {
            self.step = Step::Idle;
        }
    }

    /// The transit id of the running move, once known.
    pub fn transit_id(&self) -> Option<&str> {
        match &self.step {
            Step::Moving { transit_id, .. } => transit_id.as_deref(),
            _ => None,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::FixedOffset;
    use serde_json::json;

    fn object(value: Value) -> Object {
        value.as_object().cloned().unwrap()
    }

    fn now() -> DateTime<FixedOffset> {
        // Mon 23 Nov 2026 12:00 +01:00
        FixedOffset::east_opt(3600).unwrap().with_ymd_and_hms(2026, 11, 23, 12, 0, 0).unwrap()
    }

    fn ms(day: u32, hour: u32) -> i64 {
        FixedOffset::east_opt(3600).unwrap().with_ymd_and_hms(2026, 11, day, hour, 0, 0).unwrap().timestamp_millis()
    }

    #[test]
    fn options_word_each_code_in_the_host_order() {
        let catalog = object(json!({"providers": {
            "codex": {"label": "Codex", "sort_index": 2, "available": false, "unavailable_reason": "usage_limit", "until_ms": ms(23, 18), "models": []},
            "claude": {"label": "Claude", "sort_index": 1, "available": true,
                "models": [{"id": "opus", "label": "Opus 5.5", "default": true}, {"id": "sonnet"}, {"label": "no id"}]},
            "grok": {"sort_index": 3, "available": false, "unavailable_reason": "signed_out"},
            "agy": {"sort_index": 3, "available": false, "unavailable_reason": "usage_limit", "until_ms": ms(24, 1)},
            "opencode": {"sort_index": 4, "available": false, "unavailable_reason": "error", "unavailable_detail": "probe timed out\nsecond line"},
            "muse": {"sort_index": 5, "available": false, "unavailable_reason": "not_supported"},
            "gone": {"sort_index": 6, "available": false, "unavailable_reason": "not_installed"},
            "odd": {"sort_index": 7, "available": false, "unavailable_reason": "something_new"},
            "secret": {"hidden": true},
            "bad": 3,
        }}));
        let backends = options(&catalog, &now());
        let ids: Vec<&str> = backends.iter().map(|b| b.id.as_str()).collect();
        assert_eq!(ids, ["claude", "codex", "agy", "grok", "opencode", "muse", "gone", "odd"]);
        let reasons: Vec<Option<&str>> = backends.iter().map(|b| b.reason.as_deref()).collect();
        assert_eq!(
            reasons,
            [
                None,
                Some("At its usage limit until 18:00"),
                Some("At its usage limit until Tue 01:00"),
                Some("Not signed in"),
                Some("Couldn't check: probe timed out"),
                Some("Can't carry a conversation there yet"),
                Some("Not installed on this Host"),
                Some("Not available"),
            ]
        );
        assert_eq!(backends[3].label, "Grok");
        assert_eq!(
            backends[0].models,
            [
                Model { id: "opus".into(), label: "Opus 5.5".into(), default: true },
                Model { id: "sonnet".into(), label: "sonnet".into(), default: false }
            ]
        );
    }

    #[test]
    fn an_older_catalog_without_availability_counts_installed_as_available() {
        let catalog = object(json!({"providers": {"claude": {"label": "Claude"}, "codex": {"installed": false}}}));
        let backends = options(&catalog, &now());
        assert!(backends.iter().find(|b| b.id == "claude").unwrap().available);
        assert_eq!(backends.iter().find(|b| b.id == "codex").unwrap().reason.as_deref(), Some("Not available"));
    }

    #[test]
    fn a_long_error_detail_is_cut_to_one_short_line() {
        let detail = "x".repeat(120);
        let words = unavailable_words(Some("error"), None, Some(&detail), &now());
        assert_eq!(words.chars().count(), "Couldn't check: ".len() + 80);
        assert!(words.ends_with('…'));
        assert_eq!(unavailable_words(Some("error"), None, None, &now()), "Couldn't check");
        assert_eq!(unavailable_words(Some("usage_limit"), None, None, &now()), "At its usage limit");
    }

    #[test]
    fn refusals_are_worded() {
        assert_eq!(refusal(409, "turn_running", None), TURN_BLOCKED);
        assert_eq!(refusal(409, "messages_waiting", None), MESSAGES_WAITING);
        assert_eq!(refusal(409, "transit_running", None), "A move is already running");
        assert_eq!(refusal(400, "backend_unavailable", Some("Not signed in")), "Not signed in");
        assert_eq!(refusal(400, "backend_unavailable", None), "Not available");
        assert_eq!(refusal(400, "unknown_model", None), "This Host doesn't offer that model");
        assert_eq!(refusal(404, "Not Found", None), "This Host cannot move agents");
        assert_eq!(refusal(0, "connection refused", None), NO_ANSWER);
        assert_eq!(refusal(500, "boom", None), "Couldn't start the move");
    }

    #[test]
    fn lines_match_the_other_apps() {
        assert_eq!(value("Claude", "Opus 5.5"), "Claude · Opus 5.5");
        assert_eq!(value("Claude", ""), "Claude");
        assert_eq!(moving_line("Codex", Phase::Translating), "Moving to Codex… translating");
        assert_eq!(moving_line("Codex", Phase::Done), "Moving to Codex…");
        assert_eq!(switching_line("GPT-6"), "Switching to GPT-6…");
        assert_eq!(confirm_title("Rachel", "Codex", "GPT-6"), "Move Rachel to Codex · GPT-6?");
        assert_eq!(switch_title("Rachel", "Sonnet"), "Switch Rachel to Sonnet?");
        assert_eq!(done_line("Codex", "GPT-6", false, 0), "Now on Codex · GPT-6. The conversation came along.");
        assert_eq!(done_line("Codex", "GPT-6", false, 3), "Now on Codex · GPT-6. The conversation came along, except its oldest 3 turns.");
        assert_eq!(done_line("Claude", "Sonnet", true, 0), "Now on Sonnet.");
    }

    #[test]
    fn events_read_the_wire_fields() {
        let done = Event::from_json(&object(json!({"type": "agent-transit", "agent_id": "a1", "session": "rachel", "transit_id": "t1",
            "backend": "codex", "model": "gpt-6", "phase": "done", "error": null,
            "carried": {"turns": 4, "user": 2, "assistant": 2, "omitted": 1}}))).unwrap();
        assert_eq!((done.phase, done.omitted, done.error), (Phase::Done, 1, None));
        let failed = Event::from_json(&object(json!({"transit_id": "t1", "phase": "failed", "error": {"message": "import failed"}}))).unwrap();
        assert_eq!(failed.error.as_deref(), Some("import failed"));
        assert!(Event::from_json(&object(json!({"transit_id": "t1", "phase": "flying"}))).is_none());
        assert!(Event::from_json(&object(json!({"phase": "reading"}))).is_none());
    }

    fn event(id: &str, phase: Phase) -> Event {
        Event { session: "rachel".into(), transit_id: id.into(), backend: "codex".into(), model: "gpt-6".into(), phase, error: None, omitted: 0 }
    }

    fn backend(id: &str, available: bool) -> Backend {
        Backend { id: id.into(), label: id.into(), available, reason: None, models: vec![] }
    }

    #[test]
    fn the_flow_walks_pick_confirm_move_done() {
        let mut flow = Flow::default();
        assert!(!flow.open(true), "not while the agent is busy");
        assert!(flow.open(false));
        flow.pick_backend(&backend("grok", false));
        assert_eq!(flow.step, Step::Picking, "an unavailable backend cannot be picked");
        flow.pick_backend(&backend("codex", true));
        flow.pick_model("gpt-6");
        assert_eq!(flow.step, Step::Confirming { backend: "codex".into(), model: "gpt-6".into() });
        flow.back();
        assert_eq!(flow.step, Step::Model { backend: "codex".into() });
        flow.pick_model("gpt-6");
        let start = flow.confirm(|| "r1".into()).unwrap();
        assert_eq!(start, ("codex".into(), "gpt-6".into(), "r1".into()));
        assert!(!flow.open(false), "not while it moves");
        flow.started("t1");
        flow.apply(&event("t2", Phase::Translating));
        assert_eq!(flow.transit_id(), Some("t1"), "another transit's events are ignored");
        flow.apply(&event("t1", Phase::Translating));
        assert_eq!(flow.step, Step::Moving { transit_id: Some("t1".into()), backend: "codex".into(), phase: Phase::Translating });
        flow.reset();
        assert!(flow.moving(), "a running move is never cleared");
        flow.apply(&Event { omitted: 2, ..event("t1", Phase::Done) });
        assert_eq!(flow.step, Step::Done { backend: "codex".into(), model: "gpt-6".into(), omitted: 2 });
        assert_eq!(flow.request_id, None);
    }

    #[test]
    fn a_refused_start_fails_and_a_retry_keeps_its_request_id() {
        let mut flow = Flow::default();
        flow.open(false);
        flow.pick_backend(&backend("codex", true));
        flow.pick_model("gpt-6");
        let first = flow.confirm(|| "r1".into()).unwrap();
        flow.refused("Finish or stop the current turn first");
        assert_eq!(flow.step, Step::Failed { backend: "codex".into(), reason: TURN_BLOCKED.into() });
        assert_eq!(first.2, "r1");
        let mut retry = Flow { step: Step::Confirming { backend: "codex".into(), model: "gpt-6".into() }, request_id: Some("r1".into()), ..Flow::default() };
        assert_eq!(retry.confirm(|| "r2".into()).unwrap().2, "r1");
    }

    #[test]
    fn failure_carries_the_hosts_reason() {
        let mut flow = Flow { step: Step::Moving { transit_id: Some("t1".into()), backend: "codex".into(), phase: Phase::Reading }, ..Flow::default() };
        flow.apply(&Event { error: Some("import failed".into()), ..event("t1", Phase::Failed) });
        assert_eq!(flow.step, Step::Failed { backend: "codex".into(), reason: "import failed".into() });
    }

    #[test]
    fn a_reconnect_adopts_a_running_move_but_not_an_ended_one() {
        let mut flow = Flow::default();
        flow.adopt(&event("t9", Phase::Done));
        assert_eq!(flow.step, Step::Idle);
        flow.adopt(&event("t9", Phase::Registering));
        assert_eq!(flow.transit_id(), Some("t9"));
        flow.adopt(&event("t8", Phase::Switching));
        assert_eq!(flow.transit_id(), Some("t9"), "the held move keeps its own");
        // An ended event of a move this app never saw changes nothing.
        let mut idle = Flow::default();
        idle.apply(&event("t7", Phase::Failed));
        assert_eq!(idle.step, Step::Idle);
    }
}
