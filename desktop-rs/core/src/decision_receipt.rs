//! A resolved decision as the chat shows it. When the user answers a
//! decision or question, the Host wakes the agent with a user-role prompt
//! (`artifacts.format_delivery_prompt`): a `[Clarp decision resolved]`
//! header, `Decision ID:`/`Artifact ID:` lines, optional header lines such as
//! `Resolved by:`, then `Question:`/`Context:`/`Reference:`/`Payload:` and the
//! outcome sentence. The chat shows a receipt card instead (iOS
//! `TranscriptTurn.decisionReceipt`); the text stays the row's own, for the
//! agent and for copying.

use serde_json::Value;
use crate::tr;

// i18n-screen: chat

const HEADER: &str = "[Clarp decision resolved]\nDecision ID: ";

/// How the user resolved it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Outcome {
    Approved,
    Declined,
    /// A single choice's label or a typed answer.
    Answered(String),
    Discarded,
    Expired,
    /// An outcome this client does not know yet.
    Resolved,
}

impl Outcome {
    /// The chip's words: the status reads without its colour.
    pub fn label(&self) -> String {
        match self {
            // i18n: chat, chip on a resolved decision card: the user approved it (past participle)
            Self::Approved => tr!("preview.decision.approved" => "Approved"),
            // i18n: chat, chip on a resolved decision card: the user declined it (past participle)
            Self::Declined => tr!("preview.decision.declined" => "Declined"),
            // i18n: chat, chip on an answered question card when the answer has no words of its own (past participle)
            Self::Answered(answer) if answer.is_empty() => tr!("Answered"),
            Self::Answered(answer) => answer.clone(),
            // i18n: chat, chip on a decision card the user discarded (past participle)
            Self::Discarded => tr!("preview.decision.discarded" => "Discarded"),
            // i18n: chat, chip on a decision card that ran out of time (past participle)
            Self::Expired => tr!("preview.decision.expired" => "Expired"),
            // i18n: chat, chip on a decision card resolved in a way this app does not name (past participle)
            Self::Resolved => tr!("preview.decision.resolved" => "Resolved"),
        }
    }

    /// The theme role the chip takes: success, danger, answer or muted.
    pub fn tone(&self) -> &'static str {
        match self {
            Self::Approved => "success",
            Self::Declined => "danger",
            Self::Answered(_) => "answer",
            Self::Discarded | Self::Expired | Self::Resolved => "muted",
        }
    }

    /// The mark before the chip's words.
    pub fn mark(&self) -> &'static str {
        match self {
            Self::Approved => "✓",
            Self::Declined => "✕",
            Self::Answered(_) => "↳",
            Self::Discarded => "⊘",
            Self::Expired => "◷",
            Self::Resolved => "•",
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DecisionReceipt {
    pub decision_id: String,
    pub artifact_id: String,
    pub question: String,
    pub outcome: Outcome,
}

const ANSWERED: &str = "answered this clarification: ";
const ANSWER_END: &str = ". Continue using this answer.";
/// Every outcome names its actor first: "The user", or "The user, through
/// agent <id>" when an agent resolved it on the user's behalf.
const ACTOR: &str = "The user";
const THROUGH_AGENT: &str = ", through agent ";

/// The receipt in a user row's text, or None when it is anything else.
/// Only the Host's whole envelope is one: its header first, then the
/// decision and artifact lines, any header lines (`Resolved by:`), the
/// question, context and reference, and the one-line JSON payload; the rest
/// is the outcome (an approval repeats the question, which may run over lines).
pub fn parse(text: &str) -> Option<DecisionReceipt> {
    let rest = text.strip_prefix(HEADER)?;
    let (decision_id, rest) = rest.split_once('\n')?;
    let rest = rest.strip_prefix("Artifact ID: ")?;
    let (artifact_id, mut rest) = rest.split_once('\n')?;
    // Header lines the Host adds before the question ("Resolved by: user").
    while !rest.starts_with("Question: ") {
        let (line, next) = rest.split_once('\n')?;
        let (key, _) = line.split_once(": ")?;
        if key.is_empty() || !key.chars().all(|c| c.is_ascii_alphabetic() || c == ' ') {
            return None;
        }
        rest = next;
    }
    let rest = rest.strip_prefix("Question: ")?;
    let (question, rest) = rest.split_once("\nContext: ")?;
    // Context may run over lines; the payload is one line of JSON.
    let (_, rest) = rest.split_once("\nPayload: ")?;
    let (_, outcome) = rest.split_once('\n')?;
    Some(DecisionReceipt {
        decision_id: decision_id.to_owned(),
        artifact_id: artifact_id.to_owned(),
        question: question.trim().to_owned(),
        outcome: outcome_of(outcome),
    })
}

/// The outcome sentence's meaning, in the Host's current wording and the
/// earlier one ("The user chose: accepted."), so older chats still read.
fn outcome_of(sentence: &str) -> Outcome {
    if sentence.starts_with("The user chose: accepted.") {
        return Outcome::Approved;
    }
    if sentence.starts_with("The user chose: rejected.") {
        return Outcome::Declined;
    }
    if sentence.starts_with("The user discarded this request.") {
        return Outcome::Discarded;
    }
    if sentence.starts_with("The request expired") {
        return Outcome::Expired;
    }
    let Some(after_actor) = sentence.strip_prefix(ACTOR) else { return Outcome::Resolved };
    // "The user, through agent <id> approved …": the agent's id is one word.
    let said = match after_actor.strip_prefix(THROUGH_AGENT) {
        Some(through) => through.split_once(' ').map_or("", |(_, said)| said),
        None => after_actor.strip_prefix(' ').unwrap_or(""),
    };
    if said.starts_with("approved this exact action:") {
        Outcome::Approved
    } else if said.starts_with("rejected this exact action:") {
        Outcome::Declined
    } else if let Some(answer) = said.strip_prefix(ANSWERED) {
        let answer = answer.rsplit_once(ANSWER_END).map_or(answer, |(json, _)| json);
        Outcome::Answered(answer_words(answer).unwrap_or_default())
    } else {
        Outcome::Resolved
    }
}

/// A choice's label or the typed text (`{"option_id", "label"}` or
/// `{"text"}`).
fn answer_words(json: &str) -> Option<String> {
    let value: Value = serde_json::from_str(json).ok()?;
    ["label", "text", "option_id"]
        .iter()
        .find_map(|key| value.get(*key).and_then(Value::as_str).map(str::trim).filter(|s| !s.is_empty()))
        .map(str::to_owned)
}

/// One line for a preview (sidebar, overview, notification): voice markup
/// removed, a resolved decision read as its question and outcome, or, cut
/// short by the Host, as "Resolved a decision".
pub fn preview_line(text: &str) -> String {
    if let Some(receipt) = parse(text) {
        // i18n: chat list preview of a resolved decision; {0} is the decision's question, {1} its outcome ("Approved")
        return tr!("preview.decision" => "{0} · {1}", receipt.question, receipt.outcome.label());
    }
    if let Some(rest) = text.strip_prefix("You: ")
        && rest.starts_with("[Clarp decision resolved]")
    {
        // i18n: chat list preview of a decision the user resolved, cut short by the Host
        let resolved = tr!("Resolved a decision");
        // i18n: chat list preview of the user's own last message; {} is that message's preview
        return tr!("preview.own" => "You: {}", resolved);
    }
    if text.starts_with("[Clarp decision resolved]") {
        // i18n: chat list preview of a decision the user resolved, cut short by the Host
        return tr!("Resolved a decision");
    }
    crate::text::plain_preview_text(&crate::text::cleaned_display_text(text, false))
}
