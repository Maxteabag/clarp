//! DesktopServices: what the window offers the desktop (C++
//! `DesktopIntegration`): a tray icon with Show, Mute voice replies and Quit,
//! and notifications for replies in chats that are not open. The root
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
        include!(<QtCore/QCoreApplication>);
        type QCoreApplication;
        #[Self = "QCoreApplication"]
        #[cxx_name = "quit"]
        fn quit_application();
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

#[derive(Default)]
pub struct DesktopServicesRust {
    window: usize,
    tray: Option<ksni::Handle<ClarpTray>>,
    guards: Vec<QMetaObjectConnectionGuard>,
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

    fn start(self: Pin<&mut Self>) {
        // SAFETY: the GUI thread; the controller outlives the window's services.
        let Some(controller) = (unsafe { super::controller::window_controller() }) else { return };
        if std::env::var_os("CLARP_SCREENSHOT_PATH").is_some() {
            return;
        }
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
