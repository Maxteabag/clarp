//! A Host that does not answer (the engine's `host_down`): the list's rows,
//! the full page, the app-wide offline banner, the panel's buttons, and the
//! one-second tick that counts the status line down while one is out of
//! reach. The chat panel itself is the pane's (`panes.rs`).

use std::cell::RefCell;
use std::time::Duration;

use clarp_core::i18n::{self, format};
use clarp_engine::Change;
use clarp_engine::host_down::{HostDownStatus, HostDownView};
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

fn now_ms() -> i64 {
    chrono::Utc::now().timestamp_millis()
}

/// `{when}`: how long ago, through the locale formatter ("2 hours ago"),
/// made each time the panel is drawn so a language change shows at once.
fn ago(at_ms: i64) -> String {
    let seconds = (now_ms() - at_ms) / 1000;
    if seconds < 45 {
        // i18n: Host-down panel and chat-list row, {when} in "Last answered {when}" / "{host} isn't answering · {when}" for under a minute ago (adverb phrase, lower case: it sits mid-sentence)
        return clarp_core::tr!("host_down.when.now" => "just now");
    }
    format::relative(i18n::current(), -seconds)
}

/// `{in}` of "Trying again {in}": the next probe, at most a minute off.
fn within(left: Duration) -> String {
    let seconds = (left.as_secs() + u64::from(left.subsec_nanos() > 0)).max(1) as i64;
    if seconds < 45 {
        // i18n: Host-down panel, status line, {in} in "Trying again {in}": the next automatic try is {n} seconds away
        return clarp_core::tr!("host_down.status.retry_in.seconds" => "in {n} second" | "in {n} seconds" % seconds);
    }
    format::relative(i18n::current(), seconds)
}

/// When it last answered, relative; "" when never known.
fn last_seen(view: &HostDownView) -> String {
    view.last_ok_ms.map(ago).unwrap_or_default()
}

/// The panel's fields for the window: codes and times; the words are
/// host_down.slint's.
pub fn panel(view: &HostDownView) -> HostDownPanel {
    let (status, retry_in) = match view.status {
        HostDownStatus::Reconnecting => ("reconnecting", String::new()),
        HostDownStatus::RetryIn(left) => ("retry", within(left)),
        HostDownStatus::BackOnline => ("back", String::new()),
    };
    HostDownPanel {
        url: view.url.clone().into(),
        host: view.label.clone().into(),
        reason: view.reason.id().into(),
        last_seen: last_seen(view).into(),
        status: status.into(),
        retry_in: retry_in.into(),
        icon: view.reason.icon().into(),
        action: view.action.map_or("", |a| a.id()).into(),
        switch: view.switch,
        back: view.back_online,
        warning: view.reason.warning(),
        detail: view.detail.clone().into(),
    }
}

/// A chat's pane (mounted from `panes.rs`): the panel under the header
/// while its Host does not answer, its saved messages readable, sending
/// off with the read-only note.
pub fn pane(engine: &clarp_engine::Engine, view: &mut crate::PaneView, session: &str) {
    let down = if session.is_empty() || view.pair { None } else { engine.host_down_for_session(session) };
    let read_only = down.as_ref().is_some_and(|d| !d.back_online);
    view.down_shown = down.is_some();
    view.read_only_host = down.as_ref().filter(|_| read_only).map(|d| d.label.clone()).unwrap_or_default().into();
    view.can_send = !read_only && engine.can_send(session);
    view.down = down.as_ref().map(panel).unwrap_or_default();
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
        .map(|view| HostDownRow {
            url: view.url.clone().into(),
            host: view.label.clone().into(),
            when: ago(view.last_ok_ms.unwrap_or(view.since_ms)).into(),
        })
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

/// "Diagnostics copied", for two seconds.
fn show_copied(window: &AppWindow) {
    window.global::<HostDownBridge>().set_copied(true);
    let weak = window.as_weak();
    slint::Timer::single_shot(Duration::from_secs(2), move || {
        if let Some(window) = weak.upgrade() {
            window.global::<HostDownBridge>().set_copied(false);
        }
    });
}

fn open_connection(app: &std::rc::Rc<App>, window: &AppWindow) {
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
    bridge.on_act(|url, action| {
        let (Some(app), Some(window)) = (crate::app(), crate::window()) else { return };
        match action.as_str() {
            // Pairing and the address are the connection page's.
            "pair" | "address" => {
                open_connection(&app, &window);
                window.invoke_open_host_action(url, action);
            }
            "tailscale" => crate::view::open_link(TAILSCALE_PAGE),
            other => eprintln!("clarp-slint: no Host-down action {other}"),
        }
    });
    bridge.on_switch_host(|url| {
        if let (Some(app), Some(window)) = (crate::app(), crate::window()) {
            open_connection(&app, &window);
            window.invoke_open_host_action(url, "switch".into());
        }
    });
    bridge.on_copy_diagnostics(|url| {
        let (Some(app), Some(window)) = (crate::app(), crate::window()) else { return };
        let text = app.engine.borrow().host_diagnostics(&url, platform(), env!("CARGO_PKG_VERSION"), option_env!("CLARP_BUILD").unwrap_or("dev"));
        match crate::platform::clipboard::copy(&text) {
            Ok(()) => show_copied(&window),
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
