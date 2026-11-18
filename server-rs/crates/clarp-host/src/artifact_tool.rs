//! `clarp-agent-artifacts`: an agent's own tool for what it makes and what it asks.
//!
//! The command line is the Python Host's `scripts/agent_artifacts.py`, argument for argument:
//! skills call it by name. There the tool is a client of the Host's routes; here the Host
//! answers it from the same functions its routes call ([`crate::library`]), so the tool and
//! the routes cannot disagree.
//!
//! One thing it says more than the Python tool: when the Host refuses, the Python tool
//! prints the status alone ("HTTP Error 409: Conflict"); this one adds why.

use std::collections::{BTreeMap, BTreeSet};

use axum::http::StatusCode;
use clarp_kernel::artifacts::reports;
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};

use crate::edge::{ApiError, App};
use crate::library;
use crate::tools::{NOT_HERE, ToolAnswer, ToolCall};

const USAGE: &str = "usage: agent_artifacts.py create SESSION TYPE TITLE [SUMMARY] [JSON_PAYLOAD] | \
publish-render-spec REQUEST_FILE [--validate-only] | \
create-form SESSION TITLE HTML_FILE SCHEMA_FILE --version V --artifact-id ID | \
create-report SESSION TITLE HTML_FILE [--summary S] [--artifact-id ID] [--version V] | \
form-events ARTIFACT_ID [--draft-key KEY] [--after N] [--limit N] [--output JSONL] [--overwrite] | \
report-history ARTIFACT_ID [--version V] [--limit N] [--offset N] | \
decision SESSION TITLE QUESTION YES_LABEL NO_LABEL [JSON_PAYLOAD] [OPTIONS] | \
question SESSION TITLE QUESTION JSON_OPTIONS [OPTIONS] | \
input SESSION TITLE PROMPT [--hint one_time_code] [--expires-in S] [OPTIONS] | \
wait ARTIFACT_ID [--timeout S] | \
attention [--session SESSION] [--include-archived] | withdraw SESSION DECISION_ID | \
answer-on-behalf SESSION DECISION_ID EXPECTED_REVISION JSON_BODY | \
update ARTIFACT_ID STATUS [JSON_PAYLOAD] | progress ARTIFACT_ID VALUE [CONTENT] | list SESSION";

/// What the options every request to the user takes are called.
const ASK_VALUED: [&str; 7] = [
    "--context",
    "--reference",
    "--priority-reason",
    "--urgency",
    "--effort",
    "--deadline-at",
    "--expires-at",
];

/// A command line as it was read: what stands for itself, and what was said with a name.
#[derive(Debug, Default, PartialEq)]
pub(crate) struct Parsed {
    pub(crate) positional: Vec<String>,
    pub(crate) options: BTreeMap<String, String>,
    pub(crate) flags: BTreeSet<String>,
}

impl Parsed {
    pub(crate) fn option(&self, name: &str) -> Option<&str> {
        self.options.get(name).map(String::as_str)
    }

    /// An option that has to be one of a few words.
    pub(crate) fn choice(
        &self,
        name: &str,
        among: &[&str],
        otherwise: &str,
    ) -> Result<String, String> {
        let given = self.option(name).unwrap_or(otherwise);
        if among.contains(&given) {
            return Ok(given.to_string());
        }
        let known: Vec<String> = among.iter().map(|word| format!("'{word}'")).collect();
        Err(format!(
            "argument {name}: invalid choice: '{given}' (choose from {})",
            known.join(", ")
        ))
    }

    /// An option that has to be a whole number.
    pub(crate) fn whole(&self, name: &str) -> Result<Option<i64>, String> {
        match self.option(name) {
            None => Ok(None),
            Some(given) => given
                .trim()
                .parse()
                .map(Some)
                .map_err(|_| format!("argument {name}: invalid int value: '{given}'")),
        }
    }
}

/// Reads a command line: options with a value, flags, and everything else in its order.
/// `names` are what the things that stand for themselves are called, the ones that may be
/// left out in brackets.
pub(crate) fn parse(
    args: &[String],
    valued: &[&str],
    flags: &[&str],
    names: &[&str],
) -> Result<Parsed, String> {
    let mut parsed = Parsed::default();
    let mut args = args.iter();
    while let Some(arg) = args.next() {
        let (name, attached) = match arg.split_once('=') {
            Some((name, value)) if name.starts_with("--") => (name, Some(value.to_string())),
            _ => (arg.as_str(), None),
        };
        if valued.contains(&name) {
            let Some(value) = attached.or_else(|| args.next().cloned()) else {
                return Err(format!("argument {name}: expected one argument"));
            };
            parsed.options.insert(name.to_string(), value);
        } else if flags.contains(&name) && attached.is_none() {
            parsed.flags.insert(name.to_string());
        } else if arg.starts_with("--") {
            return Err(format!("unrecognized arguments: {arg}"));
        } else {
            parsed.positional.push(arg.clone());
        }
    }
    let required: Vec<&str> = names
        .iter()
        .copied()
        .filter(|name| !name.starts_with('['))
        .collect();
    if parsed.positional.len() < required.len() {
        let missing = &required[parsed.positional.len()..];
        return Err(format!(
            "the following arguments are required: {}",
            missing.join(", ")
        ));
    }
    if parsed.positional.len() > names.len() {
        let extra = &parsed.positional[names.len()..];
        return Err(format!("unrecognized arguments: {}", extra.join(" ")));
    }
    Ok(parsed)
}

/// A refusal of the Host as the tool says it: the status as the Python tool prints it, and
/// why.
fn refusal(error: &ApiError) -> String {
    let status = error.status();
    format!(
        "HTTP Error {}: {}: {}",
        status.as_u16(),
        status.canonical_reason().unwrap_or("Error"),
        error.message()
    )
}

fn json_object(given: &str, what: &str) -> Result<Map<String, Value>, String> {
    match serde_json::from_str(given).map_err(|error| error.to_string())? {
        Value::Object(fields) => Ok(fields),
        _ => Err(format!("{what} must be a JSON object")),
    }
}

/// The options of a question as the tool checks them before it asks.
fn question_options(given: &str) -> Result<(Vec<Value>, Vec<String>), String> {
    let options: Value = serde_json::from_str(given).map_err(|error| error.to_string())?;
    let Some(list) = options
        .as_array()
        .filter(|list| (2..=3).contains(&list.len()))
    else {
        return Err("questions require two or three options".to_string());
    };
    let mut normalized = Vec::new();
    let mut ids: Vec<String> = Vec::new();
    for option in list {
        let known = |key: &String| matches!(key.as_str(), "id" | "label" | "description");
        let Some(fields) = option.as_object().filter(|fields| fields.keys().all(known)) else {
            return Err("each option must contain id, label and optional description".to_string());
        };
        let mut entry = Map::new();
        for key in ["id", "label", "description"] {
            let none = json!("");
            let value = fields.get(key).unwrap_or(&none).as_str();
            let Some(value) =
                value.filter(|value| key == "description" || !value.trim().is_empty())
            else {
                return Err("each option needs nonempty string id and label".to_string());
            };
            entry.insert(key.to_string(), json!(value.trim()));
        }
        let id = entry["id"].as_str().unwrap_or("").to_string();
        if ids.contains(&id) {
            return Err("option IDs must be unique".to_string());
        }
        ids.push(id);
        normalized.push(Value::Object(entry));
    }
    Ok((normalized, ids))
}

/// `decision`, `question` and `input`: what is asked of the user, as a body for the Host.
fn ask_body(cmd: &str, args: &[String], now_ms: i64) -> Result<(Map<String, Value>, bool), String> {
    let mut valued: Vec<&str> = ASK_VALUED.to_vec();
    valued.push("--expires-in");
    let names: &[&str] = match cmd {
        "decision" => &[
            "session",
            "title",
            "question",
            "yes_label",
            "no_label",
            "[payload_json]",
        ],
        "input" => {
            valued.extend(["--hint", "--payload"]);
            &["session", "title", "question"]
        }
        _ => {
            valued.extend(["--recommend", "--payload"]);
            &["session", "title", "question", "options_json"]
        }
    };
    let parsed = parse(args, &valued, &["--blocks-progress", "--dry-run"], names)?;
    let at = |index: usize| parsed.positional.get(index).map_or("", String::as_str);
    let payload_json = match cmd {
        "decision" => parsed.positional.get(5).map_or("{}", String::as_str),
        _ => parsed.option("--payload").unwrap_or("{}"),
    };
    let hint = parsed.choice("--hint", &["text", "one_time_code"], "text")?;
    let urgency = parsed.choice("--urgency", &["normal", "time_sensitive"], "normal")?;
    let effort = parsed.choice("--effort", &["quick", "short", "review"], "review")?;
    let deadline_at = parsed.whole("--deadline-at")?;
    let mut expires_at = parsed.whole("--expires-at")?;
    let expires_in = parsed.whole("--expires-in")?;
    let payload = json_object(payload_json, "payload")?;
    if at(2).trim().is_empty() {
        return Err("question must not be blank".to_string());
    }
    let blocks_progress = parsed.flags.contains("--blocks-progress");
    let priority_reason = parsed.option("--priority-reason").unwrap_or("");
    if (blocks_progress || urgency != "normal") && priority_reason.trim().is_empty() {
        return Err("blocking or time-sensitive requests require --priority-reason".to_string());
    }
    let mut body = Map::new();
    for (key, value) in [
        ("session", json!(at(0))),
        ("title", json!(at(1))),
        ("question", json!(at(2))),
        ("payload", Value::Object(payload)),
        ("context", json!(parsed.option("--context").unwrap_or(""))),
        (
            "reference_id",
            json!(parsed.option("--reference").unwrap_or("")),
        ),
        ("blocks_progress", json!(blocks_progress)),
        ("priority_reason", json!(priority_reason)),
        ("urgency", json!(urgency)),
        ("response_effort", json!(effort)),
    ] {
        body.insert(key.to_string(), value);
    }
    if let Some(seconds) = expires_in {
        if expires_at.is_some() {
            return Err("use --expires-in or --expires-at, not both".to_string());
        }
        if seconds <= 0 {
            return Err("--expires-in must be positive seconds".to_string());
        }
        expires_at = Some(now_ms + seconds * 1_000);
    }
    for (key, value) in [("deadline_at", deadline_at), ("expires_at", expires_at)] {
        if let Some(value) = value {
            if value <= 0 {
                return Err(format!("{key} must be positive epoch milliseconds"));
            }
            body.insert(key.to_string(), json!(value));
        }
    }
    match cmd {
        "decision" => {
            body.insert("yes_label".to_string(), json!(at(3)));
            body.insert("no_label".to_string(), json!(at(4)));
        }
        "input" => {
            body.insert("response_type".to_string(), json!("text_input"));
            body.insert("input_hint".to_string(), json!(hint));
        }
        _ => {
            let (options, ids) = question_options(at(3))?;
            let recommended = parsed.option("--recommend");
            if recommended.is_some_and(|id| !ids.iter().any(|known| known == id)) {
                return Err("--recommend must identify an existing option".to_string());
            }
            body.insert("response_type".to_string(), json!("single_choice"));
            body.insert("options".to_string(), json!(options));
            body.insert("allow_custom_text".to_string(), json!(true));
            body.insert("recommended_option_id".to_string(), json!(recommended));
        }
    }
    Ok((body, parsed.flags.contains("--dry-run")))
}

/// `wait ARTIFACT_ID [--timeout S] [--interval F]`: how the request on an artifact ended,
/// once it has. While it has not, the tool is told to ask again in a moment, until its
/// time is up: exit 0 for an answer, 3 for an end without one, 4 for time up.
fn wait(app: &App, args: &[String], call: &ToolCall) -> Result<ToolAnswer, String> {
    let parsed = parse(args, &["--timeout", "--interval"], &[], &["artifact_id"])?;
    let artifact_id = parsed.positional[0].clone();
    let timeout = parsed.whole("--timeout")?.unwrap_or(600).max(1);
    let interval: f64 = match parsed.option("--interval") {
        None => 2.0,
        Some(given) => given
            .trim()
            .parse()
            .map_err(|_| format!("argument --interval: invalid float value: '{given}'"))?,
    };
    let Some(artifact) = library::find(app, &artifact_id) else {
        return Err("HTTP Error 404: Not Found: artifact not found".to_string());
    };
    let decision = &artifact["decision"];
    let status = decision["status"].as_str().unwrap_or("");
    if !status.is_empty() && status != "pending" {
        let outcome = json!({
            "status": status,
            "answer": decision["answer"],
            "resolved_choice": decision["resolved_choice"],
            "decision_id": decision["decision_id"],
            "artifact_id": artifact_id,
        });
        let answered = matches!(status, "answered" | "accepted" | "rejected");
        return Ok(ToolAnswer::printed(
            if answered { 0 } else { 3 },
            outcome.to_string(),
        ));
    }
    let now = app.clock.now_ms();
    let started = if call.started_ms > 0 {
        call.started_ms
    } else {
        now
    };
    if now >= started + timeout * 1_000 {
        let pending = json!({ "status": "pending", "timeout": true, "artifact_id": artifact_id });
        return Ok(ToolAnswer::printed(4, pending.to_string()));
    }
    let again_ms = (interval.max(0.5) * 1_000.0) as u64;
    Ok(ToolAnswer::again(again_ms))
}

/// The id a report gets when the agent names none: its title in plain signs, and a number
/// only this title with this page has. The same report published again is the same report.
fn report_id(title: &str, content: &str) -> String {
    let mut slug = String::new();
    let mut gap = false;
    for sign in title.to_lowercase().chars() {
        if sign.is_ascii_lowercase() || sign.is_ascii_digit() {
            if gap && !slug.is_empty() {
                slug.push('-');
            }
            slug.push(sign);
            gap = false;
        } else {
            gap = true;
        }
    }
    let slug: String = slug.chars().take(48).collect();
    let slug = match slug.trim_matches('-') {
        "" => "report",
        slug => slug,
    };
    let digest: String = Sha256::digest(format!("{title}\0{content}").as_bytes())
        .iter()
        .take(6)
        .map(|byte| format!("{byte:02x}"))
        .collect();
    format!("report-{slug}-{digest}")
}

/// Why a file could not be read, as the Python tool says it.
fn unread(error: &std::io::Error, file: &str) -> String {
    let said = error.to_string();
    let what = said.split(" (os error").next().unwrap_or(&said);
    match error.raw_os_error() {
        Some(code) => format!("[Errno {code}] {what}: '{file}'"),
        None => format!("{what}: '{file}'"),
    }
}

/// What a page may reach can be said several times: the origins that were said, and the
/// other arguments.
fn connects(rest: &[String]) -> Result<(Vec<String>, Vec<String>), String> {
    let (mut connect, mut others) = (Vec::new(), Vec::new());
    let mut args = rest.iter();
    while let Some(arg) = args.next() {
        if arg == "--connect" {
            let Some(origin) = args.next() else {
                return Err("argument --connect: expected one argument".to_string());
            };
            connect.push(origin.clone());
        } else if let Some(origin) = arg.strip_prefix("--connect=") {
            connect.push(origin.to_string());
        } else {
            others.push(arg.clone());
        }
    }
    Ok((connect, others))
}

/// A file of the agent's, read where the tool was called, as text.
fn text_of(call: &ToolCall, file: &str, what: &str) -> Result<String, String> {
    let content = std::fs::read(std::path::Path::new(&call.cwd).join(file))
        .map_err(|error| unread(&error, file))?;
    String::from_utf8(content).map_err(|_| format!("{what} is not text in UTF-8: '{file}'"))
}

/// `create-form SESSION TITLE HTML_FILE SCHEMA_FILE --version V --artifact-id ID
/// [--summary S] [--connect HTTPS_ORIGIN]... [--dry-run]`: a page that asks its user for
/// answers, with the schema that says what an answer is. The Host reads both files where
/// the tool was called. A form is made once: its id is the agent's to choose and to keep.
async fn create_form(app: &App, call: &ToolCall, rest: &[String]) -> Result<Value, String> {
    let (connect, others) = connects(rest)?;
    let names = ["session", "title", "html_file", "schema_file"];
    let parsed = parse(
        &others,
        &["--summary", "--artifact-id", "--version"],
        &["--dry-run"],
        &["[session]", "[title]", "[html_file]", "[schema_file]"],
    )?;
    // What is missing is said at once, what has a place first.
    let mut missing: Vec<&str> = names[parsed.positional.len().min(names.len())..].to_vec();
    for option in ["--version", "--artifact-id"] {
        if parsed.option(option).is_none() {
            missing.push(option);
        }
    }
    if !missing.is_empty() {
        return Err(format!(
            "the following arguments are required: {}",
            missing.join(", ")
        ));
    }
    let network = reports::connect_origins(&json!(connect)).map_err(|refused| refused.message)?;
    let (session, title) = (&parsed.positional[0], &parsed.positional[1]);
    let content = text_of(call, &parsed.positional[2], "the form's HTML")?;
    let schema = text_of(call, &parsed.positional[3], "the form's schema")?;
    let schema: Value = serde_json::from_str(&schema)
        .map_err(|error| format!("the form's schema is not JSON: {error}"))?;
    let mut payload = json!({
        "content": content,
        "version": parsed.option("--version").unwrap_or(""),
        "answer_schema": schema,
    });
    if !connect.is_empty() {
        payload["connect_origins"] = json!(network);
    }
    let body = json!({
        "session": session, "title": title, "type": "html_form",
        "summary": parsed.option("--summary").unwrap_or(""),
        "artifact_id": parsed.option("--artifact-id").unwrap_or(""), "payload": payload,
    });
    if parsed.flags.contains("--dry-run") {
        return Ok(json!({ "method": "POST", "path": "/artifacts", "body": body }));
    }
    let asked = body.as_object().cloned().unwrap_or_default();
    library::create(app, &asked)
        .await
        .map_err(|error| refusal(&error))
}

/// `create-report SESSION TITLE HTML_FILE [--summary S] [--artifact-id ID] [--version V]
/// [--connect HTTPS_ORIGIN]... [--dry-run]`: a page of HTML is handed to the user. The Host
/// reads the page where the tool was called: the file is the agent's, on this machine.
/// Under an id that is this agent's report already, the report is revised.
async fn create_report(app: &App, call: &ToolCall, rest: &[String]) -> Result<Value, String> {
    let (connect, others) = connects(rest)?;
    let parsed = parse(
        &others,
        &["--summary", "--artifact-id", "--version"],
        &["--dry-run"],
        &["session", "title", "html_file"],
    )?;
    let network = reports::connect_origins(&json!(connect)).map_err(|refused| refused.message)?;
    let (session, title, file) = (
        &parsed.positional[0],
        &parsed.positional[1],
        &parsed.positional[2],
    );
    let content = text_of(call, file, "the report's HTML")?;
    if content.trim().is_empty() {
        return Err("report HTML is empty".to_string());
    }
    let version = parsed.option("--version").unwrap_or("1");
    let artifact_id = match parsed.option("--artifact-id") {
        Some(given) if !given.is_empty() => given.to_string(),
        _ => report_id(title, &content),
    };
    let mut payload = json!({ "content": content, "version": version, "read_only": true });
    if !connect.is_empty() {
        payload["connect_origins"] = json!(network);
    }
    let mut body = json!({
        "session": session, "title": title, "type": "html_form",
        "summary": parsed.option("--summary").unwrap_or(""),
        "artifact_id": artifact_id, "payload": payload,
    });
    if parsed.flags.contains("--dry-run") {
        return Ok(json!({ "method": "POST", "path": "/artifacts", "body": body }));
    }
    let asked = |body: &Value| body.as_object().cloned().unwrap_or_default();
    let taken = match library::create(app, &asked(&body)).await {
        Ok(made) => return Ok(made),
        Err(error) if error.status() == StatusCode::CONFLICT => error,
        Err(error) => return Err(refusal(&error)),
    };
    // The id is taken. By this agent's report: then this is its next version, which follows
    // the one it has now. One try: a change that came in between is not written over.
    let Some(current) = library::find(app, &artifact_id) else {
        let gone = ApiError::new(StatusCode::NOT_FOUND, "artifact not found");
        return Err(refusal(&gone));
    };
    if current["session"] != session.as_str() || current["read_only"] != true {
        return Err("the artifact ID does not identify this agent's read-only report".to_string());
    }
    if current["version"] == version {
        return Err(refusal(&taken));
    }
    body["expected_version"] = current["version"].clone();
    library::create(app, &asked(&body))
        .await
        .map_err(|error| refusal(&error))
}

/// `report-history ARTIFACT_ID [--version V] [--limit N] [--offset N]`: what a report was.
async fn report_history(app: &App, rest: &[String]) -> Result<Value, String> {
    let parsed = parse(
        rest,
        &["--version", "--limit", "--offset"],
        &[],
        &["artifact_id"],
    )?;
    let number = |name: &str, otherwise: i64| match parsed.option(name) {
        None => Ok(otherwise),
        Some(given) => given
            .trim()
            .parse::<i64>()
            .map_err(|_| format!("argument {name}: invalid int value: '{given}'")),
    };
    let (limit, offset) = (number("--limit", 50)?, number("--offset", 0)?);
    let version = parsed.option("--version");
    library::report_history(
        app,
        &parsed.positional[0],
        version,
        limit.clamp(1, 100),
        offset.max(0),
    )
    .await
    .map_err(|error| refusal(&error))
}

/// `publish-render-spec REQUEST_FILE [--validate-only]`: a whole request to publish a render
/// spec, as the agent wrote it, is sent as its bytes, so that the Host's validator is the
/// only one: a key written twice or a number's spelling is judged there. A refusal is
/// printed as the Host gives it, on standard error.
async fn publish_render_spec(app: &App, call: &ToolCall, rest: &[String]) -> ToolAnswer {
    let failed = |why: String| ToolAnswer::said(1, format!("agent_artifacts: {why}"));
    let parsed = match parse(rest, &[], &["--validate-only"], &["request_file"]) {
        Ok(parsed) => parsed,
        Err(why) => return failed(why),
    };
    let file = &parsed.positional[0];
    let bytes = match std::fs::read(std::path::Path::new(&call.cwd).join(file)) {
        Ok(bytes) => bytes,
        Err(error) => return failed(unread(&error, file)),
    };
    let validate_only = parsed.flags.contains("--validate-only");
    match library::posted(app, &bytes, validate_only, false).await {
        Ok((_, answer)) => ToolAnswer::printed(0, answer.to_string()),
        Err(error) => {
            let told = matches!(error.status().as_u16(), 400 | 403 | 409);
            match error.detail().filter(|_| told) {
                Some(detail) => ToolAnswer::said(1, json!({ "error": detail }).to_string()),
                None => failed(refusal(&error)),
            }
        }
    }
}

/// Answers the tool.
pub async fn run(app: &App, call: &ToolCall) -> ToolAnswer {
    let failed = |why: String| ToolAnswer::said(1, format!("agent_artifacts: {why}"));
    let Some((cmd, rest)) = call.argv.split_first() else {
        return failed("list index out of range".to_string());
    };
    let printed = |value: &Value| ToolAnswer::printed(0, value.to_string());
    let result: Result<Value, String> = match (cmd.as_str(), rest.len()) {
        ("form-events", _) => {
            return ToolAnswer::said(
                NOT_HERE,
                format!(
                    "clarp-agent-artifacts {cmd}: not available on this Host yet (the Rust \
                     Host). Nothing was done."
                ),
            );
        }
        ("publish-render-spec", _) => return publish_render_spec(app, call, rest).await,
        ("create-form", _) => create_form(app, call, rest).await,
        ("create-report", _) => create_report(app, call, rest).await,
        ("report-history", _) => report_history(app, rest).await,
        ("create", 3..=5) => {
            let payload = match rest.get(4) {
                Some(given) => serde_json::from_str(given).map_err(|error| error.to_string()),
                None => Ok(json!({})),
            };
            match payload {
                Err(why) => Err(why),
                Ok(payload) => {
                    let body = json!({
                        "session": rest[0], "type": rest[1], "title": rest[2],
                        "summary": rest.get(3).map_or("", String::as_str), "payload": payload,
                    });
                    let body = body.as_object().cloned().unwrap_or_default();
                    library::create(app, &body)
                        .await
                        .map_err(|error| refusal(&error))
                }
            }
        }
        ("decision" | "question" | "input", _) => match ask_body(cmd, rest, app.clock.now_ms()) {
            Err(why) => Err(why),
            Ok((body, true)) => Ok(json!({ "method": "POST", "path": "/decisions", "body": body })),
            Ok((body, false)) => library::request(app, &body)
                .await
                .map_err(|error| refusal(&error)),
        },
        ("wait", _) => match wait(app, rest, call) {
            Ok(answer) => return answer,
            Err(why) => Err(why),
        },
        ("attention", _) => {
            parse(rest, &["--session"], &["--include-archived"], &[]).map(|parsed| {
                let mut inbox =
                    library::inbox(app, true, parsed.flags.contains("--include-archived"));
                if let Some(session) = parsed.option("--session") {
                    let items = inbox["items"].as_array().cloned().unwrap_or_default();
                    let of_session: Vec<Value> = items
                        .into_iter()
                        .filter(|item| item["session"] == session)
                        .collect();
                    inbox["count"] = json!(of_session.len());
                    inbox["items"] = json!(of_session);
                }
                inbox
            })
        }
        ("withdraw", 2) => {
            let body = json!({ "session": rest[0] });
            let body = body.as_object().cloned().unwrap_or_default();
            let rest = format!("{}/withdraw", rest[1]);
            match library::decide(app, &rest, false, &body).await {
                Ok(done) => Ok(done["artifact"].clone()),
                Err(error) => Err(refusal(&error)),
            }
        }
        ("answer-on-behalf", _) => {
            let names = ["session", "decision_id", "expected_revision", "body_json"];
            parse(rest, &[], &[], &names).and_then(|parsed| {
                let revision: i64 = parsed.positional[2].trim().parse().map_err(|_| {
                    let given = &parsed.positional[2];
                    format!("argument expected_revision: invalid int value: '{given}'")
                })?;
                let body = json_object(&parsed.positional[3], "answer body")?;
                if body.contains_key("choice") == body.contains_key("answer") {
                    return Err(
                        "answer body needs exactly one of \"choice\" or \"answer\"".to_string()
                    );
                }
                if revision < 1 {
                    return Err("expected_revision must be positive".to_string());
                }
                Err(
                    "this Host does not support answering on the user's behalf; relay the \
                     answer and let the asking agent withdraw its card instead"
                        .to_string(),
                )
            })
        }
        ("update", 2..=3) => {
            let payload = match rest.get(2) {
                Some(given) => serde_json::from_str::<Value>(given)
                    .map(Some)
                    .map_err(|error| error.to_string()),
                None => Ok(None),
            };
            match payload {
                Err(why) => Err(why),
                Ok(payload) => {
                    let mut body = Map::new();
                    body.insert("status".to_string(), json!(rest[1]));
                    if let Some(payload) = payload {
                        body.insert("payload".to_string(), payload);
                    }
                    match library::change(app, &rest[0], body).await {
                        Ok(done) => Ok(done["artifact"].clone()),
                        Err(error) => Err(refusal(&error)),
                    }
                }
            }
        }
        ("progress", 2..=3) => match rest[1].trim().parse::<f64>() {
            Err(_) => Err(format!("could not convert string to float: '{}'", rest[1])),
            Ok(progress) => {
                let mut patch = Map::new();
                patch.insert("progress".to_string(), json!(progress));
                if let Some(content) = rest.get(2) {
                    patch.insert("content".to_string(), json!(content));
                }
                let body = json!({ "status": "active", "payload_patch": patch });
                let body = body.as_object().cloned().unwrap_or_default();
                match library::change(app, &rest[0], body).await {
                    Ok(done) => Ok(done["artifact"].clone()),
                    Err(error) => Err(refusal(&error)),
                }
            }
        },
        ("list", 1) => Ok(library::of_session(app, &rest[0]).await),
        _ => return ToolAnswer::said(2, USAGE),
    };
    match result {
        Ok(value) => printed(&value),
        Err(why) => failed(why),
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use super::*;

    fn args(given: &[&str]) -> Vec<String> {
        given.iter().map(|arg| (*arg).to_string()).collect()
    }

    #[test]
    fn a_command_line_is_read_with_its_options_anywhere() {
        let line = args(&[
            "vera",
            "--context",
            "Why",
            "Title",
            "--blocks-progress",
            "Q?",
            "--effort=short",
        ]);
        let parsed = parse(
            &line,
            &["--context", "--effort"],
            &["--blocks-progress"],
            &["session", "title", "question", "[payload]"],
        )
        .unwrap();
        assert_eq!(parsed.positional, ["vera", "Title", "Q?"]);
        assert_eq!(parsed.option("--context"), Some("Why"));
        assert_eq!(parsed.option("--effort"), Some("short"));
        assert!(parsed.flags.contains("--blocks-progress"));
        let names = ["session", "title"];
        assert_eq!(
            parse(&args(&["vera"]), &[], &[], &names).unwrap_err(),
            "the following arguments are required: title"
        );
        assert_eq!(
            parse(&args(&["a", "b", "c"]), &[], &[], &names).unwrap_err(),
            "unrecognized arguments: c"
        );
        assert_eq!(
            parse(&args(&["a", "b", "--what"]), &[], &[], &names).unwrap_err(),
            "unrecognized arguments: --what"
        );
        assert_eq!(
            parse(&args(&["a", "b", "--context"]), &["--context"], &[], &names).unwrap_err(),
            "argument --context: expected one argument"
        );
    }

    #[test]
    fn what_is_asked_becomes_the_body_the_python_tool_sends() {
        let options = r#"[{"id":"keep","label":"Keep it","description":"Smallest"},{"id":"new","label":" New "}]"#;
        let line = args(&[
            "vera",
            "Choose",
            "Which one?",
            options,
            "--recommend",
            "keep",
            "--blocks-progress",
            "--effort",
            "short",
            "--priority-reason",
            "Nothing else can go on",
            "--expires-in",
            "60",
        ]);
        let (body, dry) = ask_body("question", &line, 1_000_000).unwrap();
        assert!(!dry);
        assert_eq!(
            Value::Object(body),
            json!({
                "session": "vera", "title": "Choose", "question": "Which one?", "payload": {},
                "context": "", "reference_id": "", "blocks_progress": true,
                "priority_reason": "Nothing else can go on", "urgency": "normal",
                "response_effort": "short", "expires_at": 1_060_000,
                "response_type": "single_choice",
                "options": [
                    { "id": "keep", "label": "Keep it", "description": "Smallest" },
                    { "id": "new", "label": "New", "description": "" },
                ],
                "allow_custom_text": true, "recommended_option_id": "keep",
            })
        );
        let line = args(&[
            "vera",
            "Deploy?",
            "Deploy 1.2?",
            "Deploy",
            "Hold",
            r#"{"version":"1.2"}"#,
            "--dry-run",
        ]);
        let (body, dry) = ask_body("decision", &line, 0).unwrap();
        assert!(dry);
        assert_eq!(
            (&body["yes_label"], &body["payload"]),
            (&json!("Deploy"), &json!({ "version": "1.2" }))
        );
        let line = args(&["vera", "Code", "The code?", "--hint", "one_time_code"]);
        let (body, _) = ask_body("input", &line, 0).unwrap();
        assert_eq!(
            (&body["response_type"], &body["input_hint"]),
            (&json!("text_input"), &json!("one_time_code"))
        );
    }

    #[test]
    fn what_cannot_be_asked_is_said_before_anything_is_sent() {
        let ask = |cmd: &str, line: &[&str]| ask_body(cmd, &args(line), 1_000).unwrap_err();
        assert_eq!(
            ask("decision", &["vera", "T", "  ", "Y", "N"]),
            "question must not be blank"
        );
        assert_eq!(
            ask(
                "decision",
                &["vera", "T", "Q?", "Y", "N", "--blocks-progress"]
            ),
            "blocking or time-sensitive requests require --priority-reason"
        );
        assert_eq!(
            ask("decision", &["vera", "T", "Q?", "Y", "N", "[1]"]),
            "payload must be a JSON object"
        );
        assert_eq!(
            ask(
                "decision",
                &["vera", "T", "Q?", "Y", "N", "--urgency", "now"]
            ),
            "argument --urgency: invalid choice: 'now' (choose from 'normal', 'time_sensitive')"
        );
        assert_eq!(
            ask(
                "input",
                &["vera", "T", "Q?", "--expires-in", "5", "--expires-at", "9"]
            ),
            "use --expires-in or --expires-at, not both"
        );
        assert_eq!(
            ask("input", &["vera", "T", "Q?", "--expires-in", "0"]),
            "--expires-in must be positive seconds"
        );
        assert_eq!(
            ask(
                "question",
                &["vera", "T", "Q?", r#"[{"id":"a","label":"A"}]"#]
            ),
            "questions require two or three options"
        );
        let two = r#"[{"id":"a","label":"A"},{"id":"a","label":"B"}]"#;
        assert_eq!(
            ask("question", &["vera", "T", "Q?", two]),
            "option IDs must be unique"
        );
        let ok = r#"[{"id":"a","label":"A"},{"id":"b","label":"B"}]"#;
        assert_eq!(
            ask("question", &["vera", "T", "Q?", ok, "--recommend", "c"]),
            "--recommend must identify an existing option"
        );
        assert_eq!(
            ask("question", &["vera", "T", "Q?"]),
            "the following arguments are required: options_json"
        );
    }
}
