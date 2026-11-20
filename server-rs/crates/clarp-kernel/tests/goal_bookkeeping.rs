#![allow(clippy::unwrap_used)]
use clarp_kernel::goals::bookkeeping::{self, Bookkeeper, Source};
use serde_json::{Value, json};
use std::collections::HashMap;

#[test]
fn a_delegates_citations_keep_the_reference_snapshot_and_its_accounting() {
    let fixture: Value =
        serde_json::from_str(include_str!("fixtures/goal-bookkeeping.json")).unwrap();
    let mut sources = HashMap::new();
    let text = "évidence".repeat(46);
    for wanted in fixture["snapshots"].as_array().unwrap() {
        let r = wanted["ref"].as_str().unwrap();
        let source = if r.starts_with("message:u-note") {
            Source::Message {
                revision: fixture["message_revision"].as_i64().unwrap(),
                text: &text,
            }
        } else if r.starts_with("job:") {
            Source::Unverified
        } else {
            Source::Immutable(matches!(r, "goal_event:1" | "document:proof@1"))
        };
        let snap = bookkeeping::snapshot(r, source);
        assert_eq!(snap, *wanted);
        sources.insert(r, snap);
    }
    let mut recorded = Vec::new();
    for call in fixture["record"].as_array().unwrap() {
        let args = bookkeeping::batch(call["wake"].as_str().unwrap(), &call["entries"]);
        let Ok((_, entries)) = args else {
            assert_eq!(
                args.unwrap_err().to_string(),
                call["error"].as_str().unwrap()
            );
            continue;
        };
        let by = Bookkeeper {
            delegation: fixture["delegation"].as_str().unwrap(),
            caller: "helper",
            plan: fixture["plan"].as_str().unwrap(),
            revision: 0,
            now: call["now"].as_i64().unwrap(),
        };
        for expected in call["events"].as_array().unwrap() {
            if expected["kind"].as_str().unwrap().starts_with("subgoal_") {
                continue;
            }
            let mut event =
                bookkeeping::accounting_event(&entries[0], &by, |r| Ok(sources[r].clone()))
                    .unwrap();
            event.event_id = expected["event_id"].as_i64().unwrap();
            let mut want = expected.clone();
            want.as_object_mut().unwrap().remove("actor_session");
            assert_eq!(json!(event), want);
            recorded.extend(bookkeeping::accounting_row(&event));
        }
    }
    assert_eq!(
        json!(bookkeeping::latest_accounting(recorded.iter().rev())),
        fixture["accounting"]
    );
}

#[test]
fn bookkeeping_read_options_and_replay_keys_match_the_reference() {
    let f: Value = serde_json::from_str(include_str!("fixtures/goal-bookkeeping.json")).unwrap();
    for case in f["options"].as_array().unwrap() {
        match bookkeeping::observe_options(&case["options"]) {
            Ok(v) => assert_eq!(v, case["normalized"], "{case}"),
            Err(e) => assert_eq!(e.0, case["error"].as_str().unwrap(), "{case}"),
        }
    }
    for case in f["record"].as_array().unwrap() {
        let keys = case["keys"].as_array().unwrap();
        for (entry, key) in case["entries"].as_array().into_iter().flatten().zip(keys) {
            assert_eq!(
                bookkeeping::entry_key(case["wake"].as_str().unwrap().trim(), entry).unwrap(),
                *key
            );
        }
    }
}

#[test]
fn accounting_returns_at_most_fifty_pairs_from_only_the_newest_five_hundred_entries() {
    let mut rows: Vec<_> = (0..501)
        .map(|i| json!({"event_id":i,"kind":"observation","subject":"goal"}))
        .collect();
    rows[0]["subject"] = json!("step:too-old");
    rows[1]["subject"] = json!("step:boundary");
    assert_eq!(
        bookkeeping::latest_accounting(rows.iter().rev()),
        vec![rows[500].clone(), rows[1].clone()]
    );
    for (i, row) in rows.iter_mut().enumerate() {
        row["subject"] = json!(format!("step:{i}"));
    }
    let page = bookkeeping::latest_accounting(rows.iter().rev());
    assert_eq!(page.len(), 50);
    assert_eq!(page.last().unwrap()["event_id"], 451);
}
