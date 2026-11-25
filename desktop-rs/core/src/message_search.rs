//! Message search over what this client holds: the loaded chats and the
//! on-disk transcript cache, for a Host without message search. One index
//! per chat, updated row by row as rows arrive; a row is folded to lower
//! case once, when it is new or changed. Results rank by how well they
//! match, then by how recent they are.
//!
//! A Host that lists `FEATURE` searches every chat itself (`GET /search`,
//! docs/protocol.md): `HostPage` reads one page of its answer.

use std::collections::HashMap;

use crate::protocol::Message;

/// Where a chat's rows came from: loaded in this run, or only the cache.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Source {
    Memory,
    Cache,
}

#[derive(Debug, Clone)]
struct Entry {
    id: String,
    role: String,
    timestamp: String,
    epoch_ms: i64,
    revision: i64,
    /// The searched text, whitespace collapsed to single spaces.
    text: String,
    /// `text` folded to lower case one character for one, so character
    /// places agree between the two.
    folded: String,
}

#[derive(Debug, Default)]
struct Chat {
    source: Option<Source>,
    entries: Vec<Entry>,
}

#[derive(Debug, Default)]
pub struct SearchIndex {
    chats: HashMap<String, Chat>,
}

/// A piece of a snippet; `mark` is a match.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Segment {
    pub text: String,
    pub mark: bool,
}

#[derive(Debug, Clone)]
pub struct Hit {
    pub session: String,
    pub id: String,
    pub role: String,
    pub timestamp: String,
    pub epoch_ms: i64,
    pub source: Source,
    pub score: f64,
    pub snippet: Vec<Segment>,
}

/// Lower case, one character for one (the first of a longer folding).
fn fold(text: &str) -> String {
    text.chars().map(|c| c.to_lowercase().next().unwrap_or(c)).collect()
}

fn collapse(text: &str) -> String {
    text.split_whitespace().collect::<Vec<_>>().join(" ")
}

fn searchable(message: &Message) -> Option<&str> {
    if message.activity || message.kind == "live" || !matches!(message.role.as_str(), "user" | "assistant") {
        return None;
    }
    let text = if message.display_text.is_empty() { &message.text } else { &message.display_text };
    (!text.trim().is_empty()).then_some(text.as_str())
}

fn at_word_start(folded: &str, byte: usize) -> bool {
    folded[..byte].chars().next_back().is_none_or(|c| !c.is_alphanumeric())
}

/// Every place `needle` occurs in `haystack` (bytes), and whether any is at
/// a word start.
fn occurrences(haystack: &str, needle: &str) -> (Option<usize>, bool) {
    let mut first = None;
    let mut word = false;
    let mut from = 0;
    while let Some(found) = haystack[from..].find(needle) {
        let at = from + found;
        first.get_or_insert(at);
        if at_word_start(haystack, at) {
            word = true;
            break;
        }
        from = at + needle.len().max(1);
    }
    (first, word)
}

/// How well `folded` matches: the whole phrase at a word start (3), the
/// phrase anywhere (2), every term at a word start (1.5), every term (1);
/// None when a term is missing.
fn quality(folded: &str, phrase: &str, terms: &[String]) -> Option<f64> {
    let mut all_words = true;
    for term in terms {
        let (first, word) = occurrences(folded, term);
        first?;
        all_words &= word;
    }
    if terms.len() > 1 || phrase != terms[0] {
        let (first, word) = occurrences(folded, phrase);
        if first.is_some() {
            return Some(if word { 3.0 } else { 2.0 });
        }
        return Some(if all_words { 1.5 } else { 1.0 });
    }
    Some(if all_words { 3.0 } else { 2.0 })
}

/// Newer is better: 0.4 now, half that a week ago, falling towards 0; less
/// than the step between two match qualities, so recency orders rows that
/// match alike.
fn recency(epoch_ms: i64, now_ms: i64) -> f64 {
    let days = ((now_ms - epoch_ms).max(0) as f64) / 86_400_000.0;
    0.4 / (1.0 + days / 7.0)
}

impl SearchIndex {
    /// Indexes a chat's rows as they are now; rows it no longer holds are
    /// dropped. A loaded chat is never replaced by its cached copy. Returns
    /// how many rows were (re)folded.
    pub fn update_chat(&mut self, session: &str, rows: &[Message], source: Source) -> usize {
        let chat = self.chats.entry(session.to_owned()).or_default();
        if source == Source::Cache && chat.source == Some(Source::Memory) {
            return 0;
        }
        let mut old: HashMap<String, Entry> = std::mem::take(&mut chat.entries).into_iter().map(|e| (e.id.clone(), e)).collect();
        let mut folded = 0;
        chat.source = Some(source);
        for message in rows {
            let Some(text) = searchable(message) else { continue };
            match old.remove(&message.id) {
                Some(entry) if entry.revision == message.revision && entry.timestamp == message.timestamp => chat.entries.push(entry),
                _ => {
                    let text = collapse(text);
                    folded += 1;
                    chat.entries.push(Entry {
                        id: message.id.clone(),
                        role: message.role.clone(),
                        timestamp: message.timestamp.clone(),
                        epoch_ms: crate::time_format::epoch_ms(&message.timestamp).unwrap_or(0),
                        revision: message.revision,
                        folded: fold(&text),
                        text,
                    });
                }
            }
        }
        folded
    }

    pub fn remove_chat(&mut self, session: &str) {
        self.chats.remove(session);
    }

    pub fn source(&self, session: &str) -> Option<Source> {
        self.chats.get(session).and_then(|c| c.source)
    }

    /// How many chats are indexed from `source`.
    pub fn chats(&self, source: Source) -> usize {
        self.chats.values().filter(|c| c.source == Some(source)).count()
    }

    /// The best `limit` rows holding every term of `query`.
    pub fn search(&self, query: &str, limit: usize, now_ms: i64) -> Vec<Hit> {
        let phrase = fold(&collapse(query));
        let terms: Vec<String> = phrase.split(' ').filter(|t| !t.is_empty()).map(str::to_owned).collect();
        if terms.is_empty() || limit == 0 {
            return Vec::new();
        }
        let mut found: Vec<(f64, &str, &Entry, Source)> = Vec::new();
        for (session, chat) in &self.chats {
            let source = chat.source.unwrap_or(Source::Cache);
            for entry in &chat.entries {
                if let Some(quality) = quality(&entry.folded, &phrase, &terms) {
                    found.push((quality + recency(entry.epoch_ms, now_ms), session, entry, source));
                }
            }
        }
        found.sort_by(|a, b| b.0.total_cmp(&a.0).then(b.2.epoch_ms.cmp(&a.2.epoch_ms)).then(a.1.cmp(b.1)).then(a.2.id.cmp(&b.2.id)));
        found.truncate(limit);
        found
            .into_iter()
            .map(|(score, session, entry, source)| Hit {
                session: session.to_owned(),
                id: entry.id.clone(),
                role: entry.role.clone(),
                timestamp: entry.timestamp.clone(),
                epoch_ms: entry.epoch_ms,
                source,
                score,
                snippet: snippet(&entry.text, &terms, 140),
            })
            .collect()
    }
}

/// About `width` characters of `text` on one line around its first match,
/// with every occurrence of `terms` (lower case) marked and "…" where it
/// was cut.
pub fn snippet(text: &str, terms: &[String], width: usize) -> Vec<Segment> {
    let text = collapse(text);
    let chars: Vec<char> = text.chars().collect();
    let folded: Vec<char> = chars.iter().map(|c| c.to_lowercase().next().unwrap_or(*c)).collect();
    let terms: Vec<Vec<char>> = terms.iter().filter(|t| !t.is_empty()).map(|t| t.chars().collect()).collect();
    let mut marks = vec![false; chars.len()];
    let mut first = None;
    let mut at = 0;
    while at < folded.len() {
        let hit = terms.iter().filter(|t| folded[at..].starts_with(t)).map(Vec::len).max();
        match hit {
            Some(len) => {
                first.get_or_insert(at);
                marks[at..at + len].iter_mut().for_each(|m| *m = true);
                at += len;
            }
            None => at += 1,
        }
    }
    // A third of the width before the match, cut at a space when one is near.
    let mut start = first.unwrap_or(0).saturating_sub(width / 3);
    if start > 0 && let Some(space) = (start..first.unwrap_or(start)).find(|&i| chars[i] == ' ') {
        start = space + 1;
    }
    let mut end = (start + width).min(chars.len());
    if end < chars.len() && let Some(space) = (start..end).rev().find(|&i| chars[i] == ' ').filter(|&i| i > start + width * 2 / 3) {
        end = space;
    }
    let mut segments: Vec<Segment> = Vec::new();
    if start > 0 {
        push(&mut segments, "…", false);
    }
    for (c, mark) in chars[start..end].iter().zip(&marks[start..end]) {
        push(&mut segments, c.encode_utf8(&mut [0; 4]), *mark);
    }
    if end < chars.len() {
        push(&mut segments, "…", false);
    }
    segments
}

/// Adds `text` to the last segment when it is marked alike, else a new one.
fn push(segments: &mut Vec<Segment>, text: &str, mark: bool) {
    match segments.last_mut() {
        Some(last) if last.mark == mark => last.text.push_str(text),
        _ => segments.push(Segment { text: text.to_owned(), mark }),
    }
}

// ---- the Host's message search (`GET /search`) --------------------------

/// The feature a Host lists in `/server-info` when it answers `GET /search`.
pub const FEATURE: &str = "message_search";

/// One row the Host found.
#[derive(Debug, Clone, PartialEq)]
pub struct HostHit {
    pub session: String,
    pub agent_id: String,
    /// The agent's name as the Host has it.
    pub name: String,
    /// The row's id in `GET /log` of `session`.
    pub message_id: String,
    pub role: String,
    pub timestamp: String,
    pub epoch_ms: i64,
    pub revision: i64,
    /// The matching stretch, each matched word its own marked piece.
    pub snippet: Vec<Segment>,
}

/// One page of `GET /search`.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct HostPage {
    pub hits: Vec<HostHit>,
    /// The cursor of the next page; None on the last.
    pub next_before: Option<String>,
    /// The Host is still reading older history into its index.
    pub partial: bool,
}

impl HostPage {
    pub fn from_json(object: &crate::json::Object) -> Self {
        use crate::json::{array, boolean, integer, string};
        let hits = array(object, "hits")
            .iter()
            .filter_map(serde_json::Value::as_object)
            .filter_map(|hit| {
                let (session, message_id) = (string(hit, "session"), string(hit, "message_id"));
                if session.is_empty() || message_id.is_empty() {
                    return None;
                }
                let timestamp = string(hit, "timestamp");
                // The pieces as the Host cut them; the plain snippet when it
                // sent none.
                let mut snippet: Vec<Segment> = Vec::new();
                for part in array(hit, "parts").iter().filter_map(serde_json::Value::as_object) {
                    let text = string(part, "text");
                    if !text.is_empty() {
                        push(&mut snippet, &text, boolean(part, "match"));
                    }
                }
                if snippet.is_empty() {
                    let text = collapse(&string(hit, "snippet"));
                    if !text.is_empty() {
                        snippet.push(Segment { text, mark: false });
                    }
                }
                Some(HostHit {
                    agent_id: string(hit, "agent_id"),
                    name: string(hit, "name"),
                    role: string(hit, "role"),
                    epoch_ms: crate::time_format::epoch_ms(&timestamp).unwrap_or(0),
                    revision: integer(hit, "revision"),
                    timestamp,
                    session,
                    message_id,
                    snippet,
                })
            })
            .collect();
        let next_before = object.get("next_before").and_then(serde_json::Value::as_str).filter(|s| !s.is_empty()).map(str::to_owned);
        Self { hits, next_before, partial: boolean(object, "partial") }
    }
}

#[cfg(test)]
mod host_tests {
    use super::*;

    #[test]
    fn a_host_page_reads_its_hits_cursor_and_partial() {
        let page = serde_json::json!({
            "q": "oslo",
            "hits": [
                {"session": "ada-1234", "agent_id": "a1", "name": "Ada", "message_id": "m7", "role": "user",
                 "timestamp": "2026-11-25T10:02:03.000Z", "revision": 52428800, "snippet": "…Shall we meet in Oslo on Friday?",
                 "parts": [{"text": "…Shall we meet in ", "match": false}, {"text": "Oslo", "match": true}, {"text": " on Friday?", "match": false}]},
                {"session": "", "message_id": "x"},
                {"session": "bo", "message_id": "m1", "role": "assistant", "snippet": "Oslo  again"}
            ],
            "next_before": "1807402480000000",
            "partial": true
        });
        let page = HostPage::from_json(page.as_object().unwrap());
        assert_eq!(page.hits.len(), 2, "a hit without a session is left out");
        let hit = &page.hits[0];
        assert_eq!((hit.session.as_str(), hit.message_id.as_str(), hit.name.as_str(), hit.role.as_str()), ("ada-1234", "m7", "Ada", "user"));
        assert_eq!(hit.epoch_ms, crate::time_format::epoch_ms("2026-11-25T10:02:03.000Z").unwrap());
        assert_eq!(hit.snippet.iter().filter(|s| s.mark).map(|s| s.text.as_str()).collect::<Vec<_>>(), ["Oslo"]);
        assert_eq!(page.hits[1].snippet, [Segment { text: "Oslo again".into(), mark: false }], "no parts: the snippet, unmarked");
        assert_eq!(page.next_before.as_deref(), Some("1807402480000000"));
        assert!(page.partial);
        let last = HostPage::from_json(serde_json::json!({"hits": [], "next_before": null}).as_object().unwrap());
        assert_eq!((last.next_before, last.partial), (None, false));
    }
}
