use clarp_core::artifact_summary::{self as summary, Check, Entry, HydrationCache};
use serde_json::{Value, json};

fn info(value: Value) -> clarp_core::json::Object {
    value.as_object().cloned().unwrap()
}

#[test]
fn the_feature_is_read_from_its_flag_and_version_never_the_contract_number() {
    let both = info(json!({"capabilities": {"features": ["artifact_summary_v1"]}, "contract": {"host": 63, "features": {"artifact_summary_v1": 1}}}));
    assert!(summary::advertised(&both));
    let listed_only = info(json!({"capabilities": {"features": ["artifact_summary_v1"]}, "contract": {"host": 63}}));
    assert!(!summary::advertised(&listed_only));
    let other_version = info(json!({"capabilities": {"features": ["artifact_summary_v1"]}, "contract": {"features": {"artifact_summary_v1": 2}}}));
    assert!(!summary::advertised(&other_version));
    let number_only = info(json!({"capabilities": {"features": []}, "contract": {"host": 99, "features": {}}}));
    assert!(!summary::advertised(&number_only));
}

#[test]
fn only_the_exact_refusal_falls_back() {
    assert!(summary::is_unsupported_representation(400, "unsupported artifact representation"));
    assert!(!summary::is_unsupported_representation(400, "invalid attention query"));
    assert!(!summary::is_unsupported_representation(500, "unsupported artifact representation"));
    assert!(!summary::is_unsupported_representation(400, "Bad Request"));
    assert_eq!(summary::with_representation("/artifacts?limit=50", "summary-v1"), "/artifacts?limit=50&representation=summary-v1");
    assert_eq!(summary::with_representation("/artifacts/pinned", "summary-v1"), "/artifacts/pinned?representation=summary-v1");
}

#[test]
fn markers_are_read_and_an_omitted_body_is_not_complete() {
    let row = json!({"artifact_id": "f", "type": "html_form", "updated_at": 5,
                     "content_omitted": {"reason": "summary", "bytes": 12, "sha256": "ABC", "version": 2, "updated_at": 5}});
    let marker = summary::content_marker(&row).unwrap();
    assert_eq!((marker.bytes, marker.sha256.as_deref(), marker.version, marker.updated_at), (Some(12), Some("abc"), json!(2), 5));
    assert!(!summary::complete(&row));
    let pending = json!({"content_omitted": {"reason": "summary", "bytes": null, "size_unknown": true, "sha256": null, "hash_pending": true, "version": null, "updated_at": 9}});
    let marker = summary::content_marker(&pending).unwrap();
    assert_eq!((marker.bytes, marker.sha256, marker.version), (None, None, Value::Null));
    let mut filled = row.clone();
    summary::set_content(&mut filled, Some(String::new()));
    assert!(summary::complete(&filled), "put back, even empty");
    assert!(summary::complete(&json!({"type": "html_form", "content": ""})), "a full row");
    assert!(summary::content_marker(&json!({"content_omitted": {"reason": "other"}})).is_none());

    let plan = json!({"plan": {"goal": {"history_omitted": {"reason": "summary", "count": 3, "last_at": 7, "digest": null, "digest_pending": true}}}});
    let marker = summary::history_marker(&plan).unwrap();
    assert_eq!((marker.count, marker.last_at, marker.digest), (3, Some(7), None));
    assert!(!summary::complete(&plan));
    let mut plan = plan;
    summary::set_goal_history(&mut plan, Some(json!([{"at": 7}])));
    assert!(summary::complete(&plan));
    summary::set_goal_history(&mut plan, None);
    assert!(summary::goal_history(&plan).is_none());
}

#[test]
fn content_is_checked_against_its_utf8_hash() {
    let text = "Bergen – Ålesund";
    let sha = summary::sha256_hex(text);
    assert_eq!(sha.len(), 64);
    assert_eq!(summary::check_content(Some(&sha), text), Check::Match);
    assert_eq!(summary::check_content(Some(&sha), "Bergen - Alesund"), Check::Mismatch);
    assert_eq!(summary::check_content(None, text), Check::Unknown);
    let mut entry = Entry::content("f", text, None);
    assert!(entry.has_sha256(&sha));
    assert!(!entry.has_sha256("00"));
}

#[test]
fn keys_and_scopes_tell_hosts_credentials_and_versions_apart() {
    let a = summary::scope("host-1", "https://h", "token-a");
    assert_ne!(a, summary::scope("host-1", "https://h", "token-b"), "another credential");
    assert_ne!(a, summary::scope("host-2", "https://h", "token-a"), "another Host");
    assert!(!a.contains("token-a"), "the token is not kept as such");
    assert_ne!(summary::content_key(&a, "f", 5, &json!(2)), summary::content_key(&a, "f", 6, &json!(2)));
    assert_ne!(summary::content_key(&a, "f", 5, &json!(2)), summary::content_key(&a, "f", 5, &json!(3)));
    assert_ne!(summary::history_key(&a, "p", "d1"), summary::history_key(&a, "p", "d2"));
}

#[test]
fn the_cache_drops_the_least_recently_used_by_count_and_bytes() {
    let mut cache = HydrationCache::new(2, 100);
    cache.insert("a".into(), Entry::content("a", &"x".repeat(10), None));
    cache.insert("b".into(), Entry::content("b", &"x".repeat(10), None));
    assert!(cache.get("a").is_some(), "a used again");
    let evicted = cache.insert("c".into(), Entry::content("c", &"x".repeat(10), None));
    assert_eq!(evicted, ["b"]);
    assert_eq!((cache.len(), cache.bytes()), (2, 20));
    let evicted = cache.insert("d".into(), Entry::content("d", &"x".repeat(95), None));
    assert_eq!(evicted.len(), 2, "over the byte cap: {evicted:?}");
    assert!(cache.peek("d").is_some(), "the entry just put in stays even alone over the cap");
    assert_eq!((cache.len(), cache.bytes()), (1, 95));
    cache.set_limits(10, 1000);
    cache.insert("e".into(), Entry::content("same", "1", None));
    cache.insert("f".into(), Entry::content("same", "2", None));
    cache.remove_artifact("same");
    assert_eq!(cache.len(), 1);
    cache.clear();
    assert!(cache.is_empty() && cache.bytes() == 0);
}
