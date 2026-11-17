//! The models an app's picker offers for a tool, held to what the Python Host's own functions
//! make of the same answers of the tools (`tests/fixtures/model-catalogue-vectors.json`,
//! written by `server-rs/tools/model-catalogue-vectors.py`).

#![allow(clippy::unwrap_used, clippy::expect_used)]

use clarp_backend::catalogue::{self, Found, Said};
use serde_json::{Value, json};

/// When the stories were looked at.
const AT: &str = "2027-01-15T08:00:00Z";
/// How many models Antigravity's answers in the vectors name, counted by the script that wrote
/// them.
const AGY_MODELS: usize = 15;

fn vectors() -> Value {
    serde_json::from_str(include_str!("fixtures/model-catalogue-vectors.json")).unwrap()
}

/// The same, and written the same: an app reads a row in the order it is written.
fn same(ours: &Value, theirs: &Value, what: &Value) {
    assert_eq!(ours, theirs, "{what}");
    assert_eq!(ours.to_string(), theirs.to_string(), "{what}, as text");
}

#[test]
fn what_codex_lists_is_read_as_the_python_host_reads_it() {
    let vectors = vectors();
    let mut models = 0;
    for row in vectors["codex"].as_array().unwrap() {
        let ours = catalogue::codex_models(row["said"].as_str().unwrap(), AT);
        same(&json!(ours), &row["models"], &row["name"]);
        models += ours.len();
    }
    assert_eq!(models, 18);
}

#[test]
fn what_claude_code_keeps_of_an_accounts_models_is_read_as_the_python_host_reads_it() {
    let vectors = vectors();
    let efforts = catalogue::declared("claude")["efforts"].clone();
    let mut models = 0;
    for row in vectors["claude"].as_array().unwrap() {
        let ours = catalogue::claude_models(row["kept"].as_str().unwrap(), &efforts, AT);
        same(&json!(ours), &row["models"], &row["name"]);
        models += ours.len();
    }
    assert_eq!(models, 28);
}

#[test]
fn what_antigravity_lists_is_read_as_the_python_host_reads_it() {
    let vectors = vectors();
    let mut models = 0;
    for row in vectors["agy"].as_array().unwrap() {
        let ours = catalogue::agy_models(row["said"].as_str().unwrap(), AT);
        same(&json!(ours), &row["models"], &row["name"]);
        models += ours.len();
    }
    assert_eq!(models, AGY_MODELS);
}

#[test]
fn a_tool_is_told_as_the_python_host_tells_it_wherever_it_is_and_whatever_it_says() {
    let vectors = vectors();
    let (mut told, mut there, mut seen) = (0, 0, 0);
    for story in vectors["stories"].as_array().unwrap() {
        let tool = story["tool"].as_str().unwrap();
        let program = format!("/synthetic/bin/{tool}");
        let version = story["version"].as_object().map(|said| Said {
            exit: i32::try_from(said["exit"].as_i64().unwrap()).unwrap(),
            out: said["out"].as_str().unwrap(),
            err: said["err"].as_str().unwrap(),
        });
        // Codex and Antigravity are asked by a command, whose answer counts where it ended
        // well; Claude Code keeps a file, which is there or is not.
        let models = match tool {
            "codex" | "agy" => story["models"]
                .as_object()
                .filter(|said| said["exit"] == 0)
                .map(|said| said["out"].as_str().unwrap()),
            _ => story["models"].as_str(),
        };
        let found = Found {
            program: story["there"]
                .as_bool()
                .unwrap()
                .then_some(program.as_str()),
            version,
            models,
        };
        let ours = catalogue::row(tool, &found, AT);
        same(&ours, &story["row"], &story["name"]);
        told += 1;
        there += usize::from(ours["installed"] == true);
        seen += usize::from(ours["availability"] == "available");
    }
    assert_eq!((told, there, seen), (22, 19, 11));
}

#[test]
fn the_whole_answer_is_shaped_as_the_python_hosts() {
    let vectors = vectors();
    let rows = vec![
        ("claude".to_string(), json!({ "id": "claude" })),
        ("codex".to_string(), json!({ "id": "codex" })),
    ];
    let whole = catalogue::whole(rows, AT);
    let theirs = &vectors["whole"];
    for key in ["schema_version", "observed_at", "freshness"] {
        assert_eq!(whole[key], theirs[key], "{key}");
    }
    for key in ["freshness_scope", "freshness_note"] {
        assert_eq!(whole[key], vectors["note"][key], "{key}");
    }
    let keys: Vec<&str> = whole
        .as_object()
        .unwrap()
        .keys()
        .map(String::as_str)
        .collect();
    assert_eq!(
        keys,
        [
            "schema_version",
            "observed_at",
            "freshness",
            "providers",
            "freshness_scope",
            "freshness_note"
        ]
    );
    // The tools in the order they were given, which is the order an app lists them in.
    let tools: Vec<&str> = whole["providers"]
        .as_object()
        .unwrap()
        .keys()
        .map(String::as_str)
        .collect();
    assert_eq!(tools, ["claude", "codex"]);
    // A tool this Host does not run has no row.
    assert_eq!(catalogue::row("grok", &Found::default(), AT), json!({}));
}
