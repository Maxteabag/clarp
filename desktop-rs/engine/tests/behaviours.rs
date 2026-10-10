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
    // The select and the transcript fetch go out together; either may land first.
    let selected = || host.requests("POST", "/select").last().is_some_and(|r| r["body"]["session"] == "mike");
    d.until("the Host is told mike is selected", |_| selected());
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

/// A failed snapshot keeps the roster, says it is stale (no banner) and is
/// retried on its own; a roster event while it fails is not lost.
#[test]
fn a_failed_snapshot_keeps_the_roster_and_retries_on_its_own() {
    use clarp_engine::roster_freshness::State;
    let host = Host::start("stale-roster");
    let mut d = Driver::new(&host.base);
    d.connect();
    d.until("fresh", |e| e.roster_freshness() == State::Fresh);
    let before = d.engine.roster().agents().len();
    host.control("/__control/fail", json!({"path": "/agents/snapshot", "status": 504, "count": 2}));
    let snapshots = host.requests("GET", "/agents/snapshot").len();
    d.engine.refresh_agents();
    d.until("stale", |e| e.roster_freshness().is_stale());
    assert_eq!(d.engine.roster().agents().len(), before, "the roster is kept");
    assert!(d.engine.error().is_empty(), "no banner for a background refresh: {:?}", d.engine.error());
    // An announced agent while it fails: the event only marks the roster
    // dirty (no request of its own), and the retry brings it.
    host.control("/__control/add-agent", json!({"session": "koko", "persona": "Koko"}));
    std::thread::sleep(Duration::from_millis(500));
    d.until("still waiting", |_| true);
    assert_eq!(host.requests("GET", "/agents/snapshot").len(), snapshots + 1, "no request stacked before the back-off");
    d.until("the second failure", |e| matches!(e.roster_freshness(), State::Stale { failures: 2, .. }));
    d.until("koko after the next retry", |e| e.roster().find("koko").is_some());
    assert_eq!(d.engine.roster_freshness(), State::Fresh);
    let times: Vec<f64> = host.requests("GET", "/agents/snapshot")[snapshots..].iter().map(|r| r["at"].as_f64().unwrap()).collect();
    assert_eq!(times.len(), 3, "a request per try: {times:?}");
    let (first, second) = (times[1] - times[0], times[2] - times[1]);
    assert!((1.8..4.0).contains(&first) && (4.8..8.0).contains(&second), "backed off 2 s then 5 s: {first:.2} {second:.2}");
}
