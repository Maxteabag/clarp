//! Native forms (`render_spec`, Host feature `render_spec`, issue 428): the
//! closed catalog `clarp.form` version 1 as this client draws it, the reading
//! of a published spec, and the one answer the form path takes. A port of the
//! iOS app's `RenderSpec.swift`, which is the reference for every client.
//!
//! The Host's `contract/catalogs/clarp.form.v1.json` is the authority; this
//! client checks every spec itself and does not rely on the Host having done
//! so. A catalog, catalog version, component or property this build does not
//! know is [`Reading::NeedsNewer`]; anything broken against version 1 is
//! [`Reading::Unreadable`], with a pointer that never names a value.
//!
//! Every element type the catalog will add (charts, tables, action buttons)
//! slots in as one more [`Kind`] and one more arm of [`Reader::element`];
//! until then it reads as needing a newer app.

use std::collections::{BTreeMap, BTreeSet};

use serde_json::{Map, Value};
use sha2::{Digest, Sha256};

/// The name this client gives the Host in `X-Clarp-Client`, matched exactly
/// by the Host's per-client tables (`server_identity.CLIENT_NAME`).
pub const CLIENT_NAME: &str = "desktop";

/// The desktop's own client contract series (independent of iOS's and
/// Android's). 1: draws and answers `render_spec` forms of catalog
/// `clarp.form` version 1.
pub const CLIENT_CONTRACT: u32 = 1;

/// `X-Clarp-Client`: `<name>/<build> contract=<n>`, the strict form the Host
/// reads (`server_identity.strict_client`).
pub fn client_header(build: &str) -> String {
    let build: String = build.chars().filter(|c| c.is_ascii_alphanumeric() || matches!(c, '.' | '_' | '-')).take(80).collect();
    let build = if build.is_empty() { "0".to_owned() } else { build };
    format!("{CLIENT_NAME}/{build} contract={CLIENT_CONTRACT}")
}

/// The catalog this build draws.
pub const CATALOG: &str = "clarp.form";
pub const CATALOG_VERSION: i64 = 1;
pub const MAX_ELEMENTS: usize = 200;
pub const MAX_DEPTH: usize = 12;
pub const MAX_SPEC_BYTES: usize = 32_768;
pub const MAX_STATE_BYTES: usize = 4_096;
pub const MAX_ANSWER_BYTES: usize = 16_384;
pub const INTEGER_LIMIT: i64 = 9_007_199_254_740_991;

/// Component name to the properties it takes.
fn component_props(kind: &str) -> Option<&'static [&'static str]> {
    Some(match kind {
        "stack" => &["axis"],
        "card" => &["title"],
        "heading" => &["text", "level"],
        "text" => &["text"],
        "text_field" => &["label", "placeholder", "multiline", "value"],
        "toggle" => &["label", "value"],
        "choice" => &["label", "options", "value"],
        "divider" => &[],
        _ => return None,
    })
}

fn container(kind: &str) -> bool {
    matches!(kind, "stack" | "card")
}

/// An identifier as the catalog has them: a-z first, then a-z, 0-9 or `_`,
/// 1 to 40 long.
pub fn is_identifier(text: &str) -> bool {
    let bytes = text.as_bytes();
    (1..=40).contains(&bytes.len())
        && bytes[0].is_ascii_lowercase()
        && bytes.iter().all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || *b == b'_')
}

/// The Host counts a string's length in Unicode code points.
pub fn length(text: &str) -> usize {
    text.chars().count()
}

// ---- canonical bytes -------------------------------------------------------

/// RFC 8785 bytes for the contract's JSON (no fractions, no exponents): what
/// the Host hashes. None for a value the contract does not allow (a float,
/// an integer past 2^53 - 1).
pub fn canonical(value: &Value) -> Option<Vec<u8>> {
    let mut out = Vec::new();
    write(value, &mut out).then_some(out)
}

fn write(value: &Value, out: &mut Vec<u8>) -> bool {
    match value {
        Value::Null => out.extend_from_slice(b"null"),
        Value::Bool(flag) => out.extend_from_slice(if *flag { b"true" } else { b"false" }),
        Value::Number(number) => match number.as_i64() {
            Some(n) if n.abs() <= INTEGER_LIMIT => out.extend_from_slice(n.to_string().as_bytes()),
            _ => return false,
        },
        Value::String(text) => write_string(text, out),
        Value::Array(items) => {
            out.push(b'[');
            for (index, item) in items.iter().enumerate() {
                if index > 0 {
                    out.push(b',');
                }
                if !write(item, out) {
                    return false;
                }
            }
            out.push(b']');
        }
        Value::Object(fields) => {
            out.push(b'{');
            // RFC 8785 orders keys by UTF-16 code units, not by scalar or byte.
            let mut keys: Vec<&String> = fields.keys().collect();
            keys.sort_by(|a, b| a.encode_utf16().cmp(b.encode_utf16()));
            for (index, key) in keys.into_iter().enumerate() {
                if index > 0 {
                    out.push(b',');
                }
                write_string(key, out);
                out.push(b':');
                if !write(&fields[key], out) {
                    return false;
                }
            }
            out.push(b'}');
        }
    }
    true
}

fn write_string(text: &str, out: &mut Vec<u8>) {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    out.push(b'"');
    for byte in text.bytes() {
        match byte {
            b'"' => out.extend_from_slice(b"\\\""),
            b'\\' => out.extend_from_slice(b"\\\\"),
            0x08 => out.extend_from_slice(b"\\b"),
            0x09 => out.extend_from_slice(b"\\t"),
            0x0A => out.extend_from_slice(b"\\n"),
            0x0C => out.extend_from_slice(b"\\f"),
            0x0D => out.extend_from_slice(b"\\r"),
            0..0x20 => out.extend_from_slice(&[b'\\', b'u', b'0', b'0', HEX[(byte >> 4) as usize], HEX[(byte & 0x0F) as usize]]),
            _ => out.push(byte),
        }
    }
    out.push(b'"');
}

fn canonical_len(value: &Value) -> usize {
    canonical(value).map_or(usize::MAX, |b| b.len())
}

/// `c1:` and the lowercase hex sha256 of `bytes`.
pub fn hash(bytes: &[u8]) -> String {
    format!("c1:{}", Sha256::digest(bytes).iter().map(|b| format!("{b:02x}")).collect::<String>())
}

// ---- the form --------------------------------------------------------------

/// One value an answer key holds.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Answer {
    Str(String),
    Bool(bool),
    Int(i64),
}

impl Answer {
    pub fn value(&self) -> Value {
        match self {
            Answer::Str(text) => Value::String(text.clone()),
            Answer::Bool(flag) => Value::Bool(*flag),
            Answer::Int(number) => Value::from(*number),
        }
    }

    fn from_value(value: &Value) -> Option<Answer> {
        match value {
            Value::String(text) => Some(Answer::Str(text.clone())),
            Value::Bool(flag) => Some(Answer::Bool(*flag)),
            Value::Number(n) => n.as_i64().filter(|n| n.abs() <= INTEGER_LIMIT).map(Answer::Int),
            _ => None,
        }
    }

    /// As the read-only form and a `$state` text show it.
    pub fn words(&self) -> String {
        match self {
            Answer::Str(text) => text.clone(),
            Answer::Bool(flag) => if *flag { "Yes" } else { "No" }.to_owned(),
            Answer::Int(number) => number.to_string(),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Opt {
    pub value: String,
    pub label: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum TextSource {
    Literal(String),
    State(String),
}

/// One element the catalog has, as this client draws it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Kind {
    Stack { horizontal: bool },
    Card { title: Option<String> },
    Heading { text: String, level: u8 },
    Text(TextSource),
    TextField { label: String, placeholder: Option<String>, multiline: bool, key: String },
    Toggle { label: String, key: String },
    Choice { label: String, options: Vec<Opt>, key: String },
    Divider,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Element {
    pub id: String,
    pub kind: Kind,
    pub children: Vec<Element>,
}

/// What an answer key takes.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Field {
    Str { min: usize, max: Option<usize> },
    Choice(Vec<String>),
    Boolean,
    Integer,
}

/// Why a key cannot be sent yet. The words belong to the screen.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Gap {
    Missing(String),
    TooShort(String, usize),
    TooLong(String, usize),
    AnswersTooLarge,
}

#[derive(Debug, Clone, PartialEq)]
pub enum Submission {
    /// The `{intent, state}` object the form path takes.
    Ready(Value),
    Blocked(Vec<Gap>),
}

pub type Values = BTreeMap<String, Answer>;

/// A spec this client can draw: every rule of the catalog was checked here.
#[derive(Debug, Clone, PartialEq)]
pub struct Form {
    pub version: String,
    pub root: Element,
    pub submit_intent: String,
    pub submit_label: String,
    pub fields: BTreeMap<String, Field>,
    pub required: BTreeSet<String>,
    /// Declared initial values, plus `false` for every toggle without one.
    pub initial: Values,
    /// Keys a control writes; a key outside it is sent as the spec gave it.
    pub bound: BTreeSet<String>,
    /// The spec as received, for the content identity.
    pub spec: Value,
}

impl Form {
    /// The answers exactly as the form path takes them: `{intent, state}`
    /// with only keys that have a value. An empty text and an unselected
    /// choice are left out; a toggle is always there.
    pub fn submission(&self, values: &Values) -> Submission {
        let mut state = Map::new();
        let mut gaps = Vec::new();
        for (key, field) in &self.fields {
            let required = self.required.contains(key);
            match (field, values.get(key)) {
                (Field::Str { min, max }, Some(Answer::Str(text))) => {
                    let count = length(text);
                    if count == 0 {
                        if required {
                            gaps.push(Gap::Missing(key.clone()));
                        }
                    } else if count < *min {
                        gaps.push(Gap::TooShort(key.clone(), *min));
                    } else if max.is_some_and(|max| count > max) {
                        gaps.push(Gap::TooLong(key.clone(), max.unwrap_or_default()));
                    } else {
                        state.insert(key.clone(), Value::String(text.clone()));
                    }
                }
                (Field::Choice(allowed), Some(Answer::Str(chosen))) if allowed.contains(chosen) => {
                    state.insert(key.clone(), Value::String(chosen.clone()));
                }
                (Field::Boolean, Some(Answer::Bool(flag))) => {
                    state.insert(key.clone(), Value::Bool(*flag));
                }
                (Field::Integer, Some(Answer::Int(number))) => {
                    state.insert(key.clone(), Value::from(*number));
                }
                _ => {
                    if required {
                        gaps.push(Gap::Missing(key.clone()));
                    }
                }
            }
        }
        let mut answers = Map::new();
        answers.insert("intent".into(), Value::String(self.submit_intent.clone()));
        answers.insert("state".into(), Value::Object(state));
        let answers = Value::Object(answers);
        if canonical_len(&answers) > MAX_ANSWER_BYTES {
            gaps.push(Gap::AnswersTooLarge);
        }
        if gaps.is_empty() { Submission::Ready(answers) } else { Submission::Blocked(gaps) }
    }

    /// Typed values survive a reload when their key still exists with a
    /// value the new field takes.
    pub fn carrying(&self, old: &Values) -> Values {
        let mut values = self.initial.clone();
        for (key, answer) in old {
            if !self.bound.contains(key) {
                continue;
            }
            let takes = match (self.fields.get(key), answer) {
                (Some(Field::Str { .. }), Answer::Str(_)) | (Some(Field::Boolean), Answer::Bool(_)) => true,
                (Some(Field::Choice(allowed)), Answer::Str(chosen)) => allowed.contains(chosen),
                _ => false,
            };
            if takes {
                values.insert(key.clone(), answer.clone());
            }
        }
        values
    }

    /// A text kept within the key's maxLength, counted as the Host counts.
    pub fn held(&self, key: &str, text: &str) -> String {
        match self.fields.get(key) {
            Some(Field::Str { max: Some(max), .. }) if length(text) > *max => text.chars().take(*max).collect(),
            _ => text.to_owned(),
        }
    }

    /// The submission's gaps, none when it is ready.
    pub fn gaps(&self, values: &Values) -> Vec<Gap> {
        match self.submission(values) {
            Submission::Ready(_) => Vec::new(),
            Submission::Blocked(gaps) => gaps,
        }
    }
}

/// What this client makes of a render spec payload.
#[derive(Debug, Clone, PartialEq)]
pub enum Reading {
    Form(Box<Form>),
    /// A catalog, catalog version, component or property this build does
    /// not know.
    NeedsNewer,
    /// Broken against the catalog this build knows; the pointer names the
    /// place, never a value.
    Unreadable(String),
}

/// Reads `payload`, the flat artifact object of the single read or a bare
/// payload.
pub fn read(payload: &Value) -> Reading {
    let Some(object) = payload.as_object() else { return Reading::Unreadable(String::new()) };
    match (Reader { payload: object }).form() {
        Ok(form) => Reading::Form(Box::new(form)),
        Err(Stop::Newer) => Reading::NeedsNewer,
        Err(Stop::Broken(pointer)) => Reading::Unreadable(pointer),
    }
}

enum Stop {
    Newer,
    Broken(String),
}

fn broken<T>(pointer: impl Into<String>) -> Result<T, Stop> {
    Err(Stop::Broken(pointer.into()))
}

struct Reader<'a> {
    payload: &'a Map<String, Value>,
}

/// What the tree walk collects.
#[derive(Default)]
struct Walk {
    visited: BTreeSet<String>,
    bound: BTreeSet<String>,
    toggles: Vec<String>,
}

fn text_in(value: Option<&Value>, pointer: &str, min: usize, max: usize) -> Result<String, Stop> {
    match value.and_then(Value::as_str) {
        Some(text) if (min..=max).contains(&length(text)) => Ok(text.to_owned()),
        _ => broken(pointer),
    }
}

fn closed(object: &Map<String, Value>, allowed: &[&str], pointer: &str) -> Result<(), Stop> {
    let mut extra: Vec<&String> = object.keys().filter(|k| !allowed.contains(&k.as_str())).collect();
    extra.sort();
    match extra.first() {
        Some(key) => broken(format!("{pointer}/{}", if is_identifier(key) { key.as_str() } else { "" })),
        None => Ok(()),
    }
}

fn as_int(value: &Value) -> Option<i64> {
    value.as_i64().filter(|n| n.abs() <= INTEGER_LIMIT)
}

impl Reader<'_> {
    fn form(&self) -> Result<Form, Stop> {
        let p = self.payload;
        if p.get("catalog").and_then(Value::as_str) != Some(CATALOG) || p.get("catalog_version").and_then(as_int) != Some(CATALOG_VERSION) {
            return Err(Stop::Newer);
        }
        let version = text_in(p.get("version"), "/version", 1, 80)?;
        let Some(spec_value) = p.get("spec") else { return broken("/spec") };
        let Some(spec) = spec_value.as_object() else { return broken("/spec") };
        closed(spec, &["root", "elements", "state"], "/spec")?;
        if canonical_len(spec_value) > MAX_SPEC_BYTES {
            return broken("/spec");
        }
        let (fields, required) = self.schema()?;
        let Some(submit) = p.get("submit").and_then(Value::as_object) else { return broken("/submit") };
        closed(submit, &["intent", "label"], "/submit")?;
        let intent = match submit.get("intent").and_then(Value::as_str) {
            Some(intent) if is_identifier(intent) => intent.to_owned(),
            _ => return broken("/submit/intent"),
        };
        let label = text_in(submit.get("label"), "/submit/label", 1, 40)?;

        let elements = match spec.get("elements").and_then(Value::as_object) {
            Some(e) if (1..=MAX_ELEMENTS).contains(&e.len()) && e.keys().all(|k| is_identifier(k)) => e,
            _ => return broken("/spec/elements"),
        };
        let root_id = match spec.get("root").and_then(Value::as_str) {
            Some(root) if elements.contains_key(root) => root.to_owned(),
            _ => return broken("/spec/root"),
        };
        let mut walk = Walk::default();
        let root = self.element(&root_id, elements, &fields, 1, false, &mut walk)?;
        // One rooted tree: nothing left over, nothing shared.
        if walk.visited.len() != elements.len() {
            return broken("/spec/elements");
        }

        let mut initial = Values::new();
        let empty = Value::Object(Map::new());
        let state_value = spec.get("state").unwrap_or(&empty);
        let Some(state) = state_value.as_object().filter(|_| canonical_len(state_value) <= MAX_STATE_BYTES) else {
            return broken("/spec/state");
        };
        for (key, value) in state {
            let pointer = format!("/spec/state/{}", if is_identifier(key) { key.as_str() } else { "" });
            let answer = match (fields.get(key), value) {
                (Some(Field::Str { min, max }), Value::String(text)) => {
                    let count = length(text);
                    if count < *min || max.is_some_and(|max| count > max) {
                        return broken(pointer);
                    }
                    Answer::Str(text.clone())
                }
                (Some(Field::Choice(allowed)), Value::String(chosen)) => {
                    if !allowed.contains(chosen) {
                        return broken(pointer);
                    }
                    Answer::Str(chosen.clone())
                }
                (Some(Field::Boolean), Value::Bool(flag)) => Answer::Bool(*flag),
                (Some(Field::Integer), value) if as_int(value).is_some() && !value.is_boolean() => Answer::Int(as_int(value).unwrap_or_default()),
                _ => return broken(pointer),
            };
            initial.insert(key.clone(), answer);
        }
        for key in &walk.toggles {
            initial.entry(key.clone()).or_insert(Answer::Bool(false));
        }
        Ok(Form {
            version,
            root,
            submit_intent: intent,
            submit_label: label,
            fields,
            required,
            initial,
            bound: walk.bound,
            spec: spec_value.clone(),
        })
    }

    fn schema(&self) -> Result<(BTreeMap<String, Field>, BTreeSet<String>), Stop> {
        let base = "/answer_schema";
        let Some(schema) = self.payload.get("answer_schema").and_then(Value::as_object) else { return broken(base) };
        closed(schema, &["type", "properties", "required", "additionalProperties"], base)?;
        if schema.get("type").and_then(Value::as_str) != Some("object") {
            return broken(format!("{base}/type"));
        }
        if schema.get("additionalProperties").is_some_and(|a| a != &Value::Bool(false)) {
            return broken(format!("{base}/additionalProperties"));
        }
        let empty = Map::new();
        let properties = match schema.get("properties") {
            None => &empty,
            Some(Value::Object(p)) if p.keys().all(|k| is_identifier(k)) => p,
            Some(_) => return broken(format!("{base}/properties")),
        };
        let mut fields = BTreeMap::new();
        for (key, value) in properties {
            let pointer = format!("{base}/properties/{key}");
            let Some(property) = value.as_object() else { return broken(pointer) };
            closed(property, &["type", "enum", "minLength", "maxLength"], &pointer)?;
            let kind = property.get("type").map(|t| t.as_str().unwrap_or("\u{0}"));
            if let Some(options) = property.get("enum") {
                let values: Option<Vec<String>> = options.as_array().map(|a| a.iter().map(|v| v.as_str().map(str::to_owned)).collect::<Option<Vec<_>>>()).flatten();
                let ok = matches!(kind, None | Some("string"))
                    && !property.contains_key("minLength")
                    && !property.contains_key("maxLength")
                    && values.as_ref().is_some_and(|v| !v.is_empty() && v.iter().collect::<BTreeSet<_>>().len() == v.len());
                if !ok {
                    return broken(format!("{pointer}/enum"));
                }
                fields.insert(key.clone(), Field::Choice(values.unwrap_or_default()));
                continue;
            }
            match kind {
                Some("string") => {
                    let bound = |name: &str| property.get(name).map(|v| as_int(v).filter(|n| *n >= 0).map(|n| n as usize));
                    let min = match bound("minLength") {
                        None => 0,
                        Some(Some(n)) => n,
                        Some(None) => return broken(pointer),
                    };
                    let max = match bound("maxLength") {
                        None => None,
                        Some(Some(n)) if n >= min => Some(n),
                        Some(_) => return broken(pointer),
                    };
                    fields.insert(key.clone(), Field::Str { min, max });
                }
                Some(kind @ ("boolean" | "integer")) => {
                    if property.contains_key("minLength") || property.contains_key("maxLength") {
                        return broken(pointer);
                    }
                    fields.insert(key.clone(), if kind == "boolean" { Field::Boolean } else { Field::Integer });
                }
                _ => return broken(format!("{pointer}/type")),
            }
        }
        let names: Option<Vec<String>> = match schema.get("required") {
            None => Some(Vec::new()),
            Some(Value::Array(items)) => items.iter().map(|v| v.as_str().map(str::to_owned)).collect(),
            Some(_) => None,
        };
        let Some(names) = names else { return broken(format!("{base}/required")) };
        let required: BTreeSet<String> = names.iter().cloned().collect();
        if required.len() != names.len() || !names.iter().all(|n| fields.contains_key(n)) {
            return broken(format!("{base}/required"));
        }
        Ok((fields, required))
    }

    fn element(&self, id: &str, elements: &Map<String, Value>, fields: &BTreeMap<String, Field>, depth: usize, under_card: bool, walk: &mut Walk) -> Result<Element, Stop> {
        let base = format!("/spec/elements/{id}");
        if depth > MAX_DEPTH || !walk.visited.insert(id.to_owned()) {
            return broken(base);
        }
        let Some(raw) = elements.get(id).and_then(Value::as_object) else { return broken(base) };
        closed(raw, &["type", "props", "children"], &base)?;
        let Some(kind) = raw.get("type").and_then(Value::as_str) else { return broken(format!("{base}/type")) };
        let Some(allowed) = component_props(kind) else { return Err(Stop::Newer) };
        let empty = Map::new();
        let props = match raw.get("props") {
            None => &empty,
            Some(Value::Object(props)) => props,
            Some(_) => return broken(format!("{base}/props")),
        };
        if !props.keys().all(|k| allowed.contains(&k.as_str())) {
            return Err(Stop::Newer);
        }
        let child_ids: Vec<String> = match raw.get("children") {
            None => Vec::new(),
            Some(Value::Array(items)) => match items.iter().map(|v| v.as_str().map(str::to_owned)).collect::<Option<Vec<_>>>() {
                Some(ids) => ids,
                None => return broken(format!("{base}/children")),
            },
            Some(_) => return broken(format!("{base}/children")),
        };
        if !child_ids.is_empty() && !container(kind) {
            return broken(format!("{base}/children"));
        }
        if kind == "card" && under_card {
            return broken(base);
        }
        let prop = |name: &str| format!("{base}/props/{name}");
        let bind = |check: &dyn Fn(&Field) -> bool, walk: &mut Walk| -> Result<String, Stop> {
            let key = state_key(props.get("value"), "$bindState").ok_or_else(|| Stop::Broken(prop("value")))?;
            match fields.get(&key) {
                Some(field) if check(field) && walk.bound.insert(key.clone()) => Ok(key),
                _ => broken(prop("value")),
            }
        };
        let kind_read = match kind {
            "stack" => {
                let axis = match props.get("axis") {
                    None => "vertical",
                    Some(v) => v.as_str().unwrap_or(""),
                };
                if axis != "vertical" && axis != "horizontal" {
                    return broken(prop("axis"));
                }
                Kind::Stack { horizontal: axis == "horizontal" }
            }
            "card" => Kind::Card { title: props.get("title").map(|t| text_in(Some(t), &prop("title"), 0, 120)).transpose()? },
            "heading" => {
                let level = match props.get("level") {
                    None => 2,
                    Some(v) => as_int(v).unwrap_or(0),
                };
                if !(1..=3).contains(&level) {
                    return broken(prop("level"));
                }
                Kind::Heading { text: text_in(props.get("text"), &prop("text"), 0, 200)?, level: level as u8 }
            }
            "text" => match props.get("text") {
                Some(Value::String(_)) => Kind::Text(TextSource::Literal(text_in(props.get("text"), &prop("text"), 0, 4000)?)),
                value => {
                    let key = state_key(value, "$state").ok_or_else(|| Stop::Broken(prop("text")))?;
                    if !fields.contains_key(&key) {
                        return broken(prop("text"));
                    }
                    Kind::Text(TextSource::State(key))
                }
            },
            "text_field" => {
                let label = text_in(props.get("label"), &prop("label"), 1, 120)?;
                let placeholder = props.get("placeholder").map(|p| text_in(Some(p), &prop("placeholder"), 0, 120)).transpose()?;
                let multiline = match props.get("multiline") {
                    None => false,
                    Some(Value::Bool(flag)) => *flag,
                    Some(_) => return broken(prop("multiline")),
                };
                let key = bind(&|f| matches!(f, Field::Str { .. }), walk)?;
                Kind::TextField { label, placeholder, multiline, key }
            }
            "toggle" => {
                let label = text_in(props.get("label"), &prop("label"), 1, 120)?;
                let key = bind(&|f| *f == Field::Boolean, walk)?;
                walk.toggles.push(key.clone());
                Kind::Toggle { label, key }
            }
            "choice" => {
                let label = text_in(props.get("label"), &prop("label"), 1, 120)?;
                let raw_options = match props.get("options").and_then(Value::as_array) {
                    Some(items) if (2..=12).contains(&items.len()) => items,
                    _ => return broken(prop("options")),
                };
                let mut options = Vec::new();
                for item in raw_options {
                    let Some(option) = item.as_object().filter(|o| o.len() == 2 && o.contains_key("value") && o.contains_key("label")) else {
                        return broken(prop("options"));
                    };
                    options.push(Opt { value: text_in(option.get("value"), &prop("options"), 1, 120)?, label: text_in(option.get("label"), &prop("options"), 1, 120)? });
                }
                // The answer key's enum equals the option values, in order.
                let values: Vec<String> = options.iter().map(|o| o.value.clone()).collect();
                let key = bind(&|f| *f == Field::Choice(values.clone()), walk)?;
                Kind::Choice { label, options, key }
            }
            _ => Kind::Divider,
        };
        let mut children = Vec::new();
        for child in &child_ids {
            children.push(self.element(child, elements, fields, depth + 1, under_card || kind == "card", walk)?);
        }
        Ok(Element { id: id.to_owned(), kind: kind_read, children })
    }
}

/// The key a `{"$state": "/key"}` or `{"$bindState": "/key"}` names.
fn state_key(value: Option<&Value>, operator: &str) -> Option<String> {
    let object = value?.as_object().filter(|o| o.len() == 1)?;
    let key = object.get(operator)?.as_str()?.strip_prefix('/')?;
    is_identifier(key).then(|| key.to_owned())
}

// ---- the Host's read -------------------------------------------------------

/// The Host's witness of which spec bytes a row or a read stands for.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Identity {
    pub version: String,
    pub bytes: usize,
    pub sha256: String,
}

impl Identity {
    pub fn of(value: Option<&Value>) -> Option<Identity> {
        let object = value?.as_object()?;
        Some(Identity {
            version: object.get("version")?.as_str()?.to_owned(),
            bytes: usize::try_from(object.get("bytes")?.as_u64()?).ok()?,
            sha256: object.get("sha256")?.as_str()?.to_owned(),
        })
    }
}

/// The one accepted submission, as the single read returns it.
#[derive(Debug, Clone, PartialEq)]
pub struct Accepted {
    pub submission_id: String,
    pub version: String,
    pub intent: String,
    pub state: Values,
    pub accepted_at: i64,
    pub delivery_status: String,
}

impl Accepted {
    pub fn of(value: Option<&Value>) -> Option<Accepted> {
        let object = value?.as_object()?;
        let answers = object.get("answers")?.as_object()?;
        let mut state = Values::new();
        for (key, value) in answers.get("state")?.as_object()? {
            state.insert(key.clone(), Answer::from_value(value)?);
        }
        Some(Accepted {
            submission_id: object.get("submission_id")?.as_str()?.to_owned(),
            version: object.get("version")?.as_str()?.to_owned(),
            intent: answers.get("intent")?.as_str()?.to_owned(),
            state,
            accepted_at: object.get("accepted_at")?.as_i64()?,
            delivery_status: object.get("delivery_status")?.as_str()?.to_owned(),
        })
    }
}

/// `GET /artifacts/<id>` for a render spec: the row's fields flat beside the
/// payload.
#[derive(Debug, Clone, PartialEq)]
pub struct Read {
    pub artifact_id: String,
    pub status: String,
    pub updated_at: i64,
    pub reading: Reading,
    pub identity: Option<Identity>,
    pub submission: Option<Accepted>,
}

impl Read {
    /// None when `artifact` is not a render spec's read.
    pub fn of(artifact: &Value) -> Option<Read> {
        if artifact.get("type").and_then(Value::as_str) != Some("render_spec") {
            return None;
        }
        Some(Read {
            artifact_id: artifact.get("artifact_id")?.as_str()?.to_owned(),
            status: artifact.get("status")?.as_str()?.to_owned(),
            updated_at: artifact.get("updated_at")?.as_i64()?,
            identity: Identity::of(artifact.get("content_identity")),
            submission: Accepted::of(artifact.get("submission")),
            reading: read(artifact),
        })
    }

    /// The spec read is the one the Host says it gave: its version, its
    /// canonical size and its hash.
    pub fn matches(&self) -> bool {
        let (Reading::Form(form), Some(identity)) = (&self.reading, &self.identity) else { return false };
        let Some(raw) = canonical(&form.spec) else { return false };
        identity.version == form.version && identity.bytes == raw.len() && identity.sha256 == hash(&raw)
    }
}

// ---- refusals --------------------------------------------------------------

/// What a refusal of the form path means to the screen.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Meaning {
    /// version_mismatch: read the form again, keep what was typed.
    Reopen,
    AlreadyAnswered,
    /// not_open: the form stays readable with Send off.
    Closed,
    /// client_contract: this Host does not let this client answer forms.
    AppTooOld,
    /// state_value, missing_key, answer_size.
    Answer,
    /// catalog_unavailable.
    HostFault,
    Other,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Refusal {
    pub code: String,
    pub path: String,
    pub message: String,
}

impl Refusal {
    /// `{"error": {code, path, message}}` from a refused submission's body.
    pub fn of(body: &str) -> Option<Refusal> {
        let value: Value = serde_json::from_str(body).ok()?;
        let error = value.get("error")?.as_object()?;
        Some(Refusal {
            code: error.get("code")?.as_str()?.to_owned(),
            path: error.get("path").and_then(Value::as_str).unwrap_or_default().to_owned(),
            message: error.get("message").and_then(Value::as_str).unwrap_or_default().to_owned(),
        })
    }

    pub fn meaning(&self) -> Meaning {
        match self.code.as_str() {
            "version_mismatch" => Meaning::Reopen,
            "already_answered" => Meaning::AlreadyAnswered,
            "not_open" => Meaning::Closed,
            "client_contract" => Meaning::AppTooOld,
            "state_value" | "missing_key" | "answer_size" => Meaning::Answer,
            "catalog_unavailable" => Meaning::HostFault,
            _ => Meaning::Other,
        }
    }
}

// ---- the footer's words ----------------------------------------------------

/// The line above Send (iOS's footnote): how the answer went once it was
/// accepted, else the owner's note, else what still holds Send back.
/// `captions` names a text field's key by its label.
pub fn footnote(form: &Form, values: &Values, accepted: Option<&Accepted>, note: &str, closed: bool, captions: &BTreeMap<String, String>, when: &str) -> String {
    if let Some(accepted) = accepted {
        let delivered = if accepted.delivery_status == "delivered" { "The agent has it." } else { "The agent gets it when it is next awake." };
        return if when.is_empty() { format!("Answered. {delivered}") } else { format!("Answered {when}. {delivered}") };
    }
    if !note.is_empty() || closed {
        return note.to_owned();
    }
    let gaps = form.gaps(values);
    if gaps.contains(&Gap::AnswersTooLarge) {
        return "The answers are too long to send. Shorten the text.".into();
    }
    let missing = gaps.iter().filter(|g| matches!(g, Gap::Missing(_))).count();
    if missing == 1 {
        return "One required answer is missing.".into();
    }
    if missing > 1 {
        return format!("{missing} required answers are missing.");
    }
    let short = gaps.iter().filter_map(|g| if let Gap::TooShort(key, min) = g { Some((key, *min)) } else { None }).min_by(|a, b| a.0.cmp(b.0));
    if let Some((key, min)) = short {
        return format!("{} needs at least {min} characters.", captions.get(key).map_or("A text", String::as_str));
    }
    String::new()
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    /// The iOS app's preview fixture: every component of the catalog once.
    pub fn release_check() -> Value {
        json!({
          "version": "preview-1", "catalog": "clarp.form", "catalog_version": 1,
          "spec": {
            "root": "root",
            "elements": {
              "root": {"type": "stack", "props": {}, "children": ["title", "intro", "verdict", "rule", "details", "pair", "echo_head", "echo"]},
              "title": {"type": "heading", "props": {"text": "Build 2968", "level": 1}},
              "intro": {"type": "text", "props": {"text": "The simulator checks passed."}},
              "verdict": {"type": "card", "props": {"title": "Your call"}, "children": ["decision", "reason"]},
              "decision": {"type": "choice", "props": {"label": "What should happen", "value": {"$bindState": "/decision"}, "options": [
                {"value": "ship", "label": "Send it to all testers"},
                {"value": "internal", "label": "Send it to the internal group only"},
                {"value": "hold", "label": "Hold it"},
                {"value": "drop", "label": "Drop this build"}]}},
              "reason": {"type": "text_field", "props": {"label": "Reason", "placeholder": "Why", "multiline": true, "value": {"$bindState": "/reason"}}},
              "rule": {"type": "divider", "props": {}},
              "details": {"type": "card", "props": {}, "children": ["sub", "testers", "notify", "quiet"]},
              "sub": {"type": "heading", "props": {"text": "Details", "level": 2}},
              "testers": {"type": "text_field", "props": {"label": "Tester group", "placeholder": "internal", "value": {"$bindState": "/testers"}}},
              "notify": {"type": "toggle", "props": {"label": "Tell the testers", "value": {"$bindState": "/notify"}}},
              "quiet": {"type": "toggle", "props": {"label": "Skip the release notes", "value": {"$bindState": "/quiet"}}},
              "pair": {"type": "stack", "props": {"axis": "horizontal"}, "children": ["left", "right"]},
              "left": {"type": "text", "props": {"text": "Left"}},
              "right": {"type": "text", "props": {"text": "Right"}},
              "echo_head": {"type": "heading", "props": {"text": "Group as typed", "level": 3}},
              "echo": {"type": "text", "props": {"text": {"$state": "/testers"}}}
            },
            "state": {"notify": true, "testers": "internal"}
          },
          "submit": {"intent": "release_call", "label": "Send my call"},
          "answer_schema": {"type": "object", "required": ["decision", "reason"], "properties": {
            "decision": {"type": "string", "enum": ["ship", "internal", "hold", "drop"]},
            "reason": {"type": "string", "minLength": 10, "maxLength": 400},
            "testers": {"type": "string", "maxLength": 20},
            "notify": {"type": "boolean"}, "quiet": {"type": "boolean"}}}
        })
    }

    fn form(payload: &Value) -> Form {
        match read(payload) {
            Reading::Form(form) => *form,
            other => panic!("not a form: {other:?}"),
        }
    }

    #[test]
    fn the_header_is_the_strict_form_the_host_reads() {
        assert_eq!(client_header("0.1.0"), "desktop/0.1.0 contract=1");
        assert_eq!(client_header("1.0 beta/x"), "desktop/1.0betax contract=1");
        assert_eq!(client_header(""), "desktop/0 contract=1");
    }

    #[test]
    fn canonical_bytes_are_the_hosts() {
        // The Host's wire vector (tests/fixtures/render_spec/wire.json).
        let value = json!({"\u{e000}": 1, "\u{10000}": "\u{f8}", "text": "e\u{301}"});
        let raw = canonical(&value).unwrap();
        let hex: String = raw.iter().map(|b| format!("{b:02x}")).collect();
        assert_eq!(hex, "7b2274657874223a2265cc81222c22f0908080223a22c3b8222c22ee8080223a317d");
        assert_eq!(hash(&raw), "c1:ed8a90bab40a60e3253c43eefefc99712ef2842050b6e5fa64c34d4f6170a54e");
        assert_eq!(canonical(&json!({"a": "\u{1}\n\"\\"})).unwrap(), br#"{"a":"\u0001\n\"\\"}"#.to_vec());
        assert!(canonical(&json!({"a": 1.5})).is_none());
        assert!(canonical(&json!(9_007_199_254_740_992_i64)).is_none());
    }

    #[test]
    fn the_preview_reads_with_every_component() {
        let form = form(&release_check());
        assert_eq!(form.version, "preview-1");
        assert_eq!(form.submit_label, "Send my call");
        assert_eq!(form.root.children.len(), 8);
        assert_eq!(form.required, ["decision", "reason"].iter().map(|s| s.to_string()).collect());
        assert_eq!(form.initial.get("notify"), Some(&Answer::Bool(true)));
        assert_eq!(form.initial.get("quiet"), Some(&Answer::Bool(false)), "a toggle without a value starts off");
        assert_eq!(form.initial.get("testers"), Some(&Answer::Str("internal".into())));
        assert!(matches!(form.root.children[2].kind, Kind::Card { title: Some(ref t) } if t == "Your call"));
        assert!(matches!(form.root.children[5].kind, Kind::Stack { horizontal: true }));
        assert_eq!(form.root.children[7].kind, Kind::Text(TextSource::State("testers".into())));
    }

    #[test]
    fn the_hosts_build_decision_reads() {
        let payload: Value = serde_json::from_str(include_str!("../../../tests/fixtures/render_spec/build-decision.json")).unwrap();
        let form = form(&payload);
        assert_eq!(form.submit_intent, "submit_decision");
        assert!(matches!(form.fields["choice"], Field::Choice(ref v) if v == &["send", "review"]));
    }

    #[test]
    fn send_waits_for_the_required_answers() {
        let form = form(&release_check());
        let mut values = form.initial.clone();
        assert_eq!(form.gaps(&values), vec![Gap::Missing("decision".into()), Gap::Missing("reason".into())]);
        let captions = BTreeMap::from([("reason".to_owned(), "Reason".to_owned())]);
        assert_eq!(footnote(&form, &values, None, "", false, &captions, ""), "2 required answers are missing.");
        values.insert("decision".into(), Answer::Str("hold".into()));
        assert_eq!(footnote(&form, &values, None, "", false, &captions, ""), "One required answer is missing.");
        values.insert("reason".into(), Answer::Str("too short".into()));
        assert_eq!(form.gaps(&values), vec![Gap::TooShort("reason".into(), 10)]);
        assert_eq!(footnote(&form, &values, None, "", false, &captions, ""), "Reason needs at least 10 characters.");
        values.insert("reason".into(), Answer::Str("The crash is not fixed.".into()));
        let Submission::Ready(answers) = form.submission(&values) else { panic!("ready") };
        assert_eq!(answers, json!({"intent": "release_call", "state": {
            "decision": "hold", "reason": "The crash is not fixed.", "testers": "internal", "notify": true, "quiet": false}}));
        assert_eq!(footnote(&form, &values, None, "", false, &captions, ""), "");
    }

    #[test]
    fn an_empty_text_and_an_unselected_choice_are_left_out() {
        let form = form(&release_check());
        let mut values = form.initial.clone();
        values.insert("decision".into(), Answer::Str("ship".into()));
        values.insert("reason".into(), Answer::Str("All checks green.".into()));
        values.insert("testers".into(), Answer::Str(String::new()));
        let Submission::Ready(answers) = form.submission(&values) else { panic!("ready") };
        assert!(answers["state"].get("testers").is_none());
        values.insert("decision".into(), Answer::Str("other".into()));
        assert_eq!(form.gaps(&values), vec![Gap::Missing("decision".into())], "a value outside the options is no answer");
    }

    #[test]
    fn a_text_is_held_to_its_max_length_in_code_points() {
        let form = form(&release_check());
        assert_eq!(form.held("testers", &"ø".repeat(25)), "ø".repeat(20));
        assert_eq!(form.held("reason", "short"), "short");
    }

    #[test]
    fn typed_values_survive_a_new_version_where_their_key_still_takes_them() {
        let form = form(&release_check());
        let old = Values::from([
            ("reason".to_owned(), Answer::Str("kept".into())),
            ("decision".to_owned(), Answer::Str("gone-option".into())),
            ("unbound".to_owned(), Answer::Str("x".into())),
        ]);
        let carried = form.carrying(&old);
        assert_eq!(carried.get("reason"), Some(&Answer::Str("kept".into())));
        assert_eq!(carried.get("decision"), None);
        assert_eq!(carried.get("unbound"), None);
    }

    #[test]
    fn an_unknown_catalog_component_or_property_needs_a_newer_app() {
        let mut payload = release_check();
        payload["catalog_version"] = json!(2);
        assert_eq!(read(&payload), Reading::NeedsNewer);
        let mut payload = release_check();
        payload["spec"]["elements"]["rule"]["type"] = json!("chart");
        assert_eq!(read(&payload), Reading::NeedsNewer);
        let mut payload = release_check();
        payload["spec"]["elements"]["rule"]["props"] = json!({"weight": 2});
        assert_eq!(read(&payload), Reading::NeedsNewer);
    }

    #[test]
    fn a_broken_spec_is_unreadable_at_its_place() {
        let mut payload = release_check();
        payload["spec"]["elements"]["title"]["props"]["level"] = json!(9);
        assert_eq!(read(&payload), Reading::Unreadable("/spec/elements/title/props/level".into()));
        let mut payload = release_check();
        payload["spec"]["elements"]["details"]["children"] = json!(["sub", "testers", "notify", "quiet", "verdict"]);
        assert!(matches!(read(&payload), Reading::Unreadable(_)), "a card in a card, and a shared element");
        let mut payload = release_check();
        payload["spec"]["elements"]["orphan"] = json!({"type": "divider", "props": {}});
        assert_eq!(read(&payload), Reading::Unreadable("/spec/elements".into()));
        let mut payload = release_check();
        payload["answer_schema"]["properties"]["decision"]["enum"] = json!(["ship", "hold"]);
        assert_eq!(read(&payload), Reading::Unreadable("/spec/elements/decision/props/value".into()), "options and enum differ");
        let mut payload = release_check();
        payload["spec"]["state"]["reason"] = json!("short");
        assert_eq!(read(&payload), Reading::Unreadable("/spec/state/reason".into()));
        let mut payload = release_check();
        payload["spec"]["elements"]["reason"]["props"]["value"] = json!({"$bindState": "/decision"});
        assert!(matches!(read(&payload), Reading::Unreadable(_)), "a key bound twice");
        assert!(matches!(read(&json!([])), Reading::Unreadable(_)));
    }

    #[test]
    fn a_read_matches_its_content_identity() {
        let mut artifact = release_check();
        let spec = canonical(&artifact["spec"]).unwrap();
        artifact.as_object_mut().unwrap().extend([
            ("type".into(), json!("render_spec")),
            ("artifact_id".into(), json!("rs1")),
            ("status".into(), json!("completed")),
            ("updated_at".into(), json!(5)),
            ("content_identity".into(), json!({"version": "preview-1", "bytes": spec.len(), "sha256": hash(&spec)})),
            ("submission".into(), json!({"submission_id": "s-0123456789abcdef", "version": "preview-1",
                "answers": {"intent": "release_call", "state": {"decision": "hold", "notify": true}},
                "accepted_at": 1_795_000_000_000_i64, "delivery_status": "pending"})),
        ]);
        let read = Read::of(&artifact).unwrap();
        assert!(read.matches());
        let accepted = read.submission.clone().unwrap();
        assert_eq!(accepted.state.get("decision"), Some(&Answer::Str("hold".into())));
        let form = form(&artifact);
        assert_eq!(footnote(&form, &Values::new(), Some(&accepted), "", false, &BTreeMap::new(), ""), "Answered. The agent gets it when it is next awake.");
        artifact["content_identity"]["sha256"] = json!("c1:00");
        assert!(!Read::of(&artifact).unwrap().matches());
        assert!(Read::of(&json!({"type": "html_form"})).is_none());
    }

    #[test]
    fn a_refusal_is_read_by_its_code() {
        let refusal = Refusal::of(r#"{"error": {"code": "version_mismatch", "path": "/version", "message": "The form changed."}}"#).unwrap();
        assert_eq!(refusal.meaning(), Meaning::Reopen);
        assert_eq!(Refusal::of(r#"{"error": {"code": "client_contract", "path": ""}}"#).unwrap().meaning(), Meaning::AppTooOld);
        assert_eq!(Refusal::of(r#"{"error": {"code": "state_value"}}"#).unwrap().meaning(), Meaning::Answer);
        assert!(Refusal::of("HTTP 502").is_none());
    }
}
