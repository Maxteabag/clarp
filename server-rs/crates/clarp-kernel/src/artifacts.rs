//! Artifacts: what an agent made for its user, and what it asks its user.
//!
//! A document, a table, a change to the code, a run of a workflow, a plan: each is kept as a
//! record of its kind with what its kind needs, shown in the apps beside the conversation,
//! and changed by the agent as the work goes on. A request to the user (an approval, a
//! question, something to type) is an artifact too, with a decision on it: see
//! [`decisions`](self::decisions).
//!
//! These are the rules of the Python Host's `server/lib/artifacts.py`, word for word in what
//! they refuse, held to it by `tests/fixtures/artifact-vectors.json`
//! (`server-rs/tools/artifact-vectors.py`). An artifact belongs to the agent that made it
//! and is kept in that agent's log; what the apps ask across agents (the library, the inbox)
//! is answered from the agents' books together, by the functions here that take several.
//!
//! A recording, a film and a file are artifacts that name a file their agent published
//! ([`crate::media`]): one of its own, and as what it is. A report is a page of HTML
//! ([`reports`]): the page is kept beside the log and the artifact names it, with what the
//! report was version by version.
//!
//! Interactive forms and countdowns have their own rules below.

use std::collections::BTreeMap;

use clarp_speech::markup::strip_hidden_blocks;
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value, json};

use crate::goals::{GoalBook, Plan};
use crate::media::MediaBook;
use crate::python;

pub mod answers;
pub mod countdown;
pub mod decisions;
pub mod form_events;
pub mod forms;
pub mod moved;
pub mod render_spec;
pub mod reports;
pub mod updates;

pub use decisions::{Decision, Delivery};

/// The kinds of artifact the contract knows.
const TYPES: [&str; 15] = [
    "countdown",
    "html_form",
    "decision",
    "question",
    "plan",
    "document",
    "research",
    "code_change",
    "data",
    "audio",
    "video",
    "file",
    "release",
    "directory",
    "workflow_run",
];
const STATUSES: [&str; 7] = [
    "draft",
    "active",
    "ready",
    "failed",
    "completed",
    "cancelled",
    "expired",
];
/// The states in which an artifact's work is over.
const OVER: [&str; 3] = ["completed", "cancelled", "expired"];
/// Kinds that are presentation only and are no longer listed.
const RETIRED: [&str; 4] = ["image", "image_gallery", "live_task", "event"];
/// The largest whole number a client can hold exactly.
const SAFE_INTEGER: i64 = 9_007_199_254_740_991;

/// The fields of a payload that must be text, and are shown on the artifact itself.
const STRING_FIELDS: [&str; 30] = [
    "url",
    "thumbnail_url",
    "mime_type",
    "file_name",
    "content",
    "target_at",
    "source_url",
    "commit",
    "branch",
    "repository",
    "subject",
    "starts_at",
    "ends_at",
    "time_zone",
    "location",
    "notes",
    "version",
    "build",
    "environment",
    "diff",
    "path_label",
    "asset_id",
    "root",
    "relative_path",
    "provider",
    "run_id",
    "run_url",
    "workflow_name",
    "current_step",
    "conclusion",
];
const INTEGER_FIELDS: [&str; 9] = [
    "duration_ms",
    "size_bytes",
    "row_count",
    "source_count",
    "files_changed",
    "additions",
    "deletions",
    "total_steps",
    "completed_steps",
];
/// The fields of a payload that are shown on the artifact itself, in the order they are.
const PUBLIC_FIELDS: [&str; 50] = [
    "target_at",
    "url",
    "thumbnail_url",
    "mime_type",
    "file_name",
    "content",
    "source_url",
    "commit",
    "branch",
    "repository",
    "progress",
    "duration_ms",
    "size_bytes",
    "row_count",
    "source_count",
    "sources",
    "columns",
    "rows",
    "chart",
    "recipients",
    "subject",
    "starts_at",
    "ends_at",
    "time_zone",
    "location",
    "notes",
    "all_day",
    "version",
    "build",
    "environment",
    "files_changed",
    "additions",
    "deletions",
    "diff",
    "path_label",
    "artifact_ids",
    "asset_id",
    "asset_ids",
    "root",
    "relative_path",
    "provider",
    "run_id",
    "run_url",
    "workflow_name",
    "current_step",
    "conclusion",
    "total_steps",
    "completed_steps",
    "read_only",
    "connect_origins",
];

/// How a call is refused: as a mistake of the caller's, as something the caller may not do,
/// or because what it names is taken.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Why {
    /// A mistake in what was asked: the Python Host's `ValueError`.
    Value,
    /// Not this caller's to do: `PermissionError`.
    Permission,
    /// A name that is taken: the database's own refusal, `IntegrityError`.
    Integrity,
    /// A refusal of a render spec, by its code; the message is its pointer
    /// ([`render_spec::Refusal`]).
    Spec(&'static str),
}

/// A call that is refused, in the words the Python Host uses.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Refused {
    pub why: Why,
    pub message: String,
}

impl std::fmt::Display for Refused {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.message)
    }
}

impl std::error::Error for Refused {}

pub(crate) fn refuse<T>(message: impl Into<String>) -> Result<T, Refused> {
    Err(Refused {
        why: Why::Value,
        message: message.into(),
    })
}

pub(crate) fn forbid<T>(message: &str) -> Result<T, Refused> {
    Err(Refused {
        why: Why::Permission,
        message: message.to_string(),
    })
}

fn taken<T>(what: &str) -> Result<T, Refused> {
    Err(Refused {
        why: Why::Integrity,
        message: format!("UNIQUE constraint failed: {what}"),
    })
}

/// An artifact as it is kept.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Artifact {
    pub artifact_id: String,
    pub agent_id: String,
    pub session: String,
    /// Its kind: `document`, `data`, `plan`, `decision` and the others.
    #[serde(rename = "type")]
    pub kind: String,
    pub title: String,
    pub summary: String,
    pub status: String,
    /// What it is about, in the agent's own naming: a plan's id, a deployment.
    pub reference_id: String,
    pub payload: Map<String, Value>,
    pub created_at: i64,
    pub updated_at: i64,
    #[serde(default)]
    pub completed_at: Option<i64>,
    #[serde(default)]
    pub deleted_at: Option<i64>,
    #[serde(default)]
    pub archived_at: Option<i64>,
    #[serde(default)]
    pub pinned_at: Option<i64>,
    /// The request to the user, for a `decision` or a `question`.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub decision: Option<Decision>,
    /// A report's page, by what it is: the page itself is kept beside the log, and the
    /// payload kept here is without it.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub page: Option<reports::Page>,
    /// What a report was, version by version, without the pages themselves.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub revisions: Vec<reports::Revision>,
    /// The answers users gave to a form.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub submissions: Vec<forms::Submission>,
}

/// An artifact hidden from the inbox, or shown there again, as the owner asks: with whether
/// that changed anything. One that changed since the owner saw it is refused.
pub fn archived(
    current: &Artifact,
    archived: bool,
    expected_updated_at: i64,
    now: i64,
) -> Result<(Artifact, bool), Refused> {
    if current.archived_at.is_some() == archived {
        return Ok((current.clone(), false));
    }
    if current.updated_at != expected_updated_at {
        return refuse("artifact changed");
    }
    let now = now.max(current.updated_at + 1);
    let changed = Artifact {
        archived_at: archived.then_some(now),
        updated_at: now,
        ..current.clone()
    };
    Ok((changed, true))
}

/// An artifact taken out of the library: the record stays, and nothing it points at is
/// touched. A request to the user has its own way out.
pub fn discarded(
    current: &Artifact,
    expected_updated_at: i64,
    now: i64,
) -> Result<(Artifact, bool), Refused> {
    if matches!(current.kind.as_str(), "decision" | "question") {
        return refuse("decisions and questions must use their dedicated dismissal endpoint");
    }
    if current.deleted_at.is_some() {
        return Ok((current.clone(), false));
    }
    if current.updated_at != expected_updated_at {
        return refuse("artifact changed");
    }
    let now = now.max(current.updated_at + 1);
    let changed = Artifact {
        deleted_at: Some(now),
        updated_at: now,
        pinned_at: None,
        ..current.clone()
    };
    Ok((changed, true))
}

/// What making or changing an artifact comes to.
#[derive(Debug, Clone, PartialEq)]
pub struct Made {
    pub artifact: Artifact,
    /// A report's page that came with the change: to be kept under [`reports::Page::blob`]
    /// before the artifact is written.
    pub page: Option<String>,
    /// Whether there is anything to write. A report that is published again as it is, or as
    /// it once was, is answered with the report as it stands.
    pub changed: bool,
}

impl Made {
    fn of(artifact: Artifact) -> Self {
        Self {
            artifact,
            page: None,
            changed: true,
        }
    }
}

/// What the log keeps of a change to an artifact: the artifact as the change left it.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ArtifactRecord {
    pub at_ms: i64,
    pub artifact: Artifact,
}

/// Who an artifact is made for: the agent, the name its session has, and what it is called.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Maker<'a> {
    pub agent_id: &'a str,
    pub session: &'a str,
    pub persona: &'a str,
}

/// What a call needs of the world around the artifacts.
pub struct Ctx<'a> {
    pub now_ms: i64,
    /// A new piece of an id: this many bytes, in hex.
    pub token_hex: &'a mut dyn FnMut(usize) -> String,
    /// Whether an artifact has this id anywhere on the Host.
    pub taken: &'a dyn Fn(&str) -> bool,
    /// The files the artifact's agent published: what a recording, a film or a file names.
    pub published: &'a MediaBook,
    /// Whether another agent has an artifact with this id that was not thrown away.
    pub elsewhere: &'a dyn Fn(&str) -> bool,
    /// The page that is kept under this name, when it is.
    pub page: &'a dyn Fn(&reports::Page) -> Option<String>,
}

/// What an artifact is to be made from.
#[derive(Debug, Clone, PartialEq, Default)]
pub struct New {
    pub kind: String,
    pub title: String,
    pub summary: String,
    pub status: String,
    pub reference_id: String,
    /// `None` and null are an empty payload.
    pub payload: Option<Value>,
    pub artifact_id: String,
    /// The version a report that is published again follows.
    pub expected_version: Option<Value>,
    /// Only check it: what only a render spec can be (`?validate_only=1`).
    pub validate_only: bool,
}

/// Which artifacts are listed.
#[derive(Debug, Clone, PartialEq, Default)]
pub struct Listing {
    pub session: String,
    pub agent_id: String,
    pub kind: String,
    pub search: String,
    pub created_from: Option<i64>,
    pub created_to: Option<i64>,
    pub limit: i64,
    pub offset: i64,
    /// By when they were made, instead of by when they last changed.
    pub by_creation: bool,
}

/// The artifacts of one agent.
#[derive(Debug, Clone, PartialEq, Default)]
pub struct ArtifactBook {
    artifacts: BTreeMap<String, Artifact>,
}

pub(crate) fn is_true(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(flag) => *flag,
        Value::String(text) => !text.is_empty(),
        Value::Number(number) => number.as_f64() != Some(0.0),
        Value::Array(items) => !items.is_empty(),
        Value::Object(fields) => !fields.is_empty(),
    }
}

pub(crate) fn clip(text: &str, max: usize) -> String {
    text.chars().take(max).collect()
}

/// A new id of a kind: `artifact-` or `decision-` and twelve bytes.
pub(crate) fn new_id(prefix: &str, ctx: &mut Ctx<'_>) -> String {
    format!("{prefix}-{}", (ctx.token_hex)(12))
}

fn valid_id(id: &str) -> bool {
    let plain = |c: char| c.is_ascii_alphanumeric() || "._:-".contains(c);
    (1..=180).contains(&id.chars().count()) && id.chars().all(plain)
}

/// A JSON value without the blocks that are never shown, in every text of it.
fn sanitized(value: &Value) -> Value {
    match value {
        Value::String(text) => Value::String(strip_hidden_blocks(text)),
        Value::Array(items) => Value::Array(items.iter().map(sanitized).collect()),
        Value::Object(fields) => Value::Object(
            fields
                .iter()
                .map(|(key, value)| (key.clone(), sanitized(value)))
                .collect(),
        ),
        other => other.clone(),
    }
}

/// A whole number, which a truth value is not.
fn whole(value: &Value) -> Option<i64> {
    match value {
        Value::Number(number) if number.is_i64() || number.is_u64() => number.as_i64(),
        _ => None,
    }
}

/// Checks a payload and makes it what is kept: no hidden blocks, and the fields every kind
/// shares of the type they must be.
fn payload(value: Option<&Value>) -> Result<Map<String, Value>, Refused> {
    payload_as(value, false)
}

/// The same, for a page of HTML when `page` is set: its `content` is kept as it was
/// written, since what is a hidden block in a text is markup in a page.
pub(crate) fn payload_as(value: Option<&Value>, page: bool) -> Result<Map<String, Value>, Refused> {
    let given = match value {
        None | Some(Value::Null) => return Ok(Map::new()),
        Some(Value::Object(_)) => value.map(sanitized),
        Some(_) => return refuse("payload must be an object"),
    };
    let Some(Value::Object(mut payload)) = given else {
        return Ok(Map::new());
    };
    let written = value.and_then(|value| value.get("content"));
    if let Some(written) = written.filter(|written| page && !written.is_null()) {
        payload.insert("content".to_string(), written.clone());
    }
    let limit = if payload.contains_key("answer_schema") {
        4 * 1024 * 1024
    } else {
        131_072
    };
    if python::dumps(&Value::Object(payload.clone())).len() > limit {
        return refuse("artifact payload too large");
    }
    for key in STRING_FIELDS {
        if payload.get(key).is_some_and(|value| !value.is_string()) {
            return refuse(format!("payload {key} must be a string"));
        }
    }
    for key in INTEGER_FIELDS {
        match payload.get(key).map(whole) {
            None => {}
            Some(None) => return refuse(format!("payload {key} must be an integer")),
            Some(Some(number)) if number < 0 => {
                return refuse(format!("payload {key} is outside the supported range"));
            }
            Some(Some(_)) => {}
        }
    }
    if let Some(progress) = payload.get("progress") {
        let Some(progress) = progress.as_f64().filter(|_| progress.is_number()) else {
            return refuse("payload progress must be numeric");
        };
        if !(0.0..=1.0).contains(&progress) {
            return refuse("payload progress must be between 0 and 1");
        }
    }
    if payload
        .get("all_day")
        .is_some_and(|value| !value.is_boolean())
    {
        return refuse("payload all_day must be a boolean");
    }
    // An episode of a podcast is a recording with more to it than this Host has yet.
    if payload.contains_key("podcast") {
        return refuse("podcast episodes are not available on this Host yet (the Rust Host)");
    }
    Ok(payload)
}

/// The parts of a URL that say whether it is safe to show: `None` when it is not one.
struct Url<'a> {
    scheme: &'a str,
    host: String,
    userinfo: bool,
    path: &'a str,
}

fn split_url(raw: &str) -> Option<Url<'_>> {
    let (scheme, rest) = match raw.split_once(':') {
        Some((scheme, rest))
            if scheme.starts_with(|c: char| c.is_ascii_alphabetic())
                && scheme
                    .chars()
                    .all(|c| c.is_ascii_alphanumeric() || "+-.".contains(c)) =>
        {
            (scheme, rest)
        }
        _ => ("", raw),
    };
    let Some(after) = rest.strip_prefix("//") else {
        return Some(Url {
            scheme,
            host: String::new(),
            userinfo: false,
            path: rest,
        });
    };
    let end = after.find(['/', '?', '#']).unwrap_or(after.len());
    let (netloc, tail) = after.split_at(end);
    let path = tail.split(['?', '#']).next().unwrap_or("");
    let (userinfo, hostport) = match netloc.rsplit_once('@') {
        Some((userinfo, hostport)) => (Some(userinfo), hostport),
        None => (None, netloc),
    };
    let (host, port) = match hostport.strip_prefix('[') {
        Some(bracketed) => {
            let (host, after) = bracketed.split_once(']')?;
            (host, after.strip_prefix(':'))
        }
        None => match hostport.split_once(':') {
            Some((host, port)) => (host, Some(port)),
            None => (hostport, None),
        },
    };
    // A port that is no number, or none a port can be, is no URL.
    if let Some(port) = port.filter(|port| !port.is_empty())
        && port.parse::<u16>().is_err()
    {
        return None;
    }
    // A name and a password before the host are a URL nobody should be sent to. A name
    // that is empty is none.
    let named = userinfo.is_some_and(|userinfo| {
        let (name, password) = userinfo.split_once(':').unwrap_or((userinfo, ""));
        !name.is_empty() || !password.is_empty()
    });
    Some(Url {
        scheme,
        host: host.to_lowercase(),
        userinfo: named,
        path,
    })
}

/// Refuses a URL an app should not open: anything but HTTPS to a host, or a path of the
/// Host's own where one is allowed.
fn safe_url(raw: &str, field: &str, allow_relative: bool) -> Result<(), Refused> {
    if raw.starts_with('/') && !raw.starts_with("//") {
        return if allow_relative {
            Ok(())
        } else {
            refuse(format!("payload {field} must be an HTTPS URL"))
        };
    }
    match split_url(raw) {
        Some(url) if url.scheme == "https" && !url.host.is_empty() && !url.userinfo => Ok(()),
        _ => refuse(format!(
            "payload {field} must be an HTTPS URL or safe same-origin path"
        )),
    }
}

/// A cell of a table: text, a number a client can hold, a truth value, or nothing.
fn scalar(value: &Value) -> bool {
    match value {
        Value::Null | Value::String(_) | Value::Bool(_) => true,
        Value::Number(number) => match number.as_i64() {
            Some(whole) => whole.abs() <= SAFE_INTEGER,
            None => number.as_f64().is_some_and(f64::is_finite) && !number.is_u64(),
        },
        _ => false,
    }
}

fn text_of(value: &Value) -> String {
    match value {
        Value::String(text) => text.clone(),
        other => other.to_string(),
    }
}

/// What a kind must have, by name.
fn required_fields(kind: &str) -> &'static [&'static str] {
    match kind {
        "document" | "research" => &["content"],
        "countdown" => &["target_at", "time_zone"],
        "html_form" => &["content", "version", "answer_schema"],
        "code_change" => &["repository"],
        "data" => &["columns", "rows"],
        "audio" | "video" | "file" => &["url", "mime_type", "file_name"],
        "release" => &["version"],
        "directory" => &["root", "relative_path"],
        "workflow_run" => &["provider", "run_id", "run_url", "workflow_name"],
        _ => &[],
    }
}

/// Refuses a recording, a film or a file that does not name a file its agent published, as
/// what that file is.
fn require_published(
    kind: &str,
    payload: &Map<String, Value>,
    published: &MediaBook,
) -> Result<(), Refused> {
    let field = |name: &str| payload.get(name).map(text_of).unwrap_or_default();
    let mime = field("mime_type");
    let begins = match kind {
        "audio" => "audio/",
        "video" => "video/",
        _ => "",
    };
    if !mime.to_lowercase().starts_with(begins) {
        return refuse(format!(
            "{kind} artifact mime_type must start with {begins}"
        ));
    }
    // Where this Host serves a published file, and nowhere else.
    let url = field("url");
    let plain = |id: &&str| {
        (1..=180).contains(&id.len())
            && id
                .bytes()
                .all(|byte| byte.is_ascii_alphanumeric() || byte == b'_' || byte == b'-')
    };
    let Some(asset_id) = url.strip_prefix("/media/").filter(plain) else {
        return refuse(format!(
            "{kind} artifact url must be a same-origin media path"
        ));
    };
    let Some(asset) = published.own(asset_id) else {
        return refuse(format!(
            "{kind} artifact media asset not found for this session"
        ));
    };
    if asset.mime_type != mime {
        return refuse(format!(
            "{kind} artifact mime_type must match its media asset"
        ));
    }
    if asset.source_name != field("file_name") {
        return refuse(format!(
            "{kind} artifact file_name must match its media asset"
        ));
    }
    Ok(())
}

/// Refuses a payload that cannot show what its kind is for.
fn require_payload(
    kind: &str,
    payload: &Map<String, Value>,
    published: &MediaBook,
) -> Result<(), Refused> {
    if matches!(kind, "decision" | "question" | "plan") {
        return Ok(());
    }
    let empty = |key: &str| match payload.get(key) {
        None | Some(Value::Null) => true,
        Some(Value::String(text)) => text.is_empty(),
        Some(Value::Object(fields)) => fields.is_empty(),
        // A table may be empty; nothing else that is a list may.
        Some(Value::Array(items)) => items.is_empty() && !(kind == "data" && key == "rows"),
        Some(_) => false,
    };
    let missing: Vec<&str> = required_fields(kind)
        .iter()
        .copied()
        .filter(|key| empty(key))
        .collect();
    if !missing.is_empty() {
        return refuse(format!(
            "{kind} artifact requires payload field(s): {}",
            missing.join(", ")
        ));
    }
    for field in ["source_url", "thumbnail_url"] {
        if let Some(url) = payload.get(field).filter(|url| is_true(url)) {
            safe_url(&text_of(url), field, field == "thumbnail_url")?;
        }
    }
    match kind {
        "countdown" => countdown::validate(payload),
        "data" => require_table(payload),
        "research" => {
            let none = Value::Array(Vec::new());
            let sources = payload.get("sources").unwrap_or(&none).as_array();
            let Some(sources) = sources.filter(|list| {
                list.iter()
                    .all(|item| item.is_object() && source(item).is_some())
            }) else {
                return refuse(
                    "research artifact sources must contain string title and url fields",
                );
            };
            for (_, url) in sources.iter().filter_map(source) {
                safe_url(url, "sources.url", false)?;
            }
            Ok(())
        }
        "directory" => {
            let root = payload.get("root").map(text_of).unwrap_or_default();
            let relative = payload
                .get("relative_path")
                .map(text_of)
                .unwrap_or_default();
            if !matches!(root.as_str(), "workspace" | "home") {
                return refuse("directory root must be workspace or home");
            }
            if relative.starts_with('/') || relative.split('/').any(|part| part == "..") {
                return refuse("directory relative_path must stay within its root");
            }
            Ok(())
        }
        "workflow_run" => {
            if payload.get("provider").and_then(Value::as_str) != Some("github") {
                return refuse("workflow_run provider must be github");
            }
            let run_url = payload.get("run_url").map(text_of).unwrap_or_default();
            safe_url(&run_url, "run_url", false)?;
            let a_run = split_url(&run_url).is_some_and(|url| {
                let parts: Vec<&str> = url
                    .path
                    .strip_suffix('/')
                    .unwrap_or(url.path)
                    .split('/')
                    .collect();
                url.host == "github.com"
                    && matches!(parts.as_slice(), ["", owner, repository, "actions", "runs", run]
                        if !owner.is_empty() && !repository.is_empty()
                            && !run.is_empty() && run.bytes().all(|b| b.is_ascii_digit()))
            });
            if !a_run {
                return refuse("workflow_run URL must identify a GitHub Actions run");
            }
            let steps = |field: &str| payload.get(field).and_then(whole).unwrap_or(0);
            let (total, done) = (steps("total_steps"), steps("completed_steps"));
            if total < 0 || done < 0 || (total != 0 && done > total) {
                return refuse("workflow run step counts are invalid");
            }
            Ok(())
        }
        "audio" | "video" | "file" => require_published(kind, payload, published),
        "html_form" => reports::contract(payload),
        _ => Ok(()),
    }
}

/// A source of a piece of research: its title and where it is.
fn source(item: &Value) -> Option<(&str, &str)> {
    let title = item.get("title").and_then(Value::as_str)?;
    Some((title, item.get("url").and_then(Value::as_str)?))
}

/// A table: named columns, rows of as many cells, and a chart that names columns it has.
fn require_table(payload: &Map<String, Value>) -> Result<(), Refused> {
    let columns: Option<Vec<&str>> = payload
        .get("columns")
        .and_then(Value::as_array)
        .filter(|columns| !columns.is_empty())
        .and_then(|columns| columns.iter().map(Value::as_str).collect());
    let Some(columns) = columns.filter(|names| names.iter().all(|name| !name.trim().is_empty()))
    else {
        return refuse("data artifact columns must be a non-empty string array");
    };
    if columns.len() > 30 {
        return refuse("data artifact cannot contain more than 30 columns");
    }
    let rows: Option<Vec<&Vec<Value>>> = payload
        .get("rows")
        .and_then(Value::as_array)
        .and_then(|rows| rows.iter().map(Value::as_array).collect());
    let Some(rows) = rows else {
        return refuse("data artifact rows must be an array of arrays");
    };
    if rows.len() > 500 {
        return refuse("data artifact cannot contain more than 500 rows");
    }
    if rows.iter().any(|row| row.len() != columns.len()) {
        return refuse("data artifact rows must match the column count");
    }
    if rows.iter().any(|row| row.iter().any(|cell| !scalar(cell))) {
        return refuse("data artifact cells must be strings, numbers, booleans, or null");
    }
    let Some(chart) = payload.get("chart").filter(|chart| !chart.is_null()) else {
        return Ok(());
    };
    if !chart_is_typed(chart) {
        return refuse("data artifact chart must use a supported typed specification");
    }
    let named = |field: &str| {
        chart
            .get(field)
            .and_then(Value::as_str)
            .filter(|name| !name.is_empty())
    };
    let category = named("category_column").unwrap_or(columns[0]);
    let value = named("value_column").unwrap_or_else(|| columns.get(1).copied().unwrap_or(""));
    let (Some(_), Some(value_at)) = (
        columns.iter().position(|name| *name == category),
        columns.iter().position(|name| *name == value),
    ) else {
        return refuse("data artifact chart columns must exist in the dataset");
    };
    if rows.is_empty() || rows.iter().any(|row| !row[value_at].is_number()) {
        return refuse("data artifact chart value column must contain numbers");
    }
    Ok(())
}

fn chart_is_typed(chart: &Value) -> bool {
    chart.is_object()
        && matches!(
            chart.get("kind").and_then(Value::as_str),
            Some("bar" | "line")
        )
        && ["category_column", "value_column"]
            .iter()
            .all(|field| chart.get(field).is_none_or(Value::is_string))
}

/// Whether a field of a payload is of the type it is shown as.
fn shown_as_it_is(key: &str, value: &Value) -> bool {
    let strings = |value: &Value| {
        value
            .as_array()
            .is_some_and(|items| items.iter().all(Value::is_string))
    };
    match key {
        "progress" => value
            .as_f64()
            .is_some_and(|progress| value.is_number() && (0.0..=1.0).contains(&progress)),
        "all_day" | "read_only" => value.is_boolean(),
        // What a page may reach is shown when it is a list this Host would keep.
        "connect_origins" => reports::connect_origins(value).is_ok(),
        "columns" | "recipients" | "artifact_ids" | "asset_ids" => strings(value),
        "rows" => value.as_array().is_some_and(|rows| {
            rows.iter()
                .all(|row| row.as_array().is_some_and(|cells| cells.iter().all(scalar)))
        }),
        "sources" => value.as_array().is_some_and(|sources| {
            sources.iter().all(|source| {
                source.is_object()
                    && source.get("title").is_some_and(Value::is_string)
                    && source.get("url").is_some_and(Value::is_string)
            })
        }),
        "chart" => chart_is_typed(value),
        key if INTEGER_FIELDS.contains(&key) => value.is_i64() || value.is_u64(),
        _ => value.is_string(),
    }
}

impl Artifact {
    /// The artifact as it is shown: its own fields, what of its payload its kind shows, and
    /// its request or its plan.
    pub fn public(&self, persona: &str, goals: &GoalBook, now: i64) -> Value {
        let mut item = json!({
            "artifact_id": self.artifact_id,
            "agent_id": self.agent_id,
            "session": self.session,
            "type": self.kind,
            "title": self.title,
            "summary": self.summary,
            "status": self.status,
            "reference_id": self.reference_id,
            "payload_version": 1,
            "source_message_id": "",
            "source_trace_id": "",
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "completed_at": self.completed_at,
            "deleted_at": self.deleted_at,
            "archived_at": self.archived_at,
            "pinned_at": self.pinned_at,
            "agent_name": persona,
            "pinned": self.pinned_at.is_some(),
            "payload": self.payload,
        });
        // A render spec's row is its columns alone: whoever answers with it adds what the
        // client may see ([`render_spec::marked`], [`render_spec::opened`]).
        if self.kind == render_spec::KIND {
            if let Some(fields) = item.as_object_mut() {
                fields.remove("payload");
            }
            return item;
        }
        for key in PUBLIC_FIELDS {
            if let Some(value) = self
                .payload
                .get(key)
                .filter(|value| shown_as_it_is(key, value))
            {
                item[key] = value.clone();
            }
        }
        // A report's page is not here: whoever answers with this row puts it back, or says
        // what was left out ([`reports::with_page`], [`reports::without_page`]).
        if let Some(page) = &self.page {
            item[reports::PAGE] = json!(page);
        }
        match self.kind.as_str() {
            "decision" | "question" => {
                item["decision"] = match &self.decision {
                    Some(decision) => decision.public(self),
                    None => Value::Null,
                };
            }
            "plan" if !self.reference_id.is_empty() => {
                item["plan"] = goals.get(&self.reference_id, now).unwrap_or(Value::Null);
            }
            _ => {}
        }
        item
    }

    fn listed(&self) -> bool {
        self.deleted_at.is_none() && !RETIRED.contains(&self.kind.as_str())
    }
}

impl ArtifactBook {
    pub fn new() -> Self {
        Self::default()
    }

    /// The artifact with this id, also one that was discarded.
    pub fn any(&self, artifact_id: &str) -> Option<&Artifact> {
        self.artifacts.get(artifact_id)
    }

    /// The artifact with this id, when it was not discarded.
    pub fn get(&self, artifact_id: &str) -> Option<&Artifact> {
        self.any(artifact_id)
            .filter(|artifact| artifact.deleted_at.is_none())
    }

    pub fn artifacts(&self) -> impl Iterator<Item = &Artifact> {
        self.artifacts.values()
    }

    /// What the log keeps of an artifact as a change left it.
    pub fn record(artifact: &Artifact, at_ms: i64) -> ArtifactRecord {
        ArtifactRecord {
            at_ms,
            artifact: artifact.clone(),
        }
    }

    /// Takes a change that is in the log.
    pub fn apply(&mut self, record: &ArtifactRecord) {
        self.artifacts
            .insert(record.artifact.artifact_id.clone(), record.artifact.clone());
    }

    /// Makes an artifact of a plain kind. A request and a plan are made their own way, and
    /// a report that is published under the id it has is revised.
    pub fn create(&self, maker: Maker<'_>, new: &New, ctx: &mut Ctx<'_>) -> Result<Made, Refused> {
        if matches!(
            new.kind.trim().to_lowercase().as_str(),
            "decision" | "question" | "plan"
        ) {
            return refuse(format!(
                "{} must use its dedicated lifecycle endpoint",
                new.kind
            ));
        }
        let (opening, payload) = self.opening(new, ctx)?;
        if opening.kind == "html_form" {
            // A report is published, and published again when it changes. A form is made
            // once.
            if payload.get("read_only") == Some(&Value::Bool(true)) {
                return reports::published(self, maker, new, opening, payload, ctx);
            }
            return forms::made(self, maker, new, opening, payload, ctx);
        }
        self.placed(maker, new, opening, payload, ctx).map(Made::of)
    }

    /// Makes an artifact of any kind: the checks every kind shares, then its own.
    pub(crate) fn make(
        &self,
        maker: Maker<'_>,
        new: &New,
        ctx: &mut Ctx<'_>,
    ) -> Result<Artifact, Refused> {
        let (opening, payload) = self.opening(new, ctx)?;
        self.placed(maker, new, opening, payload, ctx)
    }

    /// What a new artifact is, by what was asked for: its kind, its state, its title, its
    /// id, and its payload as it is kept. Refused in the order the Python Host refuses in.
    fn opening(
        &self,
        new: &New,
        ctx: &mut Ctx<'_>,
    ) -> Result<(Opening, Map<String, Value>), Refused> {
        let kind = new.kind.trim().to_lowercase();
        let status = new.status.trim().to_lowercase();
        let title = strip_hidden_blocks(&new.title).trim().to_string();
        if !TYPES.contains(&kind.as_str()) {
            return refuse("unsupported artifact type");
        }
        if !STATUSES.contains(&status.as_str()) {
            return refuse("unsupported artifact status");
        }
        if title.is_empty() {
            return refuse("artifact title required");
        }
        if new.validate_only {
            return refuse("validate-only requires a render spec");
        }
        let artifact_id = match new.artifact_id.trim() {
            "" => new_id("artifact", ctx),
            given => given.to_string(),
        };
        if !valid_id(&artifact_id) {
            return refuse("invalid artifact id");
        }
        let page = kind == "html_form";
        let payload = if page {
            let filled = new.payload.as_ref().map(reports::normalized).transpose()?;
            payload_as(filled.as_ref(), true)?
        } else {
            payload(new.payload.as_ref())?
        };
        require_payload(&kind, &payload, ctx.published)?;
        let opening = Opening {
            kind,
            status,
            title,
            artifact_id,
        };
        Ok((opening, payload))
    }

    /// Puts a new artifact in its place among the agent's own, when its id and what it
    /// refers to are nobody else's.
    pub(crate) fn placed(
        &self,
        maker: Maker<'_>,
        new: &New,
        opening: Opening,
        payload: Map<String, Value>,
        ctx: &mut Ctx<'_>,
    ) -> Result<Artifact, Refused> {
        let Opening {
            kind,
            status,
            title,
            artifact_id,
        } = opening;
        let reference_id = clip(strip_hidden_blocks(&new.reference_id).trim(), 500);
        if (ctx.taken)(&artifact_id) {
            return taken("artifacts.artifact_id");
        }
        let same_reference = |known: &&Artifact| {
            known.kind == kind && known.reference_id == reference_id && known.deleted_at.is_none()
        };
        if !reference_id.is_empty() && self.artifacts.values().any(|known| same_reference(&known)) {
            return taken("artifacts.agent_id, artifacts.type, artifacts.reference_id");
        }
        let now = ctx.now_ms;
        Ok(Artifact {
            artifact_id,
            agent_id: maker.agent_id.to_string(),
            session: maker.session.to_string(),
            completed_at: OVER.contains(&status.as_str()).then_some(now),
            kind,
            title: clip(&title, 300),
            summary: clip(strip_hidden_blocks(&new.summary).trim(), 4_000),
            status,
            reference_id,
            payload,
            created_at: now,
            updated_at: now,
            deleted_at: None,
            archived_at: None,
            pinned_at: None,
            decision: None,
            page: None,
            revisions: Vec::new(),
            submissions: Vec::new(),
        })
    }

    /// Changes an artifact of a plain kind: its status, its title and summary, its payload
    /// as a whole or in part. A report whose payload changes is revised.
    pub fn update(
        &self,
        artifact_id: &str,
        data: &Value,
        ctx: &mut Ctx<'_>,
    ) -> Result<Made, Refused> {
        let Some(current) = self.get(artifact_id) else {
            return refuse("artifact not found");
        };
        if matches!(current.kind.as_str(), "decision" | "question" | "plan") {
            return refuse(format!(
                "{} must use its dedicated lifecycle endpoint",
                current.kind
            ));
        }
        let (whole, patch) = (data.get("payload"), data.get("payload_patch"));
        if current.kind == "html_form" && (whole.is_some() || patch.is_some()) {
            return reports::revised(current, data, false, ctx);
        }
        let status = status_asked(current, data)?;
        let new_payload = match (whole, patch) {
            (Some(_), Some(_)) => return refuse("provide payload or payload_patch, not both"),
            (None, Some(patch)) => {
                let mut merged = current.payload.clone();
                merged.extend(payload(Some(patch))?);
                payload(Some(&Value::Object(merged)))?
            }
            (Some(whole), None) => payload(Some(whole))?,
            (None, None) => current.payload.clone(),
        };
        if whole.is_some() || patch.is_some() {
            require_payload(&current.kind, &new_payload, ctx.published)?;
        }
        applied(current, data, status, new_payload, ctx.now_ms).map(Made::of)
    }

    /// Hides an artifact from the inbox, or shows it there again. Answers the artifact and
    /// whether that changed anything.
    pub fn archive(
        &self,
        artifact_id: &str,
        archived: bool,
        expected_updated_at: i64,
        now: i64,
    ) -> Result<(Artifact, bool), Refused> {
        let Some(current) = self.get(artifact_id) else {
            return refuse("artifact not found");
        };
        self::archived(current, archived, expected_updated_at, now)
    }

    /// Takes an artifact out of the library. The record stays; nothing it points at is
    /// touched.
    pub fn discard(
        &self,
        artifact_id: &str,
        expected_updated_at: i64,
        now: i64,
    ) -> Result<(Artifact, bool), Refused> {
        let Some(current) = self.any(artifact_id) else {
            return refuse("artifact not found");
        };
        discarded(current, expected_updated_at, now)
    }

    /// Pins an artifact for its owner, or takes the pin away. A pin is a preference, not an
    /// edit: when the artifact last changed stays what it was.
    pub fn pin(
        &self,
        artifact_id: &str,
        pinned: bool,
        now: i64,
    ) -> Result<(Artifact, bool), Refused> {
        let Some(current) = self.get(artifact_id) else {
            return refuse("artifact not found");
        };
        if current.pinned_at.is_some() == pinned {
            return Ok((current.clone(), false));
        }
        let changed = Artifact {
            pinned_at: pinned.then_some(now),
            ..current.clone()
        };
        Ok((changed, true))
    }

    /// The artifact a plan is shown as, when it has one: also one that was discarded.
    pub fn of_plan(&self, plan_id: &str) -> Option<&Artifact> {
        self.artifacts
            .values()
            .find(|artifact| artifact.kind == "plan" && artifact.reference_id == plan_id)
    }

    /// The artifact a plan is shown as, as the plan now stands: made when the plan has none,
    /// and following the plan's state from then on. `title` is the title the plan was
    /// given, for an artifact that is made.
    pub fn plan_synced(
        &self,
        maker: Maker<'_>,
        plan: &Plan,
        title: &str,
        made_now: bool,
        ctx: &mut Ctx<'_>,
    ) -> Result<Artifact, Refused> {
        let current = match self.of_plan(&plan.plan_id) {
            Some(current) => current.clone(),
            None => {
                let new = New {
                    kind: "plan".to_string(),
                    title: title.to_string(),
                    status: "active".to_string(),
                    reference_id: plan.plan_id.clone(),
                    payload: Some(json!({ "plan_id": plan.plan_id })),
                    ..New::default()
                };
                let made = self.make(maker, &new, ctx)?;
                if made_now {
                    return Ok(made);
                }
                made
            }
        };
        let status = match plan.status.as_str() {
            "active" | "paused" => "active",
            "completed" => "completed",
            "blocked" => "failed",
            _ => "cancelled",
        };
        Ok(Artifact {
            status: status.to_string(),
            updated_at: plan.updated_at.max(current.updated_at + 1),
            completed_at: plan.completed_at,
            ..current
        })
    }
}

/// What a new artifact is, by what was asked for.
pub(crate) struct Opening {
    pub(crate) kind: String,
    pub(crate) status: String,
    pub(crate) title: String,
    pub(crate) artifact_id: String,
}

/// The state a change asks for: the one it names, or the one the artifact is in.
pub(crate) fn status_asked(current: &Artifact, data: &Value) -> Result<String, Refused> {
    let status = data
        .get("status")
        .filter(|value| is_true(value))
        .map_or_else(|| current.status.clone(), text_of)
        .to_lowercase();
    if !STATUSES.contains(&status.as_str()) {
        return refuse("unsupported artifact status");
    }
    Ok(status)
}

/// An artifact as a change leaves it: in the state, under the title and with the summary
/// the change names, with this payload.
pub(crate) fn applied(
    current: &Artifact,
    data: &Value,
    status: String,
    payload: Map<String, Value>,
    now: i64,
) -> Result<Artifact, Refused> {
    let given = |key: &str| data.get(key).filter(|value| is_true(value));
    let title = given("title").map_or_else(|| current.title.clone(), text_of);
    let title = strip_hidden_blocks(&title).trim().to_string();
    if title.is_empty() {
        return refuse("artifact title required");
    }
    let now = now.max(current.updated_at + 1);
    let completed_at = if status == current.status {
        current.completed_at
    } else {
        OVER.contains(&status.as_str()).then_some(now)
    };
    let summary = match data.get("summary") {
        Some(Value::Null) => "None".to_string(),
        Some(summary) => text_of(summary),
        None => current.summary.clone(),
    };
    Ok(Artifact {
        title: clip(&title, 300),
        summary: clip(&strip_hidden_blocks(&summary), 4_000),
        status,
        payload,
        updated_at: now,
        completed_at,
        ..current.clone()
    })
}

/// What making an artifact is refused with when nobody has the session it is asked for.
pub fn create_of_nobody(new: &New) -> Refused {
    let reserved = matches!(
        new.kind.trim().to_lowercase().as_str(),
        "decision" | "question" | "plan"
    );
    let message = if reserved {
        format!("{} must use its dedicated lifecycle endpoint", new.kind)
    } else {
        "unknown agent session".to_string()
    };
    Refused {
        why: Why::Value,
        message,
    }
}

/// An agent's artifacts with what is needed to show them: who the agent is, and its plans.
#[derive(Clone, Copy)]
pub struct Shelf<'a> {
    pub maker: Maker<'a>,
    pub artifacts: &'a ArtifactBook,
    pub goals: &'a GoalBook,
}

/// One artifact by its id, among the artifacts of several agents, as it is shown.
pub fn get(shelves: &[Shelf<'_>], artifact_id: &str, now: i64) -> Option<Value> {
    shelves.iter().find_map(|shelf| {
        let artifact = shelf.artifacts.get(artifact_id)?;
        Some(artifact.public(shelf.maker.persona, shelf.goals, now))
    })
}

/// The library: the artifacts of several agents that fit, newest first, one page of them.
pub fn list(shelves: &[Shelf<'_>], listing: &Listing, now: i64) -> Vec<Value> {
    let needle = clip(&listing.search.to_lowercase(), 200);
    let fits = |artifact: &&Artifact| {
        artifact.listed()
            && (listing.session.is_empty() || artifact.session == listing.session)
            && (listing.agent_id.is_empty() || artifact.agent_id == listing.agent_id)
            && (listing.kind.is_empty() || artifact.kind == listing.kind)
            && (needle.is_empty()
                || artifact.title.to_lowercase().contains(&needle)
                || artifact.summary.to_lowercase().contains(&needle))
            && listing
                .created_from
                .is_none_or(|from| artifact.created_at >= from)
            && listing.created_to.is_none_or(|to| artifact.created_at < to)
    };
    let mut found: Vec<(&Shelf<'_>, &Artifact)> = shelves
        .iter()
        .flat_map(|shelf| {
            shelf
                .artifacts
                .artifacts()
                .filter(fits)
                .map(move |a| (shelf, a))
        })
        .collect();
    let key = |artifact: &Artifact| {
        let at = if listing.by_creation {
            artifact.created_at
        } else {
            artifact.updated_at
        };
        (at, artifact.artifact_id.clone())
    };
    found.sort_by_key(|(_, artifact)| std::cmp::Reverse(key(artifact)));
    let limit = usize::try_from(listing.limit.clamp(1, 500)).unwrap_or(1);
    let offset = usize::try_from(listing.offset.max(0)).unwrap_or(0);
    found
        .into_iter()
        .skip(offset)
        .take(limit)
        .map(|(shelf, artifact)| artifact.public(shelf.maker.persona, shelf.goals, now))
        .collect()
}

/// The artifacts their owner pinned, the one pinned last first.
pub fn list_pinned(shelves: &[Shelf<'_>], limit: i64, now: i64) -> Vec<Value> {
    let mut found: Vec<(&Shelf<'_>, &Artifact, i64)> = shelves
        .iter()
        .flat_map(|shelf| {
            shelf.artifacts.artifacts().filter_map(move |artifact| {
                let pinned_at = artifact.pinned_at.filter(|_| artifact.listed())?;
                Some((shelf, artifact, pinned_at))
            })
        })
        .collect();
    found.sort_by_key(|(_, artifact, pinned_at)| {
        std::cmp::Reverse((*pinned_at, artifact.artifact_id.clone()))
    });
    found
        .into_iter()
        .take(usize::try_from(limit.clamp(1, 500)).unwrap_or(1))
        .map(|(shelf, artifact, _)| artifact.public(shelf.maker.persona, shelf.goals, now))
        .collect()
}
