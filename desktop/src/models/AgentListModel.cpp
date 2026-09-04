#include "models/AgentListModel.h"

#include <QJsonArray>
#include <QJsonValue>
#include <QSet>
#include <algorithm>

namespace clarp {

AgentListModel::AgentListModel(QObject* parent) : QAbstractListModel(parent) {}

AgentListModel::AgentListModel(bool archivedOnly, QObject* parent)
    : QAbstractListModel(parent), m_archivedOnly(archivedOnly) {}

int AgentListModel::rowCount(const QModelIndex& parent) const {
    return parent.isValid() ? 0 : static_cast<int>(m_agents.size());
}

QVariant AgentListModel::data(const QModelIndex& index, int role) const {
    if (!index.isValid() || index.row() < 0 || index.row() >= m_agents.size()) {
        return {};
    }

    const Agent& agent = m_agents.at(index.row());
    switch (role) {
    case AgentIdRole:
        return agent.agentId;
    case SessionRole:
        return agent.session;
    case NameRole:
        return displayName(agent);
    case BackendRole:
        return agent.backend;
    case WorkingDirectoryRole:
        return agent.workingDirectory;
    case ModelRole:
        return agent.model;
    case EffortRole:
        return agent.effort;
    case AvatarUrlRole:
        return agent.avatarUrl;
    case AvatarSymbolRole:
        return agent.avatarSymbol;
    case StateRole:
        return agent.latestState;
    case StatusTextRole:
        return agent.statusText;
    case LastMessageRole:
        return agent.lastMessage;
    case ConversationIdRole:
        return agent.conversationId;
    case HeadRevisionRole:
        return agent.headRevision;
    case ContextTokensRole:
        return agent.contextTokens;
    case ContextWindowRole:
        return agent.contextWindow;
    case QueueCountRole:
        return agent.queuedTurnCount;
    case AliveRole:
        return agent.alive;
    case BusyRole:
        return agent.busy;
    case FocusedRole:
        return agent.focused;
    case MutedRole:
        return agent.muted;
    case HeartbeatEnabledRole:
        return agent.heartbeatEnabled;
    case DreamingEnabledRole:
        return agent.dreamingEnabled;
    case SchedulesRole:
        return agent.schedules.toVariantList();
    case McpServersRole:
        return agent.mcpServers.toVariantList();
    case UnreadRole:
        return agent.unread;
    default:
        return {};
    }
}

QHash<int, QByteArray> AgentListModel::roleNames() const {
    return {
        {AgentIdRole, "agentId"},
        {SessionRole, "session"},
        {NameRole, "name"},
        {BackendRole, "backend"},
        {WorkingDirectoryRole, "workingDirectory"},
        {ModelRole, "modelName"},
        {EffortRole, "effort"},
        {AvatarUrlRole, "avatarUrl"},
        {AvatarSymbolRole, "avatarSymbol"},
        {StateRole, "agentState"},
        {StatusTextRole, "statusText"},
        {LastMessageRole, "lastMessage"},
        {ConversationIdRole, "conversationId"},
        {HeadRevisionRole, "headRevision"},
        {ContextTokensRole, "contextTokens"},
        {ContextWindowRole, "contextWindow"},
        {QueueCountRole, "queueCount"},
        {AliveRole, "alive"},
        {BusyRole, "busy"},
        {FocusedRole, "focused"},
        {MutedRole, "muted"},
        {HeartbeatEnabledRole, "heartbeatEnabled"},
        {DreamingEnabledRole, "dreamingEnabled"},
        {SchedulesRole, "schedules"},
        {McpServersRole, "mcpServers"},
        {UnreadRole, "unread"},
    };
}

void AgentListModel::applySnapshot(const QJsonObject& snapshot) {
    QVector<Agent> next;
    const QJsonArray rows = snapshot.value(QStringLiteral("agents")).toArray();
    next.reserve(rows.size());
    for (const auto& value : rows) {
        if (!value.isObject()) {
            continue;
        }
        Agent agent = Agent::fromJson(value.toObject());
        if (agent.session.isEmpty() || (m_archivedOnly ? !agent.archived : agent.archived)) {
            continue;
        }
        if (const Agent* old = find(agent.session)) {
            agent.unread = old->unread;
            agent.lastActivity = std::max(agent.lastActivity, old->lastActivity);
            if (!agent.conversationId.isEmpty() &&
                agent.conversationId == old->conversationId &&
                agent.headRevision < old->headRevision) {
                agent.headRevision = old->headRevision;
                agent.conversationId = old->conversationId;
                agent.lastMessage = old->lastMessage;
            }
            if (agent.latestStateTimestamp < old->latestStateTimestamp) {
                agent.latestStateTimestamp = old->latestStateTimestamp;
                agent.latestState = old->latestState;
                agent.statusText = old->statusText;
                agent.busy = old->busy;
            }
        }
        next.append(std::move(agent));
    }

    std::ranges::stable_sort(next, [](const Agent& left, const Agent& right) {
        if (left.lastActivity != right.lastActivity) {
            return left.lastActivity > right.lastActivity;
        }
        const int byName = displayName(left).localeAwareCompare(displayName(right));
        return byName != 0 ? byName < 0 : left.session < right.session;
    });

    QSet<QString> desiredSessions;
    for (const Agent& agent : std::as_const(next)) {
        desiredSessions.insert(agent.session);
    }
    bool structureChanged = false;
    for (int row = static_cast<int>(m_agents.size()) - 1; row >= 0; --row) {
        if (desiredSessions.contains(m_agents.at(row).session)) {
            continue;
        }
        beginRemoveRows({}, row, row);
        m_agents.removeAt(row);
        endRemoveRows();
        structureChanged = true;
    }
    rebuildIndex();

    for (int desiredRow = 0; desiredRow < next.size(); ++desiredRow) {
        const QString session = next.at(desiredRow).session;
        int currentRow = m_bySession.value(session, -1);
        if (currentRow < 0) {
            beginInsertRows({}, desiredRow, desiredRow);
            m_agents.insert(desiredRow, next.at(desiredRow));
            endInsertRows();
            structureChanged = true;
            rebuildIndex();
            continue;
        }
        if (currentRow != desiredRow) {
            const int destination = currentRow < desiredRow ? desiredRow + 1 : desiredRow;
            beginMoveRows({}, currentRow, currentRow, {}, destination);
            m_agents.move(currentRow, desiredRow);
            endMoveRows();
            structureChanged = true;
            rebuildIndex();
        }
        m_agents[desiredRow] = next.at(desiredRow);
        QList<int> roles;
        for (int role = AgentIdRole; role <= UnreadRole; ++role) {
            roles.append(role);
        }
        notifyRow(desiredRow, roles);
    }
    rebuildIndex();
    if (structureChanged) {
        emit countChanged();
    }
}

void AgentListModel::applyStateEvent(const QJsonObject& event) {
    const QString session = event.value(QStringLiteral("session")).toString();
    const int row = m_bySession.value(session, -1);
    if (row < 0) {
        return;
    }
    Agent& agent = *(m_agents.begin() + row);
    agent.latestState = event.value(QStringLiteral("kind")).toString(agent.latestState);
    agent.statusText = event.value(QStringLiteral("status_text")).toString();
    agent.latestStateTimestamp = event.value(QStringLiteral("ts")).toInteger();
    agent.busy = isBusyState(agent.latestState);
    notifyRow(row, {StateRole, StatusTextRole, BusyRole});
}

void AgentListModel::applyFocusEvent(const QJsonObject& event) {
    const QString selectedSession = event.value(QStringLiteral("session")).toString();
    for (qsizetype offset = 0; offset < m_agents.size(); ++offset) {
        Agent& agent = *(m_agents.begin() + offset);
        const bool focused = !selectedSession.isEmpty() && agent.session == selectedSession;
        if (agent.focused == focused) {
            continue;
        }
        agent.focused = focused;
        notifyRow(static_cast<int>(offset), {FocusedRole});
    }
}

void AgentListModel::applyQueueEvent(const QJsonObject& event) {
    const int row = m_bySession.value(event.value(QStringLiteral("session")).toString(), -1);
    if (row < 0) {
        return;
    }
    (*(m_agents.begin() + row)).queuedTurnCount =
        event.value(QStringLiteral("queue_depth")).toInt();
    notifyRow(row, {QueueCountRole});
}

void AgentListModel::applyNotificationEvent(const QJsonObject& event) {
    if (!event.value(QStringLiteral("unread")).toBool(true)) {
        return;
    }
    const int row = m_bySession.value(event.value(QStringLiteral("session")).toString(), -1);
    if (row < 0 || m_agents.at(row).unread) {
        return;
    }
    (*(m_agents.begin() + row)).unread = true;
    notifyRow(row, {UnreadRole});
}

void AgentListModel::clearUnread(const QString& session) {
    const int row = m_bySession.value(session, -1);
    if (row < 0 || !m_agents.at(row).unread) {
        return;
    }
    (*(m_agents.begin() + row)).unread = false;
    notifyRow(row, {UnreadRole});
}

const Agent* AgentListModel::find(const QString& session) const {
    const int row = m_bySession.value(session, -1);
    return row < 0 ? nullptr : &m_agents.at(row);
}

QString AgentListModel::firstSession() const {
    return m_agents.isEmpty() ? QString{} : m_agents.first().session;
}

QStringList AgentListModel::sessions() const {
    QStringList result;
    result.reserve(m_agents.size());
    for (const Agent& agent : m_agents) {
        result.append(agent.session);
    }
    return result;
}

int AgentListModel::indexOfSession(const QString& session) const {
    return m_bySession.value(session, -1);
}

void AgentListModel::rebuildIndex() {
    m_bySession.clear();
    for (int row = 0; row < m_agents.size(); ++row) {
        m_bySession.insert(m_agents.at(row).session, row);
    }
}

void AgentListModel::notifyRow(int row, const QList<int>& roles) {
    const QModelIndex changed = index(row, 0);
    emit dataChanged(changed, changed, roles);
}

} // namespace clarp
