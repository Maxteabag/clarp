//! Rows carried in `transcript-updated` (feature `events_rows`): which
//! events the desktop may apply in place of a `/log` read, and that applying
//! them draws the same chat as the read.

use clarp_core::conversation::{Conversation, LoadKind};
use clarp_core::events_rows::as_delta;
use serde_json::{Value, json};

fn object(value: Value) -> serde_json::Map<String, Value> {
    value.as_object().cloned().expect("object")
}

/// The Host's rows after each write, the way both reads see them.
struct Host {
    rows: Vec<Value>,
    revision: i64,
    told: i64,
}

impl Host {
    fn new() -> Self {
        Host { rows: Vec::new(), revision: 0, told: 0 }
    }

    fn write(&mut self, row: Value) {
        self.revision += 1;
        let mut row = row;
        row["revision"] = json!(self.revision);
        let id = row["id"].clone();
        match self.rows.iter_mut().find(|r| r["id"] == id) {
            Some(held) => *held = row,
            None => self.rows.push(row),
        }
    }

    fn after(&self, revision: i64) -> Vec<Value> {
        self.rows.iter().filter(|r| r["revision"].as_i64().unwrap_or(0) > revision).cloned().collect()
    }

    /// GET /log?after_revision= as the desktop reads it (every tool's details).
    fn log(&self, after: i64) -> Value {
        json!({"conversation_id": "c1", "turns": self.after(after), "latest_revision": self.revision, "has_more": false, "replace_required": false})
    }

    /// The transcript-updated a rows=1 stream gets: the rows since the last
    /// event, shortened as include_tool_details=0 shortens them.
    fn event(&mut self) -> Value {
        let from = std::mem::replace(&mut self.told, self.revision);
        let rows: Vec<Value> = self.after(from).into_iter().map(shortened).collect();
        json!({"type": "transcript-updated", "session": "s", "conversation_id": "c1", "head_revision": self.revision,
               "rows_from": from, "rows": rows, "rows_latest": self.revision})
    }
}

fn shortened(mut row: Value) -> Value {
    let tools = row["tools"].as_array().cloned().unwrap_or_default();
    let cells = row["display_cells"].as_array().cloned().unwrap_or_default();
    row["tool_details_available"] = json!(!tools.is_empty() || !cells.is_empty());
    row["activity_count"] = json!(if cells.is_empty() { tools.len() } else { cells.len() });
    row["tools"] = json!(tools.first().map(|t| vec![json!({"id": t["id"], "name": t["name"]})]).unwrap_or_default());
    row["display_cells"] = json!(cells.first().map(|c| vec![json!({"id": c["id"], "kind": c["kind"]})]).unwrap_or_default());
    row
}

fn opened(host: &Host) -> Conversation {
    let mut chat = Conversation::new();
    chat.open_session("s");
    chat.apply_log(&object(host.log(0)), LoadKind::Tail);
    chat
}

#[test]
fn rows_applied_in_place_of_reads_draw_the_same_chat() {
    let mut host = Host::new();
    host.write(json!({"id": "u1", "role": "user", "text": "Hello"}));
    let mut by_log = opened(&host);
    let mut by_rows = opened(&host);
    host.event();
    let writes = vec![
        json!({"id": "u2", "role": "user", "origin": "user", "text": "Fix the parser"}),
        json!({"id": "live", "role": "assistant", "kind": "live", "text": "Let me"}),
        json!({"id": "live", "role": "assistant", "kind": "live", "text": "Let me look"}),
        json!({"id": "c1", "role": "assistant", "phase": "commentary", "text": "Testing", "tools": [{"id": "t1", "name": "Bash", "input": {"command": "cargo test"}}]}),
        json!({"id": "hb", "role": "user", "origin": "heartbeat", "automated": true, "text": "heartbeat",
               "tools": [{"id": "t2", "name": "Bash"}]}),
        json!({"id": "mw", "role": "user", "origin": "made-way", "text": "Take up the turn again"}),
        json!({"id": "a1", "role": "assistant", "phase": "final", "text": "Fixed."}),
        json!({"id": "a1", "role": "assistant", "phase": "final", "text": "Fixed, and the tests pass."}),
        json!({"id": "u2", "role": "user", "origin": "user", "text": "Fix the parser", "delivery": {"state": "delivered", "for": {"client_msg_id": "2"}}}),
    ];
    let mut carried = 0;
    for (step, row) in writes.into_iter().enumerate() {
        let held = by_log.latest_revision();
        host.write(row);
        by_log.apply_log(&object(host.log(held)), LoadKind::Delta);
        let event = object(host.event());
        match as_delta(&event, by_rows.conversation_id(), by_rows.latest_revision()) {
            Some(delta) => {
                carried += 1;
                by_rows.apply_log(&delta, LoadKind::Delta);
            }
            None => by_rows.apply_log(&object(host.log(by_rows.latest_revision())), LoadKind::Delta),
        }
        assert_eq!(by_rows.rows(), by_log.rows(), "step {step}");
        assert_eq!(by_rows.latest_revision(), by_log.latest_revision(), "step {step}");
    }
    // Everything but the tool row came in the event.
    assert_eq!(carried, 8);
}

#[test]
fn a_row_with_tools_is_read_whole() {
    let event = object(json!({"conversation_id": "c1", "rows_from": 4, "rows_latest": 5,
        "rows": [{"id": "c1", "role": "assistant", "tools": [{"id": "t1", "name": "Bash"}], "tool_details_available": true}]}));
    assert!(as_delta(&event, "c1", 4).is_none());
    let cells = object(json!({"conversation_id": "c1", "rows_from": 4, "rows_latest": 5,
        "rows": [{"id": "c1", "role": "assistant", "tools": [], "display_cells": [{"id": "x", "kind": "patch"}]}]}));
    assert!(as_delta(&cells, "c1", 4).is_none());
}

#[test]
fn an_automated_row_may_carry_tools_since_it_is_never_drawn() {
    let event = object(json!({"conversation_id": "c1", "rows_from": 4, "rows_latest": 5,
        "rows": [{"id": "hb", "role": "assistant", "automated": true, "tools": [{"id": "t1"}], "tool_details_available": true}]}));
    assert!(as_delta(&event, "c1", 4).is_some());
}

#[test]
fn a_gap_another_conversation_or_no_rows_reads_the_log() {
    let event = |extra: Value| {
        let mut base = json!({"conversation_id": "c1", "rows_from": 4, "rows_latest": 6, "rows": [{"id": "a", "role": "assistant", "text": "x"}]});
        for (key, value) in extra.as_object().unwrap() {
            if value.is_null() {
                base.as_object_mut().unwrap().remove(key);
            } else {
                base[key] = value.clone();
            }
        }
        object(base)
    };
    assert!(as_delta(&event(json!({})), "c1", 4).is_some(), "held at rows_from");
    assert!(as_delta(&event(json!({})), "c1", 5).is_some(), "held past rows_from");
    assert!(as_delta(&event(json!({})), "c1", 3).is_none(), "a missed event");
    assert!(as_delta(&event(json!({})), "c2", 4).is_none(), "another conversation held");
    assert!(as_delta(&event(json!({})), "", 0).is_none(), "nothing held yet");
    assert!(as_delta(&event(json!({"rows": null, "rows_latest": null})), "c1", 4).is_none(), "a withheld agent: rows_from alone");
    assert!(as_delta(&event(json!({"rows_from": null})), "c1", 4).is_none(), "the first event after the Host started");
    assert!(as_delta(&event(json!({"rows_from": 0})), "c1", 4).is_none());
}
