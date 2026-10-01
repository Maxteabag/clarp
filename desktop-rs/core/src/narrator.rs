//! Tool-activity explanations through the Host's shared `/tool-explanations`
//! service. Port of the shared-Host half of `desktop/src/app/ToolNarrator`
//! (the controller always gives the C++ narrator an API client, so its local
//! `codex exec` fallback only ever ran in tests and is not ported).
//!
//! Presentation-only: it never changes transcripts or dispatches an agent
//! command. The state machine is pure; it returns [`Effect`]s that the Qt
//! adapter performs (a Host request, a debounce, a poll, a timeout).

use std::collections::{HashMap, HashSet, VecDeque};
use std::sync::LazyLock;

use fancy_regex::Regex;
use serde_json::{Map, Value, json};

use crate::json::{self, Object};

const MAX_CACHE_ENTRIES: usize = 512;
const MAX_QUEUED_ENTRIES: usize = 64;
const BATCH_SIZE: usize = 8;
/// Cached in place of a failed row. Never rendered: `explanation` reports it
/// as empty and `failed` as a failure, so the card shows the original call.
const FAILURE_MARKER: &str = "\u{1}explanation-unavailable";

pub const DEBOUNCE_MS: u64 = 180;
pub const POLL_MS: u64 = 600;
pub const TIMEOUT_MS: u64 = 65_000;

pub const DETAIL_LEVELS: [&str; 5] = ["Developer", "Technical", "Balanced", "Plain English", "Grandma"];
const LEVEL_DESCRIPTIONS: [&str; 5] = [
    "Original tool calls. No translation requests or model usage.",
    "Explain the effect while retaining commands, paths, and precise technical terms.",
    "Explain the action and useful context, with only essential technical details.",
    "Everyday language about the task. Omit code, paths, and implementation jargon.",
    "Short, concrete explanations for someone with no technical background.",
];

#[derive(Debug, Clone, PartialEq)]
pub enum Effect {
    /// POST `/tool-explanations` with this tag and body.
    HostRequest { tag: String, body: Value },
    /// Call `start_batch` after `DEBOUNCE_MS`.
    Debounce,
    /// Call `poll` after `POLL_MS` (tag guards against stale polls).
    Poll { tag: String },
    /// Call `timed_out(generation)` after `TIMEOUT_MS`.
    Timeout { generation: u64 },
    /// The revision moved: views re-read explanations and status.
    Changed,
    DetailLevelChanged,
    EnabledChanged,
}

fn variant_string(value: Option<&Value>) -> String {
    match value {
        Some(Value::String(s)) => s.clone(),
        Some(Value::Number(n)) => n.to_string(),
        Some(Value::Bool(b)) => b.to_string(),
        _ => String::new(),
    }
}

/// Bounded text with common inline credentials removed. Results and chat
/// history are never included; this is not a general secret scanner.
pub fn snippet(text: &str, limit: usize) -> String {
    static CREDENTIALS: LazyLock<Regex> = LazyLock::new(|| {
        Regex::new(r#"(?i)((?:authorization["']?\s*[:=]\s*["']?bearer|(?:api[_-]?key|token|password|secret)["']?\s*[=:])\s*["']?)[^\s"';]+"#)
            .expect("pattern")
    });
    static API_KEY: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"\bsk-[A-Za-z0-9_-]{12,}").expect("pattern"));
    let head: String = text.chars().take(limit + 200).collect();
    let redacted = CREDENTIALS.replace_all(&head, "${1}[redacted]");
    let redacted = API_KEY.replace_all(&redacted, "[redacted]");
    redacted.chars().take(limit).collect()
}

/// What may leave the machine for one activity. Status, result and output
/// are excluded: a streaming result must not trigger another request or
/// change what a command means. `None` when there is nothing to explain.
pub fn payload(activity: &Object) -> Option<Object> {
    let mut object = Map::new();
    if activity.contains_key("_session") {
        object.insert("_session".into(), Value::from(variant_string(activity.get("_session"))));
    }
    if activity.contains_key("id") {
        object.insert("_activity_id".into(), Value::from(variant_string(activity.get("id"))));
    }
    for field in ["kind", "name", "summary", "description", "command", "file_path"] {
        let value = variant_string(activity.get(field));
        if !value.is_empty() {
            object.insert(field.into(), Value::from(snippet(&value, 1600)));
        }
    }
    if !object.contains_key("kind") && !object.contains_key("name") {
        object.insert("name".into(), Value::from(snippet(&variant_string(activity.get("title")), 1600)));
    }
    match activity.get("input") {
        Some(Value::String(input)) => {
            object.insert("input".into(), Value::from(snippet(input, 1600)));
        }
        Some(Value::Object(arguments)) => {
            // Never serialize arbitrary nested payloads: they may hold full
            // results, image data, authentication or a whole conversation.
            let mut selected = Map::new();
            for field in ["command", "cmd", "code", "file_path", "path", "pattern", "query", "description", "cwd"] {
                if let Some(Value::String(value)) = arguments.get(field) {
                    selected.insert(field.into(), Value::from(snippet(value, 1600)));
                }
            }
            if !selected.is_empty() {
                object.insert("input".into(), Value::Object(selected));
            }
        }
        _ => {}
    }
    // Grouped cells carry their operation in labeled lines; never forward
    // stdout, stderr, diffs or media.
    let mut operations = Vec::new();
    for line in json::array(activity, "lines").iter().filter_map(Value::as_object) {
        let label = json::string(line, "label");
        if label.is_empty() || operations.len() >= 6 {
            continue;
        }
        let kind = json::string(line, "kind");
        if kind.starts_with("diff") || kind == "output" {
            continue;
        }
        let text: String = snippet(&format!("{label}: {}", json::string(line, "text")), 1600).chars().take(240).collect();
        operations.push(Value::from(text));
    }
    if !operations.is_empty() {
        object.insert("operations".into(), Value::Array(operations));
    }
    let name_only_empty = object.len() == 1 && object.get("name").and_then(Value::as_str).is_some_and(str::is_empty);
    if object.is_empty() || name_only_empty {
        return None;
    }
    Some(object)
}

/// The explanation id: SHA-256 of the compact payload (keys sorted).
pub fn key(payload: Option<&Object>) -> String {
    use sha2::{Digest, Sha256};
    let bytes = payload.map(|p| Value::Object(p.clone()).to_string()).unwrap_or_default();
    Sha256::digest(bytes.as_bytes()).iter().map(|b| format!("{b:02x}")).collect()
}

#[derive(Debug, Clone)]
struct Queued {
    id: String,
    activity: Object,
}

#[derive(Debug, Default)]
pub struct Narrator {
    enabled: bool,
    detail_level: i32,
    last_translation_level: i32,
    revision: u64,
    error: String,
    cache: HashMap<String, String>,
    cache_order: VecDeque<String>,
    requested: HashSet<String>,
    queue: VecDeque<Queued>,
    debounce_active: bool,
    view_keys: HashMap<u64, String>,
    demand_ids: HashMap<String, String>,
    remote_items: Vec<Value>,
    remote_session: String,
    remote_tag: String,
    remote_generation: u64,
    key_memo: HashMap<String, String>,
}

impl Narrator {
    pub fn new() -> Self {
        Self { last_translation_level: 3, ..Self::default() }
    }

    pub fn enabled(&self) -> bool {
        self.enabled
    }
    pub fn detail_level(&self) -> i32 {
        self.detail_level
    }
    pub fn revision(&self) -> u64 {
        self.revision
    }
    pub fn unavailable(&self) -> bool {
        !self.error.is_empty()
    }
    pub fn level_description(&self) -> &'static str {
        LEVEL_DESCRIPTIONS[self.detail_level.clamp(0, 4) as usize]
    }

    pub fn status(&self) -> String {
        if !self.enabled {
            return "Off — no background requests".into();
        }
        if !self.error.is_empty() {
            return self.error.clone();
        }
        if !self.remote_items.is_empty() {
            return format!("Host translating · {} activities", self.remote_items.len());
        }
        if !self.queue.is_empty() {
            return format!("Translating activity · {} queued", self.queue.len());
        }
        "Shared Host · Codex Spark · low".into()
    }

    fn notify(&mut self, effects: &mut Vec<Effect>) {
        self.revision += 1;
        effects.push(Effect::Changed);
    }

    /// `key(payload(activity))`, memoized on the raw fields payload reads.
    pub fn cache_key(&mut self, activity: &Object) -> String {
        const SEPARATOR: char = '\u{1f}';
        let mut raw = String::with_capacity(256);
        for field in ["_session", "id", "kind", "name", "summary", "description", "command", "file_path", "title"] {
            raw.push(SEPARATOR);
            raw += &variant_string(activity.get(field));
        }
        match activity.get("input") {
            Some(Value::String(input)) => {
                raw.push(SEPARATOR);
                raw.push('s');
                raw.push(SEPARATOR);
                raw += input;
            }
            Some(Value::Object(arguments)) => {
                raw.push(SEPARATOR);
                raw.push('m');
                for field in ["command", "cmd", "code", "file_path", "path", "pattern", "query", "description", "cwd"] {
                    raw.push(SEPARATOR);
                    if let Some(Value::String(value)) = arguments.get(field) {
                        raw.push('=');
                        raw += value;
                    }
                }
            }
            _ => {}
        }
        for line in json::array(activity, "lines").iter().filter_map(Value::as_object) {
            for field in ["label", "kind", "text"] {
                raw.push(SEPARATOR);
                raw += &variant_string(line.get(field));
            }
        }
        if let Some(found) = self.key_memo.get(&raw) {
            return found.clone();
        }
        if self.key_memo.len() >= 8192 {
            self.key_memo.clear();
        }
        let id = key(payload(activity).as_ref());
        self.key_memo.insert(raw, id.clone());
        id
    }

    pub fn explanation(&mut self, activity: &Object) -> String {
        if !self.enabled {
            return String::new();
        }
        let id = self.cache_key(activity);
        match self.cache.get(&id) {
            Some(text) if text != FAILURE_MARKER => text.clone(),
            _ => String::new(),
        }
    }

    /// True once this row's explanation definitively failed, so the view can
    /// fall back to the original tool call instead of a dead end.
    pub fn failed(&mut self, activity: &Object) -> bool {
        if !self.enabled {
            return false;
        }
        let id = self.cache_key(activity);
        self.cache.get(&id).is_some_and(|text| text == FAILURE_MARKER)
    }

    pub fn request(&mut self, activity: &Object) -> Vec<Effect> {
        let mut effects = Vec::new();
        if !self.enabled || !self.error.is_empty() {
            return effects;
        }
        let Some(payload) = payload(activity) else { return effects };
        let id = key(Some(&payload));
        if self.requested.contains(&id) || self.queue.len() >= MAX_QUEUED_ENTRIES {
            return effects;
        }
        self.requested.insert(id.clone());
        self.queue.push_back(Queued { id, activity: payload });
        if !self.debounce_active {
            self.debounce_active = true;
            effects.push(Effect::Debounce);
        }
        self.notify(&mut effects);
        effects
    }

    pub fn acquire_view(&mut self, owner: u64, activity: &Object) -> Vec<Effect> {
        let mut effects = Vec::new();
        if !self.enabled {
            return effects;
        }
        let id = key(payload(activity).as_ref());
        if self.view_keys.get(&owner) != Some(&id) {
            effects.extend(self.release_view(owner));
            self.view_keys.insert(owner, id.clone());
            self.demand_ids.entry(id).or_insert_with(|| uuid::Uuid::new_v4().to_string());
        }
        effects.extend(self.request(activity));
        effects
    }

    pub fn release_view(&mut self, owner: u64) -> Vec<Effect> {
        let mut effects = Vec::new();
        let Some(id) = self.view_keys.remove(&owner) else { return effects };
        if id.is_empty() || self.view_keys.values().any(|other| *other == id) {
            return effects;
        }
        let demand = self.demand_ids.remove(&id).unwrap_or_default();
        let in_flight = self.remote_items.iter().any(|item| item.get("id").and_then(Value::as_str) == Some(id.as_str()));
        if !demand.is_empty() && in_flight {
            effects.push(Effect::HostRequest {
                tag: "explanation-release".into(),
                body: json!({"session": self.remote_session, "detail_level": self.detail_level, "items": [], "release": [demand]}),
            });
        }
        self.queue.retain(|queued| queued.id != id);
        if !self.cache.contains_key(&id) {
            self.requested.remove(&id);
        }
        effects
    }

    /// The debounce fired: send up to eight queued activities for one session.
    pub fn start_batch(&mut self) -> Vec<Effect> {
        self.debounce_active = false;
        let mut effects = Vec::new();
        if !self.enabled || self.queue.is_empty() || !self.error.is_empty() || !self.remote_items.is_empty() {
            return effects;
        }
        let session = json::string(&self.queue[0].activity, "_session");
        if session.is_empty() {
            return self.fail("Select an agent for explanations.");
        }
        self.remote_session = session.clone();
        let remaining = self.queue.len();
        for _ in 0..remaining {
            if self.remote_items.len() >= BATCH_SIZE {
                break;
            }
            let Some(queued) = self.queue.pop_front() else { break };
            if json::string(&queued.activity, "_session") != session {
                self.queue.push_back(queued);
                continue;
            }
            let mut activity = queued.activity;
            activity.remove("_session");
            let mut item = json!({"id": queued.id, "activity": activity});
            if let Some(demand) = self.demand_ids.get(&queued.id) {
                item["demand_id"] = json!(demand);
            }
            self.remote_items.push(item);
        }
        self.remote_generation += 1;
        self.remote_tag = format!("tool-explanations:{}", self.remote_generation);
        effects.push(Effect::Timeout { generation: self.remote_generation });
        effects.extend(self.poll());
        effects
    }

    pub fn poll(&mut self) -> Vec<Effect> {
        if !self.enabled || self.remote_items.is_empty() {
            return Vec::new();
        }
        vec![Effect::HostRequest {
            tag: self.remote_tag.clone(),
            body: json!({"session": self.remote_session, "detail_level": self.detail_level, "items": self.remote_items}),
        }]
    }

    pub fn poll_for(&mut self, tag: &str) -> Vec<Effect> {
        if tag != self.remote_tag { Vec::new() } else { self.poll() }
    }

    pub fn timed_out(&mut self, generation: u64) -> Vec<Effect> {
        if generation != self.remote_generation || self.remote_items.is_empty() {
            return Vec::new();
        }
        self.fail("Timed out. Original tool details are still available; toggle off/on to retry.")
    }

    pub fn is_own_tag(&self, tag: &str) -> bool {
        !tag.is_empty() && tag == self.remote_tag
    }

    fn cache_insert(&mut self, id: String, text: String) {
        self.cache.insert(id.clone(), text);
        self.cache_order.push_back(id);
        while self.cache_order.len() > MAX_CACHE_ENTRIES {
            if let Some(evicted) = self.cache_order.pop_front() {
                self.cache.remove(&evicted);
                self.requested.remove(&evicted);
            }
        }
    }

    pub fn handle_reply(&mut self, tag: &str, result: &Object) -> Vec<Effect> {
        if !self.is_own_tag(tag) || !self.enabled {
            return Vec::new();
        }
        let rows = json::array(result, "items");
        if rows.len() != self.remote_items.len() {
            return self.fail("Invalid Host explanation response.");
        }
        let mut pending = Vec::new();
        for item in self.remote_items.clone() {
            let id = item.get("id").and_then(Value::as_str).unwrap_or_default().to_owned();
            let demand = item.get("demand_id").and_then(Value::as_str).unwrap_or_default();
            if !demand.is_empty() && self.demand_ids.get(&id).map(String::as_str) != Some(demand) {
                continue;
            }
            let Some(row) = rows.iter().filter_map(Value::as_object).find(|row| json::string(row, "id") == id) else {
                return self.fail("Invalid Host explanation response.");
            };
            match json::string(row, "status").as_str() {
                "ready" => {
                    let text = json::string(row, "text").trim().to_owned();
                    if text.is_empty() || text.chars().count() > 240 {
                        return self.fail("Invalid Host explanation text.");
                    }
                    self.cache_insert(id, text);
                }
                "pending" | "busy" => pending.push(item),
                // Fail this row only: one unavailable explanation must not
                // hide every other row.
                _ => self.cache_insert(id, FAILURE_MARKER.into()),
            }
        }
        let mut effects = Vec::new();
        let done = pending.is_empty();
        self.remote_items = pending;
        if done {
            self.remote_tag.clear();
            self.remote_generation += 1; // cancels the batch timeout
            if !self.queue.is_empty() && !self.debounce_active {
                self.debounce_active = true;
                effects.push(Effect::Debounce);
            }
        } else {
            effects.push(Effect::Poll { tag: self.remote_tag.clone() });
        }
        self.notify(&mut effects);
        effects
    }

    pub fn handle_failure(&mut self, tag: &str, status: u16) -> Vec<Effect> {
        if !self.is_own_tag(tag) {
            return Vec::new();
        }
        if status == 404 {
            self.fail("Update this Host to enable shared explanations.")
        } else {
            self.fail("Host explanation request failed. Toggle off/on to retry.")
        }
    }

    fn stop_remote(&mut self) {
        self.remote_items.clear();
        self.remote_tag.clear();
        self.remote_generation += 1;
    }

    fn fail(&mut self, message: &str) -> Vec<Effect> {
        let mut effects = Vec::new();
        self.error = message.to_owned();
        self.queue.clear();
        self.stop_remote();
        self.notify(&mut effects);
        effects
    }

    fn release_all_views(&mut self) -> Vec<Effect> {
        let owners: Vec<u64> = self.view_keys.keys().copied().collect();
        owners.into_iter().flat_map(|owner| self.release_view(owner)).collect()
    }

    pub fn set_enabled(&mut self, enabled: bool) -> Vec<Effect> {
        self.set_detail_level(if enabled { self.last_translation_level } else { 0 })
    }

    pub fn set_detail_level(&mut self, level: i32) -> Vec<Effect> {
        let level = level.clamp(0, 4);
        if self.detail_level == level {
            return Vec::new();
        }
        let mut effects = self.release_all_views();
        let was_enabled = self.enabled;
        let failed: Vec<String> = self.cache.iter().filter(|(_, t)| *t == FAILURE_MARKER).map(|(k, _)| k.clone()).collect();
        for id in failed {
            self.cache.remove(&id);
            self.cache_order.retain(|k| *k != id);
        }
        // A new audience needs new wording.
        if level > 0 && level != self.last_translation_level {
            self.cache.clear();
            self.cache_order.clear();
        }
        self.detail_level = level;
        self.enabled = level > 0;
        if self.enabled {
            self.last_translation_level = level;
        }
        self.debounce_active = false;
        self.stop_remote();
        self.queue.clear();
        self.requested = self.cache.keys().cloned().collect();
        self.error.clear();
        self.notify(&mut effects);
        effects.push(Effect::DetailLevelChanged);
        if was_enabled != self.enabled {
            effects.push(Effect::EnabledChanged);
        }
        effects
    }

    pub fn reset(&mut self) -> Vec<Effect> {
        let mut effects = self.release_all_views();
        self.debounce_active = false;
        self.stop_remote();
        self.queue.clear();
        self.requested.clear();
        self.cache.clear();
        self.cache_order.clear();
        self.error.clear();
        self.notify(&mut effects);
        effects
    }

    pub fn cache_size(&self) -> usize {
        self.cache.len()
    }
}
