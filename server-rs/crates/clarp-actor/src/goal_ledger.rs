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
) -> Result<i64, ActorError> {
    if events.is_empty() {
        return Ok(0);
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
    Ok(previous)
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

pub fn mark_empty(plan: &str, now: i64, items: &mut Vec<NewItem>) -> Result<(), ActorError> {
    items.push(record_item(kind::GOAL_LEDGER, tail(plan), &0u64, now)?);
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

fn at(log: &dyn AgentLog, plan: &str, ordinal: u64) -> Result<Event, ActorError> {
    get(log, &event_key(plan, ordinal))?.ok_or_else(|| invalid("missing indexed goal event"))
}

/// Walk only the first record's imported ordinals, newest first. Later owner
/// activity cannot add accounting to this historical prefix.
pub(super) fn imported_accounts(
    log: &dyn AgentLog,
    plan: &str,
    original: i64,
) -> Result<Vec<serde_json::Value>, ActorError> {
    let count: u64 = get(log, &tail(plan))?.unwrap_or(0);
    let mut rows = Vec::new();
    for ordinal in (1..=count.min(original as u64)).rev() {
        let event = at(log, plan, ordinal)?;
        if let Some(row) = clarp_kernel::goals::bookkeeping::accounting_row(&event) {
            rows.push(row);
            if rows.len() == 500 {
                break;
            }
        }
    }
    rows.reverse();
    Ok(rows)
}

pub fn page(
    log: &dyn AgentLog,
    plan: &str,
    after: i64,
    limit: usize,
) -> Result<(Vec<Event>, bool), ActorError> {
    let count: u64 = get(log, &tail(plan))?.unwrap_or(0);
    let mut low = 0;
    let mut high = count;
    if after > 0 {
        while low < high {
            let middle = low + (high - low) / 2;
            if at(log, plan, middle + 1)?.event_id <= after {
                low = middle + 1;
            } else {
                high = middle;
            }
        }
    }
    let limit = limit.clamp(1, 500);
    let remaining = count - low;
    let size = remaining.min(limit as u64);
    let mut events = Vec::with_capacity(size as usize);
    for offset in 1..=size {
        events.push(at(log, plan, low + offset)?);
    }
    Ok((events, remaining > size))
}

/// The roster tombstone is durable before this runs. The worker has stopped;
/// no tool or actor is started to finish deletion. The cancelled plan and its
/// event share an append, so a restart can safely complete a partial retirement.
pub fn retire(log: &mut dyn AgentLog, now: i64) -> Result<(), ActorError> {
    use clarp_kernel::goals::{GoalBook, GoalRecord, ledger};
    const DONE: &str = "ledger/retired";
    if log.find(DONE)?.is_some() {
        return Ok(());
    }
    let mut book = GoalBook::new();
    super::records(log, kind::GOALS, kind::GOAL, |r: GoalRecord| book.apply(&r))?;
    for plan in book.plans().filter(|p| p.goal.is_some()) {
        let mut items = Vec::new();
        let mut events = if has_plan(log, &plan.plan_id)? {
            vec![]
        } else {
            ledger::legacy(plan)
        };
        if plan.status == "active" {
            let change = book
                .finish(
                    &plan.plan_id,
                    "cancelled",
                    "Owner agent deleted",
                    Some(plan.revision),
                    now,
                )
                .map_err(|e| invalid(&e.to_string()))?;
            let mut e = ledger::entry_marker(plan, "", &[], now);
            e.kind = "cancelled".into();
            e.prior = serde_json::json!({"status":"active"});
            e.new = serde_json::json!({"status":"cancelled"});
            e.reason = "Owner agent deleted".into();
            e.plan_revision = None;
            items.push(record_item(
                kind::GOAL,
                format!("g-{}", plan.plan_id),
                &book.record(&change, now),
                now,
            )?);
            events.push(e);
        }
        stage(log, &plan.plan_id, events, 0, 0, now, &mut items)?;
        if !items.is_empty() {
            log.append(&items)?;
        }
    }
    log.append(&[record_item(kind::GOAL_LEDGER, DONE.into(), &true, now)?])?;
    Ok(())
}

pub(super) fn contains(log: &dyn AgentLog, id: i64) -> Result<bool, ActorError> {
    Ok(log.find(&locator(id))?.is_some())
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use super::*;
    use clarp_ports::log::{Item, Kinds, LogEngine, LogError, Seq};
    use clarp_store_mem::MemEngine;
    use serde_json::{Value, json};
    use std::{cell::RefCell, io::Write, path::Path, time::Instant};
    struct Meter {
        log: Box<dyn AgentLog>,
        keys: RefCell<Vec<String>>,
    }
    impl AgentLog for Meter {
        fn find(&self, key: &str) -> Result<Option<Item>, LogError> {
            self.keys.borrow_mut().push(key.into());
            self.log.find(key)
        }
        fn append(&mut self, items: &[NewItem]) -> Result<Seq, LogError> {
            self.log.append(items)
        }
        fn last_seq(&self) -> Result<Seq, LogError> {
            self.log.last_seq()
        }
        fn read_before(&self, _: Seq, _: Kinds, _: usize) -> Result<Vec<Item>, LogError> {
            panic!("ledger listing scanned the log")
        }
        fn read_after(&self, _: Seq, _: Kinds, _: usize) -> Result<Vec<Item>, LogError> {
            panic!("ledger listing scanned the log")
        }
    }

    #[test]
    fn a_page_seeks_only_in_its_plan_and_never_scans_the_agent_log() {
        check_pages(&[1, 1000]);
    }

    #[test]
    #[ignore = "also measures a ledger with 320,000 events"]
    fn a_page_with_ten_thousand_goals() {
        check_pages(&[10_000]);
    }

    fn check_pages(sizes: &[i64]) {
        let table: Value = serde_json::from_str(include_str!(
            "../../clarp-kernel/tests/fixtures/goal-ledger-vectors.json"
        ))
        .unwrap();
        let mut store = Meter {
            log: MemEngine::new()
                .open(
                    Path::new("ledger"),
                    clarp_codec::standard_set("none").unwrap(),
                )
                .unwrap(),
            keys: RefCell::default(),
        };
        for story in table["stories"].as_array().unwrap() {
            let plan = story["plan"].as_str().unwrap();
            let events = story["steps"]
                .as_array()
                .unwrap()
                .iter()
                .map(|v| serde_json::from_value(v["event"].clone()).unwrap())
                .collect::<Vec<Event>>();
            let size = events.len();
            let mut writes = Vec::new();
            stage(&store, plan, events, size, 0, 0, &mut writes).unwrap();
            store.append(&writes).unwrap();
        }
        for case in table["pages"].as_array().unwrap() {
            let plan = case["plan"].as_str().unwrap();
            let after = case["after"].as_i64().unwrap();
            let (rows, more) = page(
                &store,
                plan,
                after,
                case["limit"].as_u64().unwrap() as usize,
            )
            .unwrap();
            let expected = &case["page"];
            assert_eq!(
                expected["next_after"],
                rows.last().map_or(after, |e| e.event_id)
            );
            assert_eq!(expected["has_more"], more);
            assert_eq!(expected["events"].as_array().unwrap().len(), rows.len());
            for (r, e) in rows.iter().zip(expected["events"].as_array().unwrap()) {
                let mut wanted = e.clone();
                wanted.as_object_mut().unwrap().remove("actor_session");
                assert_eq!(json!(r), wanted);
            }
        }
        let target = "bench:0";
        let mut base: Event =
            serde_json::from_value(table["stories"][0]["steps"][0]["event"].clone()).unwrap();
        base.source_refs.clear();
        base.new = json!({});
        let wide = "bench:wide";
        let mut records = Vec::new();
        for id in 1..=501 {
            let mut e = base.clone();
            e.plan_id = wide.into();
            e.event_id = id;
            records
                .push(record_item(kind::GOAL_LEDGER, event_key(wide, id as u64), &e, 0).unwrap());
        }
        records.push(record_item(kind::GOAL_LEDGER, tail(wide), &501, 0).unwrap());
        store.append(&records).unwrap();
        for limit in [499, 500, 501] {
            let (window, more) = page(&store, wide, 0, limit).unwrap();
            assert_eq!(window.len(), limit.min(500));
            assert!(more);
        }
        let (last, more) = page(&store, wide, 500, 500).unwrap();
        assert_eq!(last.len(), 1);
        assert_eq!(last[0].event_id, 501);
        assert!(!more);
        let mut added = 0;
        for &size in sizes {
            for n in added..size {
                let plan = format!("bench:{n}");
                let mut writes = Vec::with_capacity(33);
                for ordinal in 1..=32 {
                    let mut e = base.clone();
                    e.plan_id = plan.clone();
                    e.event_id = n * 32 + ordinal;
                    writes.push(
                        record_item(kind::GOAL_LEDGER, event_key(&plan, ordinal as u64), &e, 0)
                            .unwrap(),
                    );
                }
                writes.push(record_item(kind::GOAL_LEDGER, tail(&plan), &32, 0).unwrap());
                store.append(&writes).unwrap();
            }
            added = size;
            let mut samples = Vec::new();
            for i in 0..105 {
                store.keys.borrow_mut().clear();
                let start = Instant::now();
                let (rows, more) = page(&store, target, 3, 20).unwrap();
                let elapsed = start.elapsed().as_micros();
                assert_eq!(rows.len(), 20);
                assert!(more);
                assert_eq!((rows[0].event_id, rows[19].event_id), (4, 23));
                let keys = store.keys.borrow();
                assert_eq!(keys.len(), 26);
                assert_eq!(keys[0], tail(target));
                assert!(
                    keys[1..]
                        .iter()
                        .all(|k| k.starts_with("ledger/event/bench:0/"))
                );
                if i >= 5 {
                    samples.push(elapsed);
                }
            }
            samples.sort_unstable();
            writeln!(
                std::io::stderr().lock(),
                "goal ledger page goals={size} rows={} reads=26 returned=20 median_us={} p95_us={}",
                size * 32,
                samples[49],
                samples[94]
            )
            .unwrap();
        }
    }
}
