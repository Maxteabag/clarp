//! The words for a tool call against the Python Host's.
//!
//! `fixtures/tool-vectors.json` is written by `server-rs/tools/tool-vectors.py` from
//! `classify_tool` and `tool_headline`: for each tool name and input, the category, the
//! label, the command and the line an app shows. The same calls must come to the same here.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use clarp_kernel::tools::{card, describe, headline, result_text, summarize};
use serde_json::Value;

#[test]
fn a_tools_use_is_the_card_the_python_host_makes_of_it() {
    let vectors: Value = serde_json::from_str(include_str!("fixtures/tool-vectors.json")).unwrap();
    let cards = vectors["cards"].as_array().unwrap();
    assert!(cards.len() >= 400);
    let mut wrong = Vec::new();
    for vector in cards {
        let name = vector["name"].as_str().unwrap();
        let ours = card(name, &vector["input"], vector["id"].as_str().unwrap());
        if ours != vector["card"] {
            wrong.push(format!(
                "{name:?} with {}:\n    here {ours}\n    the Python Host's {}",
                vector["input"], vector["card"]
            ));
        }
    }
    let results = vectors["results"].as_array().unwrap();
    assert!(results.len() >= 16);
    for vector in results {
        let ours = result_text(&vector["content"]);
        if ours != vector["text"].as_str().unwrap() {
            wrong.push(format!(
                "what {} returned: here {ours:?}, the Python Host's {}",
                vector["content"], vector["text"]
            ));
        }
    }
    let shown: Vec<&str> = wrong.iter().take(12).map(String::as_str).collect();
    assert!(
        wrong.is_empty(),
        "{} differ:\n{}",
        wrong.len(),
        shown.join("\n")
    );
}

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
        // The older summary, which the event of a tool state carries.
        let summed = summarize(name, &vector["input"]);
        let ours = (
            summed.action.as_str(),
            summed.summary.as_str(),
            summed.file_path.as_str(),
        );
        let theirs = &vector["activity"];
        let theirs = (
            theirs["action"].as_str().unwrap(),
            theirs["summary"].as_str().unwrap(),
            theirs["file_path"].as_str().unwrap(),
        );
        if ours != theirs {
            wrong.push(format!(
                "{name:?} with {}, summed up:\n    here {ours:?}\n    the Python Host's {theirs:?}",
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
