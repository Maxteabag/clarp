//! What a connection is given of the events it missed (feature `compact_resume`, issue 882).
//!
//! A phone that fell behind with a hundred agents working was cut off, and its reconnect was
//! given up to every kept event since its last one: on 26 Nov 2026, 7 to 13 MB every one to four
//! minutes, which made it fall behind again. Most of those events are older words of the same
//! thing: a chat's `transcript-updated` says its newest head, and an agent's
//! `agent-activity` its newest move. So of what was missed, only the newest of each of those is
//! given; every other event goes as it went, in order and with its own number, so a client's
//! cursor only moves forward.

use std::collections::HashMap;
use std::sync::Arc;

use serde_json::{Value, json};

use crate::hub::Published;

/// What names the events of which only the newest is given: a chat's `transcript-updated`, an
/// agent's `agent-activity`. `None` for every other event.
fn superseded_by(published: &Published) -> Option<(bool, String)> {
    let json = &published.json;
    if !json.contains("\"transcript-updated\"") && !json.contains("\"agent-activity\"") {
        return None;
    }
    let event: Value = serde_json::from_str(json).ok()?;
    let text = |field: &str| event[field].as_str().unwrap_or("").to_string();
    match event["type"].as_str()? {
        "transcript-updated" => {
            // Of each chat: a newer conversation of it says all a client needs, and an event
            // from before its conversation had a name says nothing more.
            let chat = text("session");
            let chat = if chat.is_empty() {
                text("agent_id")
            } else {
                chat
            };
            Some((true, chat))
        }
        "agent-activity" => {
            let agent = text("agent_id");
            let agent = if agent.is_empty() {
                text("session")
            } else {
                agent
            };
            Some((false, agent))
        }
        _ => None,
    }
}

/// The events of `missed` a connection is given, in the order they were given out, and how
/// many were left out because a newer one says the same.
pub fn compacted(missed: Vec<Arc<Published>>) -> (Vec<Arc<Published>>, usize) {
    let keys: Vec<Option<(bool, String)>> = missed.iter().map(|p| superseded_by(p)).collect();
    let mut newest: HashMap<&(bool, String), usize> = HashMap::new();
    for (at, key) in keys.iter().enumerate() {
        if let Some(key) = key {
            newest.insert(key, at);
        }
    }
    let total = missed.len();
    let given: Vec<Arc<Published>> = missed
        .into_iter()
        .zip(&keys)
        .enumerate()
        .filter(|(at, (_, key))| key.as_ref().is_none_or(|key| newest.get(key) == Some(at)))
        .map(|(_, (published, _))| published)
        .collect();
    let left_out = total - given.len();
    (given, left_out)
}

/// The word that comes before what a connection is given of a gap (the plain `agent-roster` that
/// ends every replay comes after it): an `agent-roster` (an
/// older app reads the snapshot again on it, as it always did on a reconnect) that says it
/// stands for a catch-up, whether the gap was whole, and how many of its events are not sent.
/// It has no number, so it moves no cursor.
pub fn marker(truncated: bool, skipped: usize) -> Value {
    json!({ "type": "agent-roster", "resync": true, "truncated": truncated, "skipped": skipped })
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use super::*;

    fn event(id: u64, json: Value) -> Arc<Published> {
        Arc::new(Published {
            id,
            at_ms: 0,
            json: json.to_string(),
        })
    }

    fn updated(id: u64, session: &str, conversation: &str, head: u64) -> Arc<Published> {
        event(
            id,
            json!({"type": "transcript-updated", "session": session, "agent_id": format!("id-{session}"),
                   "conversation_id": conversation, "head_revision": head, "event_id": id}),
        )
    }

    fn activity(id: u64, agent: &str, phase: &str) -> Arc<Published> {
        event(
            id,
            json!({"type": "agent-activity", "agent_id": agent, "session": agent, "phase": phase}),
        )
    }

    fn state(id: u64, session: &str, kind: &str) -> Arc<Published> {
        event(
            id,
            json!({"type": "agent-state", "session": session, "kind": kind}),
        )
    }

    #[test]
    fn only_the_newest_word_of_a_conversation_and_of_an_agents_activity_is_given() {
        let missed = vec![
            updated(1, "rachel", "c1", 3),
            activity(2, "a1", "tool_started"),
            state(3, "rachel", "thinking"),
            updated(4, "rachel", "c1", 4),
            updated(5, "mike", "c9", 1),
            activity(6, "a1", "tool_finished"),
            state(7, "rachel", "done"),
            updated(8, "rachel", "c2", 1),
            event(9, json!({"type": "user-notification", "session": "rachel"})),
            updated(10, "rachel", "c1", 6),
        ];
        let (given, skipped) = compacted(missed);
        let ids: Vec<u64> = given.iter().map(|p| p.id).collect();
        // Every agent-state and every other kind stays; of each chat only its newest word, also
        // across a new conversation of it.
        assert_eq!(ids, [3, 5, 6, 7, 9, 10]);
        assert_eq!(skipped, 4);
        assert!(given.last().unwrap().json.contains("\"head_revision\":6"));
        assert!(
            ids.windows(2).all(|pair| pair[0] < pair[1]),
            "in the order given out"
        );
    }

    #[test]
    fn nothing_to_leave_out_leaves_everything() {
        let missed = vec![
            state(1, "a", "thinking"),
            updated(2, "a", "c", 1),
            activity(3, "a", "x"),
        ];
        let (given, skipped) = compacted(missed.clone());
        assert_eq!(given.len(), 3);
        assert_eq!(skipped, 0);
        assert_eq!(compacted(Vec::new()).1, 0);
    }

    #[test]
    fn the_marker_is_an_unnumbered_roster_word_with_what_the_gap_was() {
        assert_eq!(
            marker(true, 12),
            json!({"type": "agent-roster", "resync": true, "truncated": true, "skipped": 12})
        );
    }
}
