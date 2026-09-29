use cxx_qt_build::{CxxQtBuilder, QResource, QResources, QmlFile, QmlModule};
use qt_build_utils::QResourceFile;

/// The QML and resource files of the C++ `Clarp.Desktop` module, read from its
/// CMakeLists so both builds ship the same presentation. `qml` and `resources`
/// in this crate are symlinks into `desktop/`, so the paths match the C++
/// layout and every `qrc:/qt/qml/Clarp/Desktop/...` URL resolves the same way.
fn module_files() -> (Vec<String>, Vec<String>) {
    let cmake = std::fs::read_to_string("../../desktop/CMakeLists.txt").expect("desktop/CMakeLists.txt");
    println!("cargo::rerun-if-changed=../../desktop/CMakeLists.txt");
    let start = cmake.find("URI Clarp.Desktop").expect("the Clarp.Desktop qml module");
    let block = &cmake[start..start + cmake[start..].find("\n)").expect("end of qt_add_qml_module")];
    let (mut qml, mut resources, mut section) = (Vec::new(), Vec::new(), "");
    for word in block.split_whitespace() {
        match word {
            "QML_FILES" | "RESOURCES" => section = word,
            _ if word.contains('/') && section == "QML_FILES" => qml.push(word.to_owned()),
            _ if word.contains('/') && section == "RESOURCES" => resources.push(word.to_owned()),
            _ => {}
        }
    }
    for file in qml.iter().chain(&resources) {
        println!("cargo::rerun-if-changed={file}");
    }
    (qml, resources)
}

/// Qt's policy QTP0004 (on under the C++ `qt_standard_project_setup(REQUIRES
/// 6.11)`) gives every subdirectory holding module QML files its own qmldir
/// that imports the module, so `qml/Main.qml` sees `AppController` and the
/// other module types without an import. cxx-qt-build does not, so write them.
fn subdirectory_qmldirs(qml: &[String]) -> Vec<QResourceFile> {
    let out = std::path::PathBuf::from(std::env::var("OUT_DIR").expect("OUT_DIR"));
    let mut dirs: Vec<&str> = qml.iter().filter_map(|path| path.rsplit_once('/').map(|(dir, _)| dir)).collect();
    dirs.sort_unstable();
    dirs.dedup();
    dirs.into_iter()
        .map(|dir| {
            let file = out.join(format!("{}.qmldir", dir.replace('/', "_")));
            std::fs::write(&file, "import Clarp.Desktop auto\n").expect("write a subdirectory qmldir");
            QResourceFile::new(file).alias(format!("{dir}/qmldir"))
        })
        .collect()
}

fn main() {
    let (qml, resources) = module_files();
    let qmldirs = subdirectory_qmldirs(&qml);
    let files = qml.iter().map(|path| QmlFile::from(path.as_str()).singleton(path.ends_with("/Theme.qml")));
    CxxQtBuilder::new_qml_module(QmlModule::new("Clarp.Desktop").qml_files(files))
        .qt_module("Network")
        .qrc_resources(
            QResources::new()
                .resource(QResource::new().files(resources.iter().map(String::as_str)))
                .resource(QResource::new().files(qmldirs)),
        )
        .files(["src/bridge/agent_filter_model.rs", "src/bridge/agent_list_model.rs", "src/bridge/controller.rs", "src/bridge/conversation_model.rs", "src/bridge/directory_models.rs", "src/bridge/pane_tree_model.rs", "src/bridge/presentation_model.rs", "src/bridge/preview_versions.rs", "src/bridge/tool_narrator.rs", "src/bridge/transcript_layout.rs", "src/bridge/transcript_rows.rs", "src/bridge/quick.rs"])
        .build();
}
