#pragma once

#include "protocol/ProtocolTypes.h"

#include <QAbstractListModel>
#include <QHash>
#include <QJsonObject>
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
    };
    Q_ENUM(Role)

    explicit AgentListModel(QObject* parent = nullptr);
    AgentListModel(bool archivedOnly, QObject* parent);

    [[nodiscard]] int rowCount(const QModelIndex& parent = {}) const override;
    [[nodiscard]] QVariant data(const QModelIndex& index, int role) const override;
    [[nodiscard]] QHash<int, QByteArray> roleNames() const override;

    void applySnapshot(const QJsonObject& snapshot);
    void applyStateEvent(const QJsonObject& event);
    void applyFocusEvent(const QJsonObject& event);
    void applyQueueEvent(const QJsonObject& event);
    void applyNotificationEvent(const QJsonObject& event);
    void clearUnread(const QString& session);

    [[nodiscard]] const Agent* find(const QString& session) const;
    [[nodiscard]] QString firstSession() const;
    [[nodiscard]] QStringList sessions() const;
    Q_INVOKABLE [[nodiscard]] int indexOfSession(const QString& session) const;

  signals:
    void countChanged();

  private:
    void rebuildIndex();
    void notifyRow(int row, const QList<int>& roles);

    QVector<Agent> m_agents;
    QHash<QString, int> m_bySession;
    bool m_archivedOnly = false;
};

} // namespace clarp
