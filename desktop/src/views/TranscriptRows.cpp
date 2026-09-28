#include "views/TranscriptRows.h"

#include "protocol/ProtocolTypes.h"

#include <algorithm>

namespace clarp {
namespace {
// Split only what is expensive to lay out as one text document.
constexpr qsizetype SplitAboveCharacters = 6'000;
constexpr int SplitTablesAboveRows = 24;
constexpr qsizetype PartCharacters = 2'500;
constexpr int TableRowsPerPart = 16;

constexpr int PartIndexRole = Qt::UserRole + 900;
constexpr int PartCountRole = Qt::UserRole + 901;
constexpr int FullBodyRole = Qt::UserRole + 902;
constexpr int RowKeyRole = Qt::UserRole + 903;

bool isTableBlock(const QStringList& lines) {
    return lines.size() >= 3 && lines.at(0).trimmed().startsWith(u'|') && lines.at(1).contains(QStringLiteral("---"));
}
} // namespace

TranscriptRows::TranscriptRows(QObject* parent) : QAbstractListModel(parent) {}

QStringList TranscriptRows::splitMarkdown(const QString& markdown) {
    const QStringList blocks = markdownDisplayBlocks(markdown);
    bool bigTable = false;
    for (const QString& block : blocks) {
        const QStringList lines = block.split(u'\n');
        if (isTableBlock(lines) && lines.size() - 2 > SplitTablesAboveRows) bigTable = true;
    }
    if (markdown.size() <= SplitAboveCharacters && !bigTable) return {markdown};

    QStringList parts;
    QString pending;
    const auto flush = [&] {
        if (!pending.isEmpty()) parts.append(pending);
        pending.clear();
    };
    for (const QString& block : blocks) {
        const QStringList lines = block.split(u'\n');
        if (isTableBlock(lines) && lines.size() - 2 > SplitTablesAboveRows) {
            flush();
            const QString header = lines.at(0) + u'\n' + lines.at(1);
            for (qsizetype row = 2; row < lines.size(); row += TableRowsPerPart)
                parts.append(header + u'\n' + lines.mid(row, TableRowsPerPart).join(u'\n'));
            continue;
        }
        if (!pending.isEmpty() && pending.size() + block.size() > PartCharacters) flush();
        pending += pending.isEmpty() ? block : QStringLiteral("\n\n") + block;
    }
    flush();
    return parts.isEmpty() ? QStringList{markdown} : parts;
}

void TranscriptRows::setSourceModel(QAbstractItemModel* model) {
    if (m_source == model) return;
    for (const auto& connection : std::as_const(m_connections)) disconnect(connection);
    m_connections.clear();
    m_source = model;
    if (model != nullptr) {
        const auto reset = [this] { rebuild(); };
        m_connections = {
            connect(model, &QAbstractItemModel::rowsInserted, this, &TranscriptRows::onRowsInserted),
            connect(model, &QAbstractItemModel::rowsRemoved, this, &TranscriptRows::onRowsRemoved),
            connect(model, &QAbstractItemModel::dataChanged, this, &TranscriptRows::onDataChanged),
            connect(model, &QAbstractItemModel::modelReset, this, reset),
            connect(model, &QAbstractItemModel::layoutChanged, this, reset),
            connect(model, &QAbstractItemModel::rowsMoved, this, reset),
        };
    }
    rebuild();
    emit sourceModelChanged();
}

int TranscriptRows::rowCount(const QModelIndex& parent) const {
    return parent.isValid() ? 0 : count();
}

QHash<int, QByteArray> TranscriptRows::roleNames() const { return m_roleNames; }

QVariant TranscriptRows::data(const QModelIndex& index, int role) const {
    if (!m_source || !index.isValid() || index.row() >= count()) return {};
    const Row& row = m_rows[static_cast<size_t>(index.row())];
    if (role == PartIndexRole) return row.part;
    if (role == PartCountRole) return row.parts;
    const QModelIndex source = m_source->index(row.source, 0);
    if (role == FullBodyRole) return m_source->data(source, m_bodyRole);
    if (role == RowKeyRole) {
        const QString id = m_source->data(source, m_messageIdRole).toString();
        return row.parts > 1 ? id + u'#' + QString::number(row.part) : id;
    }
    if (role == m_bodyRole && row.parts > 1) return row.body;
    return m_source->data(source, role);
}

int TranscriptRows::firstRowOf(int sourceRow) const {
    const auto it = std::lower_bound(m_rows.begin(), m_rows.end(), sourceRow,
        [](const Row& row, int source) { return row.source < source; });
    return static_cast<int>(std::distance(m_rows.begin(), it));
}

int TranscriptRows::rowsOf(int sourceRow) const {
    const int first = firstRowOf(sourceRow);
    int rows = 0;
    while (first + rows < count() && m_rows[static_cast<size_t>(first) + static_cast<size_t>(rows)].source == sourceRow) ++rows;
    return rows;
}

int TranscriptRows::rowForSource(int sourceRow) const {
    if (sourceRow < 0) return -1;
    const int row = firstRowOf(sourceRow);
    return row < count() && m_rows[static_cast<size_t>(row)].source == sourceRow ? row : -1;
}

int TranscriptRows::sourceRow(int row) const {
    return row >= 0 && row < count() ? m_rows[static_cast<size_t>(row)].source : -1;
}

std::vector<TranscriptRows::Row> TranscriptRows::partsFor(int sourceRow) const {
    const QModelIndex index = m_source->index(sourceRow, 0);
    const bool splittable = !m_source->data(index, m_activityRole).toBool()
        && m_source->data(index, m_kindRole).toString() != QStringLiteral("live")
        && m_source->data(index, m_authorRole).toString() == QStringLiteral("assistant");
    if (!splittable) return {Row{.source = sourceRow, .part = 0, .parts = 1, .body = {}}};
    const QString body = m_source->data(index, m_bodyRole).toString();
    if (body.size() <= SplitAboveCharacters && !body.contains(QStringLiteral("|---")) && !body.contains(QStringLiteral("| ---")))
        return {Row{.source = sourceRow, .part = 0, .parts = 1, .body = {}}};
    const QStringList parts = splitMarkdown(body);
    if (parts.size() <= 1) return {Row{.source = sourceRow, .part = 0, .parts = 1, .body = {}}};
    std::vector<Row> rows;
    rows.reserve(static_cast<size_t>(parts.size()));
    for (int part = 0; part < parts.size(); ++part)
        rows.push_back(Row{.source = sourceRow, .part = part, .parts = static_cast<int>(parts.size()), .body = parts.at(part)});
    return rows;
}

void TranscriptRows::rebuild() {
    beginResetModel();
    m_rows.clear();
    m_roleNames.clear();
    if (m_source) {
        m_roleNames = m_source->roleNames();
        const auto role = [this](const QByteArray& name) { return m_roleNames.key(name, -1); };
        m_bodyRole = role("body");
        m_kindRole = role("messageKind");
        m_activityRole = role("activity");
        m_authorRole = role("authorRole");
        m_messageIdRole = role("messageId");
        m_roleNames.insert(PartIndexRole, "partIndex");
        m_roleNames.insert(PartCountRole, "partCount");
        m_roleNames.insert(FullBodyRole, "fullBody");
        m_roleNames.insert(RowKeyRole, "rowKey");
        const int rows = m_source->rowCount();
        for (int source = 0; source < rows; ++source) {
            const auto parts = partsFor(source);
            m_rows.insert(m_rows.end(), parts.begin(), parts.end());
        }
    }
    endResetModel();
    emit countChanged();
}

void TranscriptRows::shiftSources(int from, int delta) {
    for (Row& row : m_rows)
        if (row.source >= from) row.source += delta;
}

void TranscriptRows::onRowsInserted(const QModelIndex& parent, int first, int last) {
    if (parent.isValid()) return;
    const int added = last - first + 1;
    const int at = firstRowOf(first);
    shiftSources(first, added);
    std::vector<Row> rows;
    for (int source = first; source <= last; ++source) {
        const auto parts = partsFor(source);
        rows.insert(rows.end(), parts.begin(), parts.end());
    }
    beginInsertRows({}, at, at + static_cast<int>(rows.size()) - 1);
    m_rows.insert(m_rows.begin() + at, rows.begin(), rows.end());
    endInsertRows();
    emit countChanged();
}

void TranscriptRows::onRowsRemoved(const QModelIndex& parent, int first, int last) {
    if (parent.isValid()) return;
    const int from = firstRowOf(first);
    const int to = firstRowOf(last + 1) - 1;
    if (to >= from) {
        beginRemoveRows({}, from, to);
        m_rows.erase(m_rows.begin() + from, m_rows.begin() + to + 1);
        endRemoveRows();
    }
    shiftSources(last + 1, -(last - first + 1));
    emit countChanged();
}

void TranscriptRows::onDataChanged(const QModelIndex& topLeft, const QModelIndex& bottomRight, const QList<int>& roles) {
    if (topLeft.parent().isValid()) return;
    const bool splitMayChange = roles.isEmpty() || roles.contains(m_bodyRole) || roles.contains(m_kindRole)
        || roles.contains(m_activityRole);
    for (int source = topLeft.row(); source <= bottomRight.row(); ++source) {
        const int first = firstRowOf(source);
        const int existing = rowsOf(source);
        if (!splitMayChange) {
            if (existing > 0) emit dataChanged(index(first), index(first + existing - 1), roles);
            continue;
        }
        const auto parts = partsFor(source);
        const int wanted = static_cast<int>(parts.size());
        if (wanted == existing) {
            for (int part = 0; part < wanted; ++part) m_rows[static_cast<size_t>(first) + static_cast<size_t>(part)] = parts[static_cast<size_t>(part)];
            if (existing > 0) emit dataChanged(index(first), index(first + existing - 1), roles);
            continue;
        }
        // The message now splits differently (it finished streaming, or grew
        // past the threshold): keep the rows that stay and add or drop the rest,
        // so the view keeps its delegates and position for the first part.
        const int kept = std::min(existing, wanted);
        for (int part = 0; part < kept; ++part) m_rows[static_cast<size_t>(first) + static_cast<size_t>(part)] = parts[static_cast<size_t>(part)];
        if (kept > 0) emit dataChanged(index(first), index(first + kept - 1));
        if (wanted > existing) {
            beginInsertRows({}, first + existing, first + wanted - 1);
            m_rows.insert(m_rows.begin() + first + existing, parts.begin() + existing, parts.end());
            endInsertRows();
        } else {
            beginRemoveRows({}, first + wanted, first + existing - 1);
            m_rows.erase(m_rows.begin() + first + wanted, m_rows.begin() + first + existing);
            endRemoveRows();
        }
        emit countChanged();
    }
}
} // namespace clarp
