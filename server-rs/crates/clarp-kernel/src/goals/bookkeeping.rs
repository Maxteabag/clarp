//! Delegate batches are prepared against the principal's state before the actor
//! appends them. Source snapshots describe exactly which evidence was available.
use super::{
    Refused,
    ledger::Event,
    subgoals::{Error, clean, refs},
};
use crate::python;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::collections::BTreeSet;

pub fn batch<'a>(wake: &str, entries: &'a Value) -> Result<(String, &'a [Value]), Refused> {
    let wake = clean(&json!(wake), "wake_id", true, 160)?;
    let items = entries
        .as_array()
        .filter(|v| v.len() <= 50)
        .ok_or_else(|| Refused("entries must be a list of at most 50".into()))?;
    Ok((wake, items))
}

pub fn entry_key(wake: &str, entry: &Value) -> Result<String, Refused> {
    if !entry.is_object() {
        return Err(Refused("each entry is an object".into()));
    }
    let key = &entry["key"];
    let suffix = if python::truthy(key) {
        key.as_str()
            .map(str::to_owned)
            .unwrap_or_else(|| python::repr(key))
    } else {
        let mut sorted = entry.clone();
        sorted.sort_all_objects();
        let digest = Sha256::digest(python::dumps_ascii(&sorted).as_bytes());
        format!("{digest:x}")[..16].to_owned()
    };
    Ok(format!("delegate:{wake}:{suffix}"))
}

pub enum Source<'a> {
    Message { revision: i64, text: &'a str },
    Immutable(bool),
    Unavailable,
    Unverified,
}

pub fn snapshot(reference: &str, source: Source<'_>) -> Value {
    let status = |status: &str| json!({"ref":reference,"status":status});
    let Source::Message { revision, text } = source else {
        return match source {
            Source::Immutable(true) => status("exact"),
            Source::Unverified => status("unverified"),
            _ => status("unavailable"),
        };
    };
    let cited = reference.split_once('@').map(|(_, rev)| rev).unwrap_or("");
    let digest = || format!("{:x}", Sha256::digest(text.as_bytes()));
    if !python::isdigit(cited) {
        return json!({"ref":reference,"status":"unpinned","current_revision":revision,"current_sha256":digest()});
    }
    match python::int(cited) {
        Some(n) if n == i128::from(revision) => {
            json!({"ref":reference,"status":"exact","revision":revision,"sha256":digest(),"excerpt":python::first(text,300)})
        }
        Some(n) if n < i128::from(revision) => {
            json!({"ref":reference,"status":"changed","current_revision":revision,"current_sha256":digest()})
        }
        _ => json!({"ref":reference,"status":"invalid","current_revision":revision}),
    }
}

pub struct Bookkeeper<'a> {
    pub delegation: &'a str,
    pub caller: &'a str,
    pub plan: &'a str,
    pub revision: i64,
    pub now: i64,
}

pub fn accounting_event(
    entry: &Value,
    by: &Bookkeeper<'_>,
    mut read: impl FnMut(&str) -> Result<Value, Error>,
) -> Result<Event, Error> {
    let kind = &entry["type"];
    if kind.is_array() || kind.is_object() {
        return Err(Refused(format!(
            "unhashable type: '{}'",
            super::subgoals::type_name(kind)
        ))
        .into());
    }
    let basis = match kind.as_str() {
        Some("observation" | "discrepancy") => "observed",
        Some("claim") => "claimed",
        Some("unknown") => "inferred",
        _ => {
            return Err(Error::Denied(format!(
                "a bookkeeping delegate cannot {}: it records observations, claims, discrepancies, unknowns and subgoal bookkeeping only",
                python::repr(kind)
            )));
        }
    };
    let kind = kind.as_str().unwrap_or_default();
    let references = refs(&entry["source_refs"])?;
    if kind != "unknown" && references.is_empty() {
        return Err(Refused(format!("a {kind} needs source_refs")).into());
    }
    let sources = references
        .iter()
        .map(|r| read(r))
        .collect::<Result<Vec<_>, _>>()?;
    let subject = if python::truthy(&entry["subject"]) {
        clean(&entry["subject"], "subject", false, 120)?
    } else {
        "goal".into()
    };
    if subject != "goal"
        && !matches!(
            subject.split(':').next(),
            Some("criterion" | "step" | "subgoal")
        )
    {
        return Err(
            Refused("subject is goal, criterion:<id>, step:<id> or subgoal:<id>".into()).into(),
        );
    }
    let reason = clean(&entry["reason"], "reason", false, 4000)?;
    let mut new = json!({"text":clean(&entry["text"],"text",true,4000)?,"sources":sources});
    let observed = &entry["observed_at"];
    if observed.is_i64() || observed.is_u64() || observed.is_boolean() {
        new["observed_at"] = observed.clone();
    }
    Ok(Event {
        event_id: 0,
        plan_id: by.plan.into(),
        at: by.now,
        kind: kind.into(),
        subject,
        actor_kind: "delegate".into(),
        actor_agent_id: by.caller.into(),
        delegation_id: by.delegation.into(),
        basis: basis.into(),
        source_refs: references,
        reason,
        prior: Value::Null,
        new,
        plan_revision: Some(by.revision),
    })
}

/// Compact row used by a plan's accounting view. Source snapshots stay on
/// the immutable event; the projection carries the citation strings only.
pub fn accounting_row(e: &Event) -> Option<Value> {
    (e.actor_kind=="delegate" && matches!(e.kind.as_str(),"observation"|"claim"|"discrepancy"|"unknown")).then(||json!({
        "event_id":e.event_id,"at":e.at,"subject":e.subject,"kind":e.kind,"basis":e.basis,"text":e.new["text"],
        "observed_at":e.new["observed_at"],"source_refs":e.source_refs,"actor_agent_id":e.actor_agent_id,"delegation_id":e.delegation_id
    }))
}

pub fn latest_accounting<'a>(newest: impl Iterator<Item = &'a Value>) -> Vec<Value> {
    let mut seen = BTreeSet::new();
    newest
        .take(500)
        .filter(|row| seen.insert((row["subject"].as_str(), row["kind"].as_str())))
        .take(50)
        .cloned()
        .collect()
}

impl super::GoalBook {
    pub fn accounting_for(&self, plan: &str) -> Vec<Value> {
        self.accounting
            .get(plan)
            .map_or_else(Vec::new, |rows| latest_accounting(rows.iter().rev()))
    }
    /// A batch may make successive changes to one goal. Stage just that plan.
    pub fn stage_plan(&self, id: &str) -> Self {
        let mut book = Self::new();
        if let Some(plan) = self.plan(id) {
            book.plans.insert(id.into(), plan.clone());
        }
        book
    }

    pub fn cited_document(&self, name: &str, revision: i64) -> bool {
        self.documents
            .iter()
            .any(|row| row.name == name && row.revision == revision)
    }

    pub fn observed_goals(&self) -> Vec<Value> {
        self.newest_first().into_iter().filter(|plan|matches!(plan.status.as_str(),"active"|"paused"|"blocked"))
            .filter_map(|plan|{
                let goal=plan.goal.as_ref()?;
                let mut roots: Vec<_> = plan.items.iter().filter(|i| i.parent_id.is_none()).collect();
                roots.sort_by_key(|item| item.position);
                let steps: Vec<_>=roots.into_iter().map(|i|
                    json!({"id":i.item_id.rsplit(':').next(),"title":i.title,"status":i.status})).collect();
                Some(json!({"plan_id":plan.plan_id,"revision":plan.revision,"status":plan.status,"outcome":goal.fields.get("outcome"),
                    "criteria":goal.fields.get("criteria"),"checkpoint":goal.fields.get("checkpoint"),"steps":steps,"subgoals":*plan.subgoals}))
            }).take(10).collect()
    }
}

pub fn integer(value: &Value) -> Result<i64, Refused> {
    if let Some(n) = value.as_i64() {
        return Ok(n);
    }
    if let Some(b) = value.as_bool() {
        return Ok(i64::from(b));
    }
    if let Some(s) = value.as_str() {
        return python::int(s)
            .map(|n| n.clamp(i128::from(i64::MIN), i128::from(i64::MAX)) as i64)
            .ok_or_else(|| {
                Refused(format!(
                    "invalid literal for int() with base 10: {}",
                    python::repr(value)
                ))
            });
    }
    if let Some(n) = value.as_f64() {
        return Ok(n as i64);
    }
    Err(Refused(format!(
        "int() argument must be a string, a bytes-like object or a real number, not '{}'",
        match value {
            Value::Array(_) => "list",
            Value::Object(_) => "dict",
            _ => "NoneType",
        }
    )))
}

pub fn observe_keywords(options: &Value) -> Result<(), Refused> {
    let Some(fields) = options.as_object() else {
        return Err(Refused(format!(
            "lib.goal_ledger.observe() argument after ** must be a mapping, not {}",
            super::subgoals::type_name(options)
        )));
    };
    for k in fields.keys() {
        if !matches!(k.as_str(), "after_message" | "after_goal_event" | "limit") {
            return Err(Refused(format!(
                "observe() got an unexpected keyword argument '{}'",
                k
            )));
        }
    }
    Ok(())
}

pub fn observe_options(options: &Value) -> Result<Value, Refused> {
    observe_keywords(options)?;
    let mut out = json!({});
    for key in ["after_message", "after_goal_event", "limit"] {
        if let Some(v) = options.get(key)
            && (key == "limit" || !v.is_null())
        {
            out[key] = json!(integer(v)?);
        }
    }
    Ok(out)
}
