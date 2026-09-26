#pragma once

#include <QHash>
#include <QSet>
#include <QSortFilterProxyModel>
#include <QVariantList>
#include <QtQmlIntegration/qqmlintegration.h>

namespace clarp {

/// Sidebar view over the agent roster: the search field's text and the
/// All/Unread scope chip, applied without disturbing the roster model that the
/// overview, quick switcher, and pane workspace share.
///
/// Without a search or scope filter, helpers (`role == "helper"` with a
/// `parent_agent_id` present in the list) nest under their parent using the
/// team tree walk, and finished helpers collapse into one "N helpers done"
/// line carried by the row just above them (`doneHelpers`).
class AgentFilterModel : public QSortFilterProxyModel {
    Q_OBJECT
    QML_ELEMENT
    Q_PROPERTY(QString query READ query WRITE setQuery NOTIFY queryChanged)
    Q_PROPERTY(bool unreadOnly READ unreadOnly WRITE setUnreadOnly NOTIFY unreadOnlyChanged)
    Q_PROPERTY(int count READ rowCount NOTIFY countChanged)

  public:
    enum Role {
        TreeDepthRole = Qt::UserRole + 500,
        DoneHelpersRole,
    };

    explicit AgentFilterModel(QObject* parent = nullptr);

    [[nodiscard]] QString query() const;
    [[nodiscard]] bool unreadOnly() const;

    Q_INVOKABLE [[nodiscard]] int indexOfSession(const QString& session) const;
    Q_INVOKABLE void toggleDoneHelpers(const QString& parentAgentId);
    // Expands whatever hides `session`, so selecting a finished helper
    // (from the switcher or a notification) can still highlight its row.
    Q_INVOKABLE void revealSession(const QString& session);

    void setQuery(const QString& query);
    void setUnreadOnly(bool unreadOnly);
    void setSourceModel(QAbstractItemModel* sourceModel) override;

    [[nodiscard]] QVariant data(const QModelIndex& index, int role) const override;
    [[nodiscard]] QHash<int, QByteArray> roleNames() const override;

  signals:
    void queryChanged();
    void unreadOnlyChanged();
    void countChanged();

  protected:
    [[nodiscard]] bool filterAcceptsRow(int sourceRow,
                                        const QModelIndex& sourceParent) const override;
    [[nodiscard]] bool lessThan(const QModelIndex& left, const QModelIndex& right) const override;

  private:
    struct Tree {
        QHash<QString, int> position;
        QHash<QString, int> depth;
        QSet<QString> hidden;
        QHash<QString, QVariantList> footers;
        QHash<QString, QString> hidingParent;

        bool operator==(const Tree&) const = default;
    };

    [[nodiscard]] bool treeActive() const;
    void rebuildTree();
    void refreshTree();

    QString m_query;
    bool m_unreadOnly = false;
    Tree m_tree;
    QSet<QString> m_expandedParents;
    QList<QMetaObject::Connection> m_sourceConnections;
};

} // namespace clarp
