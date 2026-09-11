#pragma once
#include <QSortFilterProxyModel>
#include <QDateTime>
#include <QSet>
#include <functional>
#include <QVariantMap>
#include <QtQmlIntegration/qqmlintegration.h>
namespace clarp {
class ConversationPresentationModel : public QSortFilterProxyModel {
    Q_OBJECT
    QML_ELEMENT
    Q_PROPERTY(bool showWhenReady READ showWhenReady WRITE setShowWhenReady NOTIFY showWhenReadyChanged)
    Q_PROPERTY(int count READ rowCount NOTIFY countChanged)
    Q_PROPERTY(int activityMode READ activityMode WRITE setActivityMode NOTIFY countChanged)
  public:
    explicit ConversationPresentationModel(QObject* parent = nullptr);
    bool showWhenReady() const { return m_showWhenReady; }
    void setShowWhenReady(bool value);
    QVariant data(const QModelIndex& item, int role) const override;
    Q_INVOKABLE int indexOfMessage(const QString& id) const;
    enum GroupRole { GroupIdsRole = Qt::UserRole + 100, GroupLabelRole, GroupExpandedRole, ActivityInlineRole, ActivityLabelRole, ExplanationRepeatRole };
    QHash<int, QByteArray> roleNames() const override;
    int activityMode() const { return m_activityMode; }
    void setActivityMode(int mode);
    void setSourceModel(QAbstractItemModel* model) override;
    Q_INVOKABLE void beginVisit();
    // Lookup is cache-only: grouping must never create explanation demand.
    void setExplanationLookup(std::function<QString(const QVariantMap&)> lookup);
    Q_INVOKABLE void updateExplanations(QObject* narrator, const QString& session, const QString& directory, bool localFiles);
    Q_INVOKABLE void toggleGroup(const QString& id);
  signals:
    void showWhenReadyChanged();
    void countChanged();
    void rowsAppended(bool fromCurrentUser);
  protected:
    bool filterAcceptsRow(int row, const QModelIndex& parent) const override;
  private:
    bool m_showWhenReady = false;
    int m_activityMode = 1;
    QDateTime m_visitStarted = QDateTime::currentDateTimeUtc();
    QSet<QString> m_expanded;
    mutable QSet<QString> m_observedLive;
    bool groupedRow(int row) const;
    bool inlineRow(const QModelIndex& row) const;
    QList<QModelIndex> groupRows(int row) const;
    QString activityLabel(const QList<QModelIndex>& rows) const;
    void refreshGroups();
    QVariant presentationData(const QModelIndex& source, int role) const;
    void rebuildExplanationRuns();
    std::function<QString(const QVariantMap&)> m_explanationLookup;
    QHash<int, QHash<int, QVariant>> m_explanationRows;
    QSet<int> m_repeatedRows;
};
}
