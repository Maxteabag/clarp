//! How the open turn's live items read (docs/live-items.md §7.3–7.6): one
//! status line, item rows in ordinal order with their labels, explore
//! groups as one row, the fold of a settled turn, and durable `/log` rows
//! taking over items.

use std::collections::HashSet;

use clarp_core::live::LiveView;
use clarp_core::live_present::{Kind, Options, Presented, present, status_line, work_title};
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
    Options { explanations: true, expanded: expanded.iter().map(|s| (*s).to_owned()).collect::<HashSet<_>>(), host_now_ms, fold_running: false, hide_settled: false, show_commentary: false, veil: false, whole_turn: false }
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

/// Grouped (#653): the running turn's work folds into one row that counts
/// its calls so far, its commentary with it; opened, every row shows in place.
#[test]
fn a_running_turn_folds_into_one_working_row_when_grouped() {
    let view = turn_full(after(17));
    let mut opts = options(&[], 1759480008200);
    opts.fold_running = true;

    let presented = present(&view, &[], &opts);

    let kinds: Vec<Kind> = presented.entries.iter().map(|e| e.kind).collect();
    assert_eq!(kinds, [Kind::Fold, Kind::Tool], "{:?}", titles(&presented));
    assert_eq!(presented.entries[0].title, "Working · 2 tools", "the calls it folds so far: the explore group's two; the failed command keeps its row");
    assert_eq!(presented.entries[0].status, "running");
    assert!(presented.entries[0].items.contains(&"cl:msg_01:1".to_owned()), "the commentary folds into it");
    assert_eq!(presented.entries[1].status, "failed", "a failed tool stays in view");
    opts.expanded.insert("live:fold:tr-1".to_owned());
    let open = present(&view, &[], &opts);
    assert_eq!(open.entries.len(), 5, "the work row and every row again: {:?}", titles(&open));
    assert!(open.entries[0].expanded);
    // Always visible and Group old: the running turn's rows as they come.
    opts.fold_running = false;
    assert_eq!(present(&view, &[], &opts).entries.len(), 4);
}

/// Hide when done (#653): the work row shows while the turn runs and goes
/// when it has finished; messages and what failed or stopped stay.
#[test]
fn hide_when_done_shows_the_work_row_only_while_the_turn_runs() {
    let mut opts = options(&[], 1759480008200);
    opts.fold_running = true;
    opts.hide_settled = true;
    let running = present(&turn_full(after(17)), &[], &opts);
    assert_eq!(running.entries[0].title, "Working · 2 tools");
    let settled = present(&turn_full(after(23)), &[], &opts);
    assert!(settled.entries.iter().all(|e| e.kind != Kind::Fold), "{:?}", titles(&settled));
    assert_eq!(titles(&settled), ["Ran npm test", "", "Stopped npm test"], "the reply and what failed or stopped; the commentary went with the row");
}

#[test]
fn the_work_row_says_worked_time_and_tools_in_the_spec_s_words() {
    let title = |running, worked, calls| work_title(running, worked, calls);
    assert_eq!(title(false, Some(11_000), 3).as_deref(), Some("Worked for 11s · 3 tools"));
    assert_eq!(title(false, None, 3).as_deref(), Some("3 tools"));
    assert_eq!(title(false, None, 1).as_deref(), Some("1 tool"));
    assert_eq!(title(false, Some(11_000), 1).as_deref(), Some("Worked for 11s · 1 tool"));
    assert_eq!(title(false, Some(11_000), 0).as_deref(), Some("Worked for 11s"), "no folded call: the time alone");
    assert_eq!(title(false, None, 0), None, "neither a time nor a folded call: no work row");
    assert_eq!(title(true, None, 2).as_deref(), Some("Working · 2 tools"));
    assert_eq!(title(true, None, 1).as_deref(), Some("Working · 1 tool"));
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
    // The commentary (not spoken) folds with the reasoning and the tools (#653); the
    // row counts the calls it opens to, never the summary's tool count.
    assert_eq!(titles(&p), ["Worked for 12s · 3 tools", "Ran npm test", "", "Stopped npm test"]);
    assert_eq!(p.entries[0].kind, Kind::Fold);
    assert_eq!(p.entries[0].key, "live:fold:tr-1");
    assert_eq!(p.entries[0].items, ["cl:msg_01:0", "cl:msg_01:1", "cl:toolu_01", "cl:toolu_02", "cl:toolu_04"], "the reasoning, the commentary and the tools fold");
    assert_eq!(p.entries[2].text, "The failure came from an off-by-one", "as written: trimmed, like `/log`");
    assert_eq!(p.entries[2].status, "interrupted");
    assert_eq!(p.entries[3].meta, "interrupted");
    let open = present(&view, &[], &options(&["live:fold:tr-1"], 1759480012000));
    assert_eq!(open.entries.len(), 1 + 7, "the fold opened shows every row again, in place");
    assert!(open.entries[0].kind == Kind::Fold && open.entries[0].expanded);
    assert_eq!(open.entries[2].text, "Let me look at the parser and its tests.", "the commentary opens in its place");
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
    // The row carries both messages, the answer among them: it shows once, in the
    // answer's place, and never folds.
    assert_eq!(titles(&p), ["Worked for 12s · 3 tools", "Ran npm test", "", "Stopped npm test"]);
    assert_eq!(p.entries[2].row, "live-abc", "the row that holds the answer stays in view, where the answer was");
    assert_eq!(p.entries[0].items, ["cl:msg_01:0", "cl:toolu_01", "cl:toolu_02", "cl:toolu_04"], "the reasoning and the tools fold");
    assert_eq!(p.stripped_calls.len(), 5);
}

/// A settled turn of message, two tools, message, tool, answer.
fn commentary_turn() -> LiveView {
    let message = |id: &str, ordinal: i64, phase: &str, text: &str| {
        json!({"id": id, "kind": "message", "rev": 1, "conv": "c-1", "turn_id": "tr-c", "status": "completed", "ordinal": ordinal,
            "started_at_ms": 1_000 + ordinal * 1_000, "ended_at_ms": 1_500 + ordinal * 1_000, "phase": phase, "text": text})
    };
    let tool = |call: &str, ordinal: i64, command: &str| {
        json!({"id": format!("cl:{call}"), "kind": "tool", "rev": 1, "conv": "c-1", "turn_id": "tr-c", "status": "completed", "ordinal": ordinal,
            "started_at_ms": 1_000 + ordinal * 1_000, "ended_at_ms": 1_500 + ordinal * 1_000,
            "tool": {"name": "Bash", "call_id": call, "category": "exec", "group": null, "label": command, "command": command}})
    };
    let snapshot = json!({"conv": "c-1", "session": "s", "agent_id": "a", "epoch": "e", "lseq": 1, "server_now_ms": 20_000,
        "activity": {"state": "idle", "turn_id": "tr-c"},
        "turn": {"turn_id": "tr-c", "status": "completed", "started_at_ms": 1_000, "ended_at_ms": 13_000, "worked_ms": 12_000, "tool_count": 3},
        "items": [
            message("cl:m:0", 1, "commentary", "Building it first."),
            tool("c1", 2, "make build"),
            tool("c2", 3, "make lint"),
            message("cl:m:1", 4, "commentary", "Built. Now the tests."),
            tool("c3", 5, "make test"),
            message("cl:m:2", 6, "final", "All green."),
        ]});
    let mut view = LiveView::new();
    view.apply_snapshot(snapshot.as_object().unwrap());
    view
}

#[test]
fn commentary_between_tools_folds_into_the_work_row_and_opens_in_place() {
    let view = commentary_turn();
    let p = present(&view, &[], &options(&[], 0));
    let read = |p: &Presented| p.entries.iter().map(|e| if e.kind == Kind::Message { e.text.clone() } else { e.title.clone() }).collect::<Vec<_>>();
    assert_eq!(read(&p), ["Worked for 12s · 3 tools", "All green."], "the commentary folds; the answer never does");
    assert_eq!(p.entries[0].items, ["cl:m:0", "cl:c1", "cl:c2", "cl:m:1", "cl:c3"]);
    let open = present(&view, &[], &options(&["live:fold:tr-c"], 0));
    assert_eq!(read(&open), ["Worked for 12s · 3 tools", "Building it first.", "Ran make build", "Ran make lint", "Built. Now the tests.", "Ran make test", "All green."]);
    let hidden = present(&view, &[], &Options { hide_settled: true, ..options(&[], 0) });
    assert_eq!(read(&hidden), ["All green."], "Hide when done: the folded commentary goes with the work row");
    let always = present(&view, &[], &Options { show_commentary: true, ..options(&[], 0) });
    assert_eq!(read(&always), ["Building it first.", "Worked for 12s · 3 tools", "Built. Now the tests.", "All green."], "Always visible keeps the commentary in its place");
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
    assert_eq!(titles(&p), ["Worked for 16s · 1 tool", "Ran sleep 60", ""], "the folded calls: the failed one stays out");
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
            "Worked for 16s · 1 tool",
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
    assert!(!tool.explaining && tool.reserve_secondary, "running, none requested (null): no Explaining…, the line kept for one");
    let pending = json!({"type": "live", "conv": "conv-1", "epoch": "boot-a", "lseq": 14, "ops": [
        {"op": "upsert", "conv": "conv-1", "id": "cl:toolu_03", "kind": "tool", "rev": 2, "item": {"tool": {"explain": {"level": 2, "status": "pending"}}}},
    ]});
    view.apply_event(pending.as_object().unwrap());
    assert!(present(&view, &[], &options(&[], 1759480005000)).entries.last().unwrap().explaining, "pending");
    // Contract 55 settles every explanation ready or failed (with a reason);
    // anything but pending is not coming, and a failed one shows nothing.
    for explain in [
        json!({"level": 2, "status": "failed", "reason": "skipped"}),
        json!({"level": 2, "status": "failed", "reason": "error", "text": "explainer timed out"}),
        json!({"level": 2, "status": "skipped"}),
        json!({"level": 2, "status": "queued"}),
    ] {
        let mut settled = view.clone();
        let op = json!({"type": "live", "conv": "conv-1", "epoch": "boot-a", "lseq": 15, "ops": [
            {"op": "upsert", "conv": "conv-1", "id": "cl:toolu_03", "kind": "tool", "rev": 3, "item": {"tool": {"explain": explain}}},
        ]});
        settled.apply_event(op.as_object().unwrap());
        let tool = present(&settled, &[], &options(&[], 1759480005000)).entries.last().unwrap().clone();
        assert!(!tool.explaining && tool.secondary.is_empty(), "{explain}: nothing");
    }
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

#[test]
fn a_running_tool_whose_log_row_landed_at_its_start_keeps_its_live_row() {
    // The real Host writes a tool's /log row (same call_id) when the tool
    // starts: the item row keeps its look, label, timer and tail, and the
    // row is not shown again.
    let path = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../tests/fixtures/live-real-settled-turn.json");
    let body: Value = serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap();
    let (mut view, mut rows) = real_turn();
    rows.extend(body["next"]["rows"].as_array().unwrap().iter().map(|r| row(r.clone())));
    let mut event = body["next"]["event"].clone();
    event["lseq"] = json!(42);
    view.apply_event(event.as_object().unwrap());
    let started = body["next"]["event"]["ops"][1]["item"]["started_at_ms"].as_i64().unwrap();
    let p = present(&view, &rows, &options(&[], started + 13_000));
    assert_eq!(titles(&p), ["Running python3 -c 'import time; time.sleep(120)'"]);
    let tool = &p.entries[0];
    assert_eq!((tool.status.as_str(), tool.meta.as_str()), ("running", "0:13"), "the running tool ticks");
    assert!(!tool.explaining && tool.reserve_secondary, "the real Host sends explain: null until it asks: nothing yet, the line kept");
    let id = "cl:toolu_017NGGuPh5DkHZyPKu7aJyz5";
    let pending = json!({"type": "live", "conv": "c-probe", "epoch": "26f356fdabe6", "lseq": 43, "ops": [
        {"op": "upsert", "conv": "c-probe", "id": id, "kind": "tool", "rev": 2, "item": {"tool": {"explain": {"level": 2, "status": "pending"}}}},
    ]});
    view.apply_event(pending.as_object().unwrap());
    assert!(present(&view, &rows, &options(&[], started + 14_000)).entries[0].explaining, "pending: Explaining…");
    assert_eq!(p.absorbed_rows, ["msg-e60c25b9161ac8b660ca"], "its /log row is shown by the item row");
    let mut done = body["next"]["done"].clone();
    done["lseq"] = json!(44);
    done["ops"][0]["rev"] = json!(3);
    view.apply_event(done.as_object().unwrap());
    let p = present(&view, &rows, &options(&[], started + 20_000));
    let tool = &p.entries[0];
    assert_eq!((tool.title.as_str(), tool.status.as_str(), tool.meta.as_str()), ("Ran python3 -c 'import time; time.sleep(120)'", "failed", "exit 1 · 19.9s"));
    assert_eq!(tool.lines, ["Exit code 137"], "the failed tool keeps its tail");
    assert_eq!(p.absorbed_rows, ["msg-e60c25b9161ac8b660ca"]);
    assert!(tool.explaining, "the turn still running, the explanation still pending");
    let ended = json!({"type": "live", "conv": "c-probe", "epoch": "26f356fdabe6", "lseq": 45, "ops": [
        {"op": "turn", "conv": "c-probe", "turn": {"turn_id": "2b2b466e35457fb0", "status": "failed", "started_at_ms": 1791028403856_i64, "ended_at_ms": 1791028426000_i64, "worked_ms": 22144, "tool_count": 1}},
    ]});
    view.apply_event(ended.as_object().unwrap());
    let p = present(&view, &rows, &options(&[], started + 21_000));
    assert!(p.entries.iter().all(|e| !e.explaining && !(e.reserve_secondary && e.secondary.is_empty())), "the turn ended: no Explaining…, no line held: {:?}", p.entries.iter().map(|e| (&e.key, e.explaining)).collect::<Vec<_>>());
}

/// A user message written at `ms` (Host time), from another turn.
fn user_at(id: &str, ms: i64) -> Message {
    let stamp = chrono::DateTime::from_timestamp_millis(ms).unwrap().format("%Y-%m-%dT%H:%M:%S%.3fZ").to_string();
    row(json!({"id": id, "role": "user", "text": "Next", "timestamp": stamp, "trace_id": format!("tr-{id}")}))
}

const REAL_STARTED: i64 = 1791028137185;

#[test]
fn a_message_written_after_the_turn_began_goes_below_the_settled_turn() {
    // The real settled turn, its tool rows not landed yet: the prompt and
    // the live answer row. A message sent once it settled goes below it.
    let (view, rows) = real_turn();
    let before: Vec<Message> = rows.iter().filter(|r| r.id.starts_with("u-") || r.id.starts_with("live-")).cloned().chain([user_at("u-next", REAL_STARTED + 30_000)]).collect();
    let p = present(&view, &before, &options(&[], 0));
    let refs: Vec<&Message> = before.iter().collect();
    assert_eq!(p.anchor(&refs), 2, "after the prompt (and the turn's own live row), before the new message");
    // The tool rows land after the new message (in /log order): the turn's
    // own rows never push its place, so it stays before the new message.
    let after: Vec<Message> = before.iter().cloned().chain(rows.iter().filter(|r| r.id.starts_with("msg-")).cloned()).collect();
    let p = present(&view, &after, &options(&[], 0));
    let refs: Vec<&Message> = after.iter().collect();
    assert_eq!(p.anchor(&refs), 2);
    // In /log order (the tool rows before the message) the same.
    let ordered: Vec<Message> = rows.iter().cloned().chain([user_at("u-next", REAL_STARTED + 30_000)]).collect();
    let p = present(&view, &ordered, &options(&[], 0));
    let refs: Vec<&Message> = ordered.iter().collect();
    assert_eq!(refs[p.anchor(&refs)].id, "u-next");
}

#[test]
fn the_anchor_follows_the_turns_prompt_and_host_times() {
    let (view, rows) = real_turn();
    let p = present(&view, &rows, &options(&[], 0));
    let earlier = user_at("u-old", REAL_STARTED - 60_000);
    let same = user_at("u-same", REAL_STARTED);
    let undated = row(json!({"id": "local", "role": "assistant", "text": "no time"}));
    let later = user_at("u-later", REAL_STARTED + 1);
    let chat = [&earlier, &rows[0], &same, &undated, &later];
    assert_eq!(p.anchor(&chat), 4, "earlier, at the start or without a time: above; a millisecond after: below");
    assert_eq!(p.anchor(&[&earlier, &rows[0]]), 2, "nothing after it: last");
    // The Host wrote the prompt a few ms after the turn began: still above.
    let mut late_prompt = rows[0].clone();
    late_prompt.timestamp = "2026-10-03T11:48:57.200Z".into();
    assert_eq!(p.anchor(&[&earlier, &late_prompt, &later]), 2, "the turn's prompt is always above its work");
}

#[test]
fn a_message_queued_while_the_turn_runs_goes_below_it() {
    let path = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../tests/fixtures/live-real-settled-turn.json");
    let body: Value = serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap();
    let (mut view, mut rows) = real_turn();
    rows.extend(body["next"]["rows"].as_array().unwrap().iter().map(|r| row(r.clone())));
    let mut event = body["next"]["event"].clone();
    event["lseq"] = json!(42);
    view.apply_event(event.as_object().unwrap());
    let started = body["next"]["event"]["ops"][0]["turn"]["started_at_ms"].as_i64().unwrap();
    // The prompt is written in the turn's first millisecond; the tool row
    // at the tool's start (the turn's own); the queued message after both.
    let queued = user_at("u-queued", started + 5_000);
    let mut chat = rows.clone();
    let tool_row = chat.pop().unwrap();
    chat.push(queued);
    chat.push(tool_row);
    let p = present(&view, &chat, &options(&[], started + 6_000));
    let refs: Vec<&Message> = chat.iter().collect();
    let at = p.anchor(&refs);
    assert_eq!(refs[at - 1].id, "u-clarp-admin-3c48398b85b08a09", "right after the prompt that started it");
    assert_eq!(refs[at].id, "u-queued");
}

/// A live message streams its voice markup as text: the row shows the
/// written reply only, never a tag, a half-arrived tag or a filler.
#[test]
fn a_streaming_message_never_shows_voice_markup() {
    let mut view = turn_full(after(6));
    let chunks = [
        "Sure <spe",
        "ak>Here it is <break time=\"35",
        "0ms\"/> the plan <vox>u",
        "m</vox>, step one",
        " and two.</speak>",
    ];
    let mut full = String::new();
    for (index, chunk) in chunks.iter().enumerate() {
        full.push_str(chunk);
        let event = json!({"type": "live", "agent_id": "agent-1", "session": "rachel", "conv": "conv-1", "epoch": "boot-a",
            "lseq": 7 + index as i64, "server_now_ms": 1759480004400i64 + index as i64,
            "ops": [{"op": "append", "conv": "conv-1", "id": "cl:msg_01:1", "kind": "message", "rev": 2 + index as i64, "field": "text", "chunk": chunk}]});
        view.apply_event(event.as_object().unwrap());
        let p = present(&view, &[], &options(&[], 1759480004500));
        let message = p.entries.iter().find(|e| e.kind == Kind::Message).expect("the message row");
        for leak in ["<", ">", "speak", "break", "vox", "um", "35"] {
            assert!(!message.text.contains(leak), "{leak:?} shows in {:?} after {full:?}", message.text);
        }
        assert!(message.text.starts_with("Sure"), "{:?}", message.text);
    }
    let done = json!({"type": "live", "agent_id": "agent-1", "session": "rachel", "conv": "conv-1", "epoch": "boot-a",
        "lseq": 7 + chunks.len() as i64, "server_now_ms": 1759480004900i64,
        "ops": [{"op": "done", "conv": "conv-1", "id": "cl:msg_01:1", "kind": "message", "rev": 2 + chunks.len() as i64, "status": "completed", "started_at_ms": 1759480004300i64, "ended_at_ms": 1759480004900i64}]});
    view.apply_event(done.as_object().unwrap());
    let p = present(&view, &[], &options(&[], 1759480005000));
    let message = p.entries.iter().find(|e| e.kind == Kind::Message).unwrap();
    // The spoken part is the summary line, the rest the written text (#823).
    assert_eq!((message.spoken_text.clone(), message.text.clone()), clarp_core::text::reply_parts(&full, false));
    assert_eq!(message.spoken_text, "Here it is, the plan step one and two.");
    assert_eq!(message.text, "Sure");
}

/// A stale live turn (tests/fixtures/live-real-stale-turn.json): GET /live
/// kept the first turn running while two newer prompts started turns of
/// their own in /log, and labelled every later item with the first turn.
fn stale_turn() -> (LiveView, Vec<Message>, Value) {
    let path = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../tests/fixtures/live-real-stale-turn.json");
    let body: Value = serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap();
    let mut view = LiveView::new();
    view.apply_snapshot(body["live"].as_object().unwrap());
    let rows = body["log"]["turns"].as_array().unwrap().iter().map(|r| row(r.clone())).collect();
    (view, rows, body)
}

/// The chat as the window shows it: the rows entries do not stand for,
/// each entry at its anchor (a durable row that took over an entry shows
/// there, as the entry).
fn shown_order(p: &Presented, rows: &[Message]) -> Vec<String> {
    let taken: HashSet<&str> = p.entries.iter().map(|e| e.row.as_str()).filter(|r| !r.is_empty()).collect();
    let rest: Vec<&Message> = rows
        .iter()
        .filter(|r| !taken.contains(r.id.as_str()) && !p.absorbed_rows.contains(&r.id) && !p.hidden_rows.contains(&r.id))
        .collect();
    let anchors = p.anchors(&rest);
    assert_eq!(anchors.len(), p.entries.len());
    assert!(anchors.windows(2).all(|w| w[0] <= w[1]), "entries keep their order: {anchors:?}");
    let mut order: Vec<String> = Vec::new();
    let mut next = 0;
    for (index, row) in rest.iter().enumerate() {
        while next < anchors.len() && anchors[next] == index {
            order.push(p.entries[next].key.clone());
            next += 1;
        }
        order.push(row.id.clone());
    }
    order.extend(p.entries[next..].iter().map(|e| e.key.clone()));
    order
}

#[test]
fn a_stale_turn_splits_at_the_newer_prompts() {
    let (view, rows, body) = stale_turn();
    let now = body["live"]["server_now_ms"].as_i64().unwrap();
    let p = present(&view, &rows, &options(&[], now));
    let order = shown_order(&p, &rows);
    assert_eq!(
        order,
        [
            "u-stale-first",
            "live:cl:msg_a1:0",
            "live:cl:toolu_a1",
            "live:cl:msg_a2:0",
            "live:cl:toolu_a3",
            "u-stale-second",
            "live:cl:msg_b1:0",
            "live:explore:cl:toolu_b1",
            "live:cl:toolu_b3",
            "live:cl:msg_b4:0",
            "u-stale-third",
            "live:cl:msg_c1:0",
            "live:cl:toolu_c1",
            "live:cl:toolu_c2",
            "live:cl:msg_c3:0",
        ],
        "each newer prompt above the items that started after it"
    );
    // The answers /log took over still show in their items' places.
    let answer = p.entries.iter().find(|e| e.key == "live:cl:msg_b4:0").unwrap();
    assert_eq!(answer.row, "msg-b4");
    // The status line keeps working.
    let line = status_line(&view, now).expect("busy");
    assert_eq!((line.text.as_str(), line.busy), ("● git push · 0:14", true));
}

#[test]
fn a_live_item_never_goes_above_a_row_written_before_it_started() {
    // Any row not the turn's own, not only a prompt: the items that started
    // after it go below it, the ones from before it stay above.
    let (view, mut rows, body) = stale_turn();
    let started = body["live"]["turn"]["started_at_ms"].as_i64().unwrap();
    let mut note = user_at("u-note", started + 43_000);
    note.role = "assistant".into();
    let at = rows.iter().position(|r| r.id == "msg-a3").unwrap();
    rows.insert(at, note);
    let p = present(&view, &rows, &options(&[], 0));
    let order = shown_order(&p, &rows);
    let place = |id: &str| order.iter().position(|o| o == id).unwrap_or_else(|| panic!("{id} in {order:?}"));
    assert!(place("live:cl:msg_a2:0") < place("u-note") && place("u-note") < place("live:cl:toolu_a3"), "{order:?}");
    assert!(place("u-stale-second") < place("live:cl:msg_b1:0"), "{order:?}");
}

/// A durable row carrying folded commentary is not shown on its own; one
/// that also carries the final reply is the reply and never folds.
#[test]
fn a_durable_commentary_row_hides_with_its_fold() {
    let mut view = commentary_turn();
    let mut snapshot = serde_json::json!({"conv": "c-1", "session": "s", "agent_id": "a", "epoch": "e", "lseq": 2, "server_now_ms": 20_000,
        "activity": {"state": "idle", "turn_id": "tr-c"},
        "turn": {"turn_id": "tr-c", "status": "completed", "started_at_ms": 1_000, "ended_at_ms": 13_000, "worked_ms": 12_000, "tool_count": 3},
        "items": view.items().iter().map(|i| Value::Object((*i).clone())).collect::<Vec<_>>()});
    for item in snapshot["items"].as_array_mut().unwrap() {
        if item["id"] == "cl:m:0" {
            item["row_id"] = json!("r-note");
        }
    }
    view.apply_snapshot(snapshot.as_object().unwrap());
    let note = row(json!({"id": "r-note", "role": "assistant", "text": "Building it first.", "revision": 3}));
    let mut opts = options(&[], 0);
    let p = present(&view, std::slice::from_ref(&note), &opts);
    assert!(p.hidden_rows.contains(&"r-note".to_owned()), "folded: {:?}", p.hidden_rows);
    opts.expanded.insert("live:fold:tr-c".to_owned());
    let open = present(&view, std::slice::from_ref(&note), &opts);
    assert!(!open.hidden_rows.contains(&"r-note".to_owned()) && open.entries.iter().any(|e| e.row == "r-note"), "opened, the row shows in its place");
}

/// A settled turn: a spoken acknowledgment (after a first tool), a tool,
/// plain commentary, a tool, the reply.
fn heard_turn() -> LiveView {
    let message = |id: &str, ordinal: i64, phase: &str, text: &str| {
        json!({"id": id, "kind": "message", "rev": 1, "conv": "c-1", "turn_id": "tr-h", "status": "completed", "ordinal": ordinal,
            "started_at_ms": 1_000 + ordinal * 1_000, "ended_at_ms": 1_500 + ordinal * 1_000, "phase": phase, "text": text})
    };
    let tool = |call: &str, ordinal: i64, command: &str| {
        json!({"id": format!("cl:{call}"), "kind": "tool", "rev": 1, "conv": "c-1", "turn_id": "tr-h", "status": "completed", "ordinal": ordinal,
            "started_at_ms": 1_000 + ordinal * 1_000, "ended_at_ms": 1_500 + ordinal * 1_000,
            "tool": {"name": "Bash", "call_id": call, "category": "exec", "group": null, "label": command, "command": command}})
    };
    let snapshot = json!({"conv": "c-1", "session": "s", "agent_id": "a", "epoch": "e", "lseq": 1, "server_now_ms": 20_000,
        "activity": {"state": "idle", "turn_id": "tr-h"},
        "turn": {"turn_id": "tr-h", "status": "completed", "started_at_ms": 1_000, "ended_at_ms": 9_000, "worked_ms": 8_000, "tool_count": 3},
        "items": [
            tool("h0", 1, "git status"),
            message("cl:h:ack", 2, "commentary", "<speak>On it, checking the build.</speak>"),
            tool("h1", 3, "make build"),
            message("cl:h:note", 4, "commentary", "Looking at the build log now."),
            tool("h2", 5, "make test"),
            message("cl:h:final", 6, "final", "All green."),
        ]});
    let mut view = LiveView::new();
    view.apply_snapshot(snapshot.as_object().unwrap());
    view
}

/// Commentary the user heard (#653) stays in view above the work row in
/// every mode, Hide when done too; plain commentary folds; the count is
/// still the folded calls.
#[test]
fn a_spoken_acknowledgment_stays_above_the_work_row() {
    let read = |p: &Presented| p.entries.iter().map(|e| if e.kind == Kind::Message { e.text.clone() } else { e.title.clone() }).collect::<Vec<_>>();
    let mut opts = options(&[], 0);
    let p = present(&heard_turn(), &[], &opts);
    assert_eq!(read(&p), ["On it, checking the build.", "Worked for 8s · 3 tools", "All green."], "the words heard, the work row, the reply");
    assert!(p.entries[0].spoken);
    opts.hide_settled = true;
    assert_eq!(read(&present(&heard_turn(), &[], &opts)), ["On it, checking the build.", "All green."], "Hide when done keeps it");
    opts.hide_settled = false;
    opts.expanded.insert("live:fold:tr-h".to_owned());
    let open = present(&heard_turn(), &[], &opts);
    assert_eq!(read(&open), ["On it, checking the build.", "Worked for 8s · 3 tools", "Ran git status", "Ran make build", "Looking at the build log now.", "Ran make test", "All green."], "opened, the rest in place");
    assert!(clarp_core::text::has_speech("Sure. <speak>On it.</speak>") && !clarp_core::text::has_speech("<speak> </speak>") && !clarp_core::text::has_speech("speak plainly"));
}

const SPOKEN_STARTED: i64 = 1791100000000;

fn iso(ms: i64) -> String {
    chrono::DateTime::from_timestamp_millis(ms).unwrap().format("%Y-%m-%dT%H:%M:%S%.3fZ").to_string()
}

/// A spoken turn as a backend that writes its answer only to `/log` sends it:
/// its live items carry the thinking, the acknowledgment it speaks first and
/// the tools, but no final message. `/log` holds the prompt, the
/// acknowledgment (with the turn's exploration cell) and the answer.
fn spoken_turn() -> (LiveView, Vec<Message>) {
    let s = SPOKEN_STARTED;
    let tool = |id: &str, call: &str, ordinal: i64, at: i64| json!({"id": id, "kind": "tool", "rev": 2, "conv": "c-voice", "turn_id": "tr-voice",
        "status": "completed", "ordinal": ordinal, "started_at_ms": s + at, "ended_at_ms": s + at + 900,
        "tool": {"name": "bash", "call_id": call, "category": "exec", "group": null, "label": "ls"}});
    let snapshot = json!({"conv": "c-voice", "session": "rachel", "agent_id": "agent-1", "epoch": "boot-v", "lseq": 30, "server_now_ms": s + 200_000,
        "activity": {"state": "idle", "turn_id": "tr-voice", "tool": null, "running_tools": 0, "headline": null, "item_id": null, "since_ms": s + 185_000, "turn_started_ms": null},
        "turn": {"turn_id": "tr-voice", "status": "completed", "started_at_ms": s, "ended_at_ms": s + 185_000, "worked_ms": 185_000, "tool_count": 2},
        "items": [
            {"id": "oc:think", "kind": "reasoning", "rev": 2, "conv": "c-voice", "turn_id": "tr-voice", "status": "completed", "ordinal": 1,
             "started_at_ms": s + 500, "ended_at_ms": s + 1_500, "title": "Plan the lookup", "text": "Plan the lookup"},
            {"id": "oc:ack", "kind": "message", "rev": 3, "conv": "c-voice", "turn_id": "tr-voice", "status": "completed", "ordinal": 2,
             "started_at_ms": s + 1_900, "ended_at_ms": s + 2_000, "phase": "commentary", "row_id": "live-ack", "text": "<speak>On it, let me look.</speak>"},
            tool("oc:t1", "call-1", 3, 2_100),
            tool("oc:t2", "call-2", 4, 3_100),
        ]});
    let mut view = LiveView::new();
    view.apply_snapshot(snapshot.as_object().unwrap());
    (view, spoken_rows())
}

fn spoken_rows() -> Vec<Message> {
    let s = SPOKEN_STARTED;
    vec![
        row(json!({"id": "u-voice", "role": "user", "text": "Add the other host too", "timestamp": iso(s), "trace_id": "tr-voice", "revision": 9})),
        row(json!({"id": "live-ack", "role": "assistant", "kind": "commentary", "phase": "commentary", "text": "<speak>On it, let me look.</speak>",
            "timestamp": iso(s + 2_000), "trace_id": "tr-voice", "revision": 12, "activity_count": 2,
            "display_cells": [{"id": "exploration-call-1-call-2", "kind": "exploration", "title": "Explored", "lines": []}]})),
        row(json!({"id": "live-answer", "role": "assistant", "kind": "final_answer", "phase": "final", "text": "Which host do you mean?",
            "timestamp": iso(s + 176_000), "trace_id": "tr-voice", "revision": 14,
            "turn": {"turn_id": "tr-voice", "status": "completed", "started_at_ms": s, "ended_at_ms": s + 185_000, "worked_ms": 185_000, "tool_count": 2}})),
    ]
}

#[test]
fn a_spoken_acknowledgment_stays_above_the_work_and_the_answer_while_live_holds_the_turn() {
    let (view, rows) = spoken_turn();
    // Folded: the acknowledgment, the fold, then the answer no item stands for.
    let p = present(&view, &rows, &options(&[], 0));
    let kinds: Vec<Kind> = p.entries.iter().map(|e| e.kind).collect();
    assert_eq!(kinds, [Kind::Message, Kind::Fold], "{:?}", titles(&p));
    assert_eq!(p.entries[0].row, "live-ack");
    assert_eq!(shown_order(&p, &rows), ["u-voice", "live:oc:ack", "live:fold:tr-voice", "live-answer"]);
    assert!(p.stripped_calls.contains("exploration-call-1-call-2"), "the row's cell of those tools is not shown again beside the fold");
    // Opened: the acknowledgment still first, the work under it, the answer last.
    let open = present(&view, &rows, &options(&["live:fold:tr-voice"], 0));
    let order = shown_order(&open, &rows);
    assert_eq!(order.first().map(String::as_str), Some("u-voice"));
    assert_eq!(order[1], "live:oc:ack");
    assert_eq!(order.last().map(String::as_str), Some("live-answer"));
    // Hide when done: the acknowledgment, then the answer.
    let hidden = present(&view, &rows, &Options { hide_settled: true, ..options(&[], 0) });
    assert_eq!(shown_order(&hidden, &rows), ["u-voice", "live:oc:ack", "live-answer"]);
}

/// A settled turn of thinking and its answer, no tool: the fold stays above
/// the answer (the answer is never taken for an acknowledgment).
#[test]
fn a_turn_of_thinking_and_its_answer_keeps_the_fold_above_the_answer() {
    let s = SPOKEN_STARTED;
    let snapshot = json!({"conv": "c-think", "session": "rachel", "agent_id": "agent-1", "epoch": "boot-t", "lseq": 12, "server_now_ms": s + 10_000,
        "activity": {"state": "idle", "turn_id": "tr-think", "tool": null, "running_tools": 0, "headline": null, "item_id": null, "since_ms": s + 5_000, "turn_started_ms": null},
        "turn": {"turn_id": "tr-think", "status": "completed", "started_at_ms": s, "ended_at_ms": s + 5_000, "worked_ms": 5_000, "tool_count": 0},
        "items": [
            {"id": "oc:think", "kind": "reasoning", "rev": 2, "conv": "c-think", "turn_id": "tr-think", "status": "completed", "ordinal": 1,
             "started_at_ms": s + 200, "ended_at_ms": s + 3_000, "title": "Weigh it", "text": "Weigh it"},
            {"id": "oc:answer", "kind": "message", "rev": 3, "conv": "c-think", "turn_id": "tr-think", "status": "completed", "ordinal": 2,
             "started_at_ms": s + 3_100, "ended_at_ms": s + 5_000, "phase": "final", "text": "Yes, that works."},
        ]});
    let mut view = LiveView::new();
    view.apply_snapshot(snapshot.as_object().unwrap());
    for expanded in [&[][..], &["live:fold:tr-think"][..]] {
        let p = present(&view, &[], &options(expanded, 0));
        let kinds: Vec<Kind> = p.entries.iter().map(|e| e.kind).collect();
        assert_eq!(kinds.first(), Some(&Kind::Fold), "the fold first: {:?}", titles(&p));
        assert_eq!(kinds.last(), Some(&Kind::Message), "the answer last: {:?}", titles(&p));
    }
}

/// The work row counts the calls it opens to (Arnie's ruling for every client): every
/// call failed, it says the time alone; no time and no folded call, there is no row.
#[test]
fn the_work_row_counts_folded_calls_and_says_its_time() {
    let s = SPOKEN_STARTED;
    let failed = |id: &str, call: &str, ordinal: i64| json!({"id": id, "kind": "tool", "rev": 2, "conv": "c-n", "turn_id": "tr-n",
        "status": "failed", "ordinal": ordinal, "started_at_ms": s + ordinal * 100, "ended_at_ms": s + ordinal * 100 + 50,
        "tool": {"name": "bash", "call_id": call, "category": "exec", "group": null, "label": "ls /gone"}});
    let view = |turn: Value, items: Vec<Value>| {
        let snapshot = json!({"conv": "c-n", "session": "rachel", "agent_id": "agent-1", "epoch": "boot-n", "lseq": 5, "server_now_ms": s + 20_000,
            "activity": {"state": "idle", "turn_id": "tr-n"}, "turn": turn, "items": items});
        let mut view = LiveView::new();
        view.apply_snapshot(snapshot.as_object().unwrap());
        view
    };
    let answer = json!({"id": "oc:a", "kind": "message", "rev": 1, "conv": "c-n", "turn_id": "tr-n", "status": "completed", "ordinal": 9,
        "started_at_ms": s + 900, "ended_at_ms": s + 950, "phase": "final", "text": "It is gone."});
    // Every call failed: the time, no count, nothing to open; the failed calls in their rows.
    let timed = json!({"turn_id": "tr-n", "status": "completed", "started_at_ms": s, "ended_at_ms": s + 11_000, "worked_ms": 11_000, "tool_count": 2});
    let p = present(&view(timed, vec![failed("oc:f1", "f1", 1), failed("oc:f2", "f2", 2), answer.clone()]), &[], &options(&[], 0));
    assert_eq!(titles(&p), ["Worked for 11s", "Ran ls /gone", "Ran ls /gone", ""]);
    assert!(!p.entries[0].expandable);
    // Neither a time nor a folded call: no row.
    let untimed = json!({"turn_id": "tr-n", "status": "completed", "tool_count": 2});
    let p = present(&view(untimed, vec![failed("oc:f1", "f1", 1), answer]), &[], &options(&[], 0));
    assert!(p.entries.iter().all(|e| e.kind != Kind::Fold), "{:?}", titles(&p));
}

#[test]
fn a_refused_tool_says_why_in_one_quiet_line_without_the_tags() {
    let mut view = turn_full(after(13));
    let refused = json!({"type": "live", "conv": "conv-1", "epoch": "boot-a", "lseq": 14, "ops": [
        {"op": "upsert", "conv": "conv-1", "id": "cl:toolu_03", "kind": "tool", "rev": 2, "item": {"status": "failed", "tool": {"output": {
            "tail": ["<tool_use_error>Blocked: standalone sleep 60.", "Use a loop instead.</tool_use_error>"], "total_lines": 2, "exit_code": 1}}}},
    ]});
    view.apply_event(refused.as_object().unwrap());
    let tool = present(&view, &[], &options(&[], 1759480005000)).entries.last().unwrap().clone();
    assert_eq!(tool.secondary, "Blocked: standalone sleep 60. Use a loop instead.");
    assert!(tool.lines.is_empty() && !tool.expandable && !tool.explaining, "no output box: {:?}", tool.lines);
    assert_eq!(clarp_core::live_present::refusal("plain output"), None);
}

/// A running turn: a tool that ran before a message was steered into it,
/// one still running that began before it too; `read_rows` as given.
fn steered_turn(read_rows: Option<Value>) -> LiveView {
    const S: i64 = 1_791_000_000_000;
    let tool = |call: &str, ordinal: i64, at: i64, status: &str| {
        let mut item = json!({"id": format!("cl:{call}"), "kind": "tool", "rev": 1, "conv": "c-1", "turn_id": "tr-s", "status": status, "ordinal": ordinal,
            "started_at_ms": S + at, "tool": {"name": "Bash", "call_id": call, "category": "exec", "group": null, "label": call, "command": call}});
        if status == "completed" {
            item["ended_at_ms"] = json!(S + at + 100);
        }
        item
    };
    let mut turn = json!({"turn_id": "tr-s", "status": "running", "started_at_ms": S + 1_000, "tool_count": 2});
    if let Some(read) = read_rows {
        turn["read_rows"] = read;
    }
    let snapshot = json!({"conv": "c-1", "session": "s", "agent_id": "a", "epoch": "e", "lseq": 1, "server_now_ms": S + 4_000,
        "activity": {"state": "tool", "turn_id": "tr-s"}, "turn": turn,
        "items": [tool("s1", 1, 2_000, "completed"), tool("s2", 2, 2_200, "running")]});
    let mut view = LiveView::new();
    view.apply_snapshot(snapshot.as_object().unwrap());
    view
}

fn steered_rows() -> Vec<Message> {
    const S: i64 = 1_791_000_000_000;
    let mut prompt = user_at("u-go", S + 1_000);
    prompt.trace_id = "tr-s".into();
    vec![prompt, user_at("u-steer", S + 2_500)]
}

#[test]
fn the_working_line_goes_below_a_message_read_into_the_running_turn() {
    let rows = steered_rows();
    // Grouped: the Working row folds both tools.
    let mut grouped = options(&[], 0);
    grouped.fold_running = true;
    let before = present(&steered_turn(None), &rows, &grouped);
    assert_eq!(shown_order(&before, &rows), ["u-go", "live:fold:tr-s", "u-steer"], "without read_rows, as before");
    let read = present(&steered_turn(Some(json!(["u-steer"]))), &rows, &grouped);
    assert_eq!(read.read_rows, ["u-steer"]);
    // What ran before the message folds above it, Working goes below it
    // (#843): the order the settled turn keeps.
    assert_eq!(shown_order(&read, &rows), ["u-go", "live:fold:tr-s@u-steer", "u-steer", "live:fold:tr-s"], "Working below the steered message");
    assert_eq!(titles(&read), ["2 tools", "Working"]);
    // Each step in its row keeps the place its time gives it, running or
    // not: both began before the message.
    let open = options(&[], 0);
    let before = present(&steered_turn(None), &rows, &open);
    assert_eq!(shown_order(&before, &rows), ["u-go", "live:cl:s1", "live:cl:s2", "u-steer"]);
    let read = present(&steered_turn(Some(json!(["u-steer"]))), &rows, &open);
    assert_eq!(shown_order(&read, &rows), ["u-go", "live:cl:s1", "live:cl:s2", "u-steer"]);
}

#[test]
fn a_message_that_waits_for_the_next_turn_stays_below_the_working_line() {
    // Read rows name only what the turn read: a waiting message is in none.
    const S: i64 = 1_791_000_000_000;
    let mut rows = steered_rows();
    rows.push(user_at("u-waits", S + 3_000));
    let mut grouped = options(&[], 0);
    grouped.fold_running = true;
    let read = present(&steered_turn(Some(json!(["u-steer"]))), &rows, &grouped);
    assert_eq!(shown_order(&read, &rows), ["u-go", "live:fold:tr-s@u-steer", "u-steer", "live:fold:tr-s", "u-waits"]);
}
