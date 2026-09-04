#include "models/ConversationModel.h"

#include <QDateTime>
#include <QJsonArray>
#include <QVariantList>
#include <algorithm>

namespace clarp {
namespace {

QVariantList jsonArrayToVariantList(const QJsonArray& array) { return array.toVariantList(); }

} // namespace

ConversationModel::ConversationModel(QObject* parent) : QAbstractListModel(parent) {}

int ConversationModel::rowCount(const QModelIndex& parent) const {
    return parent.isValid() ? 0 : static_cast<int>(m_messages.size());
}

QVariant ConversationModel::data(const QModelIndex& index, int role) const {
    if (!index.isValid() || index.row() < 0 || index.row() >= m_messages.size()) {
        return {};
    }
    const Message& message = m_messages.at(index.row());
    switch (role) {
    case MessageIdRole:
        return message.id;
    case AuthorRole:
        return message.role;
    case BodyRole:
        return message.text;
    case TimestampRole:
        return message.timestamp;
    case RevisionRole:
        return message.revision;
    case KindRole:
        return message.kind;
    case ToolNameRole:
        return message.toolName;
    case OriginRole:
        return message.origin;
    case SenderNameRole:
        return message.senderName;
    case PendingRole:
        return message.pending;
    case DeliveryFailedRole:
        return message.deliveryFailed;
    case ActivityRole:
        return message.activity;
    case ToolsRole:
        return jsonArrayToVariantList(message.tools);
    case DisplayCellsRole:
        return jsonArrayToVariantList(message.displayCells);
    default:
        return {};
    }
}

QHash<int, QByteArray> ConversationModel::roleNames() const {
    return {
        {MessageIdRole, "messageId"},
        {AuthorRole, "authorRole"},
        {BodyRole, "body"},
        {TimestampRole, "timestamp"},
        {RevisionRole, "revision"},
        {KindRole, "messageKind"},
        {ToolNameRole, "toolName"},
        {OriginRole, "origin"},
        {SenderNameRole, "senderName"},
        {PendingRole, "pending"},
        {DeliveryFailedRole, "deliveryFailed"},
        {ActivityRole, "activity"},
        {ToolsRole, "tools"},
        {DisplayCellsRole, "displayCells"},
    };
}

QString ConversationModel::session() const { return m_session; }

QString ConversationModel::conversationId() const { return m_conversationId; }

qint64 ConversationModel::latestRevision() const { return m_latestRevision; }

bool ConversationModel::hasMore() const { return m_hasMore; }

bool ConversationModel::loading() const { return m_loading; }

QString ConversationModel::error() const { return m_error; }

void ConversationModel::openSession(const QString& session) {
    if (m_session == session) {
        return;
    }
    beginResetModel();
    m_session = session;
    m_conversationId.clear();
    m_latestRevision = 0;
    m_hasMore = false;
    m_error.clear();
    m_messages.clear();
    m_byId.clear();
    endResetModel();
    emit sessionChanged();
    emit conversationIdChanged();
    emit latestRevisionChanged();
    emit hasMoreChanged();
    emit errorChanged();
    emit countChanged();
}

void ConversationModel::applyLog(const QJsonObject& response, LoadKind kind) {
    const QString nextConversationId = response.value(QStringLiteral("conversation_id")).toString();
    if (kind == LoadKind::Delta && response.value(QStringLiteral("replace_required")).toBool()) {
        emit replacementRequired();
        return;
    }
    if (kind != LoadKind::Tail && !m_conversationId.isEmpty() && !nextConversationId.isEmpty() &&
        nextConversationId != m_conversationId) {
        emit replacementRequired();
        return;
    }

    const QJsonArray turns = response.value(QStringLiteral("turns")).toArray();
    if (kind == LoadKind::Tail) {
        QVector<Message> rows;
        rows.reserve(turns.size());
        for (const auto& value : turns) {
            if (value.isObject()) {
                rows.append(Message::fromJson(value.toObject()));
            }
        }
        replaceRows(std::move(rows));
    } else {
        mergeRows(turns);
    }

    if (m_conversationId != nextConversationId) {
        m_conversationId = nextConversationId;
        emit conversationIdChanged();
    }
    const qint64 revision = response.value(QStringLiteral("latest_revision")).toInteger();
    if (m_latestRevision != revision) {
        m_latestRevision = revision;
        emit latestRevisionChanged();
    }
    const bool more = response.value(QStringLiteral("has_more")).toBool();
    if (m_hasMore != more) {
        m_hasMore = more;
        emit hasMoreChanged();
    }
    setLoading(false);
    setError({});
}

void ConversationModel::addOptimistic(const QString& clientMessageId, const QString& text) {
    Message message;
    message.id = QStringLiteral("u-") + clientMessageId;
    message.role = QStringLiteral("user");
    message.text = text;
    message.timestamp = QDateTime::currentDateTimeUtc().toString(Qt::ISODateWithMs);
    message.pending = true;

    const int row = static_cast<int>(m_messages.size());
    beginInsertRows({}, row, row);
    m_messages.append(std::move(message));
    m_byId.insert(m_messages.last().id, row);
    endInsertRows();
    emit countChanged();
}

void ConversationModel::markDeliveryFailed(const QString& clientMessageId) {
    const QString id = QStringLiteral("u-") + clientMessageId;
    const int row = m_byId.value(id, -1);
    if (row < 0 || !m_messages.at(row).pending) {
        return;
    }
    Message& message = *(m_messages.begin() + row);
    message.deliveryFailed = true;
    message.pending = false;
    const QModelIndex changed = index(row, 0);
    emit dataChanged(changed, changed, {PendingRole, DeliveryFailedRole});
}

void ConversationModel::applyActivityEvent(const QJsonObject& event) {
    clearActivity();
    Message message;
    message.id = QStringLiteral("activity:") + m_session;
    message.role = QStringLiteral("activity");
    message.activity = true;
    message.kind = event.value(QStringLiteral("kind")).toString();
    message.toolName = event.value(QStringLiteral("tool")).toString();
    message.text = event.value(QStringLiteral("summary")).toString();
    if (message.text.isEmpty()) {
        message.text = event.value(QStringLiteral("action")).toString();
    }
    message.timestamp = QDateTime::currentDateTimeUtc().toString(Qt::ISODateWithMs);
    const int row = static_cast<int>(m_messages.size());
    beginInsertRows({}, row, row);
    m_messages.append(std::move(message));
    rebuildIndex();
    endInsertRows();
    emit countChanged();
}

void ConversationModel::clearActivity() {
    for (qsizetype offset = m_messages.size(); offset > 0; --offset) {
        const int row = static_cast<int>(offset - 1);
        if (!m_messages.at(row).activity) {
            continue;
        }
        beginRemoveRows({}, row, row);
        m_messages.removeAt(row);
        endRemoveRows();
        rebuildIndex();
        emit countChanged();
    }
}

void ConversationModel::setLoading(bool loading) {
    if (m_loading == loading) {
        return;
    }
    m_loading = loading;
    emit loadingChanged();
}

void ConversationModel::setError(const QString& error) {
    if (m_error == error) {
        return;
    }
    m_error = error;
    emit errorChanged();
}

void ConversationModel::rebuildIndex() {
    m_byId.clear();
    for (int row = 0; row < m_messages.size(); ++row) {
        if (!m_messages.at(row).id.isEmpty()) {
            m_byId.insert(m_messages.at(row).id, row);
        }
    }
}

void ConversationModel::replaceRows(QVector<Message> rows) {
    QHash<QString, Message> optimistic;
    for (const Message& message : std::as_const(m_messages)) {
        if (message.pending) {
            optimistic.insert(message.id, message);
        }
    }
    for (const Message& message : std::as_const(rows)) {
        optimistic.remove(message.id);
        if (message.id.startsWith(QStringLiteral("u-"))) {
            emit deliveryConfirmed(message.id.sliced(2));
        }
    }
    for (const Message& pending : std::as_const(optimistic)) {
        rows.append(pending);
    }
    std::ranges::stable_sort(rows, [](const Message& left, const Message& right) {
        if (left.revision > 0 && right.revision > 0 && left.revision != right.revision) {
            return left.revision < right.revision;
        }
        return left.timestamp < right.timestamp;
    });

    beginResetModel();
    m_messages = std::move(rows);
    rebuildIndex();
    endResetModel();
    emit countChanged();
}

void ConversationModel::mergeRows(const QJsonArray& rows) {
    bool changed = false;
    for (const auto& value : rows) {
        if (!value.isObject()) {
            continue;
        }
        Message incoming = Message::fromJson(value.toObject());
        if (incoming.id.isEmpty()) {
            continue;
        }
        const int existing = m_byId.value(incoming.id, -1);
        if (existing >= 0) {
            if (m_messages.at(existing).revision > incoming.revision && incoming.revision != 0) {
                continue;
            }
            const bool confirmed = m_messages.at(existing).pending;
            m_messages.replace(existing, std::move(incoming));
            const QModelIndex changedIndex = index(existing, 0);
            emit dataChanged(changedIndex, changedIndex);
            if (confirmed && m_messages.at(existing).id.startsWith(QStringLiteral("u-"))) {
                emit deliveryConfirmed(m_messages.at(existing).id.sliced(2));
            }
            changed = true;
            continue;
        }
        const int insertAt = static_cast<int>(m_messages.size());
        beginInsertRows({}, insertAt, insertAt);
        m_messages.append(std::move(incoming));
        endInsertRows();
        changed = true;
    }
    if (changed) {
        sortRows();
        rebuildIndex();
        emit countChanged();
    }
    clearActivity();
}

void ConversationModel::sortRows() {
    emit layoutAboutToBeChanged();
    std::ranges::stable_sort(m_messages, [](const Message& left, const Message& right) {
        if (left.pending != right.pending) {
            return !left.pending;
        }
        if (left.revision > 0 && right.revision > 0 && left.revision != right.revision) {
            return left.revision < right.revision;
        }
        return left.timestamp < right.timestamp;
    });
    emit layoutChanged();
}

} // namespace clarp
