//! JSON as the Python Host reads the body of a request to `/artifacts`, and the one form a
//! render spec's bytes are counted and hashed in.
//!
//! The Python Host reads such a body with `json.loads(bytes, object_pairs_hook=JsonObject)`
//! (`server/server.py`, `server/lib/render_spec.py`). That reader is not JSON as it is
//! defined, and what a render spec is refused with depends on where they part: it finds the
//! encoding by the first bytes (UTF-8, UTF-16 and UTF-32, with and without a mark), lets half
//! of a pair of UTF-16 signs through, takes `NaN` and `Infinity`, reads a whole number of
//! any length up to 4,300 digits, and reads `-0` as the whole number 0. An object keeps
//! whether a key came twice (the last value counts, at the place of the first). A reader of
//! JSON as it is defined would refuse what the Python Host refuses with a code, or take what
//! it refuses, so the body of a render spec is read here.
//!
//! The canonical form is RFC 8785 for whole numbers only: keys in the order of their UTF-16
//! units, no space, signs beyond ASCII as they are (`render_spec.canonical`). What cannot be
//! written so is refused with a code and the pointer of the first value that cannot, in the
//! order the Python Host meets them.

use std::collections::HashMap;
use std::fmt::Write as _;

use serde_json::{Map, Number, Value};

use super::{Ident, Refusal, STRUCTURAL_KEYS};

/// The largest whole number a render spec may hold, and its negative.
pub const MAX_INTEGER: i128 = 9_007_199_254_740_991;
/// How deep a value may nest before the canonical form refuses it.
const DEEPEST: usize = 64;
/// How deep a body may nest before this reader refuses it, the outermost container the first
/// level. The Python Host refuses past the same 512 on every route since its PR 438 (R73),
/// counting the brackets of the bytes before it parses: `docs/differences.md`.
pub const DEEPEST_READ: usize = 512;
/// The most digits Python reads as one whole number (`sys.get_int_max_str_digits()`).
const MOST_DIGITS: usize = 4300;

/// A text as Python holds it: signs, among which half of a UTF-16 pair may stand alone.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub enum Text {
    /// A text Rust can hold.
    Valid(String),
    /// A text with at least one half of a pair standing alone: its code points.
    Lone(Vec<u32>),
}

impl Text {
    /// Its length in signs, as Python's `len` counts them.
    pub fn len(&self) -> usize {
        match self {
            Self::Valid(text) => text.chars().count(),
            Self::Lone(points) => points.len(),
        }
    }

    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }

    /// The text, when Rust can hold it.
    pub fn as_str(&self) -> Option<&str> {
        match self {
            Self::Valid(text) => Some(text),
            Self::Lone(_) => None,
        }
    }

    /// The text, with a sign that cannot stand where a half stands alone.
    pub fn lossy(&self) -> String {
        match self {
            Self::Valid(text) => text.clone(),
            Self::Lone(points) => points
                .iter()
                .map(|point| char::from_u32(*point).unwrap_or('\u{fffd}'))
                .collect(),
        }
    }

    fn of(points: Vec<u32>) -> Self {
        if points.iter().any(|point| (0xd800..0xe000).contains(point)) {
            Self::Lone(points)
        } else {
            Self::Valid(
                points
                    .iter()
                    .filter_map(|point| char::from_u32(*point))
                    .collect(),
            )
        }
    }

    /// Its UTF-16 units, by which keys are ordered: `None` for a text that cannot be
    /// written in UTF-16 (`str.encode("utf-16-be")` refuses half a pair).
    fn units(&self) -> Option<Vec<u16>> {
        self.as_str().map(|text| text.encode_utf16().collect())
    }
}

impl From<&str> for Text {
    fn from(text: &str) -> Self {
        Self::Valid(text.to_string())
    }
}

/// An object: each key once, at the place it came first, with the value it came with last.
#[derive(Debug, Clone, PartialEq, Default)]
pub struct Obj {
    pairs: Vec<(Text, J)>,
    /// Where each key is among the pairs.
    places: HashMap<Text, usize>,
    /// Whether a key came more than once (`JsonObject.duplicate`).
    pub duplicate: bool,
}

impl Obj {
    pub fn new() -> Self {
        Self::default()
    }

    /// Takes a key and its value as a reader of the text meets them.
    pub fn insert(&mut self, key: Text, value: J) {
        match self.places.get(&key) {
            Some(&at) => {
                self.pairs[at].1 = value;
                self.duplicate = true;
            }
            None => {
                self.places.insert(key.clone(), self.pairs.len());
                self.pairs.push((key, value));
            }
        }
    }

    pub fn get(&self, key: &str) -> Option<&J> {
        let at = *self.places.get(&Text::Valid(key.to_string()))?;
        self.pairs.get(at).map(|(_, value)| value)
    }

    /// The value under a key as the reader holds it.
    pub fn at(&self, key: &Text) -> Option<&J> {
        let at = *self.places.get(key)?;
        self.pairs.get(at).map(|(_, value)| value)
    }

    pub fn contains(&self, key: &str) -> bool {
        self.get(key).is_some()
    }

    pub fn len(&self) -> usize {
        self.pairs.len()
    }

    pub fn is_empty(&self) -> bool {
        self.pairs.is_empty()
    }

    pub fn iter(&self) -> impl Iterator<Item = (&Text, &J)> {
        self.pairs.iter().map(|(key, value)| (key, value))
    }

    pub fn keys(&self) -> impl Iterator<Item = &Text> {
        self.pairs.iter().map(|(key, _)| key)
    }
}

/// A value as Python's reader makes it.
#[derive(Debug, Clone, PartialEq)]
pub enum J {
    Null,
    Bool(bool),
    /// A whole number: beyond what 128 bits hold it is held at their end, where every rule
    /// refuses it as it refuses the number itself.
    Int(i128),
    Float(f64),
    Text(Text),
    Array(Vec<J>),
    Object(Obj),
}

impl J {
    pub fn object(&self) -> Option<&Obj> {
        match self {
            Self::Object(fields) => Some(fields),
            _ => None,
        }
    }

    pub fn text(&self) -> Option<&Text> {
        match self {
            Self::Text(text) => Some(text),
            _ => None,
        }
    }

    /// The text, when this is one Rust can hold.
    pub fn str(&self) -> Option<&str> {
        self.text().and_then(Text::as_str)
    }

    /// The field of an object, when this is one and has it.
    pub fn get(&self, key: &str) -> Option<&J> {
        self.object().and_then(|fields| fields.get(key))
    }

    /// Whether Python counts this as true.
    pub fn truthy(&self) -> bool {
        match self {
            Self::Null => false,
            Self::Bool(truth) => *truth,
            Self::Int(number) => *number != 0,
            Self::Float(number) => *number != 0.0,
            Self::Text(text) => !text.is_empty(),
            Self::Array(items) => !items.is_empty(),
            Self::Object(fields) => !fields.is_empty(),
        }
    }

    /// A value of the JSON this Host keeps. A half of a pair becomes the sign that stands
    /// for what cannot be shown, and a number no client holds is held at the end of 64 bits.
    pub fn to_value(&self) -> Value {
        match self {
            Self::Null => Value::Null,
            Self::Bool(truth) => Value::Bool(*truth),
            Self::Int(number) => {
                let held =
                    i64::try_from(*number).unwrap_or(if *number < 0 { i64::MIN } else { i64::MAX });
                Value::Number(Number::from(held))
            }
            Self::Float(number) => Number::from_f64(*number).map_or(Value::Null, Value::Number),
            Self::Text(text) => Value::String(text.lossy()),
            Self::Array(items) => Value::Array(items.iter().map(Self::to_value).collect()),
            Self::Object(fields) => Value::Object(
                fields
                    .iter()
                    .map(|(key, value)| (key.lossy(), value.to_value()))
                    .collect::<Map<String, Value>>(),
            ),
        }
    }

    /// A value of the JSON this Host keeps, as this reader would have read it.
    pub fn of(value: &Value) -> Self {
        match value {
            Value::Null => Self::Null,
            Value::Bool(truth) => Self::Bool(*truth),
            Value::Number(number) => match (number.as_i64(), number.as_u64()) {
                (Some(whole), _) => Self::Int(i128::from(whole)),
                (None, Some(whole)) => Self::Int(i128::from(whole)),
                _ => Self::Float(number.as_f64().unwrap_or(f64::NAN)),
            },
            Value::String(text) => Self::Text(Text::Valid(text.clone())),
            Value::Array(items) => Self::Array(items.iter().map(Self::of).collect()),
            Value::Object(fields) => {
                let mut object = Obj::new();
                for (key, value) in fields {
                    object.insert(Text::Valid(key.clone()), Self::of(value));
                }
                Self::Object(object)
            }
        }
    }
}

/// The code points of a body, decoded as Python's `json.loads` decodes bytes
/// (`json.detect_encoding`, then the codec with `surrogatepass`). `None`: it is not text.
fn decoded(bytes: &[u8]) -> Option<Vec<u32>> {
    let starts = |mark: &[u8]| bytes.starts_with(mark);
    if starts(&[0, 0, 0xfe, 0xff]) {
        return utf32(&bytes[4..], true);
    }
    if starts(&[0xff, 0xfe, 0, 0]) {
        return utf32(&bytes[4..], false);
    }
    if starts(&[0xfe, 0xff]) {
        return utf16(&bytes[2..], true);
    }
    if starts(&[0xff, 0xfe]) {
        return utf16(&bytes[2..], false);
    }
    if starts(&[0xef, 0xbb, 0xbf]) {
        return utf8(&bytes[3..]);
    }
    if bytes.len() >= 4 {
        if bytes[0] == 0 {
            return if bytes[1] != 0 {
                utf16(bytes, true)
            } else {
                utf32(bytes, true)
            };
        }
        if bytes[1] == 0 {
            return if bytes[2] != 0 || bytes[3] != 0 {
                utf16(bytes, false)
            } else {
                utf32(bytes, false)
            };
        }
    } else if bytes.len() == 2 {
        if bytes[0] == 0 {
            return utf16(bytes, true);
        }
        if bytes[1] == 0 {
            return utf16(bytes, false);
        }
    }
    utf8(bytes)
}

fn utf32(bytes: &[u8], big: bool) -> Option<Vec<u32>> {
    let (fours, rest) = bytes.as_chunks::<4>();
    if !rest.is_empty() {
        return None;
    }
    fours
        .iter()
        .map(|four| {
            let point = if big {
                u32::from_be_bytes(*four)
            } else {
                u32::from_le_bytes(*four)
            };
            (point <= 0x10_ffff).then_some(point)
        })
        .collect()
}

fn utf16(bytes: &[u8], big: bool) -> Option<Vec<u32>> {
    let (twos, rest) = bytes.as_chunks::<2>();
    if !rest.is_empty() {
        return None;
    }
    let units: Vec<u32> = twos
        .iter()
        .map(|two| {
            u32::from(if big {
                u16::from_be_bytes(*two)
            } else {
                u16::from_le_bytes(*two)
            })
        })
        .collect();
    let mut points = Vec::with_capacity(units.len());
    let mut at = 0;
    while at < units.len() {
        let unit = units[at];
        let low = units.get(at + 1).copied();
        match low {
            Some(low) if (0xd800..0xdc00).contains(&unit) && (0xdc00..0xe000).contains(&low) => {
                points.push(0x10000 + ((unit - 0xd800) << 10) + (low - 0xdc00));
                at += 2;
            }
            _ => {
                points.push(unit);
                at += 1;
            }
        }
    }
    Some(points)
}

/// UTF-8 as Python decodes it with `surrogatepass`: strictly, except that half of a pair
/// written as three bytes is let through.
fn utf8(bytes: &[u8]) -> Option<Vec<u32>> {
    let mut points = Vec::with_capacity(bytes.len());
    let mut at = 0;
    let follows = |byte: Option<&u8>| byte.copied().filter(|byte| byte & 0xc0 == 0x80);
    while at < bytes.len() {
        let first = bytes[at];
        let (point, width) = match first {
            0x00..=0x7f => (u32::from(first), 1),
            0xc2..=0xdf => {
                let second = follows(bytes.get(at + 1))?;
                ((u32::from(first & 0x1f) << 6) | u32::from(second & 0x3f), 2)
            }
            0xe0..=0xef => {
                let second = follows(bytes.get(at + 1))?;
                let third = follows(bytes.get(at + 2))?;
                // Not written longer than it must be; half a pair is let through.
                if first == 0xe0 && second < 0xa0 {
                    return None;
                }
                let point = (u32::from(first & 0x0f) << 12)
                    | (u32::from(second & 0x3f) << 6)
                    | u32::from(third & 0x3f);
                (point, 3)
            }
            0xf0..=0xf4 => {
                let second = follows(bytes.get(at + 1))?;
                let third = follows(bytes.get(at + 2))?;
                let fourth = follows(bytes.get(at + 3))?;
                if (first == 0xf0 && second < 0x90) || (first == 0xf4 && second > 0x8f) {
                    return None;
                }
                let point = (u32::from(first & 0x07) << 18)
                    | (u32::from(second & 0x3f) << 12)
                    | (u32::from(third & 0x3f) << 6)
                    | u32::from(fourth & 0x3f);
                (point, 4)
            }
            _ => return None,
        };
        points.push(point);
        at += width;
    }
    Some(points)
}

/// Reads a body as the Python Host reads one. `None`: Python's reader refuses it, and the
/// Host answers that a JSON object is required.
pub fn read(bytes: &[u8]) -> Option<J> {
    let points = decoded(bytes)?;
    let mut reader = Reader {
        text: &points,
        at: 0,
        depth: 0,
    };
    reader.blanks();
    let value = reader.value()?;
    reader.blanks();
    (reader.at == points.len()).then_some(value)
}

struct Reader<'a> {
    text: &'a [u32],
    at: usize,
    depth: usize,
}

impl Reader<'_> {
    fn peek(&self) -> Option<u32> {
        self.text.get(self.at).copied()
    }

    fn blanks(&mut self) {
        while matches!(self.peek(), Some(0x20 | 0x09 | 0x0a | 0x0d)) {
            self.at += 1;
        }
    }

    /// Whether the text goes on with this word here; taken when it does.
    fn word(&mut self, word: &str) -> bool {
        let points: Vec<u32> = word.chars().map(u32::from).collect();
        let found = self.text.get(self.at..self.at + points.len()) == Some(&points[..]);
        if found {
            self.at += points.len();
        }
        found
    }

    fn value(&mut self) -> Option<J> {
        let first = char::from_u32(self.peek()?)?;
        match first {
            '"' => {
                self.at += 1;
                self.string().map(J::Text)
            }
            '{' | '[' => {
                self.depth += 1;
                if self.depth > DEEPEST_READ {
                    return None;
                }
                self.at += 1;
                let value = if first == '{' {
                    self.object()
                } else {
                    self.array()
                };
                self.depth -= 1;
                value
            }
            'n' if self.word("null") => Some(J::Null),
            't' if self.word("true") => Some(J::Bool(true)),
            'f' if self.word("false") => Some(J::Bool(false)),
            'N' if self.word("NaN") => Some(J::Float(f64::NAN)),
            'I' if self.word("Infinity") => Some(J::Float(f64::INFINITY)),
            '-' if self.word("-Infinity") => Some(J::Float(f64::NEG_INFINITY)),
            _ => self.number(),
        }
    }

    fn digit(&self, at: usize) -> bool {
        matches!(self.text.get(at), Some(0x30..=0x39))
    }

    /// A number as Python's scanner reads one: a fraction or an exponent only where a digit
    /// follows, and otherwise what came before them.
    fn number(&mut self) -> Option<J> {
        let begin = self.at;
        let mut at = self.at;
        if self.text.get(at) == Some(&u32::from('-')) {
            at += 1;
        }
        match self.text.get(at) {
            Some(0x31..=0x39) => {
                at += 1;
                while self.digit(at) {
                    at += 1;
                }
            }
            Some(0x30) => at += 1,
            _ => return None,
        }
        let whole_end = at;
        let mut fraction = false;
        if self.text.get(at) == Some(&u32::from('.')) && self.digit(at + 1) {
            fraction = true;
            at += 2;
            while self.digit(at) {
                at += 1;
            }
        }
        if matches!(self.text.get(at), Some(0x65 | 0x45)) {
            let mark = at;
            at += 1;
            if matches!(self.text.get(at), Some(0x2b | 0x2d)) {
                at += 1;
            }
            let digits = at;
            while self.digit(at) {
                at += 1;
            }
            if at > digits {
                fraction = true;
            } else {
                at = mark;
            }
        }
        let written: String = self.text[begin..at]
            .iter()
            .filter_map(|point| char::from_u32(*point))
            .collect();
        self.at = at;
        if fraction {
            return written.parse::<f64>().ok().map(J::Float);
        }
        let digits = whole_end - begin - usize::from(written.starts_with('-'));
        if digits > MOST_DIGITS {
            return None;
        }
        // Held at the end of 128 bits beyond them: no rule tells such numbers apart.
        let held = written
            .parse::<i128>()
            .unwrap_or(if written.starts_with('-') {
                i128::MIN
            } else {
                i128::MAX
            });
        Some(J::Int(held))
    }

    fn hex(&mut self) -> Option<u32> {
        let four = self.text.get(self.at..self.at + 4)?;
        let mut value = 0;
        for point in four {
            value = (value << 4) | char::from_u32(*point)?.to_digit(16)?;
        }
        self.at += 4;
        Some(value)
    }

    /// The rest of a text, after its opening quote.
    fn string(&mut self) -> Option<Text> {
        let mut points = Vec::new();
        loop {
            let point = self.peek()?;
            self.at += 1;
            match point {
                0x22 => return Some(Text::of(points)),
                0x5c => {
                    let escaped = char::from_u32(self.peek()?)?;
                    self.at += 1;
                    let point = match escaped {
                        '"' => 0x22,
                        '\\' => 0x5c,
                        '/' => 0x2f,
                        'b' => 0x08,
                        'f' => 0x0c,
                        'n' => 0x0a,
                        'r' => 0x0d,
                        't' => 0x09,
                        'u' => self.escaped_unit()?,
                        _ => return None,
                    };
                    points.push(point);
                }
                point if point < 0x20 => return None,
                point => points.push(point),
            }
        }
    }

    /// `\uXXXX`, joined with a `\uXXXX` that follows when the two are a pair.
    fn escaped_unit(&mut self) -> Option<u32> {
        let unit = self.hex()?;
        // Python's scanner also asks that something follow the second escape: in a text
        // that is read whole, its closing quote always does.
        let next_is_escape = self.text.get(self.at) == Some(&0x5c)
            && self.text.get(self.at + 1) == Some(&u32::from('u'));
        if (0xd800..0xdc00).contains(&unit) && next_is_escape {
            let mark = self.at;
            self.at += 2;
            let low = self.hex()?;
            if (0xdc00..0xe000).contains(&low) {
                return Some(0x10000 + ((unit - 0xd800) << 10) + (low - 0xdc00));
            }
            self.at = mark;
        }
        Some(unit)
    }

    fn object(&mut self) -> Option<J> {
        let mut object = Obj::new();
        self.blanks();
        if self.peek() == Some(0x7d) {
            self.at += 1;
            return Some(J::Object(object));
        }
        loop {
            if self.peek() != Some(0x22) {
                return None;
            }
            self.at += 1;
            let key = self.string()?;
            self.blanks();
            if self.peek() != Some(0x3a) {
                return None;
            }
            self.at += 1;
            self.blanks();
            let value = self.value()?;
            object.insert(key, value);
            self.blanks();
            match self.peek()? {
                0x7d => {
                    self.at += 1;
                    return Some(J::Object(object));
                }
                0x2c => {
                    self.at += 1;
                    self.blanks();
                }
                _ => return None,
            }
        }
    }

    fn array(&mut self) -> Option<J> {
        let mut items = Vec::new();
        self.blanks();
        if self.peek() == Some(0x5d) {
            self.at += 1;
            return Some(J::Array(items));
        }
        loop {
            items.push(self.value()?);
            self.blanks();
            match self.peek()? {
                0x5d => {
                    self.at += 1;
                    return Some(J::Array(items));
                }
                0x2c => {
                    self.at += 1;
                    self.blanks();
                }
                _ => return None,
            }
        }
    }
}

/// Whether any text of a value, a key among them, holds half of a UTF-16 pair: what no store
/// and no client can hold (`request_texts.lone_surrogate`, the Python Host's R79).
pub fn holds_lone(value: &J) -> bool {
    let mut pending = vec![value];
    while let Some(item) = pending.pop() {
        match item {
            J::Text(Text::Lone(_)) => return true,
            J::Array(items) => pending.extend(items),
            J::Object(fields) => {
                for (key, value) in fields.iter() {
                    if matches!(key, Text::Lone(_)) {
                        return true;
                    }
                    pending.push(value);
                }
            }
            _ => {}
        }
    }
    false
}

/// A pointer one step further (`render_spec._path`): an index, a key the contract names, or
/// a key that passes the identifier rule. Any other key is never named: the pointer stays
/// where it was.
pub fn step(path: &str, key: &Text, ident: Option<&Ident>) -> String {
    let named = key.as_str().filter(|key| {
        STRUCTURAL_KEYS.contains(key) || ident.is_some_and(|ident| ident.matches(key))
    });
    match named {
        Some(key) => format!("{path}/{key}"),
        None => path.to_string(),
    }
}

/// A pointer one step further, to an index.
pub fn index(path: &str, at: usize) -> String {
    format!("{path}/{at}")
}

/// The keys of an object in the order of their UTF-16 units. A key that cannot be written in
/// UTF-16 is refused at the root (`render_spec._order`).
pub fn ordered(object: &Obj) -> Result<Vec<&Text>, Refusal> {
    sorted(object.keys())
}

/// Keys in the order of their UTF-16 units.
pub fn sorted<'a>(keys: impl Iterator<Item = &'a Text>) -> Result<Vec<&'a Text>, Refusal> {
    let mut keyed = Vec::new();
    for key in keys {
        let Some(units) = key.units() else {
            return Err(Refusal::new("invalid_json", ""));
        };
        keyed.push((units, key));
    }
    keyed.sort_by(|one, two| one.0.cmp(&two.0));
    Ok(keyed.into_iter().map(|(_, key)| key).collect())
}

/// The canonical bytes of a value (`render_spec.canonical`): refused, with the pointer that
/// starts at `path`, where a number is no whole number a client holds, a text holds half a
/// pair, an object has a key twice, or the value nests deeper than 64.
pub fn canonical(value: &J, path: &str, ident: Option<&Ident>) -> Result<String, Refusal> {
    let mut out = String::new();
    write(value, path, 0, ident, &mut out)?;
    Ok(out)
}

fn write(
    value: &J,
    path: &str,
    depth: usize,
    ident: Option<&Ident>,
    out: &mut String,
) -> Result<(), Refusal> {
    if depth > DEEPEST {
        return Err(Refusal::new("invalid_json", path));
    }
    match value {
        J::Null => out.push_str("null"),
        J::Bool(truth) => out.push_str(if *truth { "true" } else { "false" }),
        J::Int(number) => {
            if number.unsigned_abs() > MAX_INTEGER.unsigned_abs() {
                return Err(Refusal::new("number_form", path));
            }
            let _ = write!(out, "{number}");
        }
        J::Float(_) => return Err(Refusal::new("number_form", path)),
        J::Text(text) => match text.as_str() {
            Some(text) => quoted(text, out),
            None => return Err(Refusal::new("invalid_json", path)),
        },
        J::Array(items) => {
            out.push('[');
            for (at, item) in items.iter().enumerate() {
                if at > 0 {
                    out.push(',');
                }
                write(item, &index(path, at), depth + 1, ident, out)?;
            }
            out.push(']');
        }
        J::Object(fields) => {
            if fields.duplicate {
                return Err(Refusal::new("invalid_object", path));
            }
            out.push('{');
            for (at, key) in ordered(fields)?.into_iter().enumerate() {
                if at > 0 {
                    out.push(',');
                }
                // A key that is ordered is one Rust holds.
                quoted(key.as_str().unwrap_or_default(), out);
                out.push(':');
                if let Some(item) = fields.at(key) {
                    write(item, &step(path, key, ident), depth + 1, ident, out)?;
                }
            }
            out.push('}');
        }
    }
    Ok(())
}

/// A text between quotes as Python's `json.dumps(ensure_ascii=False)` writes it.
pub fn quoted(text: &str, out: &mut String) {
    out.push('"');
    for sign in text.chars() {
        match sign {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{8}' => out.push_str("\\b"),
            '\u{c}' => out.push_str("\\f"),
            sign if u32::from(sign) < 0x20 => {
                let _ = write!(out, "\\u{:04x}", u32::from(sign));
            }
            sign => out.push(sign),
        }
    }
    out.push('"');
}
