use cxx_qt_build::{CxxQtBuilder, QmlModule};

fn main() {
    CxxQtBuilder::new_qml_module(QmlModule::new("Clarp.Native").qml_file("qml/ScaffoldProbe.qml"))
        .qt_module("Network")
        .files(["src/bridge/agent_filter_model.rs", "src/bridge/agent_list_model.rs", "src/bridge/controller.rs", "src/bridge/conversation_model.rs", "src/bridge/directory_models.rs", "src/bridge/pane_tree_model.rs", "src/bridge/presentation_model.rs"])
        .build();
}
