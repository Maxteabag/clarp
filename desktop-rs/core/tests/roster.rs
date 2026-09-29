//! Ports of the AgentListModel, BackgroundJobTracker and TreeOrder cases in
//! desktop/tests/tst_native_core.cpp. Each step also replays the recorded ops
//! onto a mirror and checks it equals the presented rows.

use std::collections::HashMap;

use clarp_core::jobs::{JobCounts, JobTracker};
use clarp_core::list_ops::{ListOp, replay};
use clarp_core::roster::*;
use clarp_core::tree::{TreeNode, tree_order};
use serde_json::{Value, json};

struct Harness {
    roster: Roster,
    mirror: Vec<AgentRow>,
    ops: Vec<Op>,
}

fn obj(value: Value) -> serde_json::Map<String, Value> {
    value.as_object().cloned().expect("object literal")
}

impl Harness {
    fn new() -> Self {
        Self { roster: Roster::new(), mirror: Vec::new(), ops: Vec::new() }
    }
    fn sync(&mut self) {
        let ops = self.roster.take_ops();
        replay(&mut self.mirror, &ops);
        assert_eq!(self.mirror, self.roster.rows(), "mirror diverged after {ops:?}");
        self.ops.extend(ops);
    }
    fn snapshot(&mut self, agents: Value) {
        self.roster.apply_snapshot(&obj(json!({ "agents": agents })));
        self.sync();
    }
    fn with(&mut self, change: impl FnOnce(&mut Roster)) {
        change(&mut self.roster);
        self.sync();
    }
    fn row(&self, session: &str) -> &AgentRow {
        let row = self.roster.index_of_session(session).expect("session present");
        &self.mirror[row]
    }
    fn first(&self) -> &str {
        &self.mirror[0].session
    }
    fn clear(&mut self) {
        self.ops.clear();
    }
    fn count(&self, predicate: impl Fn(&Op) -> bool) -> usize {
        self.ops.iter().filter(|op| predicate(op)).count()
    }
}

fn agent(id: &str, session: &str, activity: i64, state_ts: i64, state: &str) -> Value {
    json!({"agent_id": id, "session": session, "persona": session.to_uppercase(),
           "last_activity": activity, "latest_state_ts": state_ts, "latest_state": state})
}

fn roster_row(session: &str, agent_id: &str, activity: i64, extra: Value) -> Value {
    let mut row = json!({"session": session, "agent_id": agent_id, "persona": session.to_uppercase(),
                         "last_activity": activity});
    for (k, v) in extra.as_object().cloned().unwrap_or_default() {
        row[k] = v;
    }
    row
}

fn helper_row(session: &str, parent: &str, state: &str, activity: i64) -> Value {
    roster_row(session, &format!("{session}-id"), activity,
               json!({"role": "helper", "parent_agent_id": parent, "helper_state": state}))
}

fn job(id: &str, agent_id: &str, status: &str, updated: i64, kind: &str) -> Value {
    json!({"job_id": id, "agent_id": agent_id, "session": format!("{agent_id}-session"), "kind": kind,
           "title": format!("{id} title"), "status": status, "started_at": updated - 1000,
           "heartbeat_at": updated, "updated_at": updated})
}

#[test]
fn snapshot_filters_archived_agents_and_patches_events() {
    let mut h = Harness::new();
    h.snapshot(json!([
        {"agent_id": "a", "session": "rachel", "persona": "Rachel", "latest_state": "idle",
         "schedules": [{"schedule_id": "sched-one", "enabled": true}]},
        {"agent_id": "b", "session": "old", "archived_at": 1}]));
    assert_eq!(h.mirror.len(), 1);
    assert_eq!(h.row("rachel").name, "Rachel");
    assert_eq!(h.row("rachel").schedules.len(), 1);
    h.with(|r| r.apply_state_event(&obj(json!({"session": "rachel", "kind": "thinking"}))));
    assert!(h.row("rachel").busy);
    h.with(|r| r.apply_notification_event(&obj(json!({"session": "rachel", "unread": true}))));
    assert!(h.row("rachel").unread);
    h.with(|r| r.clear_unread("rachel"));
    assert!(!h.row("rachel").unread);
    h.with(Roster::mark_transport_unavailable);
    assert_eq!(h.row("rachel").state, "offline");
    assert!(!h.row("rachel").busy);

    let mut archive = Roster::archived();
    archive.apply_snapshot(&obj(json!({"agents": [{"session": "live"}, {"session": "old", "archived_at": 5}]})));
    assert_eq!(archive.sessions(), ["old"]);
    assert!(!archive.upsert_created_agent(&obj(json!({"session": "n", "agent_id": "n"}))));
}

#[test]
fn agent_snapshot_diffs_in_place_and_rejects_stale_state() {
    let mut h = Harness::new();
    h.snapshot(json!([agent("a", "rachel", 100, 100, "idle"), agent("b", "mike", 200, 100, "idle")]));
    assert_eq!(h.first(), "mike");
    h.with(|r| r.apply_state_event(&obj(json!({"session": "rachel", "kind": "thinking", "status_text": "Working", "ts": 500}))));
    h.clear();
    h.snapshot(json!([agent("a", "rachel", 300, 400, "idle"), agent("b", "mike", 200, 100, "idle")]));
    assert_eq!(h.count(|op| matches!(op, ListOp::Reset(_))), 0);
    assert_eq!(h.count(|op| matches!(op, ListOp::Move { .. })), 1);
    assert_eq!(h.first(), "rachel");
    assert_eq!(h.row("rachel").state, "thinking");
    assert_eq!(h.row("rachel").status_text, "Working");

    let mut relaunched = Harness::new();
    relaunched.snapshot(json!([{"agent_id": "a", "session": "rachel", "conversation_id": "old", "head_revision": 8, "last_message": "Old answer"}]));
    relaunched.snapshot(json!([{"agent_id": "a", "session": "rachel", "conversation_id": "", "head_revision": 0, "last_message": ""}]));
    let row = relaunched.row("rachel");
    assert_eq!((row.conversation_id.as_str(), row.head_revision, row.last_message.as_str()), ("", 0, ""));

    let mut queues = Harness::new();
    queues.with(|r| r.apply_queue_event(&obj(json!({"session": "rachel", "queue_depth": 4, "queue_revision": 3}))));
    queues.snapshot(json!([{"agent_id": "a", "session": "rachel", "queued_turn_count": 0, "queue_revision": 1}]));
    assert_eq!(queues.row("rachel").queue_count, 4);
    queues.with(|r| r.apply_queue_event(&obj(json!({"session": "rachel", "queue_depth": 1, "queue_revision": 2}))));
    assert_eq!(queues.row("rachel").queue_count, 4);
    queues.with(|r| r.apply_queue_event(&obj(json!({"session": "rachel", "queue_depth": 5, "queue_revision": 4}))));
    assert_eq!(queues.row("rachel").queue_count, 5);

    let mut outgoing = Harness::new();
    outgoing.snapshot(json!([agent("a", "rachel", 100, 100, "idle"), agent("b", "mike", 200, 100, "idle")]));
    assert_eq!(outgoing.first(), "mike");
    let mut moved = false;
    outgoing.with(|r| moved = r.record_outgoing_activity("rachel"));
    assert!(moved);
    assert_eq!(outgoing.first(), "rachel");
    assert_eq!(outgoing.row("rachel").last_message, "");
    // The ranking holds until the Host reports newer activity for rachel.
    outgoing.snapshot(json!([agent("a", "rachel", 100, 100, "idle"), agent("b", "mike", 200, 100, "idle")]));
    assert_eq!(outgoing.first(), "rachel");
    outgoing.snapshot(json!([agent("a", "rachel", 250, 100, "idle"), agent("b", "mike", 300, 100, "idle")]));
    assert_eq!(outgoing.first(), "mike");
}

#[test]
fn snapshot_inserts_removes_and_moves_without_resetting() {
    let mut h = Harness::new();
    h.snapshot(json!([agent("a", "a", 3, 0, "idle"), agent("b", "b", 2, 0, "idle"), agent("c", "c", 1, 0, "idle")]));
    h.clear();
    h.snapshot(json!([agent("c", "c", 9, 0, "idle"), agent("d", "d", 5, 0, "idle"), agent("a", "a", 3, 0, "idle")]));
    assert_eq!(h.roster.sessions(), ["c", "d", "a"]);
    assert_eq!(h.count(|op| matches!(op, ListOp::Reset(_))), 0);
    assert_eq!(h.count(|op| matches!(op, ListOp::Remove { .. })), 1);
    assert_eq!(h.count(|op| matches!(op, ListOp::Insert { .. })), 1);
    assert_eq!(h.count(|op| matches!(op, ListOp::Signal(Signal::CountChanged))), 1);
    // Ties fall back to the name, case-insensitively, then the session.
    h.snapshot(json!([{"session": "z", "persona": "bob"}, {"session": "y", "persona": "Alice"}]));
    assert_eq!(h.roster.sessions(), ["y", "z"]);
}

#[test]
fn created_agents_are_prepended_and_upserted() {
    let mut h = Harness::new();
    h.snapshot(json!([agent("a", "a", 3, 0, "idle")]));
    h.with(|r| assert!(r.upsert_created_agent(&obj(json!({"agent_id": "n", "session": "new", "persona": "New"})))));
    assert_eq!(h.first(), "new");
    h.with(|r| r.apply_notification_event(&obj(json!({"session": "new"}))));
    h.with(|r| assert!(r.upsert_created_agent(&obj(json!({"agent_id": "n", "session": "new", "persona": "Renamed"})))));
    assert_eq!(h.row("new").name, "Renamed");
    assert!(h.row("new").unread, "unread survives the upsert");
    assert!(!h.roster.upsert_created_agent(&obj(json!({"session": "no-id"}))));
}

#[test]
fn background_job_tracker_keeps_only_active_jobs() {
    let mut tracker = JobTracker::default();
    assert!(!tracker.loaded());
    assert!(tracker.apply_list(&obj(json!({"jobs": [
        job("j1", "a", "running", 5000, "watch"), job("j2", "a", "queued", 3000, "sub-agent"),
        job("j3", "a", "succeeded", 4000, "watch"), job("j4", "b", "running", 4000, "watch"),
        {"status": "running"}, 7]}))));
    assert!(tracker.loaded());
    let counts = tracker.counts_by_agent();
    assert_eq!(counts["a"], JobCounts { total: 2, sub_agents: 1 });
    assert_eq!(counts["b"].total, 1);
    let jobs = tracker.active_jobs("a", "");
    assert_eq!(jobs.len(), 2);
    assert_eq!(jobs[0]["job_id"], "j2", "oldest first");

    let finished = job("j1", "a", "succeeded", 6000, "watch");
    assert!(tracker.apply_event(&obj(json!({"type": "background-job-updated", "job_id": "j1", "job": finished}))));
    assert_eq!(tracker.counts_by_agent()["a"].total, 1);
    // A replayed older event cannot bring a job back or rewind it.
    assert!(!tracker.apply_event(&obj(json!({"job": job("j4", "b", "succeeded", 1000, "watch")}))));
    assert_eq!(tracker.counts_by_agent()["b"].total, 1);
    let started = job("j5", "b", "running", 7000, "watch");
    assert!(tracker.apply_event(&obj(json!({"job": started.clone()}))));
    assert!(!tracker.apply_event(&obj(json!({"job": started}))), "same heartbeat is not a change");
    assert_eq!(tracker.counts_by_agent()["b"].total, 2);
    assert!(!tracker.apply_event(&obj(json!({"type": "background-job-updated"}))));
    tracker.apply_event(&obj(json!({"job": {"job_id": "orphan", "session": "s1", "status": "running"}})));
    assert_eq!(tracker.active_jobs("", "s1").len(), 1);
    assert!(!tracker.counts_by_agent().contains_key(""));
    assert!(tracker.clear());
    assert!(!tracker.loaded());
    assert!(tracker.counts_by_agent().is_empty());
    // A status carried only on the event wrapper still applies.
    tracker.apply_list(&obj(json!({"jobs": [job("w", "a", "running", 1, "watch")]})));
    assert!(tracker.apply_event(&obj(json!({"job_id": "w", "status": "failed", "job": {"job_id": "w", "updated_at": 2}}))));
    assert!(tracker.active_jobs("a", "").is_empty());
}

#[test]
fn roster_prefers_live_job_counts_and_counts_helpers() {
    let mut h = Harness::new();
    h.snapshot(json!([
        roster_row("parent", "p", 300, json!({"background_jobs": {"count": 1, "sub_agents": 1}, "latest_state": "background"})),
        helper_row("worker", "p", "running", 200),
        helper_row("old", "p", "done", 100),
        roster_row("quiet", "q", 50, json!({}))]));
    let parent = h.row("parent").clone();
    assert_eq!((parent.background_job_count, parent.sub_agent_count), (2, 1));
    assert_eq!(parent.running_children, 1, "old Host: visible running helper counts");
    assert_eq!(parent.process_count, 2);
    assert_eq!(h.row("quiet").process_count, 0);

    h.clear();
    h.with(|r| r.apply_live_job_counts(HashMap::from([("p".to_owned(), JobCounts { total: 3, sub_agents: 1 })])));
    assert_eq!(h.count(|op| matches!(op, ListOp::Update { roles, .. } if !roles.is_empty())), 1, "only the parent changed");
    assert_eq!(h.row("parent").background_job_count, 3);
    assert_eq!(h.row("parent").process_count, 3, "the helper's mirror job counts once");

    let mut tracker = JobTracker::default();
    tracker.apply_list(&obj(json!({"jobs": [job("watch", "p", "running", 10_000, "watch"),
                                          job("helper", "p", "running", 12_000, "sub-agent")]})));
    let processes = describe_agent_processes(&h.roster, &tracker, "parent", 70_000).unwrap();
    let jobs = processes["jobs"].as_array().unwrap();
    assert_eq!(jobs.len(), 2);
    assert_eq!(jobs[0]["elapsed"], "1m");
    assert_eq!(jobs[0]["heartbeat"], "1m ago");
    assert_eq!(jobs[1]["subAgent"], true);
    let helpers = processes["helpers"].as_array().unwrap();
    assert_eq!(helpers.len(), 1);
    assert_eq!(helpers[0]["session"], "worker");
    assert_eq!(processes["total"], 3);
    assert!(describe_agent_processes(&h.roster, &tracker, "missing", 0).is_none());

    h.with(Roster::clear_live_job_counts);
    assert_eq!(h.row("parent").background_job_count, 2);
    h.with(Roster::mark_transport_unavailable);
    assert_eq!(h.row("parent").process_count, 0);
}

#[test]
fn helper_state_changes_recount_the_parent() {
    let mut h = Harness::new();
    h.snapshot(json!([roster_row("parent", "p", 300, json!({}))]));
    h.with(|r| { r.upsert_created_agent(&obj(helper_row("worker", "p", "running", 1))); });
    assert_eq!(h.row("parent").running_children, 1);
    h.with(|r| { r.upsert_created_agent(&obj(helper_row("worker", "p", "reported", 1))); });
    assert_eq!(h.row("parent").running_children, 0);
}

#[test]
fn focus_events_mark_exactly_one_row() {
    let mut h = Harness::new();
    h.snapshot(json!([agent("a", "a", 2, 0, "idle"), agent("b", "b", 1, 0, "idle")]));
    h.with(|r| r.apply_focus_event(&obj(json!({"session": "b"}))));
    assert!(h.row("b").focused && !h.row("a").focused);
    h.with(|r| r.apply_focus_event(&obj(json!({"session": ""}))));
    assert!(!h.row("b").focused);
}

#[test]
fn next_attention_cycles_waiting_unread_and_pending() {
    let mut roster = Roster::new();
    let rows: Vec<Value> = ["a", "b", "c", "d"]
        .iter()
        .map(|n| json!({"session": n, "persona": n, "latest_state": if *n == "c" { "waiting" } else { "idle" }}))
        .collect();
    roster.apply_snapshot(&obj(json!({ "agents": rows })));
    roster.apply_notification_event(&obj(json!({"session": "b"})));
    let next = |r: &Roster, current: &str, pending: &[&str]| {
        r.next_attention_session(current, &pending.iter().map(|s| s.to_string()).collect::<Vec<_>>())
    };
    assert_eq!(next(&roster, "a", &[]).as_deref(), Some("b"));
    assert_eq!(next(&roster, "b", &[]).as_deref(), Some("c"));
    assert_eq!(next(&roster, "c", &[]).as_deref(), Some("b"));
    assert_eq!(next(&roster, "c", &["d"]).as_deref(), Some("d"));
    roster.clear_unread("b");
    assert_eq!(next(&roster, "a", &[]).as_deref(), Some("c"));
    assert_eq!(next(&roster, "c", &[]), None);
    assert_eq!(next(&roster, "", &["a"]).as_deref(), Some("a"));
    roster.apply_snapshot(&obj(json!({"agents": []})));
    assert_eq!(next(&roster, "", &["missing"]), None);
}

#[test]
fn first_session_prefers_a_working_agent() {
    let mut roster = Roster::new();
    roster.apply_snapshot(&obj(json!({"agents": [
        {"session": "janitor", "is_janitor": true, "last_activity": 9}, {"session": "work", "last_activity": 1}]})));
    assert_eq!(roster.first_session(), Some("work"));
    roster.apply_snapshot(&obj(json!({"agents": [{"session": "janitor", "is_janitor": true}]})));
    assert_eq!(roster.first_session(), Some("janitor"));
}

#[test]
fn tree_order_matches_the_team_walk() {
    let node = |id: &str, parent: &str, rank| TreeNode { id: id.into(), parent_id: parent.into(), rank };
    let nodes = [node("child", "root", 0), node("root", "", 0), node("orphan", "gone", 0),
                 node("grand", "child", 0), node("x", "y", 0), node("y", "x", 0)];
    let order = tree_order(&nodes);
    let ids: Vec<&str> = order.iter().map(|p| nodes[p.index].id.as_str()).collect();
    let depths: Vec<usize> = order.iter().map(|p| p.depth).collect();
    assert_eq!(ids, ["root", "child", "grand", "orphan", "x", "y"]);
    assert_eq!(depths, [0, 1, 2, 0, 0, 1]);
    let ranked = [node("p", "", 0), node("done", "p", 1), node("live", "p", 0)];
    assert_eq!(ranked[tree_order(&ranked)[1].index].id, "live");
}

#[test]
fn qt_move_destination_follows_begin_move_rows() {
    use clarp_core::list_ops::qt_move_destination;
    assert_eq!(qt_move_destination(0, 2), 3);
    assert_eq!(qt_move_destination(2, 0), 0);
    assert_eq!(qt_move_destination(3, 1), 1);
}
