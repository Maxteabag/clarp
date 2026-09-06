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
    if (m_showWhenReady && role == ConversationModel::BodyRole
        && !QSortFilterProxyModel::data(item, ConversationModel::ActivityRole).toBool()
        && QSortFilterProxyModel::data(item, ConversationModel::AuthorRole).toString() == QStringLiteral("assistant")
        && QSortFilterProxyModel::data(item, ConversationModel::KindRole).toString() == QStringLiteral("live"))
        return QString{};
    return QSortFilterProxyModel::data(item, role);
}
int ConversationPresentationModel::indexOfMessage(const QString& id) const {
    const auto* source = qobject_cast<const ConversationModel*>(sourceModel());
    if (!source) return -1;
    const int row = source->indexOfMessage(id);
    return row < 0 ? -1 : mapFromSource(source->index(row, 0)).row();
}
}
