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
//! A name that the message spells out is taken off whole: all of its words ("Anna Maria"),
//! and its letters however they are composed ("Zoë"). (Until 16 Nov 2026 the Python Host
//! knew a word by the letters of English only and gave an agent called Zoë "ë go" for "Zoë
//! go": reference defect R23, fixed there.)
//!
//! One thing is done otherwise, on purpose: a word is taken off whole or not at all. Where
//! the message spells the name another way than the agent has it ("Rachél" for Rachel), the
//! word it was found in goes whole; the Python Host leaves the agent "él go". And a name is
//! not cut out of the middle of a longer word: of "Anna Mariana please" the agent Anna
//! Maria is given "Mariana please", where the Python Host gives it "na please".
//!
//! Held to the Python Host's own answers by `tests/fixtures/addressed-vectors.json`.

use unicode_normalization::UnicodeNormalization;

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

/// A word of a message: its letters, and where it begins and ends.
struct Word {
    letters: String,
    start: usize,
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
            start,
            end: at,
        });
    }
    found
}

/// A text in small letters with every letter taken apart into its base and its marks, so
/// that two spellings of one name are one.
fn canonical(text: &str) -> String {
    text.to_lowercase().nfd().collect()
}

/// A sign a word goes on with: a letter or a digit of any alphabet, or a mark on one.
fn of_a_word(sign: char) -> bool {
    sign.is_alphanumeric() || ('\u{300}'..='\u{36f}').contains(&sign)
}

/// Where the name ends in a message that spells it out from the word that was found: after
/// all of the name's words, however its letters are composed. Never before the word's own
/// end, and nowhere else when the message does not spell the name out, or goes on with
/// more of a word right after it.
fn through_the_name(text: &str, word: &Word, name: &str) -> usize {
    let wanted = canonical(name);
    let most = wanted.chars().count();
    let mut spelled = String::new();
    for (at, sign) in text[word.start..].char_indices().take(most) {
        spelled.push(sign);
        if canonical(&spelled) == wanted {
            let end = word.start + at + sign.len_utf8();
            let goes_on = text[end..].starts_with(of_a_word);
            return if goes_on { word.end } else { word.end.max(end) };
        }
    }
    word.end
}

/// What a message says after the name that ends at `end`: without the rest of the name's
/// word, and without the blanks and marks that follow an address.
fn after(text: &str, end: usize) -> String {
    // The rest of the word: letters the Python Host does not know as letters.
    let rest = text[end..].trim_start_matches(of_a_word);
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
                return Some((session, after(text, through_the_name(text, word, name))));
            }
        }
    }
    // A name much like the first word, when that is long enough to be one.
    if opening.letters.len() < 3 {
        return None;
    }
    let mut best: Option<(f64, &str, &'a str)> = None;
    for (name, session) in &candidates {
        let score = alike(&opening.letters, name);
        if score > best.map_or(0.0, |(score, _, _)| score) {
            best = Some((score, name, session));
        }
    }
    let (score, name, session) = best?;
    (score >= ALIKE_ENOUGH).then(|| (session, after(text, through_the_name(text, opening, name))))
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
