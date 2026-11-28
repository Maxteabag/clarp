//! The one line a list of chats shows of a message: what the message holds, never the Markdown
//! it was written in.
//!
//! A message is written for the chat, where its Markdown is drawn: an image as the picture, a
//! code block as code. The list has one line and draws nothing, so the line says what the
//! message holds instead, as a [`Part`]:
//!
//! - its words, with every mark of Markdown taken out (emphasis, headings, quotes, list marks,
//!   tables, inline code, a link's address: a link is its text);
//! - where the message is nothing but what the chat draws, what that is: an image (with its
//!   caption when it has one), several images, a file (by its name), a code block;
//! - a decision the user ended, by its question and how it ended; a tool, by what it did.
//!
//! The Host sends the part as a kind and its values ([`Part::json`]), and each app words it in
//! the user's language; the English line ([`Part::english`]) is the text an app that does not
//! know a kind shows. Held by `contract/fixtures/chat-preview.json`.

use std::sync::LazyLock;

use regex::{Captures, Regex};
use serde_json::{Value, json};

/// What the chat list's line of a message says.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Part {
    /// The message's words, plain and on one line (user content, never translated).
    Text(String),
    /// One image, and its caption ("" when it has none worth saying).
    Image(String),
    /// Several images.
    Images(usize),
    /// One file, by its name.
    File(String),
    /// Several files.
    Files(usize),
    /// Images and files together.
    Attachments(usize),
    /// Only code.
    Code,
    /// A decision or question the user ended: what it asked and how it ended.
    Decision {
        question: String,
        resolved: Resolved,
    },
    /// A tool the agent used: its kind of work (the categories of `clarp_kernel::tools`), its
    /// name, and what it worked on.
    Tool {
        category: String,
        name: String,
        label: String,
    },
}

impl Part {
    /// The kind and its values, as the apps word it: `{"kind": "text", "text"}`,
    /// `{"kind": "image", "caption"}`, `{"kind": "images", "count"}`, `{"kind": "file", "name"}`,
    /// `{"kind": "files", "count"}`, `{"kind": "attachments", "count"}`, `{"kind": "code"}`,
    /// `{"kind": "decision", "question", "outcome", "answer"}`,
    /// `{"kind": "tool", "category", "name", "label"}`.
    pub fn json(&self) -> Value {
        match self {
            Self::Text(text) => json!({"kind": "text", "text": text}),
            Self::Image(caption) => json!({"kind": "image", "caption": caption}),
            Self::Images(count) => json!({"kind": "images", "count": count}),
            Self::File(name) => json!({"kind": "file", "name": name}),
            Self::Files(count) => json!({"kind": "files", "count": count}),
            Self::Attachments(count) => json!({"kind": "attachments", "count": count}),
            Self::Code => json!({"kind": "code"}),
            Self::Decision { question, resolved } => json!({
                "kind": "decision",
                "question": question,
                "outcome": resolved.name(),
                "answer": match resolved {
                    Resolved::Answered(answer) => answer.as_str(),
                    _ => "",
                },
            }),
            Self::Tool {
                category,
                name,
                label,
            } => json!({"kind": "tool", "category": category, "name": name, "label": label}),
        }
    }

    /// The line in English, for an app that does not know the kind.
    pub fn english(&self) -> String {
        match self {
            Self::Text(text) => text.clone(),
            Self::Image(caption) if caption.is_empty() => "Image".into(),
            Self::Image(caption) => format!("Image: {caption}"),
            Self::Images(count) => format!("{count} images"),
            Self::File(name) => format!("File: {name}"),
            Self::Files(count) => format!("{count} files"),
            Self::Attachments(count) => format!("{count} attachments"),
            Self::Code => "Code".into(),
            Self::Decision { question, resolved } => {
                let ended = resolved.english();
                if question.is_empty() {
                    ended
                } else {
                    format!("{question} · {ended}")
                }
            }
            Self::Tool {
                category,
                name,
                label,
            } => {
                let verb = match category.as_str() {
                    "todo" => return "Updated the plan".into(),
                    "exec" => "Ran",
                    "read" => "Read",
                    "list" => "Listed",
                    "search" => "Searched",
                    "edit" => "Edited",
                    "write" => "Wrote",
                    "fetch" => "Fetched",
                    "mcp" => "Called",
                    "agent" => "Delegated",
                    _ => return format!("Used {name}").trim().to_string(),
                };
                format!("{verb} {label}").trim().to_string()
            }
        }
    }

    /// A tool the agent used.
    pub fn tool(category: &str, name: &str, label: &str) -> Self {
        Self::Tool {
            category: category.to_string(),
            name: one_line(name),
            label: one_line(label),
        }
    }

    /// The part with each of its words cut to `max` characters ([`cut`]): what the Host sends.
    #[must_use]
    pub fn cut(self, max: usize) -> Self {
        match self {
            Self::Text(text) => Self::Text(cut(&text, max)),
            Self::Image(caption) => Self::Image(cut(&caption, max)),
            Self::File(name) => Self::File(cut(&name, max)),
            Self::Decision { question, resolved } => Self::Decision {
                question: cut(&question, max),
                resolved: match resolved {
                    Resolved::Answered(answer) => Resolved::Answered(cut(&answer, max)),
                    other => other,
                },
            },
            Self::Tool {
                category,
                name,
                label,
            } => Self::Tool {
                category,
                name,
                label: cut(&label, max),
            },
            other => other,
        }
    }
}

/// A pattern that is part of the program. One that does not compile is a mistake in this file,
/// and every test would say so.
#[allow(clippy::expect_used)]
fn pattern(text: &str) -> Regex {
    Regex::new(text).expect("a pattern of this module compiles")
}

macro_rules! patterns {
    ($($name:ident = $text:expr;)*) => {
        $(static $name: LazyLock<Regex> = LazyLock::new(|| pattern($text));)*
    };
}

patterns! {
    // `![alt](address "title")`, `![alt][ref]`.
    IMAGE = r#"!\[([^\[\]]*)\](?:\(\s*<?([^()\s>]*(?:\([^()\s]*\)[^()\s>]*)*)>?(?:\s+"[^"]*"|\s+'[^']*')?\s*\)|\[[^\]]*\])"#;
    // `[text](address "title")`, `[text][ref]`.
    LINK = r#"\[([^\[\]]*)\](?:\(\s*<?([^()\s>]*(?:\([^()\s]*\)[^()\s>]*)*)>?(?:\s+"[^"]*"|\s+'[^']*')?\s*\)|\[[^\]]*\])"#;
    AUTOLINK = r"<((?:https?|mailto|clarp-[a-z]+):[^<>\s]+)>";
    REFERENCE = r"^\s{0,3}\[[^\]]+\]:\s+\S.*$";
    HEADING = r"^\s{0,3}#{1,6}(?:\s+|$)";
    HEADING_TAIL = r"\s+#+\s*$";
    QUOTE = r"^\s{0,3}>\s?";
    LIST_MARK = r"^\s*(?:[-*+•]|\d{1,9}[.)])\s+";
    TASK_BOX = r"^\[[ xX]\]\s+";
    RULE = r"^\s{0,3}(?:(?:-\s*){3,}|(?:\*\s*){3,}|(?:_\s*){3,}|(?:=\s*){3,})$";
    TABLE_DIVIDER = r"^\s*\|?\s*:?-{2,}:?\s*(?:\|\s*:?-{2,}:?\s*)*\|?\s*$";
    STRONG_STARS = r"\*\*(\S(?:.*?\S)?)\*\*";
    STRONG_UNDERSCORES = r"__(\S(?:.*?\S)?)__";
    STRIKE = r"~~(\S(?:.*?\S)?)~~";
    EMPHASIS_STAR = r"\*(\S(?:[^*]*?\S)?)\*";
    EMPHASIS_UNDERSCORE = r"(^|[^\p{L}\p{N}_])_(\S(?:[^_]*?\S)?)_($|[^\p{L}\p{N}_])";
    HTML_TAG = r"(?i)</?(?:br|p|div|span|details|summary|b|i|em|strong|u|s|del|ins|mark|sub|sup|kbd|small|code|pre|a|img|hr|center|font|h[1-6]|ul|ol|li|table|thead|tbody|tr|td|th|blockquote)\b[^>]*/?>";
    ESCAPED = r"\\([\\`*_{}\[\]()#+\-.!|~>])|[\x{E001}\x{E002}]";
    SPACES = r"\s+";
    SPACE_BEFORE_PUNCT = r"\s+([,.;:!?])";
}

/// Whether an address is a file this Host keeps.
fn is_kept_file(address: &str) -> bool {
    address.starts_with("clarp-media://") || address.starts_with("/media/")
}

/// A caption that says nothing an image's label does not.
fn says_nothing(caption: &str) -> bool {
    matches!(
        caption.trim().to_lowercase().as_str(),
        "" | "image" | "img" | "file" | "photo" | "picture" | "bilde" | "fil" | "foto"
    )
}

/// What a message holds that the chat draws and the line names.
#[derive(Default)]
struct Held {
    /// The captions of its images, in order.
    images: Vec<String>,
    /// The names of its files, in order.
    files: Vec<String>,
    /// How many code blocks.
    code: usize,
}

/// A text on one line, its runs of white space one space.
fn one_line(text: &str) -> String {
    let s = SPACES.replace_all(text.trim(), " ");
    SPACE_BEFORE_PUNCT.replace_all(&s, "$1").into_owned()
}

const SPAN_START: char = '\u{E001}';
const SPAN_END: char = '\u{E002}';

fn save_span(words: &str, out: &mut String, kept: &mut Vec<String>) {
    out.push(SPAN_START);
    out.push_str(&kept.len().to_string());
    out.push(SPAN_END);
    kept.push(words.to_owned());
}

/// Keep escaped punctuation and literal occurrences of the two markers outside the
/// Markdown passes, just like inline code.
fn protect_prose(text: &str, out: &mut String, kept: &mut Vec<String>) {
    let mut at = 0;
    for captures in ESCAPED.captures_iter(text) {
        let Some(found) = captures.get(0) else {
            continue;
        };
        out.push_str(&text[at..found.start()]);
        let literal = captures.get(1).map_or(found.as_str(), |mark| mark.as_str());
        save_span(literal, out, kept);
        at = found.end();
    }
    out.push_str(&text[at..]);
}

/// Render a piece of the protected line. Captions and file names can contain the same
/// literal spans as the surrounding words.
fn shown_inline(text: &str, kept: &[String]) -> String {
    let mut out = String::with_capacity(text.len());
    let mut rest = text;
    while let Some(open) = rest.find(SPAN_START) {
        let after = &rest[open + SPAN_START.len_utf8()..];
        let Some(close) = after.find(SPAN_END) else {
            break;
        };
        let end = open + SPAN_START.len_utf8() + close + SPAN_END.len_utf8();
        out.push_str(&rest[..open]);
        let saved = after[..close]
            .parse::<usize>()
            .ok()
            .and_then(|i| kept.get(i));
        out.push_str(saved.map_or(&rest[open..end], String::as_str));
        rest = &rest[end..];
    }
    out.push_str(rest);
    one_line(&out)
}

/// One line of prose, its marks taken out. Images and links to kept files are counted into
/// `held`; a line that held nothing else says nothing.
fn plain_line(line: &str, held: &mut Held) -> String {
    // Inline code is words as written: no mark inside it is read, a backslash in it included.
    // It is taken out of the line as written, before any escape is read, and put back last.
    let mut kept: Vec<String> = Vec::new();
    let mut s = protect_inline(line, &mut kept);
    if REFERENCE.is_match(&s) || RULE.is_match(&s) || TABLE_DIVIDER.is_match(&s) {
        return String::new();
    }
    // The marks that open a line, as often as they are nested.
    let heading = HEADING.is_match(&s);
    loop {
        let before = s.len();
        s = HEADING.replace(&s, "").into_owned();
        s = QUOTE.replace(&s, "").into_owned();
        s = LIST_MARK.replace(&s, "").into_owned();
        s = TASK_BOX.replace(&s, "").into_owned();
        if s.len() == before {
            break;
        }
    }
    if heading {
        s = HEADING_TAIL.replace(&s, "").into_owned();
    }
    // A table's row: its cells, apart.
    let trimmed = s.trim();
    if trimmed.starts_with('|') && trimmed.ends_with('|') && trimmed.len() > 1 {
        s = trimmed[1..trimmed.len() - 1]
            .split('|')
            .map(str::trim)
            .filter(|cell| !cell.is_empty())
            .collect::<Vec<_>>()
            .join(" · ");
    }
    s = IMAGE
        .replace_all(&s, |caps: &Captures| {
            held.images.push(shown_inline(&caps[1], &kept));
            String::new()
        })
        .into_owned();
    let mut words = 0usize;
    let mut linked_files = Vec::new();
    s = LINK
        .replace_all(&s, |caps: &Captures| {
            let text = caps[1].to_string();
            let address = caps.get(2).map_or("", |m| m.as_str());
            if is_kept_file(address) {
                let named = if text.trim().is_empty() {
                    address.rsplit('/').next().unwrap_or("").to_string()
                } else {
                    text.clone()
                };
                linked_files.push((text.clone(), named));
            }
            words += 1;
            text
        })
        .into_owned();
    s = AUTOLINK.replace_all(&s, "$1").into_owned();
    s = HTML_TAG.replace_all(&s, " ").into_owned();
    s = STRONG_STARS.replace_all(&s, "$1").into_owned();
    s = STRONG_UNDERSCORES.replace_all(&s, "$1").into_owned();
    s = STRIKE.replace_all(&s, "$1").into_owned();
    s = EMPHASIS_STAR.replace_all(&s, "$1").into_owned();
    s = EMPHASIS_UNDERSCORE.replace_all(&s, "$1$2$3").into_owned();
    // A line that is only links to kept files is those files.
    if !linked_files.is_empty() && words == linked_files.len() {
        let mut rest = s.clone();
        for (label, _) in &linked_files {
            rest = rest.replacen(label.as_str(), "", 1);
        }
        if rest
            .chars()
            .all(|c| c.is_whitespace() || ",;·|-–—".contains(c))
        {
            held.files.extend(
                linked_files
                    .into_iter()
                    .map(|(_, name)| shown_inline(&name, &kept)),
            );
            return String::new();
        }
    }
    shown_inline(&s, &kept)
}

/// Protect literal code spans and the surrounding prose's escapes. A code span ends at a
/// run of the opening width. An unmatched run stays literal, and an escaped backtick never
/// opens a span.
fn protect_inline(line: &str, kept: &mut Vec<String>) -> String {
    let run_at = |text: &str, at: usize| text[at..].bytes().take_while(|b| *b == b'`').count();
    let mut out = String::with_capacity(line.len());
    let mut at = 0;
    while let Some(found) = line[at..].find('`') {
        let open = at + found;
        let slashes = line[at..open]
            .bytes()
            .rev()
            .take_while(|b| *b == b'\\')
            .count();
        if slashes % 2 == 1 {
            protect_prose(&line[at..=open], &mut out, kept);
            at = open + 1;
            continue;
        }
        let width = run_at(line, open);
        let body = open + width;
        // The next run of exactly as many backticks closes it.
        let mut look = body;
        let mut close = None;
        while let Some(next) = line[look..].find('`') {
            let start = look + next;
            let run = run_at(line, start);
            if run == width {
                close = Some(start);
                break;
            }
            look = start + run;
        }
        protect_prose(&line[at..open], &mut out, kept);
        match close {
            Some(close) if close > body => {
                save_span(line[body..close].trim(), &mut out, kept);
                at = close + width;
            }
            _ => {
                out.push_str(&line[open..body]);
                at = body;
            }
        }
    }
    protect_prose(&line[at..], &mut out, kept);
    out
}

/// The opening of a fenced code block: its fence and its info word.
fn fence_of(line: &str) -> Option<(String, String)> {
    let trimmed = line.trim_start();
    let mark = trimmed.chars().next().filter(|c| *c == '`' || *c == '~')?;
    let run = trimmed.chars().take_while(|c| *c == mark).count();
    if run < 3 {
        return None;
    }
    let info = trimmed[run..].trim();
    if mark == '`' && info.contains('`') {
        return None;
    }
    let word = info.split_whitespace().next().unwrap_or("").to_lowercase();
    Some((mark.to_string().repeat(run), word))
}

/// What the chat list says of a message's text, or `None` when nothing in it is for the user
/// to read. Its words are plain, on one line and not cut.
pub fn read(text: &str) -> Option<Part> {
    if let Some((question, resolved)) = decision_receipt(text) {
        return Some(Part::Decision { question, resolved });
    }
    let mut held = Held::default();
    let mut prose: Vec<String> = Vec::new();
    let mut lines = text.lines();
    while let Some(line) = lines.next() {
        if let Some((fence, word)) = fence_of(line) {
            let mut body = Vec::new();
            for inner in lines.by_ref() {
                let closing = inner.trim();
                if closing.starts_with(fence.as_str())
                    && closing.chars().all(|c| fence.starts_with(c))
                {
                    break;
                }
                body.push(inner);
            }
            if matches!(word.as_str(), "clarp-gallery" | "gallery") {
                for inner in body {
                    plain_line(inner, &mut held);
                }
            } else {
                held.code += 1;
            }
            continue;
        }
        let said = plain_line(line, &mut held);
        if !said.is_empty() {
            prose.push(said);
        }
    }
    let words = one_line(&prose.join(" "));
    if !words.is_empty() {
        return Some(Part::Text(words));
    }
    let (images, files) = (held.images.len(), held.files.len());
    Some(match (images, files) {
        (0, 0) if held.code > 0 => Part::Code,
        (0, 0) => return None,
        (1, 0) => {
            let caption = &held.images[0];
            Part::Image(if says_nothing(caption) {
                String::new()
            } else {
                caption.clone()
            })
        }
        (count, 0) => Part::Images(count),
        (0, 1) => Part::File(held.files[0].clone()),
        (0, count) => Part::Files(count),
        (images, files) => Part::Attachments(images + files),
    })
}

/// How the user ended a decision or question an agent asked, as the Host tells the agent
/// (`clarp_kernel::artifacts::decisions::delivery_prompt`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Resolved {
    Approved,
    Declined,
    /// A choice's label or a typed answer.
    Answered(String),
    Discarded,
    Expired,
    /// An ending this Host does not word yet.
    Other,
}

impl Resolved {
    /// Its name, as the apps key on it.
    pub fn name(&self) -> &'static str {
        match self {
            Self::Approved => "approved",
            Self::Declined => "declined",
            Self::Answered(_) => "answered",
            Self::Discarded => "discarded",
            Self::Expired => "expired",
            Self::Other => "resolved",
        }
    }

    fn english(&self) -> String {
        match self {
            Self::Answered(answer) => answer.clone(),
            Self::Approved => "Approved".into(),
            Self::Declined => "Declined".into(),
            Self::Discarded => "Discarded".into(),
            Self::Expired => "Expired".into(),
            Self::Other => "Resolved".into(),
        }
    }
}

const RECEIPT_HEADER: &str = "[Clarp decision resolved]\nDecision ID: ";
const ANSWERED: &str = "answered this clarification: ";
const ANSWER_END: &str = ". Continue using this answer.";

/// The question and its ending in the message the Host tells an agent a decision of its
/// ended with, or `None` for any other text. Its header first, the decision and artifact
/// lines, header lines such as `Resolved by:`, the question, context and reference, the
/// one-line payload, then the sentence that says how it ended.
pub fn decision_receipt(text: &str) -> Option<(String, Resolved)> {
    let rest = text.strip_prefix(RECEIPT_HEADER)?;
    let (_, rest) = rest.split_once('\n')?;
    let (_, mut rest) = rest.strip_prefix("Artifact ID: ")?.split_once('\n')?;
    while !rest.starts_with("Question: ") {
        let (line, next) = rest.split_once('\n')?;
        let (key, _) = line.split_once(": ")?;
        if key.is_empty() || !key.chars().all(|c| c.is_ascii_alphabetic() || c == ' ') {
            return None;
        }
        rest = next;
    }
    let (question, rest) = rest.strip_prefix("Question: ")?.split_once("\nContext: ")?;
    let (_, rest) = rest.split_once("\nPayload: ")?;
    let (_, sentence) = rest.split_once('\n')?;
    Some((one_line(question), ending(sentence)))
}

/// How a decision ended, by the sentence that tells it, in the Host's words now and before.
fn ending(sentence: &str) -> Resolved {
    if sentence.starts_with("The user chose: accepted.") {
        return Resolved::Approved;
    }
    if sentence.starts_with("The user chose: rejected.") {
        return Resolved::Declined;
    }
    if sentence.starts_with("The user discarded this request.") {
        return Resolved::Discarded;
    }
    if sentence.starts_with("The request expired") {
        return Resolved::Expired;
    }
    let Some(after) = sentence.strip_prefix("The user") else {
        return Resolved::Other;
    };
    // "The user, through agent <session> approved …": the session is one word.
    let said = match after.strip_prefix(", through agent ") {
        Some(through) => through.split_once(' ').map_or("", |(_, said)| said),
        None => after.strip_prefix(' ').unwrap_or(""),
    };
    if said.starts_with("approved this exact action:") {
        Resolved::Approved
    } else if said.starts_with("rejected this exact action:") {
        Resolved::Declined
    } else if let Some(answer) = said.strip_prefix(ANSWERED) {
        let answer = answer
            .rsplit_once(ANSWER_END)
            .map_or(answer, |(json, _)| json);
        let words = serde_json::from_str::<serde_json::Value>(answer)
            .ok()
            .and_then(|value| {
                ["label", "text", "option_id"].iter().find_map(|key| {
                    value[*key]
                        .as_str()
                        .map(str::trim)
                        .filter(|said| !said.is_empty())
                        .map(str::to_string)
                })
            });
        words.map_or(Resolved::Other, Resolved::Answered)
    } else {
        Resolved::Other
    }
}

/// A line cut to `max` characters: at the end of a word when one ends near enough, the last
/// character an ellipsis when something was left out.
pub fn cut(text: &str, max: usize) -> String {
    if text.chars().count() <= max {
        return text.to_string();
    }
    let kept: String = text.chars().take(max.saturating_sub(1)).collect();
    // Back to the last space, when that loses no more than a fifth of the line.
    let at_word = kept
        .rfind(' ')
        .filter(|at| kept[..*at].chars().count() >= max * 4 / 5)
        .map_or(kept.as_str(), |at| &kept[..at]);
    let at_word = at_word.trim_end_matches(|c: char| c.is_whitespace() || ",;:-–—(".contains(c));
    format!("{at_word}…")
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use super::*;

    #[test]
    fn the_shared_vectors_hold() {
        let path = concat!(
            env!("CARGO_MANIFEST_DIR"),
            "/../../../contract/fixtures/chat-preview.json"
        );
        let text = std::fs::read_to_string(path).unwrap();
        let fixture: Value = serde_json::from_str(&text).unwrap();
        let mut failed = Vec::new();
        for case in fixture["cases"].as_array().unwrap() {
            let input = case["text"].as_str().unwrap();
            let part = read(input);
            let part = match case["max"].as_u64() {
                Some(max) => part.map(|part| part.cut(usize::try_from(max).unwrap())),
                None => part,
            };
            let line = part.as_ref().map(Part::english).unwrap_or_default();
            let made = part.as_ref().map_or(Value::Null, Part::json);
            if line != case["en"] || made != case["part"] {
                failed.push(format!(
                    "{}: want {} {}, made {line:?} {made}",
                    case["name"], case["en"], case["part"]
                ));
            }
        }
        assert!(failed.is_empty(), "{}", failed.join("\n"));
    }

    #[test]
    fn a_resolved_decision_is_its_question_and_ending() {
        let receipt = |sentence: &str| {
            format!(
                "[Clarp decision resolved]\nDecision ID: d-1\nArtifact ID: a-1\nResolved by: user\n\
                 Question: Ship build 42?\nContext: \nReference: \nPayload: {{}}\n{sentence}"
            )
        };
        let approved = read(&receipt(
            "The user approved this exact action: Ship build 42? Proceed.",
        ))
        .unwrap();
        assert_eq!(approved.english(), "Ship build 42? · Approved");
        assert_eq!(
            approved.json(),
            json!({"kind": "decision", "question": "Ship build 42?", "outcome": "approved", "answer": ""})
        );
        let answered = receipt(
            "The user, through agent nori answered this clarification: \
             {\"option_id\": \"b\", \"label\": \"Review first\"}. Continue using this answer.",
        );
        assert_eq!(
            decision_receipt(&answered).unwrap().1,
            Resolved::Answered("Review first".into())
        );
        let expired = receipt("The request expired without an answer or approval.");
        assert_eq!(decision_receipt(&expired).unwrap().1, Resolved::Expired);
        assert_eq!(decision_receipt("[Clarp decision resolved]"), None);
        assert_eq!(decision_receipt("Ship it"), None);
    }

    #[test]
    fn every_pattern_compiles_and_inline_code_pairs_its_backticks() {
        assert_eq!(read("plain words").unwrap().english(), "plain words");
        assert_eq!(
            read("a `x` b ``y ` z`` c ` d").unwrap().english(),
            "a x b y ` z c ` d"
        );
        assert_eq!(
            read("x \\`y `a\\*b` \\\\`c`").unwrap().english(),
            "x `y a\\*b \\c"
        );
        assert_eq!(
            read("**bold** and __strong__ and `**kept**`")
                .unwrap()
                .english(),
            "bold and strong and **kept**"
        );
    }

    #[test]
    fn a_tool_is_said_shortly() {
        let said = |category, name, label| Part::tool(category, name, label).english();
        assert_eq!(said("exec", "Bash", "cargo build"), "Ran cargo build");
        assert_eq!(said("read", "Read", "main.rs"), "Read main.rs");
        assert_eq!(said("todo", "TodoWrite", ""), "Updated the plan");
        assert_eq!(
            said("other", "image_generation", ""),
            "Used image_generation"
        );
        assert_eq!(
            Part::tool("exec", "Bash", "ls").json(),
            json!({"kind": "tool", "category": "exec", "name": "Bash", "label": "ls"})
        );
    }
}
