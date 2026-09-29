//! QAbstractListModel over `clarp_core::conversation::Conversation`. The core
//! records ordered ops; this adapter replays them onto its row mirror between
//! the matching begin/end calls. Role names match the C++ ConversationModel,
//! so the existing QML delegates bind unchanged.

use std::pin::Pin;

use clarp_core::conversation::{Conversation, LoadKind, Op, Role, Signal, presented_display_cells};
use clarp_core::protocol::Message;
use clarp_core::time_format::day_separator;
use cxx_qt::CxxQtType;
use cxx_qt_lib::{QByteArray, QHash, QHashPair_i32_QByteArray, QModelIndex, QString, QVariant};

use crate::qjson::to_qjson_array;

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
        include!("cxx-qt-lib/qlist.h");
        type QList_i32 = cxx_qt_lib::QList<i32>;
    }

    extern "RustQt" {
        #[qobject]
        #[base = QAbstractListModel]
        #[qml_element]
        #[qproperty(QString, session, READ = session_value, NOTIFY = session_changed)]
        #[qproperty(QString, conversation_id, cxx_name = "conversationId", READ = conversation_id_value, NOTIFY = conversation_id_changed)]
        #[qproperty(i64, latest_revision, cxx_name = "latestRevision", READ = latest_revision_value, NOTIFY = latest_revision_changed)]
        #[qproperty(bool, has_more, cxx_name = "hasMore", READ = has_more_value, NOTIFY = has_more_changed)]
        #[qproperty(bool, loading, READ = loading_value, NOTIFY = loading_changed)]
        #[qproperty(QString, error, READ = error_value, NOTIFY = error_changed)]
        #[qproperty(QString, voice_error, cxx_name = "voiceError", READ = voice_error_value, NOTIFY = voice_error_changed)]
        #[qproperty(i32, count, READ = count_value, NOTIFY = count_changed)]
        type ConversationModel = super::ConversationModelRust;
    }

    unsafe extern "RustQt" {
        fn session_value(self: &ConversationModel) -> QString;
        fn conversation_id_value(self: &ConversationModel) -> QString;
        fn latest_revision_value(self: &ConversationModel) -> i64;
        fn has_more_value(self: &ConversationModel) -> bool;
        fn loading_value(self: &ConversationModel) -> bool;
        fn error_value(self: &ConversationModel) -> QString;
        fn voice_error_value(self: &ConversationModel) -> QString;
        fn count_value(self: &ConversationModel) -> i32;

        #[qsignal]
        fn session_changed(self: Pin<&mut ConversationModel>);
        #[qsignal]
        #[cxx_name = "conversationIdChanged"]
        fn conversation_id_changed(self: Pin<&mut ConversationModel>);
        #[qsignal]
        #[cxx_name = "latestRevisionChanged"]
        fn latest_revision_changed(self: Pin<&mut ConversationModel>);
        #[qsignal]
        #[cxx_name = "hasMoreChanged"]
        fn has_more_changed(self: Pin<&mut ConversationModel>);
        #[qsignal]
        #[cxx_name = "loadingChanged"]
        fn loading_changed(self: Pin<&mut ConversationModel>);
        #[qsignal]
        #[cxx_name = "errorChanged"]
        fn error_changed(self: Pin<&mut ConversationModel>);
        #[qsignal]
        #[cxx_name = "voiceErrorChanged"]
        fn voice_error_changed(self: Pin<&mut ConversationModel>);
        #[qsignal]
        #[cxx_name = "countChanged"]
        fn count_changed(self: Pin<&mut ConversationModel>);
        #[qsignal]
        #[cxx_name = "rowsAppended"]
        fn rows_appended(self: Pin<&mut ConversationModel>, from_current_user: bool);
        #[qsignal]
        #[cxx_name = "rowsPrepended"]
        fn rows_prepended(self: Pin<&mut ConversationModel>);
        #[qsignal]
        #[cxx_name = "replacementRequired"]
        fn replacement_required(self: Pin<&mut ConversationModel>);
        #[qsignal]
        #[cxx_name = "deliveryConfirmed"]
        fn delivery_confirmed(self: Pin<&mut ConversationModel>, client_message_id: QString);
        #[qsignal]
        #[cxx_name = "batchStarted"]
        fn batch_started(self: Pin<&mut ConversationModel>);
        #[qsignal]
        #[cxx_name = "batchFinished"]
        fn batch_finished(self: Pin<&mut ConversationModel>);

        #[qinvokable]
        #[cxx_name = "openSession"]
        fn open_session(self: Pin<&mut ConversationModel>, session: &QString);
        /// `kind` is tail, delta, older or replace.
        #[qinvokable]
        #[cxx_name = "applyLogText"]
        fn apply_log_text(self: Pin<&mut ConversationModel>, response: &QString, kind: &QString);
        #[qinvokable]
        #[cxx_name = "applyActivityText"]
        fn apply_activity_text(self: Pin<&mut ConversationModel>, event: &QString);
        #[qinvokable]
        #[cxx_name = "addOptimistic"]
        fn add_optimistic(self: Pin<&mut ConversationModel>, client_message_id: &QString, text: &QString);
        #[qinvokable]
        #[cxx_name = "markDeliveryFailed"]
        fn mark_delivery_failed(self: Pin<&mut ConversationModel>, client_message_id: &QString);
        #[qinvokable]
        #[cxx_name = "takeFailedMessageForRetry"]
        fn take_failed_message_for_retry(self: Pin<&mut ConversationModel>, message_id: &QString) -> QString;
        #[qinvokable]
        #[cxx_name = "clearActivity"]
        fn clear_activity(self: Pin<&mut ConversationModel>);

        #[qinvokable]
        #[cxx_name = "indexOfMessage"]
        fn index_of_message(self: &ConversationModel, id: &QString) -> i32;

        #[inherit]
        #[cxx_name = "beginInsertRows"]
        unsafe fn begin_insert_rows(self: Pin<&mut ConversationModel>, parent: &QModelIndex, first: i32, last: i32);
        #[inherit]
        #[cxx_name = "endInsertRows"]
        unsafe fn end_insert_rows(self: Pin<&mut ConversationModel>);
        #[inherit]
        #[cxx_name = "beginRemoveRows"]
        unsafe fn begin_remove_rows(self: Pin<&mut ConversationModel>, parent: &QModelIndex, first: i32, last: i32);
        #[inherit]
        #[cxx_name = "endRemoveRows"]
        unsafe fn end_remove_rows(self: Pin<&mut ConversationModel>);
        #[inherit]
        #[cxx_name = "beginMoveRows"]
        unsafe fn begin_move_rows(self: Pin<&mut ConversationModel>, source_parent: &QModelIndex, source_first: i32, source_last: i32, destination_parent: &QModelIndex, destination_child: i32) -> bool;
        #[inherit]
        #[cxx_name = "endMoveRows"]
        unsafe fn end_move_rows(self: Pin<&mut ConversationModel>);
        #[inherit]
        #[cxx_name = "beginResetModel"]
        unsafe fn begin_reset_model(self: Pin<&mut ConversationModel>);
        #[inherit]
        #[cxx_name = "endResetModel"]
        unsafe fn end_reset_model(self: Pin<&mut ConversationModel>);
        #[inherit]
        #[qsignal]
        #[cxx_name = "dataChanged"]
        fn data_changed(self: Pin<&mut ConversationModel>, top_left: &QModelIndex, bottom_right: &QModelIndex, roles: &QList_i32);
        #[inherit]
        fn index(self: &ConversationModel, row: i32, column: i32, parent: &QModelIndex) -> QModelIndex;

        #[cxx_override]
        fn data(self: &ConversationModel, index: &QModelIndex, role: i32) -> QVariant;
        #[cxx_override]
        #[cxx_name = "roleNames"]
        fn role_names(self: &ConversationModel) -> QHash_i32_QByteArray;
        #[cxx_override]
        #[cxx_name = "rowCount"]
        fn row_count(self: &ConversationModel, parent: &QModelIndex) -> i32;
    }
}

/// Qt::UserRole + 1 onward, in the C++ enum order.
const ROLES: [(Role, &str); 26] = [
    (Role::MessageId, "messageId"),
    (Role::Author, "authorRole"),
    (Role::Body, "body"),
    (Role::Timestamp, "timestamp"),
    (Role::DayLabel, "dayLabel"),
    (Role::Revision, "revision"),
    (Role::Kind, "messageKind"),
    (Role::ToolName, "toolName"),
    (Role::Origin, "origin"),
    (Role::SenderName, "senderName"),
    (Role::Pending, "pending"),
    (Role::DeliveryFailed, "deliveryFailed"),
    (Role::Activity, "activity"),
    (Role::Tools, "tools"),
    (Role::DisplayCells, "displayCells"),
    (Role::ActivityStatus, "activityStatus"),
    (Role::Automated, "automated"),
    (Role::Category, "category"),
    (Role::ToolDetailsAvailable, "toolDetailsAvailable"),
    (Role::ActivityCount, "activityCount"),
    (Role::SenderAgentId, "senderAgentId"),
    (Role::SenderSession, "senderSession"),
    (Role::ReplyToAgentId, "replyToAgentId"),
    (Role::ReplyToName, "replyToName"),
    (Role::ReplyToSession, "replyToSession"),
    (Role::Delivery, "delivery"),
];
const USER_ROLE: i32 = 0x0100;

pub fn role_id(role: Role) -> i32 {
    USER_ROLE + 1 + ROLES.iter().position(|(r, _)| *r == role).expect("every role is listed") as i32
}

fn role_for(id: i32) -> Option<Role> {
    usize::try_from(id - USER_ROLE - 1).ok().and_then(|i| ROLES.get(i)).map(|(role, _)| *role)
}

#[derive(Default)]
pub struct ConversationModelRust {
    core: Conversation,
    /// What views see; only ever changed between begin/end notifications.
    rows: Vec<Message>,
}

fn qs(text: &str) -> QString {
    QString::from(text)
}

fn cell_value(message: &Message, role: Role) -> QVariant {
    match role {
        Role::MessageId => QVariant::from(&qs(&message.id)),
        Role::Author => QVariant::from(&qs(&message.role)),
        // Deliberately allowed to be empty: while streaming, a partial voice
        // tag must vanish rather than flash raw markup.
        Role::Body => QVariant::from(&qs(&message.display_text)),
        Role::Timestamp => QVariant::from(&qs(&message.timestamp)),
        Role::DayLabel => QVariant::from(&qs(&day_separator(&message.timestamp, "", &chrono::Local::now()))),
        Role::Revision => QVariant::from(&message.revision),
        Role::Kind => QVariant::from(&qs(&message.kind)),
        Role::ToolName => QVariant::from(&qs(&message.tool_name)),
        Role::Origin => QVariant::from(&qs(&message.origin)),
        Role::SenderName => QVariant::from(&qs(&message.sender_name)),
        Role::SenderAgentId => QVariant::from(&qs(&message.sender_agent_id)),
        Role::SenderSession => QVariant::from(&qs(&message.sender_session)),
        Role::ReplyToAgentId => QVariant::from(&qs(&message.reply_to_agent_id)),
        Role::ReplyToName => QVariant::from(&qs(&message.reply_to_name)),
        Role::ReplyToSession => QVariant::from(&qs(&message.reply_to_session)),
        Role::Delivery => QVariant::from(&qs(&message.delivery)),
        Role::Pending => QVariant::from(&message.pending),
        Role::DeliveryFailed => QVariant::from(&message.delivery_failed),
        Role::Activity => QVariant::from(&message.activity),
        Role::Tools => QVariant::from(&to_qjson_array(&message.tools)),
        Role::DisplayCells => QVariant::from(&to_qjson_array(&presented_display_cells(message))),
        Role::ActivityStatus => QVariant::from(&qs(&message.activity_status)),
        Role::Automated => QVariant::from(&message.automated),
        Role::Category => QVariant::from(&qs(&message.category)),
        Role::ToolDetailsAvailable => QVariant::from(&message.tool_details_available),
        Role::ActivityCount => QVariant::from(&message.activity_count),
    }
}

impl qobject::ConversationModel {
    fn session_value(&self) -> QString {
        qs(self.core.session())
    }
    fn conversation_id_value(&self) -> QString {
        qs(self.core.conversation_id())
    }
    fn latest_revision_value(&self) -> i64 {
        self.core.latest_revision()
    }
    fn has_more_value(&self) -> bool {
        self.core.has_more()
    }
    fn loading_value(&self) -> bool {
        self.core.loading()
    }
    fn error_value(&self) -> QString {
        qs(self.core.error())
    }
    fn voice_error_value(&self) -> QString {
        qs(self.core.voice_error())
    }
    fn count_value(&self) -> i32 {
        self.rows.len() as i32
    }
    fn index_of_message(&self, id: &QString) -> i32 {
        self.rows.iter().position(|m| m.id == id.to_string()).map_or(-1, |row| row as i32)
    }

    fn data(&self, index: &QModelIndex, role: i32) -> QVariant {
        let message = usize::try_from(index.row()).ok().and_then(|row| self.rows.get(row));
        match (message, role_for(role)) {
            (Some(message), Some(role)) => cell_value(message, role),
            _ => QVariant::default(),
        }
    }

    fn role_names(&self) -> QHash<QHashPair_i32_QByteArray> {
        let mut names = QHash::<QHashPair_i32_QByteArray>::default();
        for (role, name) in ROLES {
            names.insert(role_id(role), QByteArray::from(name));
        }
        names
    }

    fn row_count(&self, parent: &QModelIndex) -> i32 {
        if parent.is_valid() { 0 } else { self.rows.len() as i32 }
    }

    /// Run a core mutation, then replay its ops as Qt model notifications.
    pub fn mutate<R>(mut self: Pin<&mut Self>, change: impl FnOnce(&mut Conversation) -> R) -> R {
        let result = change(&mut self.as_mut().rust_mut().core);
        let ops = self.as_mut().rust_mut().core.take_ops();
        for op in ops {
            self.as_mut().replay(op);
        }
        result
    }

    fn replay(mut self: Pin<&mut Self>, op: Op) {
        crate::list_replay::replay_list_op!(self, op, role_id, emit);
    }

    fn emit(self: Pin<&mut Self>, signal: Signal) {
        match signal {
            Signal::SessionChanged => self.session_changed(),
            Signal::ConversationIdChanged => self.conversation_id_changed(),
            Signal::LatestRevisionChanged => self.latest_revision_changed(),
            Signal::HasMoreChanged => self.has_more_changed(),
            Signal::LoadingChanged => self.loading_changed(),
            Signal::ErrorChanged => self.error_changed(),
            Signal::VoiceErrorChanged => self.voice_error_changed(),
            Signal::CountChanged => self.count_changed(),
            Signal::RowsAppended { from_current_user } => self.rows_appended(from_current_user),
            Signal::RowsPrepended => self.rows_prepended(),
            Signal::ReplacementRequired => self.replacement_required(),
            Signal::DeliveryConfirmed(id) => self.delivery_confirmed(qs(&id)),
            Signal::BatchStarted => self.batch_started(),
            Signal::BatchFinished => self.batch_finished(),
        }
    }

    fn open_session(self: Pin<&mut Self>, session: &QString) {
        let session = session.to_string();
        self.mutate(|core| core.open_session(&session));
    }

    fn apply_log_text(self: Pin<&mut Self>, response: &QString, kind: &QString) {
        let kind = match kind.to_string().as_str() {
            "tail" => LoadKind::Tail,
            "older" => LoadKind::Older,
            "replace" => LoadKind::Replace,
            _ => LoadKind::Delta,
        };
        match serde_json::from_str::<serde_json::Value>(&response.to_string()) {
            Ok(value) => self.apply_log_json(&value, kind),
            Err(error) => eprintln!("ConversationModel: ignoring unparseable log response: {error}"),
        }
    }

    fn apply_activity_text(self: Pin<&mut Self>, event: &QString) {
        match serde_json::from_str::<serde_json::Value>(&event.to_string()) {
            Ok(serde_json::Value::Object(event)) => self.mutate(|core| core.apply_activity_event(&event)),
            Ok(_) => eprintln!("ConversationModel: activity event is not an object"),
            Err(error) => eprintln!("ConversationModel: ignoring unparseable activity event: {error}"),
        }
    }

    fn add_optimistic(self: Pin<&mut Self>, client_message_id: &QString, text: &QString) {
        let (id, text) = (client_message_id.to_string(), text.to_string());
        self.mutate(|core| core.add_optimistic(&id, &text));
    }

    fn mark_delivery_failed(self: Pin<&mut Self>, client_message_id: &QString) {
        let id = client_message_id.to_string();
        self.mutate(|core| core.mark_delivery_failed(&id));
    }

    fn take_failed_message_for_retry(self: Pin<&mut Self>, message_id: &QString) -> QString {
        let id = message_id.to_string();
        qs(&self.mutate(|core| core.take_failed_message_for_retry(&id)).unwrap_or_default())
    }

    fn clear_activity(self: Pin<&mut Self>) {
        self.mutate(Conversation::clear_activity);
    }

    pub fn apply_log_json(self: Pin<&mut Self>, response: &serde_json::Value, kind: LoadKind) {
        if let Some(object) = response.as_object() {
            self.mutate(|core| core.apply_log(object, kind));
        }
    }
}
