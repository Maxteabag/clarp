//! A resolved decision as the chat shows it. When the user answers a
//! decision or question, the Host wakes the agent with a user-role prompt
//! (`artifacts.format_delivery_prompt`): a `[Clarp decision resolved]`
//! header, `Decision ID:`/`Artifact ID:`/`Question:`/`Context:`/
//! `Reference:`/`Payload:` lines and one outcome sentence. The chat shows a
//! receipt card instead (iOS `TranscriptTurn.decisionReceipt`); the text
//! stays the row's own, for the agent and for copying.

use serde_json::Value;

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
            Self::Approved => "Approved".into(),
            Self::Declined => "Declined".into(),
            Self::Answered(answer) => answer.clone(),
            Self::Discarded => "Discarded".into(),
            Self::Expired => "Expired".into(),
            Self::Resolved => "Resolved".into(),
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

const ANSWERED: &str = "The user answered this clarification: ";
const ANSWER_END: &str = ". Continue using this answer.";

/// The receipt in a user row's text, or None when it is anything else.
/// Only the Host's whole envelope is one: its header first, then the
/// artifact and question lines; the outcome sentence is the last line
/// (the Host writes the answer as JSON, so it holds no line break).
pub fn parse(text: &str) -> Option<DecisionReceipt> {
    let rest = text.strip_prefix(HEADER)?;
    let (decision_id, rest) = rest.split_once('\n')?;
    let rest = rest.strip_prefix("Artifact ID: ")?;
    let (artifact_id, rest) = rest.split_once('\n')?;
    let rest = rest.strip_prefix("Question: ")?;
    let (question, rest) = rest.split_once("\nContext: ")?;
    // Context may run over lines; Payload is the last line before the outcome.
    let (_, outcome) = rest.rsplit_once('\n')?;
    if !rest.contains("\nPayload: ") && !rest.starts_with("Payload: ") {
        return None;
    }
    let outcome = if outcome.starts_with("The user chose: accepted.") {
        Outcome::Approved
    } else if outcome.starts_with("The user chose: rejected.") {
        Outcome::Declined
    } else if outcome.starts_with("The user discarded this request.") {
        Outcome::Discarded
    } else if outcome.starts_with("The request expired") {
        Outcome::Expired
    } else if let Some(answer) = outcome.strip_prefix(ANSWERED) {
        let answer = answer.rsplit_once(ANSWER_END).map_or(answer, |(json, _)| json);
        Outcome::Answered(answer_words(answer).unwrap_or_else(|| "Answered".into()))
    } else {
        Outcome::Resolved
    };
    Some(DecisionReceipt {
        decision_id: decision_id.to_owned(),
        artifact_id: artifact_id.to_owned(),
        question: question.trim().to_owned(),
        outcome,
    })
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
        return format!("{} · {}", receipt.question, receipt.outcome.label());
    }
    let (speaker, rest) = text.strip_prefix("You: ").map_or(("", text), |rest| ("You: ", rest));
    if rest.starts_with("[Clarp decision resolved]") {
        return format!("{speaker}Resolved a decision");
    }
    crate::text::plain_preview_text(&crate::text::cleaned_display_text(text, false))
}
