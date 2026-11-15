//! Forms that take answers.
//!
//! A form is a page its agent made, with a schema that says what an answer to it is. It is
//! made once and never changed: what a user answered, they answered to this page. The user
//! fills it in and sends the answers under an id the app chose, so that sending them twice
//! is sending them once. The answers are kept on the form as a submission, and told to the
//! form's agent as what its user said.
//!
//! The rules and their order are the Python Host's (`server/lib/html_forms.py`, `submit`).

use serde::{Deserialize, Serialize};
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};

use super::reports::Page;
use super::{Artifact, ArtifactBook, Ctx, Made, Maker, New, Opening, Refused, answers, refuse};
use crate::python::{sorted_ascii, text};

/// What a submission to a report is refused with. The Python Host answers this one as a
/// request that was wrong (400), the others as one that cannot be now (409).
pub const READ_ONLY: &str = "read-only report; it does not accept answers";

/// The most a user's answers may weigh, written out.
const ANSWERS_MAX: usize = 131_072;

/// Answers a user gave to a form.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Submission {
    pub submission_id: String,
    /// The version of the form they were given to.
    pub version: String,
    /// What only these answers to this form at this version have: answers sent again are
    /// known by it.
    pub payload_hash: String,
    /// Whether the user wants the agent's reply spoken.
    pub synthesize_audio: bool,
    pub created_at: i64,
    /// What the form's agent is told, while it still waits to be told. Once it is told it
    /// is in the conversation, and is not kept twice.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub prompt: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub delivered_at: Option<i64>,
}

impl Submission {
    /// `pending` until its agent is told, `delivered` after.
    pub fn status(&self) -> &'static str {
        if self.delivered_at.is_some() {
            "delivered"
        } else {
            "pending"
        }
    }

    /// What the one who sent the answers is answered.
    pub fn receipt(&self, artifact_id: &str) -> Value {
        json!({
            "submission_id": self.submission_id,
            "artifact_id": artifact_id,
            "version": self.version,
            "accepted": true,
            "delivery_status": self.status(),
        })
    }

    /// Answers that come again under an id that was used: the same answers are answered as
    /// they were, anything else is another submission under a used id.
    pub fn again(&self, artifact_id: &str, sent: &Sent) -> Result<Value, Refused> {
        if self.payload_hash != sent.payload_hash {
            return refuse("submission ID already used for different answers");
        }
        if sent
            .synthesize_audio
            .is_some_and(|audio| audio != self.synthesize_audio)
        {
            return refuse("submission ID already used for a different audio preference");
        }
        Ok(self.receipt(artifact_id))
    }
}

/// What a user sent, read: the half of a submission that asks nothing of any form.
#[derive(Debug, Clone, PartialEq)]
pub struct Sent {
    pub submission_id: String,
    /// The version of the form the answers are for, as it was sent.
    pub version: Value,
    answers: Value,
    /// The answers in the one form they are kept, weighed and shown to the agent in.
    pub encoded: String,
    pub payload_hash: String,
    /// Whether the reply is to be spoken, when the sender said.
    pub synthesize_audio: Option<bool>,
}

/// Reads what a user sent to the form `artifact_id`.
pub fn sent(artifact_id: &str, data: &Map<String, Value>) -> Result<Sent, Refused> {
    let in_an_id = |byte: u8| byte.is_ascii_alphanumeric() || byte == b'-';
    let submission_id = data.get("submission_id").and_then(Value::as_str);
    let Some(submission_id) =
        submission_id.filter(|id| (16..=80).contains(&id.len()) && id.bytes().all(in_an_id))
    else {
        return refuse("invalid submission_id");
    };
    let Some(answers) = data.get("answers").filter(|answers| answers.is_object()) else {
        return refuse("answers must be an object");
    };
    let encoded = sorted_ascii(answers);
    if encoded.len() > ANSWERS_MAX {
        return refuse("answers exceed 128 KiB");
    }
    let synthesize_audio = match data.get("synthesize_audio") {
        None => None,
        Some(Value::Bool(audio)) => Some(*audio),
        Some(_) => return refuse("synthesize_audio must be a boolean"),
    };
    let version = data.get("version").cloned().unwrap_or(Value::Null);
    let weighed = sorted_ascii(&json!([artifact_id, version, encoded]));
    let payload_hash = Sha256::digest(weighed.as_bytes())
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect();
    Ok(Sent {
        submission_id: submission_id.to_string(),
        version,
        answers: answers.clone(),
        encoded,
        payload_hash,
        synthesize_audio,
    })
}

/// A form is made: its page is kept beside the log, like a report's, and the artifact
/// names it. Nothing of it is ever changed.
pub(crate) fn made(
    book: &ArtifactBook,
    maker: Maker<'_>,
    new: &New,
    opening: Opening,
    mut payload: Map<String, Value>,
    ctx: &mut Ctx<'_>,
) -> Result<Made, Refused> {
    let content = match payload.remove("content") {
        Some(Value::String(content)) => content,
        _ => String::new(),
    };
    let mut artifact = book.placed(maker, new, opening, payload, ctx)?;
    artifact.page = Some(Page::of(&content));
    Ok(Made {
        artifact,
        page: Some(content),
        changed: true,
    })
}

impl ArtifactBook {
    /// The submission under this id, with the form it was made to, wherever in this book.
    pub fn submission(&self, submission_id: &str) -> Option<(&Artifact, &Submission)> {
        self.artifacts().find_map(|artifact| {
            let found = artifact
                .submissions
                .iter()
                .find(|submission| submission.submission_id == submission_id)?;
            Some((artifact, found))
        })
    }

    /// Takes answers nobody sent before to the form `artifact_id`: the form as it is with
    /// them, and what the sender is answered.
    pub fn submitted(
        &self,
        artifact_id: &str,
        sent: &Sent,
        now: i64,
    ) -> Result<(Artifact, Value), Refused> {
        let Some(form) = self
            .get(artifact_id)
            .filter(|form| form.kind == "html_form")
        else {
            return refuse("HTML form not found");
        };
        if form.payload.get("read_only") == Some(&Value::Bool(true)) {
            return refuse(READ_ONLY);
        }
        // Putting a form away only hides it in the inbox; it does not close it.
        if !matches!(form.status.as_str(), "ready" | "active") {
            return refuse("HTML form no longer accepts answers");
        }
        let version = form.payload.get("version").and_then(Value::as_str);
        let Some(version) = version.filter(|version| sent.version.as_str() == Some(*version))
        else {
            return refuse("form version mismatch; reopen the form");
        };
        let schema = form.payload.get("answer_schema").unwrap_or(&Value::Null);
        // A form that came from another Host may say what this Host does not read: its
        // answers cannot be judged here.
        answers::check(schema)?;
        if let Some(why) = answers::unfit(schema, &sent.answers) {
            return refuse(format!("answers do not match the form schema: {why}"));
        }
        let prompt = format!(
            "The user submitted answers to HTML form {}.\nArtifact ID: {artifact_id}\n\
             Version: {version}\nSubmission ID: {}\nThese are user preferences for review, \
             not approval to perform protected actions. Interpret the answers in the context \
             of the originating plan.\nAnswers:\n{}",
            text(&form.title),
            sent.submission_id,
            sent.encoded
        );
        let submission = Submission {
            submission_id: sent.submission_id.clone(),
            version: version.to_string(),
            payload_hash: sent.payload_hash.clone(),
            synthesize_audio: sent.synthesize_audio.unwrap_or(true),
            created_at: now,
            prompt: Some(prompt),
            delivered_at: None,
        };
        let receipt = submission.receipt(artifact_id);
        let mut form = form.clone();
        form.submissions.push(submission);
        Ok((form, receipt))
    }

    /// The submissions whose agent was not told yet, the oldest first: each with the id of
    /// its form.
    pub fn untold(&self) -> Vec<(String, String)> {
        let mut waiting: Vec<(i64, String, String)> = self
            .artifacts()
            .flat_map(|artifact| {
                artifact
                    .submissions
                    .iter()
                    .filter(|submission| submission.delivered_at.is_none())
                    .map(|submission| {
                        (
                            submission.created_at,
                            artifact.artifact_id.clone(),
                            submission.submission_id.clone(),
                        )
                    })
            })
            .collect();
        waiting.sort();
        waiting
            .into_iter()
            .map(|(_, artifact_id, submission_id)| (artifact_id, submission_id))
            .collect()
    }

    /// A submission is told to its agent: the form as it is after, and what the agent is
    /// told. `None` when it is not there or was told before.
    pub fn told(
        &self,
        artifact_id: &str,
        submission_id: &str,
        now: i64,
    ) -> Option<(Artifact, String, bool)> {
        let mut form = self.any(artifact_id)?.clone();
        let submission = form
            .submissions
            .iter_mut()
            .find(|submission| submission.submission_id == submission_id)?;
        if submission.delivered_at.is_some() {
            return None;
        }
        let prompt = submission.prompt.take()?;
        submission.delivered_at = Some(now);
        let audio = submission.synthesize_audio;
        Some((form, prompt, audio))
    }
}
