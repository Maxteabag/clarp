#pragma once

#include <QHash>
#include <QString>
#include <QVector>
#include <algorithm>
#include <ranges>

namespace clarp {

struct TreeNode {
    QString id;
    QString parentId;
    // Siblings are ordered by rank, then by their position in the input.
    int rank = 0;
};

struct TreePlacement {
    int index = 0; // Position of the node in the input.
    int depth = 0;
};

// The depth-first walk TeamsPanel.qml uses for `parent_team_id`, keyed on any
// parent id: a node whose parent is missing is a root, roots keep their input
// order and each is followed by its subtree. Malformed cycles are appended as
// roots afterwards, so no node ever disappears from the result.
[[nodiscard]] inline QVector<TreePlacement> treeOrder(const QVector<TreeNode>& nodes) {
    QHash<QString, int> byId;
    for (int index = 0; index < nodes.size(); ++index) {
        if (!nodes.at(index).id.isEmpty() && !byId.contains(nodes.at(index).id)) {
            byId.insert(nodes.at(index).id, index);
        }
    }
    QHash<QString, QVector<int>> children;
    for (int index = 0; index < nodes.size(); ++index) {
        const QString& parent = nodes.at(index).parentId;
        if (!parent.isEmpty() && byId.contains(parent) && parent != nodes.at(index).id) {
            children[parent].append(index);
        }
    }
    for (QVector<int>& siblings : children) {
        std::ranges::stable_sort(siblings, [&nodes](int left, int right) {
            return nodes.at(left).rank < nodes.at(right).rank;
        });
    }

    QVector<TreePlacement> result;
    result.reserve(nodes.size());
    QVector<bool> visited(nodes.size(), false);
    // An explicit stack keeps a very deep or hostile hierarchy off the C++ stack.
    auto append = [&](int root) {
        QVector<TreePlacement> stack{{.index = root, .depth = 0}};
        while (!stack.isEmpty()) {
            const TreePlacement current = stack.takeLast();
            if (visited.at(current.index)) continue;
            visited[current.index] = true;
            result.append(current);
            const QString& id = nodes.at(current.index).id;
            if (id.isEmpty()) continue;
            // Pushed in reverse so the first sibling is visited first.
            const QVector<int> kids = children.value(id);
            for (const int kid : std::views::reverse(kids)) {
                if (!visited.at(kid)) stack.append({.index = kid, .depth = current.depth + 1});
            }
        }
    };
    for (int index = 0; index < nodes.size(); ++index) {
        const TreeNode& node = nodes.at(index);
        if (node.parentId.isEmpty() || !byId.contains(node.parentId) || node.parentId == node.id) {
            append(index);
        }
    }
    for (int index = 0; index < nodes.size(); ++index) {
        append(index);
    }
    return result;
}

} // namespace clarp
