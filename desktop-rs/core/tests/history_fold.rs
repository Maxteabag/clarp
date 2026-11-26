//! Finished turns fold in history (docs/live-items.md §7.6, §9): over the
//! recorded `/log` rows each settled turn shows one quiet `N steps` row, its
//! hint the Host's worked time and tool count; opened, one row per step;
//! what failed or stopped and every message, commentary too, stay outside
//! it, in their places. Without `log_turn_summary` history keeps its rows.

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
    Options { summaries: true, explanations: true, expanded: expanded.iter().map(|s| (*s).to_owned()).collect::<HashSet<_>>(), ..Options::default() }
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
    // The Host's summary on the turn's last row: worked_ms 16337, 2 tools.
    assert_eq!(read(fold), ["Ran sleep 60", "1 step", &format!("row {T1_ANSWER}")]);
    let failed = entry(fold, &format!("live:log:{FAILED_CALL}"));
    assert_eq!((failed.kind, failed.status.as_str()), (Kind::Tool, "failed"), "the failed tool stays in view");
    // Refused by the agent's runtime: why, in plain words, in place of the
    // explanation of a call that never ran, and no output box with its tags.
    assert!(failed.secondary.starts_with("Blocked: standalone sleep 60.") && !failed.secondary.contains("tool_use_error"), "with why it was refused: {:?}", failed.secondary);
    assert!(failed.lines.is_empty(), "and no output box: {:?}", failed.lines);
    let fold_row = entry(fold, &format!("live:fold:{T1}"));
    assert_eq!(fold_row.kind, Kind::Fold);
    assert!(fold_row.expandable && !fold_row.expanded);
    assert_eq!(sorted(&folded.hidden_rows), T1_TOOLS, "the tool rows are not shown as activity");
    assert!(folded.stripped_calls.contains(FAILED_CALL));
}

/// Hide when done (#653): a finished turn's work row goes, and its tools
/// with it; what failed and the answer stay where they were.
#[test]
fn hide_when_done_leaves_a_finished_turn_without_its_work_row() {
    let rows = real_rows(false);

    let folded = present(&rows, None, &Options { hide_settled: true, ..options(&[]) });

    assert_eq!(folded.folds.len(), 1);
    assert_eq!(read(&folded.folds[0]), ["Ran sleep 60", &format!("row {T1_ANSWER}")], "no work row; the failed tool and the reply stay");
    assert_eq!(sorted(&folded.hidden_rows), T1_TOOLS, "the tool rows are not shown either");
    // Opening it is not possible: a stored open key does not bring it back.
    let open = present(&rows, None, &Options { hide_settled: true, ..options(&[&format!("live:fold:{T1}")]) });
    assert_eq!(read(&open.folds[0]), ["Ran sleep 60", &format!("row {T1_ANSWER}")]);
}

/// The work row reads the Host's /log summary, never what /live saw (#653).
#[test]
fn the_work_row_reads_the_log_summary_not_the_live_turn() {
    let view = retired_view();
    let mut rows = real_rows(true);
    // The /log summary says 11s and 2 tools; the retired live turn 16s.
    let last = rows.iter_mut().find(|r| r.id == T1_ANSWER).unwrap();
    if let Some(turn) = last.turn.as_mut() {
        turn.insert("worked_ms".into(), json!(11_000));
    }
    let base = Options { open_turn: T3.to_owned(), busy: true, ..options(&[]) };
    let folded = present(&rows, Some(&view), &base);
    let fold = &folds(&folded.folds[0])[0];
    assert_eq!((fold.title.as_str(), fold.hint.as_str()), ("3 steps", "Worked for 11s · 1 tool"), "the summary's time, on hover (#888)");
}

#[test]
fn an_open_fold_shows_one_row_per_tool() {
    let rows = real_rows(false);
    let folded = present(&rows, None, &options(&[&format!("live:fold:{T1}")]));
    let fold = &folded.folds[0];
    assert_eq!(read(fold), ["Ran sleep 60", "1 step", "Ran sleep 60", &format!("row {T1_ANSWER}")]);
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
    assert_eq!(read(first), ["3 steps", "Ran sleep 60", &format!("row {T1_ANSWER}")], "one quiet line naming what opens (#888)");
    assert_eq!(folds(first)[0].hint, "Worked for 16s · 1 tool", "the Host's worked time and tool count, on hover");
    let failed = entry(first, &format!("live:cl:{FAILED_CALL}"));
    assert_eq!((failed.status.as_str(), failed.meta.as_str()), ("failed", "exit 1 · 0.0s"), "the live item's key and look");
    let open = present(&rows, Some(&view), &Options { expanded: [format!("live:fold:{T1}")].into(), ..base });
    assert_eq!(
        read(&open.folds[0]),
        [
            "3 steps",
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
    // It ended before the Host summed turns up: no worked time is guessed.
    assert_eq!(read(second), ["3 steps", "row live-567165763b2310817088"]);
    assert_eq!(folds(second)[0].hint, "3 tools");
    let open = present(&rows, None, &Options { open_turn: T3.to_owned(), busy: true, ..options(&[&format!("live:fold:{T2}")]) });
    assert_eq!(read(&open.folds[1]), ["3 steps", "Ran ls /var/tmp/probe", "Read /etc/hostname", "Ran date", "row live-567165763b2310817088"]);
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

/// A row as the Host sends it with `log_turn_summary`: the turn's trace id
/// and the text's phase.
fn of_turn(mut row: Value, trace: &str, phase: Option<&str>) -> Value {
    row["trace_id"] = json!(trace);
    row["phase"] = json!(phase);
    row
}

/// The turn's summary on its last row.
fn summed(mut row: Value, trace: &str, status: &str, worked_ms: i64, tool_count: i64) -> Value {
    row["turn"] = json!({"turn_id": trace, "status": status, "started_at_ms": 1_000, "ended_at_ms": 1_000 + worked_ms, "worked_ms": worked_ms, "tool_count": tool_count});
    row
}

fn prompt(id: &str, at: &str, trace: &str) -> Value {
    json!({"id": id, "role": "user", "timestamp": at, "text": "Go.", "origin": "user", "trace_id": trace})
}

#[test]
fn commentary_between_tools_folds_and_spoken_commentary_stays_above_the_work_row() {
    let rows = turn(&[
        prompt("u1", "2026-10-03T10:00:00Z", "t1"),
        of_turn(text_row("a1", "2026-10-03T10:00:01Z", "Building it first."), "t1", Some("commentary")),
        of_turn(tool_row("a2", "2026-10-03T10:00:02Z", "c1", "make build", "ok"), "t1", None),
        of_turn(tool_row("a3", "2026-10-03T10:00:04Z", "c2", "make lint", "ok"), "t1", None),
        of_turn(text_row("a4", "2026-10-03T10:00:05Z", "Built. Now the tests."), "t1", Some("commentary")),
        of_turn(tool_row("a5", "2026-10-03T10:00:09Z", "c3", "make test", "ok"), "t1", None),
        summed(of_turn(text_row("a6", "2026-10-03T10:01:12Z", "All green."), "t1", Some("final")), "t1", "completed", 72_400, 3),
    ]);
    let folded = present(&rows, None, &options(&[]));
    let fold = &folded.folds[0];
    assert_eq!(read(fold), ["5 steps", "row a6"], "the commentary folds with the work (#653); the answer never");
    assert_eq!(sorted(&folded.hidden_rows), ["a1", "a2", "a3", "a4", "a5"]);
    let open = present(&rows, None, &options(&["live:fold:t1"]));
    assert_eq!(read(&open.folds[0]), ["5 steps", "row a1", "Ran make build", "Ran make lint", "row a4", "Ran make test", "row a6"], "it opens in place");
    assert_eq!(sorted(&open.hidden_rows), ["a2", "a3", "a5"], "open, the commentary rows show where they were, the tools as tool rows");
    // Hide when done: the work row goes, the folded commentary and tools with it.
    let hidden = present(&rows, None, &Options { hide_settled: true, ..options(&[]) });
    assert_eq!(read(&hidden.folds[0]), ["row a6"]);
    assert_eq!(sorted(&hidden.hidden_rows), ["a1", "a2", "a3", "a4", "a5"]);
    // A commentary that carried speech (the user heard it) stays, above the work row,
    // in every mode.
    let mut spoken_rows = rows.clone();
    spoken_rows[4].text = "<speak>Built. Now the tests.</speak>".to_owned();
    let folded = present(&spoken_rows, None, &options(&[]));
    assert_eq!(read(&folded.folds[0]), ["row a4", "4 steps", "row a6"]);
    assert!(!folded.hidden_rows.contains("a4"));
    let hidden = present(&spoken_rows, None, &Options { hide_settled: true, ..options(&[]) });
    assert_eq!(read(&hidden.folds[0]), ["row a4", "row a6"], "what was spoken shows, Hide when done too");
    // Always visible: the commentary stays in its place.
    let always = present(&rows, None, &Options { show_commentary: true, ..options(&[]) });
    assert_eq!(read(&always.folds[0]), ["row a1", "3 steps", "row a4", "row a6"]);
}

#[test]
fn a_tool_that_never_finished_in_a_settled_turn_shows_interrupted() {
    let rows = turn(&[
        prompt("u1", "2026-10-03T10:00:00Z", "t1"),
        of_turn(tool_row("a1", "2026-10-03T10:00:02Z", "c1", "make build", "ok"), "t1", None),
        summed(of_turn(tool_row("a2", "2026-10-03T10:00:04Z", "c2", "make test", "interrupted"), "t1", None), "t1", "interrupted", 4_200, 2),
        prompt("u2", "2026-10-03T10:00:30Z", "t2"),
    ]);
    let folded = present(&rows, None, &options(&[]));
    let fold = &folded.folds[0];
    assert_eq!(read(fold), ["1 step", "Stopped make test"], "the stopped call stays out of the count");
    assert_eq!(folds(fold)[0].hint, "Worked for 4s · 1 tool");
    assert_eq!(entry(fold, "live:fold:t1").status, "interrupted");
    // A turn from before the Host marked them: a tool left running in a
    // turn that is over was stopped too.
    let older = turn(&[
        prompt("u1", "2026-10-03T10:00:00Z", "t1"),
        tool_row("a2", "2026-10-03T10:00:04Z", "c2", "make test", "running"),
        prompt("u2", "2026-10-03T10:00:30Z", "t2"),
    ]);
    // Nothing worked to fold: as live, only the stopped tool shows.
    assert_eq!(read(&present(&older, None, &options(&[])).folds[0]), ["Stopped make test"]);
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
    assert_eq!(read(&folded.folds[0]), ["2 steps", "row a2"], "its text and its tool");
    assert!(folded.hidden_rows.contains("a1"), "its text is commentary: it folds with its tool (#653)");
    let open = present(&rows, None, &options(&["live:fold:t1"]));
    assert_eq!(read(&open.folds[0]), ["2 steps", "row a1", "Ran make build", "row a2"]);
    assert!(open.stripped_calls.contains("c1") && !open.hidden_rows.contains("a1"), "opened, its text shows in place, its tool as the tool row");
}

/// The real Host lands a turn's tool rows once the turn is over, after a
/// message sent meanwhile: they still belong to the turn they were written
/// in (by time), not to the message's.
#[test]
fn tool_rows_landing_after_the_next_prompt_fold_with_their_turn() {
    let body = recorded();
    let log: Vec<&Value> = body["log"]["turns"].as_array().unwrap().iter().collect();
    let next = json!({"id": "u-next", "role": "user", "text": "And now the next step.", "timestamp": "2026-10-03T11:49:30.000Z", "trace_id": "t-next"});
    let rows: Vec<Message> = [log[0], log[3], &next, log[1], log[2]].into_iter().map(row).collect();
    let folded = present(&rows, None, &Options { open_turn: "t-next".to_owned(), busy: true, ..options(&[]) });
    assert_eq!(folded.folds.len(), 1, "{:?}", folded.folds);
    let fold = &folded.folds[0];
    assert_eq!(fold.next.as_deref(), Some("u-next"));
    assert_eq!(read(fold), ["Ran sleep 60", "1 step", &format!("row {T1_ANSWER}")]);
    assert_eq!(sorted(&folded.hidden_rows), T1_TOOLS);
}

#[test]
fn without_turn_summaries_history_keeps_its_rows() {
    let rows = real_rows(true);
    let folded = present(&rows, None, &Options { summaries: false, ..options(&[]) });
    assert!(folded.folds.is_empty() && folded.hidden_rows.is_empty() && folded.stripped_calls.is_empty());
}

/// The Host's phase decides: a turn that stopped after commentary has no
/// answer, its commentary stays in view where it was written, and a
/// summary means the turn is over even while the agent works on.
#[test]
fn the_phase_says_what_is_commentary_and_the_summary_that_the_turn_is_over() {
    let rows = turn(&[
        prompt("u1", "2026-10-03T10:00:00Z", "t1"),
        of_turn(text_row("a1", "2026-10-03T10:00:01Z", "Let me build it."), "t1", Some("commentary")),
        summed(of_turn(tool_row("a2", "2026-10-03T10:00:04Z", "c1", "make build", "ok"), "t1", None), "t1", "completed", 4_000, 1),
    ]);
    let folded = present(&rows, None, &Options { busy: true, ..options(&[]) });
    assert_eq!(read(&folded.folds[0]), ["2 steps"]);
    assert_eq!(sorted(&folded.hidden_rows), ["a1", "a2"], "the commentary folds with the work (#653)");
}

/// /live may begin the next turn while a delayed /log delta still lacks the
/// completed reply. The database can already contain it: these are distinct
/// delivery channels. Never erase the reply already shown to the reader.
#[test]
fn next_send_keeps_completed_reply_until_exact_log_row_arrives() {
    let prompt = row(&json!({"id":"u-first", "role":"user", "text":"First question",
        "trace_id":"first", "timestamp":"2026-01-01T00:00:00Z"}));
    let mut next = row(&json!({"id":"u-next", "role":"user", "text":"Next question",
        "timestamp":"2026-01-01T00:00:03Z"}));
    next.pending = true;
    let mut view = LiveView::new();
    view.apply_snapshot(json!({"epoch":"test", "lseq":1,
        "turn":{"turn_id":"first", "status":"completed", "started_at_ms":1767225600100_i64},
        "items":[{"id":"message-first", "turn_id":"first", "kind":"message",
            "status":"completed", "phase":"final", "ordinal":1, "row_id":"answer-first", "text":"First answer"}]
    }).as_object().unwrap());
    let shown = clarp_core::live_present::present(&view, std::slice::from_ref(&prompt),
        &clarp_core::live_present::Options::default());
    assert!(shown.entries.iter().any(|entry| entry.text == "First answer"));
    view.apply_snapshot(json!({"epoch":"test", "lseq":2,
        "turn":{"turn_id":"next", "status":"running", "started_at_ms":1767225603100_i64},
        "items":[]}).as_object().unwrap());
    let mut rows = vec![prompt, next];
    // Preserve the handoff even when the old turn had no tools/summary.
    let opts = Options::default();
    let missing = present(&rows, Some(&view), &opts);
    assert_eq!(missing.folds.len(), 1, "the prior answer must not disappear on the new turn");
    assert_eq!(missing.folds[0].prompt, None, "the final stays after the old work");
    assert_eq!(missing.folds[0].next.as_deref(), Some("u-next"));
    let Piece::Entry(entry) = &missing.folds[0].pieces[0] else { panic!("retained message entry") };
    assert_eq!(entry.text, "First answer");
    assert_eq!(entry.key, shown.entries[0].key, "keep the already displayed identity");
    assert!(rows[1].pending && rows[1].text == "Next question", "pending delivery stays untouched");
    assert!(present(&rows[1..], Some(&view), &opts).folds.is_empty(),
        "without the original trace-bound prompt, a new/reset conversation must not revive the reply");
    rows.insert(1, row(&json!({"id":"answer-first", "role":"assistant", "kind":"live",
        "trace_id":"first", "text":"First", "timestamp":"2026-01-01T00:00:01Z"})));
    let prefix = present(&rows, Some(&view), &opts);
    assert!(prefix.hidden_rows.contains("answer-first"), "do not duplicate the unfinished prefix");
    let Piece::Entry(entry) = &prefix.folds[0].pieces[0] else { panic!("completed reply retained") };
    assert_eq!(entry.text, "First answer", "never regress to an older prefix on the next send");
    rows[1] = row(&json!({"id":"answer-first", "role":"assistant", "phase":"final",
        "trace_id":"first", "text":"First answer", "timestamp":"2026-01-01T00:00:01Z"}));
    let canonical = present(&rows, Some(&view), &opts);
    assert!(canonical.folds.is_empty(), "the exact canonical row takes over once");
    assert!(!canonical.hidden_rows.contains("answer-first"), "canonical reply stays visible");
    assert_eq!(rows.iter().map(|r|r.id.as_str()).collect::<Vec<_>>(), ["u-first","answer-first","u-next"]);
}

/// A prompt can arrive during the previous turn's work. Retiring its final
/// answer must keep it after that prompt and before the next send, whether
/// that next send is pending or failed; delivery flags are not chronology.
#[test]
fn retained_final_stays_between_prompts_sent_during_and_after_its_reply() {
    let mut view = LiveView::new();
    view.apply_snapshot(json!({"epoch":"ordering", "lseq":1,
        "turn":{"turn_id":"first", "status":"completed", "started_at_ms":1767225600100_i64},
        "items":[{"id":"final", "turn_id":"first", "kind":"message",
            "status":"completed", "phase":"final", "ordinal":1, "row_id":"answer",
            "started_at_ms":1767225602000_i64, "text":"Synthetic final answer"}]
    }).as_object().unwrap());
    view.apply_snapshot(json!({"epoch":"ordering", "lseq":2,
        "turn":{"turn_id":"next", "status":"running", "started_at_ms":1767225604100_i64},
        "items":[]}).as_object().unwrap());
    let mut rows = vec![
        row(&prompt("u-first", "2026-01-01T00:00:00Z", "first")),
        row(&prompt("u-during", "2026-01-01T00:00:01Z", "during")),
        row(&prompt("u-after", "2026-01-01T00:00:04Z", "next")),
    ];
    for failed in [false, true] {
        rows[2].pending = !failed;
        rows[2].delivery_failed = failed;
        let folded = present(&rows, Some(&view), &Options::default());
        assert_eq!(folded.folds.len(), 1);
        assert_eq!(folded.folds[0].next.as_deref(), Some("u-after"),
            "the final must not move above the prompt sent during work");
        let mut order: Vec<&str> = rows.iter().map(|row| row.id.as_str()).collect();
        let at = order.iter().position(|id| Some(*id) == folded.folds[0].next.as_deref()).unwrap();
        order.insert(at, "answer");
        assert_eq!(order, ["u-first", "u-during", "answer", "u-after"]);
        assert_eq!(rows[2].delivery_failed, failed);
        assert_eq!(rows[2].id, "u-after");
    }
}

fn folds(fold: &Fold) -> Vec<&clarp_core::live_present::Entry> {
    fold.pieces.iter().filter_map(|p| match p {
        Piece::Entry(e) if e.kind == Kind::Fold => Some(e),
        _ => None,
    }).collect()
}

/// Arnie's ruling (#653): the count is the calls the row folds. Failed
/// calls keep their own rows and are not counted; the summary's
/// tool_count is not the number shown, its worked_ms is the time.
#[test]
fn the_work_row_counts_the_calls_it_folds_not_the_summary() {
    let rows = turn(&[
        prompt("u1", "2026-10-03T10:00:00Z", "t1"),
        of_turn(tool_row("a1", "2026-10-03T10:00:02Z", "c1", "make build", "ok"), "t1", None),
        summed(of_turn(tool_row("a2", "2026-10-03T10:00:04Z", "c2", "make test", "failed"), "t1", None), "t1", "completed", 4_000, 9),
        prompt("u2", "2026-10-03T10:00:30Z", "t2"),
    ]);
    let folded = present(&rows, None, &options(&[]));
    let fold = &folded.folds[0];
    assert_eq!((folds(fold)[0].title.as_str(), folds(fold)[0].hint.as_str()), ("1 step", "Worked for 4s · 1 tool"), "the folded build, not the summary's 9 nor the failed test");
    assert_eq!(entry(fold, "live:log:c2").status, "failed", "the failed call keeps its row");
}

#[test]
fn a_turn_whose_every_call_failed_has_no_work_row() {
    let rows = turn(&[
        prompt("u1", "2026-10-03T10:00:00Z", "t1"),
        of_turn(tool_row("a1", "2026-10-03T10:00:02Z", "c1", "make build", "failed"), "t1", None),
        summed(of_turn(tool_row("a2", "2026-10-03T10:00:04Z", "c2", "make test", "failed"), "t1", None), "t1", "completed", 11_000, 2),
        prompt("u2", "2026-10-03T10:00:30Z", "t2"),
    ]);
    let folded = present(&rows, None, &options(&[]));
    let fold = &folded.folds[0];
    assert!(folds(fold).is_empty(), "nothing folds, so nothing opens and there is no row (#888): {:?}", read(fold));
    assert_eq!(read(fold).len(), 2, "both failed calls in view: {:?}", read(fold));
}

#[test]
fn neither_a_worked_time_nor_a_folded_call_is_no_work_row() {
    let rows = turn(&[
        prompt("u1", "2026-10-03T10:00:00Z", "t1"),
        tool_row("a1", "2026-10-03T10:00:02Z", "c1", "make build", "failed"),
        prompt("u2", "2026-10-03T10:00:30Z", "t2"),
    ]);
    let folded = present(&rows, None, &options(&[]));
    assert!(folded.folds.iter().all(|f| folds(f).is_empty()), "no work row: {:?}", folded.folds.iter().map(read).collect::<Vec<_>>());
    assert!(folded.folds.iter().any(|f| f.pieces.iter().any(|p| matches!(p, Piece::Entry(e) if e.status == "failed"))), "the failed call shows");
}


/// A spoken acknowledgment (its row's `<speak>` block) stays above the
/// work row, Hide when done too; plain commentary folds (#653).
#[test]
fn a_spoken_acknowledgment_in_history_stays_above_the_work_row() {
    let commentary = |id: &str, at: &str, text: &str| of_turn(text_row(id, at, text), "t1", Some("commentary"));
    let rows = turn(&[
        prompt("u1", "2026-10-03T10:00:00Z", "t1"),
        commentary("a0", "2026-10-03T10:00:01Z", "<speak>On it, checking the build.</speak>"),
        of_turn(tool_row("a1", "2026-10-03T10:00:02Z", "c1", "make build", "ok"), "t1", None),
        commentary("a2", "2026-10-03T10:00:03Z", "Looking at the build log now."),
        of_turn(tool_row("a3", "2026-10-03T10:00:04Z", "c2", "make test", "ok"), "t1", None),
        summed(of_turn(text_row("a4", "2026-10-03T10:00:06Z", "All green."), "t1", Some("final")), "t1", "completed", 6_000, 2),
        prompt("u2", "2026-10-03T10:00:30Z", "t2"),
    ]);
    let quiet = Options { ..options(&[]) };
    let folded = present(&rows, None, &quiet);
    assert_eq!(read(&folded.folds[0]), ["row a0", "3 steps", "row a4"]);
    assert!(folded.hidden_rows.contains("a2") && !folded.hidden_rows.contains("a0"));
    let hidden = present(&rows, None, &Options { hide_settled: true, ..quiet });
    assert_eq!(read(&hidden.folds[0]), ["row a0", "row a4"], "Hide when done keeps the words heard");
}

/// A note to the user (tell-user, kind `note`, no phase) written during a
/// turn stays in view when the turn folds, and the answer is still the answer.
#[test]
fn a_note_to_the_user_never_folds_away() {
    let mut rows = real_rows(false);
    let at = rows.iter().position(|r| r.id == T1_ANSWER).expect("the answer row");
    let mut note = row(&json!({"id": "note-a-tell-1", "role": "assistant", "kind": "note", "origin": "note",
        "text": "I muted the only sound playing.", "trace_id": T1, "revision": 1}));
    note.timestamp = rows[at].timestamp.clone();
    rows.insert(at, note);
    let folded = present(&rows, None, &options(&[]));
    assert_eq!(folded.folds.len(), 1);
    assert!(!folded.hidden_rows.contains("note-a-tell-1"), "the note stays: {:?}", sorted(&folded.hidden_rows));
    assert_eq!(read(&folded.folds[0]), ["Ran sleep 60", "1 step", &format!("row {T1_ANSWER}")], "the fold is as without the note");
}

/// A row's text was written before its tool calls: settled, the turn reads
/// text, then tool, as the live turn drew it, whatever the row's phase.
#[test]
fn a_rows_text_reads_before_its_tool_calls_settled_as_live() {
    for phase in [None, Some("commentary"), Some("final")] {
        let mut first = of_turn(tool_row("a1", "2026-10-03T10:00:02Z", "c1", "make build", "failed"), "t1", phase);
        first["text"] = json!("PRELUDE");
        let rows = turn(&[
            prompt("u1", "2026-10-03T10:00:00Z", "t1"),
            first,
            summed(of_turn(text_row("a2", "2026-10-03T10:00:06Z", "FINALE"), "t1", Some("final")), "t1", "completed", 6_000, 1),
            prompt("u2", "2026-10-03T10:00:30Z", "t2"),
        ]);
        for expanded in [&[][..], &["live:fold:t1"][..]] {
            for show_commentary in [false, true] {
                let folded = present(&rows, None, &Options { show_commentary, ..options(expanded) });
                let read = read(&folded.folds[0]);
                let text = read.iter().position(|p| p == "row a1");
                let tool = read.iter().position(|p| p.ends_with("make build")).unwrap_or_else(|| panic!("{phase:?}: the failed tool shows: {read:?}"));
                if let Some(text) = text {
                    assert!(text < tool, "{phase:?} {expanded:?} {show_commentary}: the text above its tool: {read:?}");
                }
                assert_eq!(read.last().map(String::as_str), Some("row a2"), "{read:?}");
            }
        }
    }
}

/// The run of #827, with its rows as the Host gave them: the turn wrote
/// PRELUDE with its own `ls`, then FINALE. The Host's /log called both
/// rows final; its live items called PRELUDE commentary the moment `ls`
/// began. Grouped, the settled live turn folded PRELUDE into its work row,
/// and history, once the next message took the turn over, showed it again
/// above the row. Both read the turn alike now, in every mode.
#[test]
fn text_written_with_its_tool_reads_the_same_settled_live_and_in_history() {
    const TRACE: &str = "ab4ca986741e0fe7";
    const PRELUDE: &str = "r-011ChPGpst6eXKS6UgKFNeTL";
    const FINALE: &str = "r-011ChPGq2zYRt5BnM63audez";
    const CALL: &str = "toolu_017t44njdj18onwm8LKSn1AA";
    let durable = turn(&[
        json!({"id": "u-tt-claude-1795592693159-1", "role": "user", "origin": "user", "trace_id": TRACE, "view": "conversation",
            "timestamp": "2026-11-25T07:44:53.177Z", "text": "[t1] First write one sentence containing the word PRELUDE. Then run the shell command `ls`. Then reply with only the word FINALE."}),
        json!({"id": PRELUDE, "role": "assistant", "origin": "user", "trace_id": TRACE, "view": "session", "phase": "final",
            "timestamp": "2026-11-25T07:44:58.288Z", "text": "PRELUDE: I'll start by listing the directory contents.",
            "tools": [{"id": CALL, "name": "Bash", "command": "ls", "summary": "ls", "status": "ok", "result": "(Bash completed with no output)"}]}),
        json!({"id": FINALE, "role": "assistant", "origin": "user", "trace_id": TRACE, "view": "conversation", "phase": "final", "session_rows": 1,
            "timestamp": "2026-11-25T07:44:59.703Z", "text": "FINALE",
            "turn": {"turn_id": TRACE, "status": "completed", "started_at_ms": 1795592693177_i64, "ended_at_ms": 1795592699713_i64, "tool_count": 1}}),
    ]);
    let next = row(&json!({"id": "u-tt-claude-1795592707454-2", "role": "user", "origin": "user", "trace_id": "e4d61fa4e638c873",
        "timestamp": "2026-11-25T07:45:07.455Z", "text": "[t2] Reply with only the word NEXT."}));
    let mut history = durable.clone();
    history.push(next);

    // The settled live turn as GET /live had it.
    let item = |id: &str, kind: &str, ordinal: i64, extra: Value| {
        let mut item = json!({"id": id, "kind": kind, "rev": 1, "conv": "c-1", "turn_id": TRACE, "status": "completed", "ordinal": ordinal,
            "started_at_ms": 1795592697461_i64 + ordinal * 100, "ended_at_ms": 1795592697461_i64 + ordinal * 100 + 50});
        item.as_object_mut().unwrap().extend(extra.as_object().unwrap().clone());
        item
    };
    let snapshot = json!({"conv": "c-1", "session": "s", "agent_id": "a", "epoch": "e", "lseq": 1, "server_now_ms": 1795592702000_i64,
        "activity": {"state": "idle", "turn_id": TRACE},
        "turn": {"turn_id": TRACE, "status": "completed", "started_at_ms": 1795592693177_i64, "ended_at_ms": 1795592699713_i64, "tool_count": 1},
        "items": [
            item("cl:msg_011ChPGpst6eXKS6UgKFNeTL:0", "message", 1, json!({"phase": "commentary", "row_id": PRELUDE, "text": "PRELUDE: I'll start by listing the directory contents."})),
            item(&format!("cl:{CALL}"), "tool", 2, json!({"tool": {"name": "Bash", "call_id": CALL, "category": "exec", "group": null, "label": "ls", "command": "ls"}})),
            item("cl:msg_011ChPGq2zYRt5BnM63audez:0", "message", 3, json!({"phase": "final", "row_id": FINALE, "text": "FINALE"})),
        ]});
    let mut view = LiveView::new();
    view.apply_snapshot(snapshot.as_object().unwrap());

    for show_commentary in [false, true] {
        let live_options = clarp_core::live_present::Options { explanations: true, show_commentary, ..Default::default() };
        let live = clarp_core::live_present::present(&view, &durable, &live_options);
        let live_read: Vec<String> = live.entries.iter().map(|e| if e.row.is_empty() { format!("{:?}", e.kind) } else { format!("row {}", e.row) }).collect();
        let folded = present(&history, None, &Options { show_commentary, ..options(&[]) });
        assert_eq!(folded.folds.len(), 1, "{:?}", folded.folds);
        let history_read: Vec<String> = folded.folds[0].pieces.iter().map(|p| match p {
            Piece::Row(id) => format!("row {id}"),
            Piece::Entry(e) => format!("{:?}", e.kind),
        }).collect();
        let expected: Vec<String> = if show_commentary {
            vec![format!("row {PRELUDE}"), "Fold".to_owned(), format!("row {FINALE}")]
        } else {
            vec!["Fold".to_owned(), format!("row {FINALE}")]
        };
        assert_eq!(live_read, expected, "live, show_commentary {show_commentary}");
        assert_eq!(history_read, expected, "history, show_commentary {show_commentary}");
        assert_eq!(folded.hidden_rows.contains(PRELUDE), !show_commentary, "PRELUDE folds with its tool only where commentary folds");
        assert_eq!(live.hidden_rows.contains(&PRELUDE.to_owned()), !show_commentary);
    }
    // Opened, PRELUDE reads in place above its tool, as live.
    let open = present(&history, None, &options(&["live:fold:ab4ca986741e0fe7"]));
    let read = read(&open.folds[0]);
    assert_eq!(read.iter().filter(|p| p.starts_with("row ")).cloned().collect::<Vec<_>>(), [format!("row {PRELUDE}"), format!("row {FINALE}")]);
}
