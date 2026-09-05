#include "models/AgentFilterModel.h"

#include "models/AgentListModel.h"

#include <algorithm>

namespace clarp {

AgentFilterModel::AgentFilterModel(QObject* parent) : QSortFilterProxyModel(parent) {
    connect(this, &QAbstractItemModel::rowsInserted, this, &AgentFilterModel::countChanged);
    connect(this, &QAbstractItemModel::rowsRemoved, this, &AgentFilterModel::countChanged);
    connect(this, &QAbstractItemModel::modelReset, this, &AgentFilterModel::countChanged);
}

QString AgentFilterModel::query() const { return m_query; }

bool AgentFilterModel::unreadOnly() const { return m_unreadOnly; }

void AgentFilterModel::setQuery(const QString& query) {
    if (m_query == query) {
        return;
    }
    m_query = query;
    beginFilterChange();
    endFilterChange(QSortFilterProxyModel::Direction::Rows);
    emit queryChanged();
    emit countChanged();
}

void AgentFilterModel::setUnreadOnly(bool unreadOnly) {
    if (m_unreadOnly == unreadOnly) {
        return;
    }
    m_unreadOnly = unreadOnly;
    beginFilterChange();
    endFilterChange(QSortFilterProxyModel::Direction::Rows);
    emit unreadOnlyChanged();
    emit countChanged();
}

bool AgentFilterModel::filterAcceptsRow(int sourceRow, const QModelIndex& sourceParent) const {
    const QAbstractItemModel* source = sourceModel();
    if (source == nullptr) {
        return false;
    }
    const QModelIndex row = source->index(sourceRow, 0, sourceParent);
    if (m_unreadOnly && !row.data(AgentListModel::UnreadRole).toBool()) {
        return false;
    }
    const QString needle = m_query.trimmed();
    if (needle.isEmpty()) {
        return true;
    }
    const QList<int> searched{AgentListModel::NameRole, AgentListModel::BackendRole,
                              AgentListModel::SessionRole, AgentListModel::LastMessageRole,
                              AgentListModel::WorkingDirectoryRole};
    return std::ranges::any_of(searched, [&row, &needle](int role) {
        return row.data(role).toString().contains(needle, Qt::CaseInsensitive);
    });
}

} // namespace clarp
