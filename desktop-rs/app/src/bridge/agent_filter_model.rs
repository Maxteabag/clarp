//! QSortFilterProxyModel for the sidebar, over any roster-shaped source.
//! Filter/sort decisions and the helper tree come from
//! `clarp_core::sidebar`; Qt keeps the proxy mapping and applies filter
//! changes as row inserts/removes and re-sorts as layout changes, never a
//! reset (a reset recreated every sidebar row: 490 ms in the C++ client).

use std::pin::Pin;

use clarp_core::roster::Role as SourceRole;
use clarp_core::sidebar::{FilterInput, Sidebar, TreeInput};
use cxx_qt::{CxxQtType, QMetaObjectConnectionGuard};
use cxx_qt_lib::{QByteArray, QHash, QHashPair_i32_QByteArray, QJsonArray, QJsonObject, QJsonValue, QList, QModelIndex, QString, QVariant};

use super::agent_list_model::role_id as source_role;

#[cxx_qt::bridge]
pub mod qobject {
    #[namespace = "Qt"]
    #[repr(i32)]
    enum SortOrder {
        AscendingOrder = 0,
        DescendingOrder = 1,
    }

    #[namespace = "Qt"]
    unsafe extern "C++" {
        type SortOrder;
    }

    unsafe extern "C++" {
        include!(<QtCore/QSortFilterProxyModel>);
        type QSortFilterProxyModel;

        include!("cxx-qt-lib/qmodelindex.h");
        type QModelIndex = cxx_qt_lib::QModelIndex;
        include!("cxx-qt-lib/qvariant.h");
        type QVariant = cxx_qt_lib::QVariant;
        include!("cxx-qt-lib/qstring.h");
        type QString = cxx_qt_lib::QString;
        include!("cxx-qt-lib/qhash.h");
        type QHash_i32_QByteArray = cxx_qt_lib::QHash<cxx_qt_lib::QHashPair_i32_QByteArray>;
        include!("cxx-qt-lib/qlist.h");
        type QList_i32 = cxx_qt_lib::QList<i32>;
    }

    unsafe extern "C++Qt" {
        include!(<QtCore/QAbstractItemModel>);
        #[qobject]
        type QAbstractItemModel;

        fn index(self: &QAbstractItemModel, row: i32, column: i32, parent: &QModelIndex) -> QModelIndex;
        #[cxx_name = "rowCount"]
        fn row_count(self: &QAbstractItemModel, parent: &QModelIndex) -> i32;
        fn data(self: &QAbstractItemModel, index: &QModelIndex, role: i32) -> QVariant;

        #[qsignal]
        #[cxx_name = "dataChanged"]
        fn data_changed(self: Pin<&mut QAbstractItemModel>, top_left: &QModelIndex, bottom_right: &QModelIndex, roles: &QList_i32);
    }

    extern "RustQt" {
        #[qobject]
        #[base = QSortFilterProxyModel]
        #[qml_element]
        #[qproperty(QString, query, READ = query_value, WRITE = set_query, NOTIFY = query_changed)]
        #[qproperty(bool, unread_only, cxx_name = "unreadOnly", READ = unread_only_value, WRITE = set_unread_only, NOTIFY = unread_only_changed)]
        #[qproperty(i32, count, READ = count_value, NOTIFY = count_changed)]
        type AgentFilterModel = super::AgentFilterModelRust;
    }

    unsafe extern "RustQt" {
        fn query_value(self: &AgentFilterModel) -> QString;
        fn set_query(self: Pin<&mut AgentFilterModel>, query: QString);
        fn unread_only_value(self: &AgentFilterModel) -> bool;
        #[cxx_name = "setUnreadOnly"]
        fn set_unread_only(self: Pin<&mut AgentFilterModel>, unread_only: bool);
        fn count_value(self: &AgentFilterModel) -> i32;

        #[qsignal]
        #[cxx_name = "queryChanged"]
        fn query_changed(self: Pin<&mut AgentFilterModel>);
        #[qsignal]
        #[cxx_name = "unreadOnlyChanged"]
        fn unread_only_changed(self: Pin<&mut AgentFilterModel>);
        #[qsignal]
        #[cxx_name = "countChanged"]
        fn count_changed(self: Pin<&mut AgentFilterModel>);

        #[qinvokable]
        #[cxx_name = "indexOfSession"]
        fn index_of_session(self: &AgentFilterModel, session: &QString) -> i32;
        #[qinvokable]
        #[cxx_name = "toggleDoneHelpers"]
        fn toggle_done_helpers(self: Pin<&mut AgentFilterModel>, parent_agent_id: &QString);
        /// Expands whatever hides `session`, so selecting a finished helper
        /// (from the switcher or a notification) can still highlight its row.
        #[qinvokable]
        #[cxx_name = "revealSession"]
        fn reveal_session(self: Pin<&mut AgentFilterModel>, session: &QString);

        #[inherit]
        #[cxx_name = "sourceModel"]
        fn source_model(self: &AgentFilterModel) -> *mut QAbstractItemModel;
        #[inherit]
        #[cxx_name = "setSourceModel"]
        unsafe fn base_set_source_model(self: Pin<&mut AgentFilterModel>, source: *mut QAbstractItemModel);
        #[inherit]
        #[cxx_name = "invalidateRowsFilter"]
        fn invalidate_rows_filter(self: Pin<&mut AgentFilterModel>);
        #[inherit]
        fn sort(self: Pin<&mut AgentFilterModel>, column: i32, order: SortOrder);
        #[inherit]
        #[cxx_name = "rowCount"]
        fn base_row_count(self: &AgentFilterModel, parent: &QModelIndex) -> i32;
        #[inherit]
        fn index(self: &AgentFilterModel, row: i32, column: i32, parent: &QModelIndex) -> QModelIndex;
        #[inherit]
        #[cxx_name = "data"]
        fn base_data(self: &AgentFilterModel, index: &QModelIndex, role: i32) -> QVariant;
        #[inherit]
        #[cxx_name = "roleNames"]
        fn base_role_names(self: &AgentFilterModel) -> QHash_i32_QByteArray;
        #[inherit]
        #[qsignal]
        #[cxx_name = "dataChanged"]
        fn data_changed(self: Pin<&mut AgentFilterModel>, top_left: &QModelIndex, bottom_right: &QModelIndex, roles: &QList_i32);

        #[cxx_override]
        #[cxx_name = "setSourceModel"]
        unsafe fn set_source_model(self: Pin<&mut AgentFilterModel>, source: *mut QAbstractItemModel);
        #[cxx_override]
        #[cxx_name = "filterAcceptsRow"]
        fn filter_accepts_row(self: &AgentFilterModel, source_row: i32, source_parent: &QModelIndex) -> bool;
        #[cxx_override]
        #[cxx_name = "lessThan"]
        fn less_than(self: &AgentFilterModel, left: &QModelIndex, right: &QModelIndex) -> bool;
        #[cxx_override]
        fn data(self: &AgentFilterModel, index: &QModelIndex, role: i32) -> QVariant;
        #[cxx_override]
        #[cxx_name = "roleNames"]
        fn role_names(self: &AgentFilterModel) -> QHash_i32_QByteArray;
    }

    impl cxx_qt::Initialize for AgentFilterModel {}
}

const USER_ROLE: i32 = 0x0100;
pub const TREE_DEPTH_ROLE: i32 = USER_ROLE + 500;
pub const DONE_HELPERS_ROLE: i32 = USER_ROLE + 501;

/// A back-pointer for source-signal handlers. Every handler is owned by a
/// guard in this object, so it is disconnected before the object goes away.
struct FilterPtr(*mut qobject::AgentFilterModel);
unsafe impl Send for FilterPtr {}

impl FilterPtr {
    /// A method, so closures capture the Send wrapper rather than the field.
    fn get(&self) -> *mut qobject::AgentFilterModel {
        self.0
    }
}

#[derive(Default)]
pub struct AgentFilterModelRust {
    sidebar: Sidebar,
    source_connections: Vec<QMetaObjectConnectionGuard>,
    /// Qt's own row signals are private (QPrivateSignal) and cannot be
    /// connected from Rust, so `count` is re-checked after every change.
    last_count: i32,
}

fn string_role(model: &qobject::QAbstractItemModel, index: &QModelIndex, role: SourceRole) -> String {
    model.data(index, source_role(role)).value::<QString>().map(|s| s.to_string()).unwrap_or_default()
}

fn bool_role(model: &qobject::QAbstractItemModel, index: &QModelIndex, role: SourceRole) -> bool {
    model.data(index, source_role(role)).value::<bool>().unwrap_or(false)
}

fn tree_changing(roles: &QList<i32>) -> bool {
    let watched = [SourceRole::AgentId, SourceRole::Session, SourceRole::AgentRole,
                   SourceRole::ParentAgentId, SourceRole::HelperState].map(source_role);
    roles.is_empty() || roles.iter().any(|role| watched.contains(role))
}

impl cxx_qt::Initialize for qobject::AgentFilterModel {
    fn initialize(self: Pin<&mut Self>) {
        self.sort(0, qobject::SortOrder::AscendingOrder);
    }
}

impl qobject::AgentFilterModel {
    fn query_value(&self) -> QString {
        QString::from(self.sidebar.query.as_str())
    }

    fn unread_only_value(&self) -> bool {
        self.sidebar.unread_only
    }

    fn count_value(&self) -> i32 {
        self.base_row_count(&QModelIndex::default())
    }

    fn sync_count(mut self: Pin<&mut Self>) {
        let count = self.count_value();
        if count != self.last_count {
            self.as_mut().rust_mut().last_count = count;
            self.count_changed();
        }
    }

    fn source(&self) -> Option<&qobject::QAbstractItemModel> {
        // SAFETY: Qt clears the proxy's source when the source is destroyed.
        unsafe { self.source_model().as_ref() }
    }

    fn set_query(mut self: Pin<&mut Self>, query: QString) {
        let query = query.to_string();
        if self.sidebar.query == query {
            return;
        }
        let before = self.sidebar.tree.clone();
        self.as_mut().rust_mut().sidebar.query = query;
        self.as_mut().rebuild_tree();
        self.as_mut().apply_tree(before, true);
        self.as_mut().query_changed();
        self.sync_count();
    }

    fn set_unread_only(mut self: Pin<&mut Self>, unread_only: bool) {
        if self.sidebar.unread_only == unread_only {
            return;
        }
        let before = self.sidebar.tree.clone();
        self.as_mut().rust_mut().sidebar.unread_only = unread_only;
        self.as_mut().rebuild_tree();
        self.as_mut().apply_tree(before, true);
        self.as_mut().unread_only_changed();
        self.sync_count();
    }

    fn toggle_done_helpers(mut self: Pin<&mut Self>, parent_agent_id: &QString) {
        let parent = parent_agent_id.to_string();
        if self.as_mut().rust_mut().sidebar.toggle_done_helpers(&parent) {
            self.refresh_tree();
        }
    }

    fn reveal_session(mut self: Pin<&mut Self>, session: &QString) {
        let session = session.to_string();
        if self.as_mut().rust_mut().sidebar.reveal(&session) {
            self.refresh_tree();
        }
    }

    fn index_of_session(&self, session: &QString) -> i32 {
        let root = QModelIndex::default();
        let wanted = session.to_string();
        (0..self.base_row_count(&root))
            .find(|&row| {
                let value = self.base_data(&self.index(row, 0, &root), source_role(SourceRole::Session));
                value.value::<QString>().is_some_and(|s| s.to_string() == wanted)
            })
            .unwrap_or(-1)
    }

    unsafe fn set_source_model(mut self: Pin<&mut Self>, source: *mut qobject::QAbstractItemModel) {
        self.as_mut().rust_mut().source_connections.clear();
        unsafe { self.as_mut().base_set_source_model(source) };
        // Connected after the base class so its own bookkeeping has already
        // run; the tree is then recomputed and applied only if it changed.
        let address = source as usize;
        if let Some(source) = unsafe { source.as_mut() } {
            let mut source = unsafe { Pin::new_unchecked(source) };
            let this = unsafe { self.as_mut().get_unchecked_mut() } as *mut Self;
            let refresh = {
                let ptr = FilterPtr(this);
                move || {
                    // SAFETY: the guard owning this handler lives in the filter.
                    unsafe { Pin::new_unchecked(&mut *ptr.get()) }.refresh_tree();
                }
            };
            let changed = {
                let ptr = FilterPtr(this);
                move |_: Pin<&mut qobject::QAbstractItemModel>, _: &QModelIndex, _: &QModelIndex, roles: &QList<i32>| {
                    // SAFETY: as above.
                    let filter = unsafe { Pin::new_unchecked(&mut *ptr.get()) };
                    if tree_changing(roles) {
                        filter.refresh_tree();
                    } else {
                        // The base class may have filtered the row in or out.
                        filter.sync_count();
                    }
                }
            };
            let mut guards = vec![source.as_mut().on_data_changed(changed)];
            // Structural changes arrive through the Rust roster's public
            // signal; any other source only drives the tree via dataChanged.
            if let Some(guard) = super::agent_list_model::on_structure_changed(address, refresh) {
                guards.push(guard);
            }
            self.as_mut().rust_mut().source_connections = guards;
        }
        self.refresh_tree();
    }

    fn rebuild_tree(mut self: Pin<&mut Self>) {
        let rows: Vec<TreeInput> = match self.source() {
            None => Vec::new(),
            Some(source) => {
                let root = QModelIndex::default();
                (0..source.row_count(&root))
                    .map(|row| {
                        let index = source.index(row, 0, &root);
                        TreeInput {
                            session: string_role(source, &index, SourceRole::Session),
                            agent_id: string_role(source, &index, SourceRole::AgentId),
                            agent_role: string_role(source, &index, SourceRole::AgentRole),
                            parent_agent_id: string_role(source, &index, SourceRole::ParentAgentId),
                            helper_state: string_role(source, &index, SourceRole::HelperState),
                        }
                    })
                    .collect()
            }
        };
        self.as_mut().rust_mut().sidebar.rebuild(&rows);
    }

    fn refresh_tree(mut self: Pin<&mut Self>) {
        let before = self.sidebar.tree.clone();
        self.as_mut().rebuild_tree();
        let filter_changed = before.hidden != self.sidebar.tree.hidden;
        self.as_mut().apply_tree(before, filter_changed);
        self.sync_count();
    }

    /// Apply a rebuilt tree without a reset: filtering inserts and removes
    /// only the rows that changed, and a re-sort is a layout change that
    /// keeps the rows' delegates.
    fn apply_tree(mut self: Pin<&mut Self>, before: clarp_core::sidebar::Tree, filter_changed: bool) {
        if filter_changed {
            self.as_mut().invalidate_rows_filter();
        }
        if before == self.sidebar.tree {
            return;
        }
        if before.position != self.sidebar.tree.position {
            // sort() skips an unchanged column and order; sorting away and
            // back makes it use the new positions.
            self.as_mut().sort(-1, qobject::SortOrder::AscendingOrder);
            self.as_mut().sort(0, qobject::SortOrder::AscendingOrder);
        }
        let root = QModelIndex::default();
        let rows = self.base_row_count(&root);
        if rows > 0 {
            let top = self.index(0, 0, &root);
            let bottom = self.index(rows - 1, 0, &root);
            let mut roles = QList::<i32>::default();
            roles.append(TREE_DEPTH_ROLE);
            roles.append(DONE_HELPERS_ROLE);
            self.as_mut().data_changed(&top, &bottom, &roles);
        }
    }

    fn filter_accepts_row(&self, source_row: i32, source_parent: &QModelIndex) -> bool {
        let Some(source) = self.source() else { return false };
        let index = source.index(source_row, 0, source_parent);
        let row = FilterInput {
            session: string_role(source, &index, SourceRole::Session),
            unread: bool_role(source, &index, SourceRole::Unread),
            name: string_role(source, &index, SourceRole::Name),
            backend: string_role(source, &index, SourceRole::Backend),
            last_message: string_role(source, &index, SourceRole::LastMessage),
            working_directory: string_role(source, &index, SourceRole::WorkingDirectory),
        };
        self.sidebar.accepts(&row)
    }

    fn less_than(&self, left: &QModelIndex, right: &QModelIndex) -> bool {
        let Some(source) = self.source() else { return left.row() < right.row() };
        let position = |index: &QModelIndex| {
            let session = string_role(source, index, SourceRole::Session);
            self.sidebar.position(&session, usize::try_from(index.row()).unwrap_or(0))
        };
        (position(left), left.row()) < (position(right), right.row())
    }

    fn data(&self, index: &QModelIndex, role: i32) -> QVariant {
        if role != TREE_DEPTH_ROLE && role != DONE_HELPERS_ROLE {
            return self.base_data(index, role);
        }
        let session = self
            .base_data(index, source_role(SourceRole::Session))
            .value::<QString>()
            .map(|s| s.to_string())
            .unwrap_or_default();
        if role == TREE_DEPTH_ROLE {
            return QVariant::from(&(self.sidebar.depth(&session) as i32));
        }
        let mut lines = QJsonArray::default();
        for line in self.sidebar.footers(&session) {
            let mut object = QJsonObject::default();
            object.insert(&QString::from("parentAgentId"), &QJsonValue::from(&QString::from(line.parent_agent_id.as_str())));
            object.insert(&QString::from("count"), &QJsonValue::from(line.count as i64));
            object.insert(&QString::from("expanded"), &QJsonValue::from(line.expanded));
            object.insert(&QString::from("depth"), &QJsonValue::from(line.depth as i64));
            lines.append(&QJsonValue::from(&object));
        }
        QVariant::from(&lines)
    }

    fn role_names(&self) -> QHash<QHashPair_i32_QByteArray> {
        let mut names = self.base_role_names();
        names.insert(TREE_DEPTH_ROLE, QByteArray::from("treeDepth"));
        names.insert(DONE_HELPERS_ROLE, QByteArray::from("doneHelpers"));
        names
    }
}
