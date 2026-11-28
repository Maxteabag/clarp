//! `--check host-down --out DIR`: the Host not answering (the shared spec).
//! The fake Host goes away: the open chat shows its calm panel with its
//! saved messages under it, the composer says it is read-only, the list has
//! one quiet row, and Copy diagnostics puts private-data-free text on the
//! clipboard. It comes back: Try again, `Back online`, sending again. Then a
//! closed port with nothing cached: the full page, "Clarp isn't running".

use std::time::Duration;

use serde_json::json;
use slint::{ComponentHandle, Model};

use super::{Stage, app_now, check, control, run_stages, shot, view};
use clarp_engine::host_down::HostDownStatus;

use crate::HostDownBridge;

fn bridge(window: &crate::AppWindow) -> HostDownBridge<'_> {
    window.global::<HostDownBridge>()
}

pub fn host_down_check(out: String) {
    let (o1, o2, o3) = (out.clone(), out.clone(), out.clone());
    let base = std::rc::Rc::new(std::cell::RefCell::new(String::new()));
    let (base_down, base_back) = (base.clone(), base.clone());
    let came_back = std::rc::Rc::new(std::cell::Cell::new(false));
    let stages: Vec<Stage> = vec![
        ("ready", Box::new(move |app, _window, _| {
            let loaded = app.engine.borrow().conversation("rachel").is_some_and(|c| !c.rows().is_empty());
            if !loaded || app.engine.borrow().connection_state() != "live" {
                return false;
            }
            *base.borrow_mut() = app.engine.borrow().base_url().to_owned();
            check(app.engine.borrow().hosts_down().is_empty(), "a Host that answers has no panel");
            check(control("/__control/outage", &json!({"seconds": 600})).is_ok(), "the Host goes away");
            true
        })),
        ("chat panel", Box::new(move |app, window, _| {
            let down = app.engine.borrow().host_down_for_session("rachel");
            let Some(down) = down.filter(|d| !d.back_online && matches!(d.status, HostDownStatus::RetryIn(_))) else { return false };
            let pane = view();
            check(pane.down_shown && pane.down.host == "Fake Host" && pane.down.reason == down.reason.id(), &format!("the chat shows the panel: {:?} {:?}", pane.down.host, pane.down.reason));
            check(!pane.down.warning, "info grey, not a warning");
            check(pane.down.last_seen == "just now", &format!("when it last answered: {:?}", pane.down.last_seen));
            check(pane.down.status == "retry" && pane.down.retry_in.starts_with("in "), &format!("the quiet line: {:?} {:?}", pane.down.status, pane.down.retry_in));
            check(!pane.down.detail.is_empty(), "the last attempt sits behind Details");
            check(!pane.can_send, "sending waits for the Host");
            check(pane.read_only_host == "Fake Host", &format!("the read-only note names it: {:?}", pane.read_only_host));
            check(app.engine.borrow().conversation("rachel").is_some_and(|c| !c.rows().is_empty()), "the saved messages stay");
            let rows = bridge(window).get_rows();
            check(rows.row_count() == 1 && rows.row_data(0).is_some_and(|r| r.host == "Fake Host" && r.when == "just now"),
                &format!("one quiet list row: {:?}", rows.iter().map(|r| format!("{} · {}", r.host, r.when)).collect::<Vec<_>>()));
            check(window.get_error().is_empty(), &format!("no raw error over the chats: {:?}", window.get_error()));
            shot(&o1, "host-down-01-chat-panel");
            bridge(window).invoke_copy_diagnostics(down.url.clone().into());
            let clipboard = std::env::var_os("CLARP_TEST_CLIPBOARD").and_then(|p| std::fs::read_to_string(p).ok()).unwrap_or_default();
            check(clipboard.starts_with("Clarp diagnostics\n") && clipboard.lines().count() <= 15, &format!("the diagnostics: {clipboard}"));
            for private in ["127.0.0.1", "Fake Host", "fake-host", "probe-token", "rachel", "://"] {
                check(!clipboard.contains(private), &format!("the diagnostics leave out {private}: {clipboard}"));
            }
            check(bridge(window).get_copied(), "and say so");
            true
        })),
        ("copied", Box::new(move |_app, _window, elapsed| {
            if elapsed < Duration::from_millis(200) {
                return false;
            }
            shot(&o2, "host-down-02-copied");
            true
        })),
        ("back online", Box::new(move |app, _window, _| {
            if !came_back.replace(true) {
                check(control("/__control/outage", &json!({"seconds": 0})).is_ok(), "the Host comes back");
                let url = base_down.borrow().clone();
                app.engine.borrow_mut().host_try_again(&url);
                return false;
            }
            let back = app.engine.borrow().host_down_for_session("rachel");
            if !back.is_some_and(|b| b.back_online) {
                return false;
            }
            check(view().down.back && view().down.status == "back", &format!("the panel says so: {:?}", view().down.status));
            check(view().can_send, "sending again");
            shot(&o3, "host-down-03-back-online");
            true
        })),
        ("gone", Box::new(move |app, _window, _| {
            if !app.engine.borrow().hosts_down().is_empty() || app.engine.borrow().connection_state() != "live" {
                return false;
            }
            // A closed port, nothing cached: the full page.
            let closed = std::net::TcpListener::bind("127.0.0.1:0").map(|l| l.local_addr().map(|a| a.port()).unwrap_or(1)).unwrap_or(1);
            app.engine.borrow_mut().connect_to_server(&format!("http://127.0.0.1:{closed}"), "probe-token");
            true
        })),
        ("full page", Box::new(move |app, window, _| {
            let page = bridge(window).get_page();
            if !bridge(window).get_full_page() || page.reason != "server_stopped" {
                return false;
            }
            check(page.icon == "plug", &format!("its icon: {:?}", page.icon));
            check(app_now().overlay.borrow().as_str() != "connection", "the connection page stays closed");
            shot(&out, "host-down-04-full-page");
            let url = base_back.borrow().clone();
            app.engine.borrow_mut().connect_to_server(&url, "probe-token");
            true
        })),
        ("answering", Box::new(|app, window, _| {
            if app.engine.borrow().connection_state() != "live" || bridge(window).get_full_page() {
                return false;
            }
            check(bridge(window).get_rows().row_count() == 0, "no row once every Host answers");
            true
        })),
    ];
    run_stages(stages);
}
