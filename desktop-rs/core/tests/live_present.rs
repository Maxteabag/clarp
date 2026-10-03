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
    // The durable row with the message's id shows in the message's place;
    // its tools stay one row per tool (the explore group), not in the row.
    let durable = row(json!({"id": "live-abc", "role": "assistant", "text": "Let me look at the parser and its tests.", "revision": 6,
        "tools": [{"id": "toolu_01", "name": "Read"}, {"id": "toolu_02", "name": "Grep"}]}));
    let p = present(&view, &[durable], &options(&[], 0));
    assert!(p.hidden_rows.is_empty());
    assert_eq!(titles(&p), ["Thought for 4s: Finding the flaky test", "", "Explored 1 file, 1 search", "Ran npm test"]);
    assert_eq!(p.entries[1].row, "live-abc", "the durable row takes the message's place, in order");
    assert!(p.absorbed_rows.is_empty(), "a row with its own text stays: {:?}", p.absorbed_rows);
    assert_eq!(p.stripped_calls, ["toolu_01".to_owned(), "toolu_02".to_owned()].into_iter().collect::<HashSet<_>>(), "the tools the item rows show");
    assert_eq!(p.taken_over.get("cl:msg_01:1").map(String::as_str), Some("live-abc"));
    assert_eq!(p.taken_over.get("cl:toolu_02").map(String::as_str), Some("live-abc"));
    // A display cell's id takes over a tool too; a row that is only that
    // cell is shown by the tool's row.
    let cells = row(json!({"id": "d9", "role": "assistant", "text": "", "revision": 7, "display_cells": [{"id": "toolu_03", "kind": "command"}]}));
    let p = present(&view, &[cells], &options(&[], 0));
    assert_eq!(p.taken_over.get("cl:toolu_03").map(String::as_str), Some("d9"));
    assert_eq!(p.absorbed_rows, ["d9"]);
    assert_eq!(p.entries.last().unwrap().title, "Ran npm test");
}

#[test]
fn a_settled_turn_that_landed_in_the_log_keeps_its_fold() {
    let view = turn_full(after(23));
    let tools: Vec<Value> = ["toolu_01", "toolu_02", "toolu_03", "toolu_04", "toolu_05"].iter().map(|id| json!({"id": id})).collect();
    let durable = row(json!({"id": "live-abc", "role": "assistant", "text": "…", "revision": 9, "tools": tools}));
    let p = present(&view, &[durable], &options(&[], 0));
    assert_eq!(titles(&p), ["Worked for 12s · 5 tools", "Ran npm test", "", "Stopped npm test"]);
    assert_eq!(p.entries[2].row, "live-abc", "the row shows where the answer that stays was");
    assert_eq!(p.entries[0].items, ["cl:msg_01:0", "cl:toolu_01", "cl:toolu_02", "cl:toolu_04"], "the reasoning and the tools fold; the row carries the commentary");
    assert_eq!(p.stripped_calls.len(), 5);
}

/// The real Host's settled turn (tests/fixtures/live-real-settled-turn.json):
/// GET /live and the /log rows of that turn.
fn real_turn() -> (LiveView, Vec<Message>) {
    let path = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../tests/fixtures/live-real-settled-turn.json");
    let body: Value = serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap();
    let mut view = LiveView::new();
    view.apply_snapshot(body["live"].as_object().unwrap());
    let rows = body["log"]["turns"].as_array().unwrap().iter().map(|r| row(r.clone())).collect();
    (view, rows)
}

const REAL_FAILED: &str = "cl:toolu_01RL4aA1EqQJ43Y7Ay7wuYXt";
const REAL_ANSWER_ROW: &str = "live-783d39d132789c798a9a";

#[test]
fn the_real_hosts_settled_turn_folds_after_its_rows_take_over() {
    let (view, rows) = real_turn();
    let p = present(&view, &rows, &options(&[], 0));
    assert_eq!(titles(&p), ["Worked for 16s · 2 tools", "Ran sleep 60", ""]);
    let kinds: Vec<Kind> = p.entries.iter().map(|e| e.kind).collect();
    assert_eq!(kinds, [Kind::Fold, Kind::Tool, Kind::Message]);
    let fold = &p.entries[0];
    assert_eq!(fold.key, "live:fold:537555728357bf76");
    assert_eq!(
        fold.items,
        ["cl:msg_011CffFZdevXjFVDAZK9REsx:0", "cl:msg_011CffFZyy46uzmYZ4oPGgfd:0", "cl:toolu_01NHjm6d6UD9Xncd1t3gSZKG"],
        "both reasonings and the tool that worked fold"
    );
    let failed = &p.entries[1];
    assert_eq!(failed.key, format!("live:{REAL_FAILED}"));
    assert_eq!(failed.status, "failed", "the failed tool stays in view");
    assert_eq!(failed.meta, "exit 1 · 0.0s");
    assert_eq!(p.entries[2].row, REAL_ANSWER_ROW, "the answer is its durable row, outside the fold");
    // The tool rows are shown by the item rows, so they are not shown again.
    let mut absorbed = p.absorbed_rows.clone();
    absorbed.sort();
    assert_eq!(absorbed, ["msg-9dc44519ed4e93bcff29", "msg-d28c2b3f8a1150792e60"]);
    assert_eq!(p.taken_over.get(REAL_FAILED).map(String::as_str), Some("msg-9dc44519ed4e93bcff29"));
    // Open: one row per item, the reasoning among them.
    let open = present(&view, &rows, &options(&["live:fold:537555728357bf76"], 0));
    assert_eq!(
        titles(&open),
        [
            "Worked for 16s · 2 tools",
            "Thought for 4s: There's a tension here: the task explicitly says not to report to anyone, but th",
            "Ran sleep 60",
            "Thought for 2s: Since foreground sleep is blocked, I'll run this in the background instead to sa",
            "Ran sleep 60",
            "",
        ]
    );
    assert!(open.entries[0].expanded);
}

#[test]
fn a_turn_that_settled_before_the_chat_opened_folds_the_same_way() {
    let (view, rows) = real_turn();
    // The snapshot alone (the /log page not here yet), then with it.
    let before = present(&view, &[], &options(&[], 0));
    let after = present(&view, &rows, &options(&[], 0));
    let keys = |p: &Presented| p.entries.iter().map(|e| e.key.clone()).collect::<Vec<_>>();
    assert_eq!(keys(&before), keys(&after), "the rows landing changes no place");
    assert_eq!(before.entries[2].text, "Sleep is running in the background; I'll continue with step 2 once it finishes.");
    assert!(before.entries[2].row.is_empty());
}

#[test]
fn a_tool_waiting_for_its_explanation_says_explaining() {
    let mut view = turn_full(after(13));
    let p = present(&view, &[], &options(&[], 1759480005000));
    let tool = p.entries.last().unwrap();
    assert_eq!(tool.secondary, "");
    assert!(tool.explaining && tool.reserve_secondary, "running, nothing explained yet: Explaining… in the reserved line");
    let pending = json!({"type": "live", "conv": "conv-1", "epoch": "boot-a", "lseq": 14, "ops": [
        {"op": "upsert", "conv": "conv-1", "id": "cl:toolu_03", "kind": "tool", "rev": 2, "item": {"tool": {"explain": {"level": 2, "status": "pending"}}}},
    ]});
    view.apply_event(pending.as_object().unwrap());
    assert!(present(&view, &[], &options(&[], 1759480005000)).entries.last().unwrap().explaining, "pending");
    let explained = present(&turn_full(after(15)), &[], &options(&[], 1759480005300));
    let tool = explained.entries.last().unwrap();
    assert_eq!(tool.secondary, "Runs the parser tests");
    assert!(!tool.explaining);
    let mut off = options(&[], 1759480005000);
    off.explanations = false;
    let tool = present(&view, &[], &off).entries.last().unwrap().clone();
    assert!(!tool.explaining && !tool.reserve_secondary, "explanations off: no placeholder");
    // A settled tool the Host never explained needs no line.
    let (real, rows) = real_turn();
    let p = present(&real, &rows, &options(&["live:fold:537555728357bf76"], 0));
    assert!(p.entries.iter().all(|e| !e.explaining), "explained or settled: nothing pending");
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
