//! Evidence for a label check. Looking at the fleet borrows its published in-memory views.
//! Only after selection may the checker ask an actor to read its conversation.
use crate::{agent::Unavailable, fleet::Directory, wire};
use clarp_actor::conversation::Which;
use clarp_kernel::helpers;
use clarp_kernel::janitors::{
    label_audit::Evidence,
    label_evidence::{Candidate, evidence, selected},
};
use serde_json::{Value, json};
use std::collections::HashMap;

#[derive(Default)]
struct Children {
    sessions: Vec<String>,
    working: Vec<helpers::Working>,
    details: Vec<Value>,
}

fn roster(directory: &Directory) -> Vec<Candidate> {
    let mut ordered: Vec<_> = directory.members.iter().collect();
    ordered.sort_by_key(|member| member.record.created_at_ms);
    let mut children: HashMap<&str, Children> = HashMap::new();
    for member in &ordered {
        let record = &member.record;
        let Some(parent) = record.parent_agent_id.as_deref() else {
            continue;
        };
        let family = children.entry(parent).or_default();
        if record.is_helper() {
            family.sessions.push(record.session.clone());
        }
        if record.helper_state.as_deref() != Some("running") {
            continue;
        }
        let view = member.agent.view();
        let activity = wire::activity(record, &view.status);
        let text = view.status.label.trim();
        let text = if text.is_empty() {
            activity["summary"].as_str().unwrap_or_default().trim()
        } else {
            text
        };
        let state = wire::state(record, &view.status, view.backend, view.ended.as_ref());
        let bucket = if state["detail"]["account_recovery"].is_object() {
            "waiting"
        } else {
            helpers::bucket(state["kind"].as_str().unwrap_or_default())
        };
        family.working.push(helpers::Working {
            session: record.session.clone(),
            text: text.to_owned(),
            bucket,
        });
        family
            .details
            .push(json!({"session":record.session,"bucket":bucket,"text":text,"ts":state["ts"]}));
    }
    let mut facts = Vec::new();
    for member in ordered {
        let record = &member.record;
        if record.is_janitor || record.archived_at_ms.is_some() {
            continue;
        }
        let view = member.agent.view();
        let family = children
            .remove(record.agent_id.as_str())
            .unwrap_or_default();
        let names: Vec<_> = family.sessions.iter().map(String::as_str).collect();
        let mut jobs: Vec<_> = view
            .jobs
            .iter()
            .filter(|j| j.status.is_active() && wire::mirrored_helper(j, &names).is_none())
            .collect();
        jobs.sort_by_key(|j| j.started_at_ms);
        let processes: Vec<_> = jobs
            .iter()
            .map(|j| helpers::Process {
                title: j.title.clone(),
                progress: j.progress_text.clone(),
                active_at_ms: j.progress_at_ms.unwrap_or(j.started_at_ms),
            })
            .collect();
        let state = wire::state(record, &view.status, view.backend, view.ended.as_ref());
        let state_at = wire::state_since(record, &view.status);
        let ended_at = view.status.last.as_ref().map(|l| l.ended_at_ms);
        let idle_since = view
            .jobs
            .iter()
            .map(|j| j.updated_at_ms)
            .chain(ended_at)
            .fold(state_at.max(record.created_at_ms), i64::max);
        let detail = &state["detail"];
        let detail = detail["message"]
            .as_str()
            .filter(|s| !s.is_empty())
            .or_else(|| detail["summary"].as_str())
            .unwrap_or_default();
        let label = view.status.label.trim();
        facts.push(Candidate{
            agent_id:record.agent_id.clone(),name:if record.persona.is_empty(){record.session.clone()}else{record.persona.clone()},
            excluded:false,busy:view.status.live.is_some() || !view.status.waiting.is_empty(),idle_since,
            label:if label.is_empty(){helpers::work_summary(&family.working,&processes)}else{label.to_owned()},
            state:state["kind"].as_str().unwrap_or_default().into(),detail:detail.into(),state_at,ended_at,
            jobs:jobs.into_iter().map(|j|json!({"title":j.title,"kind":j.kind,"progress_text":j.progress_text,"active_at":j.progress_at_ms.unwrap_or(j.started_at_ms),"heartbeat_at":j.heartbeat_at_ms.unwrap_or(j.updated_at_ms)})).collect(),helpers:family.details,
        });
    }
    facts
}

async fn project<F: AsyncFnMut(String) -> Result<Vec<Value>, Unavailable>>(
    facts: Vec<Candidate>,
    now: i64,
    mut history: F,
) -> Result<Vec<Evidence>, Unavailable> {
    let mut result = Vec::new();
    for c in selected(facts, now) {
        let messages = history(c.agent_id.clone()).await?;
        result.push(evidence(c, now, &messages));
    }
    Ok(result)
}

/// A scan reads at most three current-conversation entries for each of the selected twelve.
pub async fn candidates(directory: &Directory, now: i64) -> Result<Vec<Evidence>, Unavailable> {
    project(roster(directory), now, async |id: String| {
        let member = directory
            .named(&id)
            .ok_or_else(|| Unavailable("label check target disappeared".into()))?;
        let page = member.agent.tail(3, Which::All).await?;
        Ok(page.entries.iter().map(wire::row).collect())
    })
    .await
}

#[cfg(test)]
mod tests {
    use super::*;
    #[tokio::test]
    async fn history_is_opened_only_for_the_twelve_oldest_labels() {
        let facts = (0..13)
            .rev()
            .map(|i| Candidate {
                agent_id: i.to_string(),
                name: "Example".into(),
                busy: false,
                excluded: false,
                idle_since: i,
                label: "Review".into(),
                state: "done".into(),
                detail: String::new(),
                state_at: 0,
                ended_at: None,
                jobs: vec![],
                helpers: vec![],
            })
            .collect();
        let mut opened = Vec::new();
        let result = project(facts, 1_000_000, async |id: String| {
            opened.push(id);
            Ok(vec![json!({"role":"user","text":"a sample"})])
        })
        .await;
        assert_eq!(opened, (0..12).map(|i| i.to_string()).collect::<Vec<_>>());
        assert!(result.is_ok_and(|entries| {
            entries.len() == 12
                && entries
                    .iter()
                    .all(|e| e.packet["recent_messages"][0]["text"] == "a sample")
        }));
    }
}
