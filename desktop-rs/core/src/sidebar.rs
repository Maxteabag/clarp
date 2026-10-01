//! The sidebar's view of the roster: search, the unread scope, helper nesting
//! and collapsed "N helpers done" lines. Pure half of
//! `desktop/src/models/AgentFilterModel`; the Qt proxy asks this for filter
//! and sort decisions.

use std::collections::{HashMap, HashSet};

use crate::roster::AgentRow;
use crate::tree::{TreeNode, tree_order};

/// The roster fields the tree needs, read from the source model.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct TreeInput {
    pub session: String,
    pub agent_id: String,
    pub agent_role: String,
    pub parent_agent_id: String,
    pub helper_state: String,
}

/// The roster fields the search and scope need.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct FilterInput {
    pub session: String,
    pub unread: bool,
    pub name: String,
    pub backend: String,
    pub last_message: String,
    pub working_directory: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DoneHelpersLine {
    pub parent_agent_id: String,
    pub count: usize,
    pub expanded: bool,
    pub depth: usize,
}

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Tree {
    pub position: HashMap<String, usize>,
    pub depth: HashMap<String, usize>,
    pub hidden: HashSet<String>,
    /// Keyed by the session of the row that carries the line.
    pub footers: HashMap<String, Vec<DoneHelpersLine>>,
    pub hiding_parent: HashMap<String, String>,
}

pub fn finished_helper_state(state: &str) -> bool {
    matches!(state, "done" | "reported" | "abandoned")
}

#[derive(Debug, Default)]
pub struct Sidebar {
    pub query: String,
    pub unread_only: bool,
    pub expanded_parents: HashSet<String>,
    pub tree: Tree,
}

impl Sidebar {
    /// Nesting applies only without a search or scope; those are flat.
    pub fn tree_active(&self) -> bool {
        !self.unread_only && self.query.trim().is_empty()
    }

    /// Toggle the done-helpers line of a parent. Returns false for an empty id.
    pub fn toggle_done_helpers(&mut self, parent_agent_id: &str) -> bool {
        if parent_agent_id.is_empty() {
            return false;
        }
        if !self.expanded_parents.remove(parent_agent_id) {
            self.expanded_parents.insert(parent_agent_id.to_owned());
        }
        true
    }

    /// Expand whatever hides `session`; true when something changed.
    pub fn reveal(&mut self, session: &str) -> bool {
        match self.tree.hiding_parent.get(session) {
            Some(parent) => self.expanded_parents.insert(parent.clone()),
            None => false,
        }
    }

    pub fn rebuild(&mut self, rows: &[TreeInput]) {
        self.tree = build_tree(rows, self.tree_active(), &self.expanded_parents);
    }

    pub fn accepts(&self, row: &FilterInput) -> bool {
        if self.tree_active() && self.tree.hidden.contains(&row.session) {
            return false;
        }
        if self.unread_only && !row.unread {
            return false;
        }
        let needle = self.query.trim().to_lowercase();
        if needle.is_empty() {
            return true;
        }
        [&row.name, &row.backend, &row.session, &row.last_message, &row.working_directory]
            .iter()
            .any(|field| field.to_lowercase().contains(&needle))
    }

    /// Sort key: tree position, falling back to the source row.
    pub fn position(&self, session: &str, source_row: usize) -> usize {
        self.tree.position.get(session).copied().unwrap_or(source_row)
    }

    pub fn depth(&self, session: &str) -> usize {
        self.tree.depth.get(session).copied().unwrap_or(0)
    }

    pub fn footers(&self, session: &str) -> &[DoneHelpersLine] {
        self.tree.footers.get(session).map_or(&[], Vec::as_slice)
    }

    /// The rows a proxy would show, in order (for tests and non-Qt callers).
    pub fn visible<'a>(&self, trees: &[TreeInput], filters: &'a [FilterInput]) -> Vec<&'a str> {
        let mut rows: Vec<(usize, usize, &FilterInput)> = filters
            .iter()
            .enumerate()
            .filter(|(_, f)| self.accepts(f))
            .map(|(row, f)| (self.position(&trees[row].session, row), row, f))
            .collect();
        rows.sort_by_key(|(position, row, _)| (*position, *row));
        rows.into_iter().map(|(_, _, f)| f.session.as_str()).collect()
    }
}

/// Helpers (`role == "helper"` whose parent is listed) nest under their parent
/// with the team walk; finished ones sort after running siblings and collapse
/// into one line on the nearest visible row above them unless expanded.
pub fn build_tree(rows: &[TreeInput], tree_active: bool, expanded: &HashSet<String>) -> Tree {
    let mut tree = Tree::default();
    let mut nodes = Vec::with_capacity(rows.len());
    let mut finished = Vec::with_capacity(rows.len());
    for row in rows {
        let helper = row.agent_role == "helper";
        let parent = if helper { row.parent_agent_id.clone() } else { String::new() };
        let done = helper && !parent.is_empty() && finished_helper_state(&row.helper_state);
        nodes.push(TreeNode {
            id: if tree_active { row.agent_id.clone() } else { String::new() },
            parent_id: if tree_active { parent } else { String::new() },
            rank: i32::from(done),
        });
        finished.push(done);
    }
    let order = tree_order(&nodes);
    for (position, placement) in order.iter().enumerate() {
        let session = &rows[placement.index].session;
        tree.position.insert(session.clone(), position);
        tree.depth.insert(session.clone(), placement.depth);
    }
    if !tree_active {
        return tree;
    }

    let by_agent_id: HashMap<&str, usize> = rows
        .iter()
        .enumerate()
        .filter(|(_, r)| !r.agent_id.is_empty())
        .map(|(i, r)| (r.agent_id.as_str(), i))
        .collect();
    // Placed finished children of each parent, in display order.
    let mut done_by_parent: HashMap<&str, Vec<usize>> = HashMap::new();
    let mut parent_order: Vec<&str> = Vec::new();
    for (position, placement) in order.iter().enumerate() {
        let parent = nodes[placement.index].parent_id.as_str();
        if !finished[placement.index] || placement.depth == 0 || !by_agent_id.contains_key(parent) {
            continue;
        }
        if !done_by_parent.contains_key(parent) {
            parent_order.push(parent);
        }
        done_by_parent.entry(parent).or_default().push(position);
    }
    // Hiding is transitive: a helper under a collapsed finished helper is
    // hidden too, and revealing it expands the collapsed ancestor.
    let mut hidden_by: Vec<String> = vec![String::new(); order.len()];
    for (position, placement) in order.iter().enumerate() {
        if placement.depth == 0 {
            continue;
        }
        let parent = nodes[placement.index].parent_id.as_str();
        let parent_position = by_agent_id
            .get(parent)
            .and_then(|&row| tree.position.get(&rows[row].session))
            .copied();
        if let Some(pp) = parent_position.filter(|&pp| !hidden_by[pp].is_empty()) {
            hidden_by[position] = hidden_by[pp].clone();
        } else if finished[placement.index] && !expanded.contains(parent) {
            hidden_by[position] = parent.to_owned();
        }
        if !hidden_by[position].is_empty() {
            let session = rows[placement.index].session.clone();
            tree.hidden.insert(session.clone());
            tree.hiding_parent.insert(session, hidden_by[position].clone());
        }
    }
    for parent in parent_order {
        let positions = &done_by_parent[parent];
        // Attach to the nearest visible row above the first finished child:
        // the parent itself or its last running descendant.
        let anchor = (0..positions[0]).rev().find(|&p| hidden_by[p].is_empty());
        let Some(anchor) = anchor else { continue };
        if tree.hidden.contains(&rows[by_agent_id[parent]].session) {
            continue;
        }
        let anchor_session = rows[order[anchor].index].session.clone();
        tree.footers.entry(anchor_session).or_default().push(DoneHelpersLine {
            parent_agent_id: parent.to_owned(),
            count: positions.len(),
            expanded: expanded.contains(parent),
            depth: order[anchor].depth,
        });
    }
    tree
}

impl From<&AgentRow> for TreeInput {
    fn from(row: &AgentRow) -> Self {
        Self {
            session: row.session.clone(),
            agent_id: row.agent_id.clone(),
            agent_role: row.agent_role.clone(),
            parent_agent_id: row.parent_agent_id.clone(),
            helper_state: row.helper_state.clone(),
        }
    }
}

impl From<&AgentRow> for FilterInput {
    fn from(row: &AgentRow) -> Self {
        Self {
            session: row.session.clone(),
            unread: row.unread,
            name: row.name.clone(),
            backend: row.backend.clone(),
            last_message: row.last_message.clone(),
            working_directory: row.working_directory.clone(),
        }
    }
}
