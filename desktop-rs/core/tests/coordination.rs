//! Coordination between agents (#653, feature `hidden_coordination`): the
//! `/log` parameters, the reload rule and the quiet line's words, as the
//! iOS app has them.

use clarp_core::conversation::{Conversation, LoadKind};
use clarp_core::coordination::{self, Count};
use serde_json::{Value, json};

fn object(value: Value) -> serde_json::Map<String, Value> {
    value.as_object().unwrap().clone()
}

#[test]
fn the_line_says_how_many_and_with_whom() {
    let count = |messages, names: &[&str], capped| Count { messages, exchanges: 1, agents: names.iter().map(|n| (*n).to_owned()).collect(), capped };
    assert_eq!(coordination::line(&count(1, &[], false)), "1 message between agents");
    assert_eq!(coordination::line(&count(3, &["Arnold"], false)), "3 messages between agents, with Arnold");
    assert_eq!(coordination::line(&count(4, &["Arnold", "Hazel"], false)), "4 messages between agents, with Arnold and Hazel");
    assert_eq!(coordination::line(&count(5, &["Arnold", "Hazel", "Nori"], false)), "5 messages between agents, with Arnold, Hazel and 1 other");
    assert_eq!(coordination::line(&count(2, &["", ""], false)), "2 messages between agents, with 2 others");
    assert_eq!(coordination::line(&count(100, &[], true)), "100+ messages between agents");
    assert_eq!(coordination::line(&count(0, &[], false)), "Work between agents");
    let parsed = Count::from_json(&object(json!({"messages": 2, "exchanges": 1, "agents": [{"agent_id": "a", "name": "Arnold"}], "capped": false})));
    assert_eq!(coordination::line(&parsed), "2 messages between agents, with Arnold");
}

#[test]
fn reads_ask_auto_or_show_and_a_delta_says_what_it_holds() {
    assert!(coordination::query(false, false, true, None).is_empty(), "a Host without the feature: as before");
    assert_eq!(coordination::query(true, false, false, None), [("coordination", "auto")]);
    assert_eq!(coordination::query(true, true, false, None), [("coordination", "show")]);
    let hidden = object(json!({"hidden": true, "source": "global", "visibility": "hide"}));
    assert_eq!(coordination::query(true, false, true, Some(&hidden)), [("coordination", "auto"), ("coordination_read", "hide")]);
    assert_eq!(coordination::query(true, false, true, None), [("coordination", "auto"), ("coordination_read", "show")], "a read without the echo held every row");
}

#[test]
fn a_chat_read_under_another_setting_is_read_again() {
    let hidden = object(json!({"visibility": "hide"}));
    assert!(coordination::needs_reload(true, false, Some(&hidden), "show"));
    assert!(!coordination::needs_reload(true, false, Some(&hidden), "hide"));
    assert!(coordination::needs_reload(true, false, None, "hide"));
    assert!(!coordination::needs_reload(true, true, Some(&hidden), "show"), "never during a visit that shows them");
    assert!(!coordination::needs_reload(true, false, Some(&hidden), ""), "nor when the value is not known");
    assert!(!coordination::needs_reload(false, false, None, "hide"));
    assert!(coordination::hides("HIDE") && !coordination::hides("whatever") && !coordination::hides(""));
}

#[test]
fn the_conversation_keeps_the_echo_and_the_count_after_the_newest_row() {
    let mut conversation = Conversation::new();
    let page = object(json!({
        "conversation_id": "c1", "latest_revision": 4, "has_more": false,
        "turns": [{"id": "own", "role": "user", "text": "Hi", "revision": 4,
                   "hidden_before": {"messages": 2, "exchanges": 1, "agents": [{"agent_id": "a", "name": "Arnold"}], "capped": false}}],
        "coordination": {"hidden": true, "source": "global", "visibility": "hide"},
        "hidden_after": {"messages": 1, "exchanges": 1, "agents": [], "capped": false},
    }));
    conversation.apply_log(&page, LoadKind::Tail);
    assert_eq!(coordination::read_visibility(conversation.coordination()), "hide");
    assert_eq!(conversation.hidden_after().and_then(|a| a.get("messages")), Some(&json!(1)));
    assert!(conversation.rows()[0].hidden_before.is_some());
    // The cache keeps them, so a delta after a restart says what it holds.
    let mut restored = Conversation::new();
    assert!(restored.restore_cache_snapshot(&conversation.cache_snapshot()));
    assert_eq!(coordination::read_visibility(restored.coordination()), "hide");
    assert!(restored.rows()[0].hidden_before.is_some());
    // A delta without anything left out at the end clears the count.
    let delta = object(json!({"conversation_id": "c1", "latest_revision": 5, "turns": [], "coordination": {"hidden": true, "source": "global", "visibility": "hide"}}));
    conversation.apply_log(&delta, LoadKind::Delta);
    assert!(conversation.hidden_after().is_none());
    // A page read for the visit is never cached.
    assert!(coordination::requested(Some(&object(json!({"source": "request"})))));
}
