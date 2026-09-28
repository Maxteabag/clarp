#include "views/TranscriptLayout.h"

#include <QAbstractItemModel>
#include <QElapsedTimer>
#include <QQmlComponent>
#include <QQmlContext>
#include <QQmlEngine>
#include <QQmlPropertyMap>
#include <algorithm>
#include <cmath>

namespace clarp {
namespace {
// Rough monospace metrics for estimates; only the scrollbar ever sees them.
constexpr qreal EstimatedCharWidth = 7.8;
constexpr qreal EstimatedLineHeight = 19;
constexpr qreal EstimatedRowChrome = 30;
constexpr qreal EstimatedSectionHeight = 26;
constexpr int MaxLayoutPasses = 8;
// Spare time per frame for rows in the margin around the viewport.
constexpr int MarginCreationBudgetMs = 4;
} // namespace

TranscriptLayout::TranscriptLayout(QQuickItem* parent) : QQuickItem(parent) {
    setFlag(ItemHasContents, false);
}

TranscriptLayout::~TranscriptLayout() {
    for (Row& row : m_rows) releaseRow(row);
}

void TranscriptLayout::setModel(QAbstractItemModel* model) {
    if (m_model == model) return;
    for (const auto& connection : std::as_const(m_modelConnections)) disconnect(connection);
    m_modelConnections.clear();
    m_model = model;
    if (model != nullptr) {
        m_modelConnections = {
            connect(model, &QAbstractItemModel::rowsInserted, this, &TranscriptLayout::onRowsInserted),
            connect(model, &QAbstractItemModel::rowsAboutToBeRemoved, this, &TranscriptLayout::onRowsAboutToBeRemoved),
            connect(model, &QAbstractItemModel::rowsRemoved, this, &TranscriptLayout::onRowsRemoved),
            connect(model, &QAbstractItemModel::dataChanged, this, &TranscriptLayout::onDataChanged),
            connect(model, &QAbstractItemModel::modelAboutToBeReset, this, &TranscriptLayout::rememberAnchorIdentity),
            connect(model, &QAbstractItemModel::modelReset, this, [this] { resetRows(); restoreAnchorIdentity(); }),
            connect(model, &QAbstractItemModel::layoutAboutToBeChanged, this, &TranscriptLayout::rememberAnchorIdentity),
            connect(model, &QAbstractItemModel::layoutChanged, this, [this] { resetRows(); restoreAnchorIdentity(); }),
            connect(model, &QAbstractItemModel::rowsAboutToBeMoved, this, &TranscriptLayout::rememberAnchorIdentity),
            connect(model, &QAbstractItemModel::rowsMoved, this, [this] { resetRows(); restoreAnchorIdentity(); }),
        };
    }
    m_anchorRow = -1;
    resetRows();
    emit modelChanged();
}

void TranscriptLayout::setDelegate(QQmlComponent* delegate) {
    if (m_delegate == delegate) return;
    m_delegate = delegate;
    m_delegatePropertiesKnown = false;
    m_delegateProperties.clear();
    resetRows();
    emit delegateChanged();
}

void TranscriptLayout::setSectionDelegate(QQmlComponent* delegate) {
    if (m_sectionDelegate == delegate) return;
    m_sectionDelegate = delegate;
    m_sectionHeight = -1;
    resetRows();
    emit sectionDelegateChanged();
}

void TranscriptLayout::setSectionRole(const QString& role) {
    if (m_sectionRole == role) return;
    m_sectionRole = role;
    resetRows();
    emit sectionRoleChanged();
}

void TranscriptLayout::setEstimateRole(const QString& role) {
    if (m_estimateRole == role) return;
    m_estimateRole = role;
    emit estimateRoleChanged();
}

void TranscriptLayout::setHeader(QQuickItem* item) {
    if (m_header == item) return;
    if (m_header) disconnect(m_header, nullptr, this, nullptr);
    m_header = item;
    if (item != nullptr) {
        item->setParentItem(this);
        connect(item, &QQuickItem::heightChanged, this, &TranscriptLayout::scheduleLayout);
    }
    scheduleLayout();
    emit headerChanged();
}

void TranscriptLayout::setFooter(QQuickItem* item) {
    if (m_footer == item) return;
    if (m_footer) disconnect(m_footer, nullptr, this, nullptr);
    m_footer = item;
    if (item != nullptr) {
        item->setParentItem(this);
        connect(item, &QQuickItem::heightChanged, this, &TranscriptLayout::scheduleLayout);
    }
    scheduleLayout();
    emit footerChanged();
}

void TranscriptLayout::setFlickable(QQuickItem* flickable) {
    if (m_flickable == flickable) return;
    for (const auto& connection : std::as_const(m_flickableConnections)) disconnect(connection);
    m_flickableConnections.clear();
    m_flickable = flickable;
    if (flickable != nullptr) {
        // Flickable's C++ type is private; its notify signal is public API.
        m_flickableConnections.append(connect(flickable, SIGNAL(contentYChanged()), this, SLOT(flickableMoved())));
        m_flickableConnections.append(connect(flickable, &QQuickItem::heightChanged, this, &TranscriptLayout::scheduleLayout));
    }
    scheduleLayout();
    emit flickableChanged();
}

void TranscriptLayout::setFollowing(bool following) {
    if (m_following == following) return;
    m_following = following;
    if (!following) anchorToViewport();
    scheduleLayout();
    emit followingChanged();
}

void TranscriptLayout::setSpacing(qreal spacing) {
    if (qFuzzyCompare(m_spacing, spacing)) return;
    m_spacing = spacing;
    m_prefixDirty = true;
    scheduleLayout();
    emit spacingChanged();
}

void TranscriptLayout::setLeftMargin(qreal margin) {
    if (qFuzzyCompare(m_leftMargin, margin)) return;
    m_leftMargin = margin;
    scheduleLayout();
    emit marginsChanged();
}

void TranscriptLayout::setRightMargin(qreal margin) {
    if (qFuzzyCompare(m_rightMargin, margin)) return;
    m_rightMargin = margin;
    scheduleLayout();
    emit marginsChanged();
}

void TranscriptLayout::setTopMargin(qreal margin) {
    if (qFuzzyCompare(m_topMargin, margin)) return;
    m_topMargin = margin;
    scheduleLayout();
    emit marginsChanged();
}

void TranscriptLayout::setBottomMargin(qreal margin) {
    if (qFuzzyCompare(m_bottomMargin, margin)) return;
    m_bottomMargin = margin;
    scheduleLayout();
    emit marginsChanged();
}

void TranscriptLayout::setCacheExtent(qreal extent) {
    if (qFuzzyCompare(m_cacheExtent, extent)) return;
    m_cacheExtent = extent;
    scheduleLayout();
    emit cacheExtentChanged();
}

void TranscriptLayout::setCreationBudget(int milliseconds) {
    if (m_creationBudget == milliseconds) return;
    m_creationBudget = milliseconds;
    if (m_creationPending) scheduleLayout();
    emit creationBudgetChanged();
}

qreal TranscriptLayout::headerHeight() const { return m_header ? m_header->height() : 0; }
qreal TranscriptLayout::footerHeight() const { return m_footer ? m_footer->height() : 0; }
qreal TranscriptLayout::rowsTop() const { return m_topMargin + headerHeight(); }

qreal TranscriptLayout::contentHeight() const {
    rebuildPrefix();
    return rowsTop() + m_prefix.back() + footerHeight() + m_bottomMargin;
}

int TranscriptLayout::measuredCount() const {
    return static_cast<int>(std::count_if(m_rows.begin(), m_rows.end(), [](const Row& row) { return row.measured; }));
}

void TranscriptLayout::rebuildPrefix() const {
    if (!m_prefixDirty && m_prefix.size() == m_rows.size() + 1) return;
    m_prefix.assign(m_rows.size() + 1, 0);
    for (size_t i = 0; i < m_rows.size(); ++i)
        m_prefix[i + 1] = m_prefix[i] + m_rows[i].height + m_spacing;
    m_prefixDirty = false;
}

qreal TranscriptLayout::positionOf(int row) const {
    rebuildPrefix();
    if (m_rows.empty()) return rowsTop();
    row = std::clamp(row, 0, count());
    return rowsTop() + m_prefix[static_cast<size_t>(row)];
}

qreal TranscriptLayout::heightOf(int row) const {
    if (row < 0 || row >= count()) return 0;
    return m_rows[static_cast<size_t>(row)].height;
}

int TranscriptLayout::rowAt(qreal y) const {
    rebuildPrefix();
    if (m_rows.empty()) return -1;
    const qreal local = y - rowsTop();
    // The last prefix entry not above local: the row whose block holds y.
    const auto it = std::upper_bound(m_prefix.begin(), m_prefix.end(), local);
    const int index = static_cast<int>(std::distance(m_prefix.begin(), it)) - 1;
    return std::clamp(index, 0, count() - 1);
}

QQuickItem* TranscriptLayout::itemAt(int row) const {
    if (row < 0 || row >= count()) return nullptr;
    return m_rows[static_cast<size_t>(row)].item;
}

bool TranscriptLayout::isMeasured(int row) const {
    return row >= 0 && row < count() && m_rows[static_cast<size_t>(row)].measured;
}

qreal TranscriptLayout::viewportY() const {
    return m_flickable ? m_flickable->property("contentY").toReal() : 0;
}

qreal TranscriptLayout::viewportHeight() const {
    return m_flickable ? m_flickable->height() : height();
}

qreal TranscriptLayout::endY() const { return std::max<qreal>(0, contentHeight() - viewportHeight()); }

qreal TranscriptLayout::rowWidth() const { return std::max<qreal>(0, width() - m_leftMargin - m_rightMargin); }

void TranscriptLayout::setViewportY(qreal y) {
    if (!m_flickable) return;
    const qreal from = viewportY();
    if (std::abs(from - y) <= 0.5) return;
    m_correcting = true;
    m_flickable->setProperty("contentY", y);
    m_correcting = false;
    emit viewportCorrected(from, y);
}

void TranscriptLayout::anchorToViewport() {
    if (m_rows.empty()) { m_anchorRow = -1; return; }
    const qreal y = viewportY();
    m_anchorRow = rowAt(y);
    m_anchorOffset = y - positionOf(m_anchorRow);
}

void TranscriptLayout::positionAtRow(int row, bool atBottom) {
    if (m_rows.empty()) return;
    row = std::clamp(row, 0, count() - 1);
    if (m_following) {
        m_following = false;
        emit followingChanged();
    }
    const qreal top = positionOf(row);
    const qreal target = atBottom ? top + heightOf(row) - viewportHeight() : top;
    setViewportY(std::clamp<qreal>(target, 0, endY()));
    anchorToViewport();
    relayout();
}

void TranscriptLayout::flickableMoved() {
    if (m_correcting || m_inLayout) return;
    if (!m_following) anchorToViewport();
    scheduleLayout();
}

void TranscriptLayout::scheduleLayout() {
    if (window() != nullptr) polish();
}

void TranscriptLayout::layoutNow() { relayout(); }

void TranscriptLayout::updatePolish() { relayout(); }

void TranscriptLayout::componentComplete() {
    QQuickItem::componentComplete();
    resetRows();
}

void TranscriptLayout::geometryChange(const QRectF& newGeometry, const QRectF& oldGeometry) {
    QQuickItem::geometryChange(newGeometry, oldGeometry);
    if (!qFuzzyCompare(newGeometry.width(), oldGeometry.width()) && m_lastWidth > 0) {
        // Rows that are not on screen keep their height at the old width as
        // an estimate, scaled to the new one; created rows re-measure.
        const qreal ratio = std::max<qreal>(1, m_lastWidth - m_leftMargin - m_rightMargin)
            / std::max<qreal>(1, rowWidth());
        for (Row& row : m_rows) {
            if (row.item != nullptr) continue;
            if (row.measured) row.height = row.height * ratio;
            row.measured = false;
        }
        m_prefixDirty = true;
    }
    m_lastWidth = newGeometry.width();
    scheduleLayout();
}

QVariantMap TranscriptLayout::roleValues(int index) const {
    QVariantMap values;
    if (!m_model) return values;
    const QModelIndex modelIndex = m_model->index(index, 0);
    for (auto it = m_roleIds.cbegin(); it != m_roleIds.cend(); ++it)
        values.insert(QString::fromUtf8(it.key()), m_model->data(modelIndex, it.value()));
    return values;
}

qreal TranscriptLayout::estimate(int index) const {
    if (!m_model) return EstimatedRowChrome;
    const QString text = m_model->data(m_model->index(index, 0), m_roleIds.value(m_estimateRole.toUtf8(), -1)).toString();
    const qreal perLine = std::max<qreal>(10, rowWidth() / EstimatedCharWidth);
    qreal lines = 0;
    qsizetype start = 0;
    while (start <= text.size()) {
        qsizetype end = text.indexOf(u'\n', start);
        if (end < 0) end = text.size();
        lines += std::max<qreal>(1, std::ceil(static_cast<qreal>(end - start) / perLine));
        start = end + 1;
    }
    return EstimatedRowChrome + (text.isEmpty() ? 0 : lines * EstimatedLineHeight);
}

void TranscriptLayout::updateSectionFlags(int from) {
    const qreal sectionHeight = m_sectionHeight >= 0 ? m_sectionHeight : EstimatedSectionHeight;
    for (int i = std::max(0, from); i < count(); ++i) {
        Row& row = m_rows[static_cast<size_t>(i)];
        const bool shown = m_sectionDelegate && !row.sectionLabel.isEmpty()
            && (i == 0 || row.sectionLabel != m_rows[static_cast<size_t>(i - 1)].sectionLabel);
        if (shown == row.sectionShown) continue;
        row.sectionShown = shown;
        if (row.item == nullptr) row.height += shown ? sectionHeight : -sectionHeight;
        m_prefixDirty = true;
    }
}

void TranscriptLayout::resetRows() {
    for (Row& row : m_rows) releaseRow(row);
    m_rows.clear();
    m_roleIds.clear();
    if (m_model) {
        const auto names = m_model->roleNames();
        for (auto it = names.cbegin(); it != names.cend(); ++it) m_roleIds.insert(it.value(), it.key());
        const int rows = m_model->rowCount();
        m_rows.resize(static_cast<size_t>(rows));
        const int sectionRole = m_roleIds.value(m_sectionRole.toUtf8(), -1);
        for (int i = 0; i < rows; ++i) {
            Row& row = m_rows[static_cast<size_t>(i)];
            row.height = estimate(i);
            if (sectionRole >= 0) row.sectionLabel = m_model->data(m_model->index(i, 0), sectionRole).toString();
        }
    }
    updateSectionFlags(0);
    m_prefixDirty = true;
    if (m_anchorRow >= count()) m_anchorRow = count() - 1;
    emit countChanged();
    scheduleLayout();
}

void TranscriptLayout::rememberAnchorIdentity() {
    m_anchorIdentity.clear();
    if (m_following || m_anchorRow < 0 || m_anchorRow >= count() || !m_model) return;
    m_anchorIdentity = m_model->data(m_model->index(m_anchorRow, 0), m_roleIds.value("messageId", -1)).toString();
    m_anchorIdentityOffset = m_anchorOffset;
}

void TranscriptLayout::restoreAnchorIdentity() {
    if (m_anchorIdentity.isEmpty() || !m_model) return;
    const int role = m_roleIds.value("messageId", -1);
    for (int i = 0; i < count(); ++i) {
        if (m_model->data(m_model->index(i, 0), role).toString() == m_anchorIdentity) {
            m_anchorRow = i;
            m_anchorOffset = m_anchorIdentityOffset;
            break;
        }
    }
    m_anchorIdentity.clear();
    scheduleLayout();
}

void TranscriptLayout::onRowsInserted(const QModelIndex& parent, int first, int last) {
    if (parent.isValid()) return;
    const int added = last - first + 1;
    const int sectionRole = m_roleIds.value(m_sectionRole.toUtf8(), -1);
    m_rows.insert(m_rows.begin() + first, static_cast<size_t>(added), Row{});
    for (int i = first; i <= last; ++i) {
        Row& row = m_rows[static_cast<size_t>(i)];
        row.height = estimate(i);
        if (sectionRole >= 0) row.sectionLabel = m_model->data(m_model->index(i, 0), sectionRole).toString();
    }
    // Rows inserted above the reader push the anchor down by the same rows,
    // so what is on screen stays where it is.
    if (m_anchorRow >= first) m_anchorRow += added;
    updateSectionFlags(first);
    m_prefixDirty = true;
    emit countChanged();
    scheduleLayout();
}

void TranscriptLayout::onRowsAboutToBeRemoved(const QModelIndex& parent, int first, int last) {
    if (parent.isValid()) return;
    for (int i = first; i <= last && i < count(); ++i) releaseRow(m_rows[static_cast<size_t>(i)]);
}

void TranscriptLayout::onRowsRemoved(const QModelIndex& parent, int first, int last) {
    if (parent.isValid()) return;
    last = std::min(last, count() - 1);
    if (first > last) return;
    const int removed = last - first + 1;
    m_rows.erase(m_rows.begin() + first, m_rows.begin() + last + 1);
    if (m_anchorRow > last) {
        m_anchorRow -= removed;
    } else if (m_anchorRow >= first) {
        // The row the reader was on is gone: keep the one that took its place.
        m_anchorRow = std::min(first, count() - 1);
        m_anchorOffset = 0;
    }
    updateSectionFlags(first);
    m_prefixDirty = true;
    emit countChanged();
    scheduleLayout();
}

void TranscriptLayout::onDataChanged(const QModelIndex& topLeft, const QModelIndex& bottomRight, const QList<int>& roles) {
    if (topLeft.parent().isValid()) return;
    const int sectionRole = m_roleIds.value(m_sectionRole.toUtf8(), -1);
    const int estimateRole = m_roleIds.value(m_estimateRole.toUtf8(), -1);
    for (int i = topLeft.row(); i <= bottomRight.row() && i < count(); ++i) {
        Row& row = m_rows[static_cast<size_t>(i)];
        const QModelIndex index = m_model->index(i, 0);
        if (row.item != nullptr) {
            for (auto it = m_roleIds.cbegin(); it != m_roleIds.cend(); ++it) {
                if (!roles.isEmpty() && !roles.contains(it.value())) continue;
                const QVariant value = m_model->data(index, it.value());
                if (row.modelData) row.modelData->insert(QString::fromUtf8(it.key()), value);
                if (m_delegateProperties.contains(it.key())) row.item->setProperty(it.key().constData(), value);
            }
            if (row.item->property("index").isValid()) row.item->setProperty("index", i);
        } else if (!row.measured && (roles.isEmpty() || roles.contains(estimateRole))) {
            const qreal sectionHeight = row.sectionShown ? (m_sectionHeight >= 0 ? m_sectionHeight : EstimatedSectionHeight) : 0;
            row.height = estimate(i) + sectionHeight;
            m_prefixDirty = true;
        }
        if (sectionRole >= 0 && (roles.isEmpty() || roles.contains(sectionRole))) {
            const QString label = m_model->data(index, sectionRole).toString();
            if (label != row.sectionLabel) {
                row.sectionLabel = label;
                if (row.section) row.section->setProperty("section", label);
                updateSectionFlags(i);
            }
        }
    }
    scheduleLayout();
}

void TranscriptLayout::onItemHeightChanged() {
    if (m_inLayout) return; // relayout measures created rows itself
    m_prefixDirty = true;
    for (Row& row : m_rows) {
        if (row.item != sender() && row.section != sender()) continue;
        const qreal sectionHeight = row.section ? row.section->height() : 0;
        row.height = sectionHeight + (row.item ? row.item->height() : 0);
        row.measured = true;
        break;
    }
    scheduleLayout();
}

void TranscriptLayout::releaseRow(Row& row) {
    if (row.item) {
        disconnect(row.item, nullptr, this, nullptr);
        row.item->setVisible(false);
        row.item->deleteLater();
        row.item = nullptr;
    }
    if (row.section) {
        disconnect(row.section, nullptr, this, nullptr);
        row.section->setVisible(false);
        row.section->deleteLater();
        row.section = nullptr;
    }
    if (row.modelData) {
        row.modelData->deleteLater();
        row.modelData = nullptr;
    }
}

void TranscriptLayout::createRow(int index) {
    Row& row = m_rows[static_cast<size_t>(index)];
    if (row.item != nullptr || !m_delegate || !m_model) return;
    QQmlContext* context = m_delegate->creationContext();
    if (context == nullptr) context = qmlContext(this);
    if (context == nullptr) return;
    const QVariantMap values = roleValues(index);
    row.modelData = QQmlPropertyMap::create(this);
    for (auto it = values.cbegin(); it != values.cend(); ++it) row.modelData->insert(it.key(), it.value());

    QObject* object = m_delegate->beginCreate(context);
    auto* item = qobject_cast<QQuickItem*>(object);
    if (item == nullptr) {
        if (object) { m_delegate->completeCreate(); delete object; }
        return;
    }
    if (!m_delegatePropertiesKnown) {
        const QMetaObject* meta = object->metaObject();
        for (int p = 0; p < meta->propertyCount(); ++p) m_delegateProperties.insert(meta->property(p).name());
        m_delegatePropertiesKnown = true;
    }
    QVariantMap initial;
    for (auto it = values.cbegin(); it != values.cend(); ++it)
        if (m_delegateProperties.contains(it.key().toUtf8())) initial.insert(it.key(), it.value());
    if (m_delegateProperties.contains("model")) initial.insert(QStringLiteral("model"), QVariant::fromValue<QObject*>(row.modelData));
    if (m_delegateProperties.contains("index")) initial.insert(QStringLiteral("index"), index);
    m_delegate->setInitialProperties(object, initial);
    item->setParentItem(this);
    m_delegate->completeCreate();
    QQmlEngine::setObjectOwnership(item, QQmlEngine::CppOwnership);
    item->setX(m_leftMargin);
    item->setWidth(rowWidth());
    row.item = item;
    connect(item, &QQuickItem::heightChanged, this, &TranscriptLayout::onItemHeightChanged);

    if (row.sectionShown && m_sectionDelegate) {
        QObject* sectionObject = m_sectionDelegate->beginCreate(m_sectionDelegate->creationContext() ? m_sectionDelegate->creationContext() : context);
        if (auto* section = qobject_cast<QQuickItem*>(sectionObject)) {
            m_sectionDelegate->setInitialProperties(section, {{QStringLiteral("section"), row.sectionLabel}});
            section->setParentItem(this);
            m_sectionDelegate->completeCreate();
            QQmlEngine::setObjectOwnership(section, QQmlEngine::CppOwnership);
            section->setX(0);
            section->setWidth(width());
            row.section = section;
            m_sectionHeight = section->height();
            connect(section, &QQuickItem::heightChanged, this, &TranscriptLayout::onItemHeightChanged);
        } else if (sectionObject) {
            m_sectionDelegate->completeCreate();
            delete sectionObject;
        }
    }
}

void TranscriptLayout::correctViewport() {
    const qreal total = contentHeight();
    if (!qFuzzyCompare(height() + 1, total + 1)) {
        setImplicitHeight(total);
        setHeight(total);
        emit contentHeightChanged();
    }
    if (!m_flickable) return;
    qreal target = viewportY();
    if (m_following) target = endY();
    else if (m_anchorRow >= 0 && m_anchorRow < count()) target = positionOf(m_anchorRow) + m_anchorOffset;
    setViewportY(std::clamp<qreal>(target, 0, endY()));
}

bool TranscriptLayout::placeVisibleRows() {
    if (m_rows.empty()) return false;
    const qreal viewTop = viewportY();
    const qreal viewBottom = viewTop + viewportHeight();
    const int first = rowAt(viewTop - m_cacheExtent);
    const int last = rowAt(viewBottom + m_cacheExtent);
    const int visibleFirst = rowAt(viewTop);
    const int visibleLast = rowAt(viewBottom);
    for (int i = 0; i < count(); ++i) {
        if ((i < first || i > last) && (m_rows[static_cast<size_t>(i)].item || m_rows[static_cast<size_t>(i)].section))
            releaseRow(m_rows[static_cast<size_t>(i)]);
    }
    // Rows on screen first, top to bottom, then the margin nearest first.
    std::vector<int> order;
    for (int i = visibleFirst; i <= visibleLast; ++i) order.push_back(i);
    for (int step = 1; visibleFirst - step >= first || visibleLast + step <= last; ++step) {
        if (visibleLast + step <= last) order.push_back(visibleLast + step);
        if (visibleFirst - step >= first) order.push_back(visibleFirst - step);
    }
    QElapsedTimer clock;
    clock.start();
    bool changed = false;
    const qreal width = rowWidth();
    for (const int i : order) {
        Row& row = m_rows[static_cast<size_t>(i)];
        if (row.item == nullptr) {
            const bool visible = i >= visibleFirst && i <= visibleLast;
            const int budget = visible ? m_creationBudget : MarginCreationBudgetMs;
            if (budget > 0 && clock.elapsed() >= budget) {
                m_creationPending = true;
                continue;
            }
            createRow(i);
        }
        if (row.item == nullptr) continue;
        if (!qFuzzyCompare(row.item->width() + 1, width + 1)) row.item->setWidth(width);
        const qreal sectionHeight = row.section ? row.section->height() : 0;
        const qreal measured = sectionHeight + row.item->height();
        if (!row.measured || std::abs(measured - row.height) > 0.5) {
            if (std::abs(measured - row.height) > 0.5) changed = true;
            row.height = measured;
            row.measured = true;
            m_prefixDirty = true;
        }
    }
    rebuildPrefix();
    for (int i = first; i <= last; ++i) {
        Row& row = m_rows[static_cast<size_t>(i)];
        const qreal y = positionOf(i);
        if (row.section) { row.section->setY(y); row.section->setWidth(this->width()); }
        if (row.item) row.item->setY(y + (row.section ? row.section->height() : 0));
    }
    return changed;
}

void TranscriptLayout::relayout() {
    if (m_inLayout || !isComponentComplete()) return;
    m_inLayout = true;
    m_creationPending = false;
    for (int pass = 0; pass < MaxLayoutPasses; ++pass) {
        correctViewport();
        if (!placeVisibleRows()) break;
    }
    correctViewport();
    if (m_header) {
        m_header->setY(m_topMargin);
        m_header->setWidth(width());
    }
    if (m_footer) {
        rebuildPrefix();
        m_footer->setY(rowsTop() + m_prefix.back());
        m_footer->setWidth(width());
    }
    m_inLayout = false;
    // Rows left for later frames: continue on the next pass of the event loop.
    if (m_creationPending) QMetaObject::invokeMethod(this, &TranscriptLayout::scheduleLayout, Qt::QueuedConnection);
}
} // namespace clarp
