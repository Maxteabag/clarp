//! `--check all-hosts`: "Chats from all paired Hosts" (Maxteabag/clarp#651)
//! against two fake Hosts. The desktop stays connected to the first; with
//! the setting on it also reads the second (`--peer`, server id
//! `fake-host-2`, "Peer Host") with its own token, and the chat list holds
//! both Hosts' agents, newest first, ties to the primary, each row naming
//! its Host. A row that moved to the primary (by server id) is left out, one
//! that moved to a Host never paired stays. Opening a peer's chat reads it
//! from the peer; sending, stopping, live updates and a decision go to the
//! peer and never to the primary. A peer that cannot be reached keeps its
//! rows with a quiet "Peer Host unreachable" line, and turning the setting
//! off leaves only the primary's chats.

use std::time::Duration;

use serde_json::{Value, json};
use slint::Model;
use slint::platform::Key;

use super::{Stage, app_now, check, posts, run_stages, shot, view};
use crate::headless;

const NOVA: &str = "@fake-host-2/nova";

fn peer_url() -> String {
    std::env::var("CLARP_TEST_PEER_URL").unwrap_or_default()
}

/// The peer fake Host's requests (`CLARP_TEST_PEER_LOG`) for `method` and `path`.
fn peer_requests(method: &str, path: &str) -> Vec<Value> {
    let Some(log) = std::env::var_os("CLARP_TEST_PEER_LOG") else { return Vec::new() };
    std::fs::read_to_string(log)
        .unwrap_or_default()
        .lines()
        .filter_map(|line| serde_json::from_str::<Value>(line).ok())
        .filter(|entry| entry["method"] == method && entry["path"] == path)
        .collect()
}

/// A test control posted to the peer fake Host.
fn peer_control(path: &str, body: &Value) -> Result<(), String> {
    use std::io::{Read, Write};
    let base = peer_url();
    let host = base.strip_prefix("http://").ok_or_else(|| format!("not a loopback Host: {base}"))?.trim_end_matches('/').to_owned();
    let body = body.to_string();
    let mut stream = std::net::TcpStream::connect(&host).map_err(|e| e.to_string())?;
    let request = format!(
        "POST {path} HTTP/1.1\r\nHost: {host}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
        body.len()
    );
    stream.write_all(request.as_bytes()).map_err(|e| e.to_string())?;
    let mut reply = String::new();
    stream.read_to_string(&mut reply).map_err(|e| e.to_string())?;
    if reply.starts_with("HTTP/1.1 200") { Ok(()) } else { Err(reply.lines().next().unwrap_or_default().to_owned()) }
}

fn sessions(app: &crate::App) -> Vec<String> {
    app.engine.borrow().roster().agents().iter().map(|a| a.session.clone()).collect()
}

/// The explorer's rows as drawn: (session, Host label).
fn drawn(window: &crate::AppWindow) -> Vec<(String, String)> {
    window.get_chats().iter().map(|c| (c.session.to_string(), c.host.to_string())).collect()
}

fn texts(app: &crate::App, session: &str) -> Vec<String> {
    app.engine.borrow().conversation(session).map(|c| c.rows().iter().map(|r| r.text.clone()).collect()).unwrap_or_default()
}

pub fn all_hosts_check(out: String) {
    let (out_open, out_gone) = (out.clone(), out.clone());
    let primary_posts_before = std::rc::Rc::new(std::cell::Cell::new(0usize));
    let posts_before = primary_posts_before.clone();
    let stages: Vec<Stage> = vec![
        ("ready", Box::new(|app, window, _| {
            let loaded = app.engine.borrow().conversation("rachel").is_some_and(|c| !c.rows().is_empty());
            if !loaded || peer_url().is_empty() {
                return false;
            }
            check(!app.engine.borrow().all_hosts(), "the setting is off by default");
            check(sessions(app) == ["rachel", "mike"], &format!("off, the list is the primary's alone: {:?}", sessions(app)));
            let primary = app.engine.borrow().base_url().to_owned();
            app.engine
                .borrow_mut()
                .settings_mut()
                .set(clarp_core::hosts::KEY, json!([{"url": primary, "name": "Fake Host"}, {"url": peer_url(), "name": "Peer Host"}]));
            // Settings → "Chats from all paired Hosts", as Enter on its row does.
            check(crate::look::step_setting(&app_now(), window, "all-hosts", 1), "the setting is on the settings page");
            check(app.engine.borrow().all_hosts(), "and is on now");
            check(
                app.engine.borrow().settings().boolean(clarp_core::all_hosts::SETTING, false),
                &format!("kept on this device under {}", clarp_core::all_hosts::SETTING),
            );
            true
        })),
        ("merged", Box::new(move |app, window, _| {
            if !sessions(app).iter().any(|s| s == NOVA) || drawn(window).len() < 5 {
                return false;
            }
            let order = sessions(app);
            check(
                order == ["@fake-host-2/wanderer", "rachel", NOVA, "mike", "@fake-host-2/tia"],
                &format!("both Hosts' chats, newest first, a tie to the primary: {order:?}"),
            );
            check(!order.iter().any(|s| s == "@fake-host-2/rachel"), "the peer's Rachel moved to the primary (by server id): left out");
            let rows = drawn(window);
            let host_of = |session: &str| rows.iter().find(|(s, _)| s == session).map(|(_, h)| h.clone()).unwrap_or_default();
            check(host_of("rachel") == "Fake Host", &format!("a primary row names its Host: {:?}", host_of("rachel")));
            check(host_of(NOVA) == "Peer Host", &format!("a peer row names its Host: {:?}", host_of(NOVA)));
            check(host_of("@fake-host-2/wanderer") == "Peer Host", "a row that moved to a Host never paired shows on one Host");
            let saved = clarp_core::hosts::list(app.engine.borrow().settings());
            check(
                saved.iter().any(|h| h.url == peer_url() && h.server_id == "fake-host-2"),
                &format!("the peer's server id is saved with it: {saved:?}"),
            );
            check(peer_requests("GET", "/agents/snapshot").iter().all(|r| r["authorization"] == "Bearer probe-token"), "the peer is read with its own token");
            check(posts("/select").iter().all(|p| !p["body"]["session"].as_str().unwrap_or("").starts_with('@')), "the primary is never asked about a peer's chat");
            shot(&out, "all-hosts-01-merged");
            posts_before.set(posts("/send").len());
            app.engine.borrow_mut().select(NOVA);
            crate::pump_now(&app_now());
            true
        })),
        ("open", Box::new(move |app, _, _| {
            if !texts(app, NOVA).iter().any(|t| t == "Nova here, on the peer Host") {
                return false;
            }
            check(app.engine.borrow().selected_session() == NOVA, "the peer's chat is open");
            check(peer_requests("POST", "/select").iter().any(|p| p["body"]["session"] == "nova"), "the peer is told which chat is open, by its own session");
            check(peer_requests("GET", "/log").iter().any(|p| p["query"]["session"] == "nova" || p["url"].as_str().unwrap_or("").contains("session=nova")), "and its log is read from the peer");
            check(view().name == "Nova · Peer Host", &format!("the header names the chat's Host: {:?}", view().name));
            shot(&out_open, "all-hosts-02-peer-chat");
            app_now().focus_composer();
            headless::type_text("hello peer");
            headless::press(Key::Return);
            true
        })),
        ("send", Box::new(move |app, _, _| {
            if !texts(app, NOVA).iter().any(|t| t == "Echo: hello peer") {
                return false;
            }
            let sent = peer_requests("POST", "/send");
            check(sent.iter().any(|p| p["body"]["session"] == "nova" && p["body"]["text"] == "hello peer"), &format!("the send went to the peer, as nova: {sent:?}"));
            check(posts("/send").len() == primary_posts_before.get(), "and not to the primary");
            // Drafts are kept per Host: the peer's Nova and a primary chat of
            // the same name never share one.
            let mut engine = app.engine.borrow_mut();
            engine.set_draft(NOVA, "peer draft");
            check(engine.draft(NOVA) == "peer draft" && engine.draft("nova").is_empty(), "a peer chat's draft is its own");
            engine.set_draft(NOVA, "");
            engine.stop_session(NOVA);
            true
        })),
        ("stop", Box::new(|_, _, _| {
            let stops = peer_requests("POST", "/stop");
            if stops.is_empty() {
                return false;
            }
            check(stops.iter().any(|p| p["body"]["session"] == "nova"), "Stop goes to the peer, as nova");
            check(posts("/stop").is_empty(), "and never to the primary");
            let pushed = peer_control("/__control/upsert", &json!({"session": "nova", "turns": [{"id": "n9", "role": "assistant", "text": "Pushed from the peer"}]}));
            check(pushed.is_ok(), &format!("the peer pushes a new reply: {pushed:?}"));
            true
        })),
        ("live", Box::new(|app, _, _| {
            if !texts(app, NOVA).iter().any(|t| t == "Pushed from the peer") {
                return false;
            }
            check(true, "the peer's events update its chat live");
            let items = json!({"items": [{"id": "pd1", "decision_id": "pd1", "session": "nova", "kind": "decision", "title": "Peer decision",
                "question": "Ship it?", "agent_name": "Nova", "revision": 1}]});
            let set = peer_control("/__control/attention", &items);
            check(set.is_ok(), &format!("the peer has a decision: {set:?}"));
            app.engine.borrow_mut().load_updates();
            true
        })),
        ("attention", Box::new(|app, _, _| {
            let item = app.engine.borrow().attention_items().iter().find(|i| i["decision_id"] == "pd1").cloned();
            let Some(item) = item else { return false };
            check(item["host"] == "Peer Host", &format!("the peer's decision names its Host: {item}"));
            check(item["session"] == NOVA, "and its chat by server id and session");
            app.engine.borrow_mut().select("rachel");
            crate::pump_now(&app_now());
            let queue = app.engine.borrow().attention_queue();
            check(queue.iter().any(|s| s == NOVA), &format!("Ctrl+J visits the peer's chat: {queue:?}"));
            app.engine.borrow_mut().resolve_decision("pd1", "yes", 1);
            true
        })),
        ("resolved", Box::new(|_, _, _| {
            let resolved = peer_requests("POST", "/decisions/pd1/resolve");
            if resolved.is_empty() {
                return false;
            }
            check(posts("/decisions/pd1/resolve").is_empty(), "the decision is answered on the peer, not the primary");
            let down = peer_control("/__control/outage", &json!({"seconds": 600}));
            check(down.is_ok(), &format!("the peer goes away: {down:?}"));
            true
        })),
        ("unreachable", Box::new(move |app, window, elapsed| {
            let note = window.get_roster_note().to_string();
            if !note.contains("Peer Host unreachable") || elapsed < Duration::from_millis(300) {
                return false;
            }
            let engine = app.engine.borrow();
            let nova = engine.roster().find(NOVA).cloned();
            check(nova.as_ref().is_some_and(|a| a.latest_state == "offline"), &format!("the peer's rows stay, offline: {:?}", nova.map(|a| a.latest_state)));
            check(engine.connection_state() == "live", &format!("the primary is not slowed: {}", engine.connection_state()));
            check(engine.roster().find("rachel").is_some_and(|a| a.latest_state != "offline"), "and its rows are as they were");
            drop(engine);
            shot(&out_gone, "all-hosts-03-unreachable");
            check(crate::look::step_setting(&app_now(), window, "all-hosts", 1), "the setting turns off");
            true
        })),
        ("off", Box::new(|app, window, _| {
            if sessions(app).iter().any(|s| s.starts_with('@')) {
                return false;
            }
            check(sessions(app) == ["rachel", "mike"], &format!("off, only the primary's chats: {:?}", sessions(app)));
            check(drawn(window).iter().all(|(_, host)| host.is_empty()), "rows name no Host with one Host listed");
            check(!window.get_roster_note().contains("unreachable"), "and the quiet line is gone");
            true
        })),
    ];
    run_stages(stages);
}
