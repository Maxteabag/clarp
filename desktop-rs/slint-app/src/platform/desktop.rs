//! What the window offers the desktop (the Qt app's `DesktopServices`): a
//! tray icon with Show, Mute voice replies and Quit; notifications for
//! replies in chats that are not open; and desktop presence (someone is at
//! this window, so the Host may pause phone alerts; `clarp_core::presence`).
//! The tray is a StatusNotifierItem; without a tray host there is no tray
//! and, as in the Qt and C++ clients, no notifications. macOS has no tray
//! (Show and Quit are the Dock's), notifies through Notification Center,
//! and takes presence from the window alone (there is no logind).
//!
//! For checks: `CLARP_TEST_NOTIFY_LOG` records notifications in a file
//! instead of sending them, `CLARP_TEST_FOREGROUND=1` treats the
//! headless window as the one in front, and `CLARP_TEST_LOGIN=unlocked`
//! (or `locked`) stands in for the login session logind would report.

use std::cell::RefCell;
#[cfg(target_os = "linux")]
use std::sync::Arc;
use std::time::{Duration, Instant};

use super::runtime;

/// What the tray asks the window to do.
#[derive(Debug, Clone, Copy)]
#[cfg_attr(not(target_os = "linux"), allow(dead_code))]
enum Action {
    Show,
    Mute(bool),
    Quit,
}

#[cfg(target_os = "linux")]
struct ClarpTray {
    muted: bool,
    icons: Vec<ksni::Icon>,
    act: Arc<dyn Fn(Action) + Send + Sync>,
}

#[cfg(target_os = "linux")]
impl ksni::Tray for ClarpTray {
    fn id(&self) -> String {
        "clarp-desktop".into()
    }
    fn title(&self) -> String {
        "Clarp".into()
    }
    fn icon_pixmap(&self) -> Vec<ksni::Icon> {
        self.icons.clone()
    }
    fn tool_tip(&self) -> ksni::ToolTip {
        ksni::ToolTip { title: "Clarp".into(), ..Default::default() }
    }
    fn activate(&mut self, _x: i32, _y: i32) {
        (self.act)(Action::Show);
    }
    fn menu(&self) -> Vec<ksni::MenuItem<Self>> {
        use ksni::menu::{CheckmarkItem, MenuItem, StandardItem};
        vec![
            StandardItem { label: "Show Clarp".into(), activate: Box::new(|tray: &mut Self| (tray.act)(Action::Show)), ..Default::default() }.into(),
            CheckmarkItem {
                label: "Mute voice replies".into(),
                checked: self.muted,
                activate: Box::new(|tray: &mut Self| {
                    tray.muted = !tray.muted;
                    (tray.act)(Action::Mute(tray.muted));
                }),
                ..Default::default()
            }
            .into(),
            MenuItem::Separator,
            StandardItem { label: "Quit".into(), activate: Box::new(|tray: &mut Self| (tray.act)(Action::Quit)), ..Default::default() }.into(),
        ]
    }
}

#[cfg(target_os = "linux")]
type TrayHandle = ksni::Handle<ClarpTray>;
/// No tray outside Linux.
#[cfg(not(target_os = "linux"))]
type TrayHandle = std::convert::Infallible;

struct Services {
    #[cfg_attr(not(target_os = "linux"), allow(dead_code))]
    tray: Option<TrayHandle>,
    presence: clarp_core::presence::Presence,
    instance: String,
    clock: Instant,
    inputs: (bool, bool),
    foreground: bool,
    /// The winit window has the keyboard (always false headless).
    focused: bool,
}

thread_local! {
    static SERVICES: RefCell<Option<Services>> = const { RefCell::new(None) };
}

#[cfg(target_os = "linux")]
const ICON: &[u8] = include_bytes!("../../../../static/icon.png");

fn later(act: impl FnOnce() + Send + 'static) {
    if let Err(error) = slint::invoke_from_event_loop(act) {
        eprintln!("clarp-slint desktop: dropped an event: {error}");
    }
}

/// Starts the tray and presence a second after launch (not needed for the
/// first frame); the window's events are watched at once (scale).
pub fn start() {
    watch_window();
    runtime::after(Duration::from_secs(1), || later(start_now));
}

thread_local! {
    /// The interface scale the reader chose (Ctrl+= / Ctrl+- / Ctrl+0).
    static UI_SCALE: std::cell::Cell<f32> = const { std::cell::Cell::new(1.0) };
    /// The monitor's own scale, as the window system last said.
    static MONITOR_SCALE: std::cell::Cell<f32> = const { std::cell::Cell::new(1.0) };
}

/// Scales the whole interface by `scale` on top of the monitor's factor.
pub fn set_ui_scale(scale: f32) {
    UI_SCALE.with(|s| s.set(scale));
    apply_scale();
}

fn apply_scale() {
    use slint::ComponentHandle;
    use slint::winit_030::WinitWindowAccessor;
    let Some(window) = crate::window() else { return };
    if let Some(monitor) = window.window().with_winit_window(|w| w.scale_factor() as f32) {
        MONITOR_SCALE.with(|s| s.set(monitor));
    }
    let scale = MONITOR_SCALE.with(|s| s.get()) * UI_SCALE.with(|s| s.get());
    rescale(window.window(), scale);
    crate::avatar_view::remake();
}

/// The window moved to a monitor of another scale (an external display
/// plugged in or out): the reader's scale stays on top of the new one.
pub(crate) fn monitor_scale_changed(monitor: f32) {
    use slint::ComponentHandle;
    MONITOR_SCALE.with(|s| s.set(monitor));
    if let Some(window) = crate::window() {
        rescale(window.window(), monitor * UI_SCALE.with(|s| s.get()));
        crate::avatar_view::remake();
    }
}

/// Draws the window at `scale` in the pixels it has. Slint's
/// ScaleFactorChanged alone keeps the logical size, so the content would
/// outgrow the software renderer's buffer, which is sized from the pixels,
/// and the next frame panics ("buffer ... is too small to handle a window
/// of size ..."): a 1100x700 window on a 2x display at the default 1.15
/// asks for 2530x1610 in 2200x1400. The logical size follows the pixels.
pub(crate) fn rescale(window: &slint::Window, scale: f32) {
    window.dispatch_event(slint::platform::WindowEvent::ScaleFactorChanged { scale_factor: scale });
    // Before the window system's window exists there are no pixels yet:
    // its first resize sets the size then.
    let pixels = window.size();
    if pixels.width > 0 && pixels.height > 0 {
        window.dispatch_event(slint::platform::WindowEvent::Resized { size: pixels.to_logical(scale) });
    }
}

fn start_now() {
    let muted = crate::app().is_some_and(|app| app.engine.borrow().muted());
    SERVICES.with(|slot| {
        *slot.borrow_mut() = Some(Services {
            tray: None,
            presence: clarp_core::presence::Presence::default(),
            instance: uuid::Uuid::new_v4().to_string(),
            clock: Instant::now(),
            inputs: (false, false),
            foreground: false,
            focused: false,
        })
    });
    presence_tick();
    runtime::handle().spawn(watch_login_session());
    start_tray(muted);
}

#[cfg(not(target_os = "linux"))]
fn start_tray(_muted: bool) {
    eprintln!("clarp-slint: no system tray on this platform; the Dock shows and quits Clarp");
}

#[cfg(target_os = "linux")]
fn start_tray(muted: bool) {
    let act: Arc<dyn Fn(Action) + Send + Sync> = Arc::new(|action| later(move || perform(action)));
    let icons = [22, 48].iter().filter_map(|&side| clarp_core::media::argb_icon(ICON, side)).map(|(width, height, data)| ksni::Icon { width, height, data }).collect();
    let tray = ClarpTray { muted, icons, act };
    runtime::handle().spawn(async move {
        use ksni::TrayMethods;
        match tray.spawn().await {
            Ok(handle) => later(move || SERVICES.with(|slot| {
                if let Some(services) = slot.borrow_mut().as_mut() {
                    services.tray = Some(handle);
                }
            })),
            // No tray host (or no session bus): no tray, as in the C++ client.
            Err(error) => eprintln!("clarp-slint: no system tray: {error}"),
        }
    });
}

/// Input and focus from the window system (winit), for presence.
fn watch_window() {
    use slint::winit_030::{WinitWindowAccessor, winit::event::WindowEvent, EventResult};
    let Some(window) = crate::window() else { return };
    use slint::ComponentHandle;
    window.window().on_winit_window_event(|_, event| {
        match event {
            // The monitor changed: keep the reader's scale on top of it.
            WindowEvent::ScaleFactorChanged { scale_factor, .. } => {
                let monitor = *scale_factor as f32;
                if let Err(error) = slint::invoke_from_event_loop(move || monitor_scale_changed(monitor)) {
                    eprintln!("clarp-slint: dropped a scale change: {error}");
                }
            }
            // A frame: Slint draws it as soon as this returns; the first
            // thing the loop runs after it ends the frame's clock.
            WindowEvent::RedrawRequested => {
                crate::perf::redraw_started();
                if let Err(error) = slint::invoke_from_event_loop(crate::perf::redraw_finished) {
                    eprintln!("clarp-slint: dropped a frame's clock: {error}");
                }
            }
            WindowEvent::Focused(focused) => {
                let focused = *focused;
                SERVICES.with(|slot| {
                    if let Some(services) = slot.borrow_mut().as_mut() {
                        services.focused = focused;
                    }
                });
                // After Slint has handed the focus back to what had it.
                if focused {
                    later(regain_keyboard);
                }
            }
            // Covered or minimised (X11; on Wayland the compositor stops
            // asking for frames instead): the chats stop animating.
            WindowEvent::Occluded(occluded) => {
                let shown = !*occluded;
                later(move || {
                    if let Some(window) = crate::window() {
                        window.global::<crate::ChatLook>().set_window_shown(shown);
                    }
                });
            }
            WindowEvent::ModifiersChanged(modifiers) => {
                let state = modifiers.state();
                super::keyboard::reported(super::keyboard::Modifiers::from_window_system(state.control_key(), state.alt_key(), state.shift_key(), state.super_key()));
            }
            WindowEvent::KeyboardInput { event, .. } => {
                use slint::winit_030::winit::keyboard::{Key, NamedKey};
                if matches!(event.logical_key, Key::Named(NamedKey::Control | NamedKey::Shift | NamedKey::Alt | NamedKey::AltGraph | NamedKey::Super)) {
                    super::keyboard::modifier_key_seen();
                } else if event.state.is_pressed() {
                    // Before Slint delivers it: a key to nobody runs nothing.
                    regain_keyboard();
                }
                note_input();
            }
            WindowEvent::MouseInput { .. } | WindowEvent::MouseWheel { .. } | WindowEvent::Touch(_) => {
                note_input();
            }
            _ => {}
        }
        EventResult::Propagate
    });
}

fn regain_keyboard() {
    if let (Some(app), Some(window)) = (crate::app(), crate::window()) {
        crate::commands::regain_keyboard(&app, &window);
    }
}

/// Someone used the window (a key, a click): they are here.
pub fn note_input() {
    let reports = SERVICES.with(|slot| {
        let mut slot = slot.borrow_mut();
        let services = slot.as_mut()?;
        if !services.foreground {
            return None;
        }
        let now = services.clock.elapsed().as_millis() as i64;
        Some(services.presence.note_interaction(now))
    });
    if let Some(reports) = reports.filter(|r| !r.is_empty()) {
        apply(reports);
    }
}

fn apply(reports: Vec<clarp_core::presence::Report>) {
    use clarp_core::presence::Report;
    let Some(app) = crate::app() else { return };
    let instance = SERVICES.with(|slot| slot.borrow().as_ref().map(|s| s.instance.clone())).unwrap_or_default();
    let engine = app.engine.borrow();
    for report in reports {
        match report {
            Report::Presence { sequence, active } => engine.report_desktop_presence(&instance, sequence, active),
            Report::Activity { sequence, foreground, input_age_ms } => engine.report_application_activity(&instance, sequence, foreground, input_age_ms),
        }
    }
}

/// Once a second: is the window in front, and what the engine says.
fn presence_tick() {
    let inputs = crate::app().map(|app| app.engine.borrow().presence_inputs()).unwrap_or((false, false));
    let test_foreground = std::env::var_os("CLARP_TEST_FOREGROUND").is_some();
    let reports = SERVICES.with(|slot| {
        let mut slot = slot.borrow_mut();
        let services = slot.as_mut()?;
        let now = services.clock.elapsed().as_millis() as i64;
        let foreground = services.focused || test_foreground;
        let mut reports = Vec::new();
        if inputs.0 != services.inputs.0 {
            reports.extend(services.presence.set_enabled(inputs.0, now));
        }
        if inputs.1 != services.inputs.1 {
            reports.extend(services.presence.set_connected(inputs.1, now));
        }
        services.inputs = inputs;
        if foreground != services.foreground {
            services.foreground = foreground;
            reports.extend(services.presence.set_foreground(foreground, now));
        }
        reports.extend(services.presence.refresh(now));
        Some(reports)
    });
    let Some(reports) = reports else { return };
    apply(reports);
    runtime::after(Duration::from_secs(1), || later(presence_tick));
}

fn login_state(state: LoginState) {
    let reports = SERVICES.with(|slot| {
        let mut slot = slot.borrow_mut();
        let services = slot.as_mut()?;
        let now = services.clock.elapsed().as_millis() as i64;
        Some(match state {
            LoginState::Session { available, unlocked } => services.presence.set_session_state(available, unlocked, now),
            LoginState::Sleeping(sleeping) => services.presence.prepare_for_sleep(sleeping, now),
        })
    });
    if let Some(reports) = reports {
        apply(reports);
    }
}

#[cfg_attr(not(target_os = "linux"), allow(dead_code))]
fn perform(action: Action) {
    use slint::ComponentHandle;
    match action {
        Action::Show => {
            if let Some(window) = crate::window()
                && let Err(error) = window.show()
            {
                eprintln!("clarp-slint: cannot show the window: {error}");
            }
        }
        Action::Mute(muted) => {
            if let Some(app) = crate::app() {
                app.engine.borrow_mut().set_muted(muted);
                crate::pump_now(&app);
            }
        }
        Action::Quit => {
            if let Err(error) = slint::quit_event_loop() {
                eprintln!("clarp-slint: {error}");
            }
        }
    }
}

/// The tray's Mute item follows mute set anywhere else.
#[cfg_attr(not(target_os = "linux"), allow(unused_variables))]
pub fn muted_changed(muted: bool) {
    #[cfg(target_os = "linux")]
    SERVICES.with(|slot| {
        if let Some(tray) = slot.borrow().as_ref().and_then(|s| s.tray.clone()) {
            runtime::handle().spawn(async move {
                tray.update(|tray| tray.muted = muted).await;
            });
        }
    });
}

/// A reply in a chat that is not open. On Linux only with a tray, as in
/// the Qt and C++ clients. `sound` asks the desktop for its message sound (Settings →
/// Notification sound).
pub fn notify(title: String, body: String, sound: bool) {
    if let Some(path) = std::env::var_os("CLARP_TEST_NOTIFY_LOG") {
        use std::io::Write;
        let line = serde_json::json!({"title": title, "body": body, "sound": sound}).to_string();
        let written = std::fs::OpenOptions::new().create(true).append(true).open(&path).and_then(|mut f| writeln!(f, "{line}"));
        if let Err(error) = written {
            eprintln!("clarp-slint: could not record a notification: {error}");
        }
        return;
    }
    #[cfg(target_os = "macos")]
    notify_macos(title, body, sound);
    #[cfg(not(target_os = "macos"))]
    notify_freedesktop(title, body, sound);
}

/// Through Notification Center (as Script Editor, the sender `osascript`
/// is): the texts go as arguments, never into the script.
#[cfg(target_os = "macos")]
fn notify_macos(title: String, body: String, sound: bool) {
    let script = if sound {
        "on run argv\ndisplay notification (item 2 of argv) with title (item 1 of argv) sound name \"Glass\"\nend run"
    } else {
        "on run argv\ndisplay notification (item 2 of argv) with title (item 1 of argv)\nend run"
    };
    let spawned = std::thread::Builder::new().name("notify".into()).spawn(move || {
        match std::process::Command::new("/usr/bin/osascript").args(["-e", script, &title, &body]).stdout(std::process::Stdio::null()).output() {
            Ok(output) if output.status.success() => {}
            Ok(output) => eprintln!("clarp-slint: notification failed: {}", String::from_utf8_lossy(&output.stderr).trim()),
            Err(error) => eprintln!("clarp-slint: notification failed: {error}"),
        }
    });
    if let Err(error) = spawned {
        eprintln!("clarp-slint: notification failed: {error}");
    }
}

#[cfg(not(target_os = "macos"))]
fn notify_freedesktop(title: String, body: String, sound: bool) {
    let has_tray = SERVICES.with(|slot| slot.borrow().as_ref().is_some_and(|s| s.tray.is_some()));
    if !has_tray {
        return;
    }
    runtime::handle().spawn(async move {
        let sent = async {
            let connection = zbus::Connection::session().await?;
            let mut hints: std::collections::HashMap<&str, zbus::zvariant::Value> = std::collections::HashMap::new();
            if sound {
                hints.insert("sound-name", zbus::zvariant::Value::from("message-new-instant"));
            }
            connection
                .call_method(
                    Some("org.freedesktop.Notifications"),
                    "/org/freedesktop/Notifications",
                    Some("org.freedesktop.Notifications"),
                    "Notify",
                    &("Clarp", 0u32, "clarp", title.as_str(), body.as_str(), Vec::<&str>::new(), hints, 8_000i32),
                )
                .await
        };
        if let Err(error) = sent.await {
            eprintln!("clarp-slint: notification failed: {error}");
        }
    });
}

#[derive(Debug, Clone, Copy)]
enum LoginState {
    Session { available: bool, unlocked: bool },
    Sleeping(bool),
}

/// This user's graphical login session: `XDG_SESSION_ID`, else the user's
/// display session.
async fn login_session(bus: &zbus::Connection) -> zbus::Result<zbus::zvariant::OwnedObjectPath> {
    const LOGIN: &str = "org.freedesktop.login1";
    if let Ok(id) = std::env::var("XDG_SESSION_ID").map(|id| id.trim().to_owned()).as_ref().map(String::as_str)
        && !id.is_empty()
    {
        let reply = bus.call_method(Some(LOGIN), "/org/freedesktop/login1", Some("org.freedesktop.login1.Manager"), "GetSession", &(id,)).await?;
        return reply.body().deserialize();
    }
    // SAFETY: getuid cannot fail.
    let uid = unsafe { libc_getuid() };
    let reply = bus.call_method(Some(LOGIN), "/org/freedesktop/login1", Some("org.freedesktop.login1.Manager"), "GetUser", &(uid,)).await?;
    let user: zbus::zvariant::OwnedObjectPath = reply.body().deserialize()?;
    let reply = bus
        .call_method(Some(LOGIN), user.as_str(), Some("org.freedesktop.DBus.Properties"), "Get", &("org.freedesktop.login1.User", "Display"))
        .await?;
    let display: zbus::zvariant::OwnedValue = reply.body().deserialize()?;
    let (_, session): (String, zbus::zvariant::OwnedObjectPath) = display.try_into()?;
    Ok(session)
}

unsafe extern "C" {
    #[link_name = "getuid"]
    fn libc_getuid() -> u32;
}

async fn session_state(bus: &zbus::Connection, session: &str) -> zbus::Result<LoginState> {
    let reply = bus
        .call_method(Some("org.freedesktop.login1"), session, Some("org.freedesktop.DBus.Properties"), "GetAll", &("org.freedesktop.login1.Session",))
        .await?;
    let values: std::collections::HashMap<String, zbus::zvariant::OwnedValue> = reply.body().deserialize()?;
    let flag = |key: &str| values.get(key).and_then(|v| bool::try_from(v).ok());
    let (active, locked) = (flag("Active"), flag("LockedHint"));
    Ok(LoginState::Session { available: active.is_some() && locked.is_some(), unlocked: active == Some(true) && locked == Some(false) })
}

/// Polls the login session (read only) every 10 s and follows suspend; an
/// unknown state never suppresses phone alerts.
async fn watch_login_session() {
    use futures_util::StreamExt;
    let send = |state: LoginState| later(move || login_state(state));
    // A check's login session ("unlocked" or "locked"), never the machine's.
    if let Ok(test) = std::env::var("CLARP_TEST_LOGIN") {
        send(LoginState::Session { available: true, unlocked: test == "unlocked" });
        return;
    }
    // No logind: presence follows the window (focus and input) alone.
    if cfg!(target_os = "macos") {
        eprintln!("clarp-slint: presence follows the window; macOS has no login session to read");
        send(LoginState::Session { available: true, unlocked: true });
        return;
    }
    let bus = match zbus::Connection::system().await {
        Ok(bus) => bus,
        Err(error) => {
            eprintln!("clarp-slint: no system bus, presence stays off: {error}");
            send(LoginState::Session { available: false, unlocked: false });
            return;
        }
    };
    let sleeps = zbus::MatchRule::builder()
        .msg_type(zbus::message::Type::Signal)
        .interface("org.freedesktop.login1.Manager")
        .and_then(|rule| rule.member("PrepareForSleep"))
        .map(|rule| rule.build());
    if let Ok(rule) = sleeps {
        match zbus::MessageStream::for_match_rule(rule, &bus, None).await {
            Ok(mut stream) => {
                runtime::handle().spawn(async move {
                    while let Some(Ok(message)) = stream.next().await {
                        let Ok(sleeping) = message.body().deserialize::<bool>() else { continue };
                        send(LoginState::Sleeping(sleeping));
                    }
                });
            }
            Err(error) => eprintln!("clarp-slint: cannot follow suspend: {error}"),
        }
    }
    let mut session: Option<zbus::zvariant::OwnedObjectPath> = None;
    loop {
        if session.is_none() {
            match login_session(&bus).await {
                Ok(path) if path.as_str() != "/" => session = Some(path),
                Ok(_) => {}
                Err(error) => eprintln!("clarp-slint: no login session: {error}"),
            }
        }
        let state = match &session {
            Some(path) => match session_state(&bus, path.as_str()).await {
                Ok(state) => state,
                Err(error) => {
                    eprintln!("clarp-slint: login session state failed: {error}");
                    session = None;
                    LoginState::Session { available: false, unlocked: false }
                }
            },
            None => LoginState::Session { available: false, unlocked: false },
        };
        send(state);
        tokio::time::sleep(Duration::from_secs(10)).await;
    }
}
