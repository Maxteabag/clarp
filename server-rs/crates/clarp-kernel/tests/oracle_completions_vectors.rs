//! What agents finished for the user, as the Oracle's voice is shown it, against the Python
//! Host's own.
//!
//! `fixtures/oracle-completions-vectors.json` is written by
//! `server-rs/tools/oracle-completions-vectors.py` from
//! `server/lib/oracle_attention.pending_completion_notifications`, run on a database of that
//! program's own: for every case what the Host has of each turn it kept the end of, and
//! what it shows of them to whoever asks for so many, at such a moment. The same must come
//! of the same here, also the order a record's fields are written in. And the name the end
//! of a turn is told under, which is the same on both Hosts.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use clarp_kernel::notify;
use clarp_kernel::oracle::attention;
use serde_json::Value;

fn vectors() -> Value {
    serde_json::from_str(include_str!("fixtures/oracle-completions-vectors.json")).unwrap()
}

#[test]
fn what_agents_finished_is_shown_as_the_python_host_shows_it() {
    let vectors = vectors();
    let now = vectors["now"].as_i64().unwrap();
    assert_eq!(vectors["most"], attention::PENDING_MOST);
    let (mut turns, mut askings, mut shown, mut old, mut refused) = (0, 0, 0, 0, 0);
    for case in vectors["cases"].as_array().unwrap() {
        let name = case["name"].as_str().unwrap();
        let had = case["had"].as_array().unwrap();
        turns += had.len();
        for ask in case["asks"].as_array().unwrap() {
            askings += 1;
            // Asked for so many; where nothing is said of how many, for as many as a voice
            // is shown at most. What is no count of anything cannot be asked for here, and
            // the Python Host refuses it.
            let most = match &ask["limit"] {
                Value::Null => Some(attention::PENDING_MOST),
                limit => limit.as_u64().map(|most| usize::try_from(most).unwrap()),
            };
            let Some(most) = most else {
                assert_eq!(
                    ask["raises"], "limit must be a non-negative integer",
                    "{name}: {}",
                    ask["limit"]
                );
                refused += 1;
                continue;
            };
            let at = now + ask["later"].as_i64().unwrap();
            let got = attention::finished(had, at, most);
            let expected = ask["shown"].as_array().unwrap();
            let written =
                |rows: &[Value]| -> Vec<String> { rows.iter().map(Value::to_string).collect() };
            assert_eq!(
                written(&got),
                written(expected),
                "{name}: asked for {most}, {} ms later",
                ask["later"]
            );
            shown += got.len();
            old += got.iter().filter(|row| row["stale"] == true).count();
        }
    }
    assert_eq!((turns, askings, shown, old, refused), (178, 31, 141, 45, 4));
}

#[test]
fn the_end_of_a_turn_is_told_under_the_name_the_python_host_gives_it() {
    let vectors = vectors();
    let named = vectors["named"].as_array().unwrap();
    for one in named {
        let agent = one["agent_id"].as_str().unwrap();
        let id = notify::id(agent, one["done_ts"].as_i64().unwrap());
        assert_eq!(one["id"], id.as_str(), "{agent:?}");
    }
    assert_eq!(named.len(), 7);
}

#[test]
fn only_what_the_user_or_the_oracle_began_is_the_users_own() {
    for (origin, own) in [
        ("user", true),
        ("oracle", true),
        ("agent", false),
        ("schedule", false),
        ("watcher", false),
        ("janitor", false),
        ("", false),
        ("User", false),
        (" user", false),
    ] {
        assert_eq!(attention::for_the_user(origin), own, "{origin:?}");
    }
}
