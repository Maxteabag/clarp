//! What the desktop shows while its Host goes away and comes back, as a
//! timeline: the engine the app draws from, run headless against a Host,
//! one JSON line each time something the user sees changes (the status
//! label, the error banner, the chat's row state and availability line, the
//! open chat's rows). Reads only; it sends nothing.
//!
//!   CLARP_PROBE_URL=http://127.0.0.1:PORT CLARP_PROBE_TOKEN=cld_… \
//!   CLARP_PROBE_SESSION=probe CLARP_PROBE_SECONDS=120 \
//!   cargo run -p clarp-engine --example outage_probe > timeline.jsonl

use std::sync::{Arc, Condvar, Mutex};
use std::time::{Duration, Instant};

use clarp_core::availability::{Clock, line};
use clarp_core::settings::Settings;
use clarp_engine::{Config, Engine};
use serde_json::{Value, json};

fn env(name: &str, default: &str) -> String {
    std::env::var(name).unwrap_or_else(|_| default.to_owned())
}

fn seen(engine: &Engine, session: &str) -> Value {
    let conversation = engine.conversation(session);
    let rows: Vec<Value> = conversation
        .map(|c| c.rows().iter().map(|r| json!({"id": r.id, "role": r.role, "text": r.text.chars().take(60).collect::<String>()})).collect())
        .unwrap_or_default();
    let chat_error = conversation.map(|c| c.error().to_owned()).unwrap_or_default();
    // The banner as the app picks it (slint-app main.rs): the Host's error,
    // else the open chat's.
    let banner = if engine.error().is_empty() { chat_error.clone() } else { engine.error().to_owned() };
    json!({
        "label": engine.connection_state(),
        "connected": engine.connected(),
        "banner": banner,
        "row_state": engine.roster().display_state(session),
        "availability": engine.roster().availability(session).and_then(|a| line(a, Clock::now(0))),
        "rows": rows,
        "outage": if engine.outage_shown() { "shown" } else if engine.outage_active() { "quiet" } else { "none" },
        "catching_up": engine.outage_catching_up(),
        "server_version": engine.server_version(),
        "server_id": engine.server_id(),
    })
}

fn main() {
    let url = env("CLARP_PROBE_URL", "");
    let token = env("CLARP_PROBE_TOKEN", "");
    let session = env("CLARP_PROBE_SESSION", "probe");
    let seconds: u64 = env("CLARP_PROBE_SECONDS", "120").parse().unwrap_or(120);
    assert!(!url.is_empty() && !token.is_empty(), "set CLARP_PROBE_URL and CLARP_PROBE_TOKEN");
    let woken = Arc::new((Mutex::new(false), Condvar::new()));
    let signal = woken.clone();
    let config = Config { base_url: url, token, settings: Settings::in_memory(), workspace_store: None, keyring: false, transcript_cache: None, form_events: None };
    let mut engine = Engine::new(config, move || {
        let (flag, condvar) = &*signal;
        *flag.lock().unwrap() = true;
        condvar.notify_all();
    })
    .expect("engine starts");
    engine.start();
    let started = Instant::now();
    let mut last = Value::Null;
    let mut selected = false;
    while started.elapsed() < Duration::from_secs(seconds) {
        engine.pump();
        if !selected && engine.roster().find(&session).is_some() {
            engine.select(&session);
            selected = true;
        }
        let now = seen(&engine, &session);
        if now != last {
            let wall_ms = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_millis() as u64;
            println!("{}", json!({"t_ms": started.elapsed().as_millis() as u64, "wall_ms": wall_ms, "seen": now}));
            last = now;
        }
        let (flag, condvar) = &*woken;
        let guard = flag.lock().unwrap();
        let (mut guard, _) = condvar.wait_timeout_while(guard, Duration::from_millis(100), |w| !*w).unwrap();
        *guard = false;
    }
    engine.shutdown();
}
