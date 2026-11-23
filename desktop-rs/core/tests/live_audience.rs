//! A live turn's audience (docs/live-items.md §1.4, docs/protocol.md "Live
//! rows follow the same rule"): in a chat that hides coordination between
//! agents, a turn for other agents the user never saw is not drawn; once the
//! user has seen it, what was shown stays shown when the turn goes back to
//! other agents (`turn_audience_returns`, Rust Host contract 164), and only
//! what it writes for them after that is left out, also after the user
//! steps in again.

use clarp_core::history_fold;
use clarp_core::live::LiveView;
use clarp_core::live_present::{Options, present};
use clarp_core::protocol::Message;
use serde_json::{Value, json};

const T: &str = "t-flip";

fn object(value: Value) -> serde_json::Map<String, Value> {
    value.as_object().unwrap().clone()
}

fn turn(audience: &str) -> Value {
    json!({"op": "turn", "turn": {"turn_id": T, "status": "running", "started_at_ms": 1_000, "audience": audience}})
}

fn message(id: &str, ordinal: i64, phase: &str, text: &str) -> Value {
    json!({"op": "upsert", "id": id, "kind": "message", "rev": 1, "item": {"turn_id": T, "kind": "message", "status": "completed",
        "phase": phase, "ordinal": ordinal, "started_at_ms": 1_000 + ordinal, "text": text, "row_id": format!("row-{id}")}})
}

/// A view fed `events` (each a list of ops) in order after an idle snapshot.
fn fed(events: &[Vec<Value>]) -> LiveView {
    let mut view = LiveView::new();
    view.apply_snapshot(&object(json!({"epoch": "e", "lseq": 0, "activity": {"state": "idle"}, "turn": null, "items": []})));
    for (n, ops) in events.iter().enumerate() {
        let effects = view.apply_event(&object(json!({"type": "live", "epoch": "e", "lseq": n as i64 + 1, "ops": ops})));
        assert!(effects.is_empty(), "event {n} applies");
    }
    view
}

/// The docs' sequence: Bella starts the turn, the user's message is read in,
/// the agent leaves a note and goes on for Bella, the user writes again.
fn sequence() -> Vec<Vec<Value>> {
    vec![
        vec![turn("agents"), message("m0", 0, "commentary", "Bella asked me to look.")],
        vec![turn("user"), message("m1", 1, "commentary", "Checking the build.")],
        vec![turn("agents")],
        vec![message("m5", 5, "final", "Bella, it is green.")],
        vec![turn("user"), message("m7", 7, "final", "Release notes next.")],
    ]
}

fn texts(view: &LiveView, veil: bool) -> Vec<String> {
    let options = Options { veil, ..Options::default() };
    present(view, &[], &options).entries.into_iter().map(|e| e.text).filter(|t| !t.is_empty()).collect()
}

fn veiled(view: &LiveView, id: &str) -> bool {
    view.veiled(view.turn().unwrap(), view.item(id).unwrap())
}

#[test]
fn a_turn_for_agents_the_user_never_saw_is_theirs() {
    let view = fed(&sequence()[..1]);
    assert!(veiled(&view, "m0"));
    assert!(texts(&view, true).is_empty(), "hidden: nothing of it is drawn");
    assert_eq!(texts(&view, false), ["Bella asked me to look."], "shown: drawn as before");
}

#[test]
fn stepping_in_draws_it_from_then_on() {
    let view = fed(&sequence()[..2]);
    assert!(!veiled(&view, "m0") && !veiled(&view, "m1"));
    assert_eq!(texts(&view, true), ["Bella asked me to look.", "Checking the build."]);
}

#[test]
fn back_to_agents_keeps_what_was_shown_and_leaves_out_what_follows() {
    let flipped = fed(&sequence()[..3]);
    assert_eq!(texts(&flipped, true), ["Bella asked me to look.", "Checking the build."], "no gap at the flip");
    let after = fed(&sequence()[..4]);
    assert!(veiled(&after, "m5"));
    assert_eq!(texts(&after, true), ["Bella asked me to look.", "Checking the build."], "the words for Bella are not drawn");
    assert_eq!(texts(&after, false).len(), 3, "a chat that shows coordination draws them");
}

#[test]
fn what_was_written_for_agents_stays_theirs_when_the_user_steps_in_again() {
    let view = fed(&sequence());
    assert!(veiled(&view, "m5") && !veiled(&view, "m7"));
    assert_eq!(texts(&view, true), ["Bella asked me to look.", "Checking the build.", "Release notes next."]);
}

#[test]
fn a_snapshot_after_the_flip_leaves_out_items_that_began_since() {
    let mut view = fed(&sequence()[..3]);
    view.apply_snapshot(&object(json!({"epoch": "e", "lseq": 9, "activity": {"state": "idle"},
        "turn": {"turn_id": T, "status": "running", "started_at_ms": 1_000, "audience": "agents"},
        "items": [
            {"id": "m0", "turn_id": T, "kind": "message", "status": "completed", "phase": "commentary", "ordinal": 0, "text": "Bella asked me to look."},
            {"id": "m1", "turn_id": T, "kind": "message", "status": "completed", "phase": "commentary", "ordinal": 1, "text": "Checking the build."},
            {"id": "m5", "turn_id": T, "kind": "message", "status": "completed", "phase": "final", "ordinal": 5, "text": "Bella, it is green."},
        ]})));
    assert!(!veiled(&view, "m1") && veiled(&view, "m5"));
}

/// The turn retires when the next one starts: its words for Bella, whose
/// `/log` row a hiding read never brings, are not kept as an unfiled reply.
#[test]
fn a_retired_turn_keeps_its_words_for_agents_out() {
    let mut events = sequence();
    events.push(vec![json!({"op": "turn", "turn": {"turn_id": T, "status": "completed", "started_at_ms": 1_000, "ended_at_ms": 9_000, "audience": "user"}})]);
    events.push(vec![json!({"op": "turn", "turn": {"turn_id": "t-next", "status": "running", "started_at_ms": 20_000}})]);
    let view = fed(&events);
    let prompt = Message::from_json(&object(json!({"id": "u1", "role": "user", "text": "Is it green?", "trace_id": T, "timestamp": "1970-01-01T00:00:00Z"})));
    let retained = |veil: bool| -> Vec<String> {
        let options = history_fold::Options { veil, ..history_fold::Options::default() };
        history_fold::present(std::slice::from_ref(&prompt), Some(&view), &options)
            .folds
            .iter()
            .flat_map(|f| f.pieces.iter())
            .filter_map(|p| match p {
                history_fold::Piece::Entry(e) => Some(e.text.clone()),
                history_fold::Piece::Row(_) => None,
            })
            .collect()
    };
    assert_eq!(retained(true), ["Release notes next."]);
    assert_eq!(retained(false).len(), 2);
}
