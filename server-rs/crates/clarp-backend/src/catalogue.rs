//! The models an app's picker offers for a tool, and how a tool is shown.
//!
//! An app asks a Host which tools it has and which models each can run
//! (`GET /agent-model-options`). What is known of a model comes from the tool itself where
//! it can be asked: Codex lists its models (`codex debug models`), and Claude Code keeps, in
//! its own file, the models the account that is signed in may run. Where a tool says
//! nothing, the models it is known to have are offered, and each row says where it came
//! from, so that an app can tell what was seen from what is assumed.
//!
//! The rules are the Python Host's (`server/lib/provider_capabilities.py`), held by
//! `tests/fixtures/model-catalogue-vectors.json`. What is declared of a tool (how it is
//! shown, the models it is known to have, the efforts it takes) is that Host's own table
//! (`server/lib/backend_catalogue.py`), written out for this one as `catalogue.json` by
//! `server-rs/tools/model-catalogue-vectors.py`. Nothing here starts a tool or reads a file:
//! what a tool answered is given to it.

use std::collections::BTreeSet;

use clarp_kernel::python;
use serde_json::{Map, Value, json};

/// What the Python Host declares of the tools this Host runs.
const TABLE: &str = include_str!("catalogue.json");

/// The version of the answer's shape, as the Python Host numbers it.
pub const SCHEMA_VERSION: u64 = 2;
/// What the answer says of what its freshness means.
pub const FRESHNESS_SCOPE: &str = "response_observation";
pub const FRESHNESS_NOTE: &str = "Top-level freshness describes cache age only; inspect each \
                                  provider and model source for evidence freshness.";
/// The names Claude Code takes for a model by itself.
const CLAUDE_ALIASES: [&str; 6] = ["sonnet", "opus", "haiku", "fable", "best", "opusplan"];
/// The sign that parts what a model is from what is said in its praise.
const POINT: char = '\u{b7}';

/// What is declared of a tool. Empty for a tool this Host does not run.
pub fn declared(tool: &str) -> Value {
    let table: Value = serde_json::from_str(TABLE).unwrap_or_default();
    table[tool].clone()
}

/// Where something that is said of a tool comes from, and how fresh it is.
fn source(kind: &str, detail: &str, observed_at: &str, freshness: &str) -> Value {
    json!({ "kind": kind, "detail": detail, "observed_at": observed_at, "freshness": freshness })
}

fn model(id: &str, label: &str, default: Value, efforts: Value, source: &Value) -> Value {
    json!({
        "id": id, "label": label, "default_effort": default, "supported_efforts": efforts,
        "source": source,
    })
}

/// A text without the blanks around it, where a value is a text.
fn word(value: &Value) -> Option<&str> {
    value.as_str().map(python::strip)
}

/// The first of several values that Python takes for something (`a or b or c`).
fn first_said<'a>(entry: &'a Map<String, Value>, keys: &[&str]) -> Option<&'a Value> {
    keys.iter()
        .filter_map(|key| entry.get(*key))
        .find(|value| python::truthy(value))
}

/// The models Codex lists (`codex debug models`): those it shows in a list, each under the
/// name it gives it, with the efforts it says the model takes. What Codex does not say is
/// not made up: a model without a list of efforts has none known (`null`), and an empty
/// list is Codex saying that it takes none.
pub fn codex_models(said: &str, observed_at: &str) -> Vec<Value> {
    let said = if said.is_empty() { "{}" } else { said };
    let Ok(Value::Object(payload)) = serde_json::from_str::<Value>(said) else {
        return Vec::new();
    };
    let Some(Value::Array(items)) = payload.get("models") else {
        return Vec::new();
    };
    let source = source("cli_probe", "codex debug models", observed_at, "fresh");
    let mut seen: BTreeSet<String> = BTreeSet::new();
    let mut out = Vec::new();
    for item in items {
        let Value::Object(item) = item else { continue };
        if item.get("visibility") != Some(&json!("list")) {
            continue;
        }
        let Some(id) = item.get("slug").and_then(word) else {
            continue;
        };
        if id.is_empty() || seen.contains(id) {
            continue;
        }
        let label = match item.get("display_name").map(word) {
            Some(Some(label)) if !label.is_empty() => label,
            _ => id,
        };
        let default = match item.get("default_reasoning_level").and_then(word) {
            Some(level) if !level.is_empty() => json!(level.to_lowercase()),
            _ => Value::Null,
        };
        let efforts = match item.get("supported_reasoning_levels") {
            Some(Value::Array(levels)) => {
                let mut read: Vec<String> = Vec::new();
                for level in levels {
                    let effort = level.get("effort").and_then(word).map(str::to_lowercase);
                    match effort {
                        Some(effort) if !effort.is_empty() && !read.contains(&effort) => {
                            read.push(effort);
                        }
                        _ => {}
                    }
                }
                // An empty list says that no effort is taken. A list of which nothing can
                // be read says nothing.
                if read.is_empty() && !levels.is_empty() {
                    Value::Null
                } else {
                    json!(read)
                }
            }
            _ => Value::Null,
        };
        out.push(model(id, label, default, efforts, &source));
        seen.insert(id.to_string());
    }
    out
}

/// Whether Claude Code takes a name for a model: one of its own short names, or a name of
/// a model of Claude's, either with the mark of the long context after it.
fn claude_takes(id: &str) -> bool {
    let id = python::strip(id);
    let id = match id.len().checked_sub(4) {
        Some(at) if id.is_char_boundary(at) && id[at..].eq_ignore_ascii_case("[1m]") => &id[..at],
        _ => id,
    };
    CLAUDE_ALIASES.contains(&id) || id.starts_with("claude-")
}

/// The models the account that is signed in to Claude Code may run, as Claude Code's own
/// file keeps them (`kept`: the text of that file). Claude Code has no command that lists
/// its models; the file is the nearest thing to one, and holds what a list that ships with
/// a Host cannot know, such as a model an account was let in to early. `efforts`: the
/// efforts Claude Code takes, which is what each of them is said to take.
pub fn claude_models(kept: &str, efforts: &Value, observed_at: &str) -> Vec<Value> {
    let kept = if kept.is_empty() { "{}" } else { kept };
    let Ok(Value::Object(payload)) = serde_json::from_str::<Value>(kept) else {
        return Vec::new();
    };
    let source = source(
        "cli_cache",
        "claude account model cache",
        observed_at,
        "fresh",
    );
    let mut seen: BTreeSet<String> = BTreeSet::new();
    let mut out = Vec::new();
    for key in ["modelAccessCache", "additionalModelOptionsCache"] {
        let entries: Vec<&Value> = match payload.get(key) {
            Some(Value::Object(by_name)) => by_name.values().collect(),
            Some(Value::Array(entries)) => entries.iter().collect(),
            _ => continue,
        };
        for entry in entries {
            let (id, label) = match entry {
                Value::String(name) => (python::strip(name), python::strip(name).to_string()),
                Value::Object(entry) => {
                    let id = first_said(entry, &["value", "id", "model"]);
                    let label = first_said(entry, &["label", "display_name", "name"]);
                    let mut label = label.and_then(word).unwrap_or_default().to_string();
                    // A short name alone does not say which model it is; what the tool
                    // says of it does, before the point.
                    if let Some(said) = entry.get("description").and_then(Value::as_str)
                        && !python::strip(said).is_empty()
                    {
                        let which = python::strip(said.split(POINT).next().unwrap_or_default());
                        if !which.is_empty() {
                            label = which.to_string();
                        }
                    }
                    (id.and_then(word).unwrap_or_default(), label)
                }
                _ => continue,
            };
            // The file also holds what is no model: a notice to update is kept there as
            // one. Only what Claude Code takes for a model is offered.
            if id.is_empty() || seen.contains(id) || !claude_takes(id) {
                continue;
            }
            let label = if label.is_empty() { id } else { &label };
            out.push(model(id, label, Value::Null, efforts.clone(), &source));
            seen.insert(id.to_string());
        }
    }
    out
}

/// Whether a text is a model's name as Antigravity writes them: small letters and digits,
/// in parts joined by a point or a dash, the first beginning with a letter.
fn agy_names(id: &str) -> bool {
    let plain = |part: &str| {
        !part.is_empty()
            && part
                .bytes()
                .all(|sign| sign.is_ascii_lowercase() || sign.is_ascii_digit())
    };
    let mut parts = id.split(['.', '-']);
    let first = parts.next().unwrap_or_default();
    first.starts_with(|sign: char| sign.is_ascii_lowercase()) && plain(first) && parts.all(plain)
}

/// The models Antigravity lists (`agy models`): a name and, after a tab, what the model is
/// called; a name alone is called by itself. What is no name of a model is passed over:
/// the words it writes before its list, an empty line. A model's name carries its effort,
/// so none is told beside it.
pub fn agy_models(said: &str, observed_at: &str) -> Vec<Value> {
    let from = source("cli_probe", "agy models", observed_at, "fresh");
    let mut seen: BTreeSet<&str> = BTreeSet::new();
    let mut models = Vec::new();
    for line in python::splitlines(said) {
        let (id, label) = match line.split_once('\t') {
            Some((id, label)) => (python::strip(id), python::strip(label)),
            None => (python::strip(line), ""),
        };
        if !agy_names(id) || !seen.insert(id) {
            continue;
        }
        let label = if label.is_empty() { id } else { label };
        models.push(model(id, label, Value::Null, Value::Null, &from));
    }
    models
}

/// The plain-text list printed by Grok Build, or Muse's cached provider rows.
fn grok_models(raw: &str, efforts: &Value, at: &str) -> Vec<Value> {
    let from = source("cli_probe", "grok models", at, "fresh");
    let mut seen = BTreeSet::new();
    let mut rows = Vec::new();
    for line in python::splitlines(raw) {
        let line = python::strip(line);
        if line.starts_with("You are") || line.starts_with("Default ") || line.to_ascii_lowercase().starts_with("available models") {
            continue;
        }
        let line = python::strip(line.trim_start_matches(['*', '-', '\u{2022}', ' ']));
        let line = if line.to_ascii_lowercase().ends_with("(default)") { &line[..line.len()-9] } else { line };
        if let Some(id) = line.split_whitespace().next()
            && !matches!(id.to_ascii_lowercase().as_str(), "available" | "models:")
            && seen.insert(id) {
            rows.push(model(id, id, Value::Null, efforts.clone(), &from));
        }
    }
    rows
}

fn muse_models(raw: &str, order: &Value, at: &str) -> Vec<Value> {
    let from = source("cli_cache", "muse model catalogue cache", at, "fresh");
    let payload: Value = serde_json::from_str(raw).unwrap_or_default();
    let mut seen = BTreeSet::new();
    let mut models = Vec::new();
    for row in payload["rows"].as_array().into_iter().flatten() {
        let id = row["model_id"].as_str().map(python::strip).unwrap_or("");
        if row["visibility"] != "visible" || id.is_empty() || !seen.insert(id) { continue; }
        let variants = &row["reasoning_effort_variants"];
        let efforts: Vec<_> = order.as_array().into_iter().flatten().filter(|effort|
            variants.as_array().into_iter().flatten().any(|v| &v["tier"] == *effort)).collect();
        let default = if efforts.iter().any(|v| **v == "high") { json!("high") } else { Value::Null };
        let efforts = if efforts.is_empty() { Value::Null } else { json!(efforts) };
        models.push(model(id, row["display_label"].as_str().filter(|l| !l.is_empty()).unwrap_or(id), default, efforts, &from));
    }
    models
}

/// What a tool said when it was started for one answer: how it ended and what it printed.
#[derive(Debug, Clone, Copy)]
pub struct Said<'a> {
    pub exit: i32,
    pub out: &'a str,
    /// What it wrote aside, where that was kept.
    pub err: &'a str,
}

/// A tool as it was found on a machine.
#[derive(Debug, Clone, Copy, Default)]
pub struct Found<'a> {
    /// Where its program is. `None`: it is not on this machine.
    pub program: Option<&'a str>,
    /// What it said of its version. `None`: it could not be started, or was not asked.
    pub version: Option<Said<'a>>,
    /// What it said of its models, as its kind of asking gives it: for a tool that is
    /// asked by a command, what the command printed where it ended well; for one that
    /// keeps a file, the text of the file. `None`: nothing could be had.
    pub models: Option<&'a str>,
}

/// The models a tool was seen to have, from what it said of them.
fn seen_models(tool: &str, declared: &Value, said: Option<&str>, observed_at: &str) -> Vec<Value> {
    match (tool, said) {
        ("codex", Some(said)) => codex_models(said, observed_at),
        ("claude", Some(kept)) => claude_models(kept, &declared["efforts"], observed_at),
        ("agy", Some(said)) => agy_models(said, observed_at),
        ("grok", Some(said)) => grok_models(said, &declared["efforts"], observed_at),
        ("muse", Some(said)) => muse_models(said, &declared["efforts"], observed_at),
        _ => Vec::new(),
    }
}

/// A tool's row of the catalogue: how it is shown, whether it is there and in which
/// version, and its models: those it was seen to have, and after them those it is known to
/// have and was not seen to. Empty for a tool this Host does not run.
pub fn row(tool: &str, found: &Found, observed_at: &str) -> Value {
    let declared = declared(tool);
    let Some(shown) = declared["shown"].as_object() else {
        return json!({});
    };
    let asked = &declared["asked"];
    let not_seen = |detail: &str| source("not_observed", detail, observed_at, "unknown");
    let (version, version_source) = match found.program {
        None => (
            Value::Null,
            not_seen(&format!("{tool} executable unavailable")),
        ),
        Some(program) => {
            let detail = format!("{program} --version");
            // What it printed, or what it wrote aside where it printed nothing at all:
            // the first line of that.
            let said = found.version.filter(|said| said.exit == 0).map(|said| {
                let all = if said.out.is_empty() {
                    said.err
                } else {
                    said.out
                };
                let lines = python::splitlines(python::strip(all));
                let line = lines.first().copied().unwrap_or_default();
                python::first(python::strip(line), 200).to_string()
            });
            match said {
                Some(version) if !version.is_empty() => (
                    json!(version),
                    source("cli_probe", &detail, observed_at, "fresh"),
                ),
                _ => (Value::Null, not_seen(&detail)),
            }
        }
    };
    let installed = found.program.is_some();
    let said = found.models.filter(|_| installed);
    let mut models = seen_models(tool, &declared, said, observed_at);
    let seen = !models.is_empty();
    // What is known of the tool without asking it, after what it was seen to have.
    let known_source = source(
        "static_fallback",
        "bundled compatibility catalog",
        observed_at,
        "unknown",
    );
    let listed: BTreeSet<String> = models
        .iter()
        .filter_map(|model| model["id"].as_str().map(str::to_string))
        .collect();
    for known in declared["models"].as_array().into_iter().flatten() {
        let id = known["id"].as_str().unwrap_or_default();
        if seen && asked["known_too"] != true {
            break;
        }
        if !listed.contains(id) {
            let label = known["label"].as_str().unwrap_or_default();
            let default = known["default_effort"].clone();
            let efforts = known["supported_efforts"].clone();
            models.push(model(id, label, default, efforts, &known_source));
        }
    }
    let availability = match (installed, seen) {
        (false, _) => "unavailable",
        (true, true) => "available",
        (true, false) => "unknown",
    };
    let catalogue_source = if seen {
        let kind = asked["source_kind"].as_str().unwrap_or("cli_probe");
        let detail = asked["detail"].as_str().unwrap_or_default();
        source(kind, detail, observed_at, "fresh")
    } else {
        known_source
    };
    // Whether efforts are each model's own, the whole tool's, or a flag of the tool whose
    // fit with a model nobody knows.
    let scope = declared["effort_scope"].as_str().unwrap_or("provider");
    let efforts = declared["efforts"]
        .as_array()
        .filter(|efforts| !efforts.is_empty());
    let mut row = Map::new();
    row.insert("id".to_string(), json!(tool));
    row.extend(shown.clone());
    let more = json!({
        "installed": installed,
        "availability": availability,
        "cli_version": version,
        "cli_version_source": version_source,
        // Whether the tool is signed in is told where the logins are told, and not joined
        // into this answer: said so in the Python Host's words.
        "authenticated": null,
        "authentication_source": not_seen("backend_auth status is not joined in P1"),
        "supported_efforts": efforts.filter(|_| scope != "model"),
        "supported_efforts_scope": (scope == "provider_flag").then_some("provider_flag"),
        "model_effort_compatibility": (scope == "provider_flag").then_some("unknown"),
        "models": models,
        "source": catalogue_source,
    });
    if let Value::Object(more) = more {
        row.extend(more);
    }
    Value::Object(row)
}

/// The whole answer: the rows of the tools, by their names, with when they were found.
pub fn whole(rows: Vec<(String, Value)>, observed_at: &str) -> Value {
    let providers: Map<String, Value> = rows.into_iter().collect();
    json!({
        "schema_version": SCHEMA_VERSION,
        "observed_at": observed_at,
        "freshness": "fresh",
        "providers": providers,
        "freshness_scope": FRESHNESS_SCOPE,
        "freshness_note": FRESHNESS_NOTE,
    })
}
