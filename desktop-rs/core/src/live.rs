//! Live items (docs/live-items.md): a conversation's open turn as items
//! pushed as ops. [`LiveView`] is the client reducer, kept in step with the
//! Host's reference reducer (`server/lib/live_items.py`) by the recorded
//! streams in `contract/live/`.

use std::collections::HashMap;

use crate::json::Object;

/// The effect asking the client to fetch `GET /live?session=`.
pub const FETCH_LIVE: &str = "fetch_live";

#[derive(Debug, Clone, Default)]
pub struct LiveView {
    epoch: Option<String>,
    lseq: Option<i64>,
    activity: Object,
    turn: Option<Object>,
    items: HashMap<String, Object>,
    awaiting_snapshot: bool,
}

impl LiveView {
    pub fn new() -> Self {
        Self::default()
    }
    pub fn lseq(&self) -> Option<i64> {
        None
    }
    pub fn activity(&self) -> &Object {
        &self.activity
    }
    pub fn turn(&self) -> Option<&Object> {
        None
    }
    pub fn awaiting_snapshot(&self) -> bool {
        false
    }
    pub fn items(&self) -> Vec<&Object> {
        Vec::new()
    }
    pub fn item(&self, _id: &str) -> Option<&Object> {
        None
    }
    pub fn apply_snapshot(&mut self, _snapshot: &Object) {}
    pub fn apply_event(&mut self, _event: &Object) -> Vec<&'static str> {
        Vec::new()
    }
}
