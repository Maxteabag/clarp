//! A Host that does not answer (the engine's `host_down`): the list's rows,
//! the full page, the app-wide offline banner, the panel's buttons, and the
//! one-second tick that counts the status line down while one is out of
//! reach. The chat panel itself is the pane's (`panes.rs`).

use std::cell::RefCell;
use std::time::Duration;

use clarp_core::host_down;
use clarp_engine::Change;
use clarp_engine::host_down::HostDownView;
use slint::{ComponentHandle, ModelRc, VecModel};

use crate::{App, AppWindow, HostDownBridge, HostDownPanel, HostDownRow};

thread_local! {
    /// The Host the full page shows ("" for the primary's).
    static PAGE: RefCell<String> = const { RefCell::new(String::new()) };
    static TICK: RefCell<Option<slint::Timer>> = const { RefCell::new(None) };
}

/// Where Check Tailscale goes: the tailnet's machines, which says whether
/// this device and the Host are connected.
const TAILSCALE_PAGE: &str = "https://login.tailscale.com/admin/machines";

fn since(view: &HostDownView) -> String {
    host_down::since_time(view.since_ms, &chrono::Local::now())
}

/// The panel's fields for the window.
pub fn panel(view: &HostDownView) -> HostDownPanel {
    HostDownPanel {
        url: view.url.clone().into(),
        title: view.title().into(),
        reason: view.sentence().into(),
        since: host_down::since_line(&since(view)).into(),
        status: view.status.clone().into(),
        icon: view.reason.icon().into(),
        action: view.action.map_or("", |a| a.id()).into(),
        action_label: view.action.map_or("", |a| a.label()).into(),
        switch: view.switch,
        back: view.back_online,
    }
}

/// The platform and build the diagnostics name.
fn platform() -> &'static str {
    if cfg!(target_os = "macos") { "desktop-macos" } else if cfg!(target_os = "windows") { "desktop-windows" } else { "desktop-linux" }
}

/// After a pump: the rows, the page, the banner, and whether the tick runs.
pub fn apply(app: &App, window: &AppWindow, changes: &[Change]) {
    if !changes.iter().any(|c| matches!(c, Change::HostDown | Change::Connection | Change::Roster | Change::ServerInfo | Change::Hosts)) {
        return;
    }
    let engine = app.engine.borrow();
    let down = engine.hosts_down();
    let bridge = window.global::<HostDownBridge>();
    let rows: Vec<HostDownRow> = down
        .iter()
        .filter(|view| !view.back_online)
        .map(|view| HostDownRow { url: view.url.clone().into(), text: host_down::list_row(&view.label, &since(view)).into() })
        .collect();
    bridge.set_rows(ModelRc::new(VecModel::from(rows)));
    bridge.set_device_offline(engine.device_offline());
    // The page: the Host opened from the list, else the primary's.
    let wanted = PAGE.with(|p| p.borrow().clone());
    let shown = down.iter().find(|v| v.url == wanted).or_else(|| down.iter().find(|v| v.url == engine.base_url()));
    // Nothing to read without the primary and nothing cached: its page
    // takes the chat area (the connection page is for a desktop with no Host).
    let primary = down.iter().find(|v| v.url == engine.base_url() && !v.back_online);
    bridge.set_full_page(primary.is_some() && engine.roster().agents().is_empty());
    match shown {
        Some(view) => bridge.set_page(panel(view)),
        None if app.overlay.borrow().as_str() == "host-down" => {
            // Its Host answers again: the page goes once Back online has shown.
            drop(engine);
            crate::commands::close_overlay(app, window);
            return;
        }
        None => {}
    }
    let any = !down.is_empty();
    drop(engine);
    ticking(any);
}

/// The status line counts down once a second while a Host is out of reach;
/// the tick also notices a change in this device's network.
fn ticking(on: bool) {
    TICK.with(|tick| {
        let mut tick = tick.borrow_mut();
        if on && tick.is_none() {
            let timer = slint::Timer::default();
            timer.start(slint::TimerMode::Repeated, Duration::from_secs(1), || {
                if let Some(app) = crate::app()
                    && let Ok(mut engine) = app.engine.try_borrow_mut()
                {
                    engine.host_down_tick();
                    drop(engine);
                    app.refresh(&[Change::HostDown]);
                    crate::pump_now(&app);
                }
            });
            *tick = Some(timer);
        } else if !on {
            *tick = None;
        }
    });
}

/// The window came to the front: every Host out of reach is asked at once.
pub fn window_focused(app: &App) {
    if !app.engine.borrow().hosts_down().is_empty() {
        app.engine.borrow_mut().hosts_probe_now();
    }
}

fn show_toast(window: &AppWindow, text: &str) {
    window.global::<HostDownBridge>().set_toast(text.into());
    let weak = window.as_weak();
    slint::Timer::single_shot(Duration::from_secs(2), move || {
        if let Some(window) = weak.upgrade() {
            window.global::<HostDownBridge>().set_toast("".into());
        }
    });
}

fn open_connection(app: &App, window: &AppWindow) {
    crate::commands::run(app, window, "connection");
}

pub fn wire(window: &AppWindow) {
    let bridge = window.global::<HostDownBridge>();
    bridge.on_try_again(|url| {
        if let Some(app) = crate::app() {
            app.engine.borrow_mut().host_try_again(&url);
            crate::pump_now(&app);
        }
    });
    bridge.on_act(|_url, action| {
        let (Some(app), Some(window)) = (crate::app(), crate::window()) else { return };
        match action.as_str() {
            // Pairing and the address are the connection page's.
            "pair" | "address" => open_connection(&app, &window),
            "tailscale" => crate::view::open_link(TAILSCALE_PAGE),
            other => eprintln!("clarp-slint: no Host-down action {other}"),
        }
    });
    bridge.on_switch_host(|| {
        if let (Some(app), Some(window)) = (crate::app(), crate::window()) {
            open_connection(&app, &window);
        }
    });
    bridge.on_copy_diagnostics(|url| {
        let (Some(app), Some(window)) = (crate::app(), crate::window()) else { return };
        let text = app.engine.borrow().host_diagnostics(&url, platform(), env!("CARGO_PKG_VERSION"), option_env!("CLARP_BUILD").unwrap_or("dev"));
        match crate::platform::clipboard::copy(&text) {
            Ok(()) => show_toast(&window, host_down::DIAGNOSTICS_COPIED),
            Err(error) => eprintln!("clarp-slint: could not copy the diagnostics: {error}"),
        }
    });
    bridge.on_open(|url| {
        let (Some(app), Some(window)) = (crate::app(), crate::window()) else { return };
        PAGE.with(|p| *p.borrow_mut() = url.to_string());
        if let Some(view) = app.engine.borrow().host_down(&url) {
            window.global::<HostDownBridge>().set_page(panel(&view));
        }
        crate::commands::open_overlay(&app, &window, "host-down");
    });
    bridge.on_close(|| {
        if let (Some(app), Some(window)) = (crate::app(), crate::window()) {
            PAGE.with(|p| p.borrow_mut().clear());
            crate::commands::close_overlay(&app, &window);
        }
    });
}
