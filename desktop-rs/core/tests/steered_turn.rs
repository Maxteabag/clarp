//! A message steered into a running turn keeps its place against the work
//! (#843): what the turn did before it stays above it, what it did after it
//! and its working line below it, while the turn runs, once it settles and
//! once history holds it. Built from a real Codex run: [s1] runs `sleep 20`
//! (begun 14.145), [s2] is read into the turn (14.269), the turn answers
//! SKUNK, then the waiting [s3] gets its own turn.

use std::collections::HashSet;

use clarp_core::history_fold::{self, Folded, Piece};
use clarp_core::live::LiveView;
use clarp_core::live_present::{Kind, Options, Presented, present};
use clarp_core::protocol::Message;
use serde_json::{Value, json};

const T: &str = "55a4a30d4fefd760";
const S1: &str = "u-tt-codex-1795607109250-1";
const S2: &str = "u-tt-codex-1795607114257-2";
const S3: &str = "u-tt-codex-1795607117262-3";
const SLEEP_ROW: &str = "c-tt-codex-1795607109250-1-exec-151189ae-be14-4e39-98d7-aa5548ea6895";
const ECHO_ROW: &str = "c-tt-codex-1795607109250-1-exec-2a7e";
const ANSWER: &str = "r-tt-codex-1795607109250-1-msg_0bb7086ebb285a7a016b06ca5eaf5087d2a27be27a40e84848";
/// 2026-11-25T11:45:00Z.
const BASE: i64 = 1_795_607_100_000;
const S1_AT: i64 = BASE + 9_251;
const SLEEP_AT: i64 = BASE + 14_145;
const S2_AT: i64 = BASE + 14_269;
/// A step begun after [s2] was read (the variant with work after it).
const ECHO_AT: i64 = BASE + 15_000;
const ANSWER_AT: i64 = BASE + 34_801;
const ENDED_AT: i64 = BASE + 34_819;

fn stamp(ms: i64) -> String {
    chrono::DateTime::from_timestamp_millis(ms).unwrap().format("%Y-%m-%dT%H:%M:%S%.3fZ").to_string()
}

fn row(value: Value) -> Message {
    Message::from_json(value.as_object().unwrap())
}

fn user(id: &str, at: i64, trace: &str, text: &str) -> Message {
    row(json!({"id": id, "role": "user", "timestamp": stamp(at), "trace_id": trace, "text": text}))
}

fn cell_row(id: &str, at: i64, command: &str) -> Message {
    row(json!({"id": id, "role": "assistant", "kind": "tool", "timestamp": stamp(at), "trace_id": T, "text": "",
        "display_cells": [{"id": id, "kind": "command", "title": "Ran", "summary": command, "status": "ok", "lines": [{"label": "", "text": "(no output)", "kind": "muted"}]}]}))
}

fn answer_row() -> Message {
    row(json!({"id": ANSWER, "role": "assistant", "timestamp": stamp(ANSWER_AT), "trace_id": T, "phase": "final", "text": "SKUNK",
        "turn": {"turn_id": T, "status": "completed", "started_at_ms": S1_AT, "ended_at_ms": ENDED_AT, "worked_ms": 25_568, "tool_count": 1}}))
}

/// The chat's `/log` rows: while the turn runs (`settled` false) the prompt,
/// the steered message and the tool rows landed so far; settled, its answer
/// and the next turn's prompt too.
fn log_rows(echo: bool, settled: bool) -> Vec<Message> {
    let mut rows = vec![user(S1, S1_AT, T, "[s1] Run `sleep 20`, then reply SKUNK."), cell_row(SLEEP_ROW, SLEEP_AT, "sleep 20"), user(S2, S2_AT, "c0752913ac9d7621", "[s2] Also put VIPER in it.")];
    if echo {
        rows.push(cell_row(ECHO_ROW, ECHO_AT, "echo VIPER"));
    }
    if settled {
        rows.push(answer_row());
        rows.push(user(S3, ENDED_AT, "21dab8e1db10033f", "[s3] Reply TAPIR."));
    }
    rows
}

fn tool_item(id: &str, call: &str, command: &str, ordinal: i64, at: i64, settled: bool) -> Value {
    let mut item = json!({"id": id, "kind": "tool", "rev": 1, "conv": "c-1", "turn_id": T, "status": if settled { "completed" } else { "running" }, "ordinal": ordinal,
        "started_at_ms": at, "tool": {"name": "exec_command", "call_id": call, "category": "exec", "group": null, "label": command, "command": command}});
    if settled {
        item["ended_at_ms"] = json!(at + 100);
    }
    item
}

/// The live turn: running (its tools running), or settled with its answer.
fn live(echo: bool, settled: bool) -> LiveView {
    live_at(echo, settled, None)
}

/// As `live`, the turn saying how [s2] was read (`read_rows_at`, contract 195).
fn live_at(echo: bool, settled: bool, at: Option<&str>) -> LiveView {
    let mut items = vec![tool_item("cx:sleep", SLEEP_ROW, "sleep 20", 1, SLEEP_AT, settled || echo)];
    if echo {
        items.push(tool_item("cx:echo", ECHO_ROW, "echo VIPER", 2, ECHO_AT, settled));
    }
    if settled {
        items.push(json!({"id": "cx:answer", "kind": "message", "rev": 1, "conv": "c-1", "turn_id": T, "status": "completed", "ordinal": 3,
            "started_at_ms": ANSWER_AT, "ended_at_ms": ANSWER_AT, "phase": "final", "text": "SKUNK", "row_id": ANSWER}));
    }
    let mut turn = json!({"turn_id": T, "status": if settled { "completed" } else { "running" }, "started_at_ms": S1_AT, "tool_count": items.len(), "read_rows": [S2]});
    if settled {
        turn["ended_at_ms"] = json!(ENDED_AT);
        turn["worked_ms"] = json!(25_568);
    }
    if let Some(at) = at {
        turn["read_rows_at"] = json!([at]);
    }
    let snapshot = json!({"conv": "c-1", "session": "s", "agent_id": "a", "epoch": "e", "lseq": 1, "server_now_ms": if settled { ENDED_AT } else { ECHO_AT + 1_000 },
        "activity": {"state": if settled { "idle" } else { "tool" }, "turn_id": T}, "turn": turn, "items": items});
    let mut view = LiveView::new();
    view.apply_snapshot(snapshot.as_object().unwrap());
    view
}

#[derive(Clone, Copy, PartialEq)]
enum Mode {
    Grouped,
    AlwaysVisible,
    Conversation,
}

fn live_options(mode: Mode) -> Options {
    Options {
        explanations: false,
        fold_running: mode != Mode::AlwaysVisible,
        show_commentary: mode == Mode::AlwaysVisible,
        whole_turn: mode == Mode::Conversation,
        ..Options::default()
    }
}

/// What the chat draws while live items hold the turn, top to bottom: rows
/// by id, entries by key (an entry a durable row took over by that row).
fn live_order(p: &Presented, rows: &[Message]) -> Vec<String> {
    let taken: HashSet<&str> = p.entries.iter().map(|e| e.row.as_str()).filter(|r| !r.is_empty()).collect();
    let rest: Vec<&Message> = rows.iter().filter(|r| !taken.contains(r.id.as_str()) && !p.absorbed_rows.contains(&r.id) && !p.hidden_rows.contains(&r.id)).collect();
    let anchors = p.anchors(&rest);
    assert!(anchors.windows(2).all(|w| w[0] <= w[1]), "entries keep their order: {anchors:?}");
    let name = |i: usize| if p.entries[i].row.is_empty() { p.entries[i].key.clone() } else { p.entries[i].row.clone() };
    let mut order = Vec::new();
    let mut next = 0;
    for (index, row) in rest.iter().enumerate() {
        while next < anchors.len() && anchors[next] == index {
            order.push(name(next));
            next += 1;
        }
        order.push(row.id.clone());
    }
    order.extend((next..anchors.len()).map(name));
    order
}

/// What the chat draws once history holds the turn: each fold's entries
/// after its prompt, its rows where they are (as the pane splices them).
fn history_order(rows: &[Message], folded: &Folded) -> Vec<String> {
    let mut order: Vec<String> = rows.iter().filter(|r| !folded.hidden_rows.contains(&r.id)).map(|r| r.id.clone()).collect();
    for fold in &folded.folds {
        let position = |order: &Vec<String>, id: &str| order.iter().position(|o| o == id);
        let first_row = fold.pieces.iter().find_map(|p| if let Piece::Row(id) = p { position(&order, id) } else { None });
        let mut at = fold.prompt.as_deref().and_then(|id| position(&order, id)).map(|i| i + 1).or(first_row).unwrap_or(order.len());
        for piece in &fold.pieces {
            match piece {
                Piece::Row(id) => {
                    if let Some(index) = position(&order, id) {
                        at = index + 1;
                    }
                }
                Piece::Entry(entry) => {
                    order.insert(at, entry.key.clone());
                    at += 1;
                }
            }
        }
    }
    order
}

fn history(rows: &[Message], mode: Mode) -> Folded {
    history_on(rows, mode, false)
}

/// History from a Host that does (`read_into` true) or does not name the
/// turn a message was read into on its row (feature `read_into`, contract 189).
fn history_on(rows: &[Message], mode: Mode, read_into: bool) -> Folded {
    let options = history_fold::Options {
        summaries: true,
        show_commentary: mode == Mode::AlwaysVisible,
        whole_turn: mode == Mode::Conversation,
        read_into,
        ..history_fold::Options::default()
    };
    history_fold::present(rows, None, &options)
}

/// The rows as a `read_into` Host writes them: [s2] names the turn it was
/// read into, `turn`.
fn read_into(mut rows: Vec<Message>, turn: &str) -> Vec<Message> {
    for row in rows.iter_mut().filter(|r| r.id == S2) {
        *row = row_with_read_into(row, turn);
    }
    rows
}

fn row_with_read_into(row: &Message, turn: &str) -> Message {
    row_read_at(row, turn, None)
}

/// [s2] read into `turn` `at` its start or as a steer (contract 195).
fn row_read_at(row: &Message, turn: &str, at: Option<&str>) -> Message {
    let mut value = json!({"id": row.id, "role": row.role, "timestamp": row.timestamp, "trace_id": row.trace_id, "text": row.text});
    value["read_into"] = json!({"trace_id": turn, "turn": 41});
    if let Some(at) = at {
        value["read_into"]["at"] = json!(at);
    }
    self::row(value)
}

fn read_at(mut rows: Vec<Message>, turn: &str, at: &str) -> Vec<Message> {
    for row in rows.iter_mut().filter(|r| r.id == S2) {
        *row = row_read_at(row, turn, Some(at));
    }
    rows
}

fn fold_keys(folded: &Folded) -> Vec<String> {
    folded.folds.iter().map(|f| f.key.clone()).collect()
}

fn before() -> String {
    format!("live:fold:{T}@{S2}")
}

fn working() -> String {
    format!("live:fold:{T}")
}

fn title(p: &Presented, key: &str) -> String {
    p.entries.iter().find(|e| e.key == key).map(|e| e.title.clone()).unwrap_or_default()
}

fn fold_title(folded: &Folded, key: &str) -> String {
    folded.folds.iter().flat_map(|f| &f.pieces).find_map(|p| match p {
        Piece::Entry(e) if e.key == key => Some(e.title.clone()),
        _ => None,
    }).unwrap_or_default()
}

#[test]
fn grouped_the_work_before_a_steered_message_stays_above_it_while_running_settled_and_in_history() {
    let running = present(&live(false, false), &log_rows(false, false), &live_options(Mode::Grouped));
    assert_eq!(live_order(&running, &log_rows(false, false)), [S1, before().as_str(), S2, working().as_str()], "the work so far, the message, Working");
    assert_eq!(title(&running, before().as_str()), "1 tool");
    assert_eq!(title(&running, working().as_str()), "Working");

    let rows = log_rows(false, true);
    let settled = present(&live(false, true), &rows, &live_options(Mode::Grouped));
    let drawn = live_order(&settled, &rows);
    assert_eq!(drawn, [S1, before().as_str(), S2, ANSWER, S3], "settled: the work above the message, the answer below it");
    assert_eq!(title(&settled, before().as_str()), "Worked for 26s · 1 tool");

    let folded = history(&rows, Mode::Grouped);
    assert_eq!(history_order(&rows, &folded), drawn, "history draws it as live did");
    assert_eq!(fold_title(&folded, before().as_str()), "Worked for 26s · 1 tool");
}

#[test]
fn grouped_work_after_the_steered_message_goes_below_it_in_every_state() {
    let running = present(&live(true, false), &log_rows(true, false), &live_options(Mode::Grouped));
    assert_eq!(live_order(&running, &log_rows(true, false)), [S1, before().as_str(), S2, working().as_str()]);
    assert_eq!(title(&running, before().as_str()), "1 tool");
    assert_eq!(title(&running, working().as_str()), "Working · 1 tool", "Working holds what runs after the message");

    let rows = log_rows(true, true);
    let settled = present(&live(true, true), &rows, &live_options(Mode::Grouped));
    let drawn = live_order(&settled, &rows);
    assert_eq!(drawn, [S1, before().as_str(), S2, working().as_str(), ANSWER, S3]);
    assert_eq!((title(&settled, before().as_str()), title(&settled, working().as_str())), ("1 tool".to_owned(), "Worked for 26s · 1 tool".to_owned()), "the worked time on the last work row");

    let folded = history(&rows, Mode::Grouped);
    assert_eq!(history_order(&rows, &folded), drawn);
    assert_eq!((fold_title(&folded, before().as_str()), fold_title(&folded, working().as_str())), ("1 tool".to_owned(), "Worked for 26s · 1 tool".to_owned()));
}

#[test]
fn always_visible_each_step_keeps_its_time_place_against_the_steered_message() {
    let running = present(&live(true, false), &log_rows(true, false), &live_options(Mode::AlwaysVisible));
    assert_eq!(live_order(&running, &log_rows(true, false)), [S1, "live:cx:sleep", S2, "live:cx:echo"], "a step begun before the message stays above it");
    let only_sleep = present(&live(false, false), &log_rows(false, false), &live_options(Mode::AlwaysVisible));
    assert_eq!(live_order(&only_sleep, &log_rows(false, false)), [S1, "live:cx:sleep", S2], "still running, it keeps its place");

    for echo in [false, true] {
        let rows = log_rows(echo, true);
        let settled = present(&live(echo, true), &rows, &live_options(Mode::AlwaysVisible));
        let drawn = live_order(&settled, &rows);
        let folded = history(&rows, Mode::AlwaysVisible);
        assert_eq!(history_order(&rows, &folded), drawn, "history as live, echo {echo}");
        let at = |key: &str| drawn.iter().position(|k| k == key);
        assert!(at(before().as_str()).is_some() && at(before().as_str()) < at(S2) && at(S2) < at(ANSWER), "{drawn:?}");
    }
}

#[test]
fn conversation_view_keeps_the_turn_whole_below_the_steered_message() {
    let running = present(&live(true, false), &log_rows(true, false), &live_options(Mode::Conversation));
    assert_eq!(live_order(&running, &log_rows(true, false)), [S1, S2, working().as_str()], "one Running box below the message");
    assert_eq!(title(&running, working().as_str()), "Working · 2 tools");

    let rows = log_rows(true, true);
    let folded = history(&rows, Mode::Conversation);
    assert_eq!(folded.folds.iter().map(|f| f.key.as_str()).collect::<Vec<_>>(), [working()], "the turn's work stays one piece");
    // Conversation view leaves a finished turn's work out; its reply stays.
    let reply: Vec<String> = history_order(&rows, &folded).into_iter().filter(|id| !id.starts_with("live:")).collect();
    assert_eq!(reply, [S1, S2, ANSWER, S3]);
    let settled = present(&live(true, true), &rows, &live_options(Mode::Conversation));
    assert!(settled.entries.iter().all(|e| e.kind != Kind::Fold || e.key == working()));
}

#[test]
fn a_part_key_names_its_turn() {
    assert_eq!(clarp_core::chat_view::fold_turn(before().as_str()), T);
    assert_eq!(clarp_core::chat_view::fold_turn(working().as_str()), T);
}

#[test]
fn a_row_parses_read_into_as_its_turn_id() {
    let rows = read_into(log_rows(false, false), T);
    assert_eq!(rows.iter().find(|r| r.id == S2).map(|r| r.read_into.as_str()), Some(T));
    assert!(rows.iter().filter(|r| r.id != S2).all(|r| r.read_into.is_empty()), "absent on every other row");
}

#[test]
fn with_read_into_history_splits_at_the_row_read_into_the_turn_as_live_does() {
    for echo in [false, true] {
        let rows = read_into(log_rows(echo, true), T);
        let settled = present(&live(echo, true), &rows, &live_options(Mode::Grouped));
        let drawn = live_order(&settled, &rows);
        let folded = history_on(&rows, Mode::Grouped, true);
        assert_eq!(history_order(&rows, &folded), drawn, "history as live, echo {echo}");
        assert!(fold_keys(&folded).contains(&before()), "the work before [s2] folds above it: {:?}", fold_keys(&folded));
    }
}

#[test]
fn with_read_into_a_message_stamped_inside_the_turn_without_it_does_not_split_it() {
    // [s2] was written while the turn worked, yet the Host says it was not
    // read into it (it waited, or a fork answered it): no cut.
    for rows in [log_rows(true, true), read_into(log_rows(true, true), "another-turn")] {
        let folded = history_on(&rows, Mode::Grouped, true);
        assert_eq!(fold_keys(&folded), [working()], "the turn's work stays one piece");
        let order = history_order(&rows, &folded);
        let at = |key: &str| order.iter().position(|k| k == key);
        assert!(at(working().as_str()) < at(S2), "the work stays above the message: {order:?}");
    }
}

#[test]
fn an_older_host_keeps_splitting_by_time() {
    let rows = log_rows(true, true);
    let folded = history_on(&rows, Mode::Grouped, false);
    assert_eq!(fold_keys(&folded), [before(), working()], "a row written inside the turn's span cuts it");
}

#[test]
fn with_read_into_a_turn_begun_without_a_trace_splits_at_its_turn_id() {
    // The turn's own message was sent without a trace: its row says "",
    // while the turn, its rows and read_into say `turn-<id>`.
    const TT: &str = "turn-tt-codex-1795607109250-1";
    let mut rows = read_into(log_rows(true, true), TT);
    for row in rows.iter_mut() {
        if row.id == S1 {
            row.trace_id = String::new();
        } else if row.trace_id == T {
            row.trace_id = TT.to_owned();
        }
        if let Some(turn) = row.turn.as_mut() {
            turn.insert("turn_id".into(), json!(TT));
        }
    }
    let folded = history_on(&rows, Mode::Grouped, true);
    let before = format!("live:fold:{TT}@{S2}");
    let working = format!("live:fold:{TT}");
    assert_eq!(fold_keys(&folded), [before.clone(), working.clone()]);
    assert_eq!(history_order(&rows, &folded), [S1, before.as_str(), S2, working.as_str(), ANSWER, S3]);
}

#[test]
fn a_message_read_at_the_turns_start_is_no_split_point_a_steer_is() {
    // Contract 195: only a steer splits the work; one read at the start
    // belongs with the turn's own message.
    let start = read_at(log_rows(true, true), T, "start");
    assert_eq!(start.iter().find(|r| r.id == S2).map(|r| r.read_into_at.as_str()), Some("start"));
    assert_eq!(fold_keys(&history_on(&start, Mode::Grouped, true)), [working()], "read at the start: one piece");
    let steer = read_at(log_rows(true, true), T, "steer");
    assert!(fold_keys(&history_on(&steer, Mode::Grouped, true)).contains(&before()), "a steer splits");

    // Live: read_rows_at says the same word per read_rows entry.
    for (word, split) in [("start", false), ("steer", true)] {
        let rows = log_rows(true, false);
        let running = present(&live_at(true, false, Some(word)), &rows, &live_options(Mode::Grouped));
        let keys: Vec<String> = running.entries.iter().map(|e| e.key.clone()).collect();
        assert_eq!(keys.contains(&before()), split, "live, read at {word}: {keys:?}");
    }
}
