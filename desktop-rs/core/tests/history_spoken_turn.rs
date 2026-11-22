//! A settled spoken turn in history (docs/live-items.md §7.6): the
//! acknowledgment the agent says first stays above the turn's work and its
//! answer, and the turn's rows read in the fold's order even where the
//! chat's own order differs (`history_fold::turn_order`).

use std::collections::HashSet;

use clarp_core::history_fold::{Fold, Options, Piece, present, turn_order};
use clarp_core::protocol::Message;
use serde_json::{Value, json};

fn row(value: &Value) -> Message {
    Message::from_json(value.as_object().unwrap())
}

fn options(expanded: &[&str]) -> Options<'static> {
    Options { summaries: true, explanations: true, expanded: expanded.iter().map(|s| (*s).to_owned()).collect::<HashSet<_>>(), ..Options::default() }
}

fn read(fold: &Fold) -> Vec<String> {
    fold.pieces
        .iter()
        .map(|p| match p {
            Piece::Row(id) => format!("row {id}"),
            Piece::Entry(e) => e.title.clone(),
        })
        .collect()
}

/// A spoken turn as `/log` holds it: the prompt, the acknowledgment the
/// agent speaks first (carrying the turn's exploration cell) and the answer
/// with the turn's summary.
fn spoken_rows() -> Vec<Message> {
    let s = 1791100000000_i64;
    let iso = |ms: i64| chrono::DateTime::from_timestamp_millis(ms).unwrap().format("%Y-%m-%dT%H:%M:%S%.3fZ").to_string();
    [
        json!({"id": "u-voice", "role": "user", "text": "Add the other host too", "timestamp": iso(s), "trace_id": "tr-voice", "revision": 9}),
        json!({"id": "live-ack", "role": "assistant", "kind": "commentary", "phase": "commentary", "text": "On it, let me look.",
            "timestamp": iso(s + 2_000), "trace_id": "tr-voice", "revision": 12, "activity_count": 2,
            "display_cells": [{"id": "exploration-call-1-call-2", "kind": "exploration", "title": "Explored", "lines": []}]}),
        json!({"id": "live-answer", "role": "assistant", "kind": "final_answer", "phase": "final", "text": "Which host do you mean?",
            "timestamp": iso(s + 176_000), "trace_id": "tr-voice", "revision": 14,
            "turn": {"turn_id": "tr-voice", "status": "completed", "started_at_ms": s, "ended_at_ms": s + 185_000, "worked_ms": 185_000, "tool_count": 2}}),
    ]
    .iter()
    .map(row)
    .collect()
}

#[test]
fn a_spoken_acknowledgment_stays_above_the_work_and_the_answer_in_history() {
    let rows = spoken_rows();
    for expanded in [&[][..], &["live:fold:tr-voice"][..]] {
        let folded = present(&rows, None, &options(expanded));
        assert_eq!(folded.folds.len(), 1, "{:?}", folded.folds);
        let read = read(&folded.folds[0]);
        assert_eq!(read.first().map(String::as_str), Some("row live-ack"), "{read:?}");
        assert!(read[1].starts_with("Worked for 3m 5s"), "{read:?}");
        assert_eq!(read.last().map(String::as_str), Some("row live-answer"), "{read:?}");
        assert!(folded.stripped_calls.contains("exploration-call-1-call-2"), "the cell is the fold's, not the row's");
    }
    // The chat holds the answer above the acknowledgment (inserted before a
    // row whose revision rose meanwhile): the rows take the fold's order in
    // the places they hold.
    let folded = present(&rows, None, &options(&[]));
    let out_of_order = ["u-voice", "live-answer", "live-ack"];
    assert_eq!(turn_order(&folded.folds[0], |id| out_of_order.iter().position(|i| *i == id)), Some((vec![1, 2], vec![2, 1])));
    let in_order = ["u-voice", "live-ack", "live-answer"];
    assert_eq!(turn_order(&folded.folds[0], |id| in_order.iter().position(|i| *i == id)), None);
}
