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

/// A Host that newly lists `conversation_view_grows`: its cached chats are
/// dropped and read afresh, once; a Host already read so keeps its cache.
#[test]
fn a_host_whose_conversation_view_grows_has_its_cached_chats_read_afresh_once() {
    use clarp_core::transcript_cache::TranscriptCache;
    let host = Host::start("view-grows");
    host.control("/__control/chat-view", json!({"on": true, "grows": true}));
    let dir = std::env::temp_dir().join(format!("clarp-grows-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    let cache = TranscriptCache::new(&dir);
    let base = clarp_core::settings::normalized_base_url(&host.base);
    let snapshot = json!({"turns": [{"id": "old", "role": "assistant", "text": "cached"}], "conversation_id": "c"});
    cache.save(&base, "rachel", snapshot.as_object().unwrap()).unwrap();
    let mut d = Driver::with_transcript_cache(&host.base, &dir);
    d.connect();
    d.until("the Host's features are read", |e| e.settings().boolean(&format!("chatView/growsRead/{base}"), false));
    assert!(cache.load(&base, "rachel").get("turns").and_then(|t| t.as_array()).is_none_or(|t| t.iter().all(|r| r["id"] != "old")),
        "the chat cached before is not kept as it was");
    let _ = std::fs::remove_dir_all(&dir);
}

/// `helper_conversation` (contract 175) reads the chats held from before
/// afresh once too, recorded apart from `conversation_view_grows`.
#[test]
fn a_host_that_shows_helpers_conversations_has_its_cached_chats_read_afresh_once() {
    use clarp_core::transcript_cache::TranscriptCache;
    let host = Host::start("helper-conversation");
    host.control("/__control/chat-view", json!({"on": true, "helper": true}));
    let dir = std::env::temp_dir().join(format!("clarp-helper-conv-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    let cache = TranscriptCache::new(&dir);
    let base = clarp_core::settings::normalized_base_url(&host.base);
    let snapshot = json!({"turns": [{"id": "old", "role": "assistant", "text": "cached"}], "conversation_id": "c"});
    cache.save(&base, "rachel", snapshot.as_object().unwrap()).unwrap();
    let mut d = Driver::with_transcript_cache(&host.base, &dir);
    d.connect();
    d.until("the Host's features are read", |e| e.settings().boolean(&format!("chatView/helperConversationRead/{base}"), false));
    assert!(!d.engine.settings().boolean(&format!("chatView/growsRead/{base}"), false), "each feature is recorded on its own");
    assert!(cache.load(&base, "rachel").get("turns").and_then(|t| t.as_array()).is_none_or(|t| t.iter().all(|r| r["id"] != "old")),
        "the chat cached before is not kept as it was");
    let _ = std::fs::remove_dir_all(&dir);
}
