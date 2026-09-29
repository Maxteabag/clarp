//! DesktopServices: what the window offers the desktop (C++
//! `DesktopIntegration`, `DesktopPresence`): a tray icon with Show, Mute voice
//! replies and Quit, notifications for replies in chats that are not open,
//! and desktop presence (someone is at this window, so the Host may pause
//! phone alerts; see `clarp_core::presence`). The root
//! document creates it next to Main and hands it the window; it starts a
//! second after launch, like the C++ deferred services. The tray is a
//! StatusNotifierItem (what Qt's tray is on Linux); without a tray host
//! there is no tray and, as in the C++ client, no notifications.

use std::pin::Pin;
use std::sync::Arc;
use std::time::Duration;

use cxx_qt::{CxxQtType, QMetaObjectConnectionGuard, Threading};

#[cxx_qt::bridge]
pub mod qobject {
    unsafe extern "C++" {
        include!(<QtQuick/QQuickWindow>);
        type QQuickWindow;
        fn show(self: Pin<&mut QQuickWindow>);
        fn raise(self: Pin<&mut QQuickWindow>);
        #[cxx_name = "requestActivate"]
        fn request_activate(self: Pin<&mut QQuickWindow>);
        #[cxx_name = "isActive"]
        fn is_active(self: &QQuickWindow) -> bool;
        #[cxx_name = "isVisible"]
        fn is_visible(self: &QQuickWindow) -> bool;
        #[cxx_name = "isExposed"]
        fn is_exposed(self: &QQuickWindow) -> bool;
        include!(<QtCore/QEvent>);
        type QEvent;
        /// Key, mouse, wheel and touch events (Qt 6); cxx cannot name the
        /// nested `QEvent::Type` enum.
        #[cxx_name = "isInputEvent"]
        fn is_input_event(self: &QEvent) -> bool;
        include!(<QtCore/QCoreApplication>);
        type QCoreApplication;
        #[Self = "QCoreApplication"]
        #[cxx_name = "quit"]
        fn quit_application();
        #[Self = "QCoreApplication"]
        #[cxx_name = "instance"]
        fn running_application() -> *mut QCoreApplication;
        #[cxx_name = "installEventFilter"]
        unsafe fn install_event_filter(self: Pin<&mut QCoreApplication>, filter: *mut QObject);
    }

    extern "RustQt" {
        #[qobject]
        #[qml_element]
        #[qproperty(*mut QQuickWindow, window, READ = window_value, WRITE = set_window, NOTIFY = window_changed)]
        type DesktopServices = super::DesktopServicesRust;
    }

    unsafe extern "RustQt" {
        fn window_value(self: &DesktopServices) -> *mut QQuickWindow;
        #[cxx_name = "setWindow"]
        unsafe fn set_window(self: Pin<&mut DesktopServices>, window: *mut QQuickWindow);
        #[qsignal]
        #[cxx_name = "windowChanged"]
        fn window_changed(self: Pin<&mut DesktopServices>);

        /// Input anywhere in the application counts as someone being there.
        #[cxx_override]
        #[cxx_name = "eventFilter"]
        unsafe fn event_filter(self: Pin<&mut DesktopServices>, watched: *mut QObject, event: *mut QEvent) -> bool;
    }

    impl cxx_qt::Threading for DesktopServices {}
    impl cxx_qt::Initialize for DesktopServices {}
}

/// What the tray asks the window to do.
#[derive(Debug, Clone, Copy)]
enum Action {
    Show,
    Mute(bool),
    Quit,
}

struct ClarpTray {
    muted: bool,
    icons: Vec<ksni::Icon>,
    act: Arc<dyn Fn(Action) + Send + Sync>,
}

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

pub struct DesktopServicesRust {
    window: usize,
    tray: Option<ksni::Handle<ClarpTray>>,
    guards: Vec<QMetaObjectConnectionGuard>,
    presence: clarp_core::presence::Presence,
    presence_on: bool,
    instance: String,
    clock: std::time::Instant,
    inputs: (bool, bool),
    foreground: bool,
}

impl Default for DesktopServicesRust {
    fn default() -> Self {
        Self {
            window: 0,
            tray: None,
            guards: Vec::new(),
            presence: clarp_core::presence::Presence::default(),
            presence_on: false,
            instance: uuid::Uuid::new_v4().to_string(),
            clock: std::time::Instant::now(),
            inputs: (false, false),
            foreground: false,
        }
    }
}

impl Drop for DesktopServicesRust {
    fn drop(&mut self) {
        self.guards.clear();
        if let Some(tray) = self.tray.take() {
            // Dropping the awaiter would not wait; the runtime finishes it.
            crate::runtime::handle().spawn(async move { tray.shutdown().await });
        }
    }
}

const ICON: &[u8] = include_bytes!("../../../../static/icon.png");

impl cxx_qt::Initialize for qobject::DesktopServices {
    fn initialize(self: Pin<&mut Self>) {
        // Not needed for the first frame: start a second after launch.
        let qt = self.qt_thread();
        crate::runtime::after(Duration::from_secs(1), move || {
            if qt.queue(|services| services.start()).is_err() {
                eprintln!("DesktopServices: dropped the start; the window is gone");
            }
        });
    }
}

impl qobject::DesktopServices {
    fn window_value(&self) -> *mut qobject::QQuickWindow {
        self.window as *mut qobject::QQuickWindow
    }

    unsafe fn set_window(mut self: Pin<&mut Self>, window: *mut qobject::QQuickWindow) {
        if self.window != window as usize {
            self.as_mut().rust_mut().window = window as usize;
            self.window_changed();
        }
    }

    fn start(mut self: Pin<&mut Self>) {
        // SAFETY: the GUI thread; the controller outlives the window's services.
        let Some(controller) = (unsafe { super::controller::window_controller() }) else { return };
        if std::env::var_os("CLARP_SCREENSHOT_PATH").is_some() {
            return;
        }
        self.as_mut().start_presence();
        let muted = controller.muted_pub();
        let qt = self.qt_thread();
        let act: Arc<dyn Fn(Action) + Send + Sync> = Arc::new(move |action| {
            if qt.queue(move |services| services.perform(action)).is_err() {
                eprintln!("DesktopServices: dropped a tray action; the window is gone");
            }
        });
        let icons = [22, 48].iter().filter_map(|&side| clarp_core::media::argb_icon(ICON, side)).map(|(width, height, data)| ksni::Icon { width, height, data }).collect();
        let tray = ClarpTray { muted, icons, act };
        let qt = self.qt_thread();
        crate::runtime::handle().spawn(async move {
            use ksni::TrayMethods;
            match tray.spawn().await {
                Ok(handle) => {
                    if qt.queue(move |services| services.tray_ready(handle)).is_err() {
                        eprintln!("DesktopServices: the tray outlived its window");
                    }
                }
                // No tray host (or no session bus): no tray, as in the C++ client.
                Err(error) => eprintln!("DesktopServices: no system tray: {error}"),
            }
        });
    }

    fn tray_ready(mut self: Pin<&mut Self>, handle: ksni::Handle<ClarpTray>) {
        let Some(mut controller) = (unsafe { super::controller::window_controller() }) else { return };
        let mute_tray = handle.clone();
        let muted = controller.as_mut().connect_muted_changed(
            move |controller| {
                let muted = controller.muted_pub();
                let tray = mute_tray.clone();
                crate::runtime::handle().spawn(async move {
                    tray.update(|tray| tray.muted = muted).await;
                });
            },
            cxx_qt::ConnectionType::QueuedConnection,
        );
        let notified = controller.connect_notification_requested(
            |_, title, body| notify(title.to_string(), body.to_string()),
            cxx_qt::ConnectionType::QueuedConnection,
        );
        let mut rust = self.as_mut().rust_mut();
        rust.guards.extend([muted, notified]);
        rust.tray = Some(handle);
    }


    // ---- presence ------------------------------------------------------------------

    fn now(&self) -> i64 {
        self.clock.elapsed().as_millis() as i64
    }

    fn start_presence(mut self: Pin<&mut Self>) {
        self.as_mut().rust_mut().presence_on = true;
        let filter = (unsafe { self.as_mut().get_unchecked_mut() } as *mut Self).cast::<qobject::QObject>();
        if let Some(application) = unsafe { qobject::QCoreApplication::running_application().as_mut() } {
            unsafe { Pin::new_unchecked(application).install_event_filter(filter) };
        }
        self.as_mut().presence_tick();
        // The login session's lock and sleep state, from logind (read only).
        let qt = self.qt_thread();
        crate::runtime::handle().spawn(async move { watch_login_session(qt).await });
    }

    fn apply_presence(self: Pin<&mut Self>, reports: Vec<clarp_core::presence::Report>) {
        use clarp_core::presence::Report;
        let Some(controller) = (unsafe { super::controller::window_controller() }) else { return };
        for report in reports {
            match report {
                Report::Presence { sequence, active } => controller.report_desktop_presence(&self.instance, sequence, active),
                Report::Activity { sequence, foreground, input_age_ms } => {
                    controller.report_application_activity(&self.instance, sequence, foreground, input_age_ms)
                }
            }
        }
    }

    /// Once a second: is the window in front, and what the controller says.
    fn presence_tick(mut self: Pin<&mut Self>) {
        let now = self.now();
        let window = unsafe { (self.window as *const qobject::QQuickWindow).as_ref() };
        let foreground = window.is_some_and(|w| w.is_active() && w.is_visible() && w.is_exposed());
        let inputs = unsafe { super::controller::window_controller() }.map(|c| c.presence_inputs()).unwrap_or((false, false));
        let mut reports = Vec::new();
        {
            let mut rust = self.as_mut().rust_mut();
            if inputs.0 != rust.inputs.0 {
                reports.extend(rust.presence.set_enabled(inputs.0, now));
            }
            if inputs.1 != rust.inputs.1 {
                reports.extend(rust.presence.set_connected(inputs.1, now));
            }
            rust.inputs = inputs;
            if foreground != rust.foreground {
                rust.foreground = foreground;
                reports.extend(rust.presence.set_foreground(foreground, now));
            }
            reports.extend(rust.presence.refresh(now));
        }
        self.as_mut().apply_presence(reports);
        let qt = self.qt_thread();
        crate::runtime::after(Duration::from_secs(1), move || {
            if qt.queue(|services| services.presence_tick()).is_err() {
                eprintln!("DesktopServices: presence stopped; the window is gone");
            }
        });
    }

    fn login_state(mut self: Pin<&mut Self>, state: LoginState) {
        let now = self.now();
        let reports = match state {
            LoginState::Session { available, unlocked } => self.as_mut().rust_mut().presence.set_session_state(available, unlocked, now),
            LoginState::Sleeping(sleeping) => self.as_mut().rust_mut().presence.prepare_for_sleep(sleeping, now),
        };
        self.apply_presence(reports);
    }

    unsafe fn event_filter(mut self: Pin<&mut Self>, _watched: *mut qobject::QObject, event: *mut qobject::QEvent) -> bool {
        let input = unsafe { event.as_ref() }.is_some_and(|e| e.is_input_event());
        if input && self.presence_on && self.foreground {
            let now = self.now();
            let reports = self.as_mut().rust_mut().presence.note_interaction(now);
            if !reports.is_empty() {
                self.apply_presence(reports);
            }
        }
        false
    }

    fn perform(self: Pin<&mut Self>, action: Action) {
        match action {
            Action::Show => {
                // SAFETY: the window owns these services and outlives them.
                if let Some(window) = unsafe { (self.window as *mut qobject::QQuickWindow).as_mut() } {
                    let mut window = unsafe { Pin::new_unchecked(window) };
                    window.as_mut().show();
                    window.as_mut().raise();
                    window.request_activate();
                }
            }
            Action::Mute(muted) => {
                if let Some(controller) = unsafe { super::controller::window_controller() } {
                    controller.set_muted_pub(muted);
                }
            }
            Action::Quit => qobject::QCoreApplication::quit_application(),
        }
    }
}

/// A desktop notification (what Qt's tray `showMessage` sends on Linux).
fn notify(title: String, body: String) {
    crate::runtime::handle().spawn(async move {
        let sent = async {
            let connection = zbus::Connection::session().await?;
            let hints: std::collections::HashMap<&str, zbus::zvariant::Value> = std::collections::HashMap::new();
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
            eprintln!("DesktopServices: notification failed: {error}");
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

/// Polls the session every 10 s and follows suspend; an unknown state
/// never suppresses phone alerts.
async fn watch_login_session(qt: cxx_qt::CxxQtThread<qobject::DesktopServices>) {
    use futures_util::StreamExt;
    let send = |state: LoginState| qt.queue(move |services| services.login_state(state)).is_ok();
    let bus = match zbus::Connection::system().await {
        Ok(bus) => bus,
        Err(error) => {
            eprintln!("DesktopServices: no system bus, presence stays off: {error}");
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
                let qt = qt.clone();
                crate::runtime::handle().spawn(async move {
                    while let Some(Ok(message)) = stream.next().await {
                        let Ok(sleeping) = message.body().deserialize::<bool>() else { continue };
                        if qt.queue(move |services| services.login_state(LoginState::Sleeping(sleeping))).is_err() {
                            return;
                        }
                    }
                });
            }
            Err(error) => eprintln!("DesktopServices: cannot follow suspend: {error}"),
        }
    }
    let mut session: Option<zbus::zvariant::OwnedObjectPath> = None;
    loop {
        if session.is_none() {
            match login_session(&bus).await {
                Ok(path) if path.as_str() != "/" => session = Some(path),
                Ok(_) => {}
                Err(error) => eprintln!("DesktopServices: no login session: {error}"),
            }
        }
        let state = match &session {
            Some(path) => match session_state(&bus, path.as_str()).await {
                Ok(state) => state,
                Err(error) => {
                    eprintln!("DesktopServices: login session state failed: {error}");
                    session = None;
                    LoginState::Session { available: false, unlocked: false }
                }
            },
            None => LoginState::Session { available: false, unlocked: false },
        };
        if !send(state) {
            return;
        }
        tokio::time::sleep(Duration::from_secs(10)).await;
    }
}
