//! The router of spoken messages: its pure rules.
//!
//! A message that is dictated hands-free and names no agent can be given to a model that
//! says whom it is for. Here is what does not depend on who is asked or on who the agents
//! are: when the router runs at all ([`should_run`]), what a model is told ([`prompt`]),
//! the JSON found in what it answered ([`extract_json`]), the decision read from that
//! ([`Decision::read`]), what of a decision an app is told ([`Decision::told`]), when the
//! router asks once more with every agent in view ([`asks_again`]), and the names a
//! provider goes by ([`provider`]). What a model is shown is in [`packet`], and a
//! message's way through the router, from the moment it comes in to what is kept of it,
//! in [`flow`].
//!
//! The rules are the Python Host's (`server/lib/orchestrator.py`, `orchestrator_model.py`,
//! `orchestrator_types.py`), held by `tests/fixtures/router-vectors.json`
//! (`server-rs/tools/router-vectors.py`).

use serde_json::{Map, Value, json};

use crate::janitors;
use crate::python;

pub mod flow;
pub mod packet;
pub mod phrases;

/// How sure the router has to be to act without asking, unless its owner sets another bar;
/// how sure for the agent that is open, and below what it does not even ask.
pub const HIGH_CONFIDENCE: f64 = 0.78;
pub const STICKY_CONFIDENCE: f64 = 0.58;
pub const CLARIFY_CONFIDENCE: f64 = 0.45;
/// How sure a model has to be that a message is noise for the router not to ask again.
const SURE_IT_IS_NOISE: f64 = 0.85;
/// The provider, model, effort, time and voice of a router nobody has set.
pub const DEFAULT_PROVIDER: &str = "openai";
pub const DEFAULT_MODEL: &str = "gpt-5.4-mini";
pub const DEFAULT_EFFORT: &str = "low";
pub const DEFAULT_TIMEOUT_MS: i64 = 30_000;
pub const DEFAULT_VOICE: &str = "79f8b5fb-2cc8-479a-80df-29f7a7cf1a3e";
/// The job of the janitor that routes, and the options of that job that are the router's.
pub const JOB: &str = "message-delegator";
pub const POLICY: [&str; 5] = [
    "fallback_only",
    "hands_free_only",
    "confidence_threshold",
    "timeout_ms",
    "voice_id",
];

/// What a model may decide.
pub const AGENT_MESSAGE: &str = "agent_message";
pub const CONTROL: &str = "control";
pub const STATUS: &str = "status_query";
pub const AGENT_CONTROL: &str = "agent_control";
pub const CORRECTION: &str = "recipient_correction";
pub const CLARIFY: &str = "clarify";
pub const AMBIGUOUS: &str = "ambiguous";
pub const IGNORED: &str = "ignored";
pub const ERROR: &str = "error";
const KINDS: [&str; 9] = [
    AGENT_MESSAGE,
    CONTROL,
    STATUS,
    AGENT_CONTROL,
    CORRECTION,
    CLARIFY,
    AMBIGUOUS,
    IGNORED,
    ERROR,
];

/// How the router is set.
#[derive(Debug, Clone, PartialEq)]
pub struct Settings {
    pub enabled: bool,
    /// Only what is dictated hands-free is routed.
    pub hands_free_only: bool,
    /// Only what an app could not place by a name is routed.
    pub fallback_only: bool,
    pub confidence_threshold: f64,
    pub provider: String,
    pub model: String,
    pub effort: String,
    pub timeout_ms: i64,
    pub voice_id: String,
}

impl Default for Settings {
    fn default() -> Self {
        Self {
            enabled: false,
            hands_free_only: true,
            fallback_only: true,
            confidence_threshold: HIGH_CONFIDENCE,
            provider: DEFAULT_PROVIDER.to_string(),
            model: DEFAULT_MODEL.to_string(),
            effort: DEFAULT_EFFORT.to_string(),
            timeout_ms: DEFAULT_TIMEOUT_MS,
            voice_id: DEFAULT_VOICE.to_string(),
        }
    }
}

impl Settings {
    /// As an app reads them, and as a model is told them.
    pub fn shown(&self) -> Value {
        json!({
            "enabled": self.enabled, "hands_free_only": self.hands_free_only,
            "fallback_only": self.fallback_only,
            "confidence_threshold": self.confidence_threshold, "provider": self.provider,
            "model": self.model, "effort": self.effort, "timeout_ms": self.timeout_ms,
            "voice_id": self.voice_id,
        })
    }
}

/// Whether a message goes through the router: it is on, the message is of the kind it is
/// set to take (dictated hands-free, unless it takes all), and an app asked for it because
/// it could place the message by no name (unless it takes every message).
pub fn should_run(settings: &Settings, hands_free: bool, fallback_request: bool) -> bool {
    settings.enabled
        && (hands_free || !settings.hands_free_only)
        && (fallback_request || !settings.fallback_only)
}

/// Whether Python takes a value as true: nothing, no, zero and what is empty are not.
fn truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(yes) => *yes,
        Value::Number(number) => number.as_f64().is_some_and(|number| number != 0.0),
        Value::String(text) => !text.is_empty(),
        Value::Array(items) => !items.is_empty(),
        Value::Object(fields) => !fields.is_empty(),
    }
}

/// A value as Python's `str()` makes a text of it.
fn text_of(value: &Value) -> String {
    match value {
        Value::String(text) => text.clone(),
        other => python::repr(other),
    }
}

/// A number as Python's `float()` reads a value, where it reads one.
fn float_of(value: &Value) -> Option<f64> {
    match value {
        Value::Bool(yes) => Some(f64::from(u8::from(*yes))),
        Value::Number(number) => number.as_f64(),
        Value::String(text) => {
            let text = python::strip(text);
            // An underscore between two digits is no part of the number.
            let signs: Vec<char> = text.chars().collect();
            let digit = |at: usize| signs.get(at).is_some_and(char::is_ascii_digit);
            let mut plain = String::with_capacity(text.len());
            for (at, sign) in signs.iter().enumerate() {
                let between = at > 0 && digit(at - 1) && digit(at + 1);
                match sign {
                    '_' if between => {}
                    '_' => return None,
                    sign => plain.push(*sign),
                }
            }
            plain.parse::<f64>().ok()
        }
        Value::Null | Value::Array(_) | Value::Object(_) => None,
    }
}

/// What a model decided about a message.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Decision {
    pub kind: String,
    pub target_session: String,
    pub confidence: f64,
    pub addressing: bool,
    pub mentioned_sessions: Vec<String>,
    pub name_corrections: Vec<Value>,
    pub candidate_scores: Vec<Value>,
    pub text_to_send: String,
    pub reason: String,
    pub phrase_key: String,
    pub spoken_text: String,
    pub pending_id: String,
    pub control_action: String,
    pub control_payload: Option<Map<String, Value>>,
    pub status_text: String,
    pub error: String,
}

impl Decision {
    /// The decision in what a model answered. What is no decision of a known kind is an
    /// error; what it says beside its kind is taken as far as it can be read.
    pub fn read(raw: &Map<String, Value>) -> Self {
        // The first of the names that is said as something: `raw.get(a) or raw.get(b)`.
        let said = |names: &[&str]| {
            let found = names.iter().filter_map(|name| raw.get(*name));
            found.filter(|value| truthy(value)).map(text_of).next()
        };
        let word = |name: &str| python::strip(&said(&[name]).unwrap_or_default()).to_string();
        let kind = python::strip(&said(&["kind", "decision_kind"]).unwrap_or_default()).to_string();
        let kind = if KINDS.contains(&kind.as_str()) {
            kind
        } else {
            ERROR.to_string()
        };
        let confidence = raw.get("confidence").filter(|value| truthy(value));
        let confidence = confidence.and_then(float_of).unwrap_or(0.0);
        // Between nothing and one. What is no number at all counts as one, as Python's
        // `min` and `max` leave it.
        let confidence = if confidence.is_nan() {
            1.0
        } else {
            confidence.clamp(0.0, 1.0)
        };
        let listed = |name: &str| match raw.get(name) {
            Some(Value::Array(items)) => items.clone(),
            _ => Vec::new(),
        };
        let payload = raw.get("control_payload").and_then(Value::as_object);
        Self {
            kind,
            target_session: word("target_session"),
            confidence,
            addressing: raw.get("addressing").is_some_and(truthy),
            mentioned_sessions: listed("mentioned_sessions").iter().map(text_of).collect(),
            name_corrections: listed("name_corrections"),
            candidate_scores: listed("candidate_scores"),
            text_to_send: word("text_to_send"),
            reason: word("reason"),
            phrase_key: word("phrase_key"),
            spoken_text: word("spoken_text"),
            pending_id: word("pending_id"),
            control_action: word("control_action"),
            control_payload: payload.cloned(),
            status_text: word("status_text"),
            error: word("error"),
        }
    }

    /// The decision whole, as it is kept.
    pub fn shown(&self) -> Value {
        json!({
            "kind": self.kind, "target_session": self.target_session,
            "confidence": self.confidence, "addressing": self.addressing,
            "mentioned_sessions": self.mentioned_sessions,
            "name_corrections": self.name_corrections,
            "candidate_scores": self.candidate_scores, "text_to_send": self.text_to_send,
            "reason": self.reason, "phrase_key": self.phrase_key,
            "spoken_text": self.spoken_text, "pending_id": self.pending_id,
            "control_action": self.control_action, "control_payload": self.control_payload,
            "status_text": self.status_text, "error": self.error,
        })
    }

    /// What of a decision an app is told.
    pub fn told(&self) -> Value {
        json!({
            "kind": self.kind, "target_session": self.target_session,
            "confidence": self.confidence, "addressing": self.addressing,
            "reason": self.reason, "phrase_key": self.phrase_key,
            "control_action": self.control_action,
        })
    }
}

/// What a model's answer that holds no JSON is told as (the Python Host's
/// `orchestrator_model.ModelAnswerNotJson`, since its PR 449).
pub const NOT_JSON: &str = "the model's answer was not JSON (JSONDecodeError)";

/// The JSON object in what a model answered: the answer itself, with or without a fence of
/// three backticks around it, or what stands between its first and its last brace. `Err`:
/// why there is none, in the Python Host's words where it has words of its own.
pub fn extract_json(text: &str) -> Result<Map<String, Value>, String> {
    let mut text = python::strip(text);
    if text.is_empty() {
        return Err("empty model response".to_string());
    }
    if let Some(fenced) = text.strip_prefix("```") {
        // The fence may name what is in it.
        let named = fenced
            .get(..4)
            .is_some_and(|word| word.eq_ignore_ascii_case("json"));
        let fenced = if named { &fenced[4..] } else { fenced };
        let fenced = python::strip(fenced);
        text = python::strip(fenced.strip_suffix("```").unwrap_or(fenced));
    }
    let whole = serde_json::from_str::<Value>(text);
    let read = match whole {
        Ok(read) => read,
        Err(_) => {
            // Words around the object: what stands between the first and the last brace.
            let braces = text.find('{').zip(text.rfind('}'));
            let inner = braces.filter(|(first, last)| first < last);
            let inner = inner.map(|(first, last)| &text[first..=last]);
            let read = inner.and_then(|inner| serde_json::from_str::<Value>(inner).ok());
            // The Python Host's own sentence since its PR 449 (R90), the same on every
            // interpreter: what its JSON reader said is in its log, and in no answer.
            read.ok_or_else(|| NOT_JSON.to_string())?
        }
    };
    match read {
        Value::Object(fields) => Ok(fields),
        _ => Err("orchestrator response must be a JSON object".to_string()),
    }
}

/// What a model is told for a packet: what the router is and does, and the packet itself.
pub fn prompt(packet: &Value) -> String {
    let failed_by_name = packet.get("fallback_request").is_some_and(truthy);
    let fallback = if failed_by_name {
        "This request is a failed deterministic delegation: the client already tried its \
         configured name-matching rule and found no single recipient. Your only job is to \
         infer which recent agent should receive the message. Use recent conversation fit, \
         not merely name mentions. Return agent_message with the best target_session and an \
         honest confidence, or ambiguous/clarify when the evidence is insufficient. Never \
         return status or control actions. Only agents included in INPUT are eligible; they \
         were active in the last 30 minutes, apart from the requested/focused session \
         retained for context.\n"
    } else {
        ""
    };
    let packet = python::compact_ascii(packet);
    format!(
        "You are the low-latency hands-free routing orchestrator for a local multi-agent \
         voice app. Return JSON only. Do not speak to the user.\n{fallback}Your job is to \
         decide whether the utterance addresses an agent, merely mentions an agent, asks \
         status, requests a control action, should be ignored as accidental \
         dictation/noise, or needs clarification. A name mention is not enough to route: \
         distinguish addressing from referring. Account for STT mistakes, for example Mark \
         may mean Mike only if the context supports it. Wrong-agent routing is worse than \
         asking a short clarification. Bias toward the sticky focus only when context is \
         compatible. Where two agents remain genuinely indistinguishable after weighing \
         the content - and only then, as a last resort rather than a shortcut - lean \
         slightly toward the one most recently spoken to. This is a tiebreak, not a \
         preference: it must never outweigh what the utterance is actually about. In \
         active hands-free dictation, if the utterance is complete nonsense, accidental \
         background speech, an unrelated fragment, or language/content that has no \
         plausible relation to any active agent, return ignored instead of routing or \
         clarifying.\nValid kind values: agent_message, status_query, control, \
         agent_control, recipient_correction, clarify, ambiguous, ignored, error.\nFor \
         agent_message include target_session, confidence 0..1, addressing, text_to_send, \
         reason. For mention-only status questions use status_query and do not route to \
         the agent. For repeat/say-that-again use control with control_action repeat_last. \
         For relaunch/resume/fork/stop/switch use agent_control with a control_action and \
         target_session. For ignored, include confidence and a short reason; do not \
         include spoken_text. If unsure whether it is accidental or meaningful, return \
         clarify or ambiguous with spoken_text.\nINPUT:\n{packet}"
    )
}

/// Whether the router asks a model once more with every agent in view, after it asked
/// with the agent that is open and those around it (`scope`: what the first packet held).
pub fn asks_again(decision: &Decision, scope: Option<&str>, requested_session: &str) -> bool {
    if scope != Some("focused") || decision.kind == ERROR {
        return false;
    }
    if decision.kind == IGNORED {
        return decision.confidence < SURE_IT_IS_NOISE;
    }
    let target = python::strip(&decision.target_session);
    let another = !target.is_empty() && target != requested_session;
    match decision.kind.as_str() {
        _ if another => true,
        CLARIFY | AMBIGUOUS => true,
        AGENT_MESSAGE => decision.confidence < STICKY_CONFIDENCE,
        STATUS => target.is_empty(),
        _ => false,
    }
}

/// The name a provider is known by: a tool of the Host's by its own name or another it
/// goes by, the two that are no tool (`openai`, `typesafe`), or what was said, in small
/// letters, for whoever asked to refuse.
pub fn provider(raw: Option<&str>) -> String {
    let name = python::strip(raw.unwrap_or_default()).to_lowercase();
    match name.as_str() {
        "openai" | "gpt" => "openai".to_string(),
        "typesafe" | "jev" => "typesafe".to_string(),
        "claude-code" => "claude".to_string(),
        _ => janitors::tool_named(&name).map_or(name, str::to_string),
    }
}
