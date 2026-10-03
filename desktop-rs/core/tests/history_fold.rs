//! Finished turns fold in history (docs/live-items.md §7.6): over the
//! recorded `/log` rows each settled turn shows one `Worked for N · K tools`
//! row; opened, one row per tool and the commentary; what failed or stopped
//! and the final answer stay outside it.

use std::collections::HashSet;

use clarp_core::history_fold::{Fold, Folded, Options, Piece, present};
use clarp_core::live::LiveView;
use clarp_core::live_present::Kind;
use clarp_core::protocol::Message;
use serde_json::{Value, json};

fn recorded() -> Value {
    let path = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../tests/fixtures/live-real-settled-turn.json");
    serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap()
}

fn row(value: &Value) -> Message {
    Message::from_json(value.as_object().unwrap())
}

/// The real turn's /log rows, then (with `next`) the turn in between and the
/// next prompt with its running tool.
fn real_rows(next: bool) -> Vec<Message> {
    let body = recorded();
    let mut rows: Vec<Message> = body["log"]["turns"].as_array().unwrap().iter().map(row).collect();
    if next {
        rows.extend(body["next"]["rows"].as_array().unwrap().iter().map(row));
    }
    rows
}

fn options(expanded: &[&str]) -> Options<'static> {
    Options { explanations: true, expanded: expanded.iter().map(|s| (*s).to_owned()).collect::<HashSet<_>>(), ..Options::default() }
}

/// Each piece as the transcript reads it: an entry's title, a row's id.
fn read(fold: &Fold) -> Vec<String> {
    fold.pieces
        .iter()
        .map(|p| match p {
            Piece::Row(id) => format!("row {id}"),
            Piece::Entry(e) => e.title.clone(),
        })
        .collect()
}

fn entry<'a>(fold: &'a Fold, key: &str) -> &'a clarp_core::live_present::Entry {
    fold.pieces
        .iter()
        .find_map(|p| match p {
            Piece::Entry(e) if e.key == key => Some(e),
            _ => None,
        })
        .unwrap_or_else(|| panic!("no entry {key} in {:?}", read(fold)))
}

fn sorted(set: &HashSet<String>) -> Vec<String> {
    let mut v: Vec<String> = set.iter().cloned().collect();
    v.sort();
    v
}

const T1: &str = "537555728357bf76";
const T2: &str = "8b701eb0b9fe4f19";
const T3: &str = "2b2b466e35457fb0";
const T1_PROMPT: &str = "u-clarp-admin-23071c64bf0fc449";
const T1_ANSWER: &str = "live-783d39d132789c798a9a";
const T1_TOOLS: [&str; 2] = ["msg-9dc44519ed4e93bcff29", "msg-d28c2b3f8a1150792e60"];
const FAILED_CALL: &str = "toolu_01RL4aA1EqQJ43Y7Ay7wuYXt";

#[test]
fn a_chat_opened_cold_folds_its_settled_turn_from_log() {
    let rows = real_rows(false);
    let explain = |tool: &serde_json::Map<String, Value>| {
        if tool.get("command").and_then(Value::as_str) == Some("sleep 60") { "Pause execution for 60 seconds.".to_owned() } else { String::new() }
    };
    let folded = present(&rows, None, &Options { explain: Some(&explain), ..options(&[]) });
    assert_eq!(folded.folds.len(), 1, "{:?}", folded.folds);
    let fold = &folded.folds[0];
    assert_eq!(fold.key, format!("live:fold:{T1}"), "the live fold's key, so the takeover is in place");
    assert_eq!(fold.prompt.as_deref(), Some(T1_PROMPT));
    // Without the Host's worked time: from the prompt to the turn's last row
    // (11:48:57.181 → 11:49:07.816).
    assert_eq!(read(fold), ["Ran sleep 60", "Worked for 11s · 2 tools", &format!("row {T1_ANSWER}")]);
    let failed = entry(fold, &format!("live:log:{FAILED_CALL}"));
    assert_eq!((failed.kind, failed.status.as_str()), (Kind::Tool, "failed"), "the failed tool stays in view");
    assert_eq!(failed.secondary, "Pause execution for 60 seconds.", "with its explanation");
    assert!(failed.lines.last().is_some_and(|l| l.contains("Blocked: standalone sleep 60")), "and its output: {:?}", failed.lines);
    let fold_row = entry(fold, &format!("live:fold:{T1}"));
    assert_eq!(fold_row.kind, Kind::Fold);
    assert!(fold_row.expandable && !fold_row.expanded);
    assert_eq!(sorted(&folded.hidden_rows), T1_TOOLS, "the tool rows are not shown as activity");
    assert!(folded.stripped_calls.contains(FAILED_CALL));
}

#[test]
fn an_open_fold_shows_one_row_per_tool() {
    let rows = real_rows(false);
    let folded = present(&rows, None, &options(&[&format!("live:fold:{T1}")]));
    let fold = &folded.folds[0];
    assert_eq!(read(fold), ["Ran sleep 60", "Worked for 11s · 2 tools", "Ran sleep 60", &format!("row {T1_ANSWER}")]);
    assert!(entry(fold, &format!("live:fold:{T1}")).expanded);
    assert_eq!(entry(fold, "live:log:toolu_01NHjm6d6UD9Xncd1t3gSZKG").status, "completed");
}

/// The live view keeps a retired turn's items, so a turn the window showed
/// live keeps its worked time, its reasoning and its keys in history.
fn retired_view() -> LiveView {
    let body = recorded();
    let mut view = LiveView::new();
    view.apply_snapshot(body["live"].as_object().unwrap());
    let mut event = body["next"]["event"].as_object().unwrap().clone();
    event.insert("lseq".into(), json!(42));
    view.apply_event(&event);
    assert_eq!(view.turn().and_then(|t| t["turn_id"].as_str()), Some(T3));
    view
}

#[test]
fn a_turn_seen_live_folds_in_history_as_it_did_live() {
    let view = retired_view();
    let rows = real_rows(true);
    let held: HashSet<String> = ["msg-e60c25b9161ac8b660ca".to_owned()].into();
    let base = Options { open_turn: T3.to_owned(), held_rows: held, busy: true, ..options(&[]) };
    let folded = present(&rows, Some(&view), &base);
    let keys: Vec<&str> = folded.folds.iter().map(|f| f.key.as_str()).collect();
    assert_eq!(keys, [format!("live:fold:{T1}"), format!("live:fold:{T2}")], "the two finished turns fold; the open one is live's");
    let first = &folded.folds[0];
    assert_eq!(read(first), ["Worked for 16s · 2 tools", "Ran sleep 60", &format!("row {T1_ANSWER}")], "the Host's worked time and tool count");
    let failed = entry(first, &format!("live:cl:{FAILED_CALL}"));
    assert_eq!((failed.status.as_str(), failed.meta.as_str()), ("failed", "exit 1 · 0.0s"), "the live item's key and look");
    let open = present(&rows, Some(&view), &Options { expanded: [format!("live:fold:{T1}")].into(), ..base });
    assert_eq!(
        read(&open.folds[0]),
        [
            "Worked for 16s · 2 tools",
            "Thought for 4s: There's a tension here: the task explicitly says not to report to anyone, but th",
            "Ran sleep 60",
            "Thought for 2s: Since foreground sleep is blocked, I'll run this in the background instead to sa",
            "Ran sleep 60",
            &format!("row {T1_ANSWER}"),
        ],
        "open, as the live fold opened"
    );
}

#[test]
fn several_finished_turns_each_fold_and_the_open_turn_is_left_alone() {
    let rows = real_rows(true);
    let folded = present(&rows, None, &Options { open_turn: T3.to_owned(), busy: true, ..options(&[]) });
    assert_eq!(folded.folds.len(), 2);
    let second = &folded.folds[1];
    assert_eq!(second.key, format!("live:fold:{T2}"));
    assert_eq!(second.next.as_deref(), Some("u-clarp-admin-3c48398b85b08a09"));
    // 11:52:12.389 → 11:52:22.707.
    assert_eq!(read(second), ["Worked for 10s · 3 tools", "row live-567165763b2310817088"]);
    let open = present(&rows, None, &Options { open_turn: T3.to_owned(), busy: true, ..options(&[&format!("live:fold:{T2}")]) });
    assert_eq!(read(&open.folds[1]), ["Worked for 10s · 3 tools", "Ran ls /var/tmp/probe", "Read /etc/hostname", "Ran date", "row live-567165763b2310817088"]);
    assert!(!folded.hidden_rows.contains("msg-e60c25b9161ac8b660ca"), "the open turn's running tool row is live's");
    // Without live items the chat's last turn, the agent still working,
    // is not over: it does not fold either.
    let busy = present(&rows, None, &Options { busy: true, ..options(&[]) });
    assert_eq!(busy.folds.len(), 2);
}

fn turn(rows: &[Value]) -> Vec<Message> {
    rows.iter().map(row).collect()
}

fn tool_row(id: &str, at: &str, call: &str, command: &str, status: &str) -> Value {
    json!({"id": id, "role": "assistant", "timestamp": at, "text": "", "origin": "agent",
        "tools": [{"name": "Bash", "summary": command, "status": status, "id": call, "command": command, "result": "ok"}]})
}

fn text_row(id: &str, at: &str, text: &str) -> Value {
    json!({"id": id, "role": "assistant", "timestamp": at, "text": text, "origin": "agent"})
}

fn prompt(id: &str, at: &str, trace: &str) -> Value {
    json!({"id": id, "role": "user", "timestamp": at, "text": "Go.", "origin": "user", "trace_id": trace})
}

#[test]
fn commentary_between_tools_folds_with_them() {
    let rows = turn(&[
        prompt("u1", "2026-10-03T10:00:00Z", "t1"),
        tool_row("a1", "2026-10-03T10:00:02Z", "c1", "make build", "ok"),
        text_row("a2", "2026-10-03T10:00:05Z", "Built. Now the tests."),
        tool_row("a3", "2026-10-03T10:00:09Z", "c2", "make test", "ok"),
        text_row("a4", "2026-10-03T10:01:12Z", "All green."),
    ]);
    let folded = present(&rows, None, &options(&[]));
    let fold = &folded.folds[0];
    assert_eq!(read(fold), ["Worked for 1m 12s · 2 tools", "row a4"]);
    assert_eq!(sorted(&folded.hidden_rows), ["a1", "a2", "a3"], "the commentary folds with the tools");
    let open = present(&rows, None, &options(&["live:fold:t1"]));
    assert_eq!(read(&open.folds[0]), ["Worked for 1m 12s · 2 tools", "Ran make build", "row a2", "Ran make test", "row a4"]);
    assert_eq!(sorted(&open.hidden_rows), ["a1", "a3"], "open, the commentary shows as its row");
}

#[test]
fn a_tool_that_never_finished_in_a_settled_turn_shows_interrupted() {
    let rows = turn(&[
        prompt("u1", "2026-10-03T10:00:00Z", "t1"),
        tool_row("a1", "2026-10-03T10:00:02Z", "c1", "make build", "ok"),
        tool_row("a2", "2026-10-03T10:00:04Z", "c2", "make test", "running"),
        prompt("u2", "2026-10-03T10:00:30Z", "t2"),
    ]);
    let folded = present(&rows, None, &options(&[]));
    let fold = &folded.folds[0];
    assert_eq!(read(fold), ["Worked for 4s · 2 tools", "Stopped make test"]);
    let stopped = entry(fold, "live:log:c2");
    assert_eq!((stopped.status.as_str(), stopped.meta.as_str()), ("interrupted", "interrupted"), "it stays in view");
}

#[test]
fn a_turn_without_tools_does_not_fold() {
    let rows = turn(&[
        prompt("u1", "2026-10-03T10:00:00Z", "t1"),
        text_row("a1", "2026-10-03T10:00:03Z", "Looking."),
        text_row("a2", "2026-10-03T10:00:05Z", "Hello."),
        prompt("u2", "2026-10-03T10:00:30Z", "t2"),
    ]);
    let folded: Folded = present(&rows, None, &options(&[]));
    assert!(folded.folds.is_empty() && folded.hidden_rows.is_empty());
}

#[test]
fn a_reply_carrying_text_and_tools_shows_its_text_and_folds_its_tools() {
    let mut first = tool_row("a1", "2026-10-03T10:00:02Z", "c1", "make build", "ok");
    first["text"] = json!("Building first.");
    let rows = turn(&[prompt("u1", "2026-10-03T10:00:00Z", "t1"), first, text_row("a2", "2026-10-03T10:00:06Z", "Done.")]);
    let folded = present(&rows, None, &options(&[]));
    assert_eq!(read(&folded.folds[0]), ["Worked for 6s · 1 tool", "row a2"]);
    assert!(folded.hidden_rows.contains("a1"), "folded commentary");
    let open = present(&rows, None, &options(&["live:fold:t1"]));
    assert_eq!(read(&open.folds[0]), ["Worked for 6s · 1 tool", "row a1", "Ran make build", "row a2"]);
    assert!(open.stripped_calls.contains("c1") && !open.hidden_rows.contains("a1"), "its text shows, its tool as the tool row");
}
