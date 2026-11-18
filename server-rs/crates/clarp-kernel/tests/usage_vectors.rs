//! What a Host's own turns used, held to what the Python Host's own accounting answers for
//! the same turns (`tests/fixtures/usage-vectors.json`, written by
//! `server-rs/tools/usage-vectors.py`).

#![allow(clippy::unwrap_used, clippy::expect_used)]

use clarp_kernel::usage::{self, Used};
use serde_json::Value;

#[test]
fn what_turns_used_is_summed_as_the_python_host_sums_it() {
    let vectors: Value = serde_json::from_str(include_str!("fixtures/usage-vectors.json")).unwrap();
    let since = vectors["since"].as_i64().unwrap();
    let (mut askings, mut counted, mut passed_over) = (0, 0, 0);
    for story in vectors["stories"].as_array().unwrap() {
        let name = &story["name"];
        let mut kept: Vec<Used> = Vec::new();
        for turn in story["turns"].as_array().unwrap() {
            let said = &turn["said"];
            let used = Used::said(
                turn["tool"].as_str().unwrap(),
                turn["at"].as_i64().unwrap(),
                (said["tokens_in"].as_u64(), said["tokens_out"].as_u64()),
                said["cost_usd"].as_f64(),
                turn["told_a_total"].as_bool().unwrap(),
            );
            match used {
                Some(used) => kept.push(used),
                None => passed_over += 1,
            }
        }
        counted += kept.len();
        for asked in story["asked"].as_array().unwrap() {
            let now = asked["now"].as_i64().unwrap();
            for tool in ["claude", "codex"] {
                let ours = usage::totals(&kept, tool, (now, since));
                let theirs = &asked["totals"][tool];
                assert_eq!(&ours, theirs, "{name}, {tool} at {now}");
                assert_eq!(ours.to_string(), theirs.to_string(), "{name}, as text");
            }
            // The row an app is given of Claude Code's turns. The moment of the newest
            // turn is written by the Host; here it is taken as the Python Host wrote it.
            let totals = usage::totals(&kept, "claude", (now, since));
            let ended_at = asked["row"]["fetched_at"].as_str().map(str::to_string);
            let ours = usage::row("claude", &totals, now, ended_at);
            assert_eq!(ours, asked["row"], "{name}, the row at {now}");
            assert_eq!(
                ours.to_string(),
                asked["row"].to_string(),
                "{name}, the row as text"
            );
            askings += 1;
        }
    }
    assert_eq!((askings, counted, passed_over), (14, 25, 2));
}

/// A dollar amount of the vectors, in millionths.
fn micros(dollars: &Value) -> Option<u64> {
    dollars
        .as_f64()
        .map(|dollars| (dollars * 1_000_000.0).round() as u64)
}

#[test]
fn a_turn_cost_what_its_sessions_total_grew_by_as_the_python_host_counts_it() {
    let vectors: Value = serde_json::from_str(include_str!("fixtures/usage-vectors.json")).unwrap();
    assert!(usage::tells_a_total("claude") && !usage::tells_a_total("codex"));
    let (mut turns, mut known) = (0, 0);
    for story in vectors["rises"].as_array().unwrap() {
        let name = &story["name"];
        // What each session's total stood at when it was told last.
        let mut stood: std::collections::BTreeMap<String, u64> = std::collections::BTreeMap::new();
        for (at, step) in story["steps"].as_array().unwrap().iter().enumerate() {
            turns += 1;
            let session = step["session"].as_str().unwrap();
            let ours = match micros(&step["total"]) {
                // A turn that told no total, or of no session, has no cost that is known.
                Some(total) if !session.is_empty() => {
                    // A session that begins with the turn stood at nothing before.
                    let begins = step["fresh"].as_bool().unwrap().then_some(0);
                    let before = stood.get(session).copied().or(begins);
                    stood.insert(session.to_string(), total);
                    usage::rise(before, total)
                }
                _ => None,
            };
            known += usize::from(ours.is_some());
            assert_eq!(ours, micros(&step["share"]), "{name}, turn {at}: {step}");
        }
    }
    assert_eq!((turns, known), (33, 23));
}
