//! The conversation sync reducer from `docs/protocol.md`, ported from the
//! shared `static/lib/conversation-sync.js` so the golden fixtures in
//! `contract/fixtures` pin this client exactly as they pin the PWA:
//!
//!   open chat      → on_open() → fetch_tail (when nothing cached)
//!   snapshot row   → apply_snapshot() → fetch_delta when behind
//!   /log response  → apply_log() → upsert by id; replace_required or a new
//!                    conversation_id means drop_cache + fetch_tail
//!   SSE wake-up    → on_event() → fetch_delta (coalesced while fetching)
//!   roster reset   → on_event() → drop_cache + fetch_tail
//!
//! Unknown event types and unknown fields are ignored (additive-only policy).

use std::collections::{HashMap, HashSet};

use serde_json::Value;

use crate::json::{js_number, js_string, truthy};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Effect {
    FetchTail,
    FetchDelta,
    FetchOlder,
    DropCache,
}

impl Effect {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::FetchTail => "fetch_tail",
            Self::FetchDelta => "fetch_delta",
            Self::FetchOlder => "fetch_older",
            Self::DropCache => "drop_cache",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum LogMode {
    Tail,
    Delta,
    Older,
}

impl LogMode {
    pub fn parse(mode: &str) -> Self {
        match mode {
            "tail" => Self::Tail,
            "older" => Self::Older,
            _ => Self::Delta,
        }
    }
}

#[derive(Debug, Clone, Default, PartialEq)]
pub struct SyncState {
    pub session: String,
    pub conversation_id: String,
    pub cursor: f64,
    pub turns: HashMap<String, Value>,
    pub order: Vec<String>,
    pub has_more: bool,
    pub missing: bool,
    pub latest_ts: String,
    pub loaded: bool,
    pub pending_fetch: Option<String>,
    pub wake_while_fetching: bool,
}

fn turn_id(turn: &Value) -> Option<String> {
    let id = turn.get("id");
    truthy(id).then(|| js_string(id))
}

fn turns_of(response: &Value) -> Vec<Value> {
    response.get("turns").and_then(Value::as_array).cloned().unwrap_or_default()
}

impl SyncState {
    pub fn new(session: &str) -> Self {
        Self { session: session.to_owned(), ..Self::default() }
    }

    /// Drop the cache. A request already in flight stays in flight.
    fn reset(&mut self) {
        let mut next = Self::new(&self.session);
        next.pending_fetch = self.pending_fetch.take();
        next.wake_while_fetching = self.wake_while_fetching;
        *self = next;
    }

    /// Ask for a delta. While a fetch is in flight the wake-up is remembered
    /// and end_fetch() turns it into exactly one follow-up.
    pub fn request_delta(&mut self) -> Vec<Effect> {
        if self.pending_fetch.is_some() {
            self.wake_while_fetching = true;
            return Vec::new();
        }
        vec![Effect::FetchDelta]
    }

    pub fn on_open(&self) -> Vec<Effect> {
        if self.loaded { Vec::new() } else { vec![Effect::FetchTail] }
    }

    /// Snapshot rows tell us which caches are behind or belong to a dead
    /// conversation. No cache yet → no opinion; the open-chat path fetches.
    pub fn apply_snapshot(&mut self, row: &Value) -> Vec<Effect> {
        if !row.is_object() {
            return Vec::new();
        }
        let conversation_id = js_string(row.get("conversation_id"));
        let head = js_number(row.get("head_revision"));
        if self.loaded
            && !conversation_id.is_empty()
            && !self.conversation_id.is_empty()
            && conversation_id != self.conversation_id
        {
            self.reset();
            return vec![Effect::DropCache, Effect::FetchTail];
        }
        if self.loaded && head > self.cursor {
            return self.request_delta();
        }
        Vec::new()
    }

    /// Merge one /log response. Identity is the turn id; a lower revision
    /// never overwrites a newer row.
    pub fn apply_log(&mut self, response: &Value, mode: LogMode) -> Vec<Effect> {
        let empty = Value::Object(Default::default());
        let d = if response.is_object() { response } else { &empty };
        match mode {
            LogMode::Tail => {
                self.reset();
                self.loaded = true;
                self.conversation_id = js_string(d.get("conversation_id"));
                self.cursor = js_number(d.get("latest_revision"));
                self.has_more = truthy(d.get("has_more"));
                self.missing = truthy(d.get("missing"));
                self.latest_ts = js_string(d.get("latest_ts"));
                for turn in turns_of(d) {
                    if let Some(id) = turn_id(&turn) {
                        self.turns.insert(id.clone(), turn);
                        self.order.push(id);
                    }
                }
                Vec::new()
            }
            LogMode::Older => {
                let mut known: HashSet<String> = self.order.iter().cloned().collect();
                let mut older = Vec::new();
                for turn in turns_of(d) {
                    if let Some(id) = turn_id(&turn).filter(|id| !known.contains(id)) {
                        known.insert(id.clone());
                        older.push(id.clone());
                        self.turns.insert(id, turn);
                    }
                }
                older.append(&mut self.order);
                self.order = older;
                self.has_more = truthy(d.get("has_more"));
                Vec::new()
            }
            LogMode::Delta => {
                let incoming_id = js_string(d.get("conversation_id"));
                if truthy(d.get("replace_required"))
                    || (!self.conversation_id.is_empty()
                        && !incoming_id.is_empty()
                        && incoming_id != self.conversation_id)
                {
                    self.reset();
                    return vec![Effect::DropCache, Effect::FetchTail];
                }
                for turn in turns_of(d) {
                    let Some(id) = turn_id(&turn) else { continue };
                    match self.turns.get(&id) {
                        None => {
                            self.turns.insert(id.clone(), turn);
                            self.order.push(id);
                        }
                        Some(previous)
                            if js_number(turn.get("revision"))
                                >= js_number(previous.get("revision")) =>
                        {
                            self.turns.insert(id, turn);
                        }
                        Some(_) => {}
                    }
                }
                // A delta's has_more says newer changes remain; `has_more`
                // here is older history, which a delta never changes.
                let newer = truthy(d.get("has_more"));
                self.cursor = self.cursor.max(js_number(d.get("latest_revision")));
                if !incoming_id.is_empty() {
                    self.conversation_id = incoming_id;
                }
                if truthy(d.get("latest_ts")) {
                    self.latest_ts = js_string(d.get("latest_ts"));
                }
                self.missing = truthy(d.get("missing")) && self.order.is_empty();
                self.loaded = true;
                if newer { vec![Effect::FetchDelta] } else { Vec::new() }
            }
        }
    }

    /// SSE wake-ups. transcript-updated coalesces while a fetch is in flight;
    /// created/relaunched/forked for our session means a new conversation.
    /// Anything else — other sessions, focus, unknown types — is ignored.
    pub fn on_event(&mut self, event: &Value) -> Vec<Effect> {
        let ours = js_string(event.get("session")) == self.session;
        match js_string(event.get("type")).as_str() {
            "transcript-updated" if ours => self.request_delta(),
            "agent-roster" if ours => match js_string(event.get("kind")).as_str() {
                "created" | "relaunched" | "forked" => {
                    self.reset();
                    vec![Effect::DropCache, Effect::FetchTail]
                }
                "deleted" => {
                    self.reset();
                    vec![Effect::DropCache]
                }
                _ => Vec::new(),
            },
            _ => Vec::new(),
        }
    }

    /// One request per session at a time; a wake-up mid-flight runs once more.
    pub fn begin_fetch(&mut self, kind: &str) {
        self.pending_fetch = Some(if kind.is_empty() { "delta".into() } else { kind.to_owned() });
        self.wake_while_fetching = false;
    }

    pub fn end_fetch(&mut self) -> Vec<Effect> {
        let woken = self.wake_while_fetching;
        self.pending_fetch = None;
        self.wake_while_fetching = false;
        if woken { vec![Effect::FetchDelta] } else { Vec::new() }
    }

    pub fn current_turns(&self) -> Vec<&Value> {
        self.order.iter().filter_map(|id| self.turns.get(id)).collect()
    }

    /// What the transcript shows: the server's turns in order, then the
    /// optimistic user bubbles the server has not filed yet (identity by id).
    pub fn visible_turns(&self, optimistic: &[Value]) -> Vec<Value> {
        let known: HashSet<&String> = self.order.iter().collect();
        let mut turns: Vec<Value> = self.current_turns().into_iter().cloned().collect();
        turns.extend(
            optimistic
                .iter()
                .filter(|t| turn_id(t).is_some_and(|id| !known.contains(&id)))
                .cloned(),
        );
        turns
    }
}

/// Clip URL precedence: playlist_url beats stream_url beats url.
pub fn pick_clip_source(event: &Value) -> Option<&'static str> {
    if truthy(event.get("playlist_url")) {
        Some("playlist")
    } else if truthy(event.get("stream_url")) {
        Some("stream")
    } else if truthy(event.get("url")) {
        Some("file")
    } else {
        None
    }
}

/// A clip already acked must not replay after reconnect.
pub fn is_clip_replay(clip_id: Option<&Value>, seen: Option<&Value>) -> bool {
    match (clip_id, seen.and_then(Value::as_array)) {
        (Some(id), Some(seen)) if !id.is_null() => seen.contains(id),
        _ => false,
    }
}
