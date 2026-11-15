//! The words for a tool call against the Python Host's.
//!
//! `fixtures/tool-vectors.json` is written by `server-rs/tools/tool-vectors.py` from
//! `classify_tool` and `tool_headline`: for each tool name and input, the category, the
//! label, the command and the line an app shows. The same calls must come to the same here.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use clarp_kernel::tools::{describe, headline};
use serde_json::Value;

#[test]
fn a_tool_call_is_named_as_the_python_host_names_it() {
    let vectors: Value = serde_json::from_str(include_str!("fixtures/tool-vectors.json")).unwrap();
    let vectors = vectors["vectors"].as_array().unwrap();
    let mut wrong = Vec::new();
    for vector in vectors {
        let name = vector["name"].as_str().unwrap();
        let described = describe(name, &vector["input"]);
        let ours = (
            described.category,
            described.label.as_str(),
            described.command.as_deref(),
            headline(name, described.category, &described.label),
        );
        let theirs = (
            vector["category"].as_str().unwrap(),
            vector["label"].as_str().unwrap(),
            vector["command"].as_str(),
            vector["headline"].as_str().unwrap().to_string(),
        );
        if ours != theirs {
            wrong.push(format!(
                "{name:?} with {}:\n    here {ours:?}\n    the Python Host's {theirs:?}",
                vector["input"]
            ));
        }
    }
    let shown: Vec<&String> = wrong.iter().take(25).collect();
    assert!(
        wrong.is_empty(),
        "{} of {} tool calls are named differently:\n{}",
        wrong.len(),
        vectors.len(),
        shown
            .iter()
            .map(|line| line.as_str())
            .collect::<Vec<_>>()
            .join("\n")
    );
    assert!(vectors.len() >= 1_000);
}
