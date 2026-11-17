//! What is kept and shown of a Claude account's two windows, held to the Python Host's own
//! showing of the same word (`tests/fixtures/claude-usage-vectors.json`, written by
//! `server-rs/tools/claude-usage-vectors.py`): what an app's usage screen is shown, and
//! whether an agent's row says that the account is used up.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use clarp_kernel::quota::Tool;
use clarp_kernel::standing::{self, Standing};
use serde_json::{Value, json};

fn vectors() -> Value {
    serde_json::from_str(include_str!("fixtures/claude-usage-vectors.json")).unwrap()
}

#[test]
fn what_is_kept_and_shown_of_a_claude_account_is_what_the_python_host_keeps_and_shows() {
    let vectors = vectors();
    let began = vectors["began_ms"].as_i64().unwrap();
    let tool = Tool {
        instance: "a-host:claude",
        provider: "claude",
    };
    let (mut steps, mut askings, mut used_up, mut words, mut none, mut ended) = (0, 0, 0, 0, 0, 0);
    let stories = vectors["stories"].as_array().unwrap();
    for story in stories {
        let name = story["name"].as_str().unwrap();
        let mut kept = Standing::default();
        for (at, step) in story["steps"].as_array().unwrap().iter().enumerate() {
            let what = format!("{name}, step {at}");
            let now = began + step["at"].as_i64().unwrap() * 1000;
            if let Some(how) = step.get("ended") {
                // The keeper is told of the turns of its own tool that completed, and of no
                // other: that is all a Host tells it.
                if how["tool"] == "claude" && how["kind"] == "done" {
                    kept.completed(now);
                }
                ended += 1;
            } else if step.get("said").is_some() {
                // The windows as the Python Host's reader of the provider's answer made
                // them; an answer it refused has none.
                let windows = step.get("windows").cloned().unwrap_or(json!({}));
                let source = "anthropic-oauth-usage";
                let was_kept = kept.seen(&windows, step["on"].as_str(), source, now);
                assert_eq!(was_kept, step.get("refused").is_none(), "{what}: kept");
                words += usize::from(was_kept);
                none += usize::from(!was_kept);
            } else {
                let active = step["active"].as_str();
                let provider = kept.provider(tool, active, now);
                assert_eq!(provider, step["provider"], "{what}: the provider");
                let of_all = step["used_up"].as_object().unwrap();
                assert!(of_all.keys().all(|of| of == "claude"), "{what}: {of_all:?}");
                let theirs = of_all.get("claude").cloned();
                assert_eq!(kept.used_up(tool, active, now), theirs, "{what}: used up");
                used_up += usize::from(theirs.is_some());
                askings += 1;
            }
            steps += 1;
        }
    }
    // Counted by the script that wrote the vectors.
    assert_eq!((stories.len(), steps, askings, used_up), (26, 83, 46, 16));
    assert_eq!((words, none, ended), (29, 1, 7));
}

#[test]
fn a_word_is_kept_whole_and_survives_being_written_down() {
    let tool = Tool {
        instance: "a-host:claude",
        provider: "claude",
    };
    let mut kept = Standing::default();
    assert_eq!(kept.seen_at_ms(), None);
    let window = |used: f64| json!({ "used_percentage": used, "resets_at": "2030-03-17T21:46:40Z", "window_minutes": 300 });
    assert!(kept.seen(
        &json!({ "five_hour": window(40.0) }),
        Some("an-account"),
        "claude-stream",
        1_000
    ));
    assert_eq!(kept.seen_at_ms(), Some(1_000));
    // A word with nothing in it leaves what was kept.
    assert!(!kept.seen(&json!({}), Some("an-account"), "claude-stream", 2_000));
    assert!(!kept.seen(
        &json!({ "five_hour": { "resets_at": "" } }),
        None,
        "claude-stream",
        2_000
    ));
    assert_eq!(kept.seen_at_ms(), Some(1_000));
    let shown = kept.provider(tool, Some("an-account"), 1_000 + standing::FRESH_MS);
    assert_eq!(shown["source"]["detail"], "claude-stream");
    assert_eq!(shown["windows"][0]["used_percentage"], 40.0);
    let written = serde_json::to_string(&kept).unwrap();
    let read: Standing = serde_json::from_str(&written).unwrap();
    assert_eq!(read, kept);
}
