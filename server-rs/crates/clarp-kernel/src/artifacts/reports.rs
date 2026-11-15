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
//! Not here yet: a form that takes answers. Its answers are held to a JSON Schema the agent
//! writes, which this Host cannot read yet; asking for one says so.

use serde_json::{Map, Value, json};

use super::{Refused, refuse};

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
    if !read_only {
        // Where the Python Host asks whether the schema is a JSON Schema.
        return refuse(FORMS_NOT_HERE);
    }
    Ok(())
}
