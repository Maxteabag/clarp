//! Conversation and Session views (#702, feature `chat_view`): the toggle's
//! state, which rows the Conversation view keeps, which reply opens its
//! turn's work, and the Running box, as the iOS app has them.

use std::collections::HashSet;

use clarp_core::chat_view::{self, Choice, Setting};
use clarp_core::live_present::{Entry, Kind, Presented};
use clarp_core::protocol::Message;
use serde_json::json;

fn row(value: serde_json::Value) -> Message {
    Message::from_json(value.as_object().unwrap())
}

fn turn() -> Vec<Message> {
    vec![
        row(json!({"id": "ask", "role": "user", "origin": "user", "text": "Ready?", "trace_id": "t1", "view": "conversation"})),
        row(json!({"id": "note", "role": "assistant", "text": "Checking.", "phase": "commentary", "trace_id": "t1", "view": "session"})),
        row(json!({"id": "tool", "role": "assistant", "text": "", "trace_id": "t1", "view": "session"})),
        row(json!({"id": "reply", "role": "assistant", "text": "Yes.", "phase": "final", "trace_id": "t1", "view": "conversation", "session_rows": 2})),
        row(json!({"id": "a2a", "role": "user", "origin": "agent", "sender_agent_id": "x", "text": "Hi", "trace_id": "t2", "view": "session"})),
    ]
}

#[test]
fn a_host_value_reads_session_unless_it_says_conversation() {
    assert_eq!(Choice::decoded(""), None);
    assert_eq!(Choice::decoded("Conversation"), Some(Choice::Conversation));
    assert_eq!(Choice::decoded("whatever"), Some(Choice::Session));
    assert_eq!(chat_view::MENU.map(|(_, l)| l), ["Follow Settings", "Conversation", "Session"]);
    assert_eq!(chat_view::menu_choice("default"), None);
}

#[test]
fn the_toggle_writes_keeps_a_refusal_and_gives_way_to_another_device() {
    let mut s = Setting::default();
    assert_eq!(s.effective(false), Choice::Session);
    assert_eq!(s.effective(true), Choice::Conversation, "Clean chat");
    let g = s.choose(Choice::Conversation).unwrap();
    assert_eq!(s.effective(false), Choice::Conversation, "shows at once");
    s.accepted(g, Some(Choice::Conversation));
    assert_eq!((s.local, s.pending), (Some(Choice::Conversation), None));
    assert_eq!(s.choose(Choice::Conversation), None, "nothing changes");
    let g2 = s.choose(Choice::Session).unwrap();
    s.accepted(g, None);
    assert_eq!(s.pending, Some(Choice::Session), "a late answer to an older write changes nothing");
    s.refused_write(g2);
    assert!(s.refused && s.local == Some(Choice::Session));
    s.host_read(Some(Choice::Session));
    assert_eq!(s.local, Some(Choice::Session), "the echo keeps it");
    s.host_read(Some(Choice::Conversation));
    assert_eq!(s.own(), None, "another device's choice replaces this one's");
    assert_eq!(s.effective(false), Choice::Conversation);
    s.choose(Choice::Session);
    assert!(s.clear_own() && s.own().is_none());
}

#[test]
fn conversation_keeps_the_users_words_and_replies_and_offers_the_work() {
    let rows = turn();
    assert!(chat_view::tagged(&rows));
    let p = chat_view::project(&rows, None, &HashSet::new());
    let hidden: HashSet<&str> = p.hidden.iter().map(String::as_str).collect();
    assert_eq!(hidden, HashSet::from(["note", "tool", "a2a"]));
    assert_eq!(p.work_item("reply"), Some((chat_view::SHOW_WORK, "work:t1")));
    assert_eq!(p.work_item("ask"), None);
    let open = chat_view::project(&rows, None, &HashSet::from(["work:t1".to_owned()]));
    assert!(!open.hidden.contains("note") && open.hidden.contains("a2a"), "Show work opens that turn only");
    assert_eq!(open.work_item("reply").unwrap().0, chat_view::HIDE_WORK);
    let running = chat_view::project(&rows, Some("t1"), &HashSet::from(["work:t1".to_owned()]));
    assert!(running.hidden.contains("note") && running.work_item("reply").is_none(), "a running turn offers no work");
    // An own message not read back yet has no tag and is the user's.
    assert!(chat_view::is_conversation(&row(json!({"id": "u", "role": "user", "text": "x"}))));
    assert!(!chat_view::is_conversation(&row(json!({"id": "r", "role": "assistant", "text": "x"}))));
}

#[test]
fn the_running_turn_is_the_live_one_or_the_newest_prompts_until_its_anchor() {
    let rows = turn();
    assert_eq!(chat_view::running(&rows, Some("t9"), true, false).as_deref(), Some("t9"));
    assert_eq!(chat_view::running(&rows, None, false, false), None);
    assert_eq!(chat_view::running(&rows, None, false, true).as_deref(), Some("t2"));
    assert_eq!(chat_view::running(&rows[..4], None, false, true), None, "its anchor says it ended");
}

#[test]
fn a_running_turn_is_one_running_box_that_opens_to_its_steps() {
    let tool = Entry { key: "live:cl:a".into(), kind: Kind::Tool, title: "Ran ls".into(), ..Entry::default() };
    let words = Entry { key: "live:cl:m".into(), kind: Kind::Message, text: "Looking.".into(), ..Entry::default() };
    let mut p = Presented { turn_id: "t1".into(), entries: vec![words.clone(), tool.clone()], ..Presented::default() };
    chat_view::conversation_live(&mut p, true, "● Running ls · 0:02", false, &|_| false);
    assert_eq!(p.entries.len(), 1);
    assert_eq!((p.entries[0].key.as_str(), p.entries[0].title.as_str()), ("live:fold:t1", "Running ls…"));
    assert!(p.entries[0].expandable && !p.entries[0].expanded);
    let fold = Entry { key: "live:fold:t1".into(), kind: Kind::Fold, expanded: true, ..Entry::default() };
    let mut open = Presented { turn_id: "t1".into(), entries: vec![words.clone(), fold, tool.clone()], ..Presented::default() };
    chat_view::conversation_live(&mut open, true, "", false, &|_| false);
    assert_eq!(open.entries.iter().map(|e| e.key.as_str()).collect::<Vec<_>>(), ["live:fold:t1", "live:cl:a"]);
    assert_eq!(open.entries[0].title, "Running…");
    // Ended: only what the conversation keeps.
    let reply = Entry { key: "live:cl:r".into(), kind: Kind::Message, row: "reply".into(), ..Entry::default() };
    let mut ended = Presented { turn_id: "t1".into(), entries: vec![words, tool, reply], ..Presented::default() };
    chat_view::conversation_live(&mut ended, false, "", false, &|id| id == "reply");
    assert_eq!(ended.entries.iter().map(|e| e.key.as_str()).collect::<Vec<_>>(), ["live:cl:r"]);
}
