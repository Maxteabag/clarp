//! Conversation and Session views against the fake Host (#702, feature
//! `chat_view`): a chat's own choice is written to its Host, another
//! device's choice replaces it, a refusal keeps it here and says so, and C
//! shows the chat the other way.

mod common;

use common::{Driver, Host};
use serde_json::json;

#[test]
fn a_chats_view_is_written_kept_given_way_and_refused() {
    let host = Host::start("chat-view");
    host.control("/__control/chat-view", json!({"on": true}));
    let mut d = Driver::new(&host.base);
    d.connect();
    d.until("the feature", |e| e.chat_view_offered("rachel"));
    assert_eq!(e_view(&d), "session", "a chat follows Settings: Session");

    // C: the other view, as the chat's own choice, written to the Host.
    assert!(d.engine.toggle_chat_view("rachel"));
    assert_eq!(e_view(&d), "conversation", "it shows at once");
    d.until("the write", |_| !host.requests("POST", "/agent-chat-view").is_empty());
    assert_eq!(host.requests("POST", "/agent-chat-view")[0]["body"], json!({"session": "rachel", "chat_view": "conversation"}));
    d.settle(std::time::Duration::from_millis(300));
    assert_eq!(d.engine.chat_view_menu_value("rachel"), "conversation", "the echo of its own write keeps it");

    // Another device chooses Session: this device's own choice gives way.
    host.control("/__control/event", json!({"type": "agent-roster", "kind": "chat_view", "session": "rachel", "chat_view": "session"}));
    d.until("another device's choice", |e| e.chat_view_menu_value("rachel") == "default");

    // A refused write keeps the choice on this device and says so.
    host.control("/__control/chat-view", json!({"refuse": true}));
    d.engine.set_chat_view("rachel", "conversation");
    d.until("the refusal", |e| e.error().starts_with("Not saved on"));
    assert_eq!(e_view(&d), "conversation");
    assert_eq!(d.engine.chat_view_menu_value("rachel"), "conversation");
}

#[test]
fn without_the_feature_nothing_changes() {
    let host = Host::start("chat-view-off");
    let mut d = Driver::new(&host.base);
    d.connect();
    assert!(!d.engine.toggle_chat_view("rachel"));
    d.engine.set_chat_view("rachel", "conversation");
    d.settle(std::time::Duration::from_millis(200));
    assert_eq!(e_view(&d), "session");
    assert!(host.requests("POST", "/agent-chat-view").is_empty());
}

fn e_view(d: &Driver) -> &'static str {
    d.engine.chat_view("rachel")
}
