//! Why a turn failed, in one plain sentence with the buttons that fit
//! (docs/turn-errors.md, Host contract 217, feature `turn_error_explained`).
//! The Host attaches an `error` object to the `/log` row that says why a
//! turn failed (`origin` `system`, also the `turn-interrupted` notice) and to
//! the live failed turn (`turn.error`). The desktop draws it as one card: the
//! sentence in the device's language, the actions, and the provider's raw
//! words behind a closed Details. Repeats of one failure with no reply of the
//! agent's between them draw as one card with a count. A row without the
//! object (an older Host) is drawn as before. Pure: the window draws the plan.

use std::collections::{HashMap, HashSet};

use serde_json::{Value, json};

use crate::json::{self, Object};
use crate::protocol::Message;

pub const FEATURE: &str = "turn_error_explained";
/// `POST /turn-retry` (same contract).
pub const RETRY_FEATURE: &str = "turn_retry";
pub const RETRY_PATH: &str = "/turn-retry";
/// The key a live failed turn's card is drawn and kept by.
pub const LIVE_PREFIX: &str = "turn-error:live:";

/// One sentence or label in both languages.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct Words {
    pub en: String,
    pub nb: String,
}

impl Words {
    fn of(value: Option<&Value>) -> Self {
        let Some(object) = value.and_then(Value::as_object) else {
            // A bare string is the English one.
            return Self { en: value.and_then(Value::as_str).unwrap_or_default().trim().to_owned(), nb: String::new() };
        };
        Self { en: json::string(object, "en").trim().to_owned(), nb: json::string(object, "nb").trim().to_owned() }
    }

    /// The words in Norwegian when `norwegian`, else English; the other
    /// language when that one is missing.
    pub fn get(&self, norwegian: bool) -> &str {
        let (first, other) = if norwegian { (&self.nb, &self.en) } else { (&self.en, &self.nb) };
        if first.is_empty() { other } else { first }
    }

    pub fn is_empty(&self) -> bool {
        self.en.is_empty() && self.nb.is_empty()
    }
}

/// One button of the card.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct Action {
    /// `retry`, `fresh_retry`, `retry_at`, `compact_retry`, `switch_model`,
    /// `sign_in`, `copy_details`.
    pub id: String,
    pub label: Words,
    /// `retry_at`: when the Host sends the message again.
    pub at_ms: Option<i64>,
    /// `sign_in`: whose sign-in.
    pub backend: String,
}

/// The action ids the desktop knows; others are left out.
pub const ACTIONS: &[&str] = &["retry", "fresh_retry", "retry_at", "compact_retry", "switch_model", "sign_in", "copy_details"];

/// Whether `id` sends the message again (`POST /turn-retry`): shown only on
/// the chat's newest failure while no reply came after it.
pub fn retries(id: &str) -> bool {
    matches!(id, "retry" | "fresh_retry" | "retry_at" | "compact_retry")
}

/// The Host's explanation of one failed turn.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct TurnError {
    pub code: String,
    /// The sentence's catalog key (`error.<code>`, docs/turn-errors.md).
    pub key: String,
    /// What fills the sentence: `backend`, `model`, `folder`, `time_ms`.
    pub params: Object,
    /// The Host's own sentence: the fallback for a key the catalog lacks.
    pub text: Words,
    /// `error`, `warning`, `info`.
    pub severity: String,
    /// In order of importance; only the ids the desktop knows.
    pub actions: Vec<Action>,
    /// The provider's raw words, for Details (may be empty).
    pub detail: String,
    pub backend: String,
    pub model: String,
    /// The user message the turn was for (what a retry sends again).
    pub message_id: String,
    pub trace_id: String,
    pub resets_at_ms: Option<i64>,
    /// Equal for the same failure: repeats collapse by it.
    pub fingerprint: String,
    /// What Copy puts on the clipboard.
    pub copy_text: String,
}

impl TurnError {
    /// The object as the Host sends it; `None` for anything without a
    /// sentence (an older Host's `turn.error` has only reason and message).
    pub fn from_json(value: &Value) -> Option<Self> {
        let object = value.as_object()?;
        let text = Words::of(object.get("text"));
        if text.is_empty() {
            return None;
        }
        let s = |key| json::string(object, key).trim().to_owned();
        let actions = json::array(object, "actions")
            .iter()
            .filter_map(Value::as_object)
            .map(|a| Action {
                id: json::string(a, "id"),
                label: Words::of(a.get("label")),
                at_ms: a.get("at_ms").and_then(Value::as_i64),
                backend: json::string(a, "backend"),
            })
            .filter(|a| ACTIONS.contains(&a.id.as_str()) && !a.label.is_empty())
            .collect();
        let severity = match s("severity").as_str() {
            "warning" => "warning",
            "info" => "info",
            _ => "error",
        };
        Some(Self {
            code: s("code"),
            key: s("key"),
            params: object.get("params").and_then(Value::as_object).cloned().unwrap_or_default(),
            text,
            severity: severity.to_owned(),
            actions,
            detail: s("detail"),
            backend: s("backend"),
            model: s("model"),
            message_id: s("message_id"),
            trace_id: s("trace_id"),
            resets_at_ms: object.get("resets_at_ms").and_then(Value::as_i64),
            fingerprint: s("fingerprint"),
            copy_text: json::string(object, "copy_text"),
        })
    }

    /// The buttons as they read at `now_ms`: a `retry_at` whose time has
    /// passed is a plain Try again (the window has reset; a card kept since
    /// then must not offer a time in the past), and sends no `at_ms`.
    pub fn actions_at(&self, now_ms: i64) -> Vec<Action> {
        let mut out: Vec<Action> = Vec::with_capacity(self.actions.len());
        for action in &self.actions {
            let action = match action.at_ms.or(self.resets_at_ms) {
                // Its words come from the catalog (`error.action.retry`).
                Some(at) if action.id == "retry_at" && at <= now_ms => Action { id: "retry".into(), ..Action::default() },
                _ => action.clone(),
            };
            if !out.iter().any(|a| a.id == action.id) {
                out.push(action);
            }
        }
        out
    }

    /// When the next `retry_at` turns into a plain retry, if one waits.
    pub fn next_change_ms(&self, now_ms: i64) -> Option<i64> {
        self.actions.iter().filter(|a| a.id == "retry_at").filter_map(|a| a.at_ms.or(self.resets_at_ms)).filter(|at| *at > now_ms).min()
    }

    /// `backend · model · trace_id`, each where known: Details' last line.
    pub fn meta(&self) -> String {
        [&self.backend, &self.model, &self.trace_id].iter().filter(|s| !s.is_empty()).map(|s| s.as_str()).collect::<Vec<_>>().join(" · ")
    }

    /// What Copy puts on the clipboard: the Host's prompt, or failing that
    /// the sentence, the raw words and where it happened.
    pub fn clipboard(&self) -> String {
        if !self.copy_text.trim().is_empty() {
            return self.copy_text.clone();
        }
        [self.text.en.as_str(), self.detail.as_str(), &self.meta()].iter().filter(|s| !s.is_empty()).copied().collect::<Vec<_>>().join("\n\n")
    }

    /// `POST /turn-retry`'s body for `action` in `session`; `None` for an
    /// action that sends nothing, or when there is no message to send again.
    pub fn retry_body(&self, session: &str, action: &Action) -> Option<Value> {
        if session.is_empty() || self.message_id.is_empty() || !retries(&action.id) {
            return None;
        }
        let mut body = json!({"session": session, "message_id": self.message_id});
        match action.id.as_str() {
            "fresh_retry" => body["fresh"] = json!(true),
            "compact_retry" => body["compact"] = json!(true),
            "retry_at" => body["at_ms"] = json!(action.at_ms.or(self.resets_at_ms)?),
            _ => {}
        }
        Some(body)
    }
}

/// The live turn's explanation: `turn.error` of a failed turn.
pub fn of_live_turn(turn: &Object) -> Option<TurnError> {
    if json::string(turn, "status") != "failed" {
        return None;
    }
    turn.get("error").and_then(TurnError::from_json)
}

/// A reply of the agent's: what ends a run of repeats and makes a card's
/// retry buttons stale. User messages, the Host's own rows (failures,
/// notices) and empty activity rows are not.
pub fn agent_reply(row: &Message) -> bool {
    row.role == "assistant" && row.origin != "system" && row.kind != "notice" && !row.automated && row.turn_error.is_none() && !row.text.trim().is_empty()
}

/// How the chat draws its failures.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Plan {
    /// Older repeats, drawn as the newest of them.
    pub hidden: HashSet<String>,
    /// Card id → how many failures it stands for (only above one).
    pub counts: HashMap<String, usize>,
    /// The card whose retry buttons show: the chat's newest failure, while
    /// no reply of the agent's came after it.
    pub actionable: Option<String>,
    /// The live failed turn's card, when no `/log` row says it yet.
    pub live: Option<(String, TurnError)>,
    /// The turns a card explains: their quiet turn-end line is not drawn too.
    pub traces: HashSet<String>,
}

impl Plan {
    pub fn count(&self, id: &str) -> usize {
        self.counts.get(id).copied().unwrap_or(1)
    }

    /// Whether `action` shows on card `id`.
    pub fn shows(&self, id: &str, action: &str) -> bool {
        !retries(action) || self.actionable.as_deref() == Some(id)
    }
}

/// The plan for `rows` (oldest first) and the live turn (`turn_id` and its
/// explanation, when it failed).
pub fn plan(rows: &[Message], live: Option<(&str, TurnError)>) -> Plan {
    let mut plan = Plan::default();
    // The live failure, unless a durable row of the same turn says it.
    let live = live.filter(|(turn_id, error)| {
        !rows.iter().any(|row| {
            let notice = row.turn_error.is_some() || !row.failure_kind.is_empty();
            let same = |trace: &str| !trace.is_empty() && (trace == *turn_id || trace == error.trace_id);
            notice && (same(&row.trace_id) || row.turn_error.as_ref().is_some_and(|e| same(&e.trace_id)))
        })
    });
    let live_key = live.as_ref().map(|(turn_id, _)| format!("{LIVE_PREFIX}{turn_id}"));
    // (id, its error), in order, with the replies between them.
    let mut items: Vec<(String, Option<&TurnError>)> =
        rows.iter().map(|row| (row.id.clone(), row.turn_error.as_ref().filter(|_| !row.automated))).collect();
    let replied: Vec<bool> = rows.iter().map(agent_reply).collect();
    if let (Some(key), Some((_, error))) = (&live_key, &live) {
        items.push((key.clone(), Some(error)));
    }
    // Fingerprint → the card drawn for its open run.
    let mut open: HashMap<&str, String> = HashMap::new();
    let mut newest: Option<String> = None;
    for (index, (id, error)) in items.iter().enumerate() {
        if replied.get(index).copied().unwrap_or(false) {
            open.clear();
            newest = None;
            continue;
        }
        let Some(error) = error else { continue };
        let trace = if error.trace_id.is_empty() { rows.get(index).map(|r| r.trace_id.as_str()).unwrap_or_default() } else { error.trace_id.as_str() };
        if !trace.is_empty() {
            plan.traces.insert(trace.to_owned());
        }
        if !error.fingerprint.is_empty() {
            if let Some(older) = open.insert(error.fingerprint.as_str(), id.clone()) {
                let count = plan.counts.remove(&older).unwrap_or(1) + 1;
                plan.hidden.insert(older);
                plan.counts.insert(id.clone(), count);
            }
        }
        newest = Some(id.clone());
    }
    if let Some((turn_id, _)) = &live {
        plan.traces.insert((*turn_id).to_owned());
    }
    plan.actionable = newest;
    plan.live = live.zip(live_key).map(|((_, error), key)| (key, error));
    plan
}
