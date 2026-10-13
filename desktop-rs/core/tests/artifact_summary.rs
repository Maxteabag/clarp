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
    assert_eq!(summary::with_representation("/pinned-artifacts", "summary-v1"), "/pinned-artifacts?representation=summary-v1");
    assert_eq!(summary::with_representation("/artifacts?limit=50&representation=flat-v1&order=updated", "summary-v1"), "/artifacts?limit=50&order=updated&representation=summary-v1", "instead of flat-v1, not beside it");
}

#[test]
fn markers_are_read_and_an_omitted_body_is_not_complete() {
    const HI: &str = "9c655e84ab91015eed7670dfbb369ffdc31f6efe04ef8bad2615cab6b9e63ca6";
    let row = json!({"artifact_id": "f", "type": "html_form", "updated_at": 5,
                     "content_omitted": {"reason": "summary", "bytes": 9, "sha256": format!("c1:{}", HI.to_uppercase()), "version": 2, "updated_at": 5}});
    let marker = summary::content_marker(&row).unwrap();
    assert_eq!((marker.bytes, marker.sha256.as_deref(), marker.version, marker.updated_at), (Some(9), Some(HI), json!(2), 5));
    // A hash of another kind (or none) cannot be checked: as pending.
    for other in [json!(format!("c2:{HI}")), json!(HI), json!("c1:not-hex"), json!(null)] {
        let row = json!({"content_omitted": {"reason": "summary", "sha256": other, "updated_at": 5}});
        assert_eq!(summary::content_marker(&row).unwrap().sha256, None, "{other}");
    }
    assert!(!summary::complete(&row));
    let pending = json!({"content_omitted": {"reason": "summary", "bytes": null, "size_unknown": true, "sha256": null, "hash_pending": true, "version": null, "updated_at": 9}});
    let marker = summary::content_marker(&pending).unwrap();
    assert_eq!((marker.bytes, marker.sha256, marker.version), (None, None, Value::Null));
    let mut filled = row.clone();
    summary::set_content(&mut filled, Some(String::new()));
    assert!(summary::complete(&filled), "put back, even empty");
    assert!(summary::complete(&json!({"type": "html_form", "content": ""})), "a full row");
    assert!(summary::content_marker(&json!({"content_omitted": {"reason": "other"}})).is_none());

    let digested = json!({"plan": {"goal": {"history_omitted": {"reason": "summary", "count": 3, "last_at": 7, "digest": "h1:ABCdef"}}}});
    assert_eq!(summary::history_marker(&digested).unwrap().digest.as_deref(), Some("h1:ABCdef"), "opaque: kept as sent");
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

/// Revision 5's vectors: the decoded string, its UTF-8 bytes, its hash.
#[test]
fn the_contract_hash_vectors_hold() {
    let vectors = [
        (r#""""#, 0, "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"),
        (r#""<p>Hi</p>""#, 9, "9c655e84ab91015eed7670dfbb369ffdc31f6efe04ef8bad2615cab6b9e63ca6"),
        (r#""\u00e9\u6f22\ud83d\ude00""#, 9, "3ea0588400cb4af7b15d0edd7dc2fe1e359a94ab6822abe8c535f4281f330a20"),
        (r#""line1\nline2\t\"q\" \\ /""#, 19, "689426335045576c85a1ac7696a1146993c503c57b489f7b2fb2e581bac1ca1b"),
        (r#""<img src=\"data:image/png;base64,iVBORw0KGgo=\">""#, 46, "9af61be3754e69b51d792026d0f7364a2bef6f29389257192c59f443e6a643d8"),
    ];
    for (wire, bytes, hex) in vectors {
        let content: String = serde_json::from_str(wire).unwrap();
        assert_eq!(content.len(), bytes, "{wire}");
        assert_eq!(summary::content_hash_of(&content), format!("c1:{hex}"), "{wire}");
        let row = json!({"content_omitted": {"reason": "summary", "sha256": format!("c1:{hex}"), "updated_at": 1}});
        let marker = summary::content_marker(&row).unwrap();
        assert_eq!(summary::check_content(marker.sha256.as_deref(), &content), Check::Match, "{wire}");
    }
    let emoji: String = serde_json::from_str(r#""é漢😀""#).unwrap();
    assert_eq!(summary::sha256_hex(&emoji), "3ea0588400cb4af7b15d0edd7dc2fe1e359a94ab6822abe8c535f4281f330a20", "raw or escaped, the same codepoints");
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
