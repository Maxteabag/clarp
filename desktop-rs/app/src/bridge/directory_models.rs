//! Contact and voice list models: replaced wholesale on each response, so a
//! reset is the whole protocol. Role names match the C++ models.

use std::collections::HashSet;
use std::pin::Pin;

use clarp_core::directory::{Contact, Voice, contacts_from_snapshot, voices_from_response};
use cxx_qt::CxxQtType;
use cxx_qt_lib::{QByteArray, QHash, QHashPair_i32_QByteArray, QModelIndex, QString, QVariant};

#[cxx_qt::bridge]
pub mod qobject {
    unsafe extern "C++" {
        include!(<QtCore/QAbstractListModel>);
        type QAbstractListModel;
        include!("cxx-qt-lib/qmodelindex.h");
        type QModelIndex = cxx_qt_lib::QModelIndex;
        include!("cxx-qt-lib/qvariant.h");
        type QVariant = cxx_qt_lib::QVariant;
        include!("cxx-qt-lib/qstring.h");
        type QString = cxx_qt_lib::QString;
        include!("cxx-qt-lib/qhash.h");
        type QHash_i32_QByteArray = cxx_qt_lib::QHash<cxx_qt_lib::QHashPair_i32_QByteArray>;
    }

    extern "RustQt" {
        #[qobject]
        #[base = QAbstractListModel]
        #[qml_element]
        #[qproperty(i32, count, READ = count_value, NOTIFY = count_changed)]
        type ContactListModel = super::ContactListModelRust;

        #[qobject]
        #[base = QAbstractListModel]
        #[qml_element]
        #[qproperty(i32, count, READ = count_value, NOTIFY = count_changed)]
        type VoiceListModel = super::VoiceListModelRust;
    }

    unsafe extern "RustQt" {
        fn count_value(self: &ContactListModel) -> i32;
        #[qsignal]
        #[cxx_name = "countChanged"]
        fn count_changed(self: Pin<&mut ContactListModel>);
        /// Probe/controller entry: `activeNames` is a JSON array of names.
        #[qinvokable]
        #[cxx_name = "applySnapshotText"]
        fn apply_snapshot_text(self: Pin<&mut ContactListModel>, snapshot: &QString, active_names: &QString);
        #[inherit]
        #[cxx_name = "beginResetModel"]
        unsafe fn begin_reset_model(self: Pin<&mut ContactListModel>);
        #[inherit]
        #[cxx_name = "endResetModel"]
        unsafe fn end_reset_model(self: Pin<&mut ContactListModel>);
        #[cxx_override]
        fn data(self: &ContactListModel, index: &QModelIndex, role: i32) -> QVariant;
        #[cxx_override]
        #[cxx_name = "roleNames"]
        fn role_names(self: &ContactListModel) -> QHash_i32_QByteArray;
        #[cxx_override]
        #[cxx_name = "rowCount"]
        fn row_count(self: &ContactListModel, parent: &QModelIndex) -> i32;
    }

    unsafe extern "RustQt" {
        fn count_value(self: &VoiceListModel) -> i32;
        #[qsignal]
        #[cxx_name = "countChanged"]
        fn count_changed(self: Pin<&mut VoiceListModel>);
        #[qinvokable]
        #[cxx_name = "applyResponseText"]
        fn apply_response_text(self: Pin<&mut VoiceListModel>, response: &QString, current_voice_id: &QString);
        #[inherit]
        #[cxx_name = "beginResetModel"]
        unsafe fn begin_reset_model(self: Pin<&mut VoiceListModel>);
        #[inherit]
        #[cxx_name = "endResetModel"]
        unsafe fn end_reset_model(self: Pin<&mut VoiceListModel>);
        #[cxx_override]
        fn data(self: &VoiceListModel, index: &QModelIndex, role: i32) -> QVariant;
        #[cxx_override]
        #[cxx_name = "roleNames"]
        fn role_names(self: &VoiceListModel) -> QHash_i32_QByteArray;
        #[cxx_override]
        #[cxx_name = "rowCount"]
        fn row_count(self: &VoiceListModel, parent: &QModelIndex) -> i32;
    }

    #[namespace = "rust::cxxqtlib1"]
    unsafe extern "C++" {
        include!("cxx-qt-lib/common.h");
        #[rust_name = "new_contact_list_model"]
        fn make_unique() -> UniquePtr<ContactListModel>;
        #[rust_name = "new_voice_list_model"]
        fn make_unique() -> UniquePtr<VoiceListModel>;
    }
}

const USER_ROLE: i32 = 0x0100;

fn names(roles: &[&str]) -> QHash<QHashPair_i32_QByteArray> {
    let mut hash = QHash::<QHashPair_i32_QByteArray>::default();
    for (offset, name) in roles.iter().enumerate() {
        hash.insert(USER_ROLE + 1 + offset as i32, QByteArray::from(*name));
    }
    hash
}

fn text(value: &str) -> QVariant {
    QVariant::from(&QString::from(value))
}

fn parse(text: &QString, what: &str) -> Option<serde_json::Value> {
    serde_json::from_str(&text.to_string())
        .map_err(|error| eprintln!("directory model: ignoring unparseable {what}: {error}"))
        .ok()
}

const CONTACT_ROLES: [&str; 6] = ["contactId", "name", "description", "builtin", "avatarSymbol", "avatarUrl"];
const VOICE_ROLES: [&str; 4] = ["voiceId", "label", "takenBy", "current"];

#[derive(Default)]
pub struct ContactListModelRust {
    rows: Vec<Contact>,
}

#[derive(Default)]
pub struct VoiceListModelRust {
    rows: Vec<Voice>,
}

impl qobject::ContactListModel {
    fn count_value(&self) -> i32 {
        self.rows.len() as i32
    }

    pub fn replace(mut self: Pin<&mut Self>, rows: Vec<Contact>) {
        unsafe { self.as_mut().begin_reset_model() };
        self.as_mut().rust_mut().rows = rows;
        unsafe { self.as_mut().end_reset_model() };
        self.count_changed();
    }

    fn apply_snapshot_text(self: Pin<&mut Self>, snapshot: &QString, active_names: &QString) {
        let Some(serde_json::Value::Object(snapshot)) = parse(snapshot, "snapshot") else { return };
        let active: HashSet<String> = parse(active_names, "active names")
            .and_then(|v| v.as_array().cloned())
            .unwrap_or_default()
            .iter()
            .filter_map(|v| v.as_str().map(str::to_lowercase))
            .collect();
        self.replace(contacts_from_snapshot(&snapshot, &active));
    }

    fn data(&self, index: &QModelIndex, role: i32) -> QVariant {
        let Some(contact) = usize::try_from(index.row()).ok().and_then(|row| self.rows.get(row)) else {
            return QVariant::default();
        };
        match role - USER_ROLE - 1 {
            0 => text(&contact.id),
            1 => text(&contact.name),
            2 => text(&contact.description),
            3 => QVariant::from(&contact.builtin),
            4 => text(&contact.avatar_symbol),
            5 => text(&contact.avatar_url),
            _ => QVariant::default(),
        }
    }

    fn role_names(&self) -> QHash<QHashPair_i32_QByteArray> {
        names(&CONTACT_ROLES)
    }

    fn row_count(&self, parent: &QModelIndex) -> i32 {
        if parent.is_valid() { 0 } else { self.rows.len() as i32 }
    }
}

impl qobject::VoiceListModel {
    fn count_value(&self) -> i32 {
        self.rows.len() as i32
    }

    pub fn replace(mut self: Pin<&mut Self>, rows: Vec<Voice>) {
        unsafe { self.as_mut().begin_reset_model() };
        self.as_mut().rust_mut().rows = rows;
        unsafe { self.as_mut().end_reset_model() };
        self.count_changed();
    }

    fn apply_response_text(self: Pin<&mut Self>, response: &QString, current_voice_id: &QString) {
        let Some(serde_json::Value::Object(response)) = parse(response, "voices response") else { return };
        self.replace(voices_from_response(&response, &current_voice_id.to_string()));
    }

    fn data(&self, index: &QModelIndex, role: i32) -> QVariant {
        let Some(voice) = usize::try_from(index.row()).ok().and_then(|row| self.rows.get(row)) else {
            return QVariant::default();
        };
        match role - USER_ROLE - 1 {
            0 => text(&voice.id),
            1 => text(&voice.label),
            2 => text(&voice.taken_by),
            3 => QVariant::from(&voice.current),
            _ => QVariant::default(),
        }
    }

    fn role_names(&self) -> QHash<QHashPair_i32_QByteArray> {
        names(&VOICE_ROLES)
    }

    fn row_count(&self, parent: &QModelIndex) -> i32 {
        if parent.is_valid() { 0 } else { self.rows.len() as i32 }
    }
}
