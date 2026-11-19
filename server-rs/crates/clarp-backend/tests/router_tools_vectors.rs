//! How the router of spoken messages asks each tool of the Host one question, held to how the Python Host's
//! router asks it (`tests/fixtures/router-tools-vectors.json` of the kernel, written by
//! `server-rs/tools/router-tools-vectors.py` from the Python Host's own `routing_cmd`, `routing_env` and
//! `routing_text`).
//!
//! Where the question goes (on standard input, in a file, or on the command line) is not held here: it is the
//! port of the Python Host's PR 437 (`routing_input`), and the arguments are compared without it. Five things
//! this Host does otherwise, on purpose, and each is named where it is put right below:
//!
//! - Codex is given its effort as a TOML string (`model_reasoning_effort="high"`), where the Python Host
//!   writes it bare: Codex reads both as the same, and a quoted value cannot be read as something else.
//!
//! - Claude Code is asked with none of its user's MCP servers (`--strict-mcp-config`): a question has no use
//!   for them, and would wait for them to start.
//! - Antigravity is asked for its answer as one JSON object that says how the question ended
//!   (`--output-format json`): a question it could not answer is told as such, not read as an answer. It is
//!   given an effort only where no model is named, and in small letters, as its turns are: a model's name
//!   carries its effort.
//! - OpenCode and DeepSeek are asked with OpenCode's own question tool denied (`OPENCODE_PERMISSION`), as
//!   their turns are: nobody is there to answer it.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use clarp_backend::{Question, by_id};
use serde_json::Value;

fn vectors() -> Value {
    serde_json::from_str(include_str!(
        "../../clarp-kernel/tests/fixtures/router-tools-vectors.json"
    ))
    .unwrap()
}

fn texts(value: &Value) -> Vec<String> {
    let items = value.as_array().unwrap().iter();
    items
        .map(|item| item.as_str().unwrap().to_string())
        .collect()
}

/// What the Python Host's command for a question would be on this Host, but for where the question goes.
fn put_right(tool: &str, model: &str, effort: &str, theirs: Vec<String>) -> Vec<String> {
    let mut want = theirs;
    match tool {
        "codex" => {
            let at = want
                .iter()
                .position(|arg| arg.starts_with("model_reasoning_effort="));
            if let Some(at) = at {
                want[at] = format!(
                    "model_reasoning_effort={}",
                    Value::String(effort.to_string())
                );
            }
        }
        "claude" => {
            let at = want
                .iter()
                .position(|arg| arg == "--no-session-persistence");
            want.insert(at.unwrap() + 1, "--strict-mcp-config".to_string());
        }
        "agy" => {
            want.splice(1..1, ["--output-format".to_string(), "json".to_string()]);
            if let Some(at) = want.iter().position(|arg| arg == "--effort") {
                if model.is_empty() {
                    want[at + 1] = effort.trim().to_lowercase();
                } else {
                    want.drain(at..at + 2);
                }
            }
        }
        _ => {}
    }
    want
}

#[test]
fn every_tool_is_asked_one_question_as_the_python_hosts_router_asks_it() {
    let vectors = vectors();
    let prompt = vectors["prompt"].as_str().unwrap();
    let (mut tools, mut commands) = (0, 0);
    for tool in vectors["tools"].as_array().unwrap() {
        let id = tool["id"].as_str().unwrap();
        let backend = by_id(id).unwrap_or_else(|| panic!("{id}: no such tool here"));
        assert_eq!(
            backend.efforts(),
            texts(&tool["efforts"]),
            "{id}: its efforts"
        );
        // A home of its own for one question: the one tool the Python Host gives one (`HOME`).
        let own = texts(&tool["env"]) == ["HOME"];
        assert_eq!(backend.own_home().is_some(), own, "{id}: a home of its own");
        let (linked, copied) = backend.own_home().map_or((vec![], vec![]), |home| {
            let all = |files: &[&str]| {
                files
                    .iter()
                    .map(|file| (*file).to_string())
                    .collect::<Vec<_>>()
            };
            (all(home.linked), all(home.copied))
        });
        assert_eq!(
            linked,
            texts(&tool["home"]["linked"]),
            "{id}: what is linked there"
        );
        assert_eq!(
            copied,
            texts(&tool["home"]["copied"]),
            "{id}: what is copied there"
        );
        for command in tool["commands"].as_array().unwrap() {
            let (model, effort) = (
                command["model"].as_str().unwrap(),
                command["effort"].as_str().unwrap(),
            );
            // As the router gives them: what is set, and nothing where nothing is.
            let given = |value: &str| Some(value.to_string()).filter(|value| !value.is_empty());
            let question = Question {
                prompt: prompt.to_string(),
                model: given(model),
                effort: given(effort),
            };
            let launch = backend
                .once(&question)
                .unwrap_or_else(|| panic!("{id}: it cannot be asked"));
            let what = format!("{id}, model {model:?}, effort {effort:?}");
            assert_eq!(
                launch.program,
                command["program"].as_str().unwrap(),
                "{what}: its program"
            );
            // The arguments without the question, and without what says it is on standard input.
            let mut ours = launch.args.clone();
            ours.retain(|arg| arg != prompt && arg != "-");
            let mut theirs = texts(&command["args"]);
            theirs.retain(|arg| arg != prompt);
            assert_eq!(ours, put_right(id, model, effort, theirs), "{what}");
            // Where the question went, it went whole: on standard input, or as one argument.
            let on_stdin = launch.stdin == prompt.as_bytes();
            let in_args = launch.args.iter().any(|arg| arg.contains(prompt));
            assert!(on_stdin != in_args, "{what}: the question once");
            // Nothing set in the tool's environment, but OpenCode's question tool, which is denied.
            let names: Vec<&str> = launch.env.iter().map(|(name, _)| name.as_str()).collect();
            let denied: &[&str] = match id {
                "opencode" | "deepseek" => &["OPENCODE_PERMISSION"],
                _ => &[],
            };
            assert_eq!(names, denied, "{what}: its environment");
            commands += 1;
        }
        tools += 1;
    }
    assert_eq!((tools, commands), (7, 240));
}

#[test]
fn the_answer_is_found_where_the_python_hosts_router_finds_it() {
    let vectors = vectors();
    let mut read = 0;
    for tool in vectors["tools"].as_array().unwrap() {
        let id = tool["id"].as_str().unwrap();
        let backend = by_id(id).unwrap();
        for text in tool["texts"].as_array().unwrap() {
            let printed = text["printed"].as_str().unwrap();
            let ours = backend.answered(printed);
            if id == "agy" {
                // Asked for one JSON object that says how it ended, Antigravity's plain words are none.
                let answer = text["answer"].as_str().unwrap();
                assert_eq!(
                    answer, printed,
                    "the Python Host takes agy's words as they are"
                );
                assert!(ours.is_err(), "agy, {printed:?}: {ours:?}");
                read += 1;
                continue;
            }
            let theirs = match (text.get("answer"), text.get("refused")) {
                (Some(answer), _) => Ok(answer.as_str().unwrap().to_string()),
                (None, Some(refused)) => Err(refused.as_str().unwrap().to_string()),
                (None, None) => panic!("{id}: neither"),
            };
            assert_eq!(ours, theirs, "{id}, {printed:?}");
            read += 1;
        }
    }
    assert_eq!(read, 13);
}

#[test]
fn antigravity_says_how_its_answer_ended() {
    let agy = by_id("agy").unwrap();
    let ended = |status: &str, response: &str| {
        let printed =
            format!("Warming up...\n{{\"status\": \"{status}\", \"response\": {response:?}}}");
        agy.answered(&printed)
    };
    assert_eq!(
        ended("SUCCESS", "{\"kind\": \"ignored\"}"),
        Ok("{\"kind\": \"ignored\"}".to_string())
    );
    assert!(
        ended("ERROR", "half")
            .unwrap_err()
            .starts_with("agy status \"ERROR\"")
    );
    assert_eq!(
        ended("SUCCESS", "  "),
        Err("agy returned no answer".to_string())
    );
}
