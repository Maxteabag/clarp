#pragma once

#include <QSortFilterProxyModel>
#include <QtQmlIntegration/qqmlintegration.h>

namespace clarp {

/// Sidebar view over the agent roster: the search field's text and the
/// All/Unread scope chip, applied without disturbing the roster model that the
/// overview, quick switcher, and pane workspace share.
class AgentFilterModel : public QSortFilterProxyModel {
    Q_OBJECT
    QML_ELEMENT
    Q_PROPERTY(QString query READ query WRITE setQuery NOTIFY queryChanged)
    Q_PROPERTY(bool unreadOnly READ unreadOnly WRITE setUnreadOnly NOTIFY unreadOnlyChanged)
    Q_PROPERTY(int count READ rowCount NOTIFY countChanged)

  public:
    explicit AgentFilterModel(QObject* parent = nullptr);

    [[nodiscard]] QString query() const;
    [[nodiscard]] bool unreadOnly() const;

    void setQuery(const QString& query);
    void setUnreadOnly(bool unreadOnly);

  signals:
    void queryChanged();
    void unreadOnlyChanged();
    void countChanged();

  protected:
    [[nodiscard]] bool filterAcceptsRow(int sourceRow,
                                        const QModelIndex& sourceParent) const override;

  private:
    QString m_query;
    bool m_unreadOnly = false;
};

} // namespace clarp
