//! Requests to the user: an approval, a question with choices, something to type.
//!
//! An agent asks, the user answers on a phone, and the answer is told to the agent as a
//! message. A request can also end without an answer: the user discards it, the agent takes
//! it back, or its time runs out. Only an answer and a run-out are told to the agent; a
//! discard is silent, so that it does not invite the agent to ask again.
//!
//! The inbox of what waits for the user is the pending requests of all agents, the ones that
//! stop work first.

use serde::{Deserialize, Serialize};
use serde_json::{Map, Value, json};

use clarp_speech::markup::strip_hidden_blocks;

use super::{
    Artifact, ArtifactBook, Ctx, Maker, New, Refused, SAFE_INTEGER, Shelf, forbid, is_true, new_id,
    refuse, valid_id,
};
use crate::python;

/// The request on an artifact of kind `decision` or `question`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Decision {
    pub decision_id: String,
    pub question: String,
    pub context: String,
    pub yes_label: String,
    pub no_label: String,
    /// `pending`, then `accepted`, `rejected`, `answered`, `cancelled` or `expired`.
    pub status: String,
    /// How it ended: the status, or `dismissed` and `withdrawn` for a cancelled one.
    pub resolved_choice: String,
    #[serde(default)]
    pub resolved_at: Option<i64>,
    /// `user`, `agent`, or `agent:<session>` for an answer given on the user's behalf.
    pub resolved_by: String,
    pub revision: i64,
    #[serde(default)]
    pub expires_at: Option<i64>,
    /// `approval`, `single_choice` or `text_input`.
    pub response_type: String,
    pub options: Vec<Value>,
    pub allow_custom_text: bool,
    #[serde(default)]
    pub recommended_option_id: Option<String>,
    pub blocks_progress: bool,
    pub priority_reason: String,
    pub urgency: String,
    pub response_effort: String,
    #[serde(default)]
    pub deadline_at: Option<i64>,
    /// What the user answered, as it was understood: a choice, an option, a text.
    #[serde(default)]
    pub answer: Value,
    /// What is to be told to the agent of how it ended, and whether it was.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub delivery: Option<Delivery>,
}

/// How a request ended, on its way to the agent that asked.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Delivery {
    /// `pending`, `delivered` or `cancelled`.
    pub status: String,
    pub choice: String,
    #[serde(default)]
    pub answer: Value,
    pub created_at: i64,
    #[serde(default)]
    pub delivered_at: Option<i64>,
}

/// What a request is to be made from, as the caller gave it.
#[derive(Debug, Clone, PartialEq)]
pub struct Ask {
    pub title: String,
    pub question: Value,
    pub context: Value,
    pub yes_label: Value,
    pub no_label: Value,
    pub payload: Option<Value>,
    pub reference_id: String,
    pub expires_at: Option<Value>,
    pub response_type: Value,
    pub options: Option<Value>,
    pub allow_custom_text: Option<Value>,
    pub recommended_option_id: Option<Value>,
    pub blocks_progress: Value,
    pub priority_reason: Value,
    pub urgency: Value,
    pub response_effort: Value,
    pub deadline_at: Option<Value>,
    pub input_hint: Option<Value>,
}

impl Default for Ask {
    fn default() -> Self {
        Self {
            title: String::new(),
            question: json!(""),
            context: json!(""),
            yes_label: json!("Yes"),
            no_label: json!("No"),
            payload: None,
            reference_id: String::new(),
            expires_at: None,
            response_type: json!("approval"),
            options: None,
            allow_custom_text: None,
            recommended_option_id: None,
            blocks_progress: json!(false),
            priority_reason: json!(""),
            urgency: json!("normal"),
            response_effort: json!("review"),
            deadline_at: None,
            input_hint: None,
        }
    }
}

/// A text of a request: without hidden blocks and the space around it, and not too long.
fn decision_text(
    value: &Value,
    field: &str,
    limit: usize,
    required: bool,
) -> Result<String, Refused> {
    let Some(text) = value.as_str() else {
        return refuse(format!("{field} must be a string"));
    };
    let text = strip_hidden_blocks(text).trim().to_string();
    if required && text.is_empty() {
        return refuse(format!("{field} required"));
    }
    if text.chars().count() > limit {
        return refuse(format!("{field} too large"));
    }
    Ok(text)
}

/// A whole number a client can hold exactly, and not below `minimum`.
pub fn integer(value: &Value, field: &str, minimum: i64) -> Result<i64, Refused> {
    let whole = match value {
        Value::Number(number) if number.is_i64() || number.is_u64() => number.as_i64(),
        _ => return refuse(format!("{field} must be an integer")),
    };
    match whole {
        Some(whole) if (minimum..=SAFE_INTEGER).contains(&whole) => Ok(whole),
        _ => refuse(format!("{field} is outside the supported range")),
    }
}

/// The options of a question: two or three, each with an id of its own and a label.
fn decision_options(options: Option<&Value>) -> Result<Vec<Value>, Refused> {
    let list = options.and_then(Value::as_array);
    let Some(list) = list.filter(|list| (2..=3).contains(&list.len())) else {
        return refuse("questions require 2 or 3 options");
    };
    let mut normalized: Vec<Value> = Vec::new();
    let mut ids: Vec<String> = Vec::new();
    for option in list {
        let known = |key: &String| matches!(key.as_str(), "id" | "label" | "description");
        let Some(fields) = option.as_object().filter(|fields| fields.keys().all(known)) else {
            return refuse("options must contain id, label and optional description");
        };
        let none = Value::Null;
        let id = decision_text(fields.get("id").unwrap_or(&none), "option id", 80, true)?;
        if !valid_id(&id) || ids.contains(&id) {
            return refuse("option ids must be valid and unique");
        }
        let label = decision_text(
            fields.get("label").unwrap_or(&none),
            "option label",
            200,
            true,
        )?;
        let mut item = json!({ "id": id, "label": label });
        if let Some(description) = fields.get("description") {
            item["description"] = json!(decision_text(
                description,
                "option description",
                1_000,
                false
            )?);
        }
        normalized.push(item);
        ids.push(id);
    }
    Ok(normalized)
}

/// What kind of typing a typed answer is: plain text, or a code from a message.
fn input_hint(payload: &Map<String, Value>) -> &str {
    match payload.get("input_hint").and_then(Value::as_str) {
        Some("one_time_code") => "one_time_code",
        _ => "text",
    }
}

/// A request as it was asked, once everything about it is in order: the request itself,
/// not yet named, and the payload its artifact gets.
fn asked(ask: &Ask, now: i64) -> Result<(Decision, Option<Value>), Refused> {
    let question = decision_text(&ask.question, "decision question", 4_000, true)?;
    let context = decision_text(&ask.context, "decision context", 16_000, false)?;
    let label = |value: &Value, otherwise: &str| {
        let text = decision_text(value, "decision label", 80, false)?;
        Ok(if text.is_empty() {
            otherwise.to_string()
        } else {
            text
        })
    };
    let yes_label = label(&ask.yes_label, "Yes")?;
    let no_label = label(&ask.no_label, "No")?;
    let response_type = match ask.response_type.as_str() {
        Some(kind @ ("approval" | "single_choice" | "text_input")) => kind,
        _ => return refuse("unsupported response_type"),
    };
    let given = |value: &Option<Value>| value.clone().filter(|value| !value.is_null());
    let options = given(&ask.options);
    let recommended = given(&ask.recommended_option_id);
    let hint = given(&ask.input_hint);
    let mut allow_custom_text = given(&ask.allow_custom_text);
    let mut payload = ask.payload.clone();
    if response_type == "text_input" {
        // A typed answer the agent needs, such as a code from a message.
        let no_options = options
            .as_ref()
            .is_none_or(|o| o.as_array().is_some_and(Vec::is_empty));
        if !no_options || recommended.is_some() || allow_custom_text == Some(json!(false)) {
            return refuse("text input requests take no options");
        }
        allow_custom_text = Some(json!(true));
        let hint = match hint.as_ref().map(Value::as_str) {
            None => "text",
            Some(Some(hint @ ("text" | "one_time_code"))) => hint,
            Some(_) => return refuse("input_hint must be text or one_time_code"),
        };
        let mut fields = match payload {
            Some(Value::Object(fields)) => fields,
            _ => Map::new(),
        };
        fields.insert("input_hint".to_string(), json!(hint));
        payload = Some(Value::Object(fields));
    } else if hint.is_some() {
        return refuse("input_hint applies only to text input requests");
    }
    let allow_custom_text = match allow_custom_text {
        None => response_type == "single_choice",
        Some(Value::Bool(allow)) => allow,
        Some(_) => return refuse("allow_custom_text must be a boolean"),
    };
    let Some(blocks_progress) = ask.blocks_progress.as_bool() else {
        return refuse("blocks_progress must be a boolean");
    };
    let urgency = match ask.urgency.as_str() {
        Some(urgency @ ("normal" | "time_sensitive")) => urgency,
        _ => return refuse("unsupported urgency"),
    };
    let response_effort = match ask.response_effort.as_str() {
        Some(effort @ ("quick" | "short" | "review")) => effort,
        _ => return refuse("unsupported response_effort"),
    };
    let priority_reason = decision_text(&ask.priority_reason, "priority_reason", 2_000, false)?;
    if (blocks_progress || urgency != "normal") && priority_reason.is_empty() {
        return refuse("blocking or time-sensitive requests require priority_reason");
    }
    let mut recommended_option_id = None;
    let options = match response_type {
        "single_choice" => {
            let options = decision_options(options.as_ref())?;
            if let Some(recommended) = &recommended {
                let id = decision_text(recommended, "recommended_option_id", 80, true)?;
                if !options
                    .iter()
                    .any(|option| option["id"].as_str() == Some(&id))
                {
                    return refuse("recommended_option_id must identify an option");
                }
                recommended_option_id = Some(id);
            }
            options
        }
        "text_input" => Vec::new(),
        _ => {
            if options.is_some() || recommended.is_some() || allow_custom_text {
                return refuse("approval requests do not support options or custom text");
            }
            Vec::new()
        }
    };
    let moment = |value: &Option<Value>, field: &str| {
        given(value)
            .map(|value| integer(&value, field, 0))
            .transpose()
    };
    let expires_at = moment(&ask.expires_at, "expires_at")?;
    if expires_at.is_some_and(|at| at > now + 365 * 24 * 60 * 60 * 1_000) {
        return refuse("expires_at is too far in the future");
    }
    let deadline_at = moment(&ask.deadline_at, "deadline_at")?;
    let decision = Decision {
        decision_id: String::new(),
        question,
        context,
        yes_label,
        no_label,
        status: "pending".to_string(),
        resolved_choice: String::new(),
        resolved_at: None,
        resolved_by: String::new(),
        revision: 1,
        expires_at,
        response_type: response_type.to_string(),
        options,
        allow_custom_text,
        recommended_option_id,
        blocks_progress,
        priority_reason,
        urgency: urgency.to_string(),
        response_effort: response_effort.to_string(),
        deadline_at,
        answer: Value::Null,
        delivery: None,
    };
    Ok((decision, payload))
}

/// What a request is refused with when nobody has the session it is asked for: what is
/// wrong with the request itself, and only then that there is no such agent.
pub fn ask_of_nobody(ask: &Ask, now: i64) -> Refused {
    match asked(ask, now) {
        Err(why) => why,
        Ok(_) => Refused {
            why: super::Why::Value,
            message: "unknown agent session".to_string(),
        },
    }
}

impl Decision {
    /// How much a request weighs in the inbox: one that stops work above one that does
    /// not, an urgent one above a normal one.
    pub fn priority(&self) -> i64 {
        let base = if self.blocks_progress { 200 } else { 100 };
        base + if self.urgency == "time_sensitive" {
            10
        } else {
            0
        }
    }

    /// Whether how it ended still waits to be told to the agent.
    pub fn delivery_pending(&self) -> bool {
        self.delivery
            .as_ref()
            .is_some_and(|delivery| delivery.status == "pending")
    }

    /// Not answered and not run out at `now`.
    pub fn open(&self, now: i64) -> bool {
        self.status == "pending" && self.expires_at.is_none_or(|at| at > now)
    }

    fn row(&self, artifact: &Artifact) -> Value {
        json!({
            "decision_id": self.decision_id,
            "artifact_id": artifact.artifact_id,
            "question": self.question,
            "context": self.context,
            "yes_label": self.yes_label,
            "no_label": self.no_label,
            "status": self.status,
            "resolved_choice": self.resolved_choice,
            "resolved_at": self.resolved_at,
            "resolved_by": self.resolved_by,
            "revision": self.revision,
            "expires_at": self.expires_at,
            "response_type": self.response_type,
            "allow_custom_text": self.allow_custom_text,
            "recommended_option_id": self.recommended_option_id,
            "blocks_progress": self.blocks_progress,
            "priority_reason": self.priority_reason,
            "urgency": self.urgency,
            "response_effort": self.response_effort,
            "deadline_at": self.deadline_at,
        })
    }

    /// The request as it is shown on its artifact.
    pub fn public(&self, artifact: &Artifact) -> Value {
        let mut item = self.row(artifact);
        item["options"] = json!(self.options);
        item["answer"] = self.answer.clone();
        item["priority"] = json!(self.priority());
        item["archived_at"] = json!(artifact.archived_at);
        item["delivery_pending"] = json!(self.delivery_pending());
        if self.response_type == "text_input" {
            item["input_hint"] = json!(input_hint(&artifact.payload));
        }
        item
    }

    /// The request as an entry of the inbox.
    fn inbox_item(&self, artifact: &Artifact, persona: &str) -> Value {
        let mut item = self.row(artifact);
        item["agent_id"] = json!(artifact.agent_id);
        item["session"] = json!(artifact.session);
        item["title"] = json!(artifact.title);
        item["summary"] = json!(artifact.summary);
        item["created_at"] = json!(artifact.created_at);
        item["updated_at"] = json!(artifact.updated_at);
        item["archived_at"] = json!(artifact.archived_at);
        item["agent_name"] = json!(persona);
        item["options"] = json!(self.options);
        item["answer"] = self.answer.clone();
        item["priority"] = json!(self.priority());
        item["delivery_pending"] = json!(self.delivery_pending());
        let kind = if self.response_type == "approval" {
            "decision"
        } else {
            "question"
        };
        item["kind"] = json!(kind);
        if self.response_type == "text_input" {
            item["input_hint"] = json!(input_hint(&artifact.payload));
        }
        item
    }

    /// Reads an answer against what was asked: how the request ends by it, and the answer
    /// as it is kept.
    fn answered(
        &self,
        choice: Option<&Value>,
        answer: Option<&Value>,
    ) -> Result<(String, Value), Refused> {
        let (choice, answer) = (
            choice.filter(|v| !v.is_null()),
            answer.filter(|v| !v.is_null()),
        );
        if self.response_type == "approval" {
            let Some(choice) = choice.and_then(Value::as_str).filter(|_| answer.is_none()) else {
                return refuse("approval requires an explicit accepted or rejected choice");
            };
            let choice = choice.trim().to_lowercase();
            if !matches!(choice.as_str(), "accepted" | "rejected") {
                return refuse("invalid decision choice");
            }
            return Ok((choice.clone(), json!({ "choice": choice })));
        }
        if !matches!(self.response_type.as_str(), "single_choice" | "text_input") {
            return refuse("unsupported response_type");
        }
        if choice.is_some() {
            return refuse("questions require a typed answer, not a binary choice");
        }
        let fields = answer.and_then(Value::as_object);
        let only = |name: &str| fields.is_some_and(|f| f.len() == 1 && f.contains_key(name));
        if self.response_type == "text_input" && !only("text") {
            return refuse("text input requests take a text answer");
        }
        if !only("option_id") && !only("text") {
            return refuse("answer must contain exactly one of option_id or text");
        }
        if let Some(identifier) = fields.and_then(|fields| fields.get("option_id")) {
            let Some(identifier) = identifier.as_str() else {
                return refuse("answer option_id must be a string");
            };
            let chosen = self
                .options
                .iter()
                .find(|option| option["id"].as_str() == Some(identifier.trim()));
            return match chosen {
                Some(option) => Ok((
                    "answered".to_string(),
                    json!({ "option_id": option["id"], "label": option["label"] }),
                )),
                None => refuse("answer option_id does not identify an option"),
            };
        }
        if !self.allow_custom_text {
            return refuse("custom text answers are not enabled");
        }
        // The user's own wording: its case, its inner spaces and its markup are kept.
        let text = fields
            .and_then(|fields| fields.get("text"))
            .and_then(Value::as_str);
        match text.map(str::trim) {
            Some(text) if !text.is_empty() && text.chars().count() <= 4_000 => {
                Ok(("answered".to_string(), json!({ "text": text })))
            }
            _ => refuse("answer text must be a nonempty string of at most 4000 characters"),
        }
    }
}

/// An answer as it was given: for the revision of the request the one who answers saw, a
/// choice for an approval, an option or a text for a question.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Given<'a> {
    pub expected_revision: i64,
    pub choice: Option<&'a Value>,
    pub answer: Option<&'a Value>,
}

/// Who answers a request in the user's place, when someone does.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct OnBehalf<'a> {
    /// The session that says it answers for the user.
    pub session: Option<&'a str>,
    /// The sessions the owner allows to do that.
    pub designated: &'a [String],
    /// The session the Host knows the caller to be, or empty.
    pub verified: &'a str,
}

impl ArtifactBook {
    /// The artifact that carries the request with this id.
    pub fn of_decision(&self, decision_id: &str) -> Option<&Artifact> {
        self.artifacts().find(|artifact| {
            artifact
                .decision
                .as_ref()
                .is_some_and(|decision| decision.decision_id == decision_id)
        })
    }

    /// Asks the user something: an approval, a question with choices, something to type.
    pub fn ask(&self, maker: Maker<'_>, ask: &Ask, ctx: &mut Ctx<'_>) -> Result<Artifact, Refused> {
        let (mut decision, payload) = asked(ask, ctx.now_ms)?;
        let kind = if decision.response_type == "approval" {
            "decision"
        } else {
            "question"
        };
        let new = New {
            kind: kind.to_string(),
            title: ask.title.clone(),
            summary: decision.question.clone(),
            status: "active".to_string(),
            reference_id: ask.reference_id.clone(),
            payload,
            artifact_id: String::new(),
            expected_version: None,
            validate_only: false,
        };
        let mut artifact = self.make(maker, &new, ctx)?;
        decision.decision_id = new_id("decision", ctx);
        artifact.decision = Some(decision);
        Ok(artifact)
    }

    /// The requests whose time ran out, each as its running out leaves it: closed, and to be
    /// told to the agent. Whoever keeps the book writes them before it answers anything.
    pub fn expired(&self, now: i64) -> Vec<Artifact> {
        let ran_out = |artifact: &&Artifact| {
            artifact.decision.as_ref().is_some_and(|decision| {
                decision.status == "pending" && decision.expires_at.is_some_and(|at| at <= now)
            })
        };
        self.artifacts()
            .filter(ran_out)
            .map(|artifact| {
                let mut artifact = artifact.clone();
                artifact.status = "expired".to_string();
                artifact.updated_at = now.max(artifact.updated_at + 1);
                artifact.completed_at = Some(now);
                if let Some(decision) = artifact.decision.as_mut() {
                    decision.status = "expired".to_string();
                    decision.resolved_at = Some(now);
                    decision.revision += 1;
                    decision.delivery = Some(Delivery {
                        status: "pending".to_string(),
                        choice: "expired".to_string(),
                        answer: Value::Null,
                        created_at: now,
                        delivered_at: None,
                    });
                }
                artifact
            })
            .collect()
    }

    /// The earliest moment a pending request runs out, for whoever sets the alarm.
    pub fn next_expiry(&self) -> Option<i64> {
        self.artifacts()
            .filter_map(|artifact| artifact.decision.as_ref())
            .filter(|decision| decision.status == "pending")
            .filter_map(|decision| decision.expires_at)
            .min()
    }

    /// The requests whose end still waits to be told to the agent, by their ids.
    pub fn undelivered(&self) -> Vec<String> {
        self.artifacts()
            .filter_map(|artifact| artifact.decision.as_ref())
            .filter(|decision| decision.delivery_pending())
            .map(|decision| decision.decision_id.clone())
            .collect()
    }

    /// The user answers a request: accepts or rejects an approval, chooses an option or
    /// writes an answer. Answers the artifact and whether that changed anything: the same
    /// answer given again changes nothing.
    pub fn resolve(
        &self,
        decision_id: &str,
        given: &Given<'_>,
        by: &OnBehalf<'_>,
        session_exists: &dyn Fn(&str) -> bool,
        now: i64,
    ) -> Result<(Artifact, bool), Refused> {
        let resolved_by = acting(by)?.map_or_else(|| "user".to_string(), |s| format!("agent:{s}"));
        let Some(artifact) = self.of_decision(decision_id) else {
            return refuse("decision not found");
        };
        let Some(decision) = &artifact.decision else {
            return refuse("decision not found");
        };
        let (outcome, normalized) = decision.answered(given.choice, given.answer)?;
        if let Some(acting) = resolved_by.strip_prefix("agent:") {
            if artifact.session == acting {
                return forbid("the asking agent may not answer its own request");
            }
            if !session_exists(acting) {
                return refuse("on-behalf session not found");
            }
        }
        if decision.status != "pending" {
            if decision.status == "expired" {
                return refuse("decision expired");
            }
            let same = decision.status == outcome
                && decision.resolved_by == resolved_by
                && decision.answer == normalized;
            return if same {
                Ok((artifact.clone(), false))
            } else {
                refuse("decision already resolved with a different choice or answer")
            };
        }
        if decision.expires_at.is_some_and(|at| at <= now) {
            return refuse("decision expired");
        }
        if decision.revision != given.expected_revision {
            return refuse("decision revision changed");
        }
        let now = now.max(artifact.updated_at + 1);
        let mut artifact = artifact.clone();
        artifact.status = "completed".to_string();
        artifact.updated_at = now;
        artifact.completed_at = Some(now);
        if let Some(decision) = artifact.decision.as_mut() {
            decision.status.clone_from(&outcome);
            decision.resolved_choice.clone_from(&outcome);
            decision.answer = normalized.clone();
            decision.resolved_at = Some(now);
            decision.resolved_by = resolved_by;
            decision.revision += 1;
            decision.delivery = Some(Delivery {
                status: "pending".to_string(),
                choice: outcome,
                answer: normalized,
                created_at: now,
                delivered_at: None,
            });
        }
        Ok((artifact, true))
    }

    /// The user discards a pending request without answering it. Nothing is told to the
    /// agent.
    pub fn dismiss(
        &self,
        decision_id: &str,
        expected_revision: i64,
        now: i64,
    ) -> Result<(Artifact, bool), Refused> {
        let found = self.of_decision(decision_id);
        let Some((artifact, decision)) = found.and_then(|a| Some((a, a.decision.as_ref()?))) else {
            return refuse("decision not found");
        };
        if decision.status == "cancelled" && decision.resolved_choice == "dismissed" {
            return Ok((artifact.clone(), false));
        }
        if decision.status != "pending" {
            return refuse("decision is no longer pending");
        }
        if decision.expires_at.is_some_and(|at| at <= now) {
            return refuse("decision expired");
        }
        if decision.revision != expected_revision {
            return refuse("decision revision changed");
        }
        Ok((closed(artifact, "dismissed", "user", now), true))
    }

    /// The agent that asked takes its own pending request back: it no longer holds.
    pub fn withdraw(
        &self,
        decision_id: &str,
        session: &str,
        now: i64,
    ) -> Result<(Artifact, bool), Refused> {
        let session = session.trim();
        if session.is_empty() {
            return refuse("session required");
        }
        let found = self.of_decision(decision_id);
        let Some((artifact, decision)) = found.and_then(|a| Some((a, a.decision.as_ref()?))) else {
            return refuse("decision not found");
        };
        if artifact.session != session {
            return forbid("only the asking agent may withdraw this request");
        }
        if decision.status == "cancelled" && decision.resolved_choice == "withdrawn" {
            return Ok((artifact.clone(), false));
        }
        if decision.status != "pending" {
            return refuse("decision is no longer pending");
        }
        Ok((closed(artifact, "withdrawn", "agent", now), true))
    }

    /// What was told to the agent is told: the request's delivery is done.
    pub fn delivered(&self, decision_id: &str, now: i64) -> Option<Artifact> {
        let mut artifact = self.of_decision(decision_id)?.clone();
        let delivery = artifact.decision.as_mut()?.delivery.as_mut()?;
        if delivery.status != "pending" {
            return None;
        }
        delivery.status = "delivered".to_string();
        delivery.delivered_at = Some(now);
        Some(artifact)
    }

    /// The agent goes: what it still asks is closed, what waits to be told to it is not
    /// told, and what it was working on is cancelled.
    pub fn cancelled_with_agent(&self, now: i64) -> Vec<Artifact> {
        let mut changed = Vec::new();
        for artifact in self.artifacts() {
            let mut next = artifact.clone();
            let asking = matches!(artifact.kind.as_str(), "decision" | "question");
            if let Some(decision) = next.decision.as_mut() {
                if let Some(delivery) = decision.delivery.as_mut().filter(|d| d.status == "pending")
                {
                    delivery.status = "cancelled".to_string();
                }
                if asking && artifact.status == "active" && decision.status == "pending" {
                    decision.status = "cancelled".to_string();
                    decision.revision += 1;
                }
            }
            if artifact.status == "active" {
                next.status = "cancelled".to_string();
                next.updated_at = now.max(artifact.updated_at + 1);
                next.completed_at = Some(now);
            }
            if next != *artifact {
                changed.push(next);
            }
        }
        changed
    }

    /// Whether the agent waits for an answer it may not go on without: any approval, and a
    /// question that says it stops the work.
    pub fn has_blocking_decision(&self, now: i64) -> bool {
        self.artifacts().any(|artifact| {
            artifact.decision.as_ref().is_some_and(|decision| {
                decision.open(now)
                    && (decision.response_type == "approval" || decision.blocks_progress)
            })
        })
    }

    /// Whether the agent has anything the user has not answered.
    pub fn has_pending_decision(&self, now: i64) -> bool {
        self.artifacts()
            .any(|artifact| artifact.decision.as_ref().is_some_and(|d| d.open(now)))
    }
}

/// The session that answers in the user's place, once it is known that it may.
fn acting<'a>(by: &OnBehalf<'a>) -> Result<Option<&'a str>, Refused> {
    let Some(session) = by.session else {
        return if by.verified.is_empty() {
            Ok(None)
        } else {
            forbid("agent resolution requires on_behalf_session")
        };
    };
    let session = session.trim();
    if session.is_empty() {
        return refuse("on_behalf_session must be a nonempty session");
    }
    if !by.designated.iter().any(|known| known.trim() == session) {
        return forbid("session is not designated to answer on the user's behalf");
    }
    if by.verified.trim() != session {
        return forbid("on-behalf answer requires the designated agent's verified turn identity");
    }
    Ok(Some(session))
}

/// A pending request closed without an answer, and without a word to the agent.
fn closed(artifact: &Artifact, choice: &str, by: &str, now: i64) -> Artifact {
    let now = now.max(artifact.updated_at + 1);
    let mut artifact = artifact.clone();
    artifact.status = "cancelled".to_string();
    artifact.updated_at = now;
    artifact.completed_at = Some(now);
    if let Some(decision) = artifact.decision.as_mut() {
        decision.status = "cancelled".to_string();
        decision.resolved_choice = choice.to_string();
        decision.resolved_by = by.to_string();
        decision.resolved_at = Some(now);
        decision.revision += 1;
    }
    artifact
}

/// The inbox: what waits for the user across the agents, the heaviest first, then the one
/// that is due first, then the oldest.
pub fn attention(
    shelves: &[Shelf<'_>],
    include_questions: bool,
    include_archived: bool,
    now: i64,
) -> Vec<Value> {
    let mut items: Vec<(i64, bool, i64, i64, String, Value)> = Vec::new();
    for shelf in shelves {
        for artifact in shelf.artifacts.artifacts() {
            let Some(decision) = &artifact.decision else {
                continue;
            };
            let shown = decision.open(now)
                && artifact.deleted_at.is_none()
                && (include_questions || decision.response_type == "approval")
                && (include_archived || artifact.archived_at.is_none());
            if shown {
                items.push((
                    -decision.priority(),
                    decision.deadline_at.is_none(),
                    decision.deadline_at.unwrap_or(0),
                    artifact.created_at,
                    decision.decision_id.clone(),
                    decision.inbox_item(artifact, shelf.maker.persona),
                ));
            }
        }
    }
    items.sort_by(|a, b| (a.0, a.1, a.2, a.3, &a.4).cmp(&(b.0, b.1, b.2, b.3, &b.4)));
    items.into_iter().map(|item| item.5).collect()
}

/// How a request ended, as it waits to be told to its agent.
pub fn delivery_row(artifact: &Artifact) -> Option<Value> {
    let decision = artifact.decision.as_ref()?;
    let delivery = decision.delivery.as_ref()?;
    let answer_json = match &delivery.answer {
        Value::Null => Value::Null,
        answer => json!(python::dumps(answer)),
    };
    Some(json!({
        "decision_id": decision.decision_id,
        "artifact_id": artifact.artifact_id,
        "session": artifact.session,
        "question": decision.question,
        "context": decision.context,
        "reference_id": artifact.reference_id,
        "payload_json": Value::Object(artifact.payload.clone()).to_string(),
        "choice": delivery.choice,
        "status": delivery.status,
        "created_at": delivery.created_at,
        "delivered_at": delivery.delivered_at,
        "response_type": decision.response_type,
        "answer_json": answer_json,
        "resolved_by": decision.resolved_by,
        "answer": delivery.answer,
    }))
}

/// What waits to be told to the agents, the oldest first.
pub fn pending_deliveries<'a>(shelves: &[Shelf<'a>]) -> Vec<&'a Artifact> {
    let mut waiting: Vec<(i64, &Artifact)> = shelves
        .iter()
        .flat_map(|shelf| shelf.artifacts.artifacts())
        .filter_map(|artifact| {
            let delivery = artifact.decision.as_ref()?.delivery.as_ref()?;
            (delivery.status == "pending").then_some((delivery.created_at, artifact))
        })
        .collect();
    waiting.sort_by_key(|(created_at, artifact)| (*created_at, artifact.artifact_id.clone()));
    waiting.into_iter().map(|(_, artifact)| artifact).collect()
}

/// The message an agent is told with how its request ended. `None` for a request that has
/// nothing to be told.
pub fn delivery_prompt(artifact: &Artifact) -> Option<String> {
    let decision = artifact.decision.as_ref()?;
    let delivery = decision.delivery.as_ref()?;
    let resolved_by = match decision.resolved_by.as_str() {
        "" => "user",
        by => by,
    };
    let actor = match resolved_by.strip_prefix("agent:") {
        Some(session) => format!("The user, through agent {session}"),
        None => "The user".to_string(),
    };
    let answered = delivery.choice == "answered" && delivery.answer.is_object();
    let outcome = match (delivery.choice.as_str(), decision.response_type.as_str()) {
        ("expired", _) => "The request expired without an answer or approval. Do not infer a \
                           choice or perform the protected action. Continue independent work only."
            .to_string(),
        ("dismissed", _) => "The user discarded this request. This is not an answer or approval. \
                             Do not guess permission or repeat the unchanged request. Continue \
                             independent work only."
            .to_string(),
        (_, "single_choice" | "text_input") if answered => format!(
            "{actor} answered this clarification: {}. Continue using this answer. This does not \
             grant approval for unrelated protected actions.",
            python::dumps(&delivery.answer)
        ),
        ("accepted", "approval") => {
            let question = decision.question.trim();
            let scope = match decision.context.trim() {
                "" => String::new(),
                context => format!(" Approved scope/context: {context}"),
            };
            format!(
                "{actor} approved this exact action: {question}{scope} Proceed with exactly \
                 that action and scope after revalidating the target and payload. This does \
                 not approve any broader, additional, or repeated action."
            )
        }
        ("rejected", "approval") => format!(
            "{actor} rejected this exact action: {} Do not perform it or a broader version of it.",
            decision.question.trim()
        ),
        _ => return None,
    };
    Some(format!(
        "[Clarp decision resolved]\nDecision ID: {}\nArtifact ID: {}\nResolved by: {resolved_by}\n\
         Question: {}\nContext: {}\nReference: {}\nPayload: {}\n{outcome}",
        decision.decision_id,
        artifact.artifact_id,
        decision.question,
        decision.context,
        artifact.reference_id,
        Value::Object(artifact.payload.clone()),
    ))
}

/// Whether a value says yes, as the callers of this module give it.
pub fn says_yes(value: &Value) -> bool {
    is_true(value)
}
