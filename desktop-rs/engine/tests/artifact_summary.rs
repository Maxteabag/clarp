//! Summary artifact lists (`representation=summary-v1`, feature
//! `artifact_summary_v1`) and lazy hydration, against the fake Host.

mod common;

use std::time::Duration;

use clarp_core::update_feeds::Intervals;
use clarp_engine::Hydration;
use common::{Driver, Host};
use serde_json::{Value, json};

const FORM: &str = "<h1>Trip</h1><form><input name=\"where\" value=\"Bergen – Ålesund\"></form>";

fn form(id: &str, content: &str, updated_at: i64) -> Value {
    json!({"artifact_id": id, "type": "html_form", "status": "active", "session": "rachel", "version": 2,
           "title": format!("Form {id}"), "summary": "", "content": content, "updated_at": updated_at,
           "answer_schema": {"type": "object", "properties": {"where": {"type": "string"}}}})
}

fn history(entries: usize) -> Value {
    Value::Array((0..entries).map(|i| json!({"at": 1_760_000_000_000_i64 + i as i64, "kind": "checked", "revision": i, "detail": {}})).collect())
}

fn plan(id: &str, entries: usize) -> Value {
    json!({"artifact_id": id, "type": "plan", "status": "active", "session": "rachel", "title": "Ship it", "updated_at": 900,
           "plan": {"plan_id": "p-1", "title": "Ship", "status": "active", "revision": 3, "items": [],
                    "goal": {"outcome": "Shipped", "history": history(entries)}}})
}

fn decision() -> Value {
    json!({"artifact_id": "dec-1", "type": "decision", "status": "active", "session": "mike", "title": "Deploy?", "updated_at": 800,
           "decision": {"decision_id": "d-1", "question": "Deploy tonight?", "status": "pending", "revision": 4,
                        "response_type": "approval", "yes_label": "Ship it", "no_label": "Hold"}})
}

fn fixtures() -> Value {
    json!([form("form-a", FORM, 1000), form("form-b", "<p>b</p>", 990), plan("plan-1", 3), decision(),
           {"artifact_id": "doc-1", "type": "document", "session": "rachel", "title": "Notes", "content": "# Notes", "updated_at": 700}])
}

/// A fake Host with these artifacts, and summary-v1 on or off.
fn seeded(name: &str, artifacts: Value, summary: bool) -> Host {
    let host = Host::start(name);
    host.control("/__control/artifacts", json!({"artifacts": artifacts}));
    host.control("/__control/artifact-summary", json!({"on": summary}));
    host
}

fn listed(host: &Host) -> Driver {
    let mut d = Driver::new(&host.base);
    d.connect();
    d.until("artifacts listed", |e| !e.update_artifacts().is_empty() && !e.updates_loading());
    d
}

fn ids(d: &Driver) -> Vec<String> {
    d.engine.update_artifacts().iter().map(|a| a["artifact_id"].as_str().unwrap_or_default().to_owned()).collect()
}

fn row(d: &Driver, id: &str) -> Value {
    d.engine.update_artifacts().iter().find(|a| a["artifact_id"] == id).cloned().unwrap_or(Value::Null)
}

/// Every `GET /artifacts/<id>` on the wire, each asking for exactly
/// flat-v1. The tests that count them announce nothing after connecting,
/// so each is a hydration; the event test tells the two apart by the
/// engine's own count (`artifact_hydrations_sent`).
fn hydrations(host: &Host, id: &str) -> usize {
    let reads = host.requests("GET", &format!("/artifacts/{id}"));
    assert!(reads.iter().all(|r| r["query"] == json!({"representation": "flat-v1"})), "item reads ask for flat-v1: {reads:?}");
    reads.len()
}

/// The bytes of each answer to `GET /artifacts/<id>` (the fake Host logs
/// them as `RESPONSE`).
fn item_bytes(host: &Host, id: &str) -> Vec<u64> {
    let path = format!("/artifacts/{id}");
    host.log().iter().filter(|r| r["method"] == "RESPONSE" && r["path"] == path.as_str()).filter_map(|r| r["bytes"].as_u64()).collect()
}

fn list_representations(host: &Host) -> Vec<String> {
    host.requests("GET", "/artifacts").iter().map(|r| r["query"]["representation"].as_str().unwrap_or_default().to_owned()).collect()
}

/// The row without what summary-v1 leaves out and its markers.
fn comparable(mut row: Value) -> Value {
    let object = row.as_object_mut().unwrap();
    object.remove("content");
    object.remove("content_omitted");
    if let Some(goal) = object.get_mut("plan").and_then(|p| p.get_mut("goal")).and_then(Value::as_object_mut) {
        goal.remove("history");
        goal.remove("history_omitted");
    }
    row
}

fn ready(d: &mut Driver, id: &str) {
    let id = id.to_owned();
    d.until(&format!("{id} read whole"), move |e| e.artifact_hydration(&id) == Hydration::Ready);
}

#[test]
fn without_the_feature_the_list_is_asked_for_as_before() {
    let host = seeded("summary-off", fixtures(), false);
    let d = listed(&host);
    assert!(!d.engine.artifact_summary_enabled());
    assert!(list_representations(&host).iter().all(|r| r == "flat-v1"), "never summary-v1 unadvertised: {:?}", list_representations(&host));
    assert_eq!(row(&d, "form-a")["content"], json!(FORM));
    assert_eq!(d.engine.artifact_hydration("form-a"), Hydration::Ready);
}

#[test]
fn summary_rows_match_the_full_list_and_approvals_work_from_them() {
    let full_host = seeded("summary-parity-full", fixtures(), false);
    let full = listed(&full_host);
    let host = seeded("summary-parity", fixtures(), true);
    let mut d = listed(&host);
    assert!(d.engine.artifact_summary_enabled());
    assert_eq!(list_representations(&host).last().map(String::as_str), Some("summary-v1"));
    assert_eq!(ids(&d), ids(&full), "the same rows in the same order");
    for (summary, whole) in d.engine.update_artifacts().iter().zip(full.engine.update_artifacts()) {
        assert_eq!(comparable(summary.clone()), comparable(whole.clone()), "only the bodies are left out");
    }
    let form = row(&d, "form-a");
    assert!(form.get("content").is_none(), "absent, never empty: {form}");
    let marker = &form["content_omitted"];
    assert_eq!(marker["bytes"], json!(FORM.len()), "UTF-8 bytes");
    assert_eq!(marker["updated_at"], json!(1000));
    assert!(row(&d, "plan-1")["plan"]["goal"].get("history").is_none());
    assert_eq!(row(&d, "plan-1")["plan"]["goal"]["history_omitted"]["count"], json!(3));
    assert_eq!(row(&d, "dec-1"), row(&full, "dec-1"), "a decision's row is whole");
    assert_eq!(row(&d, "doc-1")["content"], json!("# Notes"), "only html_form content is left out");
    assert_eq!(d.engine.artifact_hydration("form-a"), Hydration::Omitted, "nothing is read before a view needs it");
    assert_eq!(hydrations(&host, "form-a") + hydrations(&host, "plan-1"), 0);
    // An omitted html_form body is a report to open, not an empty one.
    assert!(d.engine.artifact_is_viewable_report(form.as_object().unwrap()));

    d.engine.resolve_decision("d-1", "yes", 4);
    d.until("the approval is sent", |e| !e.update_action_pending("decision", "d-1"));
    let sent = host.requests("POST", "/decisions/d-1/resolve");
    assert_eq!(sent.len(), 1);
    assert_eq!((sent[0]["body"]["choice"].as_str(), sent[0]["body"]["expected_revision"].as_i64()), (Some("accepted"), Some(4)));
}

#[test]
fn opening_a_form_reads_it_once() {
    let host = seeded("summary-once", fixtures(), true);
    host.control("/__control/artifact-summary", json!({"item_delay": 0.4}));
    let mut d = listed(&host);
    assert_eq!(d.engine.need_artifact("form-a"), Hydration::Loading);
    assert_eq!(d.engine.need_artifact("form-a"), Hydration::Loading, "a second open while the first reads");
    ready(&mut d, "form-a");
    assert_eq!(row(&d, "form-a")["content"], json!(FORM), "the body is back in its row");
    assert_eq!(d.engine.need_artifact("form-a"), Hydration::Ready);
    d.engine.load_updates();
    d.until("listed again", |e| !e.updates_loading());
    assert_eq!(row(&d, "form-a")["content"], json!(FORM), "a new list of the same version keeps it");
    assert_eq!((hydrations(&host, "form-a"), d.engine.artifact_hydrations_sent("form-a")), (1, 1), "two quick opens and a reload: one read");
    assert_eq!(d.engine.artifact_cache_size(), (1, FORM.len()));
    assert!(row(&d, "form-b").get("content").is_none(), "only what was opened is read");
}

#[test]
fn a_hash_mismatch_is_dropped_and_read_again() {
    let host = seeded("summary-sha", fixtures(), true);
    host.control("/__control/artifact-summary", json!({"corrupt": {"form-a": 1}}));
    let mut d = listed(&host);
    d.engine.need_artifact("form-a");
    ready(&mut d, "form-a");
    assert_eq!(hydrations(&host, "form-a"), 2, "the altered body is dropped and read again");
    assert_eq!(row(&d, "form-a")["content"], json!(FORM));

    // A Host whose hash never matches: an error, not a loop or a wrong body.
    host.control("/__control/artifact-summary", json!({"wrong_sha": ["form-b"]}));
    d.engine.load_updates();
    d.until("listed again", |e| !e.updates_loading());
    d.engine.need_artifact("form-b");
    d.until("form-b fails", |e| matches!(e.artifact_hydration("form-b"), Hydration::Failed(_)));
    assert_eq!(hydrations(&host, "form-b"), 2);
    assert!(row(&d, "form-b").get("content").is_none());
}

#[test]
fn a_pending_hash_is_checked_once_the_host_has_it() {
    let host = seeded("summary-pending", fixtures(), true);
    host.control("/__control/artifact-summary", json!({"hash_pending": ["form-a"]}));
    let mut d = listed(&host);
    assert_eq!(row(&d, "form-a")["content_omitted"]["hash_pending"], json!(true));
    d.engine.set_artifact_in_view(Some("form-a"));
    ready(&mut d, "form-a");
    assert_eq!(hydrations(&host, "form-a"), 1);

    // The same tuple with its hash: the cached body is hashed here, matches,
    // and is kept.
    host.control("/__control/artifact-summary", json!({"hash_pending": []}));
    d.engine.load_updates();
    d.until("listed with the hash", |e| !e.updates_loading() && e.update_artifacts().iter().any(|a| a["artifact_id"] == "form-a" && a["content_omitted"]["sha256"].is_string()));
    assert_eq!(row(&d, "form-a")["content"], json!(FORM));
    assert_eq!(hydrations(&host, "form-a"), 1, "a matching hash needs no read");

    // A hash the cached body does not have: dropped and read again.
    host.control("/__control/artifact-summary", json!({"wrong_sha": ["form-a"]}));
    d.engine.load_updates();
    d.until("form-a fails", |e| matches!(e.artifact_hydration("form-a"), Hydration::Failed(_)));
    assert_eq!(hydrations(&host, "form-a"), 3, "the cached body was dropped, then read twice");
}

#[test]
fn a_plan_history_is_read_again_when_its_digest_changes() {
    let host = seeded("summary-history", fixtures(), true);
    let mut d = listed(&host);
    d.engine.set_artifact_in_view(Some("plan-1"));
    d.until("history read", |e| e.update_artifacts().iter().any(|a| a["artifact_id"] == "plan-1" && a["plan"]["goal"]["history"].as_array().is_some_and(|h| h.len() == 3)));
    d.engine.load_updates();
    d.until("listed again", |e| !e.updates_loading());
    assert_eq!(hydrations(&host, "plan-1"), 1, "the same digest is not read again");

    // Recovery appends history without a new revision: the digest moves.
    let grown = plan("plan-1", 5)["plan"].clone();
    assert_eq!(grown["revision"], json!(3));
    host.control("/__control/artifact-quiet-update", json!({"id": "plan-1", "set": {"plan": grown}}));
    d.engine.load_updates();
    d.until("the new history read", |e| e.update_artifacts().iter().any(|a| a["artifact_id"] == "plan-1" && a["plan"]["goal"]["history"].as_array().is_some_and(|h| h.len() == 5)));
    assert_eq!((hydrations(&host, "plan-1"), d.engine.artifact_hydrations_sent("plan-1")), (2, 2));

    // Closed: a changed history is not read for nobody.
    d.engine.set_artifact_in_view(None);
    let more = plan("plan-1", 7)["plan"].clone();
    host.control("/__control/artifact-quiet-update", json!({"id": "plan-1", "set": {"plan": more}}));
    d.engine.load_updates();
    d.until("listed with 7", |e| !e.updates_loading() && e.update_artifacts().iter().any(|a| a["plan"]["goal"]["history_omitted"]["count"] == 7));
    d.settle(Duration::from_millis(300));
    assert_eq!(hydrations(&host, "plan-1"), 2);
}

#[test]
fn a_history_without_its_digest_is_read_for_the_view() {
    let host = seeded("summary-digest-pending", fixtures(), true);
    host.control("/__control/artifact-summary", json!({"digest_pending": ["plan-1"]}));
    let mut d = listed(&host);
    assert_eq!(row(&d, "plan-1")["plan"]["goal"]["history_omitted"]["digest_pending"], json!(true));
    d.engine.set_artifact_in_view(Some("plan-1"));
    ready(&mut d, "plan-1");
    d.engine.set_artifact_in_view(None);
    d.until("put away", |e| e.artifact_hydration("plan-1") == Hydration::Omitted);
    d.engine.set_artifact_in_view(Some("plan-1"));
    ready(&mut d, "plan-1");
    assert_eq!(hydrations(&host, "plan-1"), 2, "without a digest nothing vouches for a cached history");
}

#[test]
fn a_deleted_artifact_is_removed_and_said() {
    let host = seeded("summary-deleted", fixtures(), true);
    let mut d = listed(&host);
    host.control("/__control/fail", json!({"path": "/artifacts/form-a", "status": 404}));
    d.engine.set_artifact_in_view(Some("form-a"));
    d.until("gone", |e| e.artifact_hydration("form-a") == Hydration::Gone);
    assert!(!ids(&d).contains(&"form-a".to_owned()), "its row is gone");
    assert!(d.engine.error().contains("“Form form-a” was deleted on the Host"), "and the open view says so: {:?}", d.engine.error());
}

#[test]
fn only_the_old_hosts_exact_refusal_falls_back() {
    let host = seeded("summary-refused", fixtures(), true);
    host.control("/__control/artifact-summary", json!({"refuse": true}));
    let d = listed(&host);
    assert_eq!(list_representations(&host), ["summary-v1", "flat-v1"], "refused once, then asked for as before");
    assert_eq!(row(&d, "form-a")["content"], json!(FORM));
    assert!(d.engine.updates_error().is_empty(), "the fallback is not an error: {:?}", d.engine.updates_error());
    assert!(!d.engine.artifact_summary_enabled());

    let host = seeded("summary-errors", fixtures(), true);
    host.control("/__control/fail", json!({"path": "/artifacts", "status": 400}));
    let mut d = Driver::new(&host.base);
    d.connect();
    d.until("the list fails", |e| !e.updates_error().is_empty() && !e.updates_loading());
    assert!(d.engine.updates_error().contains("HTTP 400"), "another 400 is the user's to see: {:?}", d.engine.updates_error());
    assert_eq!(list_representations(&host), ["summary-v1"], "and is not taken for the old Host");
    assert!(d.engine.artifact_summary_enabled());
    host.control("/__control/fail", json!({"path": "/artifacts", "status": 500}));
    d.engine.load_updates();
    d.until("fails again", |e| e.updates_error().contains("HTTP 500") && !e.updates_loading());
    d.engine.load_updates();
    d.until("listed", |e| e.updates_error().is_empty() && !e.update_artifacts().is_empty() && !e.updates_loading());
    assert!(list_representations(&host).iter().all(|r| r == "summary-v1"), "{:?}", list_representations(&host));
}

#[test]
fn the_cache_keeps_to_its_caps() {
    let artifacts = json!([form("f1", "<p>one</p>", 1003), form("f2", "<p>two</p>", 1002), form("f3", "<p>three</p>", 1001)]);
    let host = seeded("summary-cap", artifacts, true);
    let mut d = listed(&host);
    d.engine.set_artifact_cache_limits(2, 1 << 20);
    for id in ["f1", "f2", "f3"] {
        d.engine.need_artifact(id);
        ready(&mut d, id);
        d.engine.release_artifact(id);
    }
    assert_eq!(d.engine.artifact_cache_size(), (2, "<p>two</p>".len() + "<p>three</p>".len()), "the least recently read went");
    assert!(row(&d, "f1").get("content").is_none(), "and its body left its row");
    assert_eq!(d.engine.artifact_hydration("f1"), Hydration::Omitted);
    d.engine.set_artifact_cache_limits(10, "<p>three</p>".len());
    assert_eq!(d.engine.artifact_cache_size(), (1, "<p>three</p>".len()), "by bytes too");
}

#[test]
fn the_big_report_is_read_only_when_opened() {
    let host = Host::start("summary-big");
    host.control("/__control/artifact-summary", json!({"on": true}));
    host.control("/__control/artifact-chat", json!({"session": "big", "types": ["html_big"]}));
    let mut d = listed(&host);
    let marker = row(&d, "big-report")["content_omitted"].clone();
    assert!(marker["bytes"].as_u64().unwrap() > 1_000_000, "{marker}");
    d.engine.need_artifact("big-report");
    d.engine.need_artifact("big-report");
    ready(&mut d, "big-report");
    assert_eq!(row(&d, "big-report")["content"].as_str().map(str::len), marker["bytes"].as_u64().map(|b| b as usize));
    assert_eq!((hydrations(&host, "big-report"), hydrations(&host, "big-net")), (1, 0));
}

#[test]
fn another_host_or_credential_empties_the_cache() {
    let host = seeded("summary-scope", fixtures(), true);
    let mut d = listed(&host);
    d.engine.need_artifact("form-a");
    ready(&mut d, "form-a");
    // Closed: nothing asks for it again after the cache is dropped.
    d.engine.release_artifact("form-a");
    assert_eq!(d.engine.artifact_cache_size().0, 1);

    // The same Host and token again: kept.
    d.engine.reconnect();
    d.until("reconnected", |e| e.connection_state() == "live");
    d.settle(Duration::from_millis(200));
    assert_eq!(d.engine.artifact_cache_size().0, 1);

    // Another credential.
    d.engine.connect_to_server(&host.base, "cld_probe_paired_device");
    d.until("cache dropped", |e| e.connection_state() == "live" && e.artifact_cache_size().0 == 0);
    d.until("listed", |e| !e.update_artifacts().is_empty() && !e.updates_loading());
    d.engine.need_artifact("form-a");
    ready(&mut d, "form-a");
    d.engine.release_artifact("form-a");
    assert_eq!(d.engine.artifact_cache_size().0, 1);

    // Another Host instance behind the same address.
    host.control("/__control/server-id", json!({"server_id": "fake-host-2"}));
    d.engine.reconnect();
    d.until("cache dropped again", |e| e.connection_state() == "live" && e.artifact_cache_size().0 == 0);
}

#[test]
fn a_read_for_a_row_that_moved_on_is_dropped() {
    let host = seeded("summary-late", fixtures(), true);
    host.control("/__control/artifact-summary", json!({"item_delay": 0.8}));
    let mut d = listed(&host);
    d.engine.set_artifact_in_view(Some("form-a"));
    assert_eq!(d.engine.artifact_hydration("form-a"), Hydration::Loading);
    // The form changes while it is read: the list's new marker retires
    // the read, whose late answer (the old body) never reaches the row.
    host.control("/__control/artifact-quiet-update", json!({"id": "form-a", "set": {"content": "<p>new</p>", "version": 3}}));
    d.engine.load_updates();
    d.until("the new version read", |e| e.update_artifacts().iter().any(|a| a["artifact_id"] == "form-a" && a["content"] == "<p>new</p>"));
    d.settle(Duration::from_millis(1000));
    assert_eq!(row(&d, "form-a")["content"], json!("<p>new</p>"), "the old body never came back");
    assert_eq!(row(&d, "form-a")["content_omitted"]["version"], json!(3));
    assert_eq!(hydrations(&host, "form-a"), 2, "the first read, retired, and the new version's");
}

#[test]
fn an_event_reads_a_small_form_whole_but_a_big_one_waits_for_the_list() {
    let host = Host::start("summary-events");
    host.control("/__control/artifact-summary", json!({"on": true}));
    host.control("/__control/artifact-chat", json!({"session": "big", "types": ["html_big", "html_form"]}));
    let mut d = listed(&host);
    d.engine.set_update_intervals(Intervals { artifacts: Duration::from_millis(300), ..Intervals::default() });
    let lists = host.requests("GET", "/artifacts").len();

    // A small form changes: read at once (as without summary lists), its
    // body kept in the cache and its row in summary shape.
    host.control("/__control/artifact-update", json!({"id": "form-trip", "set": {"title": "Offsite, take two"}}));
    d.until("the small form's change", |e| e.update_artifacts().iter().any(|a| a["artifact_id"] == "form-trip" && a["title"] == "Offsite, take two"));
    let trip = row(&d, "form-trip");
    assert_eq!((hydrations(&host, "form-trip"), d.engine.artifact_hydrations_sent("form-trip")), (1, 0), "one read on the wire, the event's");
    assert!(trip["content_omitted"]["sha256"].is_string() && trip["content"].as_str().is_some_and(|c| c.contains("destination")), "{trip}");
    assert_eq!(d.engine.need_artifact("form-trip"), Hydration::Ready, "the event's read serves an open");
    assert_eq!((hydrations(&host, "form-trip"), d.engine.artifact_hydrations_sent("form-trip")), (1, 0), "with no read of its own");

    // The 1.16 MB report changes: not read whole; the next summary list
    // (within its interval) carries the change.
    host.control("/__control/artifact-update", json!({"id": "big-report", "set": {"title": "Agentretten, revidert"}}));
    d.until("the big report's change", |e| e.update_artifacts().iter().any(|a| a["artifact_id"] == "big-report" && a["title"] == "Agentretten, revidert"));
    assert_eq!(host.requests("GET", "/artifacts/big-report").len(), 0, "the big body never crossed the wire");
    assert!(host.requests("GET", "/artifacts").len() > lists, "a summary list carried it");
    assert!(row(&d, "big-report").get("content").is_none());
}

#[test]
fn a_hash_of_another_kind_is_not_checked() {
    // `c9:` is no hash this client knows (and not of this content): the
    // body is identified by (id, updated_at, version) alone, as pending.
    let host = seeded("summary-unknown-hash", fixtures(), true);
    host.control("/__control/artifact-summary", json!({"unknown_hash": ["form-a"]}));
    let mut d = listed(&host);
    assert!(row(&d, "form-a")["content_omitted"]["sha256"].as_str().is_some_and(|h| h.starts_with("c9:")));
    d.engine.need_artifact("form-a");
    ready(&mut d, "form-a");
    assert_eq!(hydrations(&host, "form-a"), 1, "read once, not dropped as a mismatch");
    assert_eq!(row(&d, "form-a")["content"], json!(FORM));
}

/// The big report's body read whole, from a Host that honours flat-v1 on
/// the item route and from one that ignores it: c1: verified both ways.
fn hydrate_big(name: &str, item_ignores_flat: bool) -> (u64, String, String) {
    let host = Host::start(name);
    host.control("/__control/artifact-summary", json!({"on": true, "item_ignores_flat": item_ignores_flat}));
    host.control("/__control/artifact-chat", json!({"session": "big", "types": ["html_big"]}));
    let mut d = listed(&host);
    let sha = row(&d, "big-report")["content_omitted"]["sha256"].as_str().unwrap_or_default().to_owned();
    d.engine.need_artifact("big-report");
    d.engine.need_artifact("big-report");
    ready(&mut d, "big-report");
    assert_eq!((hydrations(&host, "big-report"), d.engine.artifact_hydrations_sent("big-report")), (1, 1), "two opens, one read");
    let content = row(&d, "big-report")["content"].as_str().unwrap_or_default().to_owned();
    let bytes = item_bytes(&host, "big-report");
    assert_eq!(bytes.len(), 1);
    (bytes[0], sha, clarp_core::artifact_summary::content_hash_of(&content))
}

#[test]
fn a_hydration_asks_for_flat_v1_and_reads_either_shape() {
    let (flat, sha, verified) = hydrate_big("summary-item-flat", false);
    assert!(sha.starts_with("c1:") && verified == sha, "the c1: hash holds on the flat-v1 body: {verified} {sha}");
    let (legacy, sha, verified) = hydrate_big("summary-item-legacy", true);
    assert!(sha.starts_with("c1:") && verified == sha, "and on the legacy body (payload ignored): {verified} {sha}");
    // The body is ~1.16 MB; the legacy answer carries it twice.
    assert!((1_100_000..1_300_000).contains(&flat), "flat-v1 hydration: {flat} bytes");
    assert!((2_200_000..2_500_000).contains(&legacy), "legacy hydration: {legacy} bytes");
}

/// One file of the canonical summary-v1 fixtures (`tests/fixtures/summary-v1/`,
/// Host dfc489bb; the core tests check each against its manifest).
fn canonical(name: &str) -> Value {
    let path = format!("{}/../tests/fixtures/summary-v1/{name}", env!("CARGO_MANIFEST_DIR"));
    serde_json::from_slice(&std::fs::read(&path).unwrap_or_else(|e| panic!("{path}: {e}"))).expect("valid JSON")
}

/// A raw `GET` to the fake Host: its status and parsed body (plan bodies are
/// compared parsed, never as bytes: the Host's `counts` key order varies).
fn get(host: &Host, path: &str) -> (u16, Value) {
    use std::io::{Read, Write};
    let address = host.base.trim_start_matches("http://");
    let mut stream = std::net::TcpStream::connect(address).expect("fake host reachable");
    write!(stream, "GET {path} HTTP/1.1\r\nHost: {address}\r\nAuthorization: Bearer probe-token\r\nConnection: close\r\n\r\n").unwrap();
    let mut response = String::new();
    stream.read_to_string(&mut response).unwrap();
    let status = response.split_whitespace().nth(1).and_then(|s| s.parse().ok()).unwrap_or(0);
    let body = response.split_once("\r\n\r\n").map_or("", |(_, body)| body);
    (status, serde_json::from_str(body).unwrap_or(Value::Null))
}

#[test]
fn the_fake_host_serves_the_canonical_snapshot_as_the_real_host_does() {
    // Loaded with the canonical rows, the fake derives every shape the Host
    // served for them: the three lists, both items in all three shapes, the
    // 400 and the 404.
    let host = Host::start("summary-canonical");
    host.control("/__control/canonical-summary", json!({}));
    for (query, file) in [("representation=summary-v1", "01-artifacts-summary-v1"), ("representation=flat-v1", "01-artifacts-flat-v1"), ("", "01-artifacts-default")] {
        assert_eq!(get(&host, &format!("/artifacts?limit=200&{query}")), (200, canonical(&format!("{file}.body"))), "{file}");
    }
    assert_eq!(get(&host, "/artifacts?representation=bogus"), (400, canonical("05-artifacts-bogus-400.body")));
    let ids = canonical("manifest.json")["db_seed"]["ids"].clone();
    for name in ["html_form_report", "plan_artifact"] {
        let id = ids[name].as_str().unwrap();
        for (query, shape) in [("", "default"), ("?representation=flat-v1", "flat-v1"), ("?representation=bogus", "bogus")] {
            assert_eq!(get(&host, &format!("/artifacts/{id}{query}")), (200, canonical(&format!("04-item-{name}-{shape}.body"))), "{name} {shape}");
        }
    }
    assert_eq!(get(&host, "/artifacts/artifact-none?representation=flat-v1"), (404, canonical("04-item-404.body")));

    // The desktop on it: the canonical summary rows, a form that hydrates and
    // verifies, a plan whose history is read for its view.
    let mut d = listed(&host);
    assert_eq!(d.engine.update_artifacts(), canonical("01-artifacts-summary-v1.body")["artifacts"].as_array().unwrap().as_slice());
    let report = ids["html_form_report"].as_str().unwrap();
    d.engine.need_artifact(report);
    ready(&mut d, report);
    assert_eq!(row(&d, report)["content"], canonical("04-item-html_form_report-flat-v1.body")["artifact"]["content"]);
    assert_eq!(d.engine.artifact_hydrations_sent(report), 1);
    let plan = ids["plan_artifact"].as_str().unwrap();
    d.engine.set_artifact_in_view(Some(plan));
    ready(&mut d, plan);
    assert_eq!(row(&d, plan)["plan"]["goal"]["history"], canonical("04-item-plan_artifact-flat-v1.body")["artifact"]["plan"]["goal"]["history"]);
    // Every vector form verifies, the empty one too.
    for vector in canonical("06-c1-vectors.json")["vectors"].as_array().unwrap() {
        let id = vector["artifact_id"].as_str().unwrap();
        d.engine.need_artifact(id);
        ready(&mut d, id);
        assert_eq!(row(&d, id)["content"], vector["content"], "{}", vector["name"]);
        assert_eq!(d.engine.artifact_hydrations_sent(id), 1, "{}: verified the first time", vector["name"]);
    }
}
