#include "models/AgentFilterModel.h"

#include "models/AgentListModel.h"
#include "models/TreeOrder.h"

#include <algorithm>

namespace clarp {
namespace {

bool finishedHelperState(const QString& state) {
    return state == QStringLiteral("done") || state == QStringLiteral("reported") ||
           state == QStringLiteral("abandoned");
}

} // namespace

AgentFilterModel::AgentFilterModel(QObject* parent) : QSortFilterProxyModel(parent) {
    connect(this, &QAbstractItemModel::rowsInserted, this, &AgentFilterModel::countChanged);
    connect(this, &QAbstractItemModel::rowsRemoved, this, &AgentFilterModel::countChanged);
    connect(this, &QAbstractItemModel::modelReset, this, &AgentFilterModel::countChanged);
    connect(this, &QAbstractItemModel::layoutChanged, this, &AgentFilterModel::countChanged);
    sort(0);
}

void AgentFilterModel::setSourceModel(QAbstractItemModel* source) {
    for (const auto& connection : std::as_const(m_sourceConnections)) disconnect(connection);
    m_sourceConnections.clear();
    QSortFilterProxyModel::setSourceModel(source);
    if (source != nullptr) {
        // Connected after the base class so its own bookkeeping has already
        // run; the tree is then recomputed and applied only if it changed.
        const auto refresh = [this] { refreshTree(); };
        m_sourceConnections = {
            connect(source, &QAbstractItemModel::rowsInserted, this, refresh),
            connect(source, &QAbstractItemModel::rowsRemoved, this, refresh),
            connect(source, &QAbstractItemModel::rowsMoved, this, refresh),
            connect(source, &QAbstractItemModel::modelReset, this, refresh),
            connect(source, &QAbstractItemModel::layoutChanged, this, refresh),
            connect(source, &QAbstractItemModel::dataChanged, this, refresh),
        };
    }
    refreshTree();
}

int AgentFilterModel::indexOfSession(const QString& session) const {
    for (int row = 0; row < rowCount(); ++row) {
        if (index(row, 0).data(AgentListModel::SessionRole).toString() == session)
            return row;
    }
    return -1;
}

void AgentFilterModel::toggleDoneHelpers(const QString& parentAgentId) {
    if (parentAgentId.isEmpty()) return;
    if (!m_expandedParents.remove(parentAgentId)) m_expandedParents.insert(parentAgentId);
    refreshTree();
}

void AgentFilterModel::revealSession(const QString& session) {
    const QString parent = m_tree.hidingParent.value(session);
    if (parent.isEmpty()) return;
    m_expandedParents.insert(parent);
    refreshTree();
}

QString AgentFilterModel::query() const { return m_query; }

bool AgentFilterModel::unreadOnly() const { return m_unreadOnly; }

void AgentFilterModel::setQuery(const QString& query) {
    if (m_query == query) {
        return;
    }
    m_query = query;
    rebuildTree();
    invalidate();
    emit queryChanged();
    emit countChanged();
}

void AgentFilterModel::setUnreadOnly(bool unreadOnly) {
    if (m_unreadOnly == unreadOnly) {
        return;
    }
    m_unreadOnly = unreadOnly;
    rebuildTree();
    invalidate();
    emit unreadOnlyChanged();
    emit countChanged();
}

bool AgentFilterModel::treeActive() const { return !m_unreadOnly && m_query.trimmed().isEmpty(); }

void AgentFilterModel::rebuildTree() {
    Tree tree;
    const QAbstractItemModel* source = sourceModel();
    if (source == nullptr) {
        m_tree = tree;
        return;
    }
    const int rows = source->rowCount();
    QVector<TreeNode> nodes;
    QStringList sessions;
    QStringList agentIds;
    QVector<bool> finished;
    nodes.reserve(rows);
    for (int row = 0; row < rows; ++row) {
        const QModelIndex index = source->index(row, 0);
        const QString agentId = index.data(AgentListModel::AgentIdRole).toString();
        const bool helper =
            index.data(AgentListModel::AgentRoleRole).toString() == QStringLiteral("helper");
        const QString parent =
            helper ? index.data(AgentListModel::ParentAgentIdRole).toString() : QString{};
        const bool done = helper && !parent.isEmpty() &&
                          finishedHelperState(index.data(AgentListModel::HelperStateRole).toString());
        nodes.append({treeActive() ? agentId : QString{}, treeActive() ? parent : QString{},
                      done ? 1 : 0});
        sessions.append(index.data(AgentListModel::SessionRole).toString());
        agentIds.append(agentId);
        finished.append(done);
    }

    const QVector<TreePlacement> order = treeOrder(nodes);
    for (int position = 0; position < order.size(); ++position) {
        const TreePlacement& placement = order.at(position);
        tree.position.insert(sessions.at(placement.index), position);
        tree.depth.insert(sessions.at(placement.index), placement.depth);
    }
    if (!treeActive()) {
        m_tree = tree;
        return;
    }

    // Placed finished children of each parent (the walk puts them after the
    // parent's running children), in display order.
    QHash<QString, QVector<int>> doneByParent;
    QStringList parentOrder;
    QHash<QString, int> byAgentId;
    for (int row = 0; row < rows; ++row) {
        if (!agentIds.at(row).isEmpty()) byAgentId.insert(agentIds.at(row), row);
    }
    for (int position = 0; position < order.size(); ++position) {
        const int row = order.at(position).index;
        const QString& parent = nodes.at(row).parentId;
        if (!finished.at(row) || order.at(position).depth == 0 || !byAgentId.contains(parent))
            continue;
        if (!doneByParent.contains(parent)) parentOrder.append(parent);
        doneByParent[parent].append(position);
    }
    // Hiding is transitive: a helper under a collapsed finished helper is
    // hidden too, and revealing it expands the collapsed ancestor.
    QVector<QString> hiddenBy(order.size());
    for (int position = 0; position < order.size(); ++position) {
        const int row = order.at(position).index;
        const QString& parent = nodes.at(row).parentId;
        if (order.at(position).depth == 0) continue;
        const int parentRow = byAgentId.value(parent, -1);
        const int parentPosition =
            parentRow >= 0 ? tree.position.value(sessions.at(parentRow), -1) : -1;
        if (parentPosition >= 0 && !hiddenBy.at(parentPosition).isEmpty()) {
            hiddenBy[position] = hiddenBy.at(parentPosition);
        } else if (finished.at(row) && !m_expandedParents.contains(parent)) {
            hiddenBy[position] = parent;
        }
        if (!hiddenBy.at(position).isEmpty()) {
            tree.hidden.insert(sessions.at(row));
            tree.hidingParent.insert(sessions.at(row), hiddenBy.at(position));
        }
    }
    for (const QString& parent : std::as_const(parentOrder)) {
        const QVector<int>& positions = doneByParent.value(parent);
        // Attach the line to the nearest visible row above the first finished
        // child: the parent itself or its last running descendant.
        int anchor = positions.first() - 1;
        while (anchor >= 0 && !hiddenBy.at(anchor).isEmpty()) --anchor;
        if (anchor < 0) continue;
        const QString anchorSession = sessions.at(order.at(anchor).index);
        if (tree.hidden.contains(sessions.at(byAgentId.value(parent)))) continue;
        tree.footers[anchorSession].append(QVariantMap{
            {QStringLiteral("parentAgentId"), parent},
            {QStringLiteral("count"), static_cast<int>(positions.size())},
            {QStringLiteral("expanded"), m_expandedParents.contains(parent)},
            {QStringLiteral("depth"), order.at(anchor).depth},
        });
    }
    m_tree = tree;
}

void AgentFilterModel::refreshTree() {
    const Tree before = m_tree;
    rebuildTree();
    if (before == m_tree) return;
    invalidate();
    if (rowCount() > 0) {
        emit dataChanged(index(0, 0), index(rowCount() - 1, 0), {TreeDepthRole, DoneHelpersRole});
    }
}

QVariant AgentFilterModel::data(const QModelIndex& index, int role) const {
    if (role == TreeDepthRole || role == DoneHelpersRole) {
        const QString session = QSortFilterProxyModel::data(index, AgentListModel::SessionRole).toString();
        if (role == TreeDepthRole) return m_tree.depth.value(session);
        return m_tree.footers.value(session);
    }
    return QSortFilterProxyModel::data(index, role);
}

QHash<int, QByteArray> AgentFilterModel::roleNames() const {
    QHash<int, QByteArray> names = QSortFilterProxyModel::roleNames();
    names.insert(TreeDepthRole, "treeDepth");
    names.insert(DoneHelpersRole, "doneHelpers");
    return names;
}

bool AgentFilterModel::lessThan(const QModelIndex& left, const QModelIndex& right) const {
    const QString leftSession = left.data(AgentListModel::SessionRole).toString();
    const QString rightSession = right.data(AgentListModel::SessionRole).toString();
    const int leftPosition = m_tree.position.value(leftSession, left.row());
    const int rightPosition = m_tree.position.value(rightSession, right.row());
    if (leftPosition != rightPosition) return leftPosition < rightPosition;
    return left.row() < right.row();
}

bool AgentFilterModel::filterAcceptsRow(int sourceRow, const QModelIndex& sourceParent) const {
    const QAbstractItemModel* source = sourceModel();
    if (source == nullptr) {
        return false;
    }
    const QModelIndex row = source->index(sourceRow, 0, sourceParent);
    if (treeActive() &&
        m_tree.hidden.contains(row.data(AgentListModel::SessionRole).toString())) {
        return false;
    }
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
