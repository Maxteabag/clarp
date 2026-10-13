//! A minute of a busy Host's events against the Updates lists (the
//! 2026-10-13 incident: a desktop refetched the 50-artifact list, 1-8 MB,
//! on every artifact and attention event). Counts what each list read,
//! checks that the user's own actions still go at once, that an approval
//! still works, and that the lists end up as the Host has them.
//!
//! The table goes to stderr uncaptured, so a CI log shows it whether the
//! test passes or not; it runs unchanged against older engines for a
//! baseline.

mod common;

use std::io::Write;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use clarp_engine::Engine;
use common::{Driver, Host};
use serde_json::{Value, json};

const STREAM_SECONDS: f64 = 60.0;

fn epoch() -> f64 {
    SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_secs_f64()
}

/// Bytes that do not compress, as a form's inline pictures.
fn noise(len: usize, mut state: u64) -> String {
    const ALPHABET: &[u8] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    (0..len)
        .map(|_| {
            state ^= state << 13;
            state ^= state >> 7;
            state ^= state << 17;
            ALPHABET[(state % 64) as usize] as char
        })
        .collect()
}

/// 70 artifacts as a busy Host has them: forms with inline pictures, plans
/// with a goal history, documents, and one pending approval (the newest).
fn seed(now_ms: i64) -> Vec<Value> {
    (0..70)
        .map(|i: i64| {
            let updated = now_ms - (70 - i) * 60_000;
            let id = format!("seed-{i:02}");
            let mut artifact = json!({"artifact_id": id, "session": "rachel", "agent_id": "a1", "title": format!("Seed {i}"),
                                      "summary": "Seeded", "status": "ready", "created_at": updated, "updated_at": updated});
            let fields = artifact.as_object_mut().unwrap();
            if i == 69 {
                fields.insert("type".into(), json!("decision"));
                fields.insert("decision".into(), json!({"decision_id": "dg1", "status": "pending", "revision": 1,
                    "options": [{"id": "yes", "label": "Yes"}, {"id": "no", "label": "No"}]}));
            } else if i % 17 == 0 {
                let html = format!("<html><body><img src=\"data:image/png;base64,{}\"></body></html>", noise(250_000, i as u64 + 1));
                fields.insert("type".into(), json!("html_form"));
                fields.insert("content".into(), json!(html));
                fields.insert("version".into(), json!(1));
                fields.insert("read_only".into(), json!(true));
            } else if i % 3 == 0 {
                let history: Vec<Value> = (0..100).map(|n| json!({"at": updated - n, "note": "a step of the goal ".repeat(10)})).collect();
                fields.insert("type".into(), json!("plan"));
                fields.insert("plan".into(), json!({"title": format!("Plan {i}"), "status": "active",
                    "items": [{"title": "One", "status": "completed"}, {"title": "Two", "status": "in_progress"}],
                    "goal": {"history": history}}));
            } else {
                fields.insert("type".into(), json!("document"));
                fields.insert("content".into(), json!("# Notes\n\n".to_owned() + &"Some findings. ".repeat(140)));
            }
            artifact
        })
        .collect()
}

fn summary(items: &[Value], keys: &[&str]) -> Vec<Vec<Value>> {
    items.iter().map(|item| keys.iter().map(|k| item.get(*k).cloned().unwrap_or(Value::Null)).collect()).collect()
}

/// The engine's lists as the Host has them now.
fn matches_host(engine: &Engine, host: &Value) -> bool {
    let artifact_keys = ["artifact_id", "updated_at", "title", "archived_at", "status"];
    let empty = Vec::new();
    let list = |key: &str| host[key].as_array().unwrap_or(&empty).clone();
    summary(engine.update_artifacts(), &artifact_keys) == summary(&list("artifacts"), &artifact_keys)
        && summary(engine.attention_items(), &["id"]) == summary(&list("attention"), &["id"])
        && summary(engine.background_jobs(), &["job_id", "metadata", "updated_at"]) == summary(&list("jobs"), &["job_id", "metadata", "updated_at"])
}

#[derive(Default, Debug)]
struct Route {
    requests: usize,
    bytes: u64,
}

fn route(path: &str) -> Option<&'static str> {
    match path {
        "/artifacts" => Some("GET /artifacts (list)"),
        "/attention" => Some("GET /attention"),
        "/background-jobs" => Some("GET /background-jobs"),
        p if p.starts_with("/artifacts/") && p.matches('/').count() == 2 => Some("GET /artifacts/<id>"),
        _ => None,
    }
}

const ROUTES: [&str; 4] = ["GET /artifacts (list)", "GET /artifacts/<id>", "GET /attention", "GET /background-jobs"];

fn traffic(host: &Host, from: f64, to: f64) -> std::collections::BTreeMap<&'static str, Route> {
    let mut routes: std::collections::BTreeMap<&'static str, Route> = ROUTES.iter().map(|r| (*r, Route::default())).collect();
    for line in host.log() {
        let at = line["at"].as_f64().unwrap_or(0.0);
        if at < from || at > to {
            continue;
        }
        let Some(name) = line["path"].as_str().and_then(route) else { continue };
        let entry = routes.get_mut(name).unwrap();
        match line["method"].as_str() {
            Some("GET") => entry.requests += 1,
            Some("RESPONSE") => entry.bytes += line["bytes"].as_u64().unwrap_or(0),
            _ => {}
        }
    }
    routes
}

/// The first `GET path` the Host saw at or after `since`.
fn first_get_since(host: &Host, path: &str, since: f64) -> Option<f64> {
    host.requests("GET", path).iter().filter_map(|r| r["at"].as_f64()).find(|at| *at >= since)
}

#[test]
fn a_minute_of_continuous_events_reads_each_list_within_bounds() {
    let host = Host::start("sse-load");
    let now_ms = (epoch() * 1000.0) as i64;
    let job = json!({"job_id": "j1", "agent_id": "a1", "session": "rachel", "status": "running", "title": "Index",
                     "metadata": {"completed": 0, "total": 1000}, "updated_at": 1});
    host.control("/__control/stream", json!({"seed": seed(now_ms), "jobs": [job],
                                             "attention": [{"id": "dg1", "session": "rachel", "kind": "decision"}]}));
    let mut d = Driver::new(&host.base);
    d.connect();
    d.until("the first lists", |e| e.update_artifacts().len() == 50 && e.background_jobs().len() == 1 && e.attention_items().len() == 1 && !e.updates_loading());
    d.settle(Duration::from_secs(1));

    let start = epoch();
    host.control("/__control/stream", json!({"seconds": STREAM_SECONDS, "artifact_every": 1.0, "attention_every": 3.0,
                                             "job_every": 0.3, "rng_seed": 11}));
    let began = Instant::now();
    d.settle(Duration::from_secs(20));

    // The user refreshes: the list goes now, whatever the interval says.
    let asked = epoch();
    d.engine.load_updates();
    d.until("the refresh's list", |_| first_get_since(&host, "/artifacts", asked).is_some());
    let refresh_delay = first_get_since(&host, "/artifacts", asked).unwrap() - asked;
    d.settle(Duration::from_secs(40).saturating_sub(began.elapsed()));

    // The user approves: sent at once, and the lists reload after it.
    let asked = epoch();
    d.engine.resolve_decision("dg1", "yes", 1);
    d.until("the approval answered", |e| !e.update_action_pending("decision", "dg1"));
    let answered = epoch();
    let approval_sent = host.requests("POST", "/decisions/dg1/resolve").first().and_then(|r| r["at"].as_f64());
    d.until("the approval's reload", |_| first_get_since(&host, "/artifacts", answered).is_some());
    let approval_reload = first_get_since(&host, "/artifacts", answered).unwrap() - answered;
    d.until("the approval shown", |e| {
        e.attention_items().iter().all(|a| a["id"] != "dg1")
            && e.update_artifacts().iter().any(|a| a["artifact_id"] == "seed-69" && a["decision"]["status"] == "accepted")
    });

    let left = Duration::from_secs_f64(STREAM_SECONDS).saturating_sub(began.elapsed());
    d.settle(left + Duration::from_millis(500));
    let end = start + STREAM_SECONDS + 0.5;

    // Eventually fresh: the lists match the Host's once the stream stops.
    let settled_at = Instant::now();
    d.until_within("the lists as the Host has them", Duration::from_secs(45), |e, _| {
        matches_host(e, &host.control_json("/__control/state", json!({"limit": 50})))
    });
    let fresh_after = settled_at.elapsed().as_secs_f64();

    let routes = traffic(&host, start, end);
    let flat = host.requests("GET", "/artifacts").iter().filter(|r| r["at"].as_f64().unwrap_or(0.0) >= start)
        .all(|r| r["query"]["representation"] == "flat-v1");
    let mut table = format!("\nSSE-LOAD GATE ({STREAM_SECONDS} s; artifact event 1/s + attention, attention 1/3 s, job 1/0.3 s)\n");
    table += "| route | requests | bytes (raw JSON) |\n|---|---|---|\n";
    for (name, r) in &routes {
        table += &format!("| {name} | {} | {} |\n", r.requests, r.bytes);
    }
    table += &format!(
        "refresh -> list GET {:.3} s; approval sent {:.3} s after asking, reload {:.3} s after its answer; \
         fresh {:.1} s after the stream; every list flat-v1: {flat}\n",
        refresh_delay,
        approval_sent.map_or(f64::NAN, |at| at - asked),
        approval_reload,
        fresh_after
    );
    std::io::stderr().write_all(table.as_bytes()).unwrap();
    if let Ok(path) = std::env::var("CLARP_SSE_LOAD_REPORT") {
        std::fs::write(path, &table).unwrap();
    }

    assert!(refresh_delay < 2.5, "a refresh reloads at once: {refresh_delay:.3} s");
    assert!(approval_sent.is_some_and(|at| at - asked < 1.0), "the approval is sent at once");
    assert!(approval_reload < 2.5, "the approval's reload goes at once: {approval_reload:.3} s");
    let lists = routes["GET /artifacts (list)"].requests;
    assert!(lists <= 6, "the artifact list at most once per 30 s plus the user's two: {lists}");
    let attention = routes["GET /attention"].requests;
    assert!(attention <= 16, "attention at most once per 5 s plus the user's two: {attention}");
    let jobs = routes["GET /background-jobs"].requests;
    assert!(jobs <= 34, "jobs at most once per 2 s plus the user's two: {jobs}");
    let items = routes["GET /artifacts/<id>"].requests;
    assert!((1..=62).contains(&items), "one read per named artifact at most: {items}");
    assert!(flat, "the list asks for flat-v1");
}
