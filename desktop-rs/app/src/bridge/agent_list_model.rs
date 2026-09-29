//! QAbstractListModel over `clarp_core::roster::Roster`, replaying its ops
//! onto a mirror of presented rows. Role names match the C++ AgentListModel.

use std::pin::Pin;

use clarp_core::roster::{ALL_ROLES, AgentRow, Op, Role, Roster, Signal};
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
        #[qproperty(i32, count, READ = count_value, NOTIFY = count_changed)]
        type AgentListModel = super::AgentListModelRust;
    }

    unsafe extern "RustQt" {
        fn count_value(self: &AgentListModel) -> i32;
        #[qsignal]
        #[cxx_name = "countChanged"]
        fn count_changed(self: Pin<&mut AgentListModel>);

        #[qinvokable]
        #[cxx_name = "indexOfSession"]
        fn index_of_session(self: &AgentListModel, session: &QString) -> i32;
        #[qinvokable]
        #[cxx_name = "applySnapshotText"]
        fn apply_snapshot_text(self: Pin<&mut AgentListModel>, snapshot: &QString);
        /// `kind` is state, focus, queue or notification.
        #[qinvokable]
        #[cxx_name = "applyEventText"]
        fn apply_event_text(self: Pin<&mut AgentListModel>, kind: &QString, event: &QString);
        #[qinvokable]
        #[cxx_name = "clearUnread"]
        fn clear_unread(self: Pin<&mut AgentListModel>, session: &QString);
        #[qinvokable]
        #[cxx_name = "markTransportUnavailable"]
        fn mark_transport_unavailable(self: Pin<&mut AgentListModel>);
        #[qinvokable]
        #[cxx_name = "recordOutgoingActivity"]
        fn record_outgoing_activity(self: Pin<&mut AgentListModel>, session: &QString) -> bool;

        #[inherit]
        #[cxx_name = "beginInsertRows"]
        unsafe fn begin_insert_rows(self: Pin<&mut AgentListModel>, parent: &QModelIndex, first: i32, last: i32);
        #[inherit]
        #[cxx_name = "endInsertRows"]
        unsafe fn end_insert_rows(self: Pin<&mut AgentListModel>);
        #[inherit]
        #[cxx_name = "beginRemoveRows"]
        unsafe fn begin_remove_rows(self: Pin<&mut AgentListModel>, parent: &QModelIndex, first: i32, last: i32);
        #[inherit]
        #[cxx_name = "endRemoveRows"]
        unsafe fn end_remove_rows(self: Pin<&mut AgentListModel>);
        #[inherit]
        #[cxx_name = "beginMoveRows"]
        unsafe fn begin_move_rows(self: Pin<&mut AgentListModel>, source_parent: &QModelIndex, source_first: i32, source_last: i32, destination_parent: &QModelIndex, destination_child: i32) -> bool;
        #[inherit]
        #[cxx_name = "endMoveRows"]
        unsafe fn end_move_rows(self: Pin<&mut AgentListModel>);
        #[inherit]
        #[cxx_name = "beginResetModel"]
        unsafe fn begin_reset_model(self: Pin<&mut AgentListModel>);
        #[inherit]
        #[cxx_name = "endResetModel"]
        unsafe fn end_reset_model(self: Pin<&mut AgentListModel>);
        #[inherit]
        #[qsignal]
        #[cxx_name = "dataChanged"]
        fn data_changed(self: Pin<&mut AgentListModel>, top_left: &QModelIndex, bottom_right: &QModelIndex, roles: &QList_i32);
        #[inherit]
        fn index(self: &AgentListModel, row: i32, column: i32, parent: &QModelIndex) -> QModelIndex;

        #[cxx_override]
        fn data(self: &AgentListModel, index: &QModelIndex, role: i32) -> QVariant;
        #[cxx_override]
        #[cxx_name = "roleNames"]
        fn role_names(self: &AgentListModel) -> QHash_i32_QByteArray;
        #[cxx_override]
        #[cxx_name = "rowCount"]
        fn row_count(self: &AgentListModel, parent: &QModelIndex) -> i32;
    }
}

const USER_ROLE: i32 = 0x0100;

fn role_name(role: Role) -> &'static str {
    match role {
        Role::AgentId => "agentId",
        Role::Session => "session",
        Role::Name => "name",
        Role::Backend => "backend",
        Role::WorkingDirectory => "workingDirectory",
        Role::Model => "modelName",
        Role::Effort => "effort",
        Role::AvatarUrl => "avatarUrl",
        Role::AvatarSymbol => "avatarSymbol",
        Role::State => "agentState",
        Role::StatusText => "statusText",
        Role::LastMessage => "lastMessage",
        Role::LastCompletedMessage => "lastCompletedMessage",
        Role::LastActivity => "lastActivity",
        Role::ConversationId => "conversationId",
        Role::HeadRevision => "headRevision",
        Role::ContextTokens => "contextTokens",
        Role::ContextWindow => "contextWindow",
        Role::QueueCount => "queueCount",
        Role::Alive => "alive",
        Role::Busy => "busy",
        Role::Focused => "focused",
        Role::Muted => "muted",
        Role::HeartbeatEnabled => "heartbeatEnabled",
        Role::DreamingEnabled => "dreamingEnabled",
        Role::Schedules => "schedules",
        Role::McpServers => "mcpServers",
        Role::Unread => "unread",
        Role::ParentAgentId => "parentAgentId",
        Role::AgentRole => "agentRole",
        Role::HelperState => "helperState",
        Role::ChildCount => "childCount",
        Role::RunningChildren => "runningChildren",
        Role::BackgroundJobCount => "backgroundJobCount",
        Role::SubAgentCount => "subAgentCount",
        Role::ProcessCount => "processCount",
    }
}

/// Qt::UserRole + 1 onward, in the C++ enum order.
pub fn role_id(role: Role) -> i32 {
    USER_ROLE + 1 + ALL_ROLES.iter().position(|r| *r == role).expect("every role is listed") as i32
}

pub fn role_for(id: i32) -> Option<Role> {
    usize::try_from(id - USER_ROLE - 1).ok().and_then(|i| ALL_ROLES.get(i)).copied()
}

fn qs(text: &str) -> QString {
    QString::from(text)
}

pub fn cell_value(row: &AgentRow, role: Role) -> QVariant {
    match role {
        Role::AgentId => QVariant::from(&qs(&row.agent_id)),
        Role::Session => QVariant::from(&qs(&row.session)),
        Role::Name => QVariant::from(&qs(&row.name)),
        Role::Backend => QVariant::from(&qs(&row.backend)),
        Role::WorkingDirectory => QVariant::from(&qs(&row.working_directory)),
        Role::Model => QVariant::from(&qs(&row.model)),
        Role::Effort => QVariant::from(&qs(&row.effort)),
        Role::AvatarUrl => QVariant::from(&qs(&row.avatar_url)),
        Role::AvatarSymbol => QVariant::from(&qs(&row.avatar_symbol)),
        Role::State => QVariant::from(&qs(&row.state)),
        Role::StatusText => QVariant::from(&qs(&row.status_text)),
        Role::LastMessage => QVariant::from(&qs(&row.last_message)),
        Role::LastCompletedMessage => QVariant::from(&qs(&row.last_completed_message)),
        Role::LastActivity => QVariant::from(&row.last_activity),
        Role::ConversationId => QVariant::from(&qs(&row.conversation_id)),
        Role::HeadRevision => QVariant::from(&row.head_revision),
        Role::ContextTokens => QVariant::from(&row.context_tokens),
        Role::ContextWindow => QVariant::from(&row.context_window),
        Role::QueueCount => QVariant::from(&row.queue_count),
        Role::Alive => QVariant::from(&row.alive),
        Role::Busy => QVariant::from(&row.busy),
        Role::Focused => QVariant::from(&row.focused),
        Role::Muted => QVariant::from(&row.muted),
        Role::HeartbeatEnabled => QVariant::from(&row.heartbeat_enabled),
        Role::DreamingEnabled => QVariant::from(&row.dreaming_enabled),
        Role::Schedules => QVariant::from(&to_qjson_array(&row.schedules)),
        Role::McpServers => QVariant::from(&to_qjson_array(&row.mcp_servers)),
        Role::Unread => QVariant::from(&row.unread),
        Role::ParentAgentId => QVariant::from(&qs(&row.parent_agent_id)),
        Role::AgentRole => QVariant::from(&qs(&row.agent_role)),
        Role::HelperState => QVariant::from(&qs(&row.helper_state)),
        Role::ChildCount => QVariant::from(&row.child_count),
        Role::RunningChildren => QVariant::from(&row.running_children),
        Role::BackgroundJobCount => QVariant::from(&row.background_job_count),
        Role::SubAgentCount => QVariant::from(&row.sub_agent_count),
        Role::ProcessCount => QVariant::from(&row.process_count),
    }
}

#[derive(Default)]
pub struct AgentListModelRust {
    core: Roster,
    /// What views see; only ever changed between begin/end notifications.
    rows: Vec<AgentRow>,
}

fn parse_object(text: &QString, what: &str) -> Option<serde_json::Map<String, serde_json::Value>> {
    match serde_json::from_str::<serde_json::Value>(&text.to_string()) {
        Ok(serde_json::Value::Object(object)) => Some(object),
        Ok(_) => {
            eprintln!("AgentListModel: {what} is not a JSON object");
            None
        }
        Err(error) => {
            eprintln!("AgentListModel: ignoring unparseable {what}: {error}");
            None
        }
    }
}

impl qobject::AgentListModel {
    fn count_value(&self) -> i32 {
        self.rows.len() as i32
    }

    fn index_of_session(&self, session: &QString) -> i32 {
        let session = session.to_string();
        self.rows.iter().position(|r| r.session == session).map_or(-1, |row| row as i32)
    }

    pub fn roster(&self) -> &Roster {
        &self.core
    }

    fn data(&self, index: &QModelIndex, role: i32) -> QVariant {
        let row = usize::try_from(index.row()).ok().and_then(|row| self.rows.get(row));
        match (row, role_for(role)) {
            (Some(row), Some(role)) => cell_value(row, role),
            _ => QVariant::default(),
        }
    }

    fn role_names(&self) -> QHash<QHashPair_i32_QByteArray> {
        let mut names = QHash::<QHashPair_i32_QByteArray>::default();
        for role in ALL_ROLES {
            names.insert(role_id(role), QByteArray::from(role_name(role)));
        }
        names
    }

    fn row_count(&self, parent: &QModelIndex) -> i32 {
        if parent.is_valid() { 0 } else { self.rows.len() as i32 }
    }

    /// Run a core mutation, then replay its ops as Qt model notifications.
    pub fn mutate<R>(mut self: Pin<&mut Self>, change: impl FnOnce(&mut Roster) -> R) -> R {
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
            Signal::CountChanged => self.count_changed(),
        }
    }

    fn apply_snapshot_text(self: Pin<&mut Self>, snapshot: &QString) {
        if let Some(snapshot) = parse_object(snapshot, "snapshot") {
            self.mutate(|core| core.apply_snapshot(&snapshot));
        }
    }

    fn apply_event_text(self: Pin<&mut Self>, kind: &QString, event: &QString) {
        let Some(event) = parse_object(event, "event") else { return };
        match kind.to_string().as_str() {
            "state" => self.mutate(|core| core.apply_state_event(&event)),
            "focus" => self.mutate(|core| core.apply_focus_event(&event)),
            "queue" => self.mutate(|core| core.apply_queue_event(&event)),
            "notification" => self.mutate(|core| core.apply_notification_event(&event)),
            other => eprintln!("AgentListModel: unknown event kind {other}"),
        }
    }

    fn clear_unread(self: Pin<&mut Self>, session: &QString) {
        let session = session.to_string();
        self.mutate(|core| core.clear_unread(&session));
    }

    fn mark_transport_unavailable(self: Pin<&mut Self>) {
        self.mutate(Roster::mark_transport_unavailable);
    }

    fn record_outgoing_activity(self: Pin<&mut Self>, session: &QString) -> bool {
        let session = session.to_string();
        self.mutate(|core| core.record_outgoing_activity(&session))
    }
}
