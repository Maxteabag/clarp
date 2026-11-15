//! HTML reports: a page an agent hands its user.
//!
//! An artifact of the kind `html_form` is a page of HTML that the apps show in a sandbox. One
//! that takes no answers (`read_only`) is a report. The page may run its own scripts, and
//! nothing of it may reach anything but the origins the artifact names: that list is read
//! here, strictly, and becomes the policy a browser is given. What a page says can never
//! widen it.
//!
//! These are the rules of the Python Host's `server/lib/html_forms.py`, held to it by
//! `tests/fixtures/report-vectors.json` (`server-rs/tools/report-vectors.py`).
//!
//! A page can be large (the reports of a week on one Host weighed megabytes) and is the
//! same from one change of its artifact to the next. So it is not part of what the log keeps
//! of the artifact, and it is not held in memory: it is kept beside the log, whole, under a
//! name made of what it is ([`Page`]), and the artifact names it. The rules see a page when
//! it comes in, which is when they judge it; afterwards they compare pages by their names.
//! A report that is published again under its id is revised, and carries what it was
//! version by version ([`Revision`]). These are the rules of
//! `server/lib/html_report_revisions.py`, held by the report's scenario in
//! `tests/fixtures/artifact-vectors.json`.
//!
//! Not here yet: a form that takes answers. Its answers are held to a JSON Schema the agent
//! writes, which this Host cannot read yet; asking for one says so.

use serde::{Deserialize, Serialize};
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};

use super::{
    Artifact, ArtifactBook, Ctx, Made, Maker, New, Opening, Refused, answers, applied, payload_as,
    refuse, require_payload, status_asked,
};

/// What a form that takes answers is refused with.
pub const FORMS_NOT_HERE: &str =
    "HTML forms that take answers are not available on this Host yet (the Rust Host)";

/// The answer schema of a report: an object with nothing in it.
pub fn read_only_schema() -> Value {
    json!({ "type": "object", "properties": {}, "additionalProperties": false })
}

/// Whether a label of a host name is one: letters, digits and dashes, no dash at either
/// end, 63 signs at most.
fn is_label(label: &str) -> bool {
    let bytes = label.as_bytes();
    let plain = |byte: &u8| byte.is_ascii_lowercase() || byte.is_ascii_digit() || *byte == b'-';
    (1..=63).contains(&bytes.len())
        && bytes.iter().all(plain)
        && !label.starts_with('-')
        && !label.ends_with('-')
}

/// Whether a label is a part of an address as it is written out: a number up to 255
/// without zeros in front of it.
fn is_octet(label: &str) -> bool {
    let written_out = label == "0" || !label.starts_with('0');
    label.len() <= 3
        && written_out
        && label.bytes().all(|byte| byte.is_ascii_digit())
        && label.parse::<u16>().is_ok_and(|number| number <= 255)
}

/// The origins a page may connect to, as they are kept: exact HTTPS origins, eight at
/// most, each once, in small letters, the port left out when it is the usual one. A URL, a
/// pattern or a piece of a policy is refused, never read as one.
pub fn connect_origins(value: &Value) -> Result<Vec<String>, Refused> {
    let Some(given) = value.as_array().filter(|given| given.len() <= 8) else {
        return refuse("connect_origins must be an array of at most 8 exact HTTPS origins");
    };
    let mut kept: Vec<String> = Vec::new();
    for origin in given {
        let Some(origin) = origin
            .as_str()
            .filter(|origin| origin.chars().count() <= 256)
        else {
            return refuse("invalid connect_origins origin");
        };
        let host_and_port = origin.strip_prefix("https://").map(|rest| {
            rest.split_once(':')
                .map_or((rest, None), |(host, port)| (host, Some(port)))
        });
        let in_a_host = |byte: u8| byte.is_ascii_alphanumeric() || byte == b'.' || byte == b'-';
        let digits =
            |port: &str| (1..=5).contains(&port.len()) && port.bytes().all(|b| b.is_ascii_digit());
        let Some((host, port)) = host_and_port.filter(|(host, port)| {
            !host.is_empty() && host.bytes().all(in_a_host) && port.is_none_or(digits)
        }) else {
            return refuse(
                "connect_origins requires exact HTTPS origins without paths or credentials",
            );
        };
        let host = host.to_lowercase();
        let labels: Vec<&str> = host.split('.').collect();
        if !labels.iter().all(|label| is_label(label)) {
            return refuse("invalid connect_origins hostname");
        }
        // A browser reads a short, a decimal or a hexadecimal number as an address. Only
        // an address written out in full is taken as one.
        let last = labels.last().copied().unwrap_or("");
        let numeric = last.bytes().all(|byte| byte.is_ascii_digit()) || last.starts_with("0x");
        if numeric && !(labels.len() == 4 && labels.iter().all(|label| is_octet(label))) {
            return refuse("invalid connect_origins IPv4 address");
        }
        let number = port.map_or(Some(443), |port| port.parse::<u32>().ok());
        let Some(number) = number.filter(|number| (1..=65_535).contains(number)) else {
            return refuse("invalid connect_origins port");
        };
        let canonical = if number == 443 {
            format!("https://{host}")
        } else {
            format!("https://{host}:{number}")
        };
        if !kept.contains(&canonical) {
            kept.push(canonical);
        }
    }
    Ok(kept)
}

/// The origins a kept payload names. A list that cannot be read names none: what is kept
/// wrong closes the page, it never opens it.
fn kept_origins(payload: &Map<String, Value>) -> Vec<String> {
    payload
        .get("connect_origins")
        .map_or_else(|| Ok(Vec::new()), connect_origins)
        .unwrap_or_default()
}

/// The policy a browser is given for a page: nothing from outside but data it carries
/// itself, and connections only to the origins the artifact names.
pub fn content_security_policy(payload: &Map<String, Value>) -> String {
    let origins = kept_origins(payload);
    let connect = if origins.is_empty() {
        "'none'".to_string()
    } else {
        origins.join(" ")
    };
    format!(
        "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; \
         img-src data:; font-src data:; media-src data:; connect-src {connect}; \
         frame-src 'none'; form-action 'none'; base-uri 'none'; object-src 'none'"
    )
}

/// A text as the value of an attribute of a page (Python's `html.escape`).
fn escaped(text: &str) -> String {
    text.replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
        .replace('"', "&quot;")
        .replace('\'', "&#x27;")
}

/// A page as a browser is given it: the artifact's HTML between the policy, what the page
/// is told it may reach, and a note for the reader of what that is. Answers the page and
/// the policy for the header it is served with.
pub fn rendered(payload: &Map<String, Value>, content: &str) -> (String, String) {
    let policy = content_security_policy(payload);
    let origins = kept_origins(payload);
    let capabilities = json!({ "network": origins }).to_string();
    let reaches = if origins.is_empty() {
        "External connections blocked".to_string()
    } else {
        origins.join("<br>")
    };
    let page = format!(
        "<!doctype html><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">\
         <meta http-equiv=\"Content-Security-Policy\" content=\"{}\">\
         <script>window.clarpForm={{capabilities:{capabilities}}};</script>{content}\
         <details style=\"position:fixed;top:8px;right:8px;background:white;color:black;\
         padding:6px;font:14px system-ui\"><summary>Network access</summary>{reaches}</details>",
        escaped(&policy)
    );
    (page, format!("sandbox allow-scripts; {policy}"))
}

/// A payload with what a report may leave out filled in: its empty answer schema, and its
/// origins as they are kept.
pub fn normalized(payload: &Value) -> Result<Value, Refused> {
    let Some(given) = payload.as_object() else {
        return Ok(payload.clone());
    };
    let mut payload = given.clone();
    if payload.get("read_only") == Some(&Value::Bool(true))
        && !payload.contains_key("answer_schema")
    {
        payload.insert("answer_schema".to_string(), read_only_schema());
    }
    if let Some(origins) = payload.get("connect_origins") {
        let kept = connect_origins(origins)?;
        payload.insert("connect_origins".to_string(), json!(kept));
    }
    Ok(Value::Object(payload))
}

/// Refuses an answer schema that points elsewhere, or that is nested deeper than anything
/// a form needs.
fn inline(value: &Value, depth: usize) -> Result<(), Refused> {
    if depth > 30 {
        return refuse("answer schema nesting too deep");
    }
    match value {
        Value::Object(fields) => {
            let points = ["$ref", "$dynamicRef", "$recursiveRef"];
            if points.iter().any(|key| fields.contains_key(*key)) {
                return refuse("schema references are not supported; inline the schema");
            }
            fields.values().try_for_each(|item| inline(item, depth + 1))
        }
        Value::Array(items) => items.iter().try_for_each(|item| inline(item, depth + 1)),
        _ => Ok(()),
    }
}

/// Whether a normalized payload is a report this Host shows: a page, a version that names
/// it, origins that are exact, and no answers.
pub fn validate(payload: &Map<String, Value>) -> Result<(), Refused> {
    if !judged(payload)? {
        // Where the Python Host asks whether the schema is a JSON Schema.
        return refuse(FORMS_NOT_HERE);
    }
    Ok(())
}

/// Whether a normalized payload is a form or a report, as the Python Host judges one: a
/// page, a version that names it, origins that are exact, and a schema of its answers that
/// is one. A schema with a word this Host does not read is refused by that word.
pub fn contract(payload: &Map<String, Value>) -> Result<(), Refused> {
    judged(payload)?;
    answers::check(payload.get("answer_schema").unwrap_or(&Value::Null))
}

/// What is asked of a form and of a report alike, up to what its schema says. Answers
/// whether it is a report: one that takes no answers.
fn judged(payload: &Map<String, Value>) -> Result<bool, Refused> {
    let content = payload.get("content").and_then(Value::as_str);
    if !content.is_some_and(|content| !content.trim().is_empty()) {
        return refuse("HTML form requires content");
    }
    let in_a_version =
        |byte: u8| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b'-');
    let version = payload.get("version").and_then(Value::as_str);
    if !version.is_some_and(|version| {
        (1..=80).contains(&version.len()) && version.bytes().all(in_a_version)
    }) {
        return refuse("HTML form requires a stable version string");
    }
    if let Some(origins) = payload.get("connect_origins") {
        connect_origins(origins)?;
    }
    let read_only = match payload.get("read_only") {
        None => false,
        Some(Value::Bool(read_only)) => *read_only,
        Some(_) => return refuse("read_only must be a boolean"),
    };
    let schema = payload.get("answer_schema");
    if read_only && schema != Some(&read_only_schema()) {
        return refuse("a read-only report cannot declare answers; omit answer_schema");
    }
    let Some(schema) = schema.filter(|schema| schema.get("type") == Some(&json!("object"))) else {
        return refuse("answer_schema must describe an object");
    };
    inline(schema, 0)?;
    Ok(read_only)
}

/// The field of a shown artifact that names its page, until whoever answers puts the page
/// back or says it was left out. It never leaves the Host.
pub const PAGE: &str = "$page";

/// A page by what it is: a number only this page has, and its length in bytes.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Page {
    pub sha256: String,
    pub bytes: u64,
}

impl Page {
    pub fn of(content: &str) -> Self {
        let sha256 = Sha256::digest(content.as_bytes())
            .iter()
            .map(|byte| format!("{byte:02x}"))
            .collect();
        Self {
            sha256,
            bytes: content.len() as u64,
        }
    }

    /// The name the page is kept under.
    pub fn blob(&self) -> String {
        format!("{}.html", self.sha256)
    }
}

/// What a report was under one version: everything but the page itself.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Revision {
    pub version: String,
    pub title: String,
    pub summary: String,
    pub created_at: i64,
    pub payload: Map<String, Value>,
    pub page: Page,
}

/// Whether an artifact is a report: a page that takes no answers.
pub fn is_report(artifact: &Artifact) -> bool {
    artifact.kind == "html_form"
        && artifact.page.is_some()
        && artifact.payload.get("read_only") == Some(&Value::Bool(true))
        && artifact.payload.get("answer_schema") == Some(&read_only_schema())
}

/// A payload without its page.
fn light(payload: &Map<String, Value>) -> Map<String, Value> {
    let mut light = payload.clone();
    light.remove("content");
    light
}

/// The version a payload names.
fn version_of(payload: &Map<String, Value>) -> String {
    payload
        .get("version")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_string()
}

impl Artifact {
    /// Remembers what the report is now, under its version, when that version is not
    /// remembered yet.
    pub(super) fn remember(&mut self) {
        let (version, Some(page)) = (version_of(&self.payload), self.page.clone()) else {
            return;
        };
        if self.revisions.iter().all(|known| known.version != version) {
            self.revisions.push(Revision {
                version,
                title: self.title.clone(),
                summary: self.summary.clone(),
                created_at: self.updated_at,
                payload: self.payload.clone(),
                page,
            });
        }
    }
}

/// A report is published: made, when its id is new, and revised when it is the id of a
/// report this agent has.
pub(crate) fn published(
    book: &ArtifactBook,
    maker: Maker<'_>,
    new: &New,
    opening: Opening,
    payload: Map<String, Value>,
    ctx: &mut Ctx<'_>,
) -> Result<Made, Refused> {
    if let Some(current) = book.get(&opening.artifact_id) {
        let mut data = json!({
            "payload": payload,
            "title": opening.title,
            "summary": new.summary,
        });
        if let Some(expected) = new
            .expected_version
            .as_ref()
            .filter(|given| !given.is_null())
        {
            data["expected_version"] = expected.clone();
        }
        return revised(current, &data, true, ctx);
    }
    if (ctx.elsewhere)(&opening.artifact_id) {
        return refuse("report artifact belongs to another agent");
    }
    let content = payload
        .get("content")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_string();
    let mut artifact = book.placed(maker, new, opening, light(&payload), ctx)?;
    artifact.page = Some(Page::of(&content));
    artifact.remember();
    Ok(Made {
        artifact,
        page: Some(content),
        changed: true,
    })
}

/// A report is revised: its page, what it may reach, or what is said beside it changes,
/// and what it was stays on record under its version.
pub(crate) fn revised(
    current: &Artifact,
    data: &Value,
    published_again: bool,
    ctx: &mut Ctx<'_>,
) -> Result<Made, Refused> {
    let Some(page_before) = current.page.as_ref().filter(|_| is_report(current)) else {
        return refuse("HTML forms are immutable; publish a new artifact/version");
    };
    let (whole, patch) = (data.get("payload"), data.get("payload_patch"));
    if whole.is_some() && patch.is_some() {
        return refuse("provide payload or payload_patch, not both");
    }
    let Some(incoming) = whole.or(patch).and_then(Value::as_object) else {
        return refuse("payload must be an object");
    };
    // A part that does not bring a page keeps the one the report has.
    let value = if patch.is_some() {
        let mut merged = current.payload.clone();
        if !incoming.contains_key("content") {
            let Some(kept) = (ctx.page)(page_before) else {
                return Err(Refused {
                    why: super::Why::Integrity,
                    message: "the report's page is not kept on this Host".to_string(),
                });
            };
            merged.insert("content".to_string(), Value::String(kept));
        }
        merged.extend(incoming.clone());
        merged
    } else {
        incoming.clone()
    };
    let mut payload = payload_as(Some(&normalized(&Value::Object(value))?), true)?;
    if payload.get("read_only") != Some(&Value::Bool(true)) {
        return refuse("a report revision must remain read-only");
    }
    let page_of =
        |payload: &Map<String, Value>| payload.get("content").and_then(Value::as_str).map(Page::of);
    let as_it_is = |payload: &Map<String, Value>| {
        light(payload) == current.payload && page_of(payload).as_ref() == Some(page_before)
    };
    // A change that names no version is given one.
    if !incoming.contains_key("version") && !as_it_is(&payload) {
        let version = format!("revision-{}", (ctx.token_hex)(16));
        payload.insert("version".to_string(), Value::String(version));
    }
    require_payload("html_form", &payload, ctx.published)?;
    let content = payload
        .get("content")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_string();
    let (page, kept, version) = (Page::of(&content), light(&payload), version_of(&payload));
    let before = version_of(&current.payload);
    let known = current
        .revisions
        .iter()
        .find(|known| known.version == version);
    if let Some(known) = known {
        if known.payload != kept || known.page != page {
            return refuse(
                "report version already used for different content; choose a new version",
            );
        }
        // An answer that was lost is asked for again: the report as it stands, whatever
        // has come after.
        if version != before || (as_it_is(&payload) && published_again) {
            return Ok(Made {
                artifact: current.clone(),
                page: None,
                changed: false,
            });
        }
    }
    let follows = data
        .get("expected_version")
        .filter(|given| !given.is_null());
    let another = follows.is_some_and(|follows| follows.as_str() != Some(before.as_str()));
    if another || (published_again && known.is_none() && follows.is_none()) {
        return refuse("expected_version must match the current report version");
    }
    let status = status_asked(current, data)?;
    let mut artifact = applied(current, data, status, kept, ctx.now_ms)?;
    artifact.page = Some(page);
    artifact.remember();
    Ok(Made {
        artifact,
        page: Some(content),
        changed: true,
    })
}

/// What was asked of a report's revisions.
#[derive(Debug, Clone, PartialEq)]
pub enum Revisions {
    /// The versions, the newest first: each with its title, its summary and when it was.
    Listed(Vec<Value>),
    /// One version, with its payload: its page is named ([`PAGE`]) for whoever answers to
    /// put back.
    One(Value),
}

impl ArtifactBook {
    /// What a report was: its versions, or one of them.
    pub fn revisions(
        &self,
        artifact_id: &str,
        version: Option<&str>,
        limit: i64,
        offset: i64,
    ) -> Result<Revisions, Refused> {
        let Some(current) = self.get(artifact_id).filter(|current| is_report(current)) else {
            return refuse("read-only report not found");
        };
        if let Some(version) = version {
            let Some(known) = current
                .revisions
                .iter()
                .find(|known| known.version == version)
            else {
                return refuse("report revision not found");
            };
            let mut payload = known.payload.clone();
            payload.insert(PAGE.to_string(), json!(known.page));
            return Ok(Revisions::One(json!({
                "artifact_id": current.artifact_id,
                "version": known.version,
                "title": known.title,
                "summary": known.summary,
                "created_at": known.created_at,
                "payload": payload,
            })));
        }
        let mut known: Vec<&Revision> = current.revisions.iter().collect();
        known.sort_by(|a, b| (b.created_at, &b.version).cmp(&(a.created_at, &a.version)));
        let skip = usize::try_from(offset.max(0)).unwrap_or(usize::MAX);
        let most = usize::try_from(limit.clamp(1, 100)).unwrap_or(100);
        let listed = known.into_iter().skip(skip).take(most).map(|known| {
            json!({
                "version": known.version,
                "title": known.title,
                "summary": known.summary,
                "created_at": known.created_at,
            })
        });
        Ok(Revisions::Listed(listed.collect()))
    }
}

/// The page a shown row or payload names, taken out of it.
pub fn named_page(shown: &mut Value) -> Option<Page> {
    let named = shown.as_object_mut()?.remove(PAGE)?;
    serde_json::from_value(named).ok()
}

/// Puts a report's page back into a row that shows it: in its payload, when the row has
/// one, and beside it.
pub fn with_page(row: &mut Value, content: &str) {
    if let Some(payload) = row.get_mut("payload").and_then(Value::as_object_mut) {
        payload.insert("content".to_string(), json!(content));
    }
    if row.is_object() {
        row["content"] = json!(content);
    }
}

/// Says in a row that a list shows which page was left out of it: how long it is and what
/// it is, by which an app knows the page it holds is this one.
pub fn without_page(row: &mut Value, page: &Page) {
    let left_out = json!({
        "reason": "summary",
        "bytes": page.bytes,
        "sha256": format!("c1:{}", page.sha256),
        "version": row.get("version").cloned().unwrap_or(Value::Null),
        "updated_at": row.get("updated_at").cloned().unwrap_or(Value::Null),
    });
    if row.is_object() {
        row["content_omitted"] = left_out;
    }
}
