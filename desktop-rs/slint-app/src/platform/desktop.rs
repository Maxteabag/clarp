//! What the window offers the desktop (the Qt app's `DesktopServices`): a
//! tray icon with Show, Mute voice replies and Quit; notifications (with
//! the agent's portrait, one per chat, and Open, Mark read and a
//! question's answers as buttons); and desktop presence (someone is at
//! this window, so the Host may pause phone alerts; `clarp_core::presence`).
//! The tray is a StatusNotifierItem; without a tray host there is no tray
//! and, as in the Qt and C++ clients, no notifications. macOS has no tray
//! (Show and Quit are the Dock's), notifies through Notification Center,
//! and takes presence from the window alone (there is no logind).
//!
//! For checks: `CLARP_TEST_NOTIFY_LOG` records notifications (and their
//! closing) in a file instead of sending them, `CLARP_TEST_FOREGROUND=1` treats the
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
            // i18n: tray icon menu: bring the Clarp window forward
            StandardItem { label: clarp_core::tr!("Show Clarp"), activate: Box::new(|tray: &mut Self| (tray.act)(Action::Show)), ..Default::default() }.into(),
            CheckmarkItem {
                // i18n: tray icon menu, check item: stop reading agents' replies aloud
                label: clarp_core::tr!("Mute voice replies"),
                checked: self.muted,
                activate: Box::new(|tray: &mut Self| {
                    tray.muted = !tray.muted;
                    (tray.act)(Action::Mute(tray.muted));
                }),
                ..Default::default()
            }
            .into(),
            MenuItem::Separator,
            // i18n: tray icon menu: quit Clarp (imperative verb)
            StandardItem { label: clarp_core::tr!("verb" => "Quit"), activate: Box::new(|tray: &mut Self| (tray.act)(Action::Quit)), ..Default::default() }.into(),
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
                // The dashcam keeps what this frame shows: Slint settles the
                // lists and their change handlers (as it would first thing
                // in the draw) and the draw shows exactly that.
                if let Some(window) = crate::window() {
                    let inner = i_slint_core::window::WindowInner::from_pub(window.window());
                    i_slint_core::properties::evaluate_no_tracking(|| inner.ensure_tree_instantiated());
                    crate::scroll_journal::frame_drawn();
                }
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
                    // A Host out of reach is asked again at once.
                    later(|| {
                        if let Some(app) = crate::app() {
                            crate::host_down_view::window_focused(&app);
                        }
                    });
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

/// A desktop popup: who, what, the chat it is about, and what its buttons
/// do. On Linux only with a tray, as in the Qt and C++ clients.
#[derive(Debug, Clone, Default)]
pub struct Note {
    pub title: String,
    /// The contact's name, for "Mute <name>".
    pub name: String,
    pub body: String,
    /// The chat: a newer popup of the same chat replaces the older one.
    pub session: String,
    /// The message a click opens the chat at.
    pub message_id: String,
    pub host: String,
    pub order: Option<clarp_core::notify_events::AlertOrder>,
    /// Ask the desktop for its message sound.
    pub sound: bool,
    /// The agent's cached portrait (`file://`).
    pub image: Option<String>,
    /// A question's id and kind, when its answers are buttons.
    pub decision_id: String,
    pub response_type: String,
    /// The buttons after Open and Mark read: (action key, label).
    pub choices: Vec<(String, String)>,
}

/// The settings row a popup's Settings opens at: the Host's first switch,
/// else this computer's.
const NOTIFICATION_SETTINGS: &[&str] = &["notify-reply", "desktop-notifications"];

/// The action key of an answer button (`choice:<option id>`).
pub const CHOICE: &str = "choice:";
/// The action key of a failed turn's button (`failure:<action id>`).
pub const FAILURE: &str = "failure:";

/// What a popup was for, kept to act on its buttons.
#[derive(Debug, Clone)]
#[cfg_attr(target_os = "macos", allow(dead_code))]
struct Shown {
    id: u32,
    action_generation: u64,
    session: String,
    message_id: String,
    host: String,
    order: Option<clarp_core::notify_events::AlertOrder>,
    decision_id: String,
    response_type: String,
}

thread_local! {
    /// The open popups, newest last (one per chat).
    static SHOWN: RefCell<Vec<Shown>> = const { RefCell::new(Vec::new()) };
}

/// The window has the keyboard: a popup about the chat on screen is not
/// needed (`CLARP_TEST_FOREGROUND` counts as focused).
pub fn window_focused() -> bool {
    std::env::var_os("CLARP_TEST_FOREGROUND").is_some() || SERVICES.with(|slot| slot.borrow().as_ref().is_some_and(|s| s.focused))
}

/// A check's own notification daemon on its private bus
/// (`CLARP_TEST_NOTIFY_DAEMON`): popups go over D-Bus as on a desktop, tray
/// or not, instead of into `CLARP_TEST_NOTIFY_LOG`.
pub fn test_daemon() -> bool {
    std::env::var_os("CLARP_TEST_NOTIFY_DAEMON").is_some()
}

/// Records what a check asked of the desktop instead of showing it.
fn test_log(line: serde_json::Value) -> bool {
    if test_daemon() {
        return false;
    }
    let Some(path) = std::env::var_os("CLARP_TEST_NOTIFY_LOG") else { return false };
    use std::io::Write;
    let written = std::fs::OpenOptions::new().create(true).append(true).open(&path).and_then(|mut f| writeln!(f, "{line}"));
    if let Err(error) = written {
        eprintln!("clarp-slint: could not record a notification: {error}");
    }
    true
}

/// Shows a popup. `sound` asks the desktop for its message sound.
pub fn notify(note: Note) {
    let logged = test_log(serde_json::json!({
        "title": note.title, "body": note.body, "sound": note.sound, "session": note.session,
        "message_id": note.message_id, "image": note.image, "category": "im.received",
        "actions": actions(&note).chunks(2).map(|pair| pair[0].clone()).collect::<Vec<_>>(),
        "replaces": SHOWN.with(|shown| shown.borrow().iter().any(|s| s.session == note.session)),
    }));
    if logged {
        // A check's popup is kept as shown, so the next of its chat replaces it.
        remember(shown_of(&note, 0));
        return;
    }
    #[cfg(target_os = "macos")]
    notify_macos(note.title, note.body, note.sound);
    #[cfg(not(target_os = "macos"))]
    notify_freedesktop(note);
}

/// The chat was opened or read on some device: its popup goes.
pub fn close_read(session: &str, host: &str, target: &clarp_core::notify_events::ReadTarget) {
    let logged = match target {
        clarp_core::notify_events::ReadTarget::Decision(id) => serde_json::json!({"closed_decision": id}),
        _ => serde_json::json!({"closed": session}),
    };
    close_where(|s| s.session == session && s.host == host && target.covers(s.order.as_ref(), &s.decision_id), logged);
}

/// Closes the popups `which` picks (`logged` is what a check records). A
/// shown popup's buttons stop at once (its entry goes); its id is closed
/// unless another popup's Notify is replacing it, which then owns the id. A
/// popup still on its way is closed as soon as the desktop names it, unless
/// a newer popup of the chat waits to replace it; a popup waiting that
/// `which` picks is not sent.
fn close_where(which: impl Fn(&Shown) -> bool, logged: serde_json::Value) {
    let gone: Vec<Shown> = SHOWN.with(|shown| {
        let mut shown = shown.borrow_mut();
        let (gone, kept) = std::mem::take(&mut *shown).into_iter().partition(|s| which(s));
        *shown = kept;
        gone
    });
    #[cfg(not(target_os = "macos"))]
    let in_flight = IN_FLIGHT.with(|flights| {
        let mut any = false;
        for flight in flights.borrow_mut().iter_mut() {
            if which(&flight.popup) {
                flight.close = true;
                any = true;
            }
            if flight.next.as_ref().is_some_and(|next| which(&shown_of(next, 0))) {
                flight.next = None;
            }
        }
        any
    });
    #[cfg(target_os = "macos")]
    let in_flight = false;
    if gone.is_empty() && !in_flight {
        return;
    }
    if test_log(logged) {
        return;
    }
    #[cfg(not(target_os = "macos"))]
    for popup in gone {
        if !defer_close_to_replacement(popup.id) {
            close_notification(popup.id);
        }
    }
}

/// Another popup's Notify is replacing `id` and keeps it (not one that is
/// itself closed and goes with it).
#[cfg(not(target_os = "macos"))]
fn defer_close_to_replacement(id: u32) -> bool {
    IN_FLIGHT.with(|flights| {
        let mut flights = flights.borrow_mut();
        let Some(flight) = flights.iter_mut().find(|flight| flight.replacing == Some(id)) else { return false };
        if flight.close && flight.next.is_none() {
            return false;
        }
        flight.replaces_closed = Some(id);
        true
    })
}

/// Closes the popup a button was pressed on, and only it.
fn close_id(id: u32) {
    let gone = SHOWN.with(|shown| {
        let mut shown = shown.borrow_mut();
        let before = shown.len();
        shown.retain(|s| s.id != id);
        before != shown.len()
    });
    #[cfg(not(target_os = "macos"))]
    if gone && !defer_close_to_replacement(id) {
        close_notification(id);
    }
    #[cfg(target_os = "macos")]
    let _ = gone;
}

/// What a popup is for, as kept once the desktop has named it `id`.
fn shown_of(note: &Note, id: u32) -> Shown {
    Shown {
        id,
        action_generation: 0,
        session: note.session.clone(),
        message_id: note.message_id.clone(),
        host: note.host.clone(),
        order: note.order.clone(),
        decision_id: note.decision_id.clone(),
        response_type: note.response_type.clone(),
    }
}

#[cfg(not(target_os = "macos"))]
fn close_notification(id: u32) {
    runtime::handle().spawn(async move {
        let closed = async {
            let connection = zbus::Connection::session().await?;
            connection.call_method(Some(NOTIFICATIONS), NOTIFICATIONS_PATH, Some(NOTIFICATIONS), "CloseNotification", &(id,)).await
        };
        if let Err(error) = closed.await {
            eprintln!("clarp-slint: could not close a notification: {error}");
        }
    });
}

/// The popup's buttons as the desktop takes them: key, label, key, label.
/// A click is Open; a question's answers come first, as a desktop shows
/// only its first few; then Mark read, Mute <name> and the settings.
fn actions(note: &Note) -> Vec<String> {
    use clarp_core::notify_events::{mute_label, text};
    let mut out = vec!["default".to_owned(), text("notify.action.open")];
    for (key, label) in &note.choices {
        out.extend([key.clone(), label.clone()]);
    }
    out.extend(["mark-read".to_owned(), text("notify.action.mark_read")]);
    if !note.name.is_empty() {
        out.extend(["mute".to_owned(), mute_label(&note.name)]);
    }
    out.extend(["settings".to_owned(), text("notify.action.settings")]);
    out
}

fn remember(popup: Shown) {
    SHOWN.with(|shown| {
        let mut shown = shown.borrow_mut();
        shown.retain(|s| s.session != popup.session && (popup.id == 0 || s.id != popup.id));
        shown.push(popup);
        // A desktop closes popups on its own; this keeps the newest few.
        let extra = shown.len().saturating_sub(32);
        shown.drain(..extra);
    });
}

/// A button of a popup was pressed (`default` is a click on it).
/// Ignore a click during replacement: the daemon may already show the new
/// popup, while it still uses the old popup's ID in ActionInvoked.
pub fn action_invoked(id: u32, key: &str) {
    #[cfg(not(target_os = "macos"))]
    if IN_FLIGHT.with(|flights| flights.borrow().iter().any(|flight| flight.replacing == Some(id))) {
        return;
    }
    let Some(popup) = SHOWN.with(|shown| shown.borrow().iter().find(|s| s.id == id).cloned()) else { return };
    let key = if key == "default" {
        key
    } else {
        let Some((generation, action)) = key.split_once(':') else { return };
        if generation.parse::<u64>().ok() != Some(popup.action_generation) {
            return;
        }
        action
    };
    if key != "mark-read" {
        close_id(id);
    }
    act(&popup.session, &popup.message_id, &popup.decision_id, &popup.response_type, key, Some(&popup));
}

/// What a popup's button does: open the chat at the message, mark it
/// read, mute the contact, open the notification settings, answer the
/// question, or send a failed turn's message again (opening the chat when
/// the answer or the retry cannot go, and for Switch model and Sign in).
pub fn act_on(session: &str, message_id: &str, decision_id: &str, response_type: &str, key: &str) {
    let shown = SHOWN.with(|shown| shown.borrow().iter().find(|s| s.session == session && s.message_id == message_id && s.decision_id == decision_id).cloned());
    if key != "mark-read" {
        close_where(|s| s.session == session && s.message_id == message_id && s.decision_id == decision_id, serde_json::json!({"closed": session}));
    }
    act(session, message_id, decision_id, response_type, key, shown.as_ref());
}

fn act(session: &str, message_id: &str, decision_id: &str, response_type: &str, key: &str, shown: Option<&Shown>) {
    let Some(app) = crate::app() else { return };
    match key {
        "mark-read" => {
            app.engine.borrow_mut().mark_popup_read(session, shown.map_or("", |s| s.host.as_str()), shown.and_then(|s| s.order.as_ref()));
            crate::pump_now(&app);
            return;
        }
        "mute" => {
            app.engine.borrow_mut().set_agent_push_muted(session, true);
            crate::pump_now(&app);
            return;
        }
        "settings" => {
            perform(Action::Show);
            if let Some(window) = crate::window() {
                crate::settings_view::open_searched(&app, &window, "notifications", NOTIFICATION_SETTINGS);
            }
            return;
        }
        _ => {}
    }
    if key.starts_with(FAILURE) {
        let retried = app.engine.borrow_mut().retry_from_notification(session, message_id);
        if retried {
            crate::pump_now(&app);
            return;
        }
    }
    if let Some(option) = key.strip_prefix(CHOICE) {
        let answered = app.engine.borrow_mut().answer_from_notification(decision_id, response_type, option);
        if answered {
            crate::pump_now(&app);
            return;
        }
    }
    perform(Action::Show);
    crate::search_view::open_message(&app, session, message_id);
}

#[cfg(not(target_os = "macos"))]
const NOTIFICATIONS: &str = "org.freedesktop.Notifications";
#[cfg(not(target_os = "macos"))]
const NOTIFICATIONS_PATH: &str = "/org/freedesktop/Notifications";

#[cfg(not(target_os = "macos"))]
thread_local! {
    /// The buttons are listened to once the first popup is shown.
    static LISTENING: std::cell::Cell<bool> = const { std::cell::Cell::new(false) };
    /// The chats whose Notify is on its way: one at a time per chat, as only
    /// its answer names the popup a newer one replaces or a close takes down.
    static IN_FLIGHT: RefCell<Vec<InFlight>> = const { RefCell::new(Vec::new()) };
}

/// A chat's Notify on its way, and what came for the chat meanwhile: the
/// newest popup, sent once the id is known (replacing it), and whether the
/// popup on its way was closed meanwhile.
#[cfg(not(target_os = "macos"))]
struct InFlight {
    popup: Shown,
    next: Option<Note>,
    close: bool,
    /// The id this Notify replaces: it becomes this popup's (and its
    /// buttons this popup's) only once the Notify returns.
    replacing: Option<u32>,
    /// The id of a closed popup this one replaces: closed if this Notify
    /// fails, so the closed popup does not stay.
    replaces_closed: Option<u32>,
}

/// Through Notification Center (as Script Editor, the sender `osascript`
/// is): the texts go as arguments, never into the script. No buttons.
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

/// A freedesktop notification: the agent's portrait, the `im.received`
/// category, the chat's earlier popup replaced, and buttons.
#[cfg(not(target_os = "macos"))]
fn notify_freedesktop(note: Note) {
    send(note, None);
}

/// Sends a popup, or queues it behind the chat's Notify on its way.
/// `closed`: the id of a closed popup of the chat it replaces.
#[cfg(not(target_os = "macos"))]
fn send(note: Note, closed: Option<u32>) {
    let has_tray = SERVICES.with(|slot| slot.borrow().as_ref().is_some_and(|s| s.tray.is_some()));
    if !has_tray && !test_daemon() {
        return;
    }
    if !LISTENING.with(|l| l.replace(true)) {
        runtime::handle().spawn(listen_to_popups());
    }
    // The chat's last Notify has not answered: this one goes once it has,
    // replacing the popup it names.
    let note = IN_FLIGHT.with(|flights| {
        let mut flights = flights.borrow_mut();
        match flights.iter_mut().find(|f| f.popup.session == note.session) {
            Some(flight) => {
                flight.next = Some(note);
                None
            }
            None => {
                let replaces = closed.or_else(|| SHOWN.with(|shown| shown.borrow().iter().find(|s| s.session == note.session).map(|s| s.id)));
                flights.push(InFlight { popup: shown_of(&note, 0), next: None, close: false, replacing: replaces, replaces_closed: closed });
                Some(note)
            }
        }
    });
    let Some(note) = note else { return };
    // The shown popup's buttons stay its own until this Notify returns.
    let replaces = IN_FLIGHT.with(|flights| flights.borrow().iter().find(|f| f.popup.session == note.session).and_then(|f| f.replacing)).unwrap_or(0);
    // The daemon may reuse a popup ID. Give each explicit button an identity
    // that stays with the popup that supplied it, even if its signal arrives late.
    thread_local! {
        static ACTION_GENERATION: std::cell::Cell<u64> = const { std::cell::Cell::new(0) };
    }
    let action_generation = ACTION_GENERATION.with(|counter| {
        let next = counter.get().wrapping_add(1);
        counter.set(next);
        next
    });
    let mut actions = actions(&note);
    for pair in actions.chunks_exact_mut(2).skip(1) {
        pair[0] = format!("{action_generation}:{}", pair[0]);
    }
    runtime::handle().spawn(async move {
        let sent = async {
            let connection = zbus::Connection::session().await?;
            let mut hints: std::collections::HashMap<&str, zbus::zvariant::Value> = std::collections::HashMap::new();
            hints.insert("category", zbus::zvariant::Value::from("im.received"));
            hints.insert("desktop-entry", zbus::zvariant::Value::from("clarp"));
            if let Some(image) = note.image.as_deref() {
                hints.insert("image-path", zbus::zvariant::Value::from(image));
            }
            if note.sound {
                hints.insert("sound-name", zbus::zvariant::Value::from("message-new-instant"));
            }
            let actions: Vec<&str> = actions.iter().map(String::as_str).collect();
            let reply = connection
                .call_method(
                    Some(NOTIFICATIONS),
                    NOTIFICATIONS_PATH,
                    Some(NOTIFICATIONS),
                    "Notify",
                    &("Clarp", replaces, "clarp", note.title.as_str(), note.body.as_str(), actions, hints, 8_000i32),
                )
                .await?;
            reply.body().deserialize::<u32>()
        };
        let answered = sent.await;
        if let Err(error) = &answered {
            eprintln!("clarp-slint: notification failed: {error}");
        }
        let id = answered.ok();
        let mut popup = shown_of(&note, id.unwrap_or(0));
        popup.action_generation = action_generation;
        later(move || delivered(popup, id));
    });
}

/// The desktop answered a chat's Notify (`id`, or None when it failed):
/// the popup is kept, or closed when a close came meanwhile, and the
/// chat's newer popup goes next, replacing it.
#[cfg(not(target_os = "macos"))]
fn delivered(popup: Shown, id: Option<u32>) {
    let session = popup.session.clone();
    let flight = IN_FLIGHT.with(|flights| {
        let mut flights = flights.borrow_mut();
        let at = flights.iter().position(|f| f.popup.session == session)?;
        Some(flights.remove(at))
    });
    let (close, next, replaces_closed) = flight.map_or((false, None, None), |f| (f.close, f.next, f.replaces_closed));
    match (id, close, next) {
        // Shown; a newer popup of the chat replaces it next.
        (Some(_), false, next) => {
            remember(popup);
            if let Some(next) = next {
                notify_freedesktop(next);
            }
        }
        // Closed meanwhile, and a newer popup waits: it takes the id, so the
        // closed one goes and the newer stays. The closed one's buttons are
        // never committed.
        (Some(id), true, Some(next)) => {
            SHOWN.with(|shown| shown.borrow_mut().retain(|s| s.id != id));
            send(next, Some(id));
        }
        (Some(id), true, None) => {
            SHOWN.with(|shown| shown.borrow_mut().retain(|s| s.id != id));
            close_notification(id);
        }
        // Not shown: a closed popup it was to replace still goes.
        (None, _, next) => {
            if let Some(closed) = replaces_closed {
                SHOWN.with(|shown| shown.borrow_mut().retain(|s| s.id != closed));
                close_notification(closed);
            }
            if let Some(next) = next {
                notify_freedesktop(next);
            }
        }
    }
}

/// Follows the popups' buttons and their closing on the session bus.
#[cfg(not(target_os = "macos"))]
async fn listen_to_popups() {
    use futures_util::StreamExt;
    let connection = match zbus::Connection::session().await {
        Ok(connection) => connection,
        Err(error) => {
            eprintln!("clarp-slint: notification buttons are not followed: {error}");
            return;
        }
    };
    let rule = zbus::MatchRule::builder()
        .msg_type(zbus::message::Type::Signal)
        .interface(NOTIFICATIONS)
        .and_then(|rule| rule.path(NOTIFICATIONS_PATH))
        .map(|rule| rule.build());
    let mut stream = match rule {
        Ok(rule) => match zbus::MessageStream::for_match_rule(rule, &connection, None).await {
            Ok(stream) => stream,
            Err(error) => {
                eprintln!("clarp-slint: notification buttons are not followed: {error}");
                return;
            }
        },
        Err(error) => {
            eprintln!("clarp-slint: notification buttons are not followed: {error}");
            return;
        }
    };
    while let Some(Ok(message)) = stream.next().await {
        let header = message.header();
        match header.member().map(|m| m.as_str()) {
            Some("ActionInvoked") => {
                let Ok((id, key)) = message.body().deserialize::<(u32, String)>() else { continue };
                later(move || action_invoked(id, &key));
            }
            Some("NotificationClosed") => {
                let Ok((id, _reason)) = message.body().deserialize::<(u32, u32)>() else { continue };
                later(move || SHOWN.with(|shown| shown.borrow_mut().retain(|s| s.id != id)));
            }
            _ => {}
        }
    }
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
