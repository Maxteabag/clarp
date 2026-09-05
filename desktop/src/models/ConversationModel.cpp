#include "models/ConversationModel.h"

#include <QDateTime>
#include <QJsonArray>
#include <QRegularExpression>
#include <QVariantList>
#include <algorithm>

namespace clarp {
namespace {

QVariantList jsonArrayToVariantList(const QJsonArray& array) { return array.toVariantList(); }

QJsonObject messageToJson(const Message& message) {
    return {{QStringLiteral("id"), message.id},
            {QStringLiteral("role"), message.role},
            {QStringLiteral("text"), message.text},
            {QStringLiteral("timestamp"), message.timestamp},
            {QStringLiteral("revision"), message.revision},
            {QStringLiteral("kind"), message.kind},
            {QStringLiteral("tool_name"), message.toolName},
            {QStringLiteral("origin"), message.origin},
            {QStringLiteral("sender_name"), message.senderName},
            {QStringLiteral("sender_agent_id"), message.senderAgentId},
            {QStringLiteral("sender_session"), message.senderSession},
            {QStringLiteral("trace_id"), message.traceId},
            {QStringLiteral("category"), message.category},
            {QStringLiteral("automated"), message.automated},
            {QStringLiteral("activity_count"), message.activityCount},
            {QStringLiteral("tool_details_available"), message.toolDetailsAvailable},
            {QStringLiteral("delivery_failed"), message.deliveryFailed},
            {QStringLiteral("tools"), message.tools},
            {QStringLiteral("display_cells"), message.displayCells}};
}

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
        // displayText is deliberately allowed to be empty. During streaming a
        // partial <speak>/<vox> tag must disappear instead of falling back to
        // the raw protocol text and briefly flashing markup in the timeline.
        return message.displayText;
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
    case ActivityStatusRole:
        return message.activityStatus;
    case AutomatedRole:
        return message.automated;
    case CategoryRole:
        return message.category;
    case ToolDetailsAvailableRole:
        return message.toolDetailsAvailable;
    case ActivityCountRole:
        return message.activityCount;
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
        {ActivityStatusRole, "activityStatus"},
        {AutomatedRole, "automated"},
        {CategoryRole, "category"},
        {ToolDetailsAvailableRole, "toolDetailsAvailable"},
        {ActivityCountRole, "activityCount"},
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
    if (kind != LoadKind::Tail && kind != LoadKind::Replace &&
        !m_conversationId.isEmpty() && !nextConversationId.isEmpty() &&
        nextConversationId != m_conversationId) {
        emit replacementRequired();
        return;
    }

    const QJsonArray turns = response.value(QStringLiteral("turns")).toArray();
    const bool replacesConversation =
        kind == LoadKind::Replace ||
        (kind == LoadKind::Tail &&
         (m_conversationId.isEmpty() || nextConversationId != m_conversationId));
    if (replacesConversation) {
        QVector<Message> rows;
        rows.reserve(turns.size());
        for (const auto& value : turns) {
            if (value.isObject()) {
                rows.append(Message::fromJson(value.toObject()));
            }
        }
        replaceRows(std::move(rows));
    } else if (kind == LoadKind::Older) {
        prependRows(turns);
    } else {
        mergeRows(turns);
    }

    if (m_conversationId != nextConversationId) {
        m_conversationId = nextConversationId;
        emit conversationIdChanged();
    }
    const qint64 responseRevision =
        response.value(QStringLiteral("latest_revision")).toInteger();
    const qint64 revision =
        replacesConversation ? responseRevision : std::max(m_latestRevision, responseRevision);
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

QJsonObject ConversationModel::cacheSnapshot() const {
    QJsonArray turns;
    for (const Message& message : m_messages) {
        if (message.activity || message.pending ||
            message.kind == QStringLiteral("live")) {
            continue;
        }
        turns.append(messageToJson(message));
    }
    return {{QStringLiteral("conversation_id"), m_conversationId},
            {QStringLiteral("turns"), turns},
            {QStringLiteral("latest_revision"), m_latestRevision},
            {QStringLiteral("has_more"), m_hasMore}};
}

bool ConversationModel::restoreCacheSnapshot(const QJsonObject& snapshot) {
    if (snapshot.isEmpty() || !snapshot.value(QStringLiteral("turns")).isArray()) {
        return false;
    }
    applyLog(snapshot, LoadKind::Tail);
    return true;
}

void ConversationModel::addOptimistic(const QString& clientMessageId, const QString& text) {
    Message message;
    message.id = QStringLiteral("u-") + clientMessageId;
    message.role = QStringLiteral("user");
    message.text = text;
    message.displayText = text;
    message.timestamp = QDateTime::currentDateTimeUtc().toString(Qt::ISODateWithMs);
    message.pending = true;

    const int row = static_cast<int>(m_messages.size());
    beginInsertRows({}, row, row);
    m_messages.append(std::move(message));
    m_byId.insert(m_messages.last().id, row);
    endInsertRows();
    emit countChanged();
    emit rowsAppended(true);
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

QString ConversationModel::takeFailedMessageForRetry(const QString& messageId) {
    const int row = m_byId.value(messageId, -1);
    if (row < 0 || !m_messages.at(row).deliveryFailed) {
        return {};
    }
    const QString text = m_messages.at(row).text;
    beginRemoveRows({}, row, row);
    m_messages.removeAt(row);
    endRemoveRows();
    rebuildIndex();
    emit countChanged();
    return text;
}

void ConversationModel::applyActivityEvent(const QJsonObject& event) {
    const QString action = event.value(QStringLiteral("activity_action"))
                               .toString(event.value(QStringLiteral("action")).toString());
    const QString tool = event.value(QStringLiteral("activity_tool"))
                             .toString(event.value(QStringLiteral("tool")).toString());
    const QString filePath = event.value(QStringLiteral("activity_file_path"))
                                 .toString(event.value(QStringLiteral("file_path")).toString());
    const QString phase = event.value(QStringLiteral("activity_phase"))
                              .toString(event.value(QStringLiteral("phase")).toString());
    const QString kind = event.value(QStringLiteral("activity_kind"))
                             .toString(event.value(QStringLiteral("kind")).toString());
    QString label;
    for (const QString& candidate : {action, phase, tool, kind}) {
        if (!candidate.isEmpty()) {
            label = candidate;
            break;
        }
    }
    QString summary = event.value(QStringLiteral("activity_summary"))
                          .toString(event.value(QStringLiteral("summary")).toString());
    if (summary.isEmpty()) {
        summary = tool;
    }
    if (label.isEmpty() && summary.isEmpty()) {
        return;
    }
    QString status = event.value(QStringLiteral("activity_status"))
                         .toString(event.value(QStringLiteral("status")).toString());
    if (status.isEmpty()) {
        const QString state = event.value(QStringLiteral("state")).toString();
        status = state == QStringLiteral("thinking") || state == QStringLiteral("tool") ||
                         state == QStringLiteral("compacting")
                     ? QStringLiteral("running")
                     : QStringLiteral("ok");
    }
    const QString matchKey = action + u'|' + tool + u'|' + filePath;

    for (qsizetype offset = m_messages.size(); offset > 0; --offset) {
        const int row = static_cast<int>(offset - 1);
        Message& existing = *(m_messages.begin() + row);
        if (!existing.activity || existing.activityMatchKey != matchKey) {
            continue;
        }
        if (status == QStringLiteral("running") &&
            existing.activityStatus != QStringLiteral("running")) {
            continue;
        }
        if (status != QStringLiteral("running") &&
            existing.activityStatus != QStringLiteral("running")) {
            return;
        }
        existing.activityStatus = status;
        if (!label.isEmpty()) {
            existing.toolName = label;
        }
        if (!summary.isEmpty()) {
            existing.text = summary;
            existing.displayText = summary;
        }
        const QModelIndex changed = index(row, 0);
        emit dataChanged(changed, changed, {BodyRole, ToolNameRole, ActivityStatusRole});
        return;
    }

    Message message;
    message.id = QStringLiteral("activity:%1:%2").arg(m_session).arg(++m_activityCounter);
    message.role = QStringLiteral("activity");
    message.activity = true;
    message.kind = kind;
    message.toolName = label;
    message.text = summary;
    message.displayText = summary;
    message.activityStatus = status;
    message.activityMatchKey = matchKey;
    message.timestamp = QDateTime::currentDateTimeUtc().toString(Qt::ISODateWithMs);
    const int row = static_cast<int>(m_messages.size());
    beginInsertRows({}, row, row);
    m_messages.append(std::move(message));
    rebuildIndex();
    endInsertRows();
    emit countChanged();
    emit rowsAppended(false);

    int activityOverflow = 0;
    for (const Message& existingMessage : std::as_const(m_messages)) {
        if (existingMessage.activity) {
            ++activityOverflow;
        }
    }
    activityOverflow -= 80;
    for (int candidateRow = 0; candidateRow < m_messages.size() && activityOverflow > 0;) {
        if (!m_messages.at(candidateRow).activity) {
            ++candidateRow;
            continue;
        }
        beginRemoveRows({}, candidateRow, candidateRow);
        m_messages.removeAt(candidateRow);
        endRemoveRows();
        --activityOverflow;
        emit countChanged();
    }
    rebuildIndex();
}

bool ConversationModel::applyToolDetails(const QString& messageId, const QJsonObject& details) {
    const int row = m_byId.value(messageId, -1);
    if (row < 0) {
        return false;
    }
    Message& message = m_messages[row];
    message.tools = details.value(QStringLiteral("tools")).toArray();
    message.displayCells = details.value(QStringLiteral("display_cells")).toArray();
    message.toolDetailsAvailable = false;
    const QModelIndex changed = index(row, 0);
    emit dataChanged(changed, changed,
                     {ToolsRole, DisplayCellsRole, ToolDetailsAvailableRole});
    return true;
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

void ConversationModel::showTransientThinking(const QString& persona) {
    applyActivityEvent({{QStringLiteral("activity_status"), QStringLiteral("running")},
                        {QStringLiteral("activity_action"), QStringLiteral("thinking")},
                        {QStringLiteral("activity_summary"),
                         QStringLiteral("%1 is working")
                             .arg(persona.isEmpty() ? QStringLiteral("Agent") : persona)}});
}

void ConversationModel::clearRunningActivity() {
    for (qsizetype offset = m_messages.size(); offset > 0; --offset) {
        const int row = static_cast<int>(offset - 1);
        if (!m_messages.at(row).activity ||
            m_messages.at(row).activityStatus != QStringLiteral("running")) {
            continue;
        }
        beginRemoveRows({}, row, row);
        m_messages.removeAt(row);
        endRemoveRows();
        emit countChanged();
    }
    rebuildIndex();
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
    beginResetModel();
    m_messages = std::move(rows);
    rebuildIndex();
    endResetModel();
    emit countChanged();
    dropSupersededLiveTurns();
}

void ConversationModel::mergeRows(const QJsonArray& rows) {
    bool inserted = false;
    bool appended = false;
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
            continue;
        }
        int insertAt = static_cast<int>(m_messages.size());
        for (int row = 0; row < m_messages.size(); ++row) {
            const Message& candidate = m_messages.at(row);
            if (candidate.kind == QStringLiteral("live") &&
                incoming.role == QStringLiteral("assistant") &&
                incoming.kind != QStringLiteral("live") &&
                (candidate.revision == 0 || incoming.revision >= candidate.revision)) {
                insertAt = row + 1;
                break;
            }
            if (candidate.pending || candidate.deliveryFailed ||
                candidate.kind == QStringLiteral("live")) {
                insertAt = row;
                break;
            }
            if (incoming.revision > 0 && candidate.revision > incoming.revision) {
                insertAt = row;
                break;
            }
        }
        const bool insertedAtEnd = insertAt == m_messages.size();
        beginInsertRows({}, insertAt, insertAt);
        m_messages.insert(insertAt, std::move(incoming));
        endInsertRows();
        inserted = true;
        appended = appended || insertedAtEnd;
        rebuildIndex();
    }
    if (inserted) {
        emit countChanged();
        if (appended) {
            emit rowsAppended(false);
        }
        clearRunningActivity();
    }
    dropSupersededLiveTurns();
}

void ConversationModel::prependRows(const QJsonArray& rows) {
    QVector<Message> older;
    older.reserve(rows.size());
    for (const auto& value : rows) {
        if (!value.isObject()) {
            continue;
        }
        Message incoming = Message::fromJson(value.toObject());
        if (incoming.id.isEmpty() || m_byId.contains(incoming.id)) {
            continue;
        }
        older.append(std::move(incoming));
    }
    if (older.isEmpty()) {
        return;
    }
    beginInsertRows({}, 0, static_cast<int>(older.size() - 1));
    m_messages = older + m_messages;
    endInsertRows();
    rebuildIndex();
    emit countChanged();
    emit rowsPrepended();
}

void ConversationModel::dropSupersededLiveTurns() {
    static const QRegularExpression tags(QStringLiteral("<[^>]+>"));
    static const QRegularExpression whitespace(QStringLiteral("\\s+"));
    const auto normalized = [&](QString text) {
        text.remove(tags);
        text.replace(whitespace, QStringLiteral(" "));
        return text.trimmed();
    };
    for (qsizetype offset = m_messages.size(); offset > 0; --offset) {
        const int row = static_cast<int>(offset - 1);
        const Message& message = m_messages.at(row);
        if (message.role != QStringLiteral("assistant") || message.kind != QStringLiteral("live")) {
            continue;
        }
        const QString live = normalized(message.text);
        bool covered = false;
        // Prefer the protocol correlation ID. Older Hosts did not include it
        // on live rows, so the fallback is deliberately narrow: only the next
        // durable assistant row in this turn (ignoring transient activity)
        // may supersede the live row. Earlier repeated answers are never
        // allowed to hide a new stream.
        for (int candidateRow = row + 1; candidateRow < m_messages.size(); ++candidateRow) {
            const Message& candidate = m_messages.at(candidateRow);
            if (candidate.activity) {
                continue;
            }
            if (candidate.role == QStringLiteral("user") ||
                candidate.kind == QStringLiteral("live")) {
                break;
            }
            if (candidate.role != QStringLiteral("assistant")) {
                continue;
            }
            const bool correlated =
                (!message.traceId.isEmpty() && message.traceId == candidate.traceId) ||
                message.traceId.isEmpty() || candidate.traceId.isEmpty();
            const QString finalText = normalized(candidate.text);
            covered = correlated && !live.isEmpty() && !finalText.isEmpty() &&
                      (finalText == live || finalText.startsWith(live) ||
                       live.startsWith(finalText));
            break;
        }
        if (!covered) {
            continue;
        }
        beginRemoveRows({}, row, row);
        m_messages.removeAt(row);
        endRemoveRows();
        emit countChanged();
    }
    rebuildIndex();
}

} // namespace clarp
