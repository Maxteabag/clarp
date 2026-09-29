mod bridge;
mod list_replay;
mod qjson;

use cxx_qt_lib::{QGuiApplication, QQmlApplicationEngine, QUrl};

fn main() {
    let mut app = QGuiApplication::new();
    let mut engine = QQmlApplicationEngine::new();
    let root = std::env::var("CLARP_RS_QML")
        .unwrap_or_else(|_| "qrc:/qt/qml/Clarp/Native/qml/ScaffoldProbe.qml".into());
    if let Some(engine) = engine.as_mut() {
        engine.load(&QUrl::from(root.as_str()));
    }
    if let Some(app) = app.as_mut() {
        std::process::exit(app.exec());
    }
}
