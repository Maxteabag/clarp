//! What a turn of Codex took, against the Python Host's own count.
//!
//! `fixtures/codex-turn-usage-vectors.json` is written by `server-rs/tools/turn-usage-vectors.py`:
//! the Python Host's Codex client (`codex_app_server._Client._notification`, since its PR 413)
//! is told the app-server's notifications of made-up turns of one thread, and says what each
//! turn took, with where it measured the turn to have begun (`opening`). This Host runs Codex as
//! `codex exec --json`, which tells only the thread's total at a turn's end (`total_at_end`):
//! a turn took what that grew by from where the turn began, which is where the turn before
//! ended, or nothing where the thread begins with the turn.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use clarp_kernel::usage;
use serde_json::Value;

fn vectors() -> Value {
    serde_json::from_str(include_str!("fixtures/codex-turn-usage-vectors.json")).unwrap()
}

fn pair(value: &Value) -> Option<(u64, u64)> {
    let halves = value.as_array()?;
    Some((halves[0].as_u64()?, halves[1].as_u64()?))
}

/// How many turns were held in each way.
#[derive(Debug, Default, PartialEq)]
struct Counted {
    begun: usize,
    continued: usize,
    not_known: usize,
    elsewhere: usize,
    no_report: usize,
}

#[test]
fn a_turn_of_codex_took_what_its_threads_total_grew_by_as_the_python_host_counts_it() {
    let vectors = vectors();
    let mut counted = Counted::default();
    for thread in vectors["threads"].as_array().unwrap() {
        let name = thread["name"].as_str().unwrap();
        let mut ended: Option<(u64, u64)> = None;
        for (number, turn) in thread["turns"].as_array().unwrap().iter().enumerate() {
            let at = format!("{name}, turn {number}");
            let took = pair(&turn["took"]);
            let Some(total) = pair(&turn["total_at_end"]) else {
                // No figure at the turn's end: `codex exec` ends no turn without one.
                counted.no_report += 1;
                ended = None;
                continue;
            };
            let opening = pair(&turn["opening"]);
            if number == 0 {
                if opening == Some((0, 0)) {
                    // The thread begins with the turn: it took all of its total.
                    assert_eq!(usage::share(Some((0, 0)), total), took, "{at}");
                    counted.begun += 1;
                } else {
                    // A thread this Host did not begin and kept no total of: what the turn
                    // took is not known here, where the reference learns it from its first call.
                    assert_eq!(usage::share(None, total), None, "{at}");
                    assert!(took.is_some(), "{at}");
                    counted.not_known += 1;
                }
            } else if opening == ended {
                // Nothing happened to the thread between the two turns: the same count.
                assert_eq!(usage::share(ended, total), took, "{at}");
                counted.continued += 1;
            } else {
                // The reference's first call says the turn began elsewhere than where the turn
                // before ended (its made-up samples, a total that fell): this Host counts from
                // where the turn before ended, and says nothing where a half of it fell.
                let here = usage::share(ended, total);
                assert_ne!(here, took, "{at}");
                let (before, now) = (ended.unwrap(), total);
                let fell = now.0 < before.0 || now.1 < before.1;
                assert_eq!(here.is_none(), fell, "{at}");
                counted.elsewhere += 1;
            }
            ended = Some(total);
        }
    }
    assert_eq!(
        counted,
        Counted {
            begun: 2,
            continued: 7,
            not_known: 10,
            elsewhere: 3,
            no_report: 1,
        }
    );
}

#[test]
fn the_share_of_a_turn_in_a_running_total() {
    // Each half on its own, and none where a half fell or where it began is not known.
    assert_eq!(usage::share(Some((10, 4)), (25, 4)), Some((15, 0)));
    assert_eq!(usage::share(Some((0, 0)), (7, 3)), Some((7, 3)));
    assert_eq!(usage::share(Some((10, 4)), (9, 9)), None);
    assert_eq!(usage::share(Some((10, 4)), (19, 3)), None);
    assert_eq!(usage::share(None, (19, 5)), None);
}
