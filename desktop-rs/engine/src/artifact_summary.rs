//! Summary artifact lists (`representation=summary-v1`, Host feature
//! `artifact_summary_v1`) and the bodies they leave out, read back one
//! artifact at a time when something needs them.
//!
//! The list asks for summary rows only when `/server-info` advertises the
//! feature, and falls back only on the old Host's exact 400. An html_form's
//! content and a plan's goal history are read with `GET /artifacts/<id>`
//! when a view needs them (opening a form or report, the report viewer, a
//! plan's history), once per exact key however often they are asked for,
//! into a capped cache. A hydrated body goes back into its row only while
//! the row's marker still names it, so a decision or form never meets a
//! body of another revision; a reply for a row that has moved on since is
//! dropped. Another Host or credential empties the cache.

use std::collections::{HashMap, HashSet};

use clarp_core::artifact_summary::{self as summary, Check, Entry, HydrationCache};
use clarp_core::json::{self, Object};
use serde_json::Value;

use crate::{Change, Engine};

/// How many bodies the cache keeps, and their total size.
pub const CACHE_ENTRIES: usize = 24;
pub const CACHE_BYTES: usize = 32 << 20;

/// A summary row's form bigger than this (or of unknown size) is not read
/// whole when an event names it: the next summary list says what changed,
/// within the list's interval. Smaller ones are read at once, as before.
pub const EVENT_ITEM_BYTES: u64 = 256 << 10;

/// Where an artifact's omitted body stands.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Hydration {
    /// The row holds everything (or never left anything out).
    Ready,
    /// Being read.
    Loading,
    /// Left out and not asked for (or not yet: the Host is still
    /// answering who it is).
    Omitted,
    /// The read failed; asking again retries.
    Failed(String),
    /// The Host has no such artifact any more (404).
    Gone,
    /// Not in the list.
    Unknown,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Kind {
    Content,
    History,
}

/// One `GET /artifacts/<id>` in flight, and the marker it was asked for.
struct Request {
    artifact_id: String,
    key: String,
    kind: Kind,
    updated_at: i64,
    version: Value,
    sha256: Option<String>,
    digest: Option<String>,
}

pub(crate) struct ArtifactSummary {
    /// `/server-info` advertised the feature.
    advertised: bool,
    /// This Host refused summary-v1 (the exact 400): plain lists until the
    /// next connection.
    refused: bool,
    /// `server_id|credential` of the last `/server-info`.
    scope: String,
    /// `/server-info` answered since the last reconnect: reads may go out.
    ready: bool,
    cache: HydrationCache,
    next_request: u64,
    requests: HashMap<u64, Request>,
    /// Exact key -> the request reading it (one at a time).
    in_flight: HashMap<String, u64>,
    /// List request tags sent as summary-v1 -> the same list without it.
    list_tags: HashMap<String, String>,
    /// Artifact id -> the key whose read failed, and why.
    failed: HashMap<String, (String, String)>,
    gone: HashSet<String>,
    /// Ids a view asked for (opened, or about to open).
    wanted: HashSet<String>,
    /// The artifact the report viewer shows.
    in_view: Option<String>,
    /// Keys refetched once after a hash mismatch.
    refetched: HashSet<String>,
    /// History read while the Host's digest was pending: (count, last_at)
    /// it came with, kept for the view that asked until it closes or the
    /// marker moves.
    unpinned: HashMap<String, (u64, Option<i64>, Value)>,
    /// Hydration requests sent, per artifact (tests count them).
    sent: HashMap<String, u64>,
}

impl Default for ArtifactSummary {
    fn default() -> Self {
        ArtifactSummary {
            advertised: false,
            refused: false,
            scope: String::new(),
            ready: false,
            cache: HydrationCache::new(CACHE_ENTRIES, CACHE_BYTES),
            next_request: 0,
            requests: HashMap::new(),
            in_flight: HashMap::new(),
            list_tags: HashMap::new(),
            failed: HashMap::new(),
            gone: HashSet::new(),
            wanted: HashSet::new(),
            in_view: None,
            refetched: HashSet::new(),
            unpinned: HashMap::new(),
            sent: HashMap::new(),
        }
    }
}

fn id_of(row: &Value) -> &str {
    row.get("artifact_id").and_then(Value::as_str).unwrap_or_default()
}

impl Engine {
    // ---- queries ---------------------------------------------------------

    /// Lists ask for summary rows: advertised and not refused.
    pub fn artifact_summary_enabled(&self) -> bool {
        self.artifact_summary.advertised && !self.artifact_summary.refused
    }

    /// Where the artifact's omitted body stands.
    pub fn artifact_hydration(&self, artifact_id: &str) -> Hydration {
        let s = &self.artifact_summary;
        if s.gone.contains(artifact_id) {
            return Hydration::Gone;
        }
        let Some(row) = self.updates.artifacts().iter().find(|a| id_of(a) == artifact_id) else { return Hydration::Unknown };
        if summary::complete(row) {
            return Hydration::Ready;
        }
        if s.requests.values().any(|r| r.artifact_id == artifact_id) {
            return Hydration::Loading;
        }
        match s.failed.get(artifact_id) {
            Some((_, why)) => Hydration::Failed(why.clone()),
            None => Hydration::Omitted,
        }
    }

    /// Hydrated bodies cached: (entries, bytes).
    pub fn artifact_cache_size(&self) -> (usize, usize) {
        (self.artifact_summary.cache.len(), self.artifact_summary.cache.bytes())
    }

    /// `GET /artifacts/<id>` hydrations sent for this artifact.
    pub fn artifact_hydrations_sent(&self, artifact_id: &str) -> u64 {
        self.artifact_summary.sent.get(artifact_id).copied().unwrap_or(0)
    }

    // ---- commands --------------------------------------------------------

    /// A view needs the artifact whole (opening a form or report, a plan's
    /// history): reads what its row left out unless it is already there or
    /// on its way. Asking again after a failure retries.
    pub fn need_artifact(&mut self, artifact_id: &str) -> Hydration {
        if artifact_id.is_empty() {
            return Hydration::Unknown;
        }
        let state = self.artifact_hydration(artifact_id);
        if matches!(state, Hydration::Ready | Hydration::Gone | Hydration::Unknown) {
            return state;
        }
        self.artifact_summary.wanted.insert(artifact_id.to_owned());
        if self.artifact_summary.failed.remove(artifact_id).is_some() {
            self.changes.push(Change::Updates);
        }
        self.reconcile_artifacts();
        self.artifact_hydration(artifact_id)
    }

    /// The view that needed it is done with it.
    pub fn release_artifact(&mut self, artifact_id: &str) {
        self.artifact_summary.wanted.remove(artifact_id);
        if self.artifact_summary.unpinned.remove(artifact_id).is_some() {
            self.reconcile_artifacts();
        }
    }

    /// The report viewer shows this artifact (None: closed). Its body is
    /// kept whole: read now, and again when its marker changes.
    pub fn set_artifact_in_view(&mut self, artifact_id: Option<&str>) {
        let next = artifact_id.filter(|id| !id.is_empty()).map(str::to_owned);
        if self.artifact_summary.in_view == next {
            return;
        }
        if let Some(previous) = std::mem::replace(&mut self.artifact_summary.in_view, next.clone()) {
            self.release_artifact(&previous);
        }
        if let Some(id) = next {
            self.need_artifact(&id);
        }
    }

    /// Smaller caps for tests.
    pub fn set_artifact_cache_limits(&mut self, entries: usize, bytes: usize) {
        self.artifact_summary.cache.set_limits(entries, bytes);
        self.reconcile_artifacts();
    }

    // ---- the list --------------------------------------------------------

    /// An event named this artifact: with summary lists, a big form no
    /// view needs waits for the next list instead of being read whole.
    pub(crate) fn artifact_summary_defers(&self, artifact_id: &str) -> bool {
        let s = &self.artifact_summary;
        if !self.artifact_summary_enabled() || s.wanted.contains(artifact_id) || s.in_view.as_deref() == Some(artifact_id) {
            return false;
        }
        let row = self.updates.artifacts().iter().find(|a| id_of(a) == artifact_id);
        row.and_then(summary::content_marker).is_some_and(|m| m.bytes.is_none_or(|bytes| bytes > EVENT_ITEM_BYTES))
    }

    /// A whole item read for an event, as a summary list holds it: an
    /// html_form's body goes to the cache (its hash taken here) and the row
    /// gets a marker, so the list stays the same shape and size whichever
    /// way its rows came.
    pub(crate) fn summarize_item(&mut self, mut item: Value) -> Value {
        let s = &mut self.artifact_summary;
        if !(s.advertised && !s.refused && s.ready) || item.get("type").and_then(Value::as_str) != Some("html_form") {
            return item;
        }
        let Some(object) = item.as_object_mut().filter(|o| o.get("content").is_some_and(Value::is_string)) else { return item };
        let Some(Value::String(content)) = object.remove("content") else { return item };
        let id = object.get("artifact_id").and_then(Value::as_str).unwrap_or_default().to_owned();
        let updated_at = object.get("updated_at").and_then(Value::as_i64).unwrap_or(0);
        let version = object.get("version").cloned().unwrap_or(Value::Null);
        let sha = summary::sha256_hex(&content);
        object.insert(
            "content_omitted".into(),
            serde_json::json!({"reason": "summary", "bytes": content.len(), "sha256": format!("{}{sha}", summary::CONTENT_HASH), "version": version, "updated_at": updated_at}),
        );
        let key = summary::content_key(&s.scope, &id, updated_at, &version);
        s.cache.insert(key, Entry::content(&id, &content, Some(sha)));
        item
    }

    /// The artifact list's path for this request: summary-v1 when the Host
    /// offers it (remembered by tag for the 400 fallback).
    pub(crate) fn artifact_list_path(&mut self, tag: &str, plain: &str) -> String {
        if !self.artifact_summary_enabled() {
            return plain.to_owned();
        }
        self.artifact_summary.list_tags.insert(tag.to_owned(), plain.to_owned());
        summary::with_representation(plain, summary::REPRESENTATION)
    }

    /// A list arrived: summary rows get the bodies cached for them, and
    /// views that need one have it read.
    pub(crate) fn artifact_list_applied(&mut self) {
        let listed: HashSet<String> = self.updates.artifacts().iter().map(|a| id_of(a).to_owned()).collect();
        self.artifact_summary.gone.retain(|id| !listed.contains(id));
        self.reconcile_artifacts();
    }

    /// Puts cached bodies into the rows whose markers name them, takes
    /// out those no longer cached, drops reads for rows that moved on, and
    /// reads what a view still needs.
    fn reconcile_artifacts(&mut self) {
        let (scope, ready) = (self.artifact_summary.scope.clone(), self.artifact_summary.ready);
        let mut needed: Vec<Request> = Vec::new();
        let mut current: HashMap<String, String> = HashMap::new();
        let mut changed = false;
        let mut rows = std::mem::take(self.updates.artifacts_mut());
        for row in rows.iter_mut() {
            let id = id_of(row).to_owned();
            let wanted = self.artifact_summary.wanted.contains(&id) || self.artifact_summary.in_view.as_deref() == Some(id.as_str());
            if let Some(marker) = summary::content_marker(row) {
                let key = summary::content_key(&scope, &id, marker.updated_at, &marker.version);
                current.insert(id.clone(), key.clone());
                let valid = self.cached_valid(&key, marker.sha256.as_deref());
                let had = row.get("content").is_some();
                if valid && !had {
                    let content = self.artifact_summary.cache.peek(&key).and_then(|e| e.value.as_str()).map(str::to_owned);
                    summary::set_content(row, content);
                    changed = true;
                } else if !valid && had {
                    summary::set_content(row, None);
                    changed = true;
                }
                if !valid && wanted && ready {
                    needed.push(Request { artifact_id: id, key, kind: Kind::Content, updated_at: marker.updated_at, version: marker.version, sha256: marker.sha256, digest: None });
                }
            } else if let Some(marker) = summary::history_marker(row) {
                let key = summary::history_key(&scope, &id, marker.digest.as_deref().unwrap_or("pending"));
                current.insert(id.clone(), key.clone());
                let history = match &marker.digest {
                    Some(_) => self.artifact_summary.cache.peek(&key).map(|e| &e.value),
                    None => self
                        .artifact_summary
                        .unpinned
                        .get(&id)
                        .filter(|(count, last_at, _)| *count == marker.count && *last_at == marker.last_at)
                        .map(|(_, _, history)| history),
                };
                let (available, had) = (history.is_some(), summary::goal_history(row).is_some());
                if available && !had {
                    summary::set_goal_history(row, history.cloned());
                    changed = true;
                } else if !available && had {
                    summary::set_goal_history(row, None);
                    changed = true;
                }
                if !available && wanted && ready {
                    needed.push(Request { artifact_id: id, key, kind: Kind::History, updated_at: 0, version: Value::Null, sha256: None, digest: marker.digest });
                }
            }
        }
        *self.updates.artifacts_mut() = rows;
        // A read for a row that has moved on (newer updated_at, version or
        // digest) is retired: its reply is dropped. So is a failure.
        let s = &mut self.artifact_summary;
        let retired: Vec<String> = s
            .in_flight
            .iter()
            .filter(|(key, n)| s.requests.get(*n).and_then(|r| current.get(&r.artifact_id)).is_some_and(|now| now != *key))
            .map(|(key, _)| key.clone())
            .collect();
        for key in retired {
            if let Some(n) = s.in_flight.remove(&key) {
                s.requests.remove(&n);
            }
        }
        s.failed.retain(|id, failed| current.get(id).is_none_or(|now| *now == failed.0));
        for request in needed {
            let s = &self.artifact_summary;
            if s.in_flight.contains_key(&request.key) || s.failed.contains_key(&request.artifact_id) {
                continue;
            }
            self.send_hydration(request);
            changed = true;
        }
        if changed {
            self.changes.push(Change::Updates);
        }
    }

    /// The key is cached, and its content has the marker's hash once one
    /// is known (otherwise it is dropped, to be read again).
    fn cached_valid(&mut self, key: &str, sha256: Option<&str>) -> bool {
        let cache = &mut self.artifact_summary.cache;
        let Some(entry) = cache.peek_mut(key) else { return false };
        let Some(expected) = sha256 else { return true };
        if entry.has_sha256(expected) {
            return true;
        }
        eprintln!("Artifacts: the cached content of {} does not match the Host's hash; reading it again", entry.artifact_id);
        cache.remove(key);
        false
    }

    fn send_hydration(&mut self, request: Request) {
        let s = &mut self.artifact_summary;
        s.next_request += 1;
        let n = s.next_request;
        *s.sent.entry(request.artifact_id.clone()).or_default() += 1;
        // flat-v1: a summary-capable Host leaves out only the duplicate
        // `payload`; an older one ignores it and answers in full. Either
        // way the top-level fields are read.
        let item = format!("/artifacts/{}", clarp_core::endpoint::percent_encode_segment(&request.artifact_id));
        let path = summary::with_representation(&item, clarp_core::update_feeds::FLAT_REPRESENTATION);
        s.in_flight.insert(request.key.clone(), n);
        s.requests.insert(n, request);
        self.api.get_with_timeout(&format!("artifact-hydrate:{n}"), &path, crate::updates::UPDATES_TIMEOUT);
    }

    /// The request a reply answers, unless it was retired or superseded.
    fn take_hydration(&mut self, tag: &str) -> Option<Option<Request>> {
        let n = tag.strip_prefix("artifact-hydrate:")?.parse::<u64>().ok();
        let s = &mut self.artifact_summary;
        let Some(request) = n.and_then(|n| s.requests.remove(&n)) else { return Some(None) };
        if s.in_flight.get(&request.key) == n.as_ref() {
            s.in_flight.remove(&request.key);
        }
        Some(Some(request))
    }

    // ---- replies -----------------------------------------------------------

    /// True when the reply was a hydration's (a list's goes on to Updates).
    pub(crate) fn artifact_summary_json(&mut self, tag: &str, object: &Object) -> bool {
        self.artifact_summary.list_tags.remove(tag);
        let Some(request) = self.take_hydration(tag) else { return false };
        let Some(request) = request else { return true };
        let item = object.get("artifact").filter(|a| a.is_object()).cloned().unwrap_or(Value::Null);
        let id = request.artifact_id.clone();
        match request.kind {
            Kind::Content => {
                let Some(content) = item.get("content").and_then(Value::as_str) else {
                    return self.hydration_failed(&id, &request.key, "the Host sent the artifact without its content");
                };
                let updated_at = item.get("updated_at").and_then(Value::as_i64).unwrap_or(0);
                let version = item.get("version").cloned().unwrap_or(Value::Null);
                if (updated_at, &version) != (request.updated_at, &request.version) {
                    // It changed since the list: kept under its own key; the
                    // list catches up and its new marker finds it.
                    eprintln!("Artifacts: {id} changed since the list was read; reloading the list");
                    let key = summary::content_key(&self.artifact_summary.scope, &id, updated_at, &version);
                    self.artifact_summary.cache.insert(key, Entry::content(&id, content, None));
                    self.load_updates();
                    self.reconcile_artifacts();
                    return true;
                }
                match summary::check_content(request.sha256.as_deref(), content) {
                    Check::Mismatch => {
                        if self.artifact_summary.refetched.insert(request.key.clone()) {
                            eprintln!("Artifacts: {id}'s content does not match the Host's hash; reading it again");
                            self.send_hydration(request);
                        } else {
                            self.hydration_failed(&id, &request.key, "its content does not match the Host's hash");
                        }
                        return true;
                    }
                    check => {
                        let known = if check == Check::Match { request.sha256.clone() } else { None };
                        let entry = Entry::content(&id, content, known);
                        self.artifact_summary.cache.insert(request.key.clone(), entry);
                    }
                }
            }
            Kind::History => {
                let Some(history) = item.get("plan").and_then(|p| p.get("goal")).and_then(|g| g.get("history")).filter(|h| h.is_array()).cloned() else {
                    return self.hydration_failed(&id, &request.key, "the Host sent the plan without its history");
                };
                match &request.digest {
                    Some(_) => {
                        self.artifact_summary.cache.insert(request.key.clone(), Entry::history(&id, history));
                    }
                    None => {
                        let marker = self.updates.artifacts().iter().find(|a| id_of(a) == id).and_then(summary::history_marker);
                        if let Some(marker) = marker {
                            self.artifact_summary.unpinned.insert(id.clone(), (marker.count, marker.last_at, history));
                        }
                    }
                }
            }
        }
        self.artifact_summary.failed.remove(&id);
        self.reconcile_artifacts();
        self.changes.push(Change::Updates);
        true
    }

    /// True when the failure was a hydration's, or a summary list's that
    /// falls back (the old Host's exact 400: the same list, plain).
    pub(crate) fn artifact_summary_failure(&mut self, tag: &str, message: &str, status: u16) -> bool {
        if let Some(plain) = self.artifact_summary.list_tags.remove(tag) {
            if !summary::is_unsupported_representation(status, message) {
                return false;
            }
            eprintln!("Artifacts: the Host refused representation={}; asking without it", summary::REPRESENTATION);
            self.artifact_summary.refused = true;
            self.api.get_with_timeout(tag, &plain, crate::updates::UPDATES_TIMEOUT);
            return true;
        }
        let Some(request) = self.take_hydration(tag) else { return false };
        let Some(request) = request else { return true };
        let (id, key) = (request.artifact_id, request.key);
        if status == 404 {
            eprintln!("Artifacts: {id} was deleted on the Host");
            let shown = self.artifact_summary.wanted.contains(&id) || self.artifact_summary.in_view.as_deref() == Some(id.as_str());
            if shown {
                let title = self.updates.artifacts().iter().find(|a| id_of(a) == id).map(|a| json::js_string(a.get("title"))).unwrap_or_default();
                let named = if title.is_empty() { "This artifact".to_owned() } else { format!("“{title}”") };
                self.set_error(&format!("{named} was deleted on the Host"));
            }
            self.artifact_summary.cache.remove_artifact(&id);
            self.artifact_summary.gone.insert(id.clone());
            self.artifact_summary.wanted.remove(&id);
            self.artifact_summary.unpinned.remove(&id);
            self.updates.artifacts_mut().retain(|a| id_of(a) != id);
            self.changes.push(Change::Updates);
            return true;
        }
        let detail = if status > 0 { format!("{message} (HTTP {status})") } else { message.to_owned() };
        self.hydration_failed(&id, &key, &detail)
    }

    fn hydration_failed(&mut self, artifact_id: &str, key: &str, why: &str) -> bool {
        eprintln!("Artifacts: {artifact_id} could not be read whole: {why}");
        self.artifact_summary.failed.insert(artifact_id.to_owned(), (key.to_owned(), why.to_owned()));
        self.changes.push(Change::Updates);
        true
    }

    // ---- the connection ----------------------------------------------------

    /// `/server-info` answered: whether this Host offers summary rows, and
    /// whose cache this is (another Host or credential empties it).
    pub(crate) fn artifact_summary_server_info(&mut self, info: &Object) {
        let scope = summary::scope(&json::string(info, "server_id"), &self.base_url, &self.token);
        let s = &mut self.artifact_summary;
        s.advertised = summary::advertised(info);
        s.refused = false;
        s.ready = true;
        if s.scope != scope {
            if !s.cache.is_empty() {
                eprintln!("Artifacts: another Host or credential; dropping {} cached bodies", s.cache.len());
            }
            s.cache.clear();
            s.unpinned.clear();
            s.refetched.clear();
            s.scope = scope;
        }
        self.reconcile_artifacts();
    }

    /// Another Host or token: nothing in flight survives, and nothing is
    /// read until `/server-info` says whose cache it is.
    pub(crate) fn reset_artifact_summary(&mut self) {
        let s = &mut self.artifact_summary;
        s.advertised = false;
        s.refused = false;
        s.ready = false;
        s.requests.clear();
        s.in_flight.clear();
        s.list_tags.clear();
        s.failed.clear();
    }
}
