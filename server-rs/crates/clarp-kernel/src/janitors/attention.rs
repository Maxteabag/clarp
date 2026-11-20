//! A janitor's failure reaches its owner: the one thing of a quiet agent that does.
//!
//! When a janitor's runs of its current configuration failed twice in a row since its last
//! verified success, an item is raised for its owner: a document of the janitor, `failed`,
//! that `GET /attention` shows among its `janitor_items` and the Updates badge counts. A further
//! failure updates it; a verified success, the janitor set anew, turned off or put away closes
//! it; the owner archives or discards it, and it is never made again for the same failure. It
//! never pushes, speaks or marks anything unread. The Python Host's `janitor_attention`
//! (`_episode`, `_description`, `_sync`, `pending`), held to it by
//! `tests/janitor_attention_vectors.rs`.
//!
//! The items are a table of the janitors' book, so an item changes in the same write as the run
//! or the configuration that changed it.

use std::sync::LazyLock;

use regex::Regex;
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};

use super::book::{Book, Config, World};
use super::runs::{Run, SCHEDULED_TASK, redact};
use crate::artifacts::Artifact;
use crate::python;
use clarp_speech::markup::strip_hidden_blocks;

/// What such an item's payload says it is.
pub const KIND: &str = "janitor_failure";
/// How the reference of every such item begins.
pub const PREFIX: &str = "janitor-failure:";
/// How the id of every such item begins.
pub const ALERT: &str = "janitor-alert-";
/// The job whose failures are reviews, not runs.
const LABELS: &str = "task-labels";
/// How many failures in a row raise an item.
const RAISED_AT: usize = 2;
const RECOVERED: &str = "Maintenance recovered or its configuration changed.";
const PAUSED: &str = "Maintenance is paused or removed.";
const OVER: [&str; 3] = ["completed", "cancelled", "expired"];

/// Whether the item with this id is such an item.
pub fn is_alert(artifact_id: &str) -> bool {
    artifact_id.starts_with(ALERT)
}

fn failed(run: &Run) -> bool {
    run.status == "failed" || run.outcome == "error"
}

fn finished(run: &Run) -> bool {
    matches!(run.status.as_str(), "completed" | "failed" | "cancelled")
}

/// The order of runs that are over, the newest last: when each ended, then when it was made.
fn order(run: &Run) -> (i64, i64, &str) {
    (
        run.finished_at.unwrap_or(0),
        run.created_at,
        run.run_id.as_str(),
    )
}

/// Whether a run is a success that closes a failure: a scheduled task's run that ended well, or
/// a run of a job that answers requests whose answer was taken as it was asked for (made for the
/// same job, by a model called for it, with something said of it).
fn verified(template: &str, run: &Run) -> bool {
    if run.status != "completed" || run.outcome != "completed" || run.finished_at.is_none() {
        return false;
    }
    if template == SCHEDULED_TASK {
        return true;
    }
    let Some(result) = run.demand_result.as_ref() else {
        return false;
    };
    let frozen = &run.configuration;
    super::demand_roles().any(|role| role == template)
        && frozen.get("executor").and_then(Value::as_str) == Some("ephemeral")
        && frozen.get("template_id").and_then(Value::as_str) == Some(template)
        && result
            .get("summary")
            .and_then(Value::as_str)
            .is_some_and(|summary| !python::strip(summary).is_empty())
}

/// One janitor's failure as it stands: the reference of its episode (from its last verified
/// success, or from the first run of its configuration), the run that failed last where the
/// last run that ended failed, and how many failed in a row.
pub fn episode<'a>(
    agent_id: &str,
    config: &Config,
    runs: impl Iterator<Item = &'a Run>,
) -> (String, Option<&'a Run>, usize) {
    let mut own: Vec<&Run> = runs
        .filter(|run| run.agent_id == agent_id && run.generation == config.generation)
        .collect();
    own.sort_by(|one, other| order(one).cmp(&order(other)));
    let success = own
        .iter()
        .rev()
        .find(|run| verified(&config.template_id, run));
    let boundary = success.map_or("initial", |run| run.run_id.as_str());
    let reference = format!("{PREFIX}{agent_id}:{}:{boundary}", config.generation);
    let over: Vec<&Run> = own.into_iter().filter(|run| finished(run)).collect();
    let Some(latest) = over.last().copied().filter(|run| failed(run)) else {
        return (reference, None, 0);
    };
    // A run that ended without failing ends the streak, a success or not.
    let since = over
        .iter()
        .rposition(|run| !failed(run))
        .map_or(0, |at| at + 1);
    (reference, Some(latest), over.len() - since)
}

/// The item's id for an episode.
pub fn alert_id(reference: &str) -> String {
    let digest = Sha256::digest(reference.as_bytes());
    let hex: String = digest.iter().map(|byte| format!("{byte:02x}")).collect();
    format!("{ALERT}{}", &hex[..40])
}

/// What the item says: whose janitor, after how many failures, and the last error, cleaned of
/// secrets and of what a model thought.
pub fn description(
    name: &str,
    template: &str,
    latest: &Run,
    count: usize,
) -> (String, String, Map<String, Value>) {
    static THOUGHT: LazyLock<Option<Regex>> = LazyLock::new(|| {
        Regex::new(r"(?is)<(?:think|analysis)\b[^>]*>.*?(?:</(?:think|analysis)>|\z)").ok()
    });
    let name = python::first(&strip_hidden_blocks(name), 80).to_string();
    let labels = template == LABELS;
    let missing = if labels {
        "Maintenance ended without all review receipts."
    } else {
        "Maintenance ended without a valid completion receipt."
    };
    let error = if latest.error.is_empty() {
        missing
    } else {
        latest.error.as_str()
    };
    let mut diagnostic = strip_hidden_blocks(&redact(error));
    if let Some(thought) = THOUGHT.as_ref() {
        diagnostic = thought.replace_all(&diagnostic, "").into_owned();
    }
    let diagnostic = diagnostic.split_whitespace().collect::<Vec<_>>().join(" ");
    let diagnostic = python::first(&diagnostic, 300).to_string();
    let summary = if labels {
        format!(
            "Task labels need attention after {count} failed reviews. Review the configuration or pause maintenance."
        )
    } else {
        let job = super::template(template)
            .ok()
            .and_then(|job| job["name"].as_str())
            .unwrap_or("Maintenance");
        format!(
            "{job} needs attention after {count} failed runs. Review the configuration or pause maintenance."
        )
    };
    let payload = json!({
        "attention_kind": KIND,
        "configuration_generation": latest.generation,
        "latest_run_id": latest.run_id,
        "failure_count": count,
        "content": format!("{summary}\n\nLatest error: {diagnostic}"),
        "diagnostic": diagnostic,
        "suggested_action": "review_configuration",
    });
    let payload = payload.as_object().cloned().unwrap_or_default();
    (format!("{name} needs attention"), summary, payload)
}

/// Whether an item is one of these, still open: a document of the janitor, `failed`, not
/// discarded, that says it is.
fn open(alert: &Artifact, agent_id: &str) -> bool {
    alert.agent_id == agent_id
        && alert.kind == "document"
        && alert.reference_id.starts_with(PREFIX)
        && alert.status == "failed"
        && alert.deleted_at.is_none()
        && alert.payload.get("attention_kind").and_then(Value::as_str) == Some(KIND)
}

/// Whether a janitor's failures are looked at: its job is on, and it is a janitor that was not
/// put away.
fn active(agent_id: &str, config: &Config, world: &dyn World) -> bool {
    let agent = world.agent(agent_id);
    config.enabled && agent.is_some_and(|agent| agent.is_janitor && !agent.archived)
}

impl Book {
    /// Brings every janitor's item in line with its runs and its configuration. Answers how
    /// many items were made or changed; one that is as it should be is not written again.
    pub fn reconcile_attention(&mut self, world: &dyn World, now_ms: i64) -> usize {
        let mut changed = 0;
        let configs: Vec<(String, Config)> = self.configs.clone().into_iter().collect();
        for (agent_id, config) in configs {
            changed += self.sync_attention(&agent_id, &config, world, now_ms);
        }
        changed
    }

    fn sync_attention(
        &mut self,
        agent_id: &str,
        config: &Config,
        world: &dyn World,
        now_ms: i64,
    ) -> usize {
        let active = active(agent_id, config, world);
        let (reference, latest, count) = if active {
            let (reference, latest, count) = episode(agent_id, config, self.runs.values());
            (reference, latest.cloned(), count)
        } else {
            (String::new(), None, 0)
        };
        let mut changed = 0;
        let closing: Vec<String> = self
            .alerts
            .values()
            .filter(|alert| open(alert, agent_id))
            .filter(|alert| !(active && alert.reference_id == reference))
            .map(|alert| alert.artifact_id.clone())
            .collect();
        for artifact_id in closing {
            if let Some(alert) = self.alerts.get_mut(&artifact_id) {
                let now = now_ms.max(alert.updated_at + 1);
                alert.status = "completed".to_string();
                alert.summary = (if active { RECOVERED } else { PAUSED }).to_string();
                alert.completed_at = Some(now);
                alert.updated_at = now;
                changed += 1;
            }
        }
        let Some(latest) = latest.filter(|_| active && count >= RAISED_AT) else {
            return changed;
        };
        let artifact_id = alert_id(&reference);
        let agent = world.agent(agent_id);
        let (name, session) =
            agent.map_or_else(Default::default, |agent| (agent.name, agent.session));
        let name = if name.is_empty() {
            session.clone()
        } else {
            name
        };
        let (title, summary, payload) = description(&name, &config.template_id, &latest, count);
        match self.alerts.get_mut(&artifact_id) {
            // Put away, discarded or over: never made again for the same failure.
            Some(existing)
                if existing.archived_at.is_some()
                    || existing.deleted_at.is_some()
                    || OVER.contains(&existing.status.as_str()) => {}
            Some(existing) => {
                let theirs = existing.agent_id == agent_id
                    && existing.reference_id == reference
                    && existing.kind == "document"
                    && existing
                        .payload
                        .get("attention_kind")
                        .and_then(Value::as_str)
                        == Some(KIND);
                let same = existing.title == title
                    && existing.summary == summary
                    && existing.status == "failed"
                    && existing.payload == payload;
                if theirs && !same {
                    let now = now_ms.max(existing.updated_at + 1);
                    existing.title = title;
                    existing.summary = summary;
                    existing.status = "failed".to_string();
                    existing.payload = payload;
                    existing.updated_at = now;
                    changed += 1;
                }
            }
            None => {
                let alert = Artifact {
                    artifact_id: artifact_id.clone(),
                    agent_id: agent_id.to_string(),
                    session,
                    kind: "document".to_string(),
                    title,
                    summary,
                    status: "failed".to_string(),
                    reference_id: reference,
                    payload,
                    created_at: now_ms,
                    updated_at: now_ms,
                    completed_at: None,
                    deleted_at: None,
                    archived_at: None,
                    pinned_at: None,
                    decision: None,
                    page: None,
                    revisions: Vec::new(),
                    submissions: Vec::new(),
                    source_trace_id: String::new(),
                };
                self.alerts.insert(artifact_id, alert);
                changed += 1;
            }
        }
        changed
    }

    /// The items that wait for the owner, as `GET /attention` shows them among its
    /// `janitor_items`: of janitors that are on and not put away, of their current
    /// configuration, the most recently changed first; those archived only when asked for.
    pub fn pending_attention(&self, world: &dyn World, include_archived: bool) -> Vec<Value> {
        let mut shown: Vec<&Artifact> = self
            .alerts
            .values()
            .filter(|alert| {
                alert.kind == "document"
                    && alert.status == "failed"
                    && alert.reference_id.starts_with(PREFIX)
                    && alert.deleted_at.is_none()
                    && (include_archived || alert.archived_at.is_none())
            })
            .filter(|alert| {
                let Some(config) = self.configs.get(&alert.agent_id) else {
                    return false;
                };
                let payload = &alert.payload;
                active(&alert.agent_id, config, world)
                    && payload.get("attention_kind").and_then(Value::as_str) == Some(KIND)
                    && payload
                        .get("configuration_generation")
                        .and_then(Value::as_u64)
                        == Some(config.generation)
                    && payload.contains_key("latest_run_id")
                    && payload.contains_key("failure_count")
            })
            .collect();
        shown.sort_by(|one, other| {
            (other.updated_at, &one.artifact_id).cmp(&(one.updated_at, &other.artifact_id))
        });
        shown
            .into_iter()
            .map(|alert| {
                let name = world.agent(&alert.agent_id).map(|agent| agent.name);
                let of = |key: &str| alert.payload.get(key).cloned().unwrap_or(Value::Null);
                json!({
                    "artifact_id": alert.artifact_id, "agent_id": alert.agent_id,
                    "session": alert.session, "agent_name": name, "type": alert.kind,
                    "status": alert.status, "title": alert.title, "summary": alert.summary,
                    "created_at": alert.created_at, "updated_at": alert.updated_at,
                    "archived_at": alert.archived_at, "reference_id": alert.reference_id,
                    "attention_kind": of("attention_kind"),
                    "configuration_generation": of("configuration_generation"),
                    "latest_run_id": of("latest_run_id"), "failure_count": of("failure_count"),
                })
            })
            .collect()
    }
}
