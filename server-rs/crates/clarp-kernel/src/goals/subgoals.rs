//! Explicit owner subgoals. Validation finishes before the owner stages a write.
use super::{Change, GoalBook, Refused, ledger::Event};
use crate::python;
use serde_json::{Map, Value, json};
use std::sync::Arc;

#[derive(Debug)]
pub enum Error {
    Invalid(Refused),
    Denied(String),
}
impl From<Refused> for Error {
    fn from(e: Refused) -> Self {
        Self::Invalid(e)
    }
}
impl std::fmt::Display for Error {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Invalid(e) => e.fmt(f),
            Self::Denied(text) => f.write_str(text),
        }
    }
}
impl Error {
    pub fn tool_exit(&self) -> i32 {
        match self {
            Self::Invalid(_) => 1,
            Self::Denied(_) => 4,
        }
    }
}

fn clean(value: &Value, name: &str, required: bool, limit: usize) -> Result<String, Refused> {
    let raw = if !python::truthy(value) {
        String::new()
    } else if let Some(s) = value.as_str() {
        s.to_owned()
    } else {
        python::repr(value)
    };
    let text = python::strip(&raw);
    if required && text.is_empty() {
        return Err(Refused(format!("{name} is required")));
    }
    if text.chars().count() > limit {
        return Err(Refused(format!("{name} is longer than {limit} characters")));
    }
    Ok(text.into())
}

fn criteria(v: &Value, field: &str) -> Result<Value, Refused> {
    let rows = v
        .as_array()
        .filter(|a| a.len() <= 20)
        .ok_or_else(|| Refused(format!("{field} must be a list of at most 20 strings")))?;
    rows.iter()
        .map(|v| clean(v, "criterion", true, 500).map(Value::String))
        .collect::<Result<Vec<_>, _>>()
        .map(Value::Array)
}
fn refs(v: &Value) -> Result<Vec<String>, Refused> {
    if v.is_null() || v == "" {
        return Ok(vec![]);
    }
    let bad = || Refused("source_refs must be a list of non-empty strings".into());
    let rows = v.as_array().ok_or_else(bad)?;
    let texts: Vec<_> = rows
        .iter()
        .map(|r| {
            r.as_str()
                .map(python::strip)
                .filter(|s| !s.is_empty())
                .ok_or_else(bad)
        })
        .collect::<Result<_, _>>()?;
    if texts.len() > 20 {
        return Err(Refused("at most 20 source_refs".into()));
    }
    Ok(texts.iter().map(|s| python::first(s, 300).into()).collect())
}
fn evidence(old: &Value, v: &Value) -> Result<Value, Refused> {
    let mut out = old.as_array().cloned().unwrap_or_default();
    for item in v
        .as_array()
        .ok_or_else(|| Refused("evidence must be a list".into()))?
    {
        if !item.is_object() {
            return Err(Refused("evidence items are objects".into()));
        }
        let text = clean(&item["text"], "evidence text", true, 4000)?;
        let basis = match item["basis"].as_str() {
            Some("observed") => "observed",
            Some("inferred") => "inferred",
            _ => "claimed",
        };
        out.push(json!({"text":text,"basis":basis,"source_refs":refs(&item["source_refs"])?}));
    }
    Ok(Value::Array(out))
}
fn integer(v: &Value) -> Option<i64> {
    v.as_i64().or_else(|| v.as_bool().map(i64::from))
}

fn changes(current: &Value, data: &Value, id: &str) -> Result<Value, Error> {
    let rev = current["revision"].as_i64().unwrap_or(1);
    if integer(&data["expected_revision"]) != Some(rev) {
        return Err(Error::from(Refused(format!(
            "subgoal {id} changed (revision {rev}); reload and reconcile instead of overwriting"
        ))));
    }
    let v = &data["fields"];
    if !python::truthy(v) {
        return Err(Error::from(Refused("nothing to change".into())));
    }
    let fields = v.as_object().ok_or_else(|| {
        Refused(format!(
            "'{}' object has no attribute 'items'",
            type_name(v)
        ))
    })?;
    let mut denied: Vec<_> = fields
        .keys()
        .filter(|s| {
            !matches!(
                s.as_str(),
                "title"
                    | "intent"
                    | "criteria"
                    | "owner"
                    | "status"
                    | "evidence"
                    | "current_action"
                    | "next_dependency"
                    | "last_observed_at"
            )
        })
        .cloned()
        .collect();
    denied.sort();
    if !denied.is_empty() {
        return Err(Error::Denied(format!(
            "cannot change {}",
            denied.join(", ")
        )));
    }
    let mut changed = Map::new();
    for (k, v) in fields {
        let taken = match k.as_str() {
            "status" => {
                if !matches!(
                    v.as_str(),
                    Some("proposed" | "active" | "blocked" | "done" | "retired" | "unknown")
                ) {
                    return Err(Error::from(Refused(format!(
                        "unknown subgoal status {}",
                        python::repr(v)
                    ))));
                }
                if v == "retired" && !python::truthy(&data["reason"]) {
                    return Err(Error::from(Refused(
                        "retiring a subgoal needs a reason".into(),
                    )));
                }
                v.clone()
            }
            "criteria" => criteria(v, "criteria")?,
            "evidence" => evidence(&current["evidence"], v)?,
            "last_observed_at" => {
                if integer(v).is_none() {
                    return Err(Error::from(Refused(
                        "last_observed_at is epoch milliseconds".into(),
                    )));
                }
                v.clone()
            }
            _ => json!(clean(v, k, false, if k == "title" { 300 } else { 4000 })?),
        };
        changed.insert(k.clone(), taken);
    }
    Ok(Value::Object(changed))
}

fn type_name(value: &Value) -> &'static str {
    match value {
        Value::Null => "NoneType",
        Value::Bool(_) => "bool",
        Value::Number(n) if n.is_f64() => "float",
        Value::Number(_) => "int",
        Value::String(_) => "str",
        Value::Array(_) => "list",
        Value::Object(_) => "dict",
    }
}

impl GoalBook {
    pub fn owner_subgoal(
        &self,
        pid: &str,
        action: &str,
        data: &Value,
        caller: Option<&str>,
        now: i64,
    ) -> Result<(Change, Event), Error> {
        let plan = self
            .plan(pid)
            .filter(|p| p.goal.is_some())
            .ok_or_else(|| Refused("no durable goal with that id".into()))?;
        if !matches!(action, "add" | "update") {
            return Err(Error::from(Refused(
                "subgoal action is add or update".into(),
            )));
        }
        if !data.is_object() {
            return Err(Error::from(Refused(format!(
                "'{}' object has no attribute 'get'",
                type_name(data)
            ))));
        }
        let id = clean(&data["subgoal_id"], "subgoal_id", true, 80)?;
        let existing = plan.subgoals.iter().position(|r| r["subgoal_id"] == id);
        let who = caller.unwrap_or(&plan.agent_id);
        let actor = if who == plan.agent_id {
            "owner"
        } else {
            "peer"
        };
        let (row, prior, new, kind) = if action == "add" {
            if existing.is_some() {
                return Err(Error::from(Refused(format!("subgoal {id} already exists"))));
            }
            let empty = json!([]);
            let c = if python::truthy(&data["criteria"]) {
                &data["criteria"]
            } else {
                &empty
            };
            if c.as_array().is_none_or(|a| a.len() > 20) {
                return Err(Error::from(Refused(
                    "subgoal criteria must be a list of at most 20 strings".into(),
                )));
            }
            let new = json!({"title":clean(&data["title"],"title",true,300)?,"intent":clean(&data["intent"],"intent",false,4000)?,"criteria":criteria(c,"subgoal criteria")?,"owner":clean(&data["owner"],"owner",false,120)?,"status":"active"});
            let mut row = json!({"subgoal_id":id,"evidence":[],"current_action":"","next_dependency":"","last_observed_at":null,"revision":1,"created_by":{"actor_kind":actor,"agent_id":who},"created_at":now,"updated_at":now});
            if let (Some(r), Some(n)) = (row.as_object_mut(), new.as_object()) {
                r.extend(n.clone());
            }
            (row, Value::Null, new, "subgoal_added")
        } else {
            let idx = existing.ok_or_else(|| Refused(format!("no subgoal {id}")))?;
            let mut row = plan.subgoals[idx].clone();
            let new = changes(&row, data, &id)?;
            let mut prior = Map::new();
            if let Some(fields) = new.as_object() {
                for (k, v) in fields {
                    prior.insert(k.clone(), row[k].clone());
                    row[k] = if k == "last_observed_at" {
                        json!(integer(v))
                    } else {
                        v.clone()
                    };
                }
            }
            let next = row["revision"]
                .as_i64()
                .and_then(|n| n.checked_add(1))
                .ok_or_else(|| Refused("subgoal revision exhausted".into()))?;
            row["revision"] = json!(next);
            row["updated_at"] = json!(now);
            (row, Value::Object(prior), new, "subgoal_updated")
        };
        let event = Event {
            event_id: 0,
            plan_id: pid.into(),
            at: now,
            kind: kind.into(),
            subject: format!("subgoal:{id}"),
            actor_kind: actor.into(),
            actor_agent_id: who.into(),
            delegation_id: String::new(),
            basis: String::new(),
            reason: clean(&data["reason"], "reason", false, 4000)?,
            source_refs: refs(&data["source_refs"])?,
            prior,
            new,
            plan_revision: Some(plan.revision),
        };
        let mut changed = plan.clone();
        let rows = Arc::make_mut(&mut changed.subgoals);
        if let Some(idx) = existing {
            rows[idx] = row;
        } else {
            rows.push(row);
            sort_rows(rows);
        }
        changed.event_count += 1;
        changed.last_event_at = Some(now.max(plan.last_event_at.unwrap_or(now)));
        Ok((
            Change {
                plan: changed,
                documents: vec![],
            },
            event,
        ))
    }
}
/// A move keeps the original creator, ordering, evidence and subgoal revisions.
/// Old exports have no subgoals key. Reject malformed entries before accepting the plan.
pub(super) fn imported(value: &Value) -> Result<Arc<Vec<Value>>, Refused> {
    if value.is_null() {
        return Ok(Default::default());
    }
    let bad = || Refused("invalid imported subgoals".into());
    let mut rows = value.as_array().cloned().ok_or_else(bad)?;
    let mut ids = std::collections::BTreeSet::new();
    for row in &rows {
        let id = row["subgoal_id"].as_str().ok_or_else(bad)?;
        if id.is_empty()
            || !ids.insert(id)
            || !row["revision"]
                .as_i64()
                .is_some_and(|n| (1..i64::MAX).contains(&n))
            || !row["created_at"].is_i64()
            || !row["updated_at"].is_i64()
            || !row["criteria"].is_array()
            || !row["evidence"].is_array()
            || !row["created_by"]["actor_kind"].is_string()
            || !row["created_by"]["agent_id"].is_string()
            || [
                "title",
                "intent",
                "owner",
                "status",
                "current_action",
                "next_dependency",
            ]
            .iter()
            .any(|k| !row[*k].is_string())
        {
            return Err(bad());
        }
    }
    sort_rows(&mut rows);
    Ok(rows.into())
}

pub(super) fn sort_rows(rows: &mut [Value]) {
    rows.sort_by(|a, b| {
        a["created_at"]
            .as_i64()
            .cmp(&b["created_at"].as_i64())
            .then_with(|| a["subgoal_id"].as_str().cmp(&b["subgoal_id"].as_str()))
    });
}
