//! The pane workspace: a binary split tree per workspace, focus, zoom,
//! spatial navigation and conflict-safe persistence. Port of
//! the C++ client's `PaneTreeModel`.

use std::collections::{BTreeMap, HashSet};
use std::sync::LazyLock;

use fancy_regex::Regex;
use serde_json::{Map, Value, json};

use crate::json::Object;

const MAXIMUM_DEPTH: usize = 8;
const MAXIMUM_PANES: usize = 16;
const MAXIMUM_WORKSPACES: usize = 8;

fn clamped_ratio(ratio: f64) -> f64 {
    ratio.clamp(0.15, 0.85)
}

#[derive(Debug, Clone, PartialEq)]
pub enum Node {
    Leaf { id: String, session: String },
    Split { id: String, direction: String, ratio: f64, first: Box<Node>, second: Box<Node> },
}

impl Node {
    fn leaf(id: String, session: String) -> Self {
        Self::Leaf { id, session }
    }

    pub fn id(&self) -> &str {
        match self {
            Self::Leaf { id, .. } | Self::Split { id, .. } => id,
        }
    }

    fn is_leaf(&self) -> bool {
        matches!(self, Self::Leaf { .. })
    }

    fn find(&self, target: &str) -> Option<&Node> {
        if target.is_empty() {
            return None;
        }
        if self.id() == target {
            return Some(self);
        }
        match self {
            Self::Leaf { .. } => None,
            Self::Split { first, second, .. } => first.find(target).or_else(|| second.find(target)),
        }
    }

    fn find_mut(&mut self, target: &str) -> Option<&mut Node> {
        if target.is_empty() {
            return None;
        }
        if self.id() == target {
            return Some(self);
        }
        match self {
            Self::Leaf { .. } => None,
            Self::Split { first, second, .. } => {
                if first.find(target).is_some() { first.find_mut(target) } else { second.find_mut(target) }
            }
        }
    }

    fn find_leaf(&self, target: &str) -> Option<&Node> {
        self.find(target).filter(|node| node.is_leaf())
    }

    fn leaves(&self) -> Vec<&Node> {
        match self {
            Self::Leaf { .. } => vec![self],
            Self::Split { first, second, .. } => {
                let mut out = first.leaves();
                out.extend(second.leaves());
                out
            }
        }
    }

    pub fn serialize(&self) -> Value {
        match self {
            Self::Leaf { id, session } => json!({"id": id, "kind": "leaf", "session": session}),
            Self::Split { id, direction, ratio, first, second } => json!({
                "id": id, "kind": "split", "direction": direction, "ratio": ratio,
                "first": first.serialize(), "second": second.serialize(),
            }),
        }
    }

    /// Replace the leaf `target` with a split of it and `new_leaf`.
    fn split(&mut self, target: &str, direction: &str, new_leaf: Node, split_id: String) -> bool {
        match self {
            Self::Leaf { id, .. } if id == target => {
                let old = std::mem::replace(self, Node::leaf(String::new(), String::new()));
                *self = Self::Split {
                    id: split_id,
                    direction: direction.to_owned(),
                    ratio: 0.5,
                    first: Box::new(old),
                    second: Box::new(new_leaf),
                };
                true
            }
            Self::Leaf { .. } => false,
            Self::Split { first, second, .. } => {
                if first.find(target).is_some() {
                    first.split(target, direction, new_leaf, split_id)
                } else {
                    second.split(target, direction, new_leaf, split_id)
                }
            }
        }
    }

    /// Remove leaf `target`; its sibling takes the parent's place.
    fn close(&mut self, target: &str) -> bool {
        let Self::Split { first, second, .. } = self else { return false };
        if first.is_leaf() && first.id() == target {
            *self = std::mem::replace(second.as_mut(), Node::leaf(String::new(), String::new()));
            return true;
        }
        if second.is_leaf() && second.id() == target {
            *self = std::mem::replace(first.as_mut(), Node::leaf(String::new(), String::new()));
            return true;
        }
        first.close(target) || second.close(target)
    }

    fn set_ratio(&mut self, split_id: &str, value: f64) -> bool {
        let Self::Split { id, ratio, first, second, .. } = self else { return false };
        if id == split_id {
            let next = clamped_ratio(value);
            if (*ratio - next).abs() <= 1e-12 * ratio.abs().max(next.abs()) {
                return false;
            }
            *ratio = next;
            return true;
        }
        first.set_ratio(split_id, value) || second.set_ratio(split_id, value)
    }

    fn resize_nearest(&mut self, target: &str, delta: f64) -> bool {
        let Self::Split { ratio, first, second, .. } = self else { return false };
        let in_first = first.find(target).is_some();
        if !in_first && second.find(target).is_none() {
            return false;
        }
        let child = if in_first { first } else { second };
        if !child.is_leaf() && child.resize_nearest(target, delta) {
            return true;
        }
        *ratio = clamped_ratio(*ratio + delta);
        true
    }

    fn equalize(&mut self) {
        if let Self::Split { ratio, first, second, .. } = self {
            *ratio = 0.5;
            first.equalize();
            second.equalize();
        }
    }

    fn layout(&self, x: f64, y: f64, width: f64, height: f64, panes: &mut Vec<Object>, splits: &mut Vec<Object>) {
        match self {
            Self::Leaf { .. } => {
                let mut pane = self.serialize().as_object().cloned().unwrap_or_default();
                pane.insert("x".into(), json!(x));
                pane.insert("y".into(), json!(y));
                pane.insert("width".into(), json!(width));
                pane.insert("height".into(), json!(height));
                panes.push(pane);
            }
            Self::Split { id, direction, ratio, first, second } => {
                splits.push(
                    json!({"id": id, "direction": direction, "ratio": ratio, "x": x, "y": y, "width": width, "height": height})
                        .as_object()
                        .cloned()
                        .unwrap_or_default(),
                );
                if direction == "vertical" {
                    let first_width = width * ratio;
                    first.layout(x, y, first_width, height, panes, splits);
                    second.layout(x + first_width, y, width - first_width, height, panes, splits);
                } else {
                    let first_height = height * ratio;
                    first.layout(x, y, width, first_height, panes, splits);
                    second.layout(x, y + first_height, width, height - first_height, panes, splits);
                }
            }
        }
    }

    fn panes(&self) -> Vec<Object> {
        let (mut panes, mut splits) = (Vec::new(), Vec::new());
        self.layout(0.0, 0.0, 1.0, 1.0, &mut panes, &mut splits);
        panes
    }
}

/// QVariant::toDouble for a JSON value: numbers and numeric strings.
fn variant_double(value: Option<&Value>, fallback: f64) -> f64 {
    match value {
        None => fallback,
        Some(Value::Number(n)) => n.as_f64().unwrap_or(0.0),
        Some(Value::String(s)) => s.trim().parse().unwrap_or(0.0),
        Some(Value::Bool(b)) => f64::from(u8::from(*b)),
        Some(_) => 0.0,
    }
}

fn text(value: Option<&Value>) -> String {
    value.and_then(Value::as_str).unwrap_or_default().to_owned()
}

/// Rebuild a tree from saved JSON, rejecting anything malformed, deeper than
/// 8 levels or with more than 16 panes. Tracks the highest numeric id.
pub fn deserialize(value: &Value, depth: usize, leaf_count: &mut usize, highest: &mut u64) -> Option<Node> {
    static ID: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^(?:pane|split)-(\d+)$").expect("pattern"));
    if depth > MAXIMUM_DEPTH {
        return None;
    }
    let id = text(value.get("id"));
    let kind = text(value.get("kind"));
    let number = ID.captures(&id).ok().flatten()?.get(1)?.as_str().parse::<u64>().unwrap_or(0);
    if kind != "leaf" && kind != "split" {
        return None;
    }
    *highest = (*highest).max(number);
    if kind == "leaf" {
        *leaf_count += 1;
        if *leaf_count > MAXIMUM_PANES {
            return None;
        }
        return Some(Node::leaf(id, text(value.get("session"))));
    }
    let direction = if text(value.get("direction")) == "horizontal" { "horizontal" } else { "vertical" };
    let ratio = clamped_ratio(variant_double(value.get("ratio"), 0.5));
    let first = deserialize(value.get("first").unwrap_or(&Value::Null), depth + 1, leaf_count, highest)?;
    let second = deserialize(value.get("second").unwrap_or(&Value::Null), depth + 1, leaf_count, highest)?;
    Some(Node::Split { id, direction: direction.into(), ratio, first: Box::new(first), second: Box::new(second) })
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Signal {
    TreeChanged,
    ActivePaneChanged,
    WorkspaceSaveWarningChanged,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct WriteRequest {
    pub encoded: String,
    pub expected_collection: String,
    pub recovery_id: String,
    pub force: bool,
}

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct WriteResult {
    pub encoded: String,
    pub saved_collection: bool,
    pub conflict: bool,
    pub settings_error: bool,
    /// On a conflict: the newer collection another window saved.
    pub latest: String,
}

/// Where the workspace collection lives. Writes are compare-and-swap: a
/// writer that lost the lock, or saw a collection it did not expect, keeps
/// its layout in its own recovery slot and never overwrites a newer one.
pub trait WorkspaceStore: Send + Sync {
    fn read_collection(&self) -> String;
    fn write(&self, request: &WriteRequest) -> WriteResult;
}

#[derive(Debug)]
pub struct PaneTree {
    root: Node,
    active_pane_id: String,
    zoomed_pane_id: String,
    next_id: u64,
    active_workspace: String,
    workspace_states: BTreeMap<String, Object>,
    workspace_names: BTreeMap<String, String>,
    persistence: bool,
    last_collection: String,
    recovery_id: String,
    save_warning: String,
    /// The newer collection a conflict found, accepted when dismissed.
    conflict_latest: String,
    pending_write: Option<WriteRequest>,
    write_in_flight: bool,
    signals: Vec<Signal>,
}

impl Default for PaneTree {
    fn default() -> Self {
        Self::new()
    }
}

impl PaneTree {
    /// A single empty pane, not persisted.
    pub fn new() -> Self {
        Self {
            root: Node::leaf("pane-1".into(), String::new()),
            active_pane_id: "pane-1".into(),
            zoomed_pane_id: String::new(),
            next_id: 1,
            active_workspace: "workspace-1".into(),
            workspace_states: BTreeMap::new(),
            workspace_names: BTreeMap::from([("workspace-1".into(), "Main".into())]),
            persistence: false,
            last_collection: String::new(),
            recovery_id: uuid::Uuid::new_v4().to_string(),
            save_warning: String::new(),
            conflict_latest: String::new(),
            pending_write: None,
            write_in_flight: false,
            signals: Vec::new(),
        }
    }

    /// Persisted to `store`. `restore` is false for an empty startup.
    pub fn persisted(store: &dyn WorkspaceStore, restore: bool) -> Self {
        let mut tree = Self::new();
        tree.persistence = true;
        tree.last_collection = store.read_collection();
        if restore {
            tree.restore_collection();
        }
        tree
    }

    fn restore_collection(&mut self) {
        let Ok(Value::Object(document)) = serde_json::from_str::<Value>(&self.last_collection) else { return };
        let states = document.get("states").and_then(Value::as_object).cloned().unwrap_or_default();
        let names = document.get("names").and_then(Value::as_object).cloned().unwrap_or_default();
        let active = text(document.get("active"));
        let version = document.get("version").and_then(Value::as_i64).unwrap_or(0);
        if version != 1 || states.is_empty() || states.len() > MAXIMUM_WORKSPACES || !states.contains_key(&active) {
            return;
        }
        let mut all_ids = HashSet::new();
        let mut highest_seen = self.next_id;
        for (key, state) in &states {
            let (mut count, mut highest) = (0, 0);
            let Some(tree) = deserialize(state.get("root").unwrap_or(&Value::Null), 0, &mut count, &mut highest) else {
                return;
            };
            if !names.contains_key(key) {
                return;
            }
            for leaf in tree.leaves() {
                if !all_ids.insert(leaf.id().to_owned()) {
                    return;
                }
            }
            if tree.find_leaf(&text(state.get("activePaneId"))).is_none() {
                return;
            }
            highest_seen = highest_seen.max(highest);
        }
        self.next_id = highest_seen;
        self.workspace_states =
            states.iter().map(|(k, v)| (k.clone(), v.as_object().cloned().unwrap_or_default())).collect();
        self.workspace_names = names.iter().map(|(k, v)| (k.clone(), text(Some(v)))).collect();
        self.active_workspace = active.clone();
        let state = self.workspace_states[&active].clone();
        self.load_state(&state);
    }

    // ---- read side -------------------------------------------------------

    pub fn take_signals(&mut self) -> Vec<Signal> {
        std::mem::take(&mut self.signals)
    }
    pub fn root_node(&self) -> Value {
        self.root.serialize()
    }
    pub fn display_root(&self) -> Value {
        match self.root.find(&self.zoomed_pane_id) {
            Some(zoomed) => zoomed.serialize(),
            None => self.root_node(),
        }
    }
    pub fn pane_layout(&self) -> Vec<Object> {
        self.root.find(&self.zoomed_pane_id).unwrap_or(&self.root).panes()
    }
    pub fn split_layout(&self) -> Vec<Object> {
        let (mut panes, mut splits) = (Vec::new(), Vec::new());
        if self.zoomed_pane_id.is_empty() {
            self.root.layout(0.0, 0.0, 1.0, 1.0, &mut panes, &mut splits);
        }
        splits
    }
    pub fn active_pane_id(&self) -> &str {
        &self.active_pane_id
    }
    pub fn active_session(&self) -> String {
        match self.root.find(&self.active_pane_id) {
            Some(Node::Leaf { session, .. }) => session.clone(),
            _ => String::new(),
        }
    }
    pub fn zoomed_pane_id(&self) -> &str {
        &self.zoomed_pane_id
    }
    pub fn pane_count(&self) -> usize {
        self.root.leaves().len()
    }
    pub fn active_workspace(&self) -> &str {
        &self.active_workspace
    }
    pub fn workspace_save_warning(&self) -> &str {
        &self.save_warning
    }
    pub fn workspaces(&self) -> Vec<Object> {
        self.workspace_names
            .iter()
            .map(|(id, name)| json!({"id": id, "name": name}).as_object().cloned().unwrap_or_default())
            .collect()
    }

    /// Every pane of every workspace, so views can keep hidden workspaces'
    /// panes alive; only the active workspace's panes are `shown`.
    pub fn view_layout(&self) -> Vec<Object> {
        let mut out = Vec::new();
        for mut row in self.root.panes() {
            let id = text(row.get("id"));
            row.insert("shown".into(), json!(self.zoomed_pane_id.is_empty() || self.zoomed_pane_id == id));
            if self.zoomed_pane_id == id {
                for (key, value) in [("x", 0), ("y", 0), ("width", 1), ("height", 1)] {
                    row.insert(key.into(), json!(value));
                }
            }
            out.push(row);
        }
        for (key, state) in &self.workspace_states {
            if *key == self.active_workspace {
                continue;
            }
            let (mut count, mut highest) = (0, 0);
            if let Some(tree) = deserialize(state.get("root").unwrap_or(&Value::Null), 0, &mut count, &mut highest) {
                for mut row in tree.panes() {
                    row.insert("shown".into(), json!(false));
                    out.push(row);
                }
            }
        }
        out
    }

    pub fn save_state(&self) -> Object {
        json!({"root": self.root_node(), "activePaneId": self.active_pane_id, "zoomedPaneId": self.zoomed_pane_id})
            .as_object()
            .cloned()
            .unwrap_or_default()
    }

    // ---- signals and persistence ------------------------------------------

    fn emit_tree(&mut self) {
        self.signals.push(Signal::TreeChanged);
        self.persist_workspaces(false);
    }

    fn emit_active(&mut self) {
        self.signals.push(Signal::ActivePaneChanged);
        self.persist_workspaces(false);
    }

    fn encoded_collection(&self) -> String {
        let mut states: Map<String, Value> =
            self.workspace_states.iter().map(|(k, v)| (k.clone(), Value::Object(v.clone()))).collect();
        states.insert(self.active_workspace.clone(), Value::Object(self.save_state()));
        let names: Map<String, Value> =
            self.workspace_names.iter().map(|(k, v)| (k.clone(), Value::from(v.as_str()))).collect();
        json!({"version": 1, "active": self.active_workspace, "names": names, "states": states}).to_string()
    }

    fn persist_workspaces(&mut self, force: bool) {
        if !self.persistence {
            return;
        }
        self.pending_write = Some(WriteRequest {
            encoded: self.encoded_collection(),
            expected_collection: self.last_collection.clone(),
            recovery_id: self.recovery_id.clone(),
            force,
        });
    }

    /// Dismiss the conflict warning, accepting the newer layout another
    /// window saved as this window's base: nothing is overwritten now, and
    /// this window's next change saves normally instead of conflicting
    /// again. Its own layout stays in recovery.
    pub fn dismiss_workspace_save_warning(&mut self) {
        if self.save_warning.is_empty() {
            return;
        }
        if !self.conflict_latest.is_empty() {
            self.last_collection = std::mem::take(&mut self.conflict_latest);
            let base = self.last_collection.clone();
            if let Some(pending) = self.pending_write.as_mut().filter(|p| !p.force) {
                pending.expected_collection = base;
            }
        }
        self.save_warning.clear();
        self.signals.push(Signal::WorkspaceSaveWarningChanged);
    }

    /// Keep this window's layout even though another window saved newer.
    pub fn save_workspace_layout_instead(&mut self) {
        self.persist_workspaces(true);
    }

    /// The next write to run, one at a time; newer layouts replace a queued
    /// one while a write is in flight.
    pub fn next_write(&mut self) -> Option<WriteRequest> {
        if self.write_in_flight {
            return None;
        }
        let request = self.pending_write.take()?;
        self.write_in_flight = true;
        Some(request)
    }

    pub fn finish_write(&mut self, result: &WriteResult) {
        self.write_in_flight = false;
        if result.saved_collection {
            self.last_collection = result.encoded.clone();
            if let Some(pending) = self.pending_write.as_mut().filter(|p| !p.force) {
                pending.expected_collection = result.encoded.clone();
            }
        }
        if result.conflict {
            self.conflict_latest = result.latest.clone();
        }
        let warning = if result.settings_error {
            "Workspace layout could not be saved. Check available storage."
        } else if result.conflict {
            "Another window saved a newer layout. This window's layout is kept in recovery."
        } else {
            ""
        };
        if warning != self.save_warning {
            self.save_warning = warning.to_owned();
            self.signals.push(Signal::WorkspaceSaveWarningChanged);
        }
    }

    /// Run every queued write synchronously (no event loop, or shutdown).
    pub fn flush_writes(&mut self, store: &dyn WorkspaceStore) {
        while let Some(request) = self.next_write() {
            let result = store.write(&request);
            self.finish_write(&result);
        }
    }

    pub fn has_pending_write(&self) -> bool {
        self.pending_write.is_some()
    }

    // ---- mutations -------------------------------------------------------

    fn next_id(&mut self, prefix: &str) -> String {
        self.next_id += 1;
        format!("{prefix}-{}", self.next_id)
    }

    pub fn load_state(&mut self, state: &Object) -> bool {
        let (mut count, mut highest) = (0, 0);
        let Some(tree) = deserialize(state.get("root").unwrap_or(&Value::Null), 0, &mut count, &mut highest) else {
            return false;
        };
        if count == 0 {
            return false;
        }
        let mut ids = HashSet::new();
        fn unique(node: &Node, ids: &mut HashSet<String>) -> bool {
            if !ids.insert(node.id().to_owned()) {
                return false;
            }
            match node {
                Node::Leaf { .. } => true,
                Node::Split { first, second, .. } => unique(first, ids) && unique(second, ids),
            }
        }
        if !unique(&tree, &mut ids) {
            return false;
        }
        let active = text(state.get("activePaneId"));
        if tree.find_leaf(&active).is_none() {
            return false;
        }
        let zoom = text(state.get("zoomedPaneId"));
        if !zoom.is_empty() && tree.find_leaf(&zoom).is_none() {
            return false;
        }
        self.root = tree;
        self.active_pane_id = active;
        self.zoomed_pane_id = zoom;
        self.next_id = self.next_id.max(highest);
        self.emit_tree();
        self.emit_active();
        true
    }

    pub fn create_workspace(&mut self, requested_name: &str) {
        if self.workspace_names.len() >= MAXIMUM_WORKSPACES {
            return;
        }
        self.workspace_states.insert(self.active_workspace.clone(), self.save_state());
        let id = format!("workspace-{}", uuid::Uuid::new_v4());
        let pane = self.next_id("pane");
        self.root = Node::leaf(pane.clone(), String::new());
        self.active_pane_id = pane;
        self.zoomed_pane_id.clear();
        self.active_workspace = id.clone();
        let name = requested_name.trim();
        self.workspace_names.insert(id, if name.is_empty() { "Workspace".into() } else { name.chars().take(60).collect() });
        self.emit_tree();
        self.emit_active();
    }

    pub fn switch_workspace(&mut self, id: &str) {
        if id == self.active_workspace || !self.workspace_states.contains_key(id) {
            return;
        }
        self.workspace_states.insert(self.active_workspace.clone(), self.save_state());
        let state = self.workspace_states[id].clone();
        self.active_workspace = id.to_owned();
        self.load_state(&state);
    }

    /// Closes workspace `id`; the active one gives way to its neighbour in
    /// the tab bar. The last workspace stays.
    pub fn close_workspace(&mut self, id: &str) {
        if self.workspace_names.len() <= 1 || !self.workspace_names.contains_key(id) {
            return;
        }
        let ids: Vec<String> = self.workspace_names.keys().cloned().collect();
        self.workspace_names.remove(id);
        self.workspace_states.remove(id);
        if id != self.active_workspace {
            self.emit_tree();
            return;
        }
        let at = ids.iter().position(|w| w == id).unwrap_or(0);
        let next = ids.get(at + 1).or_else(|| at.checked_sub(1).and_then(|i| ids.get(i))).cloned().unwrap_or_default();
        self.active_workspace = next.clone();
        let state = self.workspace_states.get(&next).cloned().unwrap_or_default();
        if !self.load_state(&state) {
            // A neighbour without a saved layout (never left) starts empty.
            let pane = self.next_id("pane");
            self.root = Node::leaf(pane.clone(), String::new());
            self.active_pane_id = pane;
            self.zoomed_pane_id.clear();
            self.emit_tree();
            self.emit_active();
        }
    }

    pub fn move_active_to_workspace(&mut self, id: &str) {
        if id == self.active_workspace || !self.workspace_states.contains_key(id) {
            return;
        }
        let Some(Node::Leaf { id: pane, session }) = self.root.find(&self.active_pane_id).cloned() else { return };
        if session.is_empty() {
            return;
        }
        let target_state = self.workspace_states[id].clone();
        let (mut count, mut highest) = (0, 0);
        let Some(mut target) = deserialize(target_state.get("root").unwrap_or(&Value::Null), 0, &mut count, &mut highest)
        else {
            return;
        };
        if count >= MAXIMUM_PANES {
            return;
        }
        let target_active = text(target_state.get("activePaneId"));
        let split_id = self.next_id("split");
        if !target.split(&target_active, "vertical", Node::leaf(pane.clone(), session), split_id) {
            return;
        }
        if self.pane_count() == 1 {
            let fresh = self.next_id("pane");
            self.root = Node::leaf(fresh.clone(), String::new());
            self.active_pane_id = fresh;
        } else {
            self.root.close(&pane);
            self.active_pane_id = self.root.leaves()[0].id().to_owned();
        }
        self.zoomed_pane_id.clear();
        self.workspace_states.insert(self.active_workspace.clone(), self.save_state());
        self.root = target;
        self.active_pane_id = pane;
        self.active_workspace = id.to_owned();
        self.emit_tree();
        self.emit_active();
    }

    pub fn set_active_session(&mut self, new_session: &str) {
        let active = self.active_pane_id.clone();
        match self.root.find_mut(&active) {
            Some(Node::Leaf { session, .. }) if session != new_session => *session = new_session.to_owned(),
            _ => return,
        }
        self.emit_tree();
        self.emit_active();
    }

    pub fn set_pane_session(&mut self, pane_id: &str, new_session: &str) {
        match self.root.find_mut(pane_id) {
            Some(Node::Leaf { session, .. }) if session != new_session => *session = new_session.to_owned(),
            _ => return,
        }
        self.active_pane_id = pane_id.to_owned();
        self.zoomed_pane_id.clear();
        self.emit_tree();
        self.emit_active();
    }

    pub fn split_active(&mut self, direction: &str, session: &str) {
        let Some(Node::Leaf { session: active_session, .. }) = self.root.find(&self.active_pane_id).cloned() else { return };
        let pane = self.next_id("pane");
        let session = if session.is_empty() { active_session } else { session.to_owned() };
        let direction = if direction == "horizontal" { "horizontal" } else { "vertical" };
        let split_id = self.next_id("split");
        let active = self.active_pane_id.clone();
        if self.root.split(&active, direction, Node::leaf(pane.clone(), session), split_id) {
            self.active_pane_id = pane;
            self.zoomed_pane_id.clear();
            self.emit_tree();
            self.emit_active();
        }
    }

    pub fn close_pane(&mut self, pane_id: &str) {
        if self.pane_count() <= 1 || self.root.find(pane_id).is_none() {
            return;
        }
        let active_closed = self.active_pane_id == pane_id;
        if !self.root.close(pane_id) {
            return;
        }
        if active_closed {
            self.active_pane_id = self.root.leaves().first().map(|l| l.id().to_owned()).unwrap_or_default();
        }
        if self.zoomed_pane_id == pane_id {
            self.zoomed_pane_id.clear();
        }
        self.emit_tree();
        self.emit_active();
    }

    pub fn focus_pane(&mut self, pane_id: &str) {
        if self.root.find_leaf(pane_id).is_none() {
            return;
        }
        let active_changed = self.active_pane_id != pane_id;
        let zoom_changed = !self.zoomed_pane_id.is_empty() && self.zoomed_pane_id != pane_id;
        if !active_changed && !zoom_changed {
            return;
        }
        self.active_pane_id = pane_id.to_owned();
        if zoom_changed {
            self.zoomed_pane_id = pane_id.to_owned();
            self.emit_tree();
        }
        if active_changed {
            self.emit_active();
        }
    }

    fn move_focus(&mut self, target: String) {
        self.active_pane_id = target.clone();
        if !self.zoomed_pane_id.is_empty() {
            self.zoomed_pane_id = target;
            self.emit_tree();
        }
        self.emit_active();
    }

    /// prev/next cycle in layout order; left/right/up/down follow the
    /// rendered rectangles and stop at an outer edge instead of wrapping.
    pub fn navigate(&mut self, direction: &str) {
        let panes = self.root.panes();
        if panes.len() <= 1 {
            return;
        }
        let number = |pane: &Object, key: &str| pane.get(key).and_then(Value::as_f64).unwrap_or(0.0);
        let Some(index) = panes.iter().position(|p| text(p.get("id")) == self.active_pane_id) else { return };
        if direction == "prev" || direction == "next" {
            let len = panes.len();
            let next = if direction == "prev" { (index + len - 1) % len } else { (index + 1) % len };
            self.move_focus(text(panes[next].get("id")));
            return;
        }
        let horizontal = direction == "left" || direction == "right";
        let vertical = direction == "up" || direction == "down";
        if !horizontal && !vertical {
            return;
        }
        let negative = direction == "left" || direction == "up";
        let current = &panes[index];
        let (cx, cy, cw, ch) = (number(current, "x"), number(current, "y"), number(current, "width"), number(current, "height"));
        let (ccx, ccy) = (cx + cw / 2.0, cy + ch / 2.0);
        let gap = |first_start: f64, first_end: f64, second_start: f64, second_end: f64| {
            if first_end < second_start {
                second_start - first_end
            } else if second_end < first_start {
                first_start - second_end
            } else {
                0.0
            }
        };
        let mut best: Option<([f64; 5], String)> = None;
        for candidate in &panes {
            let candidate_id = text(candidate.get("id"));
            if candidate_id == self.active_pane_id {
                continue;
            }
            let (x, y, w, h) = (number(candidate, "x"), number(candidate, "y"), number(candidate, "width"), number(candidate, "height"));
            let (center_x, center_y) = (x + w / 2.0, y + h / 2.0);
            let primary_delta = if horizontal { center_x - ccx } else { center_y - ccy };
            if (negative && primary_delta >= -1e-9) || (!negative && primary_delta <= 1e-9) {
                continue;
            }
            let extends_past_edge = match direction {
                "left" => x < cx - 1e-9,
                "right" => x + w > cx + cw + 1e-9,
                "up" => y < cy - 1e-9,
                _ => y + h > cy + ch + 1e-9,
            };
            if !extends_past_edge {
                continue;
            }
            let orthogonal_gap = if horizontal { gap(cy, cy + ch, y, y + h) } else { gap(cx, cx + cw, x, x + w) };
            let primary_gap = match (horizontal, negative) {
                (true, true) => (cx - (x + w)).max(0.0),
                (true, false) => (x - (cx + cw)).max(0.0),
                (false, true) => (cy - (y + h)).max(0.0),
                (false, false) => (y - (cy + ch)).max(0.0),
            };
            let orthogonal_center = if horizontal { (center_y - ccy).abs() } else { (center_x - ccx).abs() };
            let score = [
                if orthogonal_gap > 1e-9 { 1.0 } else { 0.0 },
                primary_gap,
                orthogonal_gap,
                orthogonal_center,
                primary_delta.abs(),
            ];
            let better = best.as_ref().is_none_or(|(best_score, _)| {
                score.partial_cmp(best_score) == Some(std::cmp::Ordering::Less)
            });
            if better {
                best = Some((score, candidate_id));
            }
        }
        if let Some((_, target)) = best {
            self.move_focus(target);
        }
    }

    pub fn toggle_zoom(&mut self) {
        self.zoomed_pane_id =
            if self.zoomed_pane_id == self.active_pane_id { String::new() } else { self.active_pane_id.clone() };
        self.emit_tree();
    }

    pub fn resize_active(&mut self, delta: f64) {
        let active = self.active_pane_id.clone();
        if self.root.resize_nearest(&active, delta) {
            self.emit_tree();
        }
    }

    pub fn set_split_ratio(&mut self, split_id: &str, ratio: f64) {
        if self.root.set_ratio(split_id, ratio) {
            self.emit_tree();
        }
    }

    pub fn equalize(&mut self) {
        self.root.equalize();
        self.emit_tree();
    }
}

/// A JSON file store: `{"collectionV1": "..."}` guarded by an exclusive lock
/// file and replaced atomically. Each window's recovery copy is its own file
/// under `<path>.recovery/`, so it is kept even when the lock is taken.
pub struct FileWorkspaceStore {
    path: std::path::PathBuf,
}

impl FileWorkspaceStore {
    pub fn new(path: impl Into<std::path::PathBuf>) -> Self {
        Self { path: path.into() }
    }

    fn lock_path(&self) -> std::path::PathBuf {
        let mut name = self.path.clone().into_os_string();
        name.push(".workspace-save.lock");
        name.into()
    }

    fn load(&self) -> Object {
        std::fs::read_to_string(&self.path)
            .ok()
            .and_then(|text| serde_json::from_str::<Value>(&text).ok())
            .and_then(|value| value.as_object().cloned())
            .unwrap_or_default()
    }

    fn save(&self, document: &Object) -> std::io::Result<()> {
        if let Some(parent) = self.path.parent() {
            std::fs::create_dir_all(parent)?;
        }
        let mut temporary = self.path.clone().into_os_string();
        temporary.push(".tmp");
        std::fs::write(&temporary, Value::Object(document.clone()).to_string())?;
        std::fs::rename(&temporary, &self.path)
    }

    fn recovery_dir(&self) -> std::path::PathBuf {
        let mut name = self.path.clone().into_os_string();
        name.push(".recovery");
        name.into()
    }

    fn recovery_path(&self, id: &str) -> std::path::PathBuf {
        // Ids are UUIDs; anything else cannot name a path outside the dir.
        let safe: String = id.chars().filter(|c| c.is_ascii_alphanumeric() || *c == '-').collect();
        self.recovery_dir().join(format!("{safe}.json"))
    }

    /// Recovery copies by window id, for tests and a future restore action.
    pub fn recovery(&self) -> Object {
        let Ok(entries) = std::fs::read_dir(self.recovery_dir()) else { return Object::new() };
        entries
            .filter_map(Result::ok)
            .filter_map(|entry| {
                let path = entry.path();
                let id = path.file_stem()?.to_string_lossy().into_owned();
                Some((id, Value::from(std::fs::read_to_string(&path).ok()?)))
            })
            .collect()
    }

    /// Replace the collection directly (another writer, in tests).
    pub fn set_collection(&self, encoded: &str) -> std::io::Result<()> {
        let mut document = self.load();
        document.insert("collectionV1".into(), Value::from(encoded));
        self.save(&document)
    }
}

struct LockGuard(std::path::PathBuf);

impl Drop for LockGuard {
    fn drop(&mut self) {
        if let Err(error) = std::fs::remove_file(&self.0) {
            eprintln!("workspace store: could not release lock {}: {error}", self.0.display());
        }
    }
}

impl WorkspaceStore for FileWorkspaceStore {
    fn read_collection(&self) -> String {
        text(self.load().get("collectionV1"))
    }

    fn write(&self, request: &WriteRequest) -> WriteResult {
        let mut result = WriteResult { encoded: request.encoded.clone(), ..WriteResult::default() };
        if let Some(parent) = self.path.parent()
            && let Err(error) = std::fs::create_dir_all(parent) {
                eprintln!("workspace store: {error}");
                result.settings_error = true;
                return result;
            }
        let lock = std::fs::OpenOptions::new().write(true).create_new(true).open(self.lock_path());
        let _guard = lock.is_ok().then(|| LockGuard(self.lock_path()));
        let mut document = self.load();
        let latest = text(document.get("collectionV1"));
        if lock.is_err() || (!request.force && latest != request.expected_collection) {
            // Preserve the unsaved window independently; never overwrite a
            // newer writer.
            result.conflict = true;
            result.latest = latest.clone();
            let path = self.recovery_path(&request.recovery_id);
            let saved = std::fs::create_dir_all(self.recovery_dir()).and_then(|()| std::fs::write(&path, &request.encoded));
            if let Err(error) = saved {
                eprintln!("workspace store: could not save recovery {}: {error}", path.display());
                result.settings_error = true;
            }
            return result;
        }
        document.insert("collectionV1".into(), Value::from(request.encoded.as_str()));
        match self.save(&document) {
            Ok(()) => {
                result.saved_collection = true;
                let recovery = self.recovery_path(&request.recovery_id);
                if recovery.exists()
                    && let Err(error) = std::fs::remove_file(&recovery) {
                        eprintln!("workspace store: could not drop recovery {}: {error}", recovery.display());
                    }
            }
            Err(error) => {
                eprintln!("workspace store: could not save layout: {error}");
                result.settings_error = true;
            }
        }
        result
    }
}
