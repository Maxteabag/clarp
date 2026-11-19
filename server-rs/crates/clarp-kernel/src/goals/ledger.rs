//! Attribution and before/after state of a durable goal change.
use super::{Goal, Plan};
use crate::python;
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value, json};

const TRACKED: [&str; 6] = [
    "outcome",
    "limits",
    "criteria",
    "checkpoint",
    "native_session_id",
    "superseded_by",
];

#[derive(Debug, Clone, Copy, Default)]
pub enum Author<'a> {
    #[default]
    Unattributed,
    Owner(Option<&'a str>),
    User,
    Peer(&'a str, bool),
    System,
    Keeper(&'a str),
    Legacy,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
pub struct Event {
    pub event_id: i64,
    pub plan_id: String,
    pub at: i64,
    pub kind: String,
    pub subject: String,
    pub actor_kind: String,
    pub actor_agent_id: String,
    pub delegation_id: String,
    pub basis: String,
    pub source_refs: Vec<String>,
    pub reason: String,
    pub prior: Value,
    pub new: Value,
    pub plan_revision: Option<i64>,
}

pub fn applied_through(previous: i64, requested: i64, owner_high: i64) -> i64 {
    previous.max(requested.min(owner_high))
}

pub fn changes(old: Option<&Plan>, plan: &Plan, author: Author<'_>) -> Vec<(u64, Event)> {
    let Some(goal) = plan.goal.as_ref() else {
        return Vec::new();
    };
    let empty = Goal::default();
    let was = old.and_then(|p| p.goal.as_ref()).unwrap_or(&empty);
    let mut prior = Map::new();
    let mut after = Map::new();
    for key in TRACKED {
        let a = was.fields.get(key).unwrap_or(&Value::Null);
        let b = goal.fields.get(key).unwrap_or(&Value::Null);
        if a != b {
            prior.insert(key.into(), a.clone());
            after.insert(key.into(), b.clone());
        }
    }
    let (kind, id, delegation, verified) = match author {
        Author::Unattributed => ("unattributed", "", "", false),
        Author::Owner(None) => ("owner", plan.agent_id.as_str(), "", false),
        Author::Owner(Some(id)) => (
            if id == plan.agent_id { "owner" } else { "peer" },
            id,
            "",
            true,
        ),
        Author::Peer(id, v) => ("peer", id, "", v),
        Author::System => ("system", "", "", false),
        Author::Keeper(id) => ("system", id, "goal-keeper", false),
        Author::User => ("user", "", "", false),
        Author::Legacy => ("legacy", "", "", false),
    };
    let mut status = old.map_or("active", |p| p.status.as_str());
    let mut events = Vec::new();
    let end = was.history_offset + was.history.len() as u64;
    for (i, entry) in goal.history.iter().enumerate() {
        let index = goal.history_offset + i as u64;
        if index < end {
            continue;
        }
        let action = entry["kind"].as_str().unwrap_or("change");
        let detail = &entry["detail"];
        let system = matches!(
            action,
            "wake_claim"
                | "dispatch_queued"
                | "dispatch_admitted"
                | "dispatch_retry"
                | "execution_observed"
                | "wake_undelivered"
                | "wake_failed"
        ) || (action == "dependency_result" && kind == "unattributed");
        let mut before = Map::new();
        let mut new = json!({"detail":detail});
        if !prior.is_empty() {
            before = std::mem::take(&mut prior);
            new["goal"] = json!(after);
        }
        if !system && matches!(kind, "owner" | "peer" | "unattributed") {
            new["actor_verified"] = json!(verified);
        }
        let to = match action {
            "pause" => Some("paused"),
            "resume" => Some("active"),
            "cancel" | "cancelled" => Some("cancelled"),
            "supersede" => Some("superseded"),
            "block" | "blocked" => Some("blocked"),
            "completed" => Some("completed"),
            _ => None,
        };
        if let Some(to) = to {
            before.insert("status".into(), json!(status));
            new["status"] = json!(to);
            status = to;
        }
        let subject = detail["item_id"]
            .as_str()
            .filter(|s| action == "step" && !s.is_empty())
            .map_or_else(|| "goal".into(), |s| format!("step:{s}"));
        let refs = detail["source_refs"]
            .as_array()
            .into_iter()
            .flatten()
            .filter_map(Value::as_str)
            .filter(|s| !s.trim().is_empty())
            .take(20)
            .map(|s| python::first(s, 200).to_owned())
            .collect();
        events.push((
            index,
            Event {
                event_id: 0,
                plan_id: plan.plan_id.clone(),
                at: entry["at"].as_i64().unwrap_or(plan.updated_at),
                kind: action.into(),
                subject,
                actor_kind: if system { "system" } else { kind }.into(),
                actor_agent_id: if system { "" } else { id }.into(),
                delegation_id: if system { "" } else { delegation }.into(),
                basis: String::new(),
                source_refs: refs,
                reason: python::first(detail["reason"].as_str().unwrap_or(""), 4000).to_owned(),
                prior: if before.is_empty() {
                    Value::Null
                } else {
                    Value::Object(before)
                },
                new,
                plan_revision: entry["revision"].as_i64(),
            },
        ));
    }
    events
}

pub fn entry_marker(plan: &Plan, source: &str, events: &[Event], now: i64) -> Event {
    Event {
        event_id: 0,
        plan_id: plan.plan_id.clone(),
        at: now,
        kind: "plan_imported".into(),
        subject: "goal".into(),
        actor_kind: "system".into(),
        actor_agent_id: String::new(),
        delegation_id: String::new(),
        basis: String::new(),
        source_refs: Vec::new(),
        reason: "Plan entered this agent".into(),
        prior: Value::Null,
        new: json!({"from":source,"plan_id":plan.plan_id,"first_id":events.first().map(|e|e.event_id),"last_id":events.last().map(|e|e.event_id)}),
        plan_revision: Some(plan.revision),
    }
}

/// Older goal documents retain only their last two hundred history entries.
/// Attribute only what the stored document can establish.
pub fn legacy(plan: &Plan) -> Vec<Event> {
    let Some(goal) = &plan.goal else {
        return vec![];
    };
    let mut rows = Vec::new();
    for entry in &goal.history {
        let detail = &entry["detail"];
        let action = entry["kind"].as_str().unwrap_or("change");
        let subject = detail["item_id"]
            .as_str()
            .filter(|s| action == "step" && !s.is_empty())
            .map_or_else(|| "goal".to_owned(), |s| format!("step:{s}"));
        rows.push(Event {
            event_id: 0,
            plan_id: plan.plan_id.clone(),
            at: entry["at"].as_i64().unwrap_or(0),
            kind: action.into(),
            subject,
            actor_kind: "legacy".into(),
            actor_agent_id: String::new(),
            delegation_id: String::new(),
            basis: String::new(),
            source_refs: vec![],
            reason: python::first(detail["reason"].as_str().unwrap_or_default(), 4000).to_owned(),
            prior: Value::Null,
            new: json!({"detail":detail}),
            plan_revision: entry["revision"].as_i64(),
        });
    }
    let mut baseline = entry_marker(plan, "", &[], plan.updated_at);
    baseline.kind = "ledger_started".into();
    baseline.actor_kind = "legacy".into();
    baseline.reason = "Goal ledger began on Host upgrade".into();
    baseline.new = json!({"status":plan.status});
    rows.push(baseline);
    rows
}
