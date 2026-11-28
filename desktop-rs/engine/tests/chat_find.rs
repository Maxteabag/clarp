mod common;

use common::{Driver, Host};
use serde_json::{Value, json};

fn legacy_find(name: &str, contract: Value) {
    let host = Host::start(name);
    // The capability list claims positions, and the Host even sends the
    // member unsolicited. Its ordinary hits still require a second page.
    host.control("/__control/message-search", json!({"positions": true, "page": 1, "contract": contract, "unsolicited_positions": true}));
    host.control("/__control/turns", json!({"session": "rachel", "turns": [
        {"id": "older", "role": "user", "text": "heron"},
        {"id": "newer", "role": "assistant", "text": "heron"}
    ]}));
    let mut d = Driver::new(&host.base);
    d.connect();
    d.engine.find_in_chat("rachel", "heron");
    d.until("chat search answered", |e| e.chat_find_results("rachel", "heron").answered);
    let requests = host.requests("GET", "/search");
    assert_eq!(requests.len(), 2, "{requests:?}");
    assert!(requests.iter().all(|r| r["query"].get("positions").is_none()), "{requests:?}");
    assert!(requests[0]["query"].get("before").is_none());
    assert_eq!(requests[1]["query"]["before"], "1");
    assert_eq!(d.engine.chat_find_results("rachel", "heron").ids, ["older", "newer"]);
}

#[test]
fn contract_without_search_positions_pages_hits() {
    legacy_find("find-contract-map", json!({"host": 220, "features": {"message_search": 196}}));
}

#[test]
fn missing_feature_map_pages_hits() {
    legacy_find("find-legacy-map", json!({"host": 220}));
}

#[test]
fn advertised_positions_use_one_request() {
    let host = Host::start("find-explicit-positions");
    host.control("/__control/message-search", json!({"positions": true, "page": 1,
        "contract": {"host": 220, "features": {"message_search": 196, "search_positions": 218}}}));
    host.control("/__control/turns", json!({"session": "rachel", "turns": [
        {"id": "older", "role": "user", "text": "heron"},
        {"id": "newer", "role": "assistant", "text": "heron"}
    ]}));
    let mut d = Driver::new(&host.base);
    d.connect();
    d.engine.find_in_chat("rachel", "heron");
    d.until("positions answered", |e| e.chat_find_results("rachel", "heron").answered);
    let requests = host.requests("GET", "/search");
    assert_eq!(requests.len(), 1, "{requests:?}");
    assert_eq!(requests[0]["query"]["positions"], "1");
    assert_eq!(d.engine.chat_find_results("rachel", "heron").ids, ["older", "newer"]);
}
