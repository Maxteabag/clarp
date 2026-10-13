//! The Updates lists follow the Host's events without reloading on each:
//! a named artifact is read alone, attention reloads only attention, each
//! list at most once per interval, and an older Host's 400 for flat-v1
//! falls back to the full representation.

mod common;

use std::time::{Duration, Instant};

use clarp_core::update_feeds::Intervals;
use common::{Driver, Host};
use serde_json::json;

fn gets(host: &Host, path: &str) -> usize {
    host.requests("GET", path).len()
}

fn loaded(host: &Host) -> Driver {
    let mut d = Driver::new(&host.base);
    d.connect();
    d.until("updates loaded", |e| e.update_artifacts().len() == 4 && !e.updates_loading());
    d
}

fn ids(engine: &clarp_engine::Engine) -> Vec<String> {
    engine.update_artifacts().iter().filter_map(|a| a["artifact_id"].as_str().map(str::to_owned)).collect()
}

#[test]
fn an_artifact_event_reads_that_artifact_alone_and_places_it() {
    let host = Host::start("feeds-item");
    // Listed as the Host lists them: newest updated first.
    let seed: Vec<_> = (1..=4).map(|n| json!({"artifact_id": format!("a{n}"), "type": "document", "title": format!("A{n}"),
                                               "session": "rachel", "content": "Body", "updated_at": n * 1000})).collect();
    host.control("/__control/stream", json!({"seed": seed}));
    let mut d = loaded(&host);
    assert_eq!(ids(&d.engine), ["a4", "a3", "a2", "a1"]);
    let lists = gets(&host, "/artifacts");

    // Changed: read alone, without its payload, and moved to the top.
    host.control("/__control/artifact-update", json!({"id": "a1", "set": {"title": "A1 v2"}}));
    d.until("a1 changed", |e| e.update_artifacts()[0]["title"] == "A1 v2");
    let read = host.requests("GET", "/artifacts/a1");
    assert_eq!(read.len(), 1);
    assert_eq!(read[0]["query"]["representation"], "flat-v1");
    assert!(d.engine.update_artifacts()[0].get("payload").is_none(), "patched in as flat-v1 has it");
    assert_eq!(ids(&d.engine), ["a1", "a4", "a3", "a2"]);
    // The memory line's counters saw both reads.
    let traffic = d.engine.take_list_traffic();
    assert!(traffic["artifacts"].requests >= 1 && traffic["artifacts"].bytes > 0, "{traffic:?}");
    assert_eq!(traffic["artifact"].requests, 1, "{traffic:?}");
    assert!(d.engine.take_list_traffic().is_empty(), "taken once");

    // A pin leaves updated_at alone: changed in place.
    host.control("/__control/event", json!({"type": "artifact-updated", "artifact_id": "a3"}));
    d.until("a3 read", |_| gets(&host, "/artifacts/a3") == 1);
    d.settle(Duration::from_millis(200));
    assert_eq!(ids(&d.engine), ["a1", "a4", "a3", "a2"]);

    // New: at the top. Archived: at the top too, and still listed (the
    // Host lists archived ones).
    host.control("/__control/artifact-update", json!({"id": "a2", "set": {"archived_at": 5},
        "add": [{"artifact_id": "new1", "type": "document", "title": "New", "session": "rachel"}]}));
    d.until("new1 and the archived a2", |e| ids(e).len() == 5 && ids(e)[..2].contains(&"new1".to_owned())
        && e.update_artifacts().iter().any(|a| a["artifact_id"] == "a2" && a["archived_at"] == 5));
    let host_list = host.control_json("/__control/state", json!({"limit": 50}));
    let host_ids: Vec<String> = host_list["artifacts"].as_array().unwrap().iter().map(|a| a["artifact_id"].as_str().unwrap().to_owned()).collect();
    assert_eq!(ids(&d.engine), host_ids, "the Host's order");

    // Gone (404): it leaves; the list was not full, so nothing reloads.
    host.control("/__control/artifacts", json!({"artifacts": [{"artifact_id": "a1", "title": "A1 v2", "type": "document", "updated_at": 1000}]}));
    d.until("only a1", |e| ids(e) == ["a1"]);
    d.settle(Duration::from_millis(300));
    assert_eq!(gets(&host, "/artifacts"), lists, "no event reloaded the whole list");

    // Unlisted types never join.
    host.control("/__control/artifact-update", json!({"add": [{"artifact_id": "img1", "type": "image", "title": "Pic"}]}));
    d.until("img1 read", |_| gets(&host, "/artifacts/img1") == 1);
    d.settle(Duration::from_millis(200));
    assert_eq!(ids(&d.engine), ["a1"]);
}

#[test]
fn attention_events_reload_attention_alone_once_per_interval() {
    let host = Host::start("feeds-attention");
    let mut d = loaded(&host);
    d.engine.set_update_intervals(Intervals { attention: Duration::from_secs(1), ..Intervals::default() });
    d.settle(Duration::from_millis(1100));
    let (attention, lists, jobs) = (gets(&host, "/attention"), gets(&host, "/artifacts"), gets(&host, "/background-jobs"));
    let started = Instant::now();
    for n in 0..10 {
        host.control("/__control/attention", json!({"items": [{"id": format!("q{n}"), "session": "rachel", "kind": "question"}]}));
        std::thread::sleep(Duration::from_millis(50));
        d.settle(Duration::ZERO);
    }
    d.until("the last attention item", |e| e.attention_items().len() == 1 && e.attention_items()[0]["id"] == "q9");
    d.settle(Duration::from_millis(1200));
    let read = gets(&host, "/attention") - attention;
    let seconds = started.elapsed().as_secs_f64();
    assert!(read <= 1 + seconds.ceil() as usize, "{read} attention reads in {seconds:.1} s");
    assert!(read >= 2, "the first at once and a trailing one: {read}");
    assert_eq!((gets(&host, "/artifacts"), gets(&host, "/background-jobs")), (lists, jobs), "only attention reloads");
}

#[test]
fn a_refresh_reloads_at_once_while_events_wait_their_interval() {
    let host = Host::start("feeds-refresh");
    let mut d = loaded(&host);
    d.engine.set_update_intervals(Intervals { artifacts: Duration::from_secs(60), ..Intervals::default() });
    let lists = gets(&host, "/artifacts");
    // An event that names nothing waits out the minute since the first list.
    host.control("/__control/event", json!({"type": "artifact-updated"}));
    d.settle(Duration::from_millis(500));
    assert_eq!(gets(&host, "/artifacts"), lists, "an event's reload waits its interval");
    d.engine.load_updates();
    d.until("the refresh", |_| gets(&host, "/artifacts") == lists + 1);
    // The refresh satisfied the waiting reload: none follows it.
    d.settle(Duration::from_millis(300));
    assert_eq!(gets(&host, "/artifacts"), lists + 1);
}

#[test]
fn an_older_host_that_refuses_flat_v1_gets_the_full_list() {
    let host = Host::start("feeds-old-host");
    host.control("/__control/representation", json!({"flat": false}));
    let mut d = loaded(&host);
    let asked = host.requests("GET", "/artifacts");
    assert_eq!(asked.len(), 2, "flat-v1 refused, then the full list");
    assert_eq!(asked[0]["query"]["representation"], "flat-v1");
    assert!(asked[1]["query"].get("representation").is_none());
    assert!(d.engine.updates_error().is_empty(), "the fallback is no error: {}", d.engine.updates_error());
    let doc = d.engine.update_artifacts().iter().find(|a| a["artifact_id"] == "doc1").cloned().unwrap();
    assert_eq!(doc["content"], "# Findings\n\nAll *good*.", "top-level fields read the same with a payload");
    assert!(d.engine.report_for_artifact("doc1").is_some());

    // Items are then read in full too.
    host.control("/__control/artifact-update", json!({"id": "doc1", "set": {"title": "Again"}}));
    d.until("doc1 changed", |e| e.update_artifacts().iter().any(|a| a["title"] == "Again"));
    assert!(host.requests("GET", "/artifacts/doc1")[0]["query"].get("representation").is_none());
}
