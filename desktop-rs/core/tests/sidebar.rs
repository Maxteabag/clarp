//! Ports of the AgentFilterModel cases in desktop/tests/tst_native_core.cpp,
//! run against the pure sidebar logic over real roster rows.

use clarp_core::roster::Roster;
use clarp_core::sidebar::{FilterInput, Sidebar, TreeInput};
use serde_json::{Value, json};

fn obj(value: Value) -> serde_json::Map<String, Value> {
    value.as_object().cloned().expect("object literal")
}

fn roster_row(session: &str, agent_id: &str, activity: i64) -> Value {
    json!({"session": session, "agent_id": agent_id, "persona": session.to_uppercase(), "last_activity": activity})
}

fn helper_row(session: &str, parent: &str, state: &str, activity: i64) -> Value {
    let mut row = roster_row(session, &format!("{session}-id"), activity);
    row["role"] = json!("helper");
    row["parent_agent_id"] = json!(parent);
    row["helper_state"] = json!(state);
    row
}

struct View {
    roster: Roster,
    sidebar: Sidebar,
}

impl View {
    fn new() -> Self {
        Self { roster: Roster::new(), sidebar: Sidebar::default() }
    }
    fn snapshot(&mut self, agents: Value) {
        self.roster.apply_snapshot(&obj(json!({ "agents": agents })));
        self.refresh();
    }
    fn refresh(&mut self) {
        let trees: Vec<TreeInput> = self.roster.rows().iter().map(TreeInput::from).collect();
        self.sidebar.rebuild(&trees);
    }
    fn sessions(&self) -> Vec<String> {
        let rows = self.roster.rows();
        let trees: Vec<TreeInput> = rows.iter().map(TreeInput::from).collect();
        let filters: Vec<FilterInput> = rows.iter().map(FilterInput::from).collect();
        self.sidebar.visible(&trees, &filters).into_iter().map(str::to_owned).collect()
    }
}

#[test]
fn redesigned_roster_filters_without_mutating_source() {
    let mut v = View::new();
    v.snapshot(json!([
        {"session": "alpha", "persona": "Alpha", "backend": "codex", "cwd": "/work/one",
         "last_activity": 1_788_000_000_000_i64, "last_completed_message": "Completed preview"},
        {"session": "beta", "persona": "Beta", "backend": "claude", "cwd": "/work/two"}]));
    assert_eq!(v.sessions(), ["alpha", "beta"]);
    v.sidebar.query = "WORK/TWO".into();
    v.refresh();
    assert_eq!(v.sessions(), ["beta"]);
    assert_eq!(v.roster.len(), 2);
    v.sidebar.query.clear();
    v.sidebar.unread_only = true;
    v.refresh();
    assert!(v.sessions().is_empty());
    v.roster.apply_notification_event(&obj(json!({"session": "alpha"})));
    assert_eq!(v.sessions(), ["alpha"]);
    v.roster.clear_unread("alpha");
    assert!(v.sessions().is_empty());
}

#[test]
fn sidebar_nests_helpers_and_collapses_finished_ones() {
    let mut v = View::new();
    // An old Host sends no hierarchy: the recency order is untouched.
    v.snapshot(json!([roster_row("b", "b", 300), roster_row("a", "a", 200), roster_row("c", "c", 100)]));
    assert_eq!(v.sessions(), ["b", "a", "c"]);
    assert_eq!(v.sidebar.depth("b"), 0);
    assert!(v.sidebar.footers("b").is_empty());

    // Helpers are more recent than their parent yet never reach the top level.
    v.snapshot(json!([
        helper_row("done1", "p", "done", 900), helper_row("live", "p", "running", 800),
        roster_row("other", "o", 700), helper_row("done2", "p", "reported", 600),
        helper_row("failed", "p", "failed", 550), roster_row("parent", "p", 500),
        helper_row("stray", "deleted", "running", 400)]));
    assert_eq!(v.sessions(), ["other", "parent", "live", "failed", "stray"]);
    assert_eq!(v.sidebar.depth("failed"), 1);
    // The orphan stays discoverable at the top level, like a team cycle.
    assert_eq!(v.sidebar.depth("stray"), 0);
    // The "2 helpers done" line rides on the last visible row above them.
    let footers = v.sidebar.footers("failed");
    assert_eq!(footers.len(), 1);
    assert_eq!((footers[0].count, footers[0].parent_agent_id.as_str(), footers[0].expanded), (2, "p", false));

    v.sidebar.toggle_done_helpers("p");
    v.refresh();
    assert_eq!(v.sessions(), ["other", "parent", "live", "failed", "done1", "done2", "stray"]);
    assert!(v.sidebar.footers("failed")[0].expanded);
    v.sidebar.toggle_done_helpers("p");
    v.refresh();
    assert!(!v.sessions().contains(&"done1".to_owned()));
    assert!(v.sidebar.reveal("done1"));
    v.refresh();
    assert!(v.sessions().contains(&"done1".to_owned()));
    v.sidebar.toggle_done_helpers("p");
    v.refresh();

    // A search is flat and finds finished helpers too.
    v.sidebar.query = "DONE".into();
    v.refresh();
    assert_eq!(v.sessions(), ["done1", "done2"]);
    assert_eq!(v.sidebar.depth("done1"), 0);
    v.sidebar.query.clear();
    v.refresh();
    assert!(!v.sessions().contains(&"done1".to_owned()));

    // A state change arriving through the roster re-nests.
    v.snapshot(json!([helper_row("live", "p", "done", 800), roster_row("parent", "p", 500)]));
    assert_eq!(v.sessions(), ["parent"]);
    assert_eq!(v.sidebar.footers("parent")[0].count, 1);
}

#[test]
fn sidebar_order_survives_filter_round_trips() {
    let mut v = View::new();
    v.snapshot(json!([roster_row("b", "b", 300), roster_row("a", "a", 200)]));
    v.snapshot(json!([
        helper_row("live", "p", "running", 900), roster_row("b", "b", 300), roster_row("a", "a", 200),
        helper_row("done", "p", "done", 150), roster_row("parent", "p", 100)]));
    assert_eq!(v.sessions(), ["b", "a", "parent", "live"]);
    v.sidebar.toggle_done_helpers("p");
    v.refresh();
    assert_eq!(v.sessions(), ["b", "a", "parent", "live", "done"]);
    for (query, unread) in [("a", false), ("", false), ("", true), ("", false)] {
        v.sidebar.query = query.into();
        v.sidebar.unread_only = unread;
        v.refresh();
    }
    assert_eq!(v.sessions(), ["b", "a", "parent", "live", "done"]);
}

#[test]
fn hiding_is_transitive_and_reveal_expands_the_ancestor() {
    let mut v = View::new();
    v.snapshot(json!([
        roster_row("parent", "p", 500), helper_row("mid", "p", "done", 400),
        helper_row("leaf", "mid-id", "running", 300)]));
    assert_eq!(v.sessions(), ["parent"]);
    assert_eq!(v.sidebar.tree.hiding_parent.get("leaf").map(String::as_str), Some("p"));
    assert!(v.sidebar.reveal("leaf"));
    v.refresh();
    assert_eq!(v.sessions(), ["parent", "mid", "leaf"]);
    assert_eq!(v.sidebar.depth("leaf"), 2);
    assert!(!v.sidebar.reveal("parent"), "nothing hides a root");
}
