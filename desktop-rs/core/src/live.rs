//! Live items (docs/live-items.md): a conversation's open turn as items
//! pushed as ops. [`LiveView`] is the client reducer, kept in step with the
//! Host's reference reducer (`server/lib/live_items.py`) by the recorded
//! streams in `contract/live/`.

use std::collections::HashMap;

use serde_json::{Value, json};

use crate::json::Object;

/// The effect asking the client to fetch `GET /live?session=`.
pub const FETCH_LIVE: &str = "fetch_live";
/// `tool.output.tail` holds at most this many lines.
pub const OUTPUT_TAIL_LINES: usize = 50;

pub fn is_terminal(status: &str) -> bool {
    matches!(status, "completed" | "failed" | "interrupted")
}

fn idle() -> Object {
    json!({"state": "idle"}).as_object().cloned().unwrap_or_default()
}

/// Fields absent stay, `null` clears, objects merge, other values replace.
pub fn merge_patch(target: &mut Object, patch: &Object) {
    for (key, value) in patch {
        match (value, target.get_mut(key)) {
            (Value::Object(patch), Some(Value::Object(existing))) => merge_patch(existing, patch),
            _ => {
                target.insert(key.clone(), value.clone());
            }
        }
    }
}

fn append_output(item: &mut Object, lines: &[Value], total_lines: i64) {
    let tool = item.entry("tool").or_insert_with(|| json!({}));
    if !tool.is_object() {
        *tool = json!({});
    }
    let tool = tool.as_object_mut().expect("an object");
    let output = tool.entry("output").or_insert(Value::Null);
    if !output.is_object() {
        *output = json!({"tail": [], "total_lines": 0, "truncated": false, "exit_code": null});
    }
    let output = output.as_object_mut().expect("an object");
    let mut tail: Vec<Value> = output.get("tail").and_then(Value::as_array).cloned().unwrap_or_default();
    tail.extend(lines.iter().cloned());
    let keep = tail.len().saturating_sub(OUTPUT_TAIL_LINES);
    tail.drain(..keep);
    let held = tail.len() as i64;
    output.insert("tail".into(), Value::Array(tail));
    output.insert("total_lines".into(), json!(total_lines));
    output.insert("truncated".into(), json!(total_lines > held));
}

/// One conversation's live state, built from snapshots and events.
#[derive(Debug, Clone)]
pub struct LiveView {
    epoch: Option<String>,
    lseq: Option<i64>,
    activity: Object,
    turn: Option<Object>,
    items: HashMap<String, Object>,
    awaiting_snapshot: bool,
    tool_explanations: Option<Object>,
    /// Host clock minus this machine's, from the last `server_now_ms`.
    clock_offset_ms: i64,
    /// Bumped on every applied snapshot or event (what the views compare).
    generation: u64,
}

impl Default for LiveView {
    fn default() -> Self {
        Self {
            epoch: None,
            lseq: None,
            activity: idle(),
            turn: None,
            items: HashMap::new(),
            awaiting_snapshot: false,
            tool_explanations: None,
            clock_offset_ms: 0,
            generation: 0,
        }
    }
}

impl LiveView {
    pub fn new() -> Self {
        Self::default()
    }
    pub fn epoch(&self) -> Option<&str> {
        self.epoch.as_deref()
    }
    pub fn lseq(&self) -> Option<i64> {
        self.lseq
    }
    pub fn activity(&self) -> &Object {
        &self.activity
    }
    pub fn turn(&self) -> Option<&Object> {
        self.turn.as_ref()
    }
    /// A `GET /live` is outstanding: events are ignored until it lands.
    pub fn awaiting_snapshot(&self) -> bool {
        self.awaiting_snapshot
    }
    /// The Host's tool-explanation setting, as the last snapshot said.
    pub fn tool_explanations(&self) -> Option<&Object> {
        self.tool_explanations.as_ref()
    }
    pub fn generation(&self) -> u64 {
        self.generation
    }
    /// Every item, sorted by `ordinal` (ties by id, so the order is stable).
    pub fn items(&self) -> Vec<&Object> {
        let mut items: Vec<&Object> = self.items.values().collect();
        items.sort_by(|a, b| {
            let ordinal = |i: &Object| i.get("ordinal").and_then(Value::as_i64).unwrap_or(0);
            let id = |i: &Object| i.get("id").and_then(Value::as_str).unwrap_or_default().to_owned();
            ordinal(a).cmp(&ordinal(b)).then_with(|| id(a).cmp(&id(b)))
        });
        items
    }
    pub fn item(&self, id: &str) -> Option<&Object> {
        self.items.get(id)
    }

    /// Host time now, by this machine's clock corrected with the last
    /// `server_now_ms` (what a running item's elapsed time ticks from).
    pub fn host_now_ms(&self, local_now_ms: i64) -> i64 {
        local_now_ms + self.clock_offset_ms
    }
    /// Records the Host clock an event or snapshot carried.
    pub fn observe_clock(&mut self, server_now_ms: i64, local_now_ms: i64) {
        if server_now_ms > 0 {
            self.clock_offset_ms = server_now_ms - local_now_ms;
        }
    }

    /// The client asked for `GET /live` on its own (opening a chat): events
    /// wait for it as they do after a gap.
    pub fn expect_snapshot(&mut self) {
        self.awaiting_snapshot = true;
    }
    /// The fetch failed: the next event may ask again.
    pub fn snapshot_failed(&mut self) {
        self.awaiting_snapshot = false;
    }

    /// Replaces the state with a `GET /live` answer and holds its lseq.
    pub fn apply_snapshot(&mut self, snapshot: &Object) {
        self.epoch = snapshot.get("epoch").and_then(Value::as_str).map(str::to_owned);
        self.lseq = snapshot.get("lseq").and_then(Value::as_i64);
        self.activity = snapshot.get("activity").and_then(Value::as_object).cloned().unwrap_or_else(idle);
        self.turn = snapshot.get("turn").and_then(Value::as_object).cloned();
        self.items = snapshot
            .get("items")
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
            .filter_map(Value::as_object)
            .filter_map(|item| Some((item.get("id")?.as_str()?.to_owned(), item.clone())))
            .collect();
        if let Some(settings) = snapshot.get("tool_explanations").and_then(Value::as_object) {
            self.tool_explanations = Some(settings.clone());
        }
        self.awaiting_snapshot = false;
        self.generation += 1;
    }

    /// Applies one `live` event by §3's rules; returns the effects the
    /// client must run ([`FETCH_LIVE`]). An event is applied whole or not
    /// at all.
    pub fn apply_event(&mut self, event: &Object) -> Vec<&'static str> {
        if self.awaiting_snapshot {
            return Vec::new();
        }
        let lseq = event.get("lseq").and_then(Value::as_i64).unwrap_or(0);
        let Some(held) = self.lseq else { return self.need_snapshot() };
        if event.get("epoch").and_then(Value::as_str) != self.epoch.as_deref() {
            return self.need_snapshot();
        }
        if lseq <= held {
            return Vec::new();
        }
        if lseq != held + 1 {
            return self.need_snapshot();
        }
        let mut staged = (self.activity.clone(), self.turn.clone(), self.items.clone());
        for op in event.get("ops").and_then(Value::as_array).into_iter().flatten() {
            let Some(op) = op.as_object() else { continue };
            if !apply_op(&mut staged, op) {
                return self.need_snapshot();
            }
        }
        (self.activity, self.turn, self.items) = staged;
        self.lseq = Some(lseq);
        self.generation += 1;
        Vec::new()
    }

    fn need_snapshot(&mut self) -> Vec<&'static str> {
        self.awaiting_snapshot = true;
        vec![FETCH_LIVE]
    }
}

type Staged = (Object, Option<Object>, HashMap<String, Object>);

/// One op; false when it shows something was missed (a revision out of
/// step, a patch for an unknown item).
fn apply_op((activity, turn, items): &mut Staged, op: &Object) -> bool {
    let name = op.get("op").and_then(Value::as_str).unwrap_or_default();
    match name {
        "status" => {
            *activity = op.get("activity").and_then(Value::as_object).cloned().unwrap_or_else(idle);
            return true;
        }
        "turn" => {
            let next = op.get("turn").and_then(Value::as_object).cloned();
            let id = |t: &Object| t.get("turn_id").cloned();
            // A new turn: the last one's items now live in /log.
            if let (Some(held), Some(new)) = (turn.as_ref(), next.as_ref())
                && id(held) != id(new)
            {
                items.clear();
            }
            *turn = next;
            return true;
        }
        "upsert" | "append" | "done" => {}
        // Unknown ops are ignored.
        _ => return true,
    }
    let Some(id) = op.get("id").and_then(Value::as_str) else { return true };
    let rev = op.get("rev").and_then(Value::as_i64);
    let item = match items.get_mut(id) {
        Some(item) => {
            let held = item.get("rev").and_then(Value::as_i64).unwrap_or(0);
            if rev != Some(held + 1) {
                return false;
            }
            item
        }
        None if name != "upsert" => return false,
        None => {
            let kind = op.get("kind").cloned().unwrap_or(Value::Null);
            items.insert(id.to_owned(), json!({"id": id, "kind": kind, "rev": 0}).as_object().cloned().unwrap_or_default());
            items.get_mut(id).expect("just inserted")
        }
    };
    let before = item.get("status").cloned().unwrap_or(Value::Null);
    let settled = is_terminal(before.as_str().unwrap_or_default());
    let patch = op.get("item").and_then(Value::as_object);
    match name {
        "upsert" => {
            if let Some(patch) = patch {
                merge_patch(item, patch);
            }
        }
        "append" => match op.get("field").and_then(Value::as_str) {
            Some("text") => {
                let mut text = item.get("text").and_then(Value::as_str).unwrap_or_default().to_owned();
                text.push_str(op.get("chunk").and_then(Value::as_str).unwrap_or_default());
                item.insert("text".into(), json!(text));
            }
            Some("tool.output") => {
                let lines = op.get("lines").and_then(Value::as_array).cloned().unwrap_or_default();
                append_output(item, &lines, op.get("total_lines").and_then(Value::as_i64).unwrap_or(0));
            }
            _ => {}
        },
        _ => {
            item.insert("status".into(), op.get("status").cloned().unwrap_or(Value::Null));
            for key in ["started_at_ms", "ended_at_ms"] {
                if let Some(value) = op.get(key) {
                    item.insert(key.into(), value.clone());
                }
            }
            if let Some(patch) = patch {
                merge_patch(item, patch);
            }
        }
    }
    // Terminal statuses are final: nothing moves a settled item back.
    if settled && !is_terminal(item.get("status").and_then(Value::as_str).unwrap_or_default()) {
        item.insert("status".into(), before);
    }
    item.insert("rev".into(), json!(rev.unwrap_or_else(|| item.get("rev").and_then(Value::as_i64).unwrap_or(0) + 1)));
    true
}
