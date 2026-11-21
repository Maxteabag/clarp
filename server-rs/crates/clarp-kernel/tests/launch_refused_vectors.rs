//! A send whose tool could not be started (R121), held to the Python Host's own functions
//! (`tests/fixtures/launch-refused-vectors.json`, written by `server-rs/tools/launch-refused-vectors.py`, which drives
//! `recovery_backoff.signature`, `_brief`, `RecoveryBackoff.failed`, `turn_queue.REFUSED_LAUNCH_NOTICE`, and whole
//! stories on `TurnDispatchService` with a backend that refuses).
//!
//! The stories are told again here by this Host's rule: a send's own try, a second, then the counted tries with their
//! waits, the eighth of one refusal ending the turn, and the next turn on a count of its own. Where the Python Host
//! tries a later send at once, ahead of one that waits (its own try when it is sent, while the first waits), this Host
//! does not: a turn that waits is the agent's turn, and nothing behind it overtakes (`docs/differences.md`).

#![allow(clippy::unwrap_used, clippy::expect_used)]

use clarp_kernel::launch_refused::{
    FIRST_WAIT_MS, Next, PARK_AFTER, REFUSED_IN_ALL, Refusals, brief, notice, signature, wait_ms,
};
use serde_json::Value;

fn vectors() -> Value {
    serde_json::from_str(include_str!("fixtures/launch-refused-vectors.json")).unwrap()
}

fn text(value: &Value) -> &str {
    value.as_str().unwrap()
}

fn ms(seconds: &Value) -> i64 {
    (seconds.as_f64().unwrap() * 1_000.0).round() as i64
}

#[test]
fn a_refusal_is_one_refusal_as_the_python_host_tells_it() {
    let vectors = vectors();
    let cases = vectors["signatures"].as_array().unwrap();
    assert!(cases.len() >= 40);
    for case in cases {
        assert_eq!(
            signature(text(&case["kind"]), text(&case["text"])),
            text(&case["signature"]),
            "{case}"
        );
    }
    for case in vectors["briefs"].as_array().unwrap() {
        assert_eq!(brief(text(&case["text"])), text(&case["brief"]), "{case}");
    }
}

#[test]
fn the_waits_and_the_count_are_the_python_hosts() {
    let vectors = vectors();
    assert_eq!(vectors["park_after"], PARK_AFTER);
    assert_eq!(vectors["refused_in_all"], REFUSED_IN_ALL);
    for case in vectors["waits"].as_array().unwrap() {
        let failures = u32::try_from(case["failures"].as_u64().unwrap()).unwrap();
        assert_eq!(wait_ms(failures), ms(&case["wait_s"]), "{case}");
    }
    for run in vectors["runs"].as_array().unwrap() {
        let mut refusals = Refusals::default();
        for (at, step) in run["steps"].as_array().unwrap().iter().enumerate() {
            let next = refusals.failed(text(&step["kind"]), text(&step["text"]));
            let what = format!("{}, step {at}", run["what"]);
            assert_eq!(u64::from(refusals.failures), step["failures"], "{what}");
            assert_eq!(u64::from(refusals.identical), step["identical"], "{what}");
            assert_eq!(refusals.error, text(&step["error"]), "{what}");
            // Every refusal counted here is one of the reference's `refusals` too.
            assert_eq!(u64::from(refusals.failures), step["refusals"], "{what}");
            let ends = step["ends"].as_bool().unwrap();
            assert_eq!(next == Next::End, ends, "{what}");
            if !ends {
                assert_eq!(
                    next,
                    Next::Wait {
                        ms: ms(&step["wait_s"])
                    },
                    "{what}"
                );
            }
        }
    }
    for case in vectors["notices"].as_array().unwrap() {
        let failures = u32::try_from(case["failures"].as_u64().unwrap()).unwrap();
        assert_eq!(
            notice(failures, text(&case["error"])),
            text(&case["notice"]),
            "{case}"
        );
    }
}

/// One turn by this Host's rule, from `at_ms`: its own try when it was sent to an agent that
/// was free, then its counted tries, until it starts (`started_at`, the number of tries that
/// are refused) or ends. A user's send at `asked_again_ms` asks for the agent again: the turn
/// is tried at once, on a count begun anew. Answers the tries' times and when it is over.
fn one_turn(
    at_ms: i64,
    own_try: bool,
    refused: usize,
    asked_again_ms: Option<i64>,
) -> (Vec<i64>, i64, bool) {
    one_turn_of(at_ms, own_try, refused, asked_again_ms, false)
}

/// [`one_turn`], its refusals of two kinds in turn where `alternate`.
fn one_turn_of(
    at_ms: i64,
    own_try: bool,
    refused: usize,
    asked_again_ms: Option<i64>,
    alternate: bool,
) -> (Vec<i64>, i64, bool) {
    let mut counted = 0usize;
    let mut kind = move || {
        counted += 1;
        if alternate && counted.is_multiple_of(2) {
            "Another"
        } else {
            "Refused"
        }
    };
    let mut tries = vec![at_ms];
    let mut now = at_ms;
    let mut refusals = Refusals::default();
    let mut next = if own_try {
        now + FIRST_WAIT_MS
    } else {
        match refusals.failed(kind(), "the same") {
            Next::Wait { ms } => now + ms,
            Next::End => return (tries, now, true),
        }
    };
    let mut asked = asked_again_ms;
    loop {
        if let Some(again) = asked.filter(|again| *again < next) {
            asked = None;
            refusals = Refusals::default();
            next = again;
        }
        now = next;
        tries.push(now);
        if tries.len() > refused {
            return (tries, now, false);
        }
        match refusals.failed(kind(), "the same") {
            Next::Wait { ms } => next = now + ms,
            Next::End => return (tries, now, true),
        }
    }
}

#[test]
fn the_stories_of_the_python_host_are_told_again_by_this_hosts_rule() {
    let vectors = vectors();
    let stories = vectors["stories"].as_array().unwrap();
    let story = |name: &str| stories.iter().find(|story| story["name"] == name).unwrap();
    let tries = |story: &Value, trace: &str| -> Vec<i64> {
        let tries = story["tries"].as_array().unwrap();
        tries
            .iter()
            .filter(|tried| tried["trace"] == trace)
            .map(|tried| ms(&tried["at"]))
            .collect()
    };
    let ended = |story: &Value, trace: &str| story["ended"][trace]["status"].clone();
    let never_run = |story: &Value, trace: &str| {
        let notice = &story["ended"][trace]["notice"];
        assert_eq!(
            (&notice["role"], &notice["origin"]),
            (&"assistant".into(), &"system".into())
        );
        assert!(text(&notice["text"]).starts_with("A message you sent was never run.\n\n"));
        let failures = if trace.starts_with("olga") {
            REFUSED_IN_ALL
        } else {
            PARK_AFTER
        };
        assert!(text(&notice["text"]).contains(&format!("{failures} times in a row")));
    };
    let forever = usize::MAX;

    // One send, refused every time: its own try, then eight counted ones, and it ends.
    let miso = story("miso");
    let (ours, _, ended_failed) = one_turn(0, true, forever, None);
    assert_eq!(tries(miso, "miso-1"), ours);
    assert_eq!(ours.len(), 1 + PARK_AFTER as usize);
    assert!(ended_failed);
    assert_eq!(ended(miso, "miso-1"), "failed");
    never_run(miso, "miso-1");

    // Two sends: the second is tried when the first is over, on a count of its own, and its
    // first try there is counted. (The Python Host also tried it once when it was sent, while
    // the first waited: this Host does not let it overtake.)
    let nora = story("nora");
    let (first, over_first, _) = one_turn(0, true, forever, None);
    assert_eq!(tries(nora, "nora-1"), first);
    let (second, _, second_failed) = one_turn(over_first, false, forever, None);
    let theirs = tries(nora, "nora-2");
    assert_eq!(theirs[0], 0, "the Python Host's own try of the second send");
    assert_eq!(theirs[1..], second);
    assert_eq!(second.len(), PARK_AFTER as usize);
    assert!(second_failed);
    assert_eq!(
        (ended(nora, "nora-1"), ended(nora, "nora-2")),
        ("failed".into(), "failed".into())
    );
    never_run(nora, "nora-2");

    // A user's send while the first waits asks for the agent again: the first is tried at
    // once, and counted from nothing.
    let pia = story("pia");
    let (first, over_first, _) = one_turn(0, true, forever, Some(10_000));
    assert_eq!(tries(pia, "pia-1"), first);
    let (second, _, _) = one_turn(over_first, false, forever, None);
    assert_eq!(tries(pia, "pia-2")[1..], second);

    // A send refused three times and then started: no more waits.
    let quin = story("quin");
    let (ours, _, failed) = one_turn(0, true, 3, None);
    assert_eq!(tries(quin, "quin-1"), ours);
    assert!(!failed);
    assert_eq!(ended(quin, "quin-1"), "started");

    // A send refused by two kinds in turn: never eight of one, so it ends at its twentieth
    // counted refusal (the release owner's second bound), its own try before them.
    let olga = story("olga");
    let (ours, _, failed) = one_turn_of(0, true, forever, None, true);
    assert_eq!(tries(olga, "olga-1"), ours);
    assert_eq!(ours.len(), 1 + REFUSED_IN_ALL as usize);
    assert!(failed);
    assert_eq!(ended(olga, "olga-1"), "failed");
    never_run(olga, "olga-1");
}
