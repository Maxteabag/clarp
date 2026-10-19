//! The artifact list's `summary-v1` projection (Host feature
//! `artifact_summary_v1`) and what it takes to read an omitted body back.
//!
//! A summary row is the flat-v1 row without its two bulky fields: an
//! html_form's `content` (77% of a real list's bytes, mostly inline
//! pictures) and a plan's `plan.goal.history`. Each is replaced by a marker,
//! `content_omitted` and `plan.goal.history_omitted`; the key itself is
//! absent, so an omitted body is never mistaken for an empty one. The full
//! item is `GET /artifacts/<id>` (it ignores `representation`).
//!
//! A hydrated body is pinned to the marker it was read for: content by
//! (Host, credential, artifact, `updated_at`, version), the Host bumping
//! `updated_at` on every content write, and checked against the marker's
//! sha256 when it has one; history by its digest (a plan's revision does
//! not move when recovery appends to it).

use std::collections::{HashMap, VecDeque};

use serde_json::Value;
use sha2::{Digest, Sha256};

use crate::json::Object;

/// The feature `/server-info` advertises, under `capabilities.features`,
/// with `contract.features[FEATURE]` naming the Host contract that
/// introduced it.
pub const FEATURE: &str = "artifact_summary_v1";
/// `representation=` on `GET /artifacts` and `/artifacts/pinned`.
pub const REPRESENTATION: &str = "summary-v1";
/// The old Host's answer (HTTP 400) to a representation it does not know.
pub const UNSUPPORTED: &str = "unsupported artifact representation";

/// The Host offers summary rows: the feature is listed and
/// `contract.features` gives it a positive introduction contract. That value
/// is whatever contract the feature landed in, not a version of the feature,
/// so any positive number counts. Never inferred from the Host's contract
/// number.
pub fn advertised(info: &Object) -> bool {
    let listed = info
        .get("capabilities")
        .and_then(|c| c.get("features"))
        .and_then(Value::as_array)
        .is_some_and(|features| features.iter().any(|f| f.as_str() == Some(FEATURE)));
    let introduced = info.get("contract").and_then(|c| c.get("features")).and_then(|f| f.get(FEATURE)).and_then(Value::as_i64);
    listed && introduced.is_some_and(|n| n > 0)
}

/// Exactly the old Host's refusal: the one error that falls back to the
/// previous representation. Every other failure is the user's to see.
pub fn is_unsupported_representation(status: u16, message: &str) -> bool {
    status == 400 && message.trim() == UNSUPPORTED
}

/// `path` asking for `representation` instead of any it named.
pub fn with_representation(path: &str, representation: &str) -> String {
    let (route, query) = path.split_once('?').unwrap_or((path, ""));
    let mut pairs: Vec<&str> = query.split('&').filter(|p| !p.is_empty() && !p.starts_with("representation=")).collect();
    let asked = format!("representation={representation}");
    pairs.push(&asked);
    format!("{route}?{}", pairs.join("&"))
}

/// An html_form row's `content_omitted`.
#[derive(Debug, Clone, PartialEq)]
pub struct ContentMarker {
    /// UTF-8 bytes; None when the Host had no time to measure them.
    pub bytes: Option<u64>,
    /// The sha256 hex of a `c1:` hash; None while the Host's hash is
    /// pending, or when it is of a kind this client cannot check.
    pub sha256: Option<String>,
    /// The payload's version (Null when it has none).
    pub version: Value,
    pub updated_at: i64,
}

/// A plan row's `plan.goal.history_omitted`.
#[derive(Debug, Clone, PartialEq)]
pub struct HistoryMarker {
    pub count: u64,
    pub last_at: Option<i64>,
    /// The history's identity (`h1:…`), opaque: only ever compared, never
    /// recomputed. None while the Host's digest is pending.
    pub digest: Option<String>,
}

fn summary_reason(marker: &Object) -> bool {
    marker.get("reason").and_then(Value::as_str) == Some("summary")
}

/// The prefix of a content hash this client can check: sha256 over the
/// UTF-8 bytes of the decoded `content` string, as hex.
pub const CONTENT_HASH: &str = "c1:";

/// A marker's content hash as bare hex, when it is a `c1:` one. Another
/// kind cannot be checked here: it counts as pending.
fn content_hash(value: Option<&Value>) -> Option<String> {
    let hex = value.and_then(Value::as_str)?.strip_prefix(CONTENT_HASH)?;
    (hex.len() == 64 && hex.bytes().all(|b| b.is_ascii_hexdigit())).then(|| hex.to_ascii_lowercase())
}

/// `c1:<hex>`: the content hash a summary marker carries.
pub fn content_hash_of(content: &str) -> String {
    format!("{CONTENT_HASH}{}", sha256_hex(content))
}

/// The row's content marker, whether or not its body was put back.
pub fn content_marker(row: &Value) -> Option<ContentMarker> {
    let marker = row.get("content_omitted")?.as_object().filter(|m| summary_reason(m))?;
    Some(ContentMarker {
        bytes: marker.get("bytes").and_then(Value::as_u64),
        sha256: content_hash(marker.get("sha256")),
        version: marker.get("version").cloned().unwrap_or(Value::Null),
        updated_at: marker.get("updated_at").and_then(Value::as_i64).or_else(|| row.get("updated_at").and_then(Value::as_i64)).unwrap_or(0),
    })
}

/// The plan row's history marker, whether or not its history was put back.
pub fn history_marker(row: &Value) -> Option<HistoryMarker> {
    let marker = row.get("plan")?.get("goal")?.get("history_omitted")?.as_object().filter(|m| summary_reason(m))?;
    Some(HistoryMarker {
        count: marker.get("count").and_then(Value::as_u64).unwrap_or(0),
        last_at: marker.get("last_at").and_then(Value::as_i64),
        digest: marker.get("digest").and_then(Value::as_str).filter(|d| !d.is_empty()).map(str::to_owned),
    })
}

/// The row holds every body its markers stand for (always, without them).
pub fn complete(row: &Value) -> bool {
    let content = content_marker(row).is_none() || row.get("content").is_some_and(Value::is_string);
    let history = history_marker(row).is_none() || goal_history(row).is_some();
    content && history
}

/// `plan.goal.history` when the row holds it.
pub fn goal_history(row: &Value) -> Option<&Value> {
    row.get("plan")?.get("goal")?.get("history").filter(|h| h.is_array())
}

/// Puts a hydrated history into the row's plan (or takes it out: None).
pub fn set_goal_history(row: &mut Value, history: Option<Value>) {
    let Some(goal) = row.get_mut("plan").and_then(|p| p.get_mut("goal")).and_then(Value::as_object_mut) else { return };
    match history {
        Some(history) => {
            goal.insert("history".into(), history);
        }
        None => {
            goal.remove("history");
        }
    }
}

/// Puts hydrated content into the row (or takes it out: None).
pub fn set_content(row: &mut Value, content: Option<String>) {
    let Some(object) = row.as_object_mut() else { return };
    match content {
        Some(content) => {
            object.insert("content".into(), Value::String(content));
        }
        None => {
            object.remove("content");
        }
    }
}

pub fn sha256_hex(text: &str) -> String {
    Sha256::digest(text.as_bytes()).iter().map(|b| format!("{b:02x}")).collect()
}

/// Whose cache this is: the Host's `server_id` and the desktop's own
/// credential (its address and token, never stored as such). Another Host,
/// a re-pairing or a rotated token makes another scope.
pub fn scope(server_id: &str, base_url: &str, token: &str) -> String {
    let credential = sha256_hex(&format!("{base_url}\u{0}{token}"));
    format!("{server_id}|{}", &credential[..16])
}

/// The cache key of an html_form's content.
pub fn content_key(scope: &str, artifact_id: &str, updated_at: i64, version: &Value) -> String {
    format!("{scope}\u{0}content\u{0}{artifact_id}\u{0}{updated_at}\u{0}{version}")
}

/// The cache key of a plan's history.
pub fn history_key(scope: &str, artifact_id: &str, digest: &str) -> String {
    format!("{scope}\u{0}history\u{0}{artifact_id}\u{0}{digest}")
}

/// Hydrated content against its marker's hash.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Check {
    Match,
    Mismatch,
    /// The marker has no hash (yet): identity is the key alone.
    Unknown,
}

pub fn check_content(sha256: Option<&str>, content: &str) -> Check {
    match sha256 {
        None => Check::Unknown,
        Some(expected) if sha256_hex(content) == expected => Check::Match,
        Some(_) => Check::Mismatch,
    }
}

/// One hydrated body.
#[derive(Debug, Clone, PartialEq)]
pub struct Entry {
    pub artifact_id: String,
    /// Content (a string) or history (an array).
    pub value: Value,
    pub bytes: usize,
    /// The content's own sha256, once a marker asked (history is pinned by
    /// its digest instead).
    pub sha256: Option<String>,
}

impl Entry {
    /// `sha256`: the hash it is known to have (it matched a marker).
    pub fn content(artifact_id: &str, content: &str, sha256: Option<String>) -> Self {
        Entry { artifact_id: artifact_id.to_owned(), value: Value::String(content.to_owned()), bytes: content.len(), sha256 }
    }

    pub fn history(artifact_id: &str, history: Value) -> Self {
        let bytes = history.to_string().len();
        Entry { artifact_id: artifact_id.to_owned(), value: history, bytes, sha256: None }
    }

    /// Whether the content has this hash (hashed here the first time).
    pub fn has_sha256(&mut self, expected: &str) -> bool {
        if self.sha256.is_none() {
            self.sha256 = self.value.as_str().map(sha256_hex);
        }
        self.sha256.as_deref() == Some(expected)
    }
}

/// Hydrated bodies, least recently used out first, capped by count and by
/// bytes. The entry just put in stays even when it alone is over the cap.
#[derive(Debug)]
pub struct HydrationCache {
    entries: HashMap<String, Entry>,
    /// Oldest use first.
    order: VecDeque<String>,
    bytes: usize,
    max_entries: usize,
    max_bytes: usize,
}

impl HydrationCache {
    pub fn new(max_entries: usize, max_bytes: usize) -> Self {
        HydrationCache { entries: HashMap::new(), order: VecDeque::new(), bytes: 0, max_entries: max_entries.max(1), max_bytes }
    }

    pub fn set_limits(&mut self, max_entries: usize, max_bytes: usize) {
        self.max_entries = max_entries.max(1);
        self.max_bytes = max_bytes;
        self.evict(None);
    }

    pub fn len(&self) -> usize {
        self.entries.len()
    }

    pub fn is_empty(&self) -> bool {
        self.entries.is_empty()
    }

    pub fn bytes(&self) -> usize {
        self.bytes
    }

    pub fn peek(&self, key: &str) -> Option<&Entry> {
        self.entries.get(key)
    }

    /// The entry to change, its place in the order unchanged.
    pub fn peek_mut(&mut self, key: &str) -> Option<&mut Entry> {
        self.entries.get_mut(key)
    }

    /// The entry, now the most recently used.
    pub fn get(&mut self, key: &str) -> Option<&mut Entry> {
        if !self.entries.contains_key(key) {
            return None;
        }
        self.touch(key);
        self.entries.get_mut(key)
    }

    /// Puts it in; the keys pushed out to stay under the caps.
    pub fn insert(&mut self, key: String, entry: Entry) -> Vec<String> {
        self.remove(&key);
        self.bytes += entry.bytes;
        self.entries.insert(key.clone(), entry);
        self.order.push_back(key.clone());
        self.evict(Some(&key))
    }

    pub fn remove(&mut self, key: &str) -> Option<Entry> {
        let entry = self.entries.remove(key)?;
        self.bytes -= entry.bytes;
        self.order.retain(|k| k != key);
        Some(entry)
    }

    /// Every body of one artifact (it was deleted).
    pub fn remove_artifact(&mut self, artifact_id: &str) {
        let keys: Vec<String> = self.entries.iter().filter(|(_, e)| e.artifact_id == artifact_id).map(|(k, _)| k.clone()).collect();
        for key in keys {
            self.remove(&key);
        }
    }

    pub fn clear(&mut self) {
        self.entries.clear();
        self.order.clear();
        self.bytes = 0;
    }

    fn touch(&mut self, key: &str) {
        if let Some(at) = self.order.iter().position(|k| k == key) {
            if let Some(key) = self.order.remove(at) {
                self.order.push_back(key);
            }
        }
    }

    fn evict(&mut self, keep: Option<&str>) -> Vec<String> {
        let mut evicted = Vec::new();
        while self.entries.len() > self.max_entries || self.bytes > self.max_bytes {
            let Some(oldest) = self.order.iter().find(|k| Some(k.as_str()) != keep).cloned() else { break };
            self.remove(&oldest);
            evicted.push(oldest);
        }
        evicted
    }
}
