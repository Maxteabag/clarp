#include "models/PaneTreeModel.h"

#include <QCoreApplication>
#include <QJsonDocument>
#include <QJsonObject>
#include <QRegularExpression>
#include <QSettings>
#include <algorithm>
#include <array>
#include <cmath>
#include <utility>

namespace clarp {
namespace {

double clampedRatio(double ratio) { return std::clamp(ratio, 0.15, 0.85); }

} // namespace

PaneTreeModel::PaneTreeModel(QObject* parent)
    : QObject(parent), m_root(std::make_unique<Node>()), m_activePaneId(QStringLiteral("pane-1")),
      m_nextId(1) {
    m_root->id = m_activePaneId;
    m_persistenceEnabled = QCoreApplication::organizationName() == QStringLiteral("MaxTeaBag") &&
                           QCoreApplication::applicationName() == QStringLiteral("Clarp");
    restore();
    connect(this, &PaneTreeModel::treeChanged, this, &PaneTreeModel::persist);
    connect(this, &PaneTreeModel::activePaneChanged, this, &PaneTreeModel::persist);
}

PaneTreeModel::~PaneTreeModel() = default;

QVariantMap PaneTreeModel::rootNode() const { return serialize(m_root.get()); }

QVariantMap PaneTreeModel::displayRoot() const {
    if (!m_zoomedPaneId.isEmpty()) {
        return serialize(find(m_root.get(), m_zoomedPaneId));
    }
    return rootNode();
}

QVariantList PaneTreeModel::paneLayout() const {
    QVariantList panes;
    QVariantList splits;
    const Node* root = m_zoomedPaneId.isEmpty() ? m_root.get() : find(m_root.get(), m_zoomedPaneId);
    appendLayout(root, 0.0, 0.0, 1.0, 1.0, panes, splits);
    return panes;
}

QVariantList PaneTreeModel::splitLayout() const {
    QVariantList panes;
    QVariantList splits;
    if (m_zoomedPaneId.isEmpty()) {
        appendLayout(m_root.get(), 0.0, 0.0, 1.0, 1.0, panes, splits);
    }
    return splits;
}

QString PaneTreeModel::activePaneId() const { return m_activePaneId; }

QString PaneTreeModel::activeSession() const {
    const Node* active = find(m_root.get(), m_activePaneId);
    return active == nullptr ? QString{} : active->session;
}

QString PaneTreeModel::zoomedPaneId() const { return m_zoomedPaneId; }

int PaneTreeModel::paneCount() const {
    std::vector<const Node*> leaves;
    collectLeaves(m_root.get(), leaves);
    return static_cast<int>(leaves.size());
}

void PaneTreeModel::setActiveSession(const QString& session) {
    Node* active = find(m_root.get(), m_activePaneId);
    if (active == nullptr || active->kind != Node::Kind::Leaf || active->session == session) {
        return;
    }
    active->session = session;
    emit treeChanged();
    emit activePaneChanged();
}

void PaneTreeModel::setPaneSession(const QString& paneId, const QString& session) {
    Node* pane = find(m_root.get(), paneId);
    if (pane == nullptr || pane->kind != Node::Kind::Leaf || pane->session == session) {
        return;
    }
    pane->session = session;
    m_activePaneId = paneId;
    m_zoomedPaneId.clear();
    emit treeChanged();
    emit activePaneChanged();
}

void PaneTreeModel::splitActive(const QString& direction, const QString& session) {
    Node* active = find(m_root.get(), m_activePaneId);
    if (active == nullptr || active->kind != Node::Kind::Leaf) {
        return;
    }
    auto newLeaf = std::make_unique<Node>();
    newLeaf->id = nextId(QStringLiteral("pane"));
    newLeaf->session = session.isEmpty() ? active->session : session;
    QString newActiveId;
    const QString normalizedDirection = direction == QStringLiteral("horizontal")
                                            ? QStringLiteral("horizontal")
                                            : QStringLiteral("vertical");
    const QString splitId = nextId(QStringLiteral("split"));
    if (split(m_root, m_activePaneId, normalizedDirection, std::move(newLeaf), newActiveId,
              splitId)) {
        m_activePaneId = newActiveId;
        m_zoomedPaneId.clear();
        emit treeChanged();
        emit activePaneChanged();
    }
}

void PaneTreeModel::closePane(const QString& paneId) {
    if (paneCount() <= 1 || find(m_root.get(), paneId) == nullptr) {
        return;
    }
    const bool activeClosed = m_activePaneId == paneId;
    if (!close(m_root, paneId)) {
        return;
    }
    if (activeClosed) {
        std::vector<Node*> leaves;
        collectLeaves(m_root.get(), leaves);
        m_activePaneId = leaves.empty() ? QString{} : leaves.front()->id;
    }
    if (m_zoomedPaneId == paneId) {
        m_zoomedPaneId.clear();
    }
    emit treeChanged();
    emit activePaneChanged();
}

void PaneTreeModel::focusPane(const QString& paneId) {
    const Node* pane = find(m_root.get(), paneId);
    if (pane == nullptr || pane->kind != Node::Kind::Leaf) {
        return;
    }
    const bool activeChanged = m_activePaneId != paneId;
    const bool zoomChanged = !m_zoomedPaneId.isEmpty() && m_zoomedPaneId != paneId;
    if (!activeChanged && !zoomChanged) {
        return;
    }
    m_activePaneId = paneId;
    if (zoomChanged) {
        m_zoomedPaneId = paneId;
        emit treeChanged();
    }
    if (activeChanged) {
        emit activePaneChanged();
    }
}

void PaneTreeModel::navigate(const QString& direction) {
    QVariantList panes;
    QVariantList splits;
    appendLayout(m_root.get(), 0.0, 0.0, 1.0, 1.0, panes, splits);
    if (panes.size() <= 1) {
        return;
    }

    const auto currentIt =
        std::ranges::find_if(std::as_const(panes), [this](const QVariant& value) {
            return value.toMap().value(QStringLiteral("id")).toString() == m_activePaneId;
        });
    if (currentIt == panes.end()) {
        return;
    }

    if (direction == QStringLiteral("prev") || direction == QStringLiteral("next")) {
        const qsizetype index = std::distance(panes.cbegin(), currentIt);
        const qsizetype delta = direction == QStringLiteral("prev") ? -1 : 1;
        const qsizetype next = (index + delta + panes.size()) % panes.size();
        const QString target = panes.at(next).toMap().value(QStringLiteral("id")).toString();
        m_activePaneId = target;
        if (!m_zoomedPaneId.isEmpty()) {
            m_zoomedPaneId = target;
            emit treeChanged();
        }
        emit activePaneChanged();
        return;
    }

    const bool horizontal =
        direction == QStringLiteral("left") || direction == QStringLiteral("right");
    const bool vertical = direction == QStringLiteral("up") || direction == QStringLiteral("down");
    if (!horizontal && !vertical) {
        return;
    }
    const bool negative = direction == QStringLiteral("left") || direction == QStringLiteral("up");
    const QVariantMap current = currentIt->toMap();
    const double currentX = current.value(QStringLiteral("x")).toDouble();
    const double currentY = current.value(QStringLiteral("y")).toDouble();
    const double currentWidth = current.value(QStringLiteral("width")).toDouble();
    const double currentHeight = current.value(QStringLiteral("height")).toDouble();
    const double currentCenterX = currentX + currentWidth / 2.0;
    const double currentCenterY = currentY + currentHeight / 2.0;

    QString bestId;
    std::array<double, 5> bestScore{};
    bool hasBest = false;
    for (const QVariant& value : std::as_const(panes)) {
        const QVariantMap candidate = value.toMap();
        const QString candidateId = candidate.value(QStringLiteral("id")).toString();
        if (candidateId == m_activePaneId) {
            continue;
        }
        const double x = candidate.value(QStringLiteral("x")).toDouble();
        const double y = candidate.value(QStringLiteral("y")).toDouble();
        const double width = candidate.value(QStringLiteral("width")).toDouble();
        const double height = candidate.value(QStringLiteral("height")).toDouble();
        const double centerX = x + width / 2.0;
        const double centerY = y + height / 2.0;
        const double primaryDelta =
            horizontal ? centerX - currentCenterX : centerY - currentCenterY;
        if ((negative && primaryDelta >= -1e-9) || (!negative && primaryDelta <= 1e-9)) {
            continue;
        }
        const bool extendsPastEdge =
            direction == QStringLiteral("left")    ? x < currentX - 1e-9
            : direction == QStringLiteral("right") ? x + width > currentX + currentWidth + 1e-9
            : direction == QStringLiteral("up")    ? y < currentY - 1e-9
                                                   : y + height > currentY + currentHeight + 1e-9;
        if (!extendsPastEdge) {
            continue;
        }

        const auto intervalGap = [](double firstStart, double firstEnd, double secondStart,
                                    double secondEnd) {
            if (firstEnd < secondStart) {
                return secondStart - firstEnd;
            }
            return secondEnd < firstStart ? firstStart - secondEnd : 0.0;
        };
        const double orthogonalGap =
            horizontal ? intervalGap(currentY, currentY + currentHeight, y, y + height)
                       : intervalGap(currentX, currentX + currentWidth, x, x + width);
        const double primaryGap = horizontal
                                      ? (negative ? std::max(0.0, currentX - (x + width))
                                                  : std::max(0.0, x - (currentX + currentWidth)))
                                      : (negative ? std::max(0.0, currentY - (y + height))
                                                  : std::max(0.0, y - (currentY + currentHeight)));
        const double orthogonalCenter =
            horizontal ? std::abs(centerY - currentCenterY) : std::abs(centerX - currentCenterX);
        const std::array score{orthogonalGap > 1e-9 ? 1.0 : 0.0, primaryGap, orthogonalGap,
                               orthogonalCenter, std::abs(primaryDelta)};
        if (!hasBest || score < bestScore) {
            bestId = candidateId;
            bestScore = score;
            hasBest = true;
        }
    }

    // Spatial movement stops at an outer edge instead of wrapping to an
    // unrelated pane on the opposite side of the workspace.
    if (!hasBest) {
        return;
    }
    m_activePaneId = bestId;
    if (!m_zoomedPaneId.isEmpty()) {
        m_zoomedPaneId = bestId;
        emit treeChanged();
    }
    emit activePaneChanged();
}

void PaneTreeModel::toggleZoom() {
    m_zoomedPaneId = m_zoomedPaneId == m_activePaneId ? QString{} : m_activePaneId;
    emit treeChanged();
}

void PaneTreeModel::resizeActive(double delta) {
    if (resizeNearest(m_root.get(), m_activePaneId, delta)) {
        emit treeChanged();
    }
}

void PaneTreeModel::setSplitRatio(const QString& splitId, double ratio) {
    if (setRatio(m_root.get(), splitId, ratio)) {
        emit treeChanged();
    }
}

void PaneTreeModel::equalize() {
    equalizeNode(m_root.get());
    emit treeChanged();
}

QString PaneTreeModel::nextId(const QString& prefix) {
    ++m_nextId;
    return prefix + QLatin1Char('-') + QString::number(m_nextId);
}

QVariantMap PaneTreeModel::serialize(const Node* node) {
    if (node == nullptr) {
        return {};
    }
    QVariantMap value{
        {QStringLiteral("id"), node->id},
        {QStringLiteral("kind"),
         node->kind == Node::Kind::Leaf ? QStringLiteral("leaf") : QStringLiteral("split")},
    };
    if (node->kind == Node::Kind::Leaf) {
        value.insert(QStringLiteral("session"), node->session);
    } else {
        value.insert(QStringLiteral("direction"), node->direction);
        value.insert(QStringLiteral("ratio"), node->ratio);
        value.insert(QStringLiteral("first"), serialize(node->first.get()));
        value.insert(QStringLiteral("second"), serialize(node->second.get()));
    }
    return value;
}

PaneTreeModel::Node* PaneTreeModel::find(Node* node, const QString& id) {
    if (node == nullptr || id.isEmpty()) {
        return nullptr;
    }
    if (node->id == id) {
        return node;
    }
    if (node->kind == Node::Kind::Leaf) {
        return nullptr;
    }
    if (Node* first = find(node->first.get(), id)) {
        return first;
    }
    return find(node->second.get(), id);
}

const PaneTreeModel::Node* PaneTreeModel::find(const Node* node, const QString& id) {
    if (node == nullptr || id.isEmpty()) {
        return nullptr;
    }
    if (node->id == id) {
        return node;
    }
    if (node->kind == Node::Kind::Leaf) {
        return nullptr;
    }
    if (const Node* first = find(node->first.get(), id)) {
        return first;
    }
    return find(node->second.get(), id);
}

void PaneTreeModel::collectLeaves(Node* node, std::vector<Node*>& output) {
    if (node == nullptr) {
        return;
    }
    if (node->kind == Node::Kind::Leaf) {
        output.push_back(node);
        return;
    }
    collectLeaves(node->first.get(), output);
    collectLeaves(node->second.get(), output);
}

void PaneTreeModel::collectLeaves(const Node* node, std::vector<const Node*>& output) {
    if (node == nullptr) {
        return;
    }
    if (node->kind == Node::Kind::Leaf) {
        output.push_back(node);
        return;
    }
    collectLeaves(node->first.get(), output);
    collectLeaves(node->second.get(), output);
}

bool PaneTreeModel::split(std::unique_ptr<Node>& node, const QString& targetId,
                          const QString& direction, std::unique_ptr<Node> newLeaf,
                          QString& newActiveId, const QString& splitId) {
    if (node == nullptr) {
        return false;
    }
    if (node->kind == Node::Kind::Leaf && node->id == targetId) {
        newActiveId = newLeaf->id;
        auto splitNode = std::make_unique<Node>();
        splitNode->kind = Node::Kind::Split;
        splitNode->id = splitId;
        splitNode->direction = direction;
        splitNode->first = std::move(node);
        splitNode->second = std::move(newLeaf);
        node = std::move(splitNode);
        return true;
    }
    if (node->kind == Node::Kind::Leaf) {
        return false;
    }
    if (find(node->first.get(), targetId) != nullptr) {
        return split(node->first, targetId, direction, std::move(newLeaf), newActiveId, splitId);
    }
    return split(node->second, targetId, direction, std::move(newLeaf), newActiveId, splitId);
}

bool PaneTreeModel::close(std::unique_ptr<Node>& node, const QString& targetId) {
    if (node == nullptr || node->kind == Node::Kind::Leaf) {
        return false;
    }
    if (node->first->kind == Node::Kind::Leaf && node->first->id == targetId) {
        node = std::move(node->second);
        return true;
    }
    if (node->second->kind == Node::Kind::Leaf && node->second->id == targetId) {
        node = std::move(node->first);
        return true;
    }
    return close(node->first, targetId) || close(node->second, targetId);
}

bool PaneTreeModel::setRatio(Node* node, const QString& splitId, double ratio) {
    if (node == nullptr || node->kind == Node::Kind::Leaf) {
        return false;
    }
    if (node->id == splitId) {
        const double next = clampedRatio(ratio);
        if (qFuzzyCompare(node->ratio, next)) {
            return false;
        }
        node->ratio = next;
        return true;
    }
    return setRatio(node->first.get(), splitId, ratio) ||
           setRatio(node->second.get(), splitId, ratio);
}

bool PaneTreeModel::resizeNearest(Node* node, const QString& targetId, double delta) {
    if (node == nullptr || node->kind == Node::Kind::Leaf) {
        return false;
    }
    const bool inFirst = find(node->first.get(), targetId) != nullptr;
    const bool inSecond = find(node->second.get(), targetId) != nullptr;
    if (!inFirst && !inSecond) {
        return false;
    }
    Node* child = inFirst ? node->first.get() : node->second.get();
    if (child->kind == Node::Kind::Split && resizeNearest(child, targetId, delta)) {
        return true;
    }
    node->ratio = clampedRatio(node->ratio + delta);
    return true;
}

void PaneTreeModel::equalizeNode(Node* node) {
    if (node == nullptr || node->kind == Node::Kind::Leaf) {
        return;
    }
    node->ratio = 0.5;
    equalizeNode(node->first.get());
    equalizeNode(node->second.get());
}

void PaneTreeModel::appendLayout(const Node* node, double x, double y, double width, double height,
                                 QVariantList& panes, QVariantList& splits) {
    if (node == nullptr) {
        return;
    }
    if (node->kind == Node::Kind::Leaf) {
        QVariantMap pane = serialize(node);
        pane.insert(QStringLiteral("x"), x);
        pane.insert(QStringLiteral("y"), y);
        pane.insert(QStringLiteral("width"), width);
        pane.insert(QStringLiteral("height"), height);
        panes.append(pane);
        return;
    }

    splits.append(QVariantMap{
        {QStringLiteral("id"), node->id},
        {QStringLiteral("direction"), node->direction},
        {QStringLiteral("ratio"), node->ratio},
        {QStringLiteral("x"), x},
        {QStringLiteral("y"), y},
        {QStringLiteral("width"), width},
        {QStringLiteral("height"), height},
    });

    if (node->direction == QStringLiteral("vertical")) {
        const double firstWidth = width * node->ratio;
        appendLayout(node->first.get(), x, y, firstWidth, height, panes, splits);
        appendLayout(node->second.get(), x + firstWidth, y, width - firstWidth, height, panes,
                     splits);
    } else {
        const double firstHeight = height * node->ratio;
        appendLayout(node->first.get(), x, y, width, firstHeight, panes, splits);
        appendLayout(node->second.get(), x, y + firstHeight, width, height - firstHeight, panes,
                     splits);
    }
}

std::unique_ptr<PaneTreeModel::Node> PaneTreeModel::deserialize(const QVariantMap& value, int depth,
                                                                int& leafCount,
                                                                quint64& highestId) {
    constexpr int MaximumDepth = 8;
    constexpr int MaximumPanes = 16;
    if (depth > MaximumDepth) {
        return nullptr;
    }
    const QString id = value.value(QStringLiteral("id")).toString();
    const QString kind = value.value(QStringLiteral("kind")).toString();
    static const QRegularExpression idPattern(QStringLiteral(R"(^(?:pane|split)-(\d+)$)"));
    const QRegularExpressionMatch idMatch = idPattern.match(id);
    if (!idMatch.hasMatch() ||
        (kind != QStringLiteral("leaf") && kind != QStringLiteral("split"))) {
        return nullptr;
    }
    highestId = std::max(highestId, idMatch.captured(1).toULongLong());

    auto node = std::make_unique<Node>();
    node->id = id;
    if (kind == QStringLiteral("leaf")) {
        ++leafCount;
        if (leafCount > MaximumPanes) {
            return nullptr;
        }
        node->session = value.value(QStringLiteral("session")).toString();
        return node;
    }

    node->kind = Node::Kind::Split;
    node->direction = value.value(QStringLiteral("direction")).toString();
    if (node->direction != QStringLiteral("horizontal")) {
        node->direction = QStringLiteral("vertical");
    }
    node->ratio = clampedRatio(value.value(QStringLiteral("ratio"), 0.5).toDouble());
    node->first =
        deserialize(value.value(QStringLiteral("first")).toMap(), depth + 1, leafCount, highestId);
    node->second =
        deserialize(value.value(QStringLiteral("second")).toMap(), depth + 1, leafCount, highestId);
    if (node->first == nullptr || node->second == nullptr) {
        return nullptr;
    }
    return node;
}

void PaneTreeModel::restore() {
    if (!m_persistenceEnabled) {
        return;
    }
    const QByteArray encoded =
        QSettings().value(QStringLiteral("workspace/paneTree")).toByteArray();
    const QJsonDocument document = QJsonDocument::fromJson(encoded);
    if (!document.isObject()) {
        return;
    }
    const QJsonObject state = document.object();
    int leafCount = 0;
    quint64 highestId = 0;
    std::unique_ptr<Node> restored = deserialize(
        state.value(QStringLiteral("root")).toObject().toVariantMap(), 0, leafCount, highestId);
    if (restored == nullptr || leafCount == 0) {
        return;
    }
    const QString activeId = state.value(QStringLiteral("activePaneId")).toString();
    const Node* active = find(restored.get(), activeId);
    m_root = std::move(restored);
    m_activePaneId = active != nullptr && active->kind == Node::Kind::Leaf
                         ? activeId
                         : serialize(m_root.get()).value(QStringLiteral("id")).toString();
    if (find(m_root.get(), m_activePaneId)->kind != Node::Kind::Leaf) {
        std::vector<Node*> leaves;
        collectLeaves(m_root.get(), leaves);
        m_activePaneId = leaves.front()->id;
    }
    const QString zoomedId = state.value(QStringLiteral("zoomedPaneId")).toString();
    const Node* zoomed = find(m_root.get(), zoomedId);
    m_zoomedPaneId = zoomed != nullptr && zoomed->kind == Node::Kind::Leaf ? zoomedId : QString{};
    m_nextId = std::max(m_nextId, highestId);
}

void PaneTreeModel::persist() const {
    if (!m_persistenceEnabled) {
        return;
    }
    const QJsonObject state{
        {QStringLiteral("root"), QJsonObject::fromVariantMap(rootNode())},
        {QStringLiteral("activePaneId"), m_activePaneId},
        {QStringLiteral("zoomedPaneId"), m_zoomedPaneId},
    };
    QSettings().setValue(QStringLiteral("workspace/paneTree"),
                         QJsonDocument(state).toJson(QJsonDocument::Compact));
}

} // namespace clarp
