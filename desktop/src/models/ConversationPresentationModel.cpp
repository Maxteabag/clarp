#include "models/ConversationPresentationModel.h"
#include "models/ConversationModel.h"
namespace clarp {
ConversationPresentationModel::ConversationPresentationModel(QObject* parent) : QSortFilterProxyModel(parent) {
    connect(this, &QAbstractItemModel::modelReset, this, &ConversationPresentationModel::countChanged);
    connect(this, &QAbstractItemModel::rowsRemoved, this, &ConversationPresentationModel::countChanged);
    connect(this, &QAbstractItemModel::rowsInserted, this, [this](const QModelIndex&, int first, int last) {
        emit countChanged();
        if (last == rowCount() - 1)
            emit rowsAppended(first == last && data(index(first, 0), ConversationModel::PendingRole).toBool());
    });
}
void ConversationPresentationModel::setShowWhenReady(bool value) {
    if (m_showWhenReady == value) return;
    m_showWhenReady = value;
    beginFilterChange();
    endFilterChange(QSortFilterProxyModel::Direction::Rows);
    if (rowCount() > 0) emit dataChanged(index(0, 0), index(rowCount() - 1, 0),
        {ConversationModel::BodyRole});
    emit showWhenReadyChanged();
}
bool ConversationPresentationModel::filterAcceptsRow(int row, const QModelIndex& parent) const {
    if (groupedRow(row) && row > 0 && groupedRow(row - 1)) return false;
    if (!m_showWhenReady) return true;
    const QModelIndex item = sourceModel()->index(row, 0, parent);
    if (item.data(ConversationModel::ActivityRole).toBool()) {
        const QString label = item.data(ConversationModel::ToolNameRole).toString();
        // The typing indicator already represents these lifecycle placeholders.
        return label != QStringLiteral("thinking") && label != QStringLiteral("compacting");
    }
    if (item.data(ConversationModel::AuthorRole).toString() != QStringLiteral("assistant")) return true;
    const bool hasTools = !item.data(ConversationModel::ToolsRole).toList().isEmpty()
        || !item.data(ConversationModel::DisplayCellsRole).toList().isEmpty()
        || item.data(ConversationModel::ActivityCountRole).toInt() > 0;
    return hasTools || (item.data(ConversationModel::KindRole).toString() != QStringLiteral("live")
        && !item.data(ConversationModel::BodyRole).toString().isEmpty());
}
QVariant ConversationPresentationModel::data(const QModelIndex& item, int role) const {
    const QModelIndex source = mapToSource(item);
    if (role == ActivityInlineRole) return inlineRow(source);
    const auto rows = groupedRow(source.row()) ? groupRows(source.row()) : QList<QModelIndex>{};
    if (!rows.isEmpty()) {
        const bool expanded = m_expanded.contains(source.data(ConversationModel::MessageIdRole).toString());
        if (role == GroupExpandedRole) return expanded;
        if (role == GroupIdsRole) {
            QStringList ids;
            for (const auto& row : rows) if (row.data(ConversationModel::ToolDetailsAvailableRole).toBool())
                ids.append(row.data(ConversationModel::MessageIdRole).toString());
            return ids;
        }
        if (role == GroupLabelRole || role == ConversationModel::ActivityCountRole) {
            int count = 0;
            for (const auto& row : rows) count += std::max({1, row.data(ConversationModel::ActivityCountRole).toInt(),
                static_cast<int>(row.data(ConversationModel::ToolsRole).toList().size()),
                static_cast<int>(row.data(ConversationModel::DisplayCellsRole).toList().size())});
            if (role == ConversationModel::ActivityCountRole) return count;
            QString label = QStringLiteral("%1 tool calls").arg(count);
            const auto start = QDateTime::fromString(rows.first().data(ConversationModel::TimestampRole).toString(), Qt::ISODateWithMs);
            const auto end = QDateTime::fromString(rows.last().data(ConversationModel::TimestampRole).toString(), Qt::ISODateWithMs);
            const qint64 seconds = start.secsTo(end);
            if (start.isValid() && end.isValid() && seconds > 0)
                label += QStringLiteral(" · %1h %2m %3s").arg(seconds / 3600).arg(seconds / 60 % 60).arg(seconds % 60);
            return label;
        }
        if (role == ConversationModel::BodyRole) return QString{};
        if (role == ConversationModel::ActivityRole) return false;
        if (role == ConversationModel::ToolsRole || role == ConversationModel::DisplayCellsRole) {
            QVariantList values;
            if (expanded) for (const auto& row : rows) {
                values.append(row.data(role).toList());
                if (role == ConversationModel::ToolsRole && row.data(ConversationModel::ActivityRole).toBool())
                    values.append(QVariantMap{{QStringLiteral("name"), row.data(ConversationModel::ToolNameRole)},
                        {QStringLiteral("summary"), row.data(ConversationModel::BodyRole)}});
            }
            return values;
        }
    }
    if (role == GroupIdsRole) return QStringList{};
    if (role == GroupLabelRole) return QString{};
    if (role == GroupExpandedRole) return false;
    if (m_showWhenReady && role == ConversationModel::BodyRole
        && !QSortFilterProxyModel::data(item, ConversationModel::ActivityRole).toBool()
        && QSortFilterProxyModel::data(item, ConversationModel::AuthorRole).toString() == QStringLiteral("assistant")
        && QSortFilterProxyModel::data(item, ConversationModel::KindRole).toString() == QStringLiteral("live"))
        return QString{};
    return QSortFilterProxyModel::data(item, role);
}
QHash<int, QByteArray> ConversationPresentationModel::roleNames() const {
    auto roles = QSortFilterProxyModel::roleNames();
    roles.insert(GroupIdsRole, "groupIds"); roles.insert(GroupLabelRole, "groupLabel");
    roles.insert(GroupExpandedRole, "groupExpanded"); roles.insert(ActivityInlineRole, "activityInline");
    return roles;
}
bool ConversationPresentationModel::inlineRow(const QModelIndex& row) const {
    if (m_activityMode == 1) return true;
    if (m_activityMode != 2) return false;
    const auto time = QDateTime::fromString(row.data(ConversationModel::TimestampRole).toString(), Qt::ISODateWithMs);
    const auto id = row.data(ConversationModel::MessageIdRole).toString();
    if ((time.isValid() && time >= m_visitStarted) || row.data(ConversationModel::KindRole).toString() == QStringLiteral("live")
        || row.data(ConversationModel::ActivityStatusRole).toString() == QStringLiteral("running")) m_observedLive.insert(id);
    return m_observedLive.contains(id);
}
bool ConversationPresentationModel::groupedRow(int row) const {
    if (!sourceModel() || row < 0 || row >= sourceModel()->rowCount()) return false;
    const auto item = sourceModel()->index(row, 0);
    if (inlineRow(item)) return false;
    if (item.data(ConversationModel::ActivityRole).toBool()) {
        const auto label = item.data(ConversationModel::ToolNameRole).toString();
        return label != QStringLiteral("thinking") && label != QStringLiteral("compacting");
    }
    if (!item.data(ConversationModel::OriginRole).toString().isEmpty()
        || item.data(ConversationModel::AutomatedRole).toBool()) return false;
    return item.data(ConversationModel::AuthorRole).toString() == QStringLiteral("assistant")
        && item.data(ConversationModel::BodyRole).toString().isEmpty()
        && (item.data(ConversationModel::ActivityCountRole).toInt() > 0
            || !item.data(ConversationModel::ToolsRole).toList().isEmpty()
            || !item.data(ConversationModel::DisplayCellsRole).toList().isEmpty());
}
QList<QModelIndex> ConversationPresentationModel::groupRows(int row) const {
    QList<QModelIndex> rows;
    while (groupedRow(row)) rows.append(sourceModel()->index(row++, 0));
    return rows;
}
void ConversationPresentationModel::refreshGroups() {
    beginFilterChange(); endFilterChange(QSortFilterProxyModel::Direction::Rows);
    if (rowCount()) emit dataChanged(index(0, 0), index(rowCount() - 1, 0));
}
void ConversationPresentationModel::setActivityMode(int mode) {
    if (m_activityMode == mode) return;
    m_activityMode = mode; refreshGroups(); emit countChanged();
}
void ConversationPresentationModel::beginVisit() {
    m_visitStarted = QDateTime::currentDateTimeUtc(); m_expanded.clear(); m_observedLive.clear(); refreshGroups();
}
void ConversationPresentationModel::setSourceModel(QAbstractItemModel* model) {
    if (sourceModel()) disconnect(sourceModel(), nullptr, this, nullptr);
    QSortFilterProxyModel::setSourceModel(model);
    if (model) {
        connect(model, &QAbstractItemModel::dataChanged, this, [this](const QModelIndex& first, const QModelIndex& last) {
            if (m_activityMode == 1) return;
            for (int row = first.row(); row <= last.row(); ++row)
                if (groupedRow(row) || groupedRow(row - 1)) { refreshGroups(); break; }
        });
        connect(model, &QAbstractItemModel::rowsInserted, this, [this] { if (m_activityMode != 1) refreshGroups(); });
        connect(model, &QAbstractItemModel::rowsRemoved, this, [this] { if (m_activityMode != 1) refreshGroups(); });
    }
    beginVisit();
}
void ConversationPresentationModel::toggleGroup(const QString& id) {
    if (!m_expanded.remove(id)) m_expanded.insert(id);
    refreshGroups();
}
int ConversationPresentationModel::indexOfMessage(const QString& id) const {
    const auto* source = qobject_cast<const ConversationModel*>(sourceModel());
    if (!source) return -1;
    int row = source->indexOfMessage(id);
    if (groupedRow(row)) while (row > 0 && groupedRow(row - 1)) --row;
    return row < 0 ? -1 : mapFromSource(source->index(row, 0)).row();
}
}
