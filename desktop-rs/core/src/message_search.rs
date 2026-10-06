//! Message search over what this client holds: the loaded chats and the
//! on-disk transcript cache (the Host has no search). One index per chat,
//! updated row by row as rows arrive; a row is folded to lower case once,
//! when it is new or changed. Results rank by how well they match, then by
//! how recent they are.

use std::collections::HashMap;

use crate::protocol::Message;

/// Where a chat's rows came from: loaded in this run, or only the cache.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Source {
    Memory,
    Cache,
}

#[allow(dead_code)]
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

#[allow(dead_code)]
#[derive(Debug, Default)]
struct Chat {
    source: Option<Source>,
    entries: Vec<Entry>,
}

#[allow(dead_code)]
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

impl SearchIndex {
    pub fn update_chat(&mut self, _session: &str, _rows: &[Message], _source: Source) -> usize {
        0
    }

    pub fn remove_chat(&mut self, _session: &str) {}

    pub fn source(&self, _session: &str) -> Option<Source> {
        None
    }

    pub fn chats(&self, _source: Source) -> usize {
        0
    }

    pub fn search(&self, _query: &str, _limit: usize, _now_ms: i64) -> Vec<Hit> {
        Vec::new()
    }
}

pub fn snippet(_text: &str, _terms: &[String], _width: usize) -> Vec<Segment> {
    Vec::new()
}
