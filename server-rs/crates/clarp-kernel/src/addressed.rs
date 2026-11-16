//! Who a spoken message is for.
//!
//! A message that was dictated hands-free, and that does not say it is for the agent it
//! names, is for the agent whose name it begins with: within the first three words after
//! "hey", "ok" and the like, by the name as it is written or, for the first of those words
//! only, by a name that is much like it (a transcript writes "Bell" for "Bella"). The name
//! and what leads up to it are taken off the message. Without a name it stays with the agent
//! that is open, then with the one it names, then with the Host's default. As the Python
//! Host does with its router of spoken messages off, which is how it comes
//! (`server/lib/routing.py`, `server/lib/send_service.py`).
//!
//! One thing is done otherwise, on purpose: a name's word is taken off whole. The Python
//! Host knows a word by the letters of English, so for an agent called Zoë it gives the
//! agent "ë go" for "Zoë go" (reference defect R23).
//!
//! Held to the Python Host's own answers by `tests/fixtures/addressed-vectors.json`.

use crate::difflib;
use crate::python::{blank, strip as stripped};

/// What is said before a name and is no part of the address.
pub const FILLERS: [&str; 13] = [
    "also", "and", "hey", "hi", "ok", "okay", "please", "so", "uh", "um", "yeah", "yep", "yo",
];
/// How many words, after those, a name is looked for in.
pub const WORDS: usize = 3;
/// How alike the first word and a name must be to be taken for it.
const ALIKE_ENOUGH: f64 = 0.78;
/// The agent a message without any other is for: the Python Host's default of its setting
/// `default_session`.
pub const DEFAULT_SESSION: &str = "claude";

/// How alike two short words are, from 0 to 1: one that begins the other counts for much,
/// so that "Bell" is taken for "Bella" and an unrelated word is not.
pub fn alike(a: &str, b: &str) -> f64 {
    let (a, b) = (a.to_lowercase(), b.to_lowercase());
    if a.is_empty() || b.is_empty() {
        return 0.0;
    }
    if a == b {
        return 1.0;
    }
    if a.starts_with(&b) || b.starts_with(&a) {
        let (a, b) = (a.chars().count(), b.chars().count());
        return 0.85 + 0.1 * a.min(b) as f64 / a.max(b) as f64;
    }
    let signs = |text: &'_ str| -> Vec<(usize, usize)> {
        text.char_indices()
            .map(|(at, sign)| (at, at + sign.len_utf8()))
            .collect()
    };
    let (in_a, in_b) = (signs(&a), signs(&b));
    let of_a: Vec<&str> = in_a.iter().map(|&(from, to)| &a[from..to]).collect();
    let of_b: Vec<&str> = in_b.iter().map(|&(from, to)| &b[from..to]).collect();
    difflib::ratio(&of_a, &of_b)
}

/// The letters of English in a word, small.
fn letters(word: &str) -> String {
    word.chars()
        .filter(char::is_ascii_alphabetic)
        .map(|sign| sign.to_ascii_lowercase())
        .collect()
}

/// A word of a message: its letters, and where it ends.
struct Word {
    letters: String,
    end: usize,
}

/// The words of a message: each begins with a letter of English and goes on in letters,
/// digits, `_` and `-`.
fn words(text: &str) -> Vec<Word> {
    let bytes = text.as_bytes();
    let mut found = Vec::new();
    let mut at = 0;
    while at < bytes.len() {
        if !bytes[at].is_ascii_alphabetic() {
            at += 1;
            continue;
        }
        let start = at;
        while at < bytes.len()
            && (bytes[at].is_ascii_alphanumeric() || matches!(bytes[at], b'_' | b'-'))
        {
            at += 1;
        }
        found.push(Word {
            letters: letters(&text[start..at]),
            end: at,
        });
    }
    found
}

/// What a message says after the name that ends at `end`: without the rest of the name's
/// word, and without the blanks and marks that follow an address.
fn after(text: &str, end: usize) -> String {
    // The rest of the word: letters the Python Host does not know as letters.
    let whole = |sign: char| sign.is_alphanumeric() || ('\u{300}'..='\u{36f}').contains(&sign);
    let rest = text[end..].trim_start_matches(whole);
    let mark = |sign: char| blank(sign) || matches!(sign, ',' | ';' | ':' | '!' | '?' | '.' | '-');
    stripped(rest.trim_start_matches(mark)).to_string()
}

/// The agent a message names at its beginning, and what it says to it. `agents` are the
/// Host's, in the order they were made: session and name.
pub fn named<'a>(text: &str, agents: &[(&'a str, &str)]) -> Option<(&'a str, String)> {
    let words = words(text);
    // A longer name before a shorter one, so that "Bella" is found before "Bel".
    let mut candidates: Vec<(&str, &'a str)> = agents
        .iter()
        .map(|&(session, name)| (stripped(name), session))
        .filter(|(name, _)| !name.is_empty())
        .collect();
    candidates.sort_by_key(|(name, _)| std::cmp::Reverse(name.chars().count()));
    let first = words
        .iter()
        .take_while(|word| FILLERS.contains(&word.letters.as_str()))
        .count();
    let address = &words[first..words.len().min(first + WORDS)];
    let opening = address.first()?;
    // The name as it is written, in any of the words.
    for word in address {
        for (name, session) in &candidates {
            if word.letters == letters(name) {
                return Some((session, after(text, word.end)));
            }
        }
    }
    // A name much like the first word, when that is long enough to be one.
    if opening.letters.len() < 3 {
        return None;
    }
    let mut best: Option<(f64, &'a str)> = None;
    for (name, session) in &candidates {
        let score = alike(&opening.letters, name);
        if score > best.map_or(0.0, |(score, _)| score) {
            best = Some((score, session));
        }
    }
    let (score, session) = best?;
    (score >= ALIKE_ENOUGH).then(|| (session, after(text, opening.end)))
}

/// Where a message goes, and what of it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Target {
    pub session: String,
    pub text: String,
    /// Whether the message named its agent: that agent is then the open one.
    pub by_name: bool,
}

/// Where a message goes that does not say it is for the agent it names (`requested`):
/// to the agent whose name it begins with; else to the one that is open (`open`), then the
/// one it names, then the Host's default; and to the default when that is no agent.
pub fn target(
    text: &str,
    (requested, open, default): (&str, &str, &str),
    agents: &[(&str, &str)],
) -> Target {
    let is_agent = |session: &str| agents.iter().any(|(known, _)| *known == session);
    let (session, text, by_name) = match named(text, agents) {
        Some((session, rest)) if rest.is_empty() => (session.to_string(), text.to_string(), true),
        Some((session, rest)) => (session.to_string(), rest, true),
        None => {
            let first = [open, requested, default]
                .into_iter()
                .find(|session| !session.is_empty());
            let session = match stripped(first.unwrap_or("")) {
                "" => default,
                session => session,
            };
            (session.to_string(), text.to_string(), false)
        }
    };
    if is_agent(&session) {
        Target {
            session,
            text,
            by_name,
        }
    } else {
        Target {
            session: default.to_string(),
            text,
            by_name: false,
        }
    }
}
