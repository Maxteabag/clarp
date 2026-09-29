//! ConversationPresentationModel: the transcript as shown. Recomputes
//! `clarp_core::presentation::present` whenever its source ConversationModel
//! changes (once per mutation, so once per log batch) and applies the row
//! diff, so unchanged rows keep their delegates and nothing ever resets.

use std::pin::Pin;

use clarp_core::conversation::Role as Base;
use clarp_core::presentation::{Op, Presentation, PresentedRow, Role, Settings, diff, leading_day_label, present};
use clarp_core::time_format::day_separator;
use cxx_qt::{CxxQtType, QMetaObjectConnectionGuard};
use cxx_qt_lib::{QByteArray, QHash, QHashPair_i32_QByteArray, QModelIndex, QString, QStringList, QVariant};

use super::conversation_model::{self, cell_value};
use crate::qjson::to_qjson_array;

#[cxx_qt::bridge]
pub mod qobject {
    unsafe extern "C++" {
        include!(<QtCore/QAbstractListModel>);
        type QAbstractListModel;
        include!(<QtCore/QAbstractItemModel>);
        type QAbstractItemModel;
        include!(<QtCore/QObject>);
        type QObject;
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

    extern "RustQt" {
        #[qobject]
        #[base = QAbstractListModel]
        #[qml_element]
        #[qproperty(*mut QAbstractItemModel, source_model, cxx_name = "sourceModel", READ = source_model_value, WRITE = set_source_model, NOTIFY = source_model_changed)]
        #[qproperty(bool, show_when_ready, cxx_name = "showWhenReady", READ = show_when_ready_value, WRITE = set_show_when_ready, NOTIFY = show_when_ready_changed)]
        #[qproperty(QString, leading_day_label, cxx_name = "leadingDayLabel", READ = leading_day_label_value, NOTIFY = leading_day_label_changed)]
        #[qproperty(i32, count, READ = count_value, NOTIFY = count_changed)]
        #[qproperty(i32, activity_mode, cxx_name = "activityMode", READ = activity_mode_value, WRITE = set_activity_mode, NOTIFY = count_changed)]
        type ConversationPresentationModel = super::PresentationRust;
    }

    unsafe extern "RustQt" {
        fn source_model_value(self: &ConversationPresentationModel) -> *mut QAbstractItemModel;
        #[cxx_name = "setSourceModel"]
        unsafe fn set_source_model(self: Pin<&mut ConversationPresentationModel>, model: *mut QAbstractItemModel);
        fn show_when_ready_value(self: &ConversationPresentationModel) -> bool;
        #[cxx_name = "setShowWhenReady"]
        fn set_show_when_ready(self: Pin<&mut ConversationPresentationModel>, value: bool);
        fn leading_day_label_value(self: &ConversationPresentationModel) -> QString;
        fn count_value(self: &ConversationPresentationModel) -> i32;
        fn activity_mode_value(self: &ConversationPresentationModel) -> i32;
        #[cxx_name = "setActivityMode"]
        fn set_activity_mode(self: Pin<&mut ConversationPresentationModel>, mode: i32);

        #[qsignal]
        #[cxx_name = "sourceModelChanged"]
        fn source_model_changed(self: Pin<&mut ConversationPresentationModel>);
        #[qsignal]
        #[cxx_name = "showWhenReadyChanged"]
        fn show_when_ready_changed(self: Pin<&mut ConversationPresentationModel>);
        #[qsignal]
        #[cxx_name = "leadingDayLabelChanged"]
        fn leading_day_label_changed(self: Pin<&mut ConversationPresentationModel>);
        #[qsignal]
        #[cxx_name = "countChanged"]
        fn count_changed(self: Pin<&mut ConversationPresentationModel>);
        #[qsignal]
        #[cxx_name = "rowsAppended"]
        fn rows_appended(self: Pin<&mut ConversationPresentationModel>, from_current_user: bool);

        #[qinvokable]
        #[cxx_name = "indexOfMessage"]
        fn index_of_message(self: &ConversationPresentationModel, id: &QString) -> i32;
        #[qinvokable]
        #[cxx_name = "beginVisit"]
        fn begin_visit(self: Pin<&mut ConversationPresentationModel>);
        #[qinvokable]
        #[cxx_name = "toggleGroup"]
        fn toggle_group(self: Pin<&mut ConversationPresentationModel>, id: &QString);
        /// Explanations come from the ToolNarrator cache. Until the narrator
        /// is ported this records the request and shows no explanation runs.
        #[qinvokable]
        #[cxx_name = "updateExplanations"]
        unsafe fn update_explanations(self: Pin<&mut ConversationPresentationModel>, narrator: *mut QObject, session: &QString, directory: &QString, local_files: bool);

        #[inherit]
        #[cxx_name = "beginInsertRows"]
        unsafe fn begin_insert_rows(self: Pin<&mut ConversationPresentationModel>, parent: &QModelIndex, first: i32, last: i32);
        #[inherit]
        #[cxx_name = "endInsertRows"]
        unsafe fn end_insert_rows(self: Pin<&mut ConversationPresentationModel>);
        #[inherit]
        #[cxx_name = "beginRemoveRows"]
        unsafe fn begin_remove_rows(self: Pin<&mut ConversationPresentationModel>, parent: &QModelIndex, first: i32, last: i32);
        #[inherit]
        #[cxx_name = "endRemoveRows"]
        unsafe fn end_remove_rows(self: Pin<&mut ConversationPresentationModel>);
        #[inherit]
        #[cxx_name = "beginMoveRows"]
        unsafe fn begin_move_rows(self: Pin<&mut ConversationPresentationModel>, source_parent: &QModelIndex, source_first: i32, source_last: i32, destination_parent: &QModelIndex, destination_child: i32) -> bool;
        #[inherit]
        #[cxx_name = "endMoveRows"]
        unsafe fn end_move_rows(self: Pin<&mut ConversationPresentationModel>);
        #[inherit]
        #[cxx_name = "beginResetModel"]
        unsafe fn begin_reset_model(self: Pin<&mut ConversationPresentationModel>);
        #[inherit]
        #[cxx_name = "endResetModel"]
        unsafe fn end_reset_model(self: Pin<&mut ConversationPresentationModel>);
        #[inherit]
        #[qsignal]
        #[cxx_name = "dataChanged"]
        fn data_changed(self: Pin<&mut ConversationPresentationModel>, top_left: &QModelIndex, bottom_right: &QModelIndex, roles: &QList_i32);
        #[inherit]
        fn index(self: &ConversationPresentationModel, row: i32, column: i32, parent: &QModelIndex) -> QModelIndex;

        #[cxx_override]
        fn data(self: &ConversationPresentationModel, index: &QModelIndex, role: i32) -> QVariant;
        #[cxx_override]
        #[cxx_name = "roleNames"]
        fn role_names(self: &ConversationPresentationModel) -> QHash_i32_QByteArray;
        #[cxx_override]
        #[cxx_name = "rowCount"]
        fn row_count(self: &ConversationPresentationModel, parent: &QModelIndex) -> i32;
    }
}

const USER_ROLE: i32 = 0x0100;
const GROUP_ROLES: [(Role, &str); 6] = [
    (Role::GroupIds, "groupIds"),
    (Role::GroupLabel, "groupLabel"),
    (Role::GroupExpanded, "groupExpanded"),
    (Role::ActivityInline, "activityInline"),
    (Role::ActivityLabel, "activityLabel"),
    (Role::ExplanationRepeat, "explanationRepeat"),
];

/// Conversation roles keep their ids; group roles start at UserRole + 100.
fn role_id(role: Role) -> i32 {
    match role {
        Role::Base(base) => conversation_model::role_id(base),
        other => USER_ROLE + 100 + GROUP_ROLES.iter().position(|(r, _)| *r == other).expect("listed") as i32,
    }
}

struct SelfPtr(*mut qobject::ConversationPresentationModel);
unsafe impl Send for SelfPtr {}

impl SelfPtr {
    fn get(&self) -> *mut qobject::ConversationPresentationModel {
        self.0
    }
}

pub struct PresentationRust {
    source: *mut qobject::QAbstractItemModel,
    settings: Settings,
    presentation: Presentation,
    /// What views see; only ever changed between begin/end notifications.
    rows: Vec<PresentedRow>,
    leading_day: String,
    source_connection: Option<QMetaObjectConnectionGuard>,
    /// The ToolNarrator and scope explanation runs read from (cache-only).
    narrator: usize,
    narrator_session: String,
}

impl Default for PresentationRust {
    fn default() -> Self {
        Self {
            source: std::ptr::null_mut(),
            settings: Settings::default(),
            presentation: Presentation::default(),
            rows: Vec::new(),
            leading_day: String::new(),
            source_connection: None,
            narrator: 0,
            narrator_session: String::new(),
        }
    }
}

fn day_label(message: &clarp_core::protocol::Message) -> String {
    day_separator(&message.timestamp, "", &chrono::Local::now())
}

impl qobject::ConversationPresentationModel {
    fn source_model_value(&self) -> *mut qobject::QAbstractItemModel {
        self.source
    }
    fn show_when_ready_value(&self) -> bool {
        self.settings.show_when_ready
    }
    fn leading_day_label_value(&self) -> QString {
        QString::from(self.leading_day.as_str())
    }
    fn count_value(&self) -> i32 {
        self.rows.len() as i32
    }
    fn activity_mode_value(&self) -> i32 {
        self.settings.activity_mode
    }

    unsafe fn set_source_model(mut self: Pin<&mut Self>, model: *mut qobject::QAbstractItemModel) {
        self.as_mut().rust_mut().source_connection = None;
        self.as_mut().rust_mut().source = model;
        let address = model as usize;
        let this = SelfPtr(unsafe { self.as_mut().get_unchecked_mut() } as *mut Self);
        let guard = conversation_model::on_changed(address, move || {
            // SAFETY: the guard owning this handler lives in the model.
            unsafe { Pin::new_unchecked(&mut *this.get()) }.refresh();
        });
        match guard {
            Some(guard) => self.as_mut().rust_mut().source_connection = Some(guard),
            None if !model.is_null() => {
                eprintln!("ConversationPresentationModel: source is not a Rust ConversationModel; showing nothing");
            }
            None => {}
        }
        self.as_mut().rust_mut().settings.begin_visit();
        self.as_mut().refresh();
        self.source_model_changed();
    }

    fn set_show_when_ready(mut self: Pin<&mut Self>, value: bool) {
        if self.settings.show_when_ready == value {
            return;
        }
        self.as_mut().rust_mut().settings.show_when_ready = value;
        self.as_mut().refresh();
        self.show_when_ready_changed();
    }

    fn set_activity_mode(mut self: Pin<&mut Self>, mode: i32) {
        if self.settings.activity_mode == mode {
            return;
        }
        self.as_mut().rust_mut().settings.activity_mode = mode;
        self.as_mut().refresh();
        self.count_changed();
    }

    fn begin_visit(mut self: Pin<&mut Self>) {
        self.as_mut().rust_mut().settings.begin_visit();
        self.refresh();
    }

    fn toggle_group(mut self: Pin<&mut Self>, id: &QString) {
        self.as_mut().rust_mut().settings.toggle_group(&id.to_string());
        self.refresh();
    }

    /// Explanation runs read the narrator's cache; a disabled or failed
    /// narrator shows developer mode (every original row).
    unsafe fn update_explanations(mut self: Pin<&mut Self>, narrator: *mut qobject::QObject, session: &QString, _directory: &QString, _local_files: bool) {
        self.as_mut().rust_mut().narrator = narrator as usize;
        self.as_mut().rust_mut().narrator_session = session.to_string();
        self.refresh();
    }

    fn index_of_message(&self, id: &QString) -> i32 {
        let Some(source) = (unsafe { conversation_model::live(self.source as usize) }) else { return -1 };
        self.presentation.index_of_message(source.rows(), &id.to_string()).map_or(-1, |row| row as i32)
    }

    /// Recompute from the source and apply only what changed.
    fn refresh(mut self: Pin<&mut Self>) {
        let messages = match unsafe { conversation_model::live(self.source as usize) } {
            Some(source) => source.rows().to_vec(),
            None => Vec::new(),
        };
        let narrator = unsafe { super::tool_narrator::live(self.narrator) }
            .filter(|n| n.enabled_value_pub() && !n.unavailable_value_pub());
        let session = self.narrator_session.clone();
        let lookup = move |activity: &serde_json::Map<String, serde_json::Value>| -> String {
            let mut activity = activity.clone();
            activity.insert("_session".into(), serde_json::Value::from(session.as_str()));
            narrator.map(|n| n.explain(&activity)).unwrap_or_default()
        };
        let presentation = {
            let settings = &mut self.as_mut().rust_mut().settings;
            present(&messages, settings, narrator.is_some().then_some(&lookup as &dyn Fn(&_) -> String))
        };
        let ops = diff(&self.rows, &presentation.rows);
        self.as_mut().rust_mut().presentation = presentation;
        let before = self.rows.len();
        for op in ops {
            let appended = match &op {
                Op::Insert { at, rows } => Some((*at, rows.len())),
                _ => None,
            };
            self.as_mut().replay(op);
            if let Some((at, count)) = appended.filter(|(at, count)| at + count == self.rows.len()) {
                let from_user = count == 1 && self.rows[at].message.pending;
                self.as_mut().rows_appended(from_user);
            }
        }
        if self.rows.len() != before {
            self.as_mut().count_changed();
        }
        let leading = leading_day_label(&self.rows, day_label);
        if leading != self.leading_day {
            self.as_mut().rust_mut().leading_day = leading;
            self.leading_day_label_changed();
        }
    }

    fn replay(mut self: Pin<&mut Self>, op: Op) {
        crate::list_replay::replay_list_op!(self, op, role_id, ignore_signal);
    }

    fn ignore_signal(self: Pin<&mut Self>, _signal: ()) {}

    fn data(&self, index: &QModelIndex, role: i32) -> QVariant {
        let Some(row) = usize::try_from(index.row()).ok().and_then(|r| self.rows.get(r)) else {
            return QVariant::default();
        };
        if let Some((group, _)) = GROUP_ROLES.iter().find(|(r, _)| role_id(*r) == role) {
            return match group {
                Role::GroupIds => {
                    let mut ids = QStringList::default();
                    for id in &row.group_ids {
                        ids.append(QString::from(id.as_str()));
                    }
                    QVariant::from(&ids)
                }
                Role::GroupLabel => QVariant::from(&QString::from(row.group_label.as_str())),
                Role::GroupExpanded => QVariant::from(&row.group_expanded),
                Role::ActivityInline => QVariant::from(&row.activity_inline),
                Role::ActivityLabel => QVariant::from(&QString::from(row.activity_label.as_str())),
                _ => QVariant::from(&row.explanation_repeat),
            };
        }
        let Some(base) = conversation_model::role_for(role) else { return QVariant::default() };
        match base {
            Base::Body => QVariant::from(&QString::from(row.body.as_str())),
            Base::Activity => QVariant::from(&row.activity),
            Base::Tools => QVariant::from(&to_qjson_array(&row.tools)),
            Base::DisplayCells => QVariant::from(&to_qjson_array(&row.display_cells)),
            Base::ActivityCount => QVariant::from(&row.activity_count),
            other => cell_value(&row.message, other),
        }
    }

    fn role_names(&self) -> QHash<QHashPair_i32_QByteArray> {
        let mut names = conversation_model::role_names_hash();
        for (role, name) in GROUP_ROLES {
            names.insert(role_id(role), QByteArray::from(name));
        }
        names
    }

    fn row_count(&self, parent: &QModelIndex) -> i32 {
        if parent.is_valid() { 0 } else { self.rows.len() as i32 }
    }
}
