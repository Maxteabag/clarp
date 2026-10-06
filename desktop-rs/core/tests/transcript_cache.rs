use clarp_core::conversation::{Conversation, LoadKind};
use clarp_core::transcript_cache::TranscriptCache;
use serde_json::json;

fn ids(conversation: &Conversation) -> Vec<String> {
    conversation.rows().iter().map(|m| m.id.clone()).collect()
}

fn log(value: serde_json::Value) -> clarp_core::json::Object {
    value.as_object().cloned().unwrap()
}

#[test]
fn transcript_cache_restores_durable_rows_without_stale_regression() {
    let directory = std::env::temp_dir().join(format!("clarp-transcript-cache-{}", std::process::id()));
    let cache = TranscriptCache::new(&directory);
    let mut original = Conversation::default();
    original.open_session("rachel");
    original.apply_log(
        &log(json!({"conversation_id": "conversation-1", "latest_revision": 2, "has_more": true, "turns": [
            {"id": "one", "role": "assistant", "text": "First", "revision": 1},
            {"id": "two", "role": "assistant", "text": "Second", "revision": 2}]})),
        LoadKind::Tail,
    );
    original.add_optimistic("pending", "Do not persist");
    original.mark_delivery_failed("pending");
    original.show_transient_thinking("Rachel");

    let snapshot = original.cache_snapshot();
    assert_eq!(snapshot["turns"].as_array().unwrap().len(), 3, "the failed delivery is kept, the thinking row is not");
    cache.save("https://host-a.example", "rachel", &snapshot).unwrap();
    assert!(cache.load("https://host-b.example", "rachel").is_empty(), "never restored into another Host");

    let mut restored = Conversation::default();
    restored.open_session("rachel");
    assert!(restored.restore_cache_snapshot(&cache.load("https://host-a.example", "rachel")));
    assert_eq!(ids(&restored), ["one", "two", "u-pending"]);
    assert!(restored.rows()[2].delivery_failed);
    assert_eq!(restored.latest_revision(), 2);
    assert!(restored.has_more());

    // A partial or older tail may refresh fields, but must not truncate a
    // fuller cache or move the revision cursor backwards.
    restored.apply_log(
        &log(json!({"conversation_id": "conversation-1", "latest_revision": 1, "turns": [
            {"id": "one", "role": "assistant", "text": "First", "revision": 1}]})),
        LoadKind::Tail,
    );
    assert_eq!(ids(&restored), ["one", "two", "u-pending"]);
    assert_eq!(restored.latest_revision(), 2);

    restored.apply_log(&log(json!({"conversation_id": "", "turns": [], "latest_revision": 0})), LoadKind::Tail);
    assert!(restored.rows().is_empty());
    assert_eq!(restored.conversation_id(), "");
    assert_eq!(restored.latest_revision(), 0);

    cache.remove("https://host-a.example", "rachel");
    assert!(cache.load("https://host-a.example", "rachel").is_empty());
    std::fs::remove_dir_all(directory).ok();
}

#[test]
fn damaged_or_foreign_files_restore_nothing() {
    let directory = std::env::temp_dir().join(format!("clarp-transcript-cache-bad-{}", std::process::id()));
    let cache = TranscriptCache::new(&directory);
    let snapshot = log(json!({"turns": [], "latest_revision": 0}));
    cache.save("https://h", "s", &snapshot).unwrap();
    let file = std::fs::read_dir(&directory).unwrap().next().unwrap().unwrap().path();
    std::fs::write(&file, b"{not json").unwrap();
    assert!(cache.load("https://h", "s").is_empty());
    std::fs::write(&file, br#"{"schema": 2, "base_url": "https://h", "session": "s", "snapshot": {"turns": []}}"#).unwrap();
    assert!(cache.load("https://h", "s").is_empty(), "unknown schema");
    assert!(cache.save("https://h", "", &snapshot).is_err());
    std::fs::remove_dir_all(directory).ok();
}

#[test]
fn every_chat_cached_for_a_host_is_listed() {
    let directory = std::env::temp_dir().join(format!("clarp-transcript-cache-all-{}", std::process::id()));
    let cache = TranscriptCache::new(&directory);
    assert!(cache.all("http://a").unwrap().is_empty(), "no folder yet: nothing cached");
    let snapshot = |text: &str| log(json!({"conversation_id": "c", "latest_revision": 1, "has_more": false, "turns": [{"id": "1", "role": "user", "text": text, "revision": 1}]}));
    cache.save("http://a", "rachel", &snapshot("one")).unwrap();
    cache.save("http://a", "mike", &snapshot("two")).unwrap();
    cache.save("http://b", "rachel", &snapshot("elsewhere")).unwrap();
    let chats = cache.all("http://a").unwrap();
    assert_eq!(chats.iter().map(|(s, _)| s.as_str()).collect::<Vec<_>>(), ["mike", "rachel"], "only this Host's chats, by session");
    assert_eq!(chats[1].1["turns"][0]["text"], "one");
    std::fs::remove_dir_all(&directory).unwrap();
}
