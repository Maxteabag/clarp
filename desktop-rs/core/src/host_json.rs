//! Host JSON as the desktop reads it. The Host is Python: a string holding a
//! lone UTF-16 surrogate goes out of `json.dumps` as an unpaired `\ud800`
//! escape, and serde_json rejects that, so one bad artifact used to fail the
//! whole list, chat or snapshot it rode in. Every Host body is read through
//! here: unpaired `\uD800`-`\uDFFF` escapes become `�` (U+FFFD) and every
//! other byte, valid pairs included, stays as it was.
//!
//! Content hashes: a summary-v1 `c1:` hash is taken over the Host's decoded
//! string, so a replaced string no longer matches it. The Host cannot hash a
//! lone surrogate either and sends `hash_pending` for such content; the repair
//! never touches that field and never makes a hash of its own, so a replaced
//! string is never claimed verified.

use std::borrow::Cow;

use serde_json::Value;

const REPLACEMENT: &[u8; 6] = b"\\ufffd";

/// The body with each unpaired surrogate escape replaced, and how many there
/// were. One pass over the bytes; a body without any comes back borrowed.
///
/// A backslash always starts an escape inside a JSON string (and is invalid
/// outside one), and no byte of a multi-byte UTF-8 sequence is a backslash, so
/// stepping over each escape keeps `\\ud800`, a literal backslash followed by
/// `ud800`, out of reach.
pub fn repair_lone_surrogates(bytes: &[u8]) -> (Cow<'_, [u8]>, usize) {
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
        return (Cow::Borrowed(bytes), 0);
    }
    let mut repaired = bytes.to_vec();
    for at in &lone {
        repaired[*at..*at + 6].copy_from_slice(REPLACEMENT);
    }
    (Cow::Owned(repaired), lone.len())
}

fn hex_unit(bytes: &[u8], at: usize) -> Option<u16> {
    let digits = bytes.get(at..at + 4)?;
    digits.iter().try_fold(0u16, |unit, b| Some((unit << 4) | (*b as char).to_digit(16)? as u16))
}

fn is_low_escape(bytes: &[u8], at: usize) -> bool {
    bytes.get(at..at + 2) == Some(&b"\\u"[..]) && hex_unit(bytes, at + 2).is_some_and(|u| (0xDC00..=0xDFFF).contains(&u))
}

/// Parse a Host body after the repair, logging once per body that needed it:
/// the route and the count, never the content.
pub fn parse(bytes: &[u8], route: impl std::fmt::Display) -> serde_json::Result<Value> {
    let (bytes, replaced) = repair_lone_surrogates(bytes);
    if replaced > 0 {
        eprintln!("host json: replaced {replaced} lone surrogate escape(s) in {route}");
    }
    serde_json::from_slice(&bytes)
}
