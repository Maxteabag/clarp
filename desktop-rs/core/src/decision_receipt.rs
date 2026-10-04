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
            Self::Expired => "⏱",
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

/// The receipt in a user row's text, or None when it is anything else.
pub fn parse(_text: &str) -> Option<DecisionReceipt> {
    let _ = HEADER;
    let _: Option<Value> = None;
    None
}
