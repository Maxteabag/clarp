//! Artifacts that come with an agent that moves here.
//!
//! An agent's library moves with it: each artifact as the Host it was on showed it
//! (`GET /artifacts`), and for a report what it was, version by version
//! (`GET /artifacts/<id>/revisions`). Nothing of it is made here and nothing is judged
//! anew. An artifact keeps its id, its times and its state; a request to the user is as
//! open or as answered as it was, and an answer that still waited to be told to its agent
//! is told here; a report's pages are kept beside the log like any other page.

use serde_json::Value;

use super::decisions::{Decision, Delivery};
use super::reports::{self, Page, Revision};
use super::{Artifact, ArtifactBook, Maker, Refused, STATUSES, TYPES, refuse, valid_id};

/// An artifact that arrived: as it is kept, and the pages to keep beside the log before it
/// is written.
#[derive(Debug, Clone, PartialEq)]
pub struct Arrived {
    pub artifact: Artifact,
    pub pages: Vec<String>,
}

fn text(shown: &Value, name: &str) -> String {
    let said = shown.get(name).and_then(Value::as_str);
    said.unwrap_or("").to_string()
}

fn whole(shown: &Value, name: &str) -> Option<i64> {
    shown.get(name).and_then(Value::as_i64)
}

/// A yes or no, as a truth value or as the number a table keeps it as.
fn truth(shown: &Value, name: &str) -> bool {
    match shown.get(name) {
        Some(Value::Bool(truth)) => *truth,
        Some(Value::Number(number)) => number.as_i64() != Some(0),
        _ => false,
    }
}

impl Decision {
    /// A request to the user as another Host showed it on its artifact: what was asked,
    /// how it stands, what was answered.
    fn taken_over(shown: &Value) -> Result<Self, Refused> {
        let decision_id = text(shown, "decision_id");
        let status = text(shown, "status");
        if decision_id.is_empty() || status.is_empty() {
            return refuse("a request to the user came without what it asks");
        }
        let resolved_choice = text(shown, "resolved_choice");
        let resolved_at = whole(shown, "resolved_at");
        let answer = shown.get("answer").cloned().unwrap_or(Value::Null);
        // How it ended, when its agent was not told yet: it is told here.
        let delivery = truth(shown, "delivery_pending").then(|| Delivery {
            status: "pending".to_string(),
            choice: resolved_choice.clone(),
            answer: answer.clone(),
            created_at: resolved_at.unwrap_or(0),
            delivered_at: None,
        });
        let options = shown.get("options").and_then(Value::as_array);
        let recommended = shown.get("recommended_option_id").and_then(Value::as_str);
        Ok(Self {
            decision_id,
            question: text(shown, "question"),
            context: text(shown, "context"),
            yes_label: text(shown, "yes_label"),
            no_label: text(shown, "no_label"),
            status,
            resolved_choice,
            resolved_at,
            resolved_by: text(shown, "resolved_by"),
            revision: whole(shown, "revision").unwrap_or(1),
            expires_at: whole(shown, "expires_at"),
            response_type: text(shown, "response_type"),
            options: options.cloned().unwrap_or_default(),
            allow_custom_text: truth(shown, "allow_custom_text"),
            recommended_option_id: recommended.map(str::to_string),
            blocks_progress: truth(shown, "blocks_progress"),
            priority_reason: text(shown, "priority_reason"),
            urgency: text(shown, "urgency"),
            response_effort: text(shown, "response_effort"),
            deadline_at: whole(shown, "deadline_at"),
            answer,
            delivery,
        })
    }
}

impl ArtifactBook {
    /// An artifact that comes with an agent that moves here, as the Host it was on showed
    /// it, with the versions a report had. `taken` says whether an artifact has this id
    /// anywhere on this Host already.
    pub fn taken_over(
        &self,
        maker: Maker<'_>,
        shown: &Value,
        revisions: &[Value],
        taken: &dyn Fn(&str) -> bool,
    ) -> Result<Arrived, Refused> {
        let artifact_id = text(shown, "artifact_id");
        if !valid_id(&artifact_id) {
            return refuse("invalid artifact id");
        }
        if text(shown, "agent_id") != maker.agent_id {
            return refuse("the artifact is not this agent's");
        }
        if taken(&artifact_id) {
            return refuse("an artifact with this id is here already");
        }
        let (kind, status) = (text(shown, "type"), text(shown, "status"));
        if !TYPES.contains(&kind.as_str()) {
            return refuse("unsupported artifact type");
        }
        if !STATUSES.contains(&status.as_str()) {
            return refuse("unsupported artifact status");
        }
        let payload = shown.get("payload").and_then(Value::as_object);
        let mut payload = payload.cloned().unwrap_or_default();
        // A page is kept beside the log, and the artifact names it: its own, and every one
        // it had.
        let (mut page, mut pages, mut was) = (None, Vec::new(), Vec::new());
        if kind == "html_form" {
            if let Some(Value::String(content)) = payload.remove("content") {
                page = Some(Page::of(&content));
                pages.push(content);
            }
            for revision in revisions {
                let kept = revision.get("payload").and_then(Value::as_object);
                let Some(mut kept) = kept.cloned() else {
                    continue;
                };
                let Some(Value::String(content)) = kept.remove("content") else {
                    continue;
                };
                was.push(Revision {
                    version: text(revision, "version"),
                    title: text(revision, "title"),
                    summary: text(revision, "summary"),
                    created_at: whole(revision, "created_at").unwrap_or(0),
                    payload: kept,
                    page: Page::of(&content),
                });
                pages.push(content);
            }
        }
        let decision = match kind.as_str() {
            "decision" | "question" => {
                let asked = shown.get("decision").unwrap_or(&Value::Null);
                Some(Decision::taken_over(asked)?)
            }
            _ => None,
        };
        let mut artifact = Artifact {
            artifact_id,
            agent_id: maker.agent_id.to_string(),
            session: maker.session.to_string(),
            kind,
            title: text(shown, "title"),
            summary: text(shown, "summary"),
            status,
            reference_id: text(shown, "reference_id"),
            payload,
            created_at: whole(shown, "created_at").unwrap_or(0),
            updated_at: whole(shown, "updated_at").unwrap_or(0),
            completed_at: whole(shown, "completed_at"),
            deleted_at: None,
            archived_at: whole(shown, "archived_at"),
            pinned_at: whole(shown, "pinned_at"),
            decision,
            page,
            revisions: was,
        };
        // A report that came without what it was is at least what it is.
        if reports::is_report(&artifact) {
            artifact.remember();
        }
        Ok(Arrived { artifact, pages })
    }
}
