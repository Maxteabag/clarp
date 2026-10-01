//! QObject over `clarp_core::panes::PaneTree`. Layout writes run on a worker
//! thread, one at a time, and their results come back on the Qt thread; a
//! write still pending when the model is destroyed is finished synchronously.

use std::path::PathBuf;
use std::pin::Pin;
use std::sync::Arc;
use std::thread::JoinHandle;

use clarp_core::panes::{FileWorkspaceStore, PaneTree, Signal, WorkspaceStore, WriteResult};
use cxx_qt::{CxxQtType, Threading};
use cxx_qt_lib::{QJsonArray, QJsonObject, QString};
use serde_json::Value;

use crate::qjson::{to_qjson, to_qjson_array};

#[cxx_qt::bridge]
pub mod qobject {
    unsafe extern "C++" {
        include!("cxx-qt-lib/qstring.h");
        type QString = cxx_qt_lib::QString;
        include!("cxx-qt-lib/qjsonobject.h");
        type QJsonObject = cxx_qt_lib::QJsonObject;
        include!("cxx-qt-lib/qjsonarray.h");
        type QJsonArray = cxx_qt_lib::QJsonArray;
    }

    extern "RustQt" {
        #[qobject]
        #[qml_element]
        #[qproperty(QJsonObject, root_node, cxx_name = "rootNode", READ = root_node_value, NOTIFY = tree_changed)]
        #[qproperty(QJsonObject, display_root, cxx_name = "displayRoot", READ = display_root_value, NOTIFY = tree_changed)]
        #[qproperty(QJsonArray, pane_layout, cxx_name = "paneLayout", READ = pane_layout_value, NOTIFY = tree_changed)]
        #[qproperty(QJsonArray, split_layout, cxx_name = "splitLayout", READ = split_layout_value, NOTIFY = tree_changed)]
        #[qproperty(QString, active_pane_id, cxx_name = "activePaneId", READ = active_pane_id_value, NOTIFY = active_pane_changed)]
        #[qproperty(QString, active_session, cxx_name = "activeSession", READ = active_session_value, NOTIFY = active_pane_changed)]
        #[qproperty(QString, zoomed_pane_id, cxx_name = "zoomedPaneId", READ = zoomed_pane_id_value, NOTIFY = tree_changed)]
        #[qproperty(QJsonArray, view_layout, cxx_name = "viewLayout", READ = view_layout_value, NOTIFY = tree_changed)]
        #[qproperty(QJsonArray, workspaces, READ = workspaces_value, NOTIFY = tree_changed)]
        #[qproperty(QString, active_workspace, cxx_name = "activeWorkspace", READ = active_workspace_value, NOTIFY = tree_changed)]
        #[qproperty(QString, workspace_save_warning, cxx_name = "workspaceSaveWarning", READ = workspace_save_warning_value, NOTIFY = workspace_save_warning_changed)]
        #[qproperty(i32, pane_count, cxx_name = "paneCount", READ = pane_count_value, NOTIFY = tree_changed)]
        type PaneTreeModel = super::PaneTreeModelRust;
    }

    unsafe extern "RustQt" {
        fn root_node_value(self: &PaneTreeModel) -> QJsonObject;
        fn display_root_value(self: &PaneTreeModel) -> QJsonObject;
        fn pane_layout_value(self: &PaneTreeModel) -> QJsonArray;
        fn split_layout_value(self: &PaneTreeModel) -> QJsonArray;
        fn active_pane_id_value(self: &PaneTreeModel) -> QString;
        fn active_session_value(self: &PaneTreeModel) -> QString;
        fn zoomed_pane_id_value(self: &PaneTreeModel) -> QString;
        fn view_layout_value(self: &PaneTreeModel) -> QJsonArray;
        fn workspaces_value(self: &PaneTreeModel) -> QJsonArray;
        fn active_workspace_value(self: &PaneTreeModel) -> QString;
        fn workspace_save_warning_value(self: &PaneTreeModel) -> QString;
        fn pane_count_value(self: &PaneTreeModel) -> i32;

        #[qsignal]
        #[cxx_name = "treeChanged"]
        fn tree_changed(self: Pin<&mut PaneTreeModel>);
        #[qsignal]
        #[cxx_name = "activePaneChanged"]
        fn active_pane_changed(self: Pin<&mut PaneTreeModel>);
        #[qsignal]
        #[cxx_name = "workspaceSaveWarningChanged"]
        fn workspace_save_warning_changed(self: Pin<&mut PaneTreeModel>);

        #[qinvokable]
        #[cxx_name = "saveWorkspaceLayoutInstead"]
        fn save_workspace_layout_instead(self: Pin<&mut PaneTreeModel>);
        #[qinvokable]
        #[cxx_name = "createWorkspace"]
        fn create_workspace(self: Pin<&mut PaneTreeModel>, name: &QString);
        #[qinvokable]
        #[cxx_name = "switchWorkspace"]
        fn switch_workspace(self: Pin<&mut PaneTreeModel>, id: &QString);
        #[qinvokable]
        #[cxx_name = "moveActiveToWorkspace"]
        fn move_active_to_workspace(self: Pin<&mut PaneTreeModel>, id: &QString);
        #[qinvokable]
        #[cxx_name = "saveState"]
        fn save_state(self: &PaneTreeModel) -> QJsonObject;
        /// Takes the state as JSON text (QML: `JSON.stringify(state)`).
        #[qinvokable]
        #[cxx_name = "loadStateText"]
        fn load_state_text(self: Pin<&mut PaneTreeModel>, state: &QString) -> bool;
        #[qinvokable]
        #[cxx_name = "setActiveSession"]
        fn set_active_session(self: Pin<&mut PaneTreeModel>, session: &QString);
        #[qinvokable]
        #[cxx_name = "setPaneSession"]
        fn set_pane_session(self: Pin<&mut PaneTreeModel>, pane_id: &QString, session: &QString);
        #[qinvokable]
        #[cxx_name = "splitActive"]
        fn split_active(self: Pin<&mut PaneTreeModel>, direction: &QString, session: &QString);
        #[qinvokable]
        #[cxx_name = "closePane"]
        fn close_pane(self: Pin<&mut PaneTreeModel>, pane_id: &QString);
        #[qinvokable]
        #[cxx_name = "focusPane"]
        fn focus_pane(self: Pin<&mut PaneTreeModel>, pane_id: &QString);
        #[qinvokable]
        fn navigate(self: Pin<&mut PaneTreeModel>, direction: &QString);
        #[qinvokable]
        #[cxx_name = "toggleZoom"]
        fn toggle_zoom(self: Pin<&mut PaneTreeModel>);
        #[qinvokable]
        #[cxx_name = "resizeActive"]
        fn resize_active(self: Pin<&mut PaneTreeModel>, delta: f64);
        #[qinvokable]
        #[cxx_name = "setSplitRatio"]
        fn set_split_ratio(self: Pin<&mut PaneTreeModel>, split_id: &QString, ratio: f64);
        #[qinvokable]
        fn equalize(self: Pin<&mut PaneTreeModel>);
    }

    impl cxx_qt::Threading for PaneTreeModel {}
    impl cxx_qt::Initialize for PaneTreeModel {}

    #[namespace = "rust::cxxqtlib1"]
    unsafe extern "C++" {
        include!("cxx-qt-lib/common.h");
        /// Owned by the AppController, which QML reaches it through.
        #[rust_name = "new_pane_tree_model"]
        fn make_unique() -> UniquePtr<PaneTreeModel>;
    }
}

/// `CLARP_WORKSPACE_STORE=off` disables persistence; any other value is the
/// store path. By default layouts live in the user config directory.
fn store_path() -> Option<PathBuf> {
    match std::env::var("CLARP_WORKSPACE_STORE") {
        Ok(value) if value == "off" => None,
        Ok(value) if !value.is_empty() => Some(PathBuf::from(value)),
        _ => {
            let config = std::env::var_os("XDG_CONFIG_HOME")
                .map(PathBuf::from)
                .or_else(|| std::env::var_os("HOME").map(|home| PathBuf::from(home).join(".config")))?;
            Some(config.join("MaxTeaBag").join("ClarpRust").join("workspaces.json"))
        }
    }
}

pub struct PaneTreeModelRust {
    core: PaneTree,
    store: Option<Arc<dyn WorkspaceStore>>,
    writer: Option<JoinHandle<WriteResult>>,
}

impl Default for PaneTreeModelRust {
    fn default() -> Self {
        let store: Option<Arc<dyn WorkspaceStore>> =
            store_path().map(|path| Arc::new(FileWorkspaceStore::new(path)) as Arc<dyn WorkspaceStore>);
        let restore = std::env::var_os("CLARP_EMPTY_STARTUP").is_none();
        let core = match &store {
            Some(store) => PaneTree::persisted(store.as_ref(), restore),
            None => PaneTree::new(),
        };
        Self { core, store, writer: None }
    }
}

impl Drop for PaneTreeModelRust {
    fn drop(&mut self) {
        if let Some(writer) = self.writer.take() {
            match writer.join() {
                Ok(result) => self.core.finish_write(&result),
                Err(_) => eprintln!("PaneTreeModel: the layout writer panicked"),
            }
        }
        if let Some(store) = &self.store {
            self.core.flush_writes(store.as_ref());
        }
    }
}

fn object(value: Value) -> QJsonObject {
    match value {
        Value::Object(_) => to_qjson(&value).to_object(),
        _ => QJsonObject::default(),
    }
}

fn array(rows: Vec<serde_json::Map<String, Value>>) -> QJsonArray {
    to_qjson_array(&rows.into_iter().map(Value::Object).collect::<Vec<_>>())
}

impl cxx_qt::Initialize for qobject::PaneTreeModel {
    fn initialize(self: Pin<&mut Self>) {
        // Restoring at construction queued a write; start it now that the
        // object can receive the result.
        self.mutate(|_| ());
    }
}

impl qobject::PaneTreeModel {
    fn root_node_value(&self) -> QJsonObject {
        object(self.core.root_node())
    }
    fn display_root_value(&self) -> QJsonObject {
        object(self.core.display_root())
    }
    fn pane_layout_value(&self) -> QJsonArray {
        array(self.core.pane_layout())
    }
    fn split_layout_value(&self) -> QJsonArray {
        array(self.core.split_layout())
    }
    fn active_pane_id_value(&self) -> QString {
        QString::from(self.core.active_pane_id())
    }
    fn active_session_value(&self) -> QString {
        QString::from(self.core.active_session().as_str())
    }
    fn zoomed_pane_id_value(&self) -> QString {
        QString::from(self.core.zoomed_pane_id())
    }
    fn view_layout_value(&self) -> QJsonArray {
        array(self.core.view_layout())
    }
    fn workspaces_value(&self) -> QJsonArray {
        array(self.core.workspaces())
    }
    fn active_workspace_value(&self) -> QString {
        QString::from(self.core.active_workspace())
    }
    fn workspace_save_warning_value(&self) -> QString {
        QString::from(self.core.workspace_save_warning())
    }
    fn pane_count_value(&self) -> i32 {
        self.core.pane_count() as i32
    }

    pub fn core(&self) -> &PaneTree {
        &self.core
    }

    /// Run a core mutation, emit its signals, then start any queued write.
    pub fn mutate<R>(mut self: Pin<&mut Self>, change: impl FnOnce(&mut PaneTree) -> R) -> R {
        let result = change(&mut self.as_mut().rust_mut().core);
        self.as_mut().emit_signals();
        self.start_write();
        result
    }

    fn emit_signals(mut self: Pin<&mut Self>) {
        for signal in self.as_mut().rust_mut().core.take_signals() {
            match signal {
                Signal::TreeChanged => self.as_mut().tree_changed(),
                Signal::ActivePaneChanged => self.as_mut().active_pane_changed(),
                Signal::WorkspaceSaveWarningChanged => self.as_mut().workspace_save_warning_changed(),
            }
        }
    }

    fn start_write(mut self: Pin<&mut Self>) {
        let Some(store) = self.store.clone() else { return };
        if self.writer.is_some() {
            return;
        }
        let Some(request) = self.as_mut().rust_mut().core.next_write() else { return };
        let qt = self.qt_thread();
        let writer = std::thread::spawn(move || {
            let result = store.write(&request);
            let queued = qt.queue(|model: Pin<&mut qobject::PaneTreeModel>| model.finish_write());
            if queued.is_err() {
                // The model is gone; its Drop joins this thread and applies it.
            }
            result
        });
        self.as_mut().rust_mut().writer = Some(writer);
    }

    fn finish_write(mut self: Pin<&mut Self>) {
        let Some(writer) = self.as_mut().rust_mut().writer.take() else { return };
        match writer.join() {
            Ok(result) => self.as_mut().rust_mut().core.finish_write(&result),
            Err(_) => eprintln!("PaneTreeModel: the layout writer panicked"),
        }
        self.as_mut().emit_signals();
        self.start_write();
    }

    fn save_workspace_layout_instead(self: Pin<&mut Self>) {
        self.mutate(PaneTree::save_workspace_layout_instead);
    }
    fn create_workspace(self: Pin<&mut Self>, name: &QString) {
        let name = name.to_string();
        self.mutate(|core| core.create_workspace(&name));
    }
    fn switch_workspace(self: Pin<&mut Self>, id: &QString) {
        let id = id.to_string();
        self.mutate(|core| core.switch_workspace(&id));
    }
    fn move_active_to_workspace(self: Pin<&mut Self>, id: &QString) {
        let id = id.to_string();
        self.mutate(|core| core.move_active_to_workspace(&id));
    }
    fn save_state(&self) -> QJsonObject {
        object(Value::Object(self.core.save_state()))
    }
    fn load_state_text(self: Pin<&mut Self>, state: &QString) -> bool {
        match serde_json::from_str::<Value>(&state.to_string()) {
            Ok(Value::Object(state)) => self.mutate(|core| core.load_state(&state)),
            _ => false,
        }
    }
    fn set_active_session(self: Pin<&mut Self>, session: &QString) {
        let session = session.to_string();
        self.mutate(|core| core.set_active_session(&session));
    }
    fn set_pane_session(self: Pin<&mut Self>, pane_id: &QString, session: &QString) {
        let (pane, session) = (pane_id.to_string(), session.to_string());
        self.mutate(|core| core.set_pane_session(&pane, &session));
    }
    fn split_active(self: Pin<&mut Self>, direction: &QString, session: &QString) {
        let (direction, session) = (direction.to_string(), session.to_string());
        self.mutate(|core| core.split_active(&direction, &session));
    }
    fn close_pane(self: Pin<&mut Self>, pane_id: &QString) {
        let pane = pane_id.to_string();
        self.mutate(|core| core.close_pane(&pane));
    }
    fn focus_pane(self: Pin<&mut Self>, pane_id: &QString) {
        let pane = pane_id.to_string();
        self.mutate(|core| core.focus_pane(&pane));
    }
    fn navigate(self: Pin<&mut Self>, direction: &QString) {
        let direction = direction.to_string();
        self.mutate(|core| core.navigate(&direction));
    }
    fn toggle_zoom(self: Pin<&mut Self>) {
        self.mutate(PaneTree::toggle_zoom);
    }
    fn resize_active(self: Pin<&mut Self>, delta: f64) {
        self.mutate(|core| core.resize_active(delta));
    }
    fn set_split_ratio(self: Pin<&mut Self>, split_id: &QString, ratio: f64) {
        let split = split_id.to_string();
        self.mutate(|core| core.set_split_ratio(&split, ratio));
    }
    fn equalize(self: Pin<&mut Self>) {
        self.mutate(PaneTree::equalize);
    }
}
