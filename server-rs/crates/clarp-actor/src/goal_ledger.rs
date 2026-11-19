//! A goal event is owned by the same log as its plan. A dense plan ordinal
//! names the event body. An owner-local ID names the locator for citations.
use super::{ActorError, record_item};
use clarp_kernel::{goals::ledger::Event, journal::kind};
use clarp_ports::log::{AgentLog, NewItem};
use serde::de::DeserializeOwned;
use std::collections::BTreeSet;

const COUNTER: &str = "ledger/next";
fn tail(plan: &str) -> String {
    format!("ledger/tail/{plan}")
}
fn event_key(plan: &str, n: u64) -> String {
    format!("ledger/event/{plan}/{n}")
}
fn locator(id: i64) -> String {
    format!("ledger/id/{id}")
}
fn invalid(why: &str) -> ActorError {
    ActorError::Corrupt(why.into())
}
fn get<T: DeserializeOwned>(log: &dyn AgentLog, key: &str) -> Result<Option<T>, ActorError> {
    log.find(key)?
        .map(|item| {
            serde_json::from_slice(&item.body)
                .map_err(|e| ActorError::Corrupt(format!("goal ledger record {key}: {e}")))
        })
        .transpose()
}
pub fn next_id(log: &dyn AgentLog) -> Result<i64, ActorError> {
    Ok(get(log, COUNTER)?.unwrap_or(1))
}

/// All items are staged before the actor commits the single append.
pub fn stage(
    log: &dyn AgentLog,
    plan: &str,
    events: Vec<Event>,
    imported: usize,
    import_high: i64,
    now: i64,
    items: &mut Vec<NewItem>,
) -> Result<(), ActorError> {
    if events.is_empty() {
        return Ok(());
    }
    let mut next = next_id(log)?.max(
        import_high
            .checked_add(1)
            .ok_or_else(|| invalid("goal ledger ID exhausted"))?,
    );
    let mut ordinal: u64 = get(log, &tail(plan))?.unwrap_or(0);
    let mut previous = 0;
    for (i, mut event) in events.into_iter().enumerate() {
        if event.plan_id != plan {
            return Err(invalid("ledger event belongs to another plan"));
        }
        if i < imported {
            if event.event_id <= previous {
                return Err(invalid(
                    "imported ledger IDs must be positive and increasing within a plan",
                ));
            }
            if log.find(&locator(event.event_id))?.is_some() {
                return Err(invalid(
                    "imported ledger event ID already belongs to this agent",
                ));
            }
        } else {
            event.event_id = next;
        }
        previous = event.event_id;
        next = next.max(
            previous
                .checked_add(1)
                .ok_or_else(|| invalid("goal ledger ID exhausted"))?,
        );
        ordinal = ordinal
            .checked_add(1)
            .ok_or_else(|| invalid("goal ledger ordinal exhausted"))?;
        items.push(record_item(
            kind::GOAL_LEDGER,
            locator(previous),
            &(plan, ordinal),
            now,
        )?);
        items.push(record_item(
            kind::GOAL_LEDGER,
            event_key(plan, ordinal),
            &event,
            now,
        )?);
    }
    items.push(record_item(kind::GOAL_LEDGER, tail(plan), &ordinal, now)?);
    items.push(record_item(kind::GOAL_LEDGER, COUNTER.into(), &next, now)?);
    Ok(())
}

pub fn check_import(log: &dyn AgentLog, plans: &[(String, &[Event])]) -> Result<(), ActorError> {
    let mut ids = BTreeSet::new();
    for (plan, events) in plans {
        let mut last = 0;
        for e in *events {
            if e.plan_id != *plan || e.event_id <= last || e.event_id == i64::MAX {
                return Err(invalid("invalid imported goal ledger ID or plan"));
            }
            if !ids.insert(e.event_id) || log.find(&locator(e.event_id))?.is_some() {
                return Err(invalid(
                    "imported ledger event ID already belongs to this agent",
                ));
            }
            last = e.event_id;
        }
    }
    Ok(())
}

pub fn has_plan(log: &dyn AgentLog, plan: &str) -> Result<bool, ActorError> {
    Ok(log.find(&tail(plan))?.is_some())
}

pub fn cursor(
    log: &dyn AgentLog,
    delegation: &str,
    requested: i64,
    now: i64,
    pending: &[NewItem],
) -> Result<NewItem, ActorError> {
    let key = format!("ledger/applied/{delegation}");
    let prior = get(log, &key)?.unwrap_or(0);
    let next = match pending
        .iter()
        .rev()
        .find(|i| i.key.as_deref() == Some(COUNTER))
    {
        Some(i) => serde_json::from_slice::<i64>(&i.body).map_err(|e| invalid(&e.to_string()))?,
        None => next_id(log)?,
    };
    let through = clarp_kernel::goals::ledger::applied_through(prior, requested, next - 1);
    record_item(kind::GOAL_LEDGER, key, &through, now)
}
