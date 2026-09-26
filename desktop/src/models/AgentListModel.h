#pragma once

#include "models/BackgroundJobTracker.h"
#include "protocol/ProtocolTypes.h"

#include <QAbstractListModel>
#include <QHash>
#include <QJsonObject>
#include <QVariantMap>
#include <QVector>
#include <QtQmlIntegration/qqmlintegration.h>

namespace clarp {

class AgentListModel : public QAbstractListModel {
    Q_OBJECT
    QML_ANONYMOUS
    Q_PROPERTY(int count READ rowCount NOTIFY countChanged)

  public:
    enum Role {
        AgentIdRole = Qt::UserRole + 1,
        SessionRole,
        NameRole,
        BackendRole,
        WorkingDirectoryRole,
        ModelRole,
        EffortRole,
        AvatarUrlRole,
        AvatarSymbolRole,
        StateRole,
        StatusTextRole,
        LastMessageRole,
        LastCompletedMessageRole,
        LastActivityRole,
        ConversationIdRole,
        HeadRevisionRole,
        ContextTokensRole,
        ContextWindowRole,
        QueueCountRole,
        AliveRole,
        BusyRole,
        FocusedRole,
        MutedRole,
        HeartbeatEnabledRole,
        DreamingEnabledRole,
        SchedulesRole,
        McpServersRole,
        UnreadRole,
        ParentAgentIdRole,
        AgentRoleRole,
        HelperStateRole,
        ChildCountRole,
        RunningChildrenRole,
        BackgroundJobCountRole,
        SubAgentCountRole,
        ProcessCountRole,
        LastRole = ProcessCountRole,
    };
    Q_ENUM(Role)

    explicit AgentListModel(QObject* parent = nullptr);
    AgentListModel(bool archivedOnly, QObject* parent);

    [[nodiscard]] int rowCount(const QModelIndex& parent = {}) const override;
    [[nodiscard]] QVariant data(const QModelIndex& index, int role) const override;
    [[nodiscard]] QHash<int, QByteArray> roleNames() const override;

    void applySnapshot(const QJsonObject& snapshot);
    bool upsertCreatedAgent(const QJsonObject& object);
    void applyStateEvent(const QJsonObject& event);
    void applyFocusEvent(const QJsonObject& event);
    void applyQueueEvent(const QJsonObject& event);
    void applyNotificationEvent(const QJsonObject& event);
    void clearUnread(const QString& session);
    void markTransportUnavailable();
    bool recordOutgoingActivity(const QString& session);
    // Live counts from BackgroundJobTracker replace the snapshot's once known.
    void applyLiveJobCounts(const QHash<QString, BackgroundJobCounts>& byAgent);
    void clearLiveJobCounts();

    [[nodiscard]] const Agent* find(const QString& session) const;
    [[nodiscard]] QString nextAttentionSession(const QString& current, const QStringList& pending = {}) const;
    [[nodiscard]] QString firstSession() const;
    [[nodiscard]] QStringList sessions() const;
    Q_INVOKABLE [[nodiscard]] int indexOfSession(const QString& session) const;
    [[nodiscard]] QString displayState(const QString& session) const;
    [[nodiscard]] const Agent* findByAgentId(const QString& agentId) const;
    // Helpers whose parent is `agentId`, in list order.
    [[nodiscard]] QList<const Agent*> helpersOf(const QString& agentId) const;
    [[nodiscard]] BackgroundJobCounts jobCounts(const Agent& agent) const;
    // Running helpers: the Host's `running_children`, or the helpers visible
    // in this list when that is larger (or the Host predates the field).
    [[nodiscard]] int runningChildren(const Agent& agent) const;

  signals:
    void countChanged();

  private:
    void rebuildIndex();
    void notifyRow(int row, const QList<int>& roles);
    void recountHelpers();

    QVector<Agent> m_agents;
    QHash<QString, int> m_bySession;
    QHash<QString, QJsonObject> m_pendingQueueEvents;
    QHash<QString, QPair<quint64, qint64>> m_outgoingRanks;
    QHash<QString, BackgroundJobCounts> m_liveJobCounts;
    QHash<QString, int> m_runningHelpersByParent;
    bool m_liveJobsKnown = false;
    quint64 m_outgoingCounter = 0;
    bool m_archivedOnly = false;
    bool m_transportAvailable = false;
};

// What the process popover lists for one agent: `jobs` (active background
// jobs with kind, title, detail, elapsed and heartbeat labels) and `helpers`
// (running child helpers with session and name), plus the counts behind the
// list and header indicators.
[[nodiscard]] QVariantMap describeAgentProcesses(const AgentListModel& agents,
                                                 const BackgroundJobTracker& jobs,
                                                 const QString& session, qint64 nowMs);

} // namespace clarp
