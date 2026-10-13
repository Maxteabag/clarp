//! Host JSON as the desktop reads it. The Host is Python: a string holding a
//! lone UTF-16 surrogate goes out of `json.dumps` as an unpaired surrogate
//! escape (backslash, `u`, `d800`), and serde_json rejects that, so one bad
//! artifact used to fail the whole list, chat or snapshot it rode in. Every
//! Host body is read through here.
//!
//! - Only an unpaired escape in the range D800-DFFF is rewritten, to the
//!   six-byte escape of U+FFFD. Every other byte, valid pairs, raw UTF-8 and
//!   escaped backslashes included, stays as it was, in keys and values alike.
//! - A body without one goes to serde_json as the very same bytes.
//! - Each object that holds a repaired string, directly or deeper down, gets
//!   [`REPAIRED_FIELD`]: how many repaired strings it holds. Nothing else
//!   gets it, so its absence means the text is the Host's own.
//! - A repaired string is never the Host's original: [`verify_content`]
//!   refuses to call it verified against a summary-v1 `c1:` hash and falls
//!   back to `(id, updated_at, version)`, as the Host's `hash_pending` asks.

use std::borrow::Cow;

use serde_json::Value;
use sha2::{Digest, Sha256};

use crate::json::Object;

/// Added to each object that holds a repaired string: the count beneath it.
pub const REPAIRED_FIELD: &str = "_desktop_repaired";

const REPLACEMENT: &[u8; 6] = b"\\ufffd";

/// The body with each unpaired surrogate escape replaced, and the byte offsets
/// of the backslashes it replaced. One pass; a body without any comes back
/// borrowed, so serde_json reads the caller's own bytes.
///
/// A backslash always starts an escape inside a JSON string (and is invalid
/// outside one), and no byte of a multi-byte UTF-8 sequence is a backslash, so
/// stepping over each escape keeps an escaped backslash followed by `ud800`
/// out of reach.
pub fn repair_lone_surrogates(bytes: &[u8]) -> (Cow<'_, [u8]>, Vec<usize>) {
    let mut lone = Vec::new();
    let mut i = 0;
    while let Some(offset) = bytes[i..].iter().position(|b| *b == b'\\') {
        let at = i + offset;
        i = match bytes.get(at + 1) {
            Some(b'u') => match hex_unit(bytes, at + 2) {
                Some(0xD800..=0xDBFF) if is_low_escape(bytes, at + 6) => at + 12,
                Some(0xD800..=0xDFFF) => {
                    lone.push(at);
                    at + 6
                }
                _ => at + 2,
            },
            Some(_) => at + 2,
            None => break,
        };
    }
    if lone.is_empty() {
        return (Cow::Borrowed(bytes), lone);
    }
    let mut repaired = bytes.to_vec();
    for at in &lone {
        repaired[*at..*at + 6].copy_from_slice(REPLACEMENT);
    }
    (Cow::Owned(repaired), lone)
}

fn hex_unit(bytes: &[u8], at: usize) -> Option<u16> {
    let digits = bytes.get(at..at + 4)?;
    digits.iter().try_fold(0u16, |unit, b| Some((unit << 4) | (*b as char).to_digit(16)? as u16))
}

fn is_low_escape(bytes: &[u8], at: usize) -> bool {
    bytes.get(at..at + 2) == Some(&b"\\u"[..]) && hex_unit(bytes, at + 2).is_some_and(|u| (0xDC00..=0xDFFF).contains(&u))
}

/// Parse a Host body after the repair. When something was repaired it logs
/// once for the body (the route and the count, never the content) and marks
/// the objects holding it with [`REPAIRED_FIELD`].
pub fn parse(bytes: &[u8], route: impl std::fmt::Display) -> serde_json::Result<Value> {
    let (bytes, lone) = repair_lone_surrogates(bytes);
    let mut value: Value = serde_json::from_slice(&bytes)?;
    if !lone.is_empty() {
        eprintln!("host json: replaced {} lone surrogate escape(s) in {route}", lone.len());
        for path in repaired_paths(&bytes, &lone) {
            mark(&mut value, &path);
        }
    }
    Ok(value)
}

#[derive(Debug, Clone)]
enum Segment {
    Key(String),
    Index(usize),
}

struct Frame {
    object: bool,
    expect_key: bool,
    key: Option<String>,
    index: usize,
}

impl Frame {
    fn segment(&self) -> Option<Segment> {
        if self.object { self.key.clone().map(Segment::Key) } else { Some(Segment::Index(self.index)) }
    }
}

/// For each repaired escape, the path to the innermost array or object whose
/// key or value holds it. Runs only on a body that parsed, so the structure is
/// well formed; strings are skipped escape by escape exactly as the repair
/// steps over them, so a quote, brace or comma inside a string is never
/// structure.
fn repaired_paths(bytes: &[u8], lone: &[usize]) -> Vec<Vec<Segment>> {
    let mut paths = Vec::new();
    let mut stack: Vec<Frame> = Vec::new();
    let mut lone = lone.iter().peekable();
    let mut i = 0;
    while i < bytes.len() {
        match bytes[i] {
            b'"' => {
                let start = i;
                let mut hits = 0;
                i += 1;
                while i < bytes.len() && bytes[i] != b'"' {
                    if bytes[i] == b'\\' {
                        if lone.next_if(|at| **at == i).is_some() {
                            hits += 1;
                        }
                        i += 2;
                    } else {
                        i += 1;
                    }
                }
                i += 1;
                if let Some(top) = stack.last_mut()
                    && top.object
                    && top.expect_key
                {
                    top.key = serde_json::from_slice::<String>(&bytes[start..i.min(bytes.len())]).ok();
                    top.expect_key = false;
                }
                if hits > 0 {
                    // A key that will not decode leaves only the root marked.
                    let path: Option<Vec<Segment>> = stack[..stack.len().saturating_sub(1)].iter().map(Frame::segment).collect();
                    paths.extend(std::iter::repeat_n(path.unwrap_or_default(), hits));
                }
                continue;
            }
            b'{' | b'[' => stack.push(Frame { object: bytes[i] == b'{', expect_key: bytes[i] == b'{', key: None, index: 0 }),
            b'}' | b']' => {
                stack.pop();
            }
            b',' => {
                if let Some(top) = stack.last_mut() {
                    if top.object {
                        top.expect_key = true;
                    } else {
                        top.index += 1;
                    }
                }
            }
            _ => {}
        }
        i += 1;
    }
    paths
}

/// Count one repaired string on every object from the root down the path.
fn mark(value: &mut Value, path: &[Segment]) {
    let mut node = value;
    for step in path.iter().map(Some).chain([None]) {
        if let Value::Object(object) = &mut *node {
            let count = object.get(REPAIRED_FIELD).and_then(Value::as_u64).unwrap_or(0);
            object.insert(REPAIRED_FIELD.into(), (count + 1).into());
        }
        let next = match step {
            None => return,
            Some(Segment::Key(key)) => node.get_mut(key.as_str()),
            Some(Segment::Index(index)) => node.get_mut(*index),
        };
        let Some(next) = next else { return };
        node = next;
    }
}

/// How many repaired strings an object holds, 0 when its text is the Host's.
pub fn repaired_strings(object: &Object) -> u64 {
    object.get(REPAIRED_FIELD).and_then(Value::as_u64).unwrap_or(0)
}

/// What a hydrated item's content is known to be, against the summary-v1
/// `content_omitted` marker that announced it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ContentIdentity {
    /// The content hashes to the marker's `c1:` digest.
    Verified(String),
    /// The content does not match the digest.
    Mismatch,
    /// Not checked: the Host gave no digest (`hash_pending`) or the desktop
    /// repaired the item. Identity is `(id, updated_at, version)` only.
    Unverified { id: String, updated_at: Option<i64>, version: Option<i64>, repaired: bool },
}

/// Check `content` from a hydrated `item` against its summary `marker`. A
/// repaired item is never verified, whatever its hash would say.
pub fn verify_content(item: &Object, content: &str, marker: &Object) -> ContentIdentity {
    let repaired = repaired_strings(item) > 0;
    let digest = marker.get("sha256").and_then(Value::as_str).filter(|d| d.starts_with("c1:"));
    let pending = marker.get("hash_pending").and_then(Value::as_bool).unwrap_or(false);
    match digest {
        Some(digest) if !repaired && !pending => {
            let ours: String = Sha256::digest(content.as_bytes()).iter().map(|b| format!("{b:02x}")).collect();
            if digest[3..] == ours { ContentIdentity::Verified(digest.to_owned()) } else { ContentIdentity::Mismatch }
        }
        _ => ContentIdentity::Unverified {
            id: item.get("id").and_then(Value::as_str).unwrap_or_default().to_owned(),
            updated_at: marker.get("updated_at").or_else(|| item.get("updated_at")).and_then(Value::as_i64),
            version: marker.get("version").or_else(|| item.get("version")).and_then(Value::as_i64),
            repaired,
        },
    }
}
