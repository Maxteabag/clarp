//! A tool's use as it is read for an explanation.
//!
//! A use comes from a turn or from an app as whatever was known of it: a name, a command,
//! what the tool was given, lines of what it looked at. Of that only a bounded part is
//! read: the fields that say what the use is, each cut to a length, with what looks like a
//! secret taken out and with what a heredoc only feeds to a script or a file withheld
//! ([`super::shapes::redact`]). Everything after this reading sees only that.
//!
//! The rules are the Python Host's (`server/lib/tool_explanations.py`, `snippet` and
//! `normalize_activity`), held by `use` and `read` of every row of
//! `tests/fixtures/tool-explanation-vectors.json`.

use std::sync::LazyLock;

use regex::Regex;
use serde_json::{Map, Value};

use super::{pattern, shapes, text};
use crate::python;

/// How much of a field is read.
const FIELD: usize = 1600;
/// How much of a line of what a tool looked at is read, and how many lines.
const LINE: usize = 240;
const LINES: usize = 6;

/// What follows a name that says a secret follows: `token=`, `api_key:`, `Authorization:
/// Bearer`.
static SECRET: LazyLock<Regex> = LazyLock::new(|| {
    pattern(concat!(
        r#"(?i)((?:authorization["']?\s*[:=]\s*["']?bearer"#,
        r#"|(?:api[_-]?key|token|password|secret)["']?\s*[=:])\s*["']?)[^\s"';]+"#,
    ))
});
/// A key as a provider writes one.
static PROVIDER_KEY: LazyLock<Regex> = LazyLock::new(|| pattern(r"\bsk-[A-Za-z0-9_-]{12,}"));

/// What is read of a value: a text as it is, anything else as Python writes it, cut to
/// `limit` signs, with secrets taken out.
pub fn snippet(value: &Value, limit: usize) -> String {
    let written = match value {
        Value::String(said) => said.clone(),
        other => python::dumps(other),
    };
    // A secret that the cut would halve is still found whole.
    let cut = python::first(&written, limit + 200);
    let hidden = SECRET.replace_all(cut, "${1}[redacted]");
    let hidden = PROVIDER_KEY.replace_all(&hidden, "[redacted]");
    python::first(&hidden, limit).to_string()
}

/// A value that is a command, with what its heredocs hold withheld; any other value as it
/// is.
fn withheld(value: &Value) -> Value {
    match value {
        Value::String(command) => text(&shapes::redact(command)),
        other => other.clone(),
    }
}

/// The bounded fields the explainer reads of a use.
pub fn normalize(activity: &Map<String, Value>) -> Map<String, Value> {
    let said = |source: &Map<String, Value>, key: &str| {
        let value = source.get(key);
        value.filter(|value| python::truthy(value)).cloned()
    };
    let commanded = activity.get("kind").and_then(Value::as_str) == Some("command");
    let mut read = Map::new();
    for key in [
        "kind",
        "name",
        "title",
        "summary",
        "description",
        "command",
        "file_path",
        "path",
        "pattern",
    ] {
        let Some(value) = said(activity, key) else {
            continue;
        };
        let a_command = key == "command" || (key == "summary" && commanded);
        let value = if a_command { withheld(&value) } else { value };
        read.insert(key.to_string(), text(&snippet(&value, FIELD)));
    }
    match activity.get("input") {
        Some(Value::Object(given)) => {
            let mut selected = Map::new();
            for key in [
                "command",
                "cmd",
                "code",
                "path",
                "file_path",
                "pattern",
                "query",
                "description",
            ] {
                let Some(value) = said(given, key) else {
                    continue;
                };
                let a_command = key == "command" || key == "cmd";
                let value = if a_command { withheld(&value) } else { value };
                selected.insert(key.to_string(), text(&snippet(&value, FIELD)));
            }
            if !selected.is_empty() {
                read.insert("input".to_string(), Value::Object(selected));
            }
        }
        Some(given @ Value::String(_)) => {
            read.insert("input".to_string(), text(&snippet(given, FIELD)));
        }
        _ => {}
    }
    if let Some(Value::Array(operations)) = activity.get("operations") {
        let lines: Vec<Value> = operations
            .iter()
            .take(LINES)
            .filter(|line| line.as_str().is_some_and(|line| !line.is_empty()))
            .map(|line| text(&snippet(line, LINE)))
            .collect();
        if !lines.is_empty() {
            read.insert("operations".to_string(), Value::Array(lines));
        }
    }
    read
}

/// The endings a script that an agent runs is known by.
const SCRIPTS: [&str; 8] = [".js", ".mjs", ".cjs", ".py", ".sh", ".bash", ".ts", ".rb"];
/// How much of a script is shown to whoever explains the call that runs it.
const SCRIPT: usize = 6000;

/// The scripts a use names: every word of its command that ends as a script does. What a
/// script holds is part of what the call does, so whoever explains the call is shown the
/// beginning of it; which of these are read, and how, is the Host's (only what lies in the
/// agent's own directory, plainly).
pub fn script_names(activity: &Map<String, Value>) -> Vec<String> {
    let said = |source: &Map<String, Value>, key: &str| match source.get(key) {
        None => String::new(),
        Some(Value::String(said)) => said.clone(),
        Some(other) => python::repr(other),
    };
    let mut lines: Vec<String> = ["command", "summary", "name"]
        .iter()
        .map(|key| said(activity, key))
        .collect();
    match activity.get("input") {
        Some(Value::String(given)) => lines.push(given.clone()),
        Some(Value::Object(given)) => {
            lines.extend(["cmd", "command"].iter().map(|key| said(given, key)));
        }
        _ => lines.push(String::new()),
    }
    let words = crate::tools::shell_split(&lines.join("\n")).unwrap_or_default();
    let a_script = |word: &String| {
        let name = word.rsplit('/').next().unwrap_or_default();
        let ending = name.rfind('.').filter(|at| *at > 0).map(|at| &name[at..]);
        ending.is_some_and(|ending| SCRIPTS.contains(&ending))
    };
    words.into_iter().filter(a_script).collect()
}

/// What whoever explains a call is shown of a script it runs: its name and its beginning.
pub fn script_excerpt(name: &str, beginning: &str) -> Value {
    serde_json::json!({
        "file": name, "source_excerpt": snippet(&text(beginning), SCRIPT), "excerpt_only": true,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn a_secret_is_taken_out_of_what_is_read() {
        let read = |said: &str| snippet(&json!(said), FIELD);
        assert_eq!(read("token=abcdef123456 after"), "token=[redacted] after");
        assert_eq!(read("a sk-abcdefghijklmnop b"), "a [redacted] b");
        assert_eq!(read("no secret here"), "no secret here");
        assert_eq!(
            snippet(&json!({"a": [1, null]}), FIELD),
            r#"{"a": [1, null]}"#
        );
    }

    #[test]
    fn what_a_heredoc_feeds_a_script_is_withheld_from_what_is_read() {
        let command = "python3 - <<'PY'\nprint('a secret plan')\nPY";
        let activity = json!({"name": "Bash", "command": command, "ignored": "x"});
        let read = normalize(activity.as_object().unwrap_or(&Map::new()));
        assert_eq!(
            Value::Object(read),
            json!({"name": "Bash", "command": "python3 - <<'EOF'\n\u{2026}\nEOF"})
        );
    }
}
