//! The shared audio journal (C++ `AudioCoordinator`): windows on one
//! user/Host/account elect one player through a session-bus name; only that
//! owner writes this journal. Pending clips survive an owner change, while
//! a clip already started is never replayed after its owner crashed. The
//! JSON shape matches the C++ journal.

use serde_json::{Value, json};
use sha2::{Digest, Sha256};

use crate::json::Object;

pub const COMPLETED_LIMIT: usize = 4096;
pub const TICK_MS: u64 = 500;
pub const BUS_PATH: &str = "/com/maxteabag/Clarp/Audio";
pub const BUS_INTERFACE: &str = "com.maxteabag.Clarp.Audio";

fn digest(bytes: &[u8]) -> String {
    Sha256::digest(bytes).iter().map(|b| format!("{b:02x}")).collect()
}

/// The bus name for a Host and account: `scope` is base URL, NUL, token.
pub fn service_name(scope: &str) -> String {
    format!("com.maxteabag.Clarp.Audio.h{}", digest(scope.as_bytes()))
}

pub fn journal_file_name(scope: &str) -> String {
    format!("{}.json", digest(scope.as_bytes()))
}

/// A clip's identity: its id, else a digest of the whole event.
pub fn clip_key(event: &Object) -> String {
    match event.get("clip_id").and_then(Value::as_i64) {
        Some(id) if id > 0 => id.to_string(),
        _ => digest(serde_json::to_string(event).unwrap_or_default().as_bytes()),
    }
}

#[derive(Debug, Clone, Default, PartialEq)]
pub struct Journal {
    root: Object,
}

impl Journal {
    pub fn new(muted: bool) -> Self {
        let mut root = Object::new();
        root.insert("muted".into(), Value::from(muted));
        Self { root }
    }

    /// The journal a new owner starts from: its saved state (an invalid one
    /// is an error, so playback stays stopped), with every clip that had
    /// started moved to completed. Returns the pending events in order.
    pub fn take_over(saved: Option<&str>, muted: bool) -> Result<(Self, Vec<Object>), String> {
        let mut journal = match saved {
            None => Self::new(muted),
            Some(text) => match serde_json::from_str::<Value>(text) {
                Ok(Value::Object(root)) => Self { root },
                _ => return Err("Shared audio state is invalid; playback remains stopped".into()),
            },
        };
        let started: Vec<String> =
            journal.pending().iter().filter(|(_, record)| record.get("started").and_then(Value::as_bool) == Some(true)).map(|(k, _)| k.clone()).collect();
        for key in started {
            journal.finish_key(&key);
        }
        let mut queued: Vec<(i64, Object)> = journal
            .pending()
            .values()
            .filter_map(|record| {
                let sequence = record.get("sequence").and_then(Value::as_i64).unwrap_or(0);
                record.get("event").and_then(Value::as_object).map(|event| (sequence, event.clone()))
            })
            .collect();
        queued.sort_by_key(|(sequence, _)| *sequence);
        Ok((journal, queued.into_iter().map(|(_, event)| event).collect()))
    }

    pub fn to_json(&self) -> String {
        serde_json::to_string(&self.root).unwrap_or_default()
    }

    pub fn muted(&self) -> bool {
        self.root.get("muted").and_then(Value::as_bool).unwrap_or(false)
    }

    pub fn set_muted(&mut self, muted: bool) {
        self.root.insert("muted".into(), Value::from(muted));
    }

    fn pending(&self) -> Object {
        self.root.get("pending").and_then(Value::as_object).cloned().unwrap_or_default()
    }

    fn completed(&self) -> Vec<Value> {
        self.root.get("completed").and_then(Value::as_array).cloned().unwrap_or_default()
    }

    /// Takes offered clips; returns those new to the journal, in order. A
    /// muted journal takes nothing (and reports success).
    pub fn offer(&mut self, events: &[Object]) -> Vec<Object> {
        if self.muted() {
            return Vec::new();
        }
        let mut pending = self.pending();
        let completed = self.completed();
        let mut sequence = self.root.get("sequence").and_then(Value::as_i64).unwrap_or(0);
        let mut added = Vec::new();
        for event in events {
            let key = clip_key(event);
            if pending.contains_key(&key) || completed.iter().any(|k| k.as_str() == Some(key.as_str())) {
                continue;
            }
            sequence += 1;
            pending.insert(key, json!({"event": event, "started": false, "sequence": sequence}));
            added.push(event.clone());
        }
        if !added.is_empty() {
            self.root.insert("sequence".into(), Value::from(sequence));
            self.root.insert("pending".into(), Value::Object(pending));
        }
        added
    }

    /// Marks a pending clip started; false when it is unknown or had started.
    pub fn begin(&mut self, event: &Object) -> bool {
        let key = clip_key(event);
        let mut pending = self.pending();
        let Some(record) = pending.get_mut(&key).and_then(Value::as_object_mut) else { return false };
        if record.get("started").and_then(Value::as_bool) == Some(true) {
            return false;
        }
        record.insert("started".into(), Value::from(true));
        self.root.insert("pending".into(), Value::Object(pending));
        true
    }

    pub fn finish(&mut self, event: &Object) {
        self.finish_key(&clip_key(event));
    }

    fn finish_key(&mut self, key: &str) {
        let mut pending = self.pending();
        pending.remove(key);
        self.root.insert("pending".into(), Value::Object(pending));
        let mut completed = self.completed();
        if !completed.iter().any(|k| k.as_str() == Some(key)) {
            completed.push(Value::from(key));
        }
        let excess = completed.len().saturating_sub(COMPLETED_LIMIT);
        completed.drain(..excess);
        self.root.insert("completed".into(), Value::Array(completed));
    }
}

/// What the owner tells every window (C++ `snapshot`).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct State {
    pub muted: bool,
    pub playing: bool,
    pub paused: bool,
    pub available: bool,
}

impl State {
    pub fn to_json(self) -> String {
        json!({"muted": self.muted, "playing": self.playing, "paused": self.paused, "available": self.available}).to_string()
    }

    pub fn from_json(text: &str) -> Option<Self> {
        let value: Value = serde_json::from_str(text).ok()?;
        let flag = |key: &str| value.get(key).and_then(Value::as_bool).unwrap_or(false);
        value.is_object().then(|| Self { muted: flag("muted"), playing: flag("playing"), paused: flag("paused"), available: flag("available") })
    }
}
