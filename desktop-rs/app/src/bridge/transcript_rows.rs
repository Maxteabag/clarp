//! TranscriptRows: the rows the transcript view shows (C++ `TranscriptRows`).
//! A list model over any source model; `clarp_core::transcript_rows` decides
//! how messages split. Parts carry their message's roles plus `partIndex`,
//! `partCount`, `fullBody` and `rowKey`, and a split message's `body` is the
//! part's own markdown.

use std::pin::Pin;

use clarp_core::transcript_rows::{Change, Part, Source, TranscriptRows};
use cxx_qt::{CxxQtType, QMetaObjectConnectionGuard};
use cxx_qt_lib::{QByteArray, QHash, QHashPair_i32_QByteArray, QList, QModelIndex, QString, QVariant};

use super::quick::{self, ffi as q};

const USER_ROLE: i32 = 0x0100;
const PART_INDEX_ROLE: i32 = USER_ROLE + 900;
const PART_COUNT_ROLE: i32 = USER_ROLE + 901;
const FULL_BODY_ROLE: i32 = USER_ROLE + 902;
const ROW_KEY_ROLE: i32 = USER_ROLE + 903;

#[cxx_qt::bridge]
pub mod qobject {
    unsafe extern "C++" {
        include!(<QtCore/QAbstractListModel>);
        type QAbstractListModel;
        include!(<QtCore/QAbstractItemModel>);
        type QAbstractItemModel;
        include!("cxx-qt-lib/qmodelindex.h");
        type QModelIndex = cxx_qt_lib::QModelIndex;
        include!("cxx-qt-lib/qvariant.h");
        type QVariant = cxx_qt_lib::QVariant;
        include!("cxx-qt-lib/qhash.h");
        type QHash_i32_QByteArray = cxx_qt_lib::QHash<cxx_qt_lib::QHashPair_i32_QByteArray>;
        include!("cxx-qt-lib/qlist.h");
        type QList_i32 = cxx_qt_lib::QList<i32>;
    }

    extern "RustQt" {
        #[qobject]
        #[qml_element]
        #[base = QAbstractListModel]
        #[qproperty(*mut QAbstractItemModel, source_model, cxx_name = "sourceModel", READ = source_model_value, WRITE = set_source_model, NOTIFY = source_model_changed)]
        #[qproperty(i32, count, READ = count_value, NOTIFY = count_changed)]
        type TranscriptRows = super::TranscriptRowsRust;
    }

    unsafe extern "RustQt" {
        fn source_model_value(self: &TranscriptRows) -> *mut QAbstractItemModel;
        #[cxx_name = "setSourceModel"]
        unsafe fn set_source_model(self: Pin<&mut TranscriptRows>, model: *mut QAbstractItemModel);
        fn count_value(self: &TranscriptRows) -> i32;
        #[qsignal]
        #[cxx_name = "sourceModelChanged"]
        fn source_model_changed(self: Pin<&mut TranscriptRows>);
        #[qsignal]
        #[cxx_name = "countChanged"]
        fn count_changed(self: Pin<&mut TranscriptRows>);

        /// The first view row of a source row, for jumping to a message.
        #[qinvokable]
        #[cxx_name = "rowForSource"]
        fn row_for_source(self: &TranscriptRows, source_row: i32) -> i32;
        #[qinvokable]
        #[cxx_name = "sourceRow"]
        fn source_row(self: &TranscriptRows, row: i32) -> i32;

        // Forwarding targets for the source's (private) signals.
        #[qsignal]
        #[cxx_name = "sourceRowsInserted"]
        fn source_rows_inserted(self: Pin<&mut TranscriptRows>, parent: &QModelIndex, first: i32, last: i32);
        #[qsignal]
        #[cxx_name = "sourceRowsRemoved"]
        fn source_rows_removed(self: Pin<&mut TranscriptRows>, parent: &QModelIndex, first: i32, last: i32);
        #[qsignal]
        #[cxx_name = "sourceDataChanged"]
        fn source_data_changed(self: Pin<&mut TranscriptRows>, top_left: &QModelIndex, bottom_right: &QModelIndex, roles: &QList_i32);
        #[qsignal]
        #[cxx_name = "sourceRebuilt"]
        fn source_rebuilt(self: Pin<&mut TranscriptRows>);

        #[inherit]
        #[cxx_name = "beginInsertRows"]
        unsafe fn begin_insert_rows(self: Pin<&mut TranscriptRows>, parent: &QModelIndex, first: i32, last: i32);
        #[inherit]
        #[cxx_name = "endInsertRows"]
        unsafe fn end_insert_rows(self: Pin<&mut TranscriptRows>);
        #[inherit]
        #[cxx_name = "beginRemoveRows"]
        unsafe fn begin_remove_rows(self: Pin<&mut TranscriptRows>, parent: &QModelIndex, first: i32, last: i32);
        #[inherit]
        #[cxx_name = "endRemoveRows"]
        unsafe fn end_remove_rows(self: Pin<&mut TranscriptRows>);
        #[inherit]
        #[cxx_name = "beginResetModel"]
        unsafe fn begin_reset_model(self: Pin<&mut TranscriptRows>);
        #[inherit]
        #[cxx_name = "endResetModel"]
        unsafe fn end_reset_model(self: Pin<&mut TranscriptRows>);
        #[inherit]
        #[qsignal]
        #[cxx_name = "dataChanged"]
        fn data_changed(self: Pin<&mut TranscriptRows>, top_left: &QModelIndex, bottom_right: &QModelIndex, roles: &QList_i32);
        #[inherit]
        fn index(self: &TranscriptRows, row: i32, column: i32, parent: &QModelIndex) -> QModelIndex;

        #[cxx_override]
        fn data(self: &TranscriptRows, index: &QModelIndex, role: i32) -> QVariant;
        #[cxx_override]
        #[cxx_name = "roleNames"]
        fn role_names(self: &TranscriptRows) -> QHash_i32_QByteArray;
        #[cxx_override]
        #[cxx_name = "rowCount"]
        fn row_count(self: &TranscriptRows, parent: &QModelIndex) -> i32;
    }

    impl cxx_qt::Initialize for TranscriptRows {}
}

pub struct TranscriptRowsRust {
    source: *mut qobject::QAbstractItemModel,
    guards: Vec<QMetaObjectConnectionGuard>,
    core: TranscriptRows,
    /// What views see; only changed between begin/end notifications.
    rows: Vec<Part>,
    role_names: Vec<(i32, String)>,
    body_role: i32,
    kind_role: i32,
    activity_role: i32,
    author_role: i32,
    message_id_role: i32,
}

impl Default for TranscriptRowsRust {
    fn default() -> Self {
        Self {
            source: std::ptr::null_mut(),
            guards: Vec::new(),
            core: TranscriptRows::default(),
            rows: Vec::new(),
            role_names: Vec::new(),
            body_role: -1,
            kind_role: -1,
            activity_role: -1,
            author_role: -1,
            message_id_role: -1,
        }
    }
}

fn source_model(model: *mut qobject::QAbstractItemModel) -> Option<&'static q::QAbstractItemModel> {
    // SAFETY: the source outlives its assignment; setSourceModel(null) clears it.
    unsafe { model.cast::<q::QAbstractItemModel>().as_ref() }
}

impl cxx_qt::Initialize for qobject::TranscriptRows {
    fn initialize(mut self: Pin<&mut Self>) {
        let guards = [
            self.as_mut().on_source_rows_inserted(|this, parent, first, last| this.rows_inserted(parent, first, last)),
            self.as_mut().on_source_rows_removed(|this, parent, first, last| this.rows_removed(parent, first, last)),
            self.as_mut().on_source_data_changed(|this, top_left, bottom_right, roles| this.source_changed(top_left, bottom_right, roles)),
            self.as_mut().on_source_rebuilt(|this| this.rebuild()),
        ];
        for guard in guards {
            guard.release();
        }
    }
}

impl qobject::TranscriptRows {
    fn source_model_value(&self) -> *mut qobject::QAbstractItemModel {
        self.source
    }

    fn count_value(&self) -> i32 {
        self.rows.len() as i32
    }

    unsafe fn set_source_model(mut self: Pin<&mut Self>, model: *mut qobject::QAbstractItemModel) {
        if self.source == model {
            return;
        }
        self.as_mut().rust_mut().guards.clear();
        self.as_mut().rust_mut().source = model;
        if !model.is_null() {
            let (sender, receiver) = (model.cast::<q::QObject>().cast_const(), (&*self as *const Self).cast::<q::QObject>());
            let forwards = [
                (c"rowsInserted(QModelIndex,int,int)", c"sourceRowsInserted(QModelIndex,int,int)"),
                (c"rowsRemoved(QModelIndex,int,int)", c"sourceRowsRemoved(QModelIndex,int,int)"),
                (c"dataChanged(QModelIndex,QModelIndex,QList<int>)", c"sourceDataChanged(QModelIndex,QModelIndex,QList<int>)"),
                (c"modelReset()", c"sourceRebuilt()"),
                (c"layoutChanged()", c"sourceRebuilt()"),
                (c"rowsMoved(QModelIndex,int,int,QModelIndex,int)", c"sourceRebuilt()"),
            ];
            let guards: Vec<_> =
                forwards.iter().filter_map(|(from, to)| unsafe { quick::forward_signal(sender, from, receiver, to) }).collect();
            self.as_mut().rust_mut().guards = guards;
        }
        self.as_mut().rebuild();
        self.source_model_changed();
    }

    fn value(&self, source_row: usize, role: i32) -> QVariant {
        match source_model(self.source) {
            Some(model) if role >= 0 => model.model_data(&model.model_index(source_row as i32, 0, &QModelIndex::default()), role),
            _ => QVariant::default(),
        }
    }

    fn text(&self, source_row: usize, role: i32) -> String {
        self.value(source_row, role).value::<QString>().map(|s| s.to_string()).unwrap_or_default()
    }

    fn read(&self, source_row: usize) -> Source {
        Source {
            id: self.text(source_row, self.message_id_role),
            body: self.text(source_row, self.body_role),
            kind: self.text(source_row, self.kind_role),
            author: self.text(source_row, self.author_role),
            activity: self.value(source_row, self.activity_role).value::<bool>().unwrap_or(false),
        }
    }

    fn rebuild(mut self: Pin<&mut Self>) {
        unsafe { self.as_mut().begin_reset_model() };
        let (names, count) = match source_model(self.source) {
            Some(model) => {
                let names: Vec<(i32, String)> = model.model_role_names().iter().map(|(id, name)| (*id, name.to_string())).collect();
                (names, model.model_row_count(&QModelIndex::default()).max(0) as usize)
            }
            None => (Vec::new(), 0),
        };
        {
            let role = |name: &str| names.iter().find(|(_, n)| n == name).map_or(-1, |(id, _)| *id);
            let mut rust = self.as_mut().rust_mut();
            rust.body_role = role("body");
            rust.kind_role = role("messageKind");
            rust.activity_role = role("activity");
            rust.author_role = role("authorRole");
            rust.message_id_role = role("messageId");
        }
        let has_source = !self.source.is_null();
        let mut core = std::mem::take(&mut self.as_mut().rust_mut().core);
        core.rebuild(count, |row| self.read(row));
        let mut rust = self.as_mut().rust_mut();
        rust.rows = core.rows().to_vec();
        rust.core = core;
        rust.role_names = names;
        if has_source {
            for (id, name) in [(PART_INDEX_ROLE, "partIndex"), (PART_COUNT_ROLE, "partCount"), (FULL_BODY_ROLE, "fullBody"), (ROW_KEY_ROLE, "rowKey")] {
                rust.role_names.push((id, name.into()));
            }
        }
        unsafe { self.as_mut().end_reset_model() };
        self.count_changed();
    }

    /// Replays core changes onto the view rows, each between its notifications.
    fn apply(mut self: Pin<&mut Self>, changes: Vec<Change>, roles: &QList<i32>) {
        let root = QModelIndex::default();
        let mut counted = false;
        for change in changes {
            match change {
                Change::Reset => {}
                Change::Insert { at, count } if count > 0 => {
                    unsafe { self.as_mut().begin_insert_rows(&root, at as i32, (at + count - 1) as i32) };
                    let mut rust = self.as_mut().rust_mut();
                    let parts = rust.core.rows()[at..at + count].to_vec();
                    rust.rows.splice(at..at, parts);
                    unsafe { self.as_mut().end_insert_rows() };
                    counted = true;
                }
                Change::Remove { at, count } if count > 0 => {
                    unsafe { self.as_mut().begin_remove_rows(&root, at as i32, (at + count - 1) as i32) };
                    self.as_mut().rust_mut().rows.drain(at..at + count);
                    unsafe { self.as_mut().end_remove_rows() };
                    counted = true;
                }
                Change::Update { first, last, all_roles } => {
                    {
                        let mut rust = self.as_mut().rust_mut();
                        let parts = rust.core.rows()[first..=last].to_vec();
                        rust.rows.splice(first..=last, parts);
                    }
                    let (top, bottom) = (self.index(first as i32, 0, &root), self.index(last as i32, 0, &root));
                    let roles = if all_roles { QList::default() } else { roles.clone() };
                    self.as_mut().data_changed(&top, &bottom, &roles);
                }
                _ => {}
            }
        }
        // Inserts and removals also shift the source index of the rows after
        // them; that changes nothing a view sees, so it needs no signal.
        let mut rust = self.as_mut().rust_mut();
        debug_assert_eq!(rust.rows.len(), rust.core.count());
        rust.rows = rust.core.rows().to_vec();
        if counted {
            self.count_changed();
        }
    }

    fn rows_inserted(mut self: Pin<&mut Self>, parent: &QModelIndex, first: i32, last: i32) {
        if parent.is_valid() || first < 0 || last < first {
            return;
        }
        let mut core = std::mem::take(&mut self.as_mut().rust_mut().core);
        let changes = core.inserted(first as usize, last as usize, |row| self.read(row));
        self.as_mut().rust_mut().core = core;
        self.apply(changes, &QList::default());
    }

    fn rows_removed(mut self: Pin<&mut Self>, parent: &QModelIndex, first: i32, last: i32) {
        if parent.is_valid() || first < 0 || last < first {
            return;
        }
        let changes = self.as_mut().rust_mut().core.removed(first as usize, last as usize);
        let removed_parts = !changes.is_empty();
        self.as_mut().apply(changes, &QList::default());
        // Like the C++ model, a removal always announces the count.
        if !removed_parts {
            self.count_changed();
        }
    }

    fn source_changed(mut self: Pin<&mut Self>, top_left: &QModelIndex, bottom_right: &QModelIndex, roles: &QList<i32>) {
        if top_left.parent().is_valid() || top_left.row() < 0 {
            return;
        }
        let list: Vec<i32> = roles.iter().copied().collect();
        let split_may_change =
            list.is_empty() || list.contains(&self.body_role) || list.contains(&self.kind_role) || list.contains(&self.activity_role);
        let (first, last) = (top_left.row() as usize, bottom_right.row().max(top_left.row()) as usize);
        let mut core = std::mem::take(&mut self.as_mut().rust_mut().core);
        let changes = core.data_changed(first, last, |row| self.read(row), split_may_change, list.is_empty());
        self.as_mut().rust_mut().core = core;
        self.apply(changes, roles);
    }

    fn row_for_source(&self, source_row: i32) -> i32 {
        let row = usize::try_from(source_row).ok().map(|s| self.rows.partition_point(|r| r.source < s));
        match row {
            Some(row) if self.rows.get(row).is_some_and(|r| r.source as i32 == source_row) => row as i32,
            _ => -1,
        }
    }

    fn source_row(&self, row: i32) -> i32 {
        usize::try_from(row).ok().and_then(|r| self.rows.get(r)).map_or(-1, |r| r.source as i32)
    }

    fn data(&self, index: &QModelIndex, role: i32) -> QVariant {
        if self.source.is_null() {
            return QVariant::default();
        }
        let Some(row) = usize::try_from(index.row()).ok().and_then(|r| self.rows.get(r)) else { return QVariant::default() };
        match role {
            PART_INDEX_ROLE => QVariant::from(&(row.part as i32)),
            PART_COUNT_ROLE => QVariant::from(&(row.parts as i32)),
            FULL_BODY_ROLE => self.value(row.source, self.body_role),
            ROW_KEY_ROLE => QVariant::from(&QString::from(row.row_key(&self.text(row.source, self.message_id_role)).as_str())),
            role if role == self.body_role && row.parts > 1 => QVariant::from(&QString::from(row.body.as_str())),
            role => self.value(row.source, role),
        }
    }

    fn role_names(&self) -> QHash<QHashPair_i32_QByteArray> {
        let mut names = QHash::<QHashPair_i32_QByteArray>::default();
        for (id, name) in &self.role_names {
            names.insert(*id, QByteArray::from(name.as_str()));
        }
        names
    }

    fn row_count(&self, parent: &QModelIndex) -> i32 {
        if parent.is_valid() { 0 } else { self.rows.len() as i32 }
    }
}
