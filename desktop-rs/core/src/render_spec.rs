//! Native forms (`render_spec`, Host feature `render_spec`, issue 428): the
//! closed catalog `clarp.form` versions 1 and 2 as this client draws them,
//! the reading of a published spec, and the one answer the form path takes.
//! A port of the iOS app's `RenderSpec.swift` and `RenderSpecV2.swift`, the
//! reference for every client.
//!
//! The Host's `contract/catalogs/clarp.form.v1.json` and `.v2.json` are the
//! authority; this client checks every spec itself and does not rely on the
//! Host having done so. A catalog, catalog version, component or property
//! this build does not know is [`Reading::NeedsNewer`]; anything broken
//! against the version the spec names is [`Reading::Unreadable`], with a
//! pointer that never names a value.
//!
//! Version 2 (issue 673) is version 1 plus a chart and a table to read and a
//! button that submits the form: a form with buttons has no footer submit,
//! and is sent with intent `button` and the pressed button's action in its
//! state. An element type a later catalog adds slots in as one more [`Kind`]
//! and one more arm of [`Reader::element`]; until then it reads as needing a
//! newer app.

use std::collections::{BTreeMap, BTreeSet};

use serde_json::{Map, Value};
use sha2::{Digest, Sha256};

/// The name this client gives the Host in `X-Clarp-Client`, matched exactly
/// by the Host's per-client tables (`server_identity.CLIENT_NAME`).
pub const CLIENT_NAME: &str = "desktop";

/// The desktop's own client contract series (independent of iOS's and
/// Android's). 1: draws and answers `render_spec` forms of catalog
/// `clarp.form` version 1. 2: also version 2 (chart, table, button; the
/// Host's `render_spec_v2_min_contract` names `desktop = 2`).
pub const CLIENT_CONTRACT: u32 = 2;

/// `X-Clarp-Client`: `<name>/<build> contract=<n>`, the strict form the Host
/// reads (`server_identity.strict_client`).
pub fn client_header(build: &str) -> String {
    let build: String = build.chars().filter(|c| c.is_ascii_alphanumeric() || matches!(c, '.' | '_' | '-')).take(80).collect();
    let build = if build.is_empty() { "0".to_owned() } else { build };
    format!("{CLIENT_NAME}/{build} contract={CLIENT_CONTRACT}")
}

/// The catalog this build draws, and its newest version (every version
/// from 1 up to it is drawn).
pub const CATALOG: &str = "clarp.form";
pub const CATALOG_VERSION: i64 = 2;
/// A button form is sent with this intent; its state names the pressed
/// button's action under [`ACTION_KEY`].
pub const BUTTON_INTENT: &str = "button";
pub const ACTION_KEY: &str = "action";
pub const MAX_ELEMENTS: usize = 200;
pub const MAX_DEPTH: usize = 12;
pub const MAX_SPEC_BYTES: usize = 32_768;
pub const MAX_STATE_BYTES: usize = 4_096;
pub const MAX_ANSWER_BYTES: usize = 16_384;
pub const INTEGER_LIMIT: i64 = 9_007_199_254_740_991;

/// Component name to the properties it takes, in catalog `version`.
fn component_props(kind: &str, version: i64) -> Option<&'static [&'static str]> {
    Some(match kind {
        "stack" => &["axis"],
        "card" => &["title"],
        "heading" => &["text", "level"],
        "text" => &["text"],
        "text_field" => &["label", "placeholder", "multiline", "value"],
        "toggle" => &["label", "value"],
        "choice" => &["label", "options", "value"],
        "divider" => &[],
        "chart" if version >= 2 => &["kind", "title", "x_label", "y_label", "unit", "decimals", "series"],
        "table" if version >= 2 => &["title", "columns", "rows"],
        "button" if version >= 2 => &["label", "action", "style", "confirm"],
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

// ---- numbers ---------------------------------------------------------------

/// A number as version 2 draws it (issue 673): the integer divided by
/// 10^decimals, with exactly `decimals` digits after `separator`, never
/// grouped (12500 stays 12500). Exact for every integer the contract allows:
/// no floating point on the way.
pub fn number_text(value: i64, decimals: u32, separator: char) -> String {
    let places = decimals.min(6) as usize;
    let digits = value.unsigned_abs().to_string();
    let padded = if digits.len() <= places { format!("{}{digits}", "0".repeat(places + 1 - digits.len())) } else { digits };
    let (whole, fraction) = padded.split_at(padded.len() - places);
    let mut out = String::with_capacity(padded.len() + 2);
    if value < 0 {
        out.push('-');
    }
    out.push_str(whole);
    if places > 0 {
        out.push(separator);
        out.push_str(fraction);
    }
    out
}

/// The decimal separator of a POSIX locale name (`nb_NO.UTF-8`,
/// `de_CH@euro`, `en-US`, `C`), as CLDR has it for the language and, where
/// it differs, the region.
pub fn decimal_separator(locale: &str) -> char {
    let base = locale.split(['.', '@']).next().unwrap_or_default();
    let mut parts = base.split(['_', '-']);
    let language = parts.next().unwrap_or_default().to_ascii_lowercase();
    let region = parts.next().unwrap_or_default().to_ascii_uppercase();
    match (language.as_str(), region.as_str()) {
        ("de" | "it", "CH" | "LI") => '.',
        ("es", "MX" | "US" | "GT" | "HN" | "NI" | "PA" | "PR" | "DO" | "SV" | "PE") => '.',
        ("en", "ZA") => ',',
        (
            "af" | "az" | "be" | "bg" | "bs" | "ca" | "cs" | "da" | "de" | "el" | "es" | "et" | "eu" | "fi" | "fo" | "fr" | "gl" | "hr" | "hu" | "hy" | "id"
            | "is" | "it" | "ka" | "kk" | "ky" | "lt" | "lv" | "mk" | "mn" | "nb" | "nl" | "nn" | "no" | "pl" | "pt" | "ro" | "ru" | "se" | "sk" | "sl"
            | "sq" | "sr" | "sv" | "tr" | "uk" | "uz" | "vi",
            _,
        ) => ',',
        _ => '.',
    }
}

/// The device's decimal separator: the numeric locale the session runs in
/// (`LC_ALL`, then `LC_NUMERIC`, then `LANG`, as POSIX orders them).
pub fn device_decimal_separator() -> char {
    ["LC_ALL", "LC_NUMERIC", "LANG"]
        .iter()
        .find_map(|name| std::env::var(name).ok().filter(|v| !v.is_empty()))
        .map_or('.', |locale| decimal_separator(&locale))
}

/// The compact card's word for an answered form: "Answered · <button
/// label>" once the pressed button is known, "Answered" until then.
pub fn answered_word(label: Option<&str>) -> String {
    match label.filter(|l| !l.is_empty()) {
        Some(label) => format!("Answered · {label}"),
        None => "Answered".into(),
    }
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

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ChartKind {
    Bar,
    Line,
    Pie,
}

/// Every x of a chart is a string, or every x a number.
#[derive(Debug, Clone, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub enum X {
    Category(String),
    Number(i64),
}

impl X {
    /// As drawn: a category as given, a numeric x the integer given.
    pub fn words(&self) -> String {
        match self {
            X::Category(text) => text.clone(),
            X::Number(number) => number.to_string(),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Point {
    pub x: X,
    /// Drawn divided by 10^decimals.
    pub y: i64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Series {
    pub name: String,
    pub points: Vec<Point>,
}

/// A chart to read: bar, line or pie. Its values are not interactive.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Chart {
    pub kind: ChartKind,
    pub title: Option<String>,
    pub x_label: Option<String>,
    pub y_label: Option<String>,
    pub unit: Option<String>,
    pub decimals: u32,
    pub series: Vec<Series>,
}

impl Chart {
    /// Only a line draws numeric x to scale; bar and pie take every x as a
    /// category, in the order given.
    pub fn scaled_x(&self) -> bool {
        self.kind == ChartKind::Line && self.series.first().and_then(|s| s.points.first()).is_some_and(|p| matches!(p.x, X::Number(_)))
    }

    /// A legend when there is more than one series; a pie's names its points.
    pub fn legend(&self) -> bool {
        self.kind == ChartKind::Pie || self.series.len() > 1
    }

    /// A value as drawn, with the unit after it.
    pub fn value_text(&self, y: i64, separator: char) -> String {
        let number = number_text(y, self.decimals, separator);
        match self.unit.as_deref().filter(|u| !u.is_empty()) {
            Some(unit) => format!("{number} {unit}"),
            None => number,
        }
    }

    /// Categories in the order they first appear, across every series.
    pub fn categories(&self) -> Vec<X> {
        let mut seen = BTreeSet::new();
        self.series.iter().flat_map(|s| &s.points).filter(|p| seen.insert(p.x.clone())).map(|p| p.x.clone()).collect()
    }

    /// What a screen reader says for the whole chart (iOS's summary): its
    /// title and kind, then each series, a short one point by point and a
    /// long one by its range.
    pub fn summary(&self, separator: char) -> String {
        let mut parts = Vec::new();
        if let Some(title) = self.title.as_deref().filter(|t| !t.is_empty()) {
            parts.push(title.to_owned());
        }
        parts.push(match self.kind {
            ChartKind::Bar => "Bar chart",
            ChartKind::Line => "Line chart",
            ChartKind::Pie => "Pie chart",
        }.to_owned());
        if self.kind != ChartKind::Pie {
            if let Some(label) = self.x_label.as_deref().filter(|t| !t.is_empty()) {
                parts.push(format!("Across: {label}"));
            }
            if let Some(label) = self.y_label.as_deref().filter(|t| !t.is_empty()) {
                parts.push(format!("Up: {label}"));
            }
        }
        for series in &self.series {
            let name = if self.kind == ChartKind::Pie { String::new() } else { format!("{}: ", series.name) };
            let points = &series.points;
            if points.len() <= 12 {
                parts.push(name + &points.iter().map(|p| format!("{} {}", p.x.words(), self.value_text(p.y, separator))).collect::<Vec<_>>().join(", "));
            } else if let (Some(low), Some(high), Some(first), Some(last)) =
                (points.iter().min_by_key(|p| p.y), points.iter().max_by_key(|p| p.y), points.first(), points.last())
            {
                parts.push(format!(
                    "{name}{} points from {} to {}, lowest {} at {}, highest {} at {}",
                    points.len(),
                    first.x.words(),
                    last.x.words(),
                    self.value_text(low.y, separator),
                    low.x.words(),
                    self.value_text(high.y, separator),
                    high.x.words()
                ));
            }
        }
        parts.join(". ")
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Column {
    pub key: String,
    pub label: String,
    pub align_end: bool,
    pub decimals: u32,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Cell {
    Empty,
    Text(String),
    Number(i64),
    Flag(bool),
}

/// A table to read: one cell per column in each row, in column order; a
/// missing key or null is an empty cell.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Table {
    pub title: Option<String>,
    pub columns: Vec<Column>,
    pub rows: Vec<Vec<Cell>>,
}

impl Table {
    /// A cell's words as a screen reader says them (a flag is drawn as a
    /// check or a dash).
    pub fn words(cell: &Cell, column: &Column, separator: char) -> String {
        match cell {
            Cell::Empty => String::new(),
            Cell::Text(text) => text.clone(),
            Cell::Number(number) => number_text(*number, column.decimals, separator),
            Cell::Flag(flag) => if *flag { "Yes" } else { "No" }.to_owned(),
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ButtonStyle {
    Primary,
    Secondary,
    Destructive,
}

/// A button that submits the form with its action.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Button {
    pub label: String,
    pub action: String,
    pub style: ButtonStyle,
    /// Asked (OK / Cancel) before the form is sent.
    pub confirm: Option<String>,
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
    Chart(Chart),
    Table(Table),
    Button(Button),
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
    pub catalog_version: i64,
    pub root: Element,
    /// The footer's submit; a form with buttons has none (intent `button`,
    /// label ""): each button submits it.
    pub submit_intent: String,
    pub submit_label: String,
    /// Every button, in element order.
    pub buttons: Vec<Button>,
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
    /// The key of the form's one input when that is a choice (headings and
    /// text around it allowed, no button, no other field): a click on an
    /// option answers it.
    pub fn one_choice(&self) -> Option<&str> {
        if !self.buttons.is_empty() || self.fields.len() != 1 {
            return None;
        }
        self.fields.iter().find(|(_, field)| matches!(field, Field::Choice(_))).map(|(key, _)| key.as_str())
    }

    /// One click answers the form: its one input is a choice, or it is a
    /// form of buttons with no input at all.
    pub fn one_tap(&self) -> bool {
        self.one_choice().is_some() || (!self.buttons.is_empty() && self.fields.is_empty())
    }

    /// The answers exactly as the form path takes them: `{intent, state}`
    /// with only keys that have a value. An empty text and an unselected
    /// choice are left out; a toggle is always there. A button form is sent
    /// with intent `button` and the action of the button `pressing` in its
    /// state.
    pub fn submission(&self, values: &Values, pressing: Option<&str>) -> Submission {
        let mut state = Map::new();
        let mut gaps = Vec::new();
        let buttons = !self.buttons.is_empty();
        if buttons {
            match pressing.filter(|action| self.buttons.iter().any(|b| b.action == *action)) {
                Some(action) => {
                    state.insert(ACTION_KEY.into(), Value::String(action.to_owned()));
                }
                None => gaps.push(Gap::Missing(ACTION_KEY.into())),
            }
        }
        for (key, field) in &self.fields {
            if buttons && key == ACTION_KEY {
                continue;
            }
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

    /// What holds every button (or the submit) back before one is pressed:
    /// none when it is ready. The required values hold every button alike;
    /// the longest action stands in for the size.
    pub fn gaps(&self, values: &Values) -> Vec<Gap> {
        let probe = self.buttons.iter().max_by_key(|b| b.action.len()).map(|b| b.action.as_str());
        match self.submission(values, probe) {
            Submission::Ready(_) => Vec::new(),
            Submission::Blocked(gaps) => gaps,
        }
    }

    /// The button an accepted submission pressed, if this is a button form.
    pub fn chosen_button(&self, accepted: Option<&Accepted>) -> Option<&Button> {
        let accepted = accepted?;
        if self.buttons.is_empty() || accepted.intent != BUTTON_INTENT {
            return None;
        }
        let Some(Answer::Str(action)) = accepted.state.get(ACTION_KEY) else { return None };
        self.buttons.iter().find(|b| &b.action == action)
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
    let version = match (object.get("catalog").and_then(Value::as_str), object.get("catalog_version").and_then(as_int)) {
        (Some(CATALOG), Some(version)) if (1..=CATALOG_VERSION).contains(&version) => version,
        _ => return Reading::NeedsNewer,
    };
    match (Reader { payload: object, version }).form() {
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
    /// The catalog version the payload names.
    version: i64,
}

/// What the tree walk collects.
#[derive(Default)]
struct Walk {
    visited: BTreeSet<String>,
    bound: BTreeSet<String>,
    toggles: Vec<String>,
    buttons: Vec<Button>,
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

fn optional_text(value: Option<&Value>, pointer: &str, min: usize, max: usize) -> Result<Option<String>, Stop> {
    value.map(|v| text_in(Some(v), pointer, min, max)).transpose()
}

/// A `decimals`: 0 to 6, 0 when absent.
fn decimals(value: Option<&Value>, pointer: &str) -> Result<u32, Stop> {
    match value {
        None => Ok(0),
        Some(v) => match as_int(v) {
            Some(n) if (0..=6).contains(&n) => Ok(n as u32),
            _ => broken(pointer),
        },
    }
}

/// An object with only `allowed` keys.
fn closed_item<'v>(value: &'v Value, allowed: &[&str], pointer: &str) -> Result<&'v Map<String, Value>, Stop> {
    let Some(object) = value.as_object() else { return broken(pointer) };
    closed(object, allowed, pointer)?;
    Ok(object)
}

/// The chart's schema, then what it cannot say, in the Host's order: series
/// names unique, a pie's one series, the total, per series the bar and pie
/// counts, per point one x kind for the whole chart, x unique in its
/// series, numeric x rising, a pie's y not below zero.
fn chart(props: &Map<String, Value>, base: &str) -> Result<Chart, Stop> {
    let at = |name: &str| format!("{base}/{name}");
    let kind = match props.get("kind").and_then(Value::as_str) {
        Some("bar") => ChartKind::Bar,
        Some("line") => ChartKind::Line,
        Some("pie") => ChartKind::Pie,
        _ => return broken(at("kind")),
    };
    let title = optional_text(props.get("title"), &at("title"), 0, 120)?;
    let x_label = optional_text(props.get("x_label"), &at("x_label"), 0, 60)?;
    let y_label = optional_text(props.get("y_label"), &at("y_label"), 0, 60)?;
    let unit = optional_text(props.get("unit"), &at("unit"), 0, 12)?;
    let places = decimals(props.get("decimals"), &at("decimals"))?;
    let raw_series = match props.get("series").and_then(Value::as_array) {
        Some(items) if (1..=6).contains(&items.len()) => items,
        _ => return broken(at("series")),
    };
    let mut series = Vec::new();
    for (index, item) in raw_series.iter().enumerate() {
        let pointer = format!("{base}/series/{index}");
        let object = closed_item(item, &["name", "points"], &pointer)?;
        let name = text_in(object.get("name"), &format!("{pointer}/name"), 1, 60)?;
        let raw_points = match object.get("points").and_then(Value::as_array) {
            Some(items) if (1..=200).contains(&items.len()) => items,
            _ => return broken(format!("{pointer}/points")),
        };
        let mut points = Vec::new();
        for (offset, value) in raw_points.iter().enumerate() {
            let at = format!("{pointer}/points/{offset}");
            let point = closed_item(value, &["x", "y"], &at)?;
            let x = match point.get("x") {
                Some(Value::String(text)) if (1..=60).contains(&length(text)) => X::Category(text.clone()),
                Some(value @ Value::Number(_)) if as_int(value).is_some() => X::Number(as_int(value).unwrap_or_default()),
                _ => return broken(format!("{at}/x")),
            };
            let Some(y) = point.get("y").and_then(as_int) else { return broken(format!("{at}/y")) };
            points.push(Point { x, y });
        }
        series.push(Series { name, points });
    }
    let mut names = BTreeSet::new();
    for (index, one) in series.iter().enumerate() {
        if !names.insert(one.name.as_str()) {
            return broken(format!("{base}/series/{index}/name"));
        }
    }
    if kind == ChartKind::Pie && series.len() != 1 {
        return broken(at("series"));
    }
    if series.iter().map(|s| s.points.len()).sum::<usize>() > 600 {
        return broken(at("series"));
    }
    let mut strings: Option<bool> = None;
    for (index, one) in series.iter().enumerate() {
        let pointer = format!("{base}/series/{index}/points");
        if (kind == ChartKind::Bar && one.points.len() > 60) || (kind == ChartKind::Pie && one.points.len() > 12) {
            return broken(pointer);
        }
        let mut seen = BTreeSet::new();
        let mut last: Option<i64> = None;
        for (offset, point) in one.points.iter().enumerate() {
            let at = format!("{pointer}/{offset}");
            let is_string = matches!(point.x, X::Category(_));
            if strings.is_some_and(|s| s != is_string) || !seen.insert(&point.x) {
                return broken(format!("{at}/x"));
            }
            strings = Some(is_string);
            if let X::Number(number) = point.x {
                if last.is_some_and(|last| number <= last) {
                    return broken(format!("{at}/x"));
                }
                last = Some(number);
            }
            if kind == ChartKind::Pie && point.y < 0 {
                return broken(format!("{at}/y"));
            }
        }
    }
    Ok(Chart { kind, title, x_label, y_label, unit, decimals: places, series })
}

/// Column keys are identifiers and unique; at most 1000 cells; a row names
/// column keys only.
fn table(props: &Map<String, Value>, base: &str) -> Result<Table, Stop> {
    let title = optional_text(props.get("title"), &format!("{base}/title"), 0, 120)?;
    let raw_columns = match props.get("columns").and_then(Value::as_array) {
        Some(items) if (1..=8).contains(&items.len()) => items,
        _ => return broken(format!("{base}/columns")),
    };
    let mut columns: Vec<Column> = Vec::new();
    for (index, item) in raw_columns.iter().enumerate() {
        let pointer = format!("{base}/columns/{index}");
        let object = closed_item(item, &["key", "label", "align", "decimals"], &pointer)?;
        let key = match object.get("key").and_then(Value::as_str) {
            Some(key) if is_identifier(key) && !columns.iter().any(|c| c.key == key) => key.to_owned(),
            _ => return broken(format!("{pointer}/key")),
        };
        let label = text_in(object.get("label"), &format!("{pointer}/label"), 1, 60)?;
        let align = match object.get("align") {
            None => "start",
            Some(v) => v.as_str().unwrap_or_default(),
        };
        if align != "start" && align != "end" {
            return broken(format!("{pointer}/align"));
        }
        let decimals = decimals(object.get("decimals"), &format!("{pointer}/decimals"))?;
        columns.push(Column { key, label, align_end: align == "end", decimals });
    }
    let raw_rows = match props.get("rows").and_then(Value::as_array) {
        Some(items) if items.len() <= 200 && items.len() * columns.len() <= 1000 => items,
        _ => return broken(format!("{base}/rows")),
    };
    let keys: Vec<&str> = columns.iter().map(|c| c.key.as_str()).collect();
    let mut rows = Vec::new();
    for (index, item) in raw_rows.iter().enumerate() {
        let pointer = format!("{base}/rows/{index}");
        let row = closed_item(item, &keys, &pointer)?;
        let mut cells = Vec::new();
        for column in &columns {
            cells.push(match row.get(&column.key) {
                None | Some(Value::Null) => Cell::Empty,
                Some(Value::String(text)) if length(text) <= 200 => Cell::Text(text.clone()),
                Some(Value::Bool(flag)) => Cell::Flag(*flag),
                Some(value @ Value::Number(_)) if as_int(value).is_some() => Cell::Number(as_int(value).unwrap_or_default()),
                _ => return broken(format!("{pointer}/{}", column.key)),
            });
        }
        rows.push(cells);
    }
    Ok(Table { title, columns, rows })
}

fn button(props: &Map<String, Value>, base: &str) -> Result<Button, Stop> {
    let label = text_in(props.get("label"), &format!("{base}/label"), 1, 40)?;
    let action = match props.get("action").and_then(Value::as_str) {
        Some(action) if is_identifier(action) => action.to_owned(),
        _ => return broken(format!("{base}/action")),
    };
    let style = match props.get("style").map(|s| s.as_str().unwrap_or_default()) {
        None | Some("secondary") => ButtonStyle::Secondary,
        Some("primary") => ButtonStyle::Primary,
        Some("destructive") => ButtonStyle::Destructive,
        Some(_) => return broken(format!("{base}/style")),
    };
    let confirm = optional_text(props.get("confirm"), &format!("{base}/confirm"), 1, 200)?;
    Ok(Button { label, action, style, confirm })
}

impl Reader<'_> {
    fn form(&self) -> Result<Form, Stop> {
        let p = self.payload;
        let version = text_in(p.get("version"), "/version", 1, 80)?;
        let Some(spec_value) = p.get("spec") else { return broken("/spec") };
        let Some(spec) = spec_value.as_object() else { return broken("/spec") };
        closed(spec, &["root", "elements", "state"], "/spec")?;
        if canonical_len(spec_value) > MAX_SPEC_BYTES {
            return broken("/spec");
        }
        let (fields, required) = self.schema()?;

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
        if !walk.buttons.is_empty() {
            // The action is a required string whose enum is exactly the
            // buttons' actions; no element binds it.
            let at = format!("/answer_schema/properties/{ACTION_KEY}");
            let declared = p.get("answer_schema").and_then(|s| s.get("properties")).and_then(|s| s.get(ACTION_KEY)).and_then(|s| s.get("type"));
            let actions: BTreeSet<&str> = walk.buttons.iter().map(|b| b.action.as_str()).collect();
            let exact = matches!(fields.get(ACTION_KEY), Some(Field::Choice(enum_values))
                if enum_values.len() == actions.len() && enum_values.iter().all(|a| actions.contains(a.as_str())));
            if declared.and_then(Value::as_str) != Some("string") || !exact || !required.contains(ACTION_KEY) || walk.bound.contains(ACTION_KEY) {
                return broken(at);
            }
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
        // A form with buttons has no submit; without one it needs its submit.
        let (intent, label) = if walk.buttons.is_empty() {
            let Some(submit) = p.get("submit").and_then(Value::as_object) else { return broken("/submit") };
            closed(submit, &["intent", "label"], "/submit")?;
            let intent = match submit.get("intent").and_then(Value::as_str) {
                Some(intent) if is_identifier(intent) => intent.to_owned(),
                _ => return broken("/submit/intent"),
            };
            (intent, text_in(submit.get("label"), "/submit/label", 1, 40)?)
        } else {
            if p.contains_key("submit") {
                return broken("/submit");
            }
            (BUTTON_INTENT.to_owned(), String::new())
        };
        Ok(Form {
            version,
            catalog_version: self.version,
            root,
            submit_intent: intent,
            submit_label: label,
            buttons: walk.buttons,
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
        let Some(allowed) = component_props(kind, self.version) else { return Err(Stop::Newer) };
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
            "chart" => Kind::Chart(chart(props, &format!("{base}/props"))?),
            "table" => Kind::Table(table(props, &format!("{base}/props"))?),
            "button" => {
                let button = button(props, &format!("{base}/props"))?;
                if walk.buttons.iter().any(|b| b.action == button.action) {
                    return broken(prop("action"));
                }
                walk.buttons.push(button.clone());
                Kind::Button(button)
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
    fn a_form_whose_one_input_is_a_choice_answers_on_the_click() {
        let one = json!({
          "version": "preview-1", "catalog": "clarp.form", "catalog_version": 1,
          "spec": {"root": "root", "elements": {
            "root": {"type": "stack", "props": {}, "children": ["title", "pick"]},
            "title": {"type": "heading", "props": {"text": "Lunch", "level": 1}},
            "pick": {"type": "choice", "props": {"label": "Where", "value": {"$bindState": "/pick"}, "options": [
              {"value": "inn", "label": "The inn"}, {"value": "boat", "label": "The boat"}]}}}},
          "submit": {"intent": "lunch", "label": "Send"},
          "answer_schema": {"type": "object", "required": ["pick"], "properties": {"pick": {"type": "string", "enum": ["inn", "boat"]}}}
        });
        assert_eq!(form(&one).one_choice(), Some("pick"));
        assert_eq!(form(&release_check()).one_choice(), None, "a form with more inputs keeps its Send");
        assert!(form(&one).one_tap() && !form(&release_check()).one_tap());
    }

    #[test]
    fn the_header_is_the_strict_form_the_host_reads() {
        assert_eq!(client_header("0.1.0"), "desktop/0.1.0 contract=2");
        assert_eq!(client_header("1.0 beta/x"), "desktop/1.0betax contract=2");
        assert_eq!(client_header(""), "desktop/0 contract=2");
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
        let Submission::Ready(answers) = form.submission(&values, None) else { panic!("ready") };
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
        let Submission::Ready(answers) = form.submission(&values, None) else { panic!("ready") };
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
        payload["catalog_version"] = json!(3);
        assert_eq!(read(&payload), Reading::NeedsNewer);
        let mut payload = release_check();
        payload["spec"]["elements"]["rule"]["type"] = json!("gauge");
        assert_eq!(read(&payload), Reading::NeedsNewer);
        // Version 1 has no chart: the spec names a catalog without it.
        let mut payload = release_check();
        payload["spec"]["elements"]["rule"] = json!({"type": "chart", "props": {"kind": "bar", "series": [{"name": "a", "points": [{"x": "Mon", "y": 1}]}]}});
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

    // ---- version 2 (issue 673) ------------------------------------------

    fn deploy() -> Value {
        serde_json::from_str(include_str!("../../../tests/fixtures/render_spec/deploy-v2.json")).unwrap()
    }

    fn element<'f>(form: &'f Form, id: &str) -> &'f Element {
        fn find<'e>(element: &'e Element, id: &str) -> Option<&'e Element> {
            if element.id == id {
                return Some(element);
            }
            element.children.iter().find_map(|c| find(c, id))
        }
        find(&form.root, id).expect("element")
    }

    fn unreadable_at(payload: &Value) -> String {
        match read(payload) {
            Reading::Unreadable(pointer) => pointer,
            other => panic!("readable: {other:?}"),
        }
    }

    #[test]
    fn numbers_are_the_integer_over_ten_to_the_decimals_never_grouped() {
        assert_eq!(number_text(125, 1, ','), "12,5");
        assert_eq!(number_text(125, 2, ','), "1,25");
        assert_eq!(number_text(120, 1, ','), "12,0", "exactly `decimals` digits");
        assert_eq!(number_text(12_500, 0, ','), "12500", "no thousands grouping");
        assert_eq!(number_text(1_234_567, 2, '.'), "12345.67");
        assert_eq!(number_text(5, 3, '.'), "0.005");
        assert_eq!(number_text(-5, 2, ','), "-0,05");
        assert_eq!(number_text(-125, 1, ','), "-12,5");
        assert_eq!(number_text(0, 0, ','), "0");
        assert_eq!(number_text(7, 6, ','), "0,000007");
        assert_eq!(number_text(INTEGER_LIMIT, 6, '.'), "9007199254.740991", "exact at the contract's limit");
        assert_eq!(number_text(i64::MIN, 0, '.'), "-9223372036854775808");
    }

    #[test]
    fn the_separator_is_the_numeric_locales() {
        assert_eq!(decimal_separator("nb_NO.UTF-8"), ',');
        assert_eq!(decimal_separator("nn_NO"), ',');
        assert_eq!(decimal_separator("de_DE@euro"), ',');
        assert_eq!(decimal_separator("de_CH.UTF-8"), '.');
        assert_eq!(decimal_separator("en_US.UTF-8"), '.');
        assert_eq!(decimal_separator("en-GB"), '.');
        assert_eq!(decimal_separator("es_MX.UTF-8"), '.');
        assert_eq!(decimal_separator("es_ES.UTF-8"), ',');
        assert_eq!(decimal_separator("ja_JP.UTF-8"), '.');
        assert_eq!(decimal_separator("C"), '.');
        assert_eq!(decimal_separator("POSIX"), '.');
        assert_eq!(decimal_separator(""), '.');
    }

    #[test]
    fn the_hosts_v2_example_reads_with_chart_table_and_buttons() {
        let form = form(&deploy());
        assert_eq!(form.catalog_version, 2);
        assert_eq!(form.submit_intent, BUTTON_INTENT);
        assert!(form.submit_label.is_empty(), "a button form has no footer submit");
        assert_eq!(form.buttons.iter().map(|b| b.action.as_str()).collect::<Vec<_>>(), ["ship", "hold"]);
        let Kind::Chart(chart) = &element(&form, "lat").kind else { panic!("chart") };
        assert_eq!(chart.kind, ChartKind::Line);
        assert!(!chart.scaled_x(), "string x on a line are categories");
        assert!(chart.legend(), "two series have a legend");
        assert_eq!(chart.categories(), [X::Category("Mon".into()), X::Category("Tue".into())]);
        assert_eq!(chart.summary(','), "p95 latency. Line chart. before: Mon 120 ms, Tue 118 ms. after: Mon 80 ms, Tue 77 ms");
        let Kind::Table(table) = &element(&form, "cmp").kind else { panic!("table") };
        assert_eq!(table.rows, [vec![Cell::Text("one".into()), Cell::Flag(true)], vec![Cell::Text("two".into()), Cell::Flag(false)]]);
        assert!(table.columns[1].align_end && !table.columns[0].align_end);
        let Kind::Button(hold) = &element(&form, "hold").kind else { panic!("button") };
        assert_eq!(hold.style, ButtonStyle::Destructive);
        assert_eq!(hold.confirm.as_deref(), Some("Hold the release?"));
        let mut payload = deploy();
        payload["spec"]["elements"]["ship"]["props"].as_object_mut().unwrap().remove("style");
        let plain = self::form(&payload);
        let Kind::Button(ship) = &element(&plain, "ship").kind else { panic!("button") };
        assert_eq!(ship.style, ButtonStyle::Secondary, "secondary by default");
    }

    #[test]
    fn a_button_sends_its_action_with_intent_button_once_the_values_are_there() {
        let mut payload = deploy();
        payload["answer_schema"]["required"] = json!(["action", "note"]);
        let form = form(&payload);
        let mut values = form.initial.clone();
        assert_eq!(form.gaps(&values), vec![Gap::Missing("note".into())], "every button waits for the required values");
        assert_eq!(footnote(&form, &values, None, "", false, &BTreeMap::new(), ""), "One required answer is missing.");
        values.insert("note".into(), Answer::Str("Looks good".into()));
        assert!(form.gaps(&values).is_empty());
        assert_eq!(form.submission(&values, None), Submission::Blocked(vec![Gap::Missing(ACTION_KEY.into())]), "no button, no answer");
        assert_eq!(form.submission(&values, Some("drop")), Submission::Blocked(vec![Gap::Missing(ACTION_KEY.into())]), "not a button of the form");
        let Submission::Ready(answers) = form.submission(&values, Some("ship")) else { panic!("ready") };
        assert_eq!(answers, json!({"intent": "button", "state": {"action": "ship", "note": "Looks good"}}));
        let accepted = Accepted { submission_id: "s".into(), version: "deploy-7".into(), intent: "button".into(),
            state: Values::from([("action".to_owned(), Answer::Str("ship".into()))]), accepted_at: 0, delivery_status: "pending".into() };
        assert_eq!(form.chosen_button(Some(&accepted)).map(|b| b.label.as_str()), Some("Ship it"));
        assert_eq!(answered_word(form.chosen_button(Some(&accepted)).map(|b| b.label.as_str())), "Answered · Ship it");
        assert_eq!(answered_word(None), "Answered");
        assert_eq!(form.chosen_button(None), None);
    }

    #[test]
    fn a_button_form_keeps_the_v2_button_rules() {
        let mut payload = deploy();
        payload["submit"] = json!({"intent": "go", "label": "Go"});
        assert_eq!(unreadable_at(&payload), "/submit", "no submit beside a button");
        let mut payload = deploy();
        payload["answer_schema"]["properties"]["action"]["enum"] = json!(["ship"]);
        assert_eq!(unreadable_at(&payload), "/answer_schema/properties/action");
        let mut payload = deploy();
        payload["answer_schema"]["required"] = json!([]);
        assert_eq!(unreadable_at(&payload), "/answer_schema/properties/action", "the action is required");
        let mut payload = deploy();
        payload["spec"]["elements"]["hold"]["props"]["action"] = json!("ship");
        assert!(matches!(read(&payload), Reading::Unreadable(_)), "each button its own action");
        let mut payload = deploy();
        payload["spec"]["elements"]["note"]["props"]["value"] = json!({"$bindState": "/action"});
        assert!(matches!(read(&payload), Reading::Unreadable(_)), "no element binds the action");
        let mut payload = deploy();
        payload["spec"]["elements"]["ship"]["props"]["style"] = json!("loud");
        assert_eq!(unreadable_at(&payload), "/spec/elements/ship/props/style");
    }

    fn with_chart(chart: Value) -> Value {
        let mut payload = deploy();
        payload["spec"]["elements"]["lat"]["props"] = chart;
        payload
    }

    fn points(n: usize) -> Value {
        Value::Array((0..n).map(|i| json!({"x": format!("c{i}"), "y": i})).collect())
    }

    #[test]
    fn a_chart_keeps_the_catalogs_limits() {
        let at = "/spec/elements/lat/props";
        let ok = form(&with_chart(json!({"kind": "bar", "decimals": 1, "series": [{"name": "a", "points": points(60)}]})));
        let Kind::Chart(chart) = &element(&ok, "lat").kind else { panic!("chart") };
        assert_eq!(chart.decimals, 1);
        assert_eq!(chart.value_text(125, ','), "12,5");
        assert_eq!(unreadable_at(&with_chart(json!({"kind": "bar", "series": [{"name": "a", "points": points(61)}]}))), format!("{at}/series/0/points"), "60 per bar series");
        let four: Vec<Value> = (0..4).map(|i| json!({"name": format!("s{i}"), "points": points(151)})).collect();
        assert_eq!(unreadable_at(&with_chart(json!({"kind": "line", "series": four}))), format!("{at}/series"), "600 in all");
        assert_eq!(unreadable_at(&with_chart(json!({"kind": "line", "series": [{"name": "a", "points": points(201)}]}))), format!("{at}/series/0/points"));
        assert_eq!(unreadable_at(&with_chart(json!({"kind": "pie", "series": [{"name": "a", "points": points(2)}, {"name": "b", "points": points(2)}]}))), format!("{at}/series"), "a pie has one series");
        assert_eq!(unreadable_at(&with_chart(json!({"kind": "pie", "series": [{"name": "a", "points": points(13)}]}))), format!("{at}/series/0/points"));
        assert_eq!(unreadable_at(&with_chart(json!({"kind": "pie", "series": [{"name": "a", "points": [{"x": "a", "y": -1}]}]}))), format!("{at}/series/0/points/0/y"));
        assert_eq!(unreadable_at(&with_chart(json!({"kind": "line", "series": [{"name": "a", "points": [{"x": 1, "y": 1}]}, {"name": "b", "points": [{"x": "Mon", "y": 1}]}]}))), format!("{at}/series/1/points/0/x"), "mixed x");
        assert_eq!(unreadable_at(&with_chart(json!({"kind": "line", "series": [{"name": "a", "points": [{"x": 2, "y": 1}, {"x": 1, "y": 1}]}]}))), format!("{at}/series/0/points/1/x"), "numeric x rises");
        assert_eq!(unreadable_at(&with_chart(json!({"kind": "bar", "series": [{"name": "a", "points": [{"x": "a", "y": 1}, {"x": "a", "y": 2}]}]}))), format!("{at}/series/0/points/1/x"), "x unique in a series");
        assert_eq!(unreadable_at(&with_chart(json!({"kind": "bar", "series": [{"name": "a", "points": points(1)}, {"name": "a", "points": points(1)}]}))), format!("{at}/series/1/name"), "series names unique");
        assert_eq!(unreadable_at(&with_chart(json!({"kind": "bar", "decimals": 7, "series": [{"name": "a", "points": points(1)}]}))), format!("{at}/decimals"));
        assert_eq!(unreadable_at(&with_chart(json!({"kind": "bar", "series": [{"name": "a", "points": [{"x": "a", "y": 1.5}]}]}))), "/spec", "a fraction is not canonical JSON: the spec is refused whole");
        let line = form(&with_chart(json!({"kind": "line", "series": [{"name": "a", "points": [{"x": 1, "y": 1}, {"x": 5, "y": 2}]}]})));
        let Kind::Chart(chart) = &element(&line, "lat").kind else { panic!("chart") };
        assert!(chart.scaled_x() && !chart.legend(), "a numeric line draws x to scale; one series has no legend");
        let bar = form(&with_chart(json!({"kind": "bar", "series": [{"name": "a", "points": [{"x": 1, "y": 1}, {"x": 5, "y": 2}]}]})));
        let Kind::Chart(chart) = &element(&bar, "lat").kind else { panic!("chart") };
        assert!(!chart.scaled_x(), "a bar takes numeric x as categories");
    }

    #[test]
    fn a_table_draws_every_cell_and_keeps_its_size() {
        let mut payload = deploy();
        payload["spec"]["elements"]["cmp"]["props"] = json!({"columns": [
            {"key": "host", "label": "Host"}, {"key": "cpu", "label": "CPU", "align": "end", "decimals": 2}, {"key": "ok", "label": "OK"}],
            "rows": [{"host": "one", "cpu": 125, "ok": true}, {"host": null, "cpu": 12500}, {}]});
        let form = form(&payload);
        let Kind::Table(table) = &element(&form, "cmp").kind else { panic!("table") };
        assert_eq!(table.rows[1], [Cell::Empty, Cell::Number(12_500), Cell::Empty], "null and a missing key draw empty");
        assert_eq!(table.rows[2], [Cell::Empty, Cell::Empty, Cell::Empty]);
        assert_eq!(Table::words(&table.rows[0][1], &table.columns[1], ','), "1,25");
        assert_eq!(Table::words(&table.rows[1][1], &table.columns[1], ','), "125,00");
        assert_eq!(Table::words(&table.rows[0][2], &table.columns[2], ','), "Yes");
        let mut payload = deploy();
        let columns: Vec<Value> = (0..8).map(|i| json!({"key": format!("c{i}"), "label": "C"})).collect();
        payload["spec"]["elements"]["cmp"]["props"] = json!({"columns": columns, "rows": vec![json!({}); 126]});
        assert_eq!(unreadable_at(&payload), "/spec/elements/cmp/props/rows", "at most 1000 cells");
        let mut payload = deploy();
        payload["spec"]["elements"]["cmp"]["props"]["rows"] = json!([{"host": "one", "cpu": 1}]);
        assert_eq!(unreadable_at(&payload), "/spec/elements/cmp/props/rows/0/cpu", "a row names column keys only");
        let mut payload = deploy();
        payload["spec"]["elements"]["cmp"]["props"]["columns"][1]["key"] = json!("host");
        assert_eq!(unreadable_at(&payload), "/spec/elements/cmp/props/columns/1/key");
    }
}
