#pragma once

#include "protocol/ProtocolTypes.h"

#include <QAbstractListModel>
#include <QHash>
#include <QJsonObject>
#include <QVector>
#include <QtQmlIntegration/qqmlintegration.h>

namespace clarp {

class ConversationModel : public QAbstractListModel {
    Q_OBJECT
    QML_ANONYMOUS
    Q_PROPERTY(QString session READ session NOTIFY sessionChanged)
    Q_PROPERTY(QString conversationId READ conversationId NOTIFY conversationIdChanged)
    Q_PROPERTY(qint64 latestRevision READ latestRevision NOTIFY latestRevisionChanged)
    Q_PROPERTY(bool hasMore READ hasMore NOTIFY hasMoreChanged)
    Q_PROPERTY(bool loading READ loading WRITE setLoading NOTIFY loadingChanged)
    Q_PROPERTY(QString error READ error WRITE setError NOTIFY errorChanged)
    Q_PROPERTY(int count READ rowCount NOTIFY countChanged)

  public:
    enum Role {
        MessageIdRole = Qt::UserRole + 1,
        AuthorRole,
        BodyRole,
        TimestampRole,
        RevisionRole,
        KindRole,
        ToolNameRole,
        OriginRole,
        SenderNameRole,
        PendingRole,
        DeliveryFailedRole,
        ActivityRole,
        ToolsRole,
        DisplayCellsRole,
    };
    Q_ENUM(Role)

    enum class LoadKind { Tail, Delta, Older };

    explicit ConversationModel(QObject* parent = nullptr);

    [[nodiscard]] int rowCount(const QModelIndex& parent = {}) const override;
    [[nodiscard]] QVariant data(const QModelIndex& index, int role) const override;
    [[nodiscard]] QHash<int, QByteArray> roleNames() const override;

    [[nodiscard]] QString session() const;
    [[nodiscard]] QString conversationId() const;
    [[nodiscard]] qint64 latestRevision() const;
    [[nodiscard]] bool hasMore() const;
    [[nodiscard]] bool loading() const;
    [[nodiscard]] QString error() const;

    void openSession(const QString& session);
    void applyLog(const QJsonObject& response, LoadKind kind);
    void addOptimistic(const QString& clientMessageId, const QString& text);
    void markDeliveryFailed(const QString& clientMessageId);
    void applyActivityEvent(const QJsonObject& event);
    void clearActivity();
    void setLoading(bool loading);
    void setError(const QString& error);

  signals:
    void sessionChanged();
    void conversationIdChanged();
    void latestRevisionChanged();
    void hasMoreChanged();
    void loadingChanged();
    void errorChanged();
    void countChanged();
    void replacementRequired();
    void deliveryConfirmed(const QString& clientMessageId);

  private:
    void rebuildIndex();
    void replaceRows(QVector<Message> rows);
    void mergeRows(const QJsonArray& rows);
    void sortRows();

    QString m_session;
    QString m_conversationId;
    qint64 m_latestRevision = 0;
    bool m_hasMore = false;
    bool m_loading = false;
    QString m_error;
    QVector<Message> m_messages;
    QHash<QString, int> m_byId;
};

} // namespace clarp
