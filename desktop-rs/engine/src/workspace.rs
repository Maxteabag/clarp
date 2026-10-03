//! The pane workspace (the Qt `PaneTreeModel` around `clarp_core::panes`):
//! the split tree, the active pane (whose chat is the selection), and the
//! layout's persistence. Writes run on a thread, one at a time; their
//! results come back through the engine's queue.

use std::path::PathBuf;
use std::sync::Arc;

use clarp_core::panes::{FileWorkspaceStore, PaneTree, Signal, WorkspaceStore};

use crate::{Change, Engine, Message};

/// `CLARP_WORKSPACE_STORE=off` keeps layouts in memory; any other value is
/// the store's path. By default they live beside the Slint app's settings,
/// apart from the Qt apps' layouts.
pub fn default_store_path() -> Option<PathBuf> {
    match std::env::var("CLARP_WORKSPACE_STORE") {
        Ok(value) if value == "off" => None,
        Ok(value) if !value.is_empty() => Some(PathBuf::from(value)),
        _ => {
            let config = std::env::var_os("XDG_CONFIG_HOME")
                .filter(|dir| !dir.is_empty())
                .map(PathBuf::from)
                .or_else(|| std::env::var_os("HOME").map(|home| PathBuf::from(home).join(".config")))?;
            Some(config.join("MaxTeaBag").join("ClarpSlint").join("workspaces.json"))
        }
    }
}

pub(crate) struct Panes {
    pub(crate) tree: PaneTree,
    store: Option<Arc<dyn WorkspaceStore>>,
    /// The write in flight; closing waits for it so it cannot land after
    /// the final one.
    writer: Option<std::thread::JoinHandle<()>>,
    writing: bool,
}

impl Panes {
    pub(crate) fn new(store: Option<PathBuf>) -> Self {
        let store: Option<Arc<dyn WorkspaceStore>> = store.map(|path| Arc::new(FileWorkspaceStore::new(path)) as Arc<dyn WorkspaceStore>);
        let restore = std::env::var_os("CLARP_EMPTY_STARTUP").is_none();
        let tree = match &store {
            Some(store) => PaneTree::persisted(store.as_ref(), restore),
            None => PaneTree::new(),
        };
        Self { tree, store, writer: None, writing: false }
    }
}

impl Engine {
    pub fn panes(&self) -> &PaneTree {
        &self.panes.tree
    }

    /// Runs a layout change: the view hears of it, the active pane's chat
    /// becomes the selection, every shown pane's chat loads, and the layout
    /// is saved.
    pub fn with_panes<R>(&mut self, change: impl FnOnce(&mut PaneTree) -> R) -> R {
        let result = change(&mut self.panes.tree);
        self.pane_signals();
        self.start_pane_write();
        result
    }

    fn pane_signals(&mut self) {
        for signal in self.panes.tree.take_signals() {
            match signal {
                Signal::TreeChanged | Signal::WorkspaceSaveWarningChanged => self.changes.push(Change::Panes),
                Signal::ActivePaneChanged => {
                    self.changes.push(Change::Panes);
                    let session = self.panes.tree.active_session();
                    if !session.is_empty() && session != self.selected {
                        self.select(&session);
                    } else if session.is_empty() && !self.selected.is_empty() {
                        self.selected.clear();
                        self.changes.push(Change::Selection);
                    }
                }
            }
        }
        self.open_shown_panes();
    }

    /// Every shown pane's chat is loaded, not only the selected one's.
    pub(crate) fn open_shown_panes(&mut self) {
        let sessions: Vec<String> = self
            .panes
            .tree
            .view_layout()
            .iter()
            .filter(|pane| pane.get("shown").and_then(serde_json::Value::as_bool) == Some(true))
            .filter_map(|pane| pane.get("session").and_then(serde_json::Value::as_str).map(str::to_owned))
            .filter(|session| !session.is_empty())
            .collect();
        for session in sessions {
            if !self.conversations.contains_key(&session) {
                self.open_log(&session);
            }
        }
    }

    fn start_pane_write(&mut self) {
        let Some(store) = self.panes.store.clone() else { return };
        if self.panes.writing {
            return;
        }
        let Some(request) = self.panes.tree.next_write() else { return };
        self.panes.writing = true;
        let (sender, wake) = (self.sender.clone(), self.wake.clone());
        let spawned = std::thread::Builder::new().name("layout-writer".into()).spawn(move || {
            let result = store.write(&request);
            if sender.send(Message::PanesWritten(result)).is_ok() {
                wake();
            }
        });
        match spawned {
            Ok(handle) => self.panes.writer = Some(handle),
            Err(error) => {
                self.panes.writing = false;
                eprintln!("Engine: cannot save the pane layout: {error}");
            }
        }
    }

    pub(crate) fn panes_written(&mut self, result: clarp_core::panes::WriteResult) {
        self.panes.writing = false;
        if let Some(writer) = self.panes.writer.take()
            && writer.join().is_err()
        {
            eprintln!("Engine: the layout writer panicked");
        }
        self.panes.tree.finish_write(&result);
        self.pane_signals();
        self.start_pane_write();
    }

    /// Writes what is still pending, on the calling thread (closing).
    pub(crate) fn flush_panes(&mut self) {
        if let Some(writer) = self.panes.writer.take()
            && writer.join().is_err()
        {
            eprintln!("Engine: the layout writer panicked");
        }
        // Its result is still queued: apply it so the final write expects
        // the collection it left.
        while let Ok(message) = self.receiver.try_recv() {
            if let Message::PanesWritten(result) = message {
                self.panes.writing = false;
                self.panes.tree.finish_write(&result);
            }
        }
        if let Some(store) = self.panes.store.clone() {
            self.panes.tree.flush_writes(store.as_ref());
        }
    }
}
