#include "models/ConversationPresentationModel.h"
#include "models/ConversationModel.h"
#include "app/ToolNarrator.h"
#include <QPointer>
namespace clarp {
namespace {
// The Host stamps every ordinary row "user" and uses a dispatcher name
// ("agent", "heartbeat", "janitor", "automation", ...) for anything it sent on
// someone else's behalf. Only an unstamped fixture row is ever empty, so
// testing for emptiness alone treated the entire live transcript as foreign
// and left every activity row in a group of its own.
bool ownTurn(const QModelIndex& row) {
    const auto origin = row.data(ConversationModel::OriginRole).toString();
    return origin.isEmpty() || origin == QStringLiteral("user");
}
}
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
    refreshGroups();
    if (rowCount() > 0) emit dataChanged(index(0, 0), index(rowCount() - 1, 0),
        {ConversationModel::BodyRole});
    emit showWhenReadyChanged();
}
bool ConversationPresentationModel::filterAcceptsRow(int row, const QModelIndex& parent) const {
    if (m_repeatedRows.contains(row)) return false;
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
    if (m_explanationRows.value(source.row()).contains(role))
        return m_explanationRows.value(source.row()).value(role);
    if (role == ExplanationRepeatRole) return 1;
    return presentationData(source, role);
}
QVariant ConversationPresentationModel::presentationData(const QModelIndex& source, int role) const {
    if (role == ActivityInlineRole) return inlineRow(source);
    const auto rows = groupedRow(source.row()) ? groupRows(source.row()) : QList<QModelIndex>{};
    if (role == ActivityLabelRole) return activityLabel(rows.isEmpty() ? QList<QModelIndex>{source} : rows);
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
            return activityLabel(rows);
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
        && !source.data(ConversationModel::ActivityRole).toBool()
        && source.data(ConversationModel::AuthorRole).toString() == QStringLiteral("assistant")
        && source.data(ConversationModel::KindRole).toString() == QStringLiteral("live"))
        return QString{};
    return source.data(role);
}
QHash<int, QByteArray> ConversationPresentationModel::roleNames() const {
    auto roles = QSortFilterProxyModel::roleNames();
    roles.insert(GroupIdsRole, "groupIds"); roles.insert(GroupLabelRole, "groupLabel");
    roles.insert(GroupExpandedRole, "groupExpanded"); roles.insert(ActivityInlineRole, "activityInline");
    roles.insert(ActivityLabelRole, "activityLabel");
    roles.insert(ExplanationRepeatRole, "explanationRepeat");
    return roles;
}
QString ConversationPresentationModel::activityLabel(const QList<QModelIndex>& rows) const {
    if (rows.isEmpty() || !rows.first().isValid()) return {};
    int count = 0;
    for (const auto& row : rows) count += std::max({row.data(ConversationModel::ActivityRole).toBool() ? 1 : 0,
        row.data(ConversationModel::ActivityCountRole).toInt(),
        static_cast<int>(row.data(ConversationModel::ToolsRole).toList().size()),
        static_cast<int>(row.data(ConversationModel::DisplayCellsRole).toList().size())});
    if (count == 0) return {};
    QString label = QStringLiteral("%1 tool call%2").arg(count).arg(count == 1 ? QString{} : QStringLiteral("s"));
    const auto timeOf = [](const QModelIndex& row) {
        return QDateTime::fromString(row.data(ConversationModel::TimestampRole).toString(), Qt::ISODateWithMs);
    };
    const auto start = timeOf(rows.first());
    auto end = timeOf(rows.last());
    // The following assistant message closes the activity interval. Never
    // count time waiting for the next user or a teammate as tool execution.
    const auto next = sourceModel()->index(rows.last().row() + 1, 0);
    if (next.isValid() && next.data(ConversationModel::AuthorRole).toString() == QStringLiteral("assistant")
        && ownTurn(next)
        && !next.data(ConversationModel::AutomatedRole).toBool()
        && next.data(ConversationModel::KindRole).toString() != QStringLiteral("live")
        && !next.data(ConversationModel::ActivityRole).toBool()) {
        const auto boundary = timeOf(next);
        if (boundary.isValid() && (!end.isValid() || boundary > end)) end = boundary;
    }
    const qint64 seconds = start.secsTo(end);
    if (start.isValid() && end.isValid() && seconds > 0) {
        QString elapsed;
        if (seconds >= 3600) elapsed += QStringLiteral("%1h ").arg(seconds / 3600);
        if (seconds >= 60) elapsed += QStringLiteral("%1m ").arg(seconds / 60 % 60);
        elapsed += QStringLiteral("%1s").arg(seconds % 60);
        label += QStringLiteral(" · %1 elapsed").arg(elapsed);
    }
    return label;
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
    if (sourceModel() == nullptr || row < 0 || row >= sourceModel()->rowCount()) return false;
    const auto item = sourceModel()->index(row, 0);
    if (inlineRow(item)) return false;
    if (item.data(ConversationModel::ActivityRole).toBool()) {
        const auto label = item.data(ConversationModel::ToolNameRole).toString();
        return label != QStringLiteral("thinking") && label != QStringLiteral("compacting");
    }
    if (!ownTurn(item) || item.data(ConversationModel::AutomatedRole).toBool()) return false;
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
    beginFilterChange();
    rebuildExplanationRuns();
    endFilterChange(QSortFilterProxyModel::Direction::Rows);
    if (rowCount() > 0) emit dataChanged(index(0, 0), index(rowCount() - 1, 0));
}
void ConversationPresentationModel::setExplanationLookup(std::function<QString(const QVariantMap&)> lookup) {
    m_explanationLookup = std::move(lookup);
    refreshGroups();
}
void ConversationPresentationModel::updateExplanations(QObject* object, const QString& session,
                                                       const QString& directory, bool localFiles) {
    QPointer<ToolNarrator> narrator = qobject_cast<ToolNarrator*>(object);
    if (!narrator || !narrator->enabled() || narrator->unavailable()) {
        setExplanationLookup({});
        return;
    }
    setExplanationLookup([narrator, session, directory, localFiles](QVariantMap activity) {
        activity.insert(QStringLiteral("_session"), session);
        return narrator ? narrator->explanation(activity, directory, localFiles) : QString{};
    });
}
void ConversationPresentationModel::rebuildExplanationRuns() {
    m_explanationRows.clear();
    m_repeatedRows.clear();
    if (!sourceModel() || !m_explanationLookup) return;
    struct Entry { int row; int role; int offset; };
    Entry first{-1, 0, 0};
    QString previous;
    QString previousStatus;
    int count = 0;
    auto annotate = [this](const Entry& entry, int repetitions) {
        if (entry.role == ExplanationRepeatRole) {
            m_explanationRows[entry.row][entry.role] = repetitions;
        } else {
            auto values = m_explanationRows[entry.row][entry.role].toList();
            auto value = values[entry.offset].toMap();
            value.insert(QStringLiteral("_explanationRepeat"), repetitions);
            values[entry.offset] = value;
            m_explanationRows[entry.row][entry.role] = values;
        }
    };
    auto append = [&](const Entry& entry, const QVariantMap& value) {
        const QString text = m_explanationLookup(value);
        const QString status = value.value(QStringLiteral("status"), QStringLiteral("recorded")).toString();
        if (!text.isEmpty() && text == previous && status == previousStatus) {
            annotate(first, ++count);
            annotate(entry, 0);
            return false;
        }
        previous = text;
        previousStatus = status;
        first = entry;
        count = 1;
        return true;
    };
    for (int row = 0; row < sourceModel()->rowCount(); ++row) {
        if (groupedRow(row) && row > 0 && groupedRow(row - 1)) continue;
        const auto source = sourceModel()->index(row, 0);
        const bool activity = presentationData(source, ConversationModel::ActivityRole).toBool();
        const bool prose = !presentationData(source, ConversationModel::BodyRole).toString().isEmpty() && !activity;
        const bool collapsed = groupedRow(row) && !m_expanded.contains(source.data(ConversationModel::MessageIdRole).toString());
        if (prose || groupedRow(row)) previous.clear();
        if (collapsed) continue;
        bool any = false;
        bool retained = false;
        if (activity) {
            any = true;
            retained = append({row, ExplanationRepeatRole, 0}, {
                {QStringLiteral("name"), source.data(ConversationModel::ToolNameRole)},
                {QStringLiteral("summary"), source.data(ConversationModel::BodyRole)},
                {QStringLiteral("status"), source.data(ConversationModel::ActivityStatusRole)}});
        } else {
            const auto cells = presentationData(source, ConversationModel::DisplayCellsRole).toList();
            for (const int role : {ConversationModel::DisplayCellsRole, ConversationModel::ToolsRole}) {
                const auto values = presentationData(source, role).toList();
                m_explanationRows[row][role] = values;
                for (int offset = 0; offset < values.size(); ++offset) {
                    const auto value = values[offset].toMap();
                    const auto name = value.value(QStringLiteral("name")).toString();
                    if (role == ConversationModel::ToolsRole && !groupedRow(row) && !cells.isEmpty()
                        && name != QStringLiteral("Edit") && name != QStringLiteral("MultiEdit") && name != QStringLiteral("Write")) continue;
                    any = true;
                    retained = append({row, role, offset}, value) || retained;
                }
            }
        }
        if (!any) previous.clear();
        if (any && !retained && !prose && !groupedRow(row)) m_repeatedRows.insert(row);
        if (prose || groupedRow(row)) previous.clear();
    }
}
void ConversationPresentationModel::setActivityMode(int mode) {
    if (m_activityMode == mode) return;
    m_activityMode = mode; refreshGroups(); emit countChanged();
}
void ConversationPresentationModel::beginVisit() {
    m_visitStarted = QDateTime::currentDateTimeUtc(); m_expanded.clear(); m_observedLive.clear(); refreshGroups();
}
void ConversationPresentationModel::setSourceModel(QAbstractItemModel* model) {
    if (sourceModel() != nullptr) disconnect(sourceModel(), nullptr, this, nullptr);
    QSortFilterProxyModel::setSourceModel(model);
    if (model != nullptr) {
        connect(model, &QAbstractItemModel::dataChanged, this, [this](const QModelIndex& first, const QModelIndex& last) {
            if (m_explanationLookup) { refreshGroups(); return; }
            if (m_activityMode == 1) return;
            for (int row = first.row(); row <= last.row(); ++row)
                if (groupedRow(row) || groupedRow(row - 1)) { refreshGroups(); return; }
            // A reply timestamp can close the preceding message's tool span.
            // Notify derived labels even when neither row is a synthetic group.
            for (int row = std::max(0, first.row() - 1); row <= last.row(); ++row) {
                const auto item = mapFromSource(sourceModel()->index(row, 0));
                if (item.isValid()) emit dataChanged(item, item, {ActivityLabelRole});
            }
        });
        connect(model, &QAbstractItemModel::modelReset, this, [this] { refreshGroups(); });
        connect(model, &QAbstractItemModel::rowsInserted, this, [this] { if (m_activityMode != 1 || m_explanationLookup) refreshGroups(); });
        connect(model, &QAbstractItemModel::rowsRemoved, this, [this] { if (m_activityMode != 1 || m_explanationLookup) refreshGroups(); });
    }
    beginVisit();
}
void ConversationPresentationModel::toggleGroup(const QString& id) {
    if (!m_expanded.remove(id)) m_expanded.insert(id);
    refreshGroups();
}
int ConversationPresentationModel::indexOfMessage(const QString& id) const {
    const auto* source = qobject_cast<const ConversationModel*>(sourceModel());
    if (source == nullptr) return -1;
    int row = source->indexOfMessage(id);
    while (row > 0 && m_repeatedRows.contains(row)) --row;
    if (groupedRow(row)) while (row > 0 && groupedRow(row - 1)) --row;
    return row < 0 ? -1 : mapFromSource(source->index(row, 0)).row();
}
}
