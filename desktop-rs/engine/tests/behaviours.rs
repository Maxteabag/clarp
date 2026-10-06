//! The REWRITE_PLAN behaviours the desktop must keep, against the fake Host,
//! through the engine the Slint app runs on (SLINT_PARITY.md's behaviour
//! table points here).

mod common;

use std::time::Duration;

use clarp_engine::Change;
use common::{Driver, Host};
use serde_json::json;

#[test]
fn the_roster_comes_from_the_snapshot_and_follows_roster_events() {
    let host = Host::start("roster");
    let mut d = Driver::new(&host.base);
    d.connect();
    let names: Vec<String> = d.engine.roster().agents().iter().map(|a| a.session.clone()).collect();
    assert!(names.contains(&"rachel".into()) && names.contains(&"mike".into()), "{names:?}");
    assert!(!host.requests("GET", "/agents/snapshot").is_empty());
    host.control("/__control/add-agent", json!({"session": "nina", "persona": "Nina"}));
    d.until("a created agent appears", |e| e.roster().find("nina").is_some());
    assert!(d.changes.contains(&Change::Roster));
}

#[test]
fn selecting_focuses_the_host_and_server_focus_moves_with_other_clients() {
    let host = Host::start("focus");
    let mut d = Driver::new(&host.base);
    d.connect();
    d.engine.select("mike");
    d.until("mike opens", |e| e.conversation("mike").is_some_and(|c| !c.rows().is_empty()));
    assert_eq!(host.requests("POST", "/select").last().unwrap()["body"]["session"], "mike");
    // Another client focuses Rachel: the roster says so.
    host.control("/__control/event", json!({"type": "agent-focus", "session": "rachel"}));
    d.until("focus follows the Host", |e| e.roster().find("rachel").is_some_and(|a| a.focused) && e.roster().find("mike").is_some_and(|a| !a.focused));
}

#[test]
fn a_new_conversation_id_replaces_the_transcript() {
    let host = Host::start("replace");
    let mut d = Driver::new(&host.base);
    d.connect();
    d.until("rachel open", |e| e.conversation("rachel").is_some_and(|c| c.rows().len() == 2));
    host.control("/__control/fill", json!({"session": "rachel", "count": 3, "prefix": "Fresh", "conversation_id": "c-rachel-2"}));
    d.until("replaced, not appended", |e| {
        e.conversation("rachel").is_some_and(|c| c.rows().len() == 3 && c.rows().iter().all(|r| r.text.starts_with("Fresh")))
    });
}

#[test]
fn a_growing_message_is_merged_by_id_and_revision() {
    let host = Host::start("merge");
    let mut d = Driver::new(&host.base);
    d.connect();
    d.until("rachel open", |e| e.conversation("rachel").is_some_and(|c| c.rows().len() == 2));
    for text in ["Partial", "Partial answer", "Partial answer, complete."] {
        host.control("/__control/turns", json!({"session": "rachel", "turns": [
            {"id": "r1", "role": "user", "text": "Hello"},
            {"id": "r2", "role": "assistant", "text": text},
        ]}));
        d.until(text, |e| e.conversation("rachel").is_some_and(|c| c.rows().last().is_some_and(|r| r.text == text)));
        let rows = d.engine.conversation("rachel").unwrap().rows();
        assert_eq!(rows.len(), 2, "no duplicated turns while it grows: {:?}", rows.iter().map(|r| &r.text).collect::<Vec<_>>());
    }
}

#[test]
fn one_event_stream_resumes_after_an_outage_and_ignores_unknown_events() {
    let host = Host::start("stream");
    let mut d = Driver::new(&host.base);
    d.connect();
    // An event the desktop does not know is ignored, not an error.
    host.control("/__control/event", json!({"type": "something-new", "session": "rachel", "extra": {"x": 1}}));
    host.control("/__control/event", json!({"type": "agent-focus", "session": "mike", "new_field": true}));
    d.until("a known event with an extra field applies", |e| e.roster().find("mike").is_some_and(|a| a.focused));
    assert!(d.engine.error().is_empty(), "{:?}", d.engine.error());
    assert_eq!(host.requests("GET", "/events").len(), 1, "one stream");
    host.control("/__control/outage", json!({"seconds": 1.0}));
    d.until("the stream drops", |e| e.connection_state() != "live");
    d.until("and comes back", |e| e.connection_state() == "live");
    d.settle(Duration::from_millis(300));
    let streams = host.requests("GET", "/events");
    let resumed = streams.last().unwrap();
    assert!(streams.len() >= 2 && resumed["last_event_id"].as_str().is_some_and(|id| !id.is_empty()), "it resumes where it left: {streams:?}");
}

#[test]
fn stopping_a_turn_and_the_states_a_chat_shows() {
    let host = Host::start("states");
    let mut d = Driver::new(&host.base);
    d.connect();
    for state in ["thinking", "waiting", "interrupted", "tool", "idle"] {
        let now = chrono_now();
        host.control("/__control/event", json!({"type": "agent-state", "session": "rachel", "kind": state, "ts": now}));
        d.until(state, |e| e.roster().display_state("rachel").as_deref() == Some(state));
    }
    host.control("/__control/event", json!({"type": "queue-updated", "session": "rachel", "queue_depth": 2, "queue_revision": 5}));
    d.until("the queue shows", |e| e.queue_count("rachel") == 2);
    d.engine.stop_session("rachel");
    d.until("stop sent", |_| !host.requests("POST", "/stop").is_empty());
    assert_eq!(host.requests("POST", "/stop")[0]["body"]["session"], "rachel");
}

fn chrono_now() -> i64 {
    std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_millis() as i64).unwrap_or(0)
}

#[test]
fn a_chat_opens_from_its_cached_copy_and_asks_only_for_what_is_newer() {
    let host = Host::start("cache-open");
    let cache = host.dir.join("transcripts");
    let rachel_logs = |host: &Host| host.requests("GET", "/log").into_iter().filter(|r| r["query"]["session"] == "rachel").collect::<Vec<_>>();
    let cached = {
        let mut d = Driver::with_transcript_cache(&host.base, &cache);
        d.connect();
        d.until("rachel open", |e| e.conversation("rachel").is_some_and(|c| c.rows().len() == 2 && !e.log_pending("rachel")));
        d.until("rachel cached", |_| std::fs::read_dir(&cache).is_ok_and(|mut files| files.any(|f| f.is_ok_and(|f| f.path().extension().is_some_and(|x| x == "json")))));
        d.engine.conversation("rachel").unwrap().latest_revision()
    };
    assert!(cached > 0);
    let before = rachel_logs(&host).len();
    let mut d = Driver::with_transcript_cache(&host.base, &cache);
    d.engine.start();
    d.until("the cached rows show", |e| e.conversation("rachel").is_some_and(|c| c.rows().len() == 2 && c.latest_revision() == cached));
    d.until("the Host answered", |e| !e.log_pending("rachel") && e.connected());
    let asked = rachel_logs(&host).split_off(before);
    assert!(!asked.is_empty());
    assert!(asked.iter().all(|r| r["query"]["after_revision"] == cached.to_string()), "only the delta after the cached copy: {asked:?}");
}

#[test]
fn message_search_finds_loaded_chats_and_chats_only_the_cache_holds() {
    use clarp_core::message_search::Source;
    let host = Host::start("message-search");
    let cache = host.dir.join("transcripts");
    let mut d = Driver::with_transcript_cache(&host.base, &cache);
    let snapshot = json!({"conversation_id": "c-mike", "latest_revision": 1, "has_more": false, "turns": [
        {"id": "m-cached", "role": "assistant", "text": "The quarterly pelican report is ready", "revision": 1, "timestamp": "2026-09-28T09:00:00Z"}]});
    clarp_core::transcript_cache::TranscriptCache::new(&cache).save(d.engine.base_url(), "mike", snapshot.as_object().unwrap()).unwrap();
    d.connect();
    d.until("rachel open", |e| e.conversation("rachel").is_some_and(|c| c.rows().len() == 2 && !e.log_pending("rachel")));
    let first = d.engine.search_messages("pelican", 10);
    assert!(first.cache_reading, "the first search starts reading the cache");
    d.until_change(&Change::Search);
    let found = d.engine.search_messages("PELICAN report", 10);
    assert_eq!(found.hits.iter().map(|h| (h.session.as_str(), h.id.as_str())).collect::<Vec<_>>(), [("mike", "m-cached")]);
    assert_eq!(found.hits[0].source, Source::Cache);
    assert!(!found.cache_reading && found.cache_error.is_empty());
    assert!(found.cached_chats >= 1 && found.loaded_chats >= 1, "{found:?}");
    let help = d.engine.search_messages("can I help", 10);
    assert_eq!(help.hits.first().map(|h| (h.session.as_str(), h.id.as_str(), h.source)), Some(("rachel", "r2", Source::Memory)), "a loaded chat is searched as loaded");
}
