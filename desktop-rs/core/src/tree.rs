//! The depth-first walk TeamsPanel.qml uses for `parent_team_id`, keyed on
//! any parent id. Port of the C++ client's `TreeOrder.h`.

use std::collections::HashMap;

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct TreeNode {
    pub id: String,
    pub parent_id: String,
    /// Siblings are ordered by rank, then by their position in the input.
    pub rank: i32,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct TreePlacement {
    /// Position of the node in the input.
    pub index: usize,
    pub depth: usize,
}

/// A node whose parent is missing is a root; roots keep their input order and
/// each is followed by its subtree. Malformed cycles are appended as roots
/// afterwards, so no node ever disappears from the result.
pub fn tree_order(nodes: &[TreeNode]) -> Vec<TreePlacement> {
    let mut by_id: HashMap<&str, usize> = HashMap::new();
    for (index, node) in nodes.iter().enumerate() {
        if !node.id.is_empty() {
            by_id.entry(node.id.as_str()).or_insert(index);
        }
    }
    let is_root = |node: &TreeNode| {
        node.parent_id.is_empty() || !by_id.contains_key(node.parent_id.as_str()) || node.parent_id == node.id
    };
    let mut children: HashMap<&str, Vec<usize>> = HashMap::new();
    for (index, node) in nodes.iter().enumerate() {
        if !is_root(node) {
            children.entry(node.parent_id.as_str()).or_default().push(index);
        }
    }
    for siblings in children.values_mut() {
        siblings.sort_by_key(|&i| nodes[i].rank);
    }

    let mut result = Vec::with_capacity(nodes.len());
    let mut visited = vec![false; nodes.len()];
    // An explicit stack keeps a very deep or hostile hierarchy off the stack.
    let mut walk = |root: usize, result: &mut Vec<TreePlacement>| {
        let mut stack = vec![TreePlacement { index: root, depth: 0 }];
        while let Some(current) = stack.pop() {
            if visited[current.index] {
                continue;
            }
            visited[current.index] = true;
            result.push(current);
            let id = nodes[current.index].id.as_str();
            if id.is_empty() {
                continue;
            }
            for &kid in children.get(id).into_iter().flatten().rev() {
                if !visited[kid] {
                    stack.push(TreePlacement { index: kid, depth: current.depth + 1 });
                }
            }
        }
    };
    for (index, node) in nodes.iter().enumerate() {
        if is_root(node) {
            walk(index, &mut result);
        }
    }
    for index in 0..nodes.len() {
        walk(index, &mut result);
    }
    result
}
