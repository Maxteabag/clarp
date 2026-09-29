use cxx_qt_build::{CxxQtBuilder, QmlModule};

fn main() {
    CxxQtBuilder::new_qml_module(QmlModule::new("Clarp.Native").qml_file("qml/ScaffoldProbe.qml"))
        .qt_module("Network")
        .files(["src/bridge/controller.rs", "src/bridge/conversation_model.rs"])
        .build();
}
