mod audio_coordinator;
mod audio_input;
mod audio_output;
mod bridge;
mod fonts;
mod list_replay;
mod qjson;
mod runtime;

use cxx_qt_lib::{QByteArray, QGuiApplication, QMap, QMapPair_QString_QVariant, QQmlApplicationEngine, QString, QUrl, QVariant};

/// The C++ app calls `loadFromModule("Clarp.Desktop", "Main")`, which cxx-qt
/// does not bind. Instantiating `Main` from a document that imports the
/// module is equivalent: Main.qml resolves inside its module, so the native
/// types it uses unqualified (AppController, ...) are found.
const MAIN_DOCUMENT: &str = "import Clarp.Desktop\nMain {}\n";

/// Screenshot runs (`CLARP_SCREENSHOT_PATH`, like the C++ app): size the
/// window, wait for the chat to settle, grab it to the path and quit. A
/// failed capture exits non-zero.
const SCREENSHOT_DOCUMENT: &str = r#"import QtQuick
import Clarp.Desktop
Main {
    id: shot
    property string screenshotPath: ""
    property int screenshotDelay: 2000
    property int screenshotWidth: 0
    property int screenshotHeight: 0
    Component.onCompleted: {
        if (screenshotWidth > 0 && screenshotHeight > 0) {
            shot.width = Math.max(760, screenshotWidth)
            shot.height = Math.max(520, screenshotHeight)
        }
    }
    WindowCapture { id: capture }
    Timer {
        interval: shot.screenshotDelay
        running: shot.screenshotPath !== ""
        onTriggered: Qt.exit(capture.capture(shot, shot.screenshotPath) ? 0 : 1)
    }
}
"#;

fn main() {
    // The app owns its Qt Quick style, like the C++ QQuickStyle::setStyle.
    // SAFETY: single-threaded here; no other thread reads the environment yet.
    unsafe {
        std::env::remove_var("QT_STYLE_OVERRIDE");
        std::env::set_var("QT_QUICK_CONTROLS_STYLE", "Basic");
        // The software renderer paints the first frame sooner and keeps each
        // instance smaller; CLARP_RENDERER=gl or QT_QUICK_BACKEND opts out.
        if std::env::var_os("QT_QUICK_BACKEND").is_none() && std::env::var("CLARP_RENDERER").as_deref() != Ok("gl") {
            std::env::set_var("QT_QUICK_BACKEND", "software");
        }
    }
    let arguments: Vec<String> = std::env::args().skip(1).collect();
    let no_new_agent = arguments.iter().any(|a| a == "--no-new-agent");
    let screenshot = std::env::var_os("CLARP_SCREENSHOT_PATH").is_some();

    let mut app = QGuiApplication::new();
    if let Some(mut app) = app.as_mut() {
        // Its own settings namespace: the Rust build never shares QSettings
        // with an installed C++ Clarp, and screenshot runs never touch either.
        let name = if screenshot { "ClarpRustScreenshot" } else { "ClarpRust" };
        app.as_mut().set_application_name(&QString::from(name));
        app.as_mut().set_application_display_name(&QString::from("Clarp"));
        app.as_mut().set_organization_name(&QString::from("MaxTeaBag"));
        app.as_mut().set_organization_domain(&QString::from("maxteabag.com"));
        app.as_mut().set_application_version(&QString::from(env!("CARGO_PKG_VERSION")));
    }
    let mut engine = QQmlApplicationEngine::new();
    let probe = std::env::var("CLARP_RS_QML").ok();
    if let Some(mut engine) = engine.as_mut() {
        if probe.is_none() {
            let settings = clarp_core::settings::Settings::user();
            let launch_on_startup =
                !no_new_agent && settings.boolean("launch/newAgentOnStartup", true) && !screenshot;
            let mut properties = QMap::<QMapPair_QString_QVariant>::default();
            properties.insert(QString::from("launchOnStartup"), QVariant::from(&launch_on_startup));
            properties.insert(QString::from("sidebarVisible"), QVariant::from(&no_new_agent));
            if let Ok(path) = std::env::var("CLARP_SCREENSHOT_PATH") {
                let delay = std::env::var("CLARP_SCREENSHOT_DELAY_MS").ok().and_then(|d| d.parse::<i32>().ok());
                let delay = delay.filter(|d| *d > 0).map_or(2_000, |d| d.clamp(2_400, 60_000));
                properties.insert(QString::from("screenshotPath"), QVariant::from(&QString::from(path.as_str())));
                properties.insert(QString::from("screenshotDelay"), QVariant::from(&delay));
                let size = std::env::var("CLARP_SCREENSHOT_SIZE").unwrap_or_default();
                if let Some((width, height)) = size.split_once('x').and_then(|(w, h)| Some((w.parse::<i32>().ok()?, h.parse::<i32>().ok()?))) {
                    properties.insert(QString::from("screenshotWidth"), QVariant::from(&width));
                    properties.insert(QString::from("screenshotHeight"), QVariant::from(&height));
                }
            }
            engine.as_mut().set_initial_properties(&properties);
        }
        match probe.as_deref() {
            Some(path) => engine.as_mut().load(&QUrl::from(path)),
            None => {
                let document = if screenshot { SCREENSHOT_DOCUMENT } else { MAIN_DOCUMENT };
                engine.as_mut().load_data(&QByteArray::from(document), &QUrl::from("qrc:/clarp-rust/Root.qml"))
            }
        }
    }
    if let Some(app) = app.as_mut() {
        std::process::exit(app.exec());
    }
}
