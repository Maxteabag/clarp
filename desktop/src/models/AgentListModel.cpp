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
        return m_transportAvailable ? agent.latestState : QStringLiteral("offline");
    case StatusTextRole:
        return m_transportAvailable ? agent.statusText : QString{};
    case LastActivityRole:
        return agent.lastActivity;
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
        return m_transportAvailable && agent.alive;
    case BusyRole:
        return m_transportAvailable && agent.busy;
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
        {LastActivityRole, "lastActivity"},
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
    m_transportAvailable = true;
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
        if (const auto rank = m_outgoingRanks.find(agent.session);
            rank != m_outgoingRanks.end() && agent.lastActivity > rank->second) {
            m_outgoingRanks.erase(rank);
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
            if (agent.queueRevision < old->queueRevision) {
                agent.queueRevision = old->queueRevision;
                agent.queuedTurnCount = old->queuedTurnCount;
            }
        }
        if (const auto pending = m_pendingQueueEvents.find(agent.session);
            pending != m_pendingQueueEvents.end()) {
            const qint64 revision = pending->value(QStringLiteral("queue_revision")).toInteger();
            if (revision >= agent.queueRevision) {
                agent.queueRevision = revision;
                agent.queuedTurnCount = pending->value(QStringLiteral("queue_depth")).toInt();
            }
            m_pendingQueueEvents.erase(pending);
        }
        next.append(std::move(agent));
    }

    std::ranges::stable_sort(next, [this](const Agent& left, const Agent& right) {
        const quint64 leftRank = m_outgoingRanks.value(left.session).first;
        const quint64 rightRank = m_outgoingRanks.value(right.session).first;
        if (leftRank != rightRank) {
            return leftRank > rightRank;
        }
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
    for (auto it = m_outgoingRanks.begin(); it != m_outgoingRanks.end();) {
        if (!desiredSessions.contains(it.key())) {
            it = m_outgoingRanks.erase(it);
        } else {
            ++it;
        }
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
        m_agents.replace(desiredRow, next.at(desiredRow));
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
    const QString session = event.value(QStringLiteral("session")).toString();
    const int row = m_bySession.value(session, -1);
    if (row < 0) {
        const qint64 incomingRevision =
            event.value(QStringLiteral("queue_revision")).toInteger();
        const qint64 pendingRevision =
            m_pendingQueueEvents.value(session).value(QStringLiteral("queue_revision")).toInteger();
        if (!session.isEmpty() && incomingRevision >= pendingRevision) {
            m_pendingQueueEvents.insert(session, event);
        }
        return;
    }
    Agent& agent = *(m_agents.begin() + row);
    const qint64 revision = event.value(QStringLiteral("queue_revision")).toInteger();
    if ((revision == 0 && agent.queueRevision > 0) || revision < agent.queueRevision) {
        return;
    }
    agent.queueRevision = revision;
    agent.queuedTurnCount = event.value(QStringLiteral("queue_depth")).toInt();
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

void AgentListModel::markTransportUnavailable() {
    if (!m_transportAvailable) {
        return;
    }
    m_transportAvailable = false;
    if (!m_agents.isEmpty()) {
        emit dataChanged(index(0, 0), index(static_cast<int>(m_agents.size()) - 1, 0),
                         {StateRole, StatusTextRole, AliveRole, BusyRole});
    }
}

bool AgentListModel::recordOutgoingActivity(const QString& session) {
    const int row = m_bySession.value(session, -1);
    if (row < 0) {
        return false;
    }
    m_outgoingRanks.insert(
        session, {++m_outgoingCounter, m_agents.at(row).lastActivity});
    if (row > 0) {
        beginMoveRows({}, row, row, {}, 0);
        m_agents.move(row, 0);
        endMoveRows();
        rebuildIndex();
    }
    return true;
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

QString AgentListModel::displayState(const QString& session) const {
    const Agent* agent = find(session);
    if (agent == nullptr) {
        return {};
    }
    return m_transportAvailable ? agent->latestState : QStringLiteral("offline");
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
