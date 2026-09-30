mod audio_coordinator;
mod audio_input;
mod audio_output;
mod bridge;
mod fonts;
mod list_replay;
mod qjson;
mod runtime;

use cxx_qt_lib::{QByteArray, QGuiApplication, QMap, QMapPair_QString_QVariant, QQmlApplicationEngine, QString, QUrl, QVariant};

/// The root document. The C++ app calls `loadFromModule("Clarp.Desktop",
/// "Main")`, which cxx-qt does not bind; instantiating `Main` from a document
/// that imports the module is equivalent (Main.qml resolves inside its
/// module, so the native types it uses unqualified are found). It also hands
/// Main the command-line launch once loaded (C++ `openLaunchAgent` through
/// `invokeMethod`) and runs screenshot captures (`CLARP_SCREENSHOT_PATH`):
/// size the window, wait, grab it to the path and quit; a failed capture
/// exits non-zero.
const MAIN_DOCUMENT: &str = r#"import QtQuick
import Clarp.Desktop
Main {
    id: root
    property bool launchNow: false
    property string launchBackend: ""
    property string launchModel: ""
    property string launchEffort: ""
    property int launchAnonymous: -1
    property string launchDirectory: ""
    property string screenshotPath: ""
    property int screenshotDelay: 2000
    property int screenshotWidth: 0
    property int screenshotHeight: 0
    property string instanceSocket: ""
    Component.onCompleted: {
        if (screenshotWidth > 0 && screenshotHeight > 0) {
            root.width = Math.max(760, screenshotWidth)
            root.height = Math.max(520, screenshotHeight)
        }
        if (launchNow)
            Qt.callLater(() => root.openLaunchAgent(launchBackend, launchModel, launchEffort, launchAnonymous, launchDirectory))
    }
    DesktopServices { window: root }
    // A second launch opens its window here. A closed extra window is
    // destroyed with its controller, so it stops costing memory; the first
    // window keeps the process state, tray and presence.
    InstanceServer {
        socketPath: root.instanceSocket
        onWindowRequested: (launch, sidebarVisible, backend, model, effort, anonymous, directory) => {
            const window = extraWindow.createObject(null, { launchOnStartup: launch, sidebarVisible: sidebarVisible })
            if (!window) {
                console.warn("Clarp: could not open a window for a second launch:", extraWindow.errorString())
                return
            }
            window.visibleChanged.connect(() => { if (!window.visible) window.destroy() })
            if (launch)
                Qt.callLater(() => window.openLaunchAgent(backend, model, effort, anonymous, directory))
            window.requestActivate()
        }
    }
    Component { id: extraWindow; Main {} }
    WindowCapture { id: capture }
    Timer {
        interval: root.screenshotDelay
        running: root.screenshotPath !== ""
        onTriggered: Qt.exit(capture.capture(root, root.screenshotPath) ? 0 : 1)
    }
}
"#;

/// `--preview-versions`: the version manager window instead of the desktop.
const VERSIONS_DOCUMENT: &str = "import Clarp.Desktop\nPreviewVersionWindow {}\n";

fn main() {
    let arguments: Vec<String> = std::env::args().skip(1).collect();
    let options = match clarp_core::launch::parse(&arguments) {
        Ok(options) => options,
        Err(message) => {
            eprintln!("{message}");
            std::process::exit(1);
        }
    };
    if options.help {
        print!("{}", clarp_core::launch::HELP);
        return;
    }
    if options.version {
        println!("clarp-desktop {}", env!("CARGO_PKG_VERSION"));
        return;
    }
    // A second launch of this desktop opens its window in the running
    // process (~57 ms against ~210 ms for the C++ client) and exits here.
    let instance_socket = std::env::current_exe()
        .ok()
        .and_then(|executable| clarp_core::instance::socket_path(&arguments, &executable, |name| std::env::var(name).ok()));
    if let Some(socket) = &instance_socket
        && clarp_core::instance::forward(socket, &arguments)
    {
        return;
    }
    let screenshot = std::env::var_os("CLARP_SCREENSHOT_PATH").is_some();
    let restoring = std::env::var("CLARP_RESTORE_DESKTOP").as_deref() == Ok("1");
    let settings = clarp_core::settings::Settings::user();
    let launch_on_startup = options.launch_on_startup(restoring, settings.boolean("launch/newAgentOnStartup", true), screenshot);
    // The app owns its Qt Quick style, like the C++ QQuickStyle::setStyle.
    // The controller reads how it was launched from the environment: it is
    // created by QML, out of reach of main.
    // SAFETY: single-threaded here; no other thread reads the environment yet.
    unsafe {
        std::env::remove_var("QT_STYLE_OVERRIDE");
        std::env::set_var("QT_QUICK_CONTROLS_STYLE", "Basic");
        // The software renderer paints the first frame sooner and keeps each
        // instance smaller; CLARP_RENDERER=gl or QT_QUICK_BACKEND opts out.
        if std::env::var_os("QT_QUICK_BACKEND").is_none() && std::env::var("CLARP_RENDERER").as_deref() != Ok("gl") {
            std::env::set_var("QT_QUICK_BACKEND", "software");
        }
        if options.auto_start(launch_on_startup) {
            std::env::set_var("CLARP_RS_LAUNCH_MODE", "1");
        }
        if options.empty_startup(restoring) {
            std::env::set_var("CLARP_EMPTY_STARTUP", "1");
        }
    }

    let mut app = QGuiApplication::new();
    if let Some(mut app) = app.as_mut() {
        // Its own settings namespace: the Rust build never shares QSettings
        // with an installed C++ Clarp, and screenshot runs never touch either.
        let name = if options.preview_versions {
            "ClarpRustPreviewVersionManager"
        } else if screenshot {
            "ClarpRustScreenshot"
        } else {
            "ClarpRust"
        };
        app.as_mut().set_application_name(&QString::from(name));
        app.as_mut().set_application_display_name(&QString::from("Clarp"));
        app.as_mut().set_organization_name(&QString::from("MaxTeaBag"));
        app.as_mut().set_organization_domain(&QString::from("maxteabag.com"));
        app.as_mut().set_application_version(&QString::from(env!("CARGO_PKG_VERSION")));
    }
    let mut engine = QQmlApplicationEngine::new();
    let probe = std::env::var("CLARP_RS_QML").ok();
    if let Some(mut engine) = engine.as_mut() {
        if probe.is_none() && !options.preview_versions {
            let mut properties = QMap::<QMapPair_QString_QVariant>::default();
            let text = |value: &str| QVariant::from(&QString::from(value));
            properties.insert(QString::from("launchOnStartup"), QVariant::from(&launch_on_startup));
            properties.insert(QString::from("sidebarVisible"), QVariant::from(&options.empty_startup(restoring)));
            properties.insert(QString::from("launchNow"), QVariant::from(&launch_on_startup));
            properties.insert(QString::from("launchBackend"), text(&options.backend));
            properties.insert(QString::from("launchModel"), text(options.model.as_deref().unwrap_or_default()));
            properties.insert(QString::from("launchEffort"), text(options.effort.as_deref().unwrap_or_default()));
            properties.insert(QString::from("launchAnonymous"), QVariant::from(&options.anonymous_mode()));
            properties.insert(QString::from("launchDirectory"), text(options.cwd.as_deref().unwrap_or_default()));
            if let Some(socket) = &instance_socket {
                properties.insert(QString::from("instanceSocket"), text(&socket.to_string_lossy()));
            }
            if let Ok(path) = std::env::var("CLARP_SCREENSHOT_PATH") {
                let delay = std::env::var("CLARP_SCREENSHOT_DELAY_MS").ok().and_then(|d| d.parse::<i32>().ok());
                let delay = delay.filter(|d| *d > 0).map_or(2_000, |d| d.clamp(2_400, 60_000));
                properties.insert(QString::from("screenshotPath"), text(&path));
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
                let document = if options.preview_versions { VERSIONS_DOCUMENT } else { MAIN_DOCUMENT };
                engine.as_mut().load_data(&QByteArray::from(document), &QUrl::from("qrc:/clarp-rust/Root.qml"))
            }
        }
    }
    // The restore request stays in the environment: worker threads run by
    // now, so it cannot be removed safely. Processes the controller starts
    // drop it themselves (see `clarp_core::launch::RESTORE_VARIABLES`).
    let code = app.as_mut().map_or(1, |app| app.exec());
    // Tear the windows down before exiting, so what their objects release
    // on destruction happens: drafts are saved, the instance socket goes.
    drop(engine);
    drop(app);
    std::process::exit(code);
}
