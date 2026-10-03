//! How the open turn's live items read (docs/live-items.md §7.3–7.6): one
//! status line, item rows in ordinal order with their labels, explore
//! groups as one row, the fold of a settled turn, and durable `/log` rows
//! taking over items.

use std::collections::HashSet;

use clarp_core::live::LiveView;
use clarp_core::live_present::{Kind, Options, Presented, present, status_line};
use clarp_core::protocol::Message;
use serde_json::{Value, json};

fn fixture(name: &str) -> Value {
    let path = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join(format!("../../contract/live/{name}.json"));
    serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap()
}

/// The turn-full stream up to (not including) step `through`.
fn turn_full(through: usize) -> LiveView {
    let body = fixture("turn-full");
    let mut view = LiveView::new();
    for step in body["steps"].as_array().unwrap().iter().take(through) {
        if let Some(snapshot) = step.get("snapshot") {
            view.apply_snapshot(snapshot.as_object().unwrap());
        } else {
            view.apply_event(step["event"].as_object().unwrap());
        }
    }
    view
}

/// The step index after the event with this lseq.
fn after(lseq: i64) -> usize {
    let body = fixture("turn-full");
    body["steps"].as_array().unwrap().iter().position(|s| s["event"]["lseq"] == lseq).unwrap() + 1
}

fn row(value: Value) -> Message {
    Message::from_json(value.as_object().unwrap())
}

fn options(expanded: &[&str], host_now_ms: i64) -> Options {
    Options { explanations: true, expanded: expanded.iter().map(|s| (*s).to_owned()).collect::<HashSet<_>>(), host_now_ms }
}

fn titles(presented: &Presented) -> Vec<String> {
    presented.entries.iter().map(|e| e.title.clone()).collect()
}

#[test]
fn the_status_line_names_the_tool_with_its_elapsed_time_from_the_host_clock() {
    let view = turn_full(after(10));
    // Grep started at …4750, Read at …4700 and is still running: +1.
    let line = status_line(&view, 1759480004750 + 12_000).expect("busy");
    assert_eq!(line.text, "● Searching tokenize · 0:12 +1");
    assert!(line.busy);
    let line = status_line(&turn_full(after(4)), 1759480000000 + 3_400).unwrap();
    assert_eq!(line.text, "◌ Thinking: Finding the flaky test · 0:03", "thinking ticks from turn_started_ms");
    assert_eq!(status_line(&turn_full(after(6)), 1759480004300).unwrap().text, "◌ Responding · 0:04");
    let ended = status_line(&turn_full(after(23)), 1759480099999).unwrap();
    assert_eq!(ended.text, "■ Interrupted");
    assert!(!ended.busy);
    assert!(status_line(&turn_full(1), 0).is_none(), "nothing when idle");
}

#[test]
fn items_read_as_rows_in_ordinal_order() {
    let view = turn_full(after(17));
    let p = present(&view, &[], &options(&[], 1759480008200));
    assert_eq!(
        titles(&p),
        ["Thought for 4s: Finding the flaky test", "", "Explored 1 file, 1 search", "Ran npm test"],
    );
    let kinds: Vec<Kind> = p.entries.iter().map(|e| e.kind).collect();
    assert_eq!(kinds, [Kind::Reasoning, Kind::Message, Kind::Explore, Kind::Tool]);
    let message = &p.entries[1];
    assert_eq!(message.text, "Let me look at the parser and its tests.");
    assert_eq!(message.key, "live:cl:msg_01:1");
    let tool = &p.entries[3];
    assert_eq!(tool.status, "failed");
    assert_eq!(tool.meta, "exit 1 · 3.2s");
    assert_eq!(tool.secondary, "Runs the parser tests", "the explanation under the label");
    assert_eq!(tool.lines, ["line 809", "line 810", "line 811", "line 812"], "a failed command keeps its tail");
    assert_eq!(tool.more, "+808 lines");
    assert!(tool.expandable && !tool.expanded);
    let open = present(&view, &[], &options(&["live:cl:toolu_03"], 1759480008200));
    let tool = &open.entries[3];
    assert_eq!(tool.lines.len(), 50, "expanded: the whole tail the Host keeps");
    assert_eq!(tool.more, "+762 earlier lines");
}

#[test]
fn running_rows_tick_and_settle_in_place() {
    let view = turn_full(after(9));
    let p = present(&view, &[], &options(&[], 1759480004700 + 1_000));
    let explore = p.entries.last().unwrap();
    assert_eq!(explore.title, "Exploring");
    assert_eq!(explore.key, "live:explore:cl:toolu_01", "the group's id is its first member's");
    let view = turn_full(after(15));
    let p = present(&view, &[], &options(&[], 1759480005000 + 7_000));
    let tool = p.entries.last().unwrap();
    assert_eq!(tool.title, "Running npm test");
    assert_eq!(tool.meta, "0:07");
    assert_eq!(tool.lines, ["line 27", "line 28", "line 29", "line 30"]);
    assert_eq!(tool.more, "+26 lines");
    let reasoning = present(&turn_full(after(3)), &[], &options(&[], 1759480000600)).entries[0].clone();
    assert_eq!(reasoning.title, "Thinking");
    assert_eq!(present(&turn_full(after(4)), &[], &options(&[], 0)).entries[0].title, "Thinking: Finding the flaky test");
}

#[test]
fn an_edit_shows_its_diff_stats_and_preview_when_open() {
    let view = turn_full(after(19));
    let p = present(&view, &[], &options(&["live:cl:toolu_04"], 1759480008400));
    let edit = p.entries.iter().find(|e| e.key == "live:cl:toolu_04").unwrap();
    assert_eq!(edit.title, "Edited src/tokenizer.ts");
    assert_eq!(edit.meta, "+3 −1");
    assert_eq!(edit.lines[0], "@@ -40,3 +40,5 @@");
    assert_eq!(edit.lines.len(), 6);
}

#[test]
fn without_explanations_the_second_line_is_the_command() {
    let view = turn_full(after(17));
    let mut opts = options(&[], 1759480008200);
    opts.explanations = false;
    let p = present(&view, &[], &opts);
    assert_eq!(p.entries[3].secondary, "npm test -- parser");
    // On but not here yet: an empty line is reserved so it lands without a jump.
    let early = present(&turn_full(after(13)), &[], &options(&[], 1759480005000));
    let tool = early.entries.last().unwrap();
    assert_eq!(tool.secondary, "");
    assert!(tool.reserve_secondary);
}

#[test]
fn a_settled_turn_folds_its_work_and_keeps_what_failed_or_stopped_in_view() {
    let view = turn_full(after(23));
    let p = present(&view, &[], &options(&[], 1759480012000));
    assert_eq!(titles(&p), ["Worked for 12s · 5 tools", "Ran npm test", "", "Stopped npm test"]);
    assert_eq!(p.entries[0].kind, Kind::Fold);
    assert_eq!(p.entries[0].key, "live:fold:tr-1");
    assert_eq!(p.entries[0].items, ["cl:msg_01:0", "cl:msg_01:1", "cl:toolu_01", "cl:toolu_02", "cl:toolu_04"]);
    assert_eq!(p.entries[2].text, "The failure came from an off-by-one ");
    assert_eq!(p.entries[2].status, "interrupted");
    assert_eq!(p.entries[3].meta, "interrupted");
    let open = present(&view, &[], &options(&["live:fold:tr-1"], 1759480012000));
    assert_eq!(open.entries.len(), 1 + 7, "the fold opened shows every row again");
    assert!(open.entries[0].expanded);
}

#[test]
fn durable_rows_take_over_their_items() {
    let view = turn_full(after(17));
    // The streaming row the Host keeps in /log is the items' row: hidden.
    let live_row = row(json!({"id": "live-abc", "role": "assistant", "kind": "live", "text": "Let me look", "revision": 5}));
    let p = present(&view, &[live_row], &options(&[], 0));
    assert_eq!(p.hidden_rows, ["live-abc"]);
    assert_eq!(p.entries.len(), 4);
    // The durable row with the same id and the tools takes the items' place.
    let durable = row(json!({"id": "live-abc", "role": "assistant", "text": "Let me look at the parser and its tests.", "revision": 6,
        "tools": [{"id": "toolu_01", "name": "Read"}, {"id": "toolu_02", "name": "Grep"}]}));
    let p = present(&view, &[durable], &options(&[], 0));
    assert!(p.hidden_rows.is_empty());
    assert_eq!(titles(&p), ["Thought for 4s: Finding the flaky test", "Ran npm test"]);
    assert_eq!(p.taken_over.get("cl:msg_01:1").map(String::as_str), Some("live-abc"));
    assert_eq!(p.taken_over.get("cl:toolu_02").map(String::as_str), Some("live-abc"));
    // A display cell's id takes over a tool too.
    let cells = row(json!({"id": "d9", "role": "assistant", "text": "", "revision": 7, "display_cells": [{"id": "toolu_03", "kind": "command"}]}));
    let p = present(&view, &[cells], &options(&[], 0));
    assert_eq!(p.taken_over.get("cl:toolu_03").map(String::as_str), Some("d9"));
}

#[test]
fn unknown_kinds_are_not_shown_and_only_the_open_turn_is() {
    let mut view = turn_full(after(5));
    let event = json!({"type": "live", "conv": "conv-1", "epoch": "boot-a", "lseq": 6, "ops": [
        {"op": "upsert", "conv": "conv-1", "id": "x:1", "kind": "hologram", "rev": 1, "item": {"id": "x:1", "turn_id": "tr-1", "ordinal": 2, "status": "running"}},
        {"op": "upsert", "conv": "conv-1", "id": "old:1", "kind": "tool", "rev": 1, "item": {"id": "old:1", "turn_id": "tr-0", "ordinal": 0, "status": "completed",
            "tool": {"name": "Bash", "call_id": "old", "category": "exec", "label": "ls"}}},
    ]});
    view.apply_event(event.as_object().unwrap());
    let p = present(&view, &[], &options(&[], 0));
    assert_eq!(p.entries.len(), 1, "{:?}", titles(&p));
}
