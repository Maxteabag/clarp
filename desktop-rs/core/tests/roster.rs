//! Ports of the AgentListModel, BackgroundJobTracker and TreeOrder cases in
//! the C++ client's `tst_native_core.cpp`. Each step also replays the recorded ops
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
    // The whole queue, in the order Ctrl+J visits it (the explorer numbers it).
    let queue = |current: &str, pending: &[&str]| roster.attention_queue(current, &pending.iter().map(|s| s.to_string()).collect::<Vec<_>>());
    assert_eq!(queue("c", &["d"]), ["d", "b"]);
    assert_eq!(queue("a", &["d"]), ["b", "c", "d"]);
    roster.clear_unread("b");
    assert_eq!(next(&roster, "a", &[]).as_deref(), Some("c"));
    assert_eq!(next(&roster, "c", &[]), None);
    assert_eq!(next(&roster, "", &["a"]).as_deref(), Some("a"));
    roster.apply_snapshot(&obj(json!({"agents": []})));
    assert_eq!(next(&roster, "", &["missing"]), None);
}

#[test]
fn a_quiet_agent_is_never_unread_or_in_the_attention_queue() {
    let mut roster = Roster::new();
    roster.apply_snapshot(&obj(json!({"agents": [
        {"session": "a", "persona": "a"},
        {"session": "quiet", "persona": "quiet", "quiet": true, "latest_state": "waiting"},
        {"session": "old-host-janitor", "persona": "j", "is_janitor": true, "latest_state": "waiting"},
        {"session": "work", "persona": "work", "latest_state": "waiting"}]})));
    for session in ["quiet", "old-host-janitor", "work"] {
        roster.apply_notification_event(&obj(json!({"session": session})));
    }
    let pending = ["quiet".to_string(), "old-host-janitor".to_string()];
    assert_eq!(roster.attention_queue("a", &pending), ["work"]);
    assert!(roster.rows().iter().all(|row| row.unread == (row.session == "work")));
}

#[test]
fn a_helper_is_never_in_the_attention_queue() {
    let mut roster = Roster::new();
    roster.apply_snapshot(&obj(json!({"agents": [
        agent("p", "parent", 100, 100, "idle"),
        helper_row("worker", "p", "running", 200),
        agent("o", "other", 100, 100, "waiting")]})));
    roster.apply_notification_event(&obj(json!({"session": "worker"})));
    let pending = ["worker".to_string()];
    assert_eq!(roster.attention_queue("parent", &pending), ["other"], "Ctrl+J skips the helper");
    assert_eq!(roster.next_attention_session("other", &pending), None);
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

#[test]
fn job_progress_prefers_fraction_then_counts_then_percent() {
    use clarp_core::jobs::job_progress;
    let p = |metadata: Value| job_progress(&obj(json!({"metadata": metadata})));
    assert_eq!(p(json!({"progress": 0.3})), 0.3);
    assert_eq!(p(json!({"progress": 7})), 1.0);
    assert_eq!(p(json!({"completed": 3, "total": 4})), 0.75);
    assert_eq!(p(json!({"current": 1, "total": 4})), 0.25);
    assert_eq!(p(json!({"progress_percent": 40})), 0.4);
    assert_eq!(p(json!({"completed": 3})), -1.0);
    assert_eq!(job_progress(&obj(json!({}))), -1.0);
}

/// tst_native_core::quickSwitcherPutsTheExactNameFirst
#[test]
fn quick_switcher_puts_the_exact_name_first() {
    // Roster order is recency: Adam was active most recently.
    let rows = [("adam", "Adam"), ("kada", "Kada"), ("team-ada", "Team Ada"), ("ada", "Ada"), ("ada-helper", "Adaline")];
    let mut ranked = rows.to_vec();
    ranked.sort_by_key(|(session, name)| clarp_core::roster::switcher_rank(name, session, " Ada "));
    let names: Vec<&str> = ranked.iter().map(|(_, name)| *name).collect();
    assert_eq!(names.len(), 5);
    assert_eq!(names[0], "Ada");
    assert!(names.iter().position(|n| *n == "Team Ada") > names.iter().position(|n| *n == "Adaline"));
    assert_eq!(names.last(), Some(&"Kada"));
    assert_eq!(names, ["Ada", "Adam", "Adaline", "Team Ada", "Kada"]);
}

/// A snapshot that lists a session twice (a Host bug, or a race while it
/// adds an agent) keeps one row per session instead of aborting the app.
#[test]
fn a_duplicated_session_in_a_snapshot_is_one_row() {
    let mut roster = clarp_core::roster::Roster::default();
    let row = |session: &str, activity: i64| serde_json::json!({"session": session, "persona": session, "last_activity": activity});
    roster.apply_snapshot(serde_json::json!({"agents": [row("a", 3), row("b", 2)]}).as_object().unwrap());
    roster.apply_snapshot(serde_json::json!({"agents": [row("a", 3), row("b", 2), row("c", 1), row("c", 1), row("c", 1)]}).as_object().unwrap());
    assert_eq!(roster.sessions(), vec!["a".to_owned(), "b".to_owned(), "c".to_owned()]);
}

/// #354: the snapshot's `turn_started_at` reaches the row while a turn runs;
/// 0, null or absent (an older Host) is None, and a turn that starts or ends
/// between snapshots starts or stops the clock from the state event.
#[test]
fn turn_started_at_follows_the_running_turn() {
    use clarp_core::protocol::Agent;
    let parse = |extra: Value| Agent::from_json(&obj(roster_row("a", "a-id", 10, extra)));
    assert_eq!(parse(json!({"busy": true, "turn_started_at": 5_000})).turn_started_at, Some(5_000));
    assert_eq!(parse(json!({"busy": true, "turn_started_at": null})).turn_started_at, None);
    assert_eq!(parse(json!({"busy": false, "turn_started_at": 0})).turn_started_at, None);
    assert_eq!(parse(json!({"busy": true})).turn_started_at, None);

    let mut h = Harness::new();
    h.snapshot(json!([roster_row("now", "now-id", 10, json!({"turn_started_at": 0})),
                      roster_row("old", "old-id", 9, json!({}))]));
    assert_eq!(h.row("now").turn_started_at, None);
    h.with(|r| r.apply_state_event(&obj(json!({"session": "now", "kind": "thinking", "ts": 7_000}))));
    assert_eq!(h.row("now").turn_started_at, Some(7_000), "a turn begun between snapshots counts from its first busy state");
    h.with(|r| r.apply_state_event(&obj(json!({"session": "now", "kind": "tool", "ts": 9_000}))));
    assert_eq!(h.row("now").turn_started_at, Some(7_000), "a later state in the same turn keeps its start");
    h.with(|r| r.apply_state_event(&obj(json!({"session": "now", "kind": "idle", "ts": 12_000}))));
    assert_eq!(h.row("now").turn_started_at, None, "idle again: no clock");

    let mut old = Harness::new();
    old.snapshot(json!([roster_row("old", "old-id", 9, json!({}))]));
    old.with(|r| r.apply_state_event(&obj(json!({"session": "old", "kind": "thinking", "ts": 7_000}))));
    assert_eq!(old.row("old").turn_started_at, None, "a Host without turn_started_at keeps the last-message time");
}

#[test]
fn a_running_turn_reads_in_seconds_minutes_and_hours() {
    use clarp_core::time_format::turn_elapsed;
    assert_eq!(turn_elapsed(-3_000), "0s", "a start ahead of the local clock never reads negative");
    assert_eq!(turn_elapsed(12_400), "12s");
    assert_eq!(turn_elapsed(399_000), "6m 39s");
    assert_eq!(turn_elapsed(3_900_000), "1h 05m");
}

/// #249: a Host offering `stalled_turn` puts `stalled_since` and
/// `stalled_action` on the snapshot row and on `agent-activity` (start and
/// clear); null, absent or an older Host means no stall.
#[test]
fn a_stalled_turn_follows_the_host() {
    use clarp_core::protocol::Agent;
    use clarp_core::stall;
    let parse = |extra: Value| Agent::from_json(&obj(roster_row("a", "a-id", 10, extra)));
    let stalled = parse(json!({"busy": true, "stalled_since": 5_000, "stalled_action": "stop"}));
    assert_eq!((stalled.stalled_since, stalled.stalled_action.as_deref()), (Some(5_000), Some("stop")));
    assert_eq!(stall::stop_offered(&stalled), Some(5_000));
    let cleared = parse(json!({"busy": true, "stalled_since": null, "stalled_action": null}));
    assert_eq!((cleared.stalled_since, cleared.stalled_action), (None, None));
    assert_eq!(parse(json!({"busy": true})).stalled_since, None);
    let idle = parse(json!({"busy": false, "stalled_since": 5_000, "stalled_action": "stop"}));
    assert_eq!(stall::stop_offered(&idle), None, "an idle agent is not stuck");
    let other = parse(json!({"busy": true, "stalled_since": 5_000, "stalled_action": "wait"}));
    assert_eq!(stall::stop_offered(&other), None, "only a Stop the Host offers shows");

    let busy = json!({"latest_state": "tool", "busy": true});
    let mut h = Harness::new();
    h.with(|r| r.set_stall_feature(true));
    h.snapshot(json!([roster_row("sam", "sam-id", 10, busy.clone())]));
    assert_eq!(h.row("sam").stalled_since, None);
    h.with(|r| r.apply_activity_event(&obj(json!({"type": "agent-activity", "session": "sam", "stalled_since": 7_000, "stalled_action": "stop"}))));
    assert_eq!(h.row("sam").stalled_since, Some(7_000), "a stall starts from the event");
    h.with(|r| r.apply_activity_event(&obj(json!({"type": "agent-activity", "session": "sam", "status": "running"}))));
    assert_eq!(h.row("sam").stalled_since, Some(7_000), "an event without the fields leaves it");
    h.with(|r| r.apply_activity_event(&obj(json!({"type": "agent-activity", "session": "sam", "stalled_since": null, "stalled_action": null}))));
    assert_eq!(h.row("sam").stalled_since, None, "and clears from the event");
    h.snapshot(json!([roster_row("sam", "sam-id", 10, json!({"latest_state": "tool", "busy": true, "stalled_since": 8_000, "stalled_action": "stop"}))]));
    assert_eq!(h.row("sam").stalled_since, Some(8_000), "a snapshot carries it too");
    h.with(|r| r.apply_state_event(&obj(json!({"session": "sam", "kind": "idle", "ts": 9_000}))));
    assert_eq!(h.row("sam").stalled_since, None, "the turn ending ends it");

    // A Host without the feature shows nothing new, whatever it sends.
    let mut old = Harness::new();
    old.snapshot(json!([roster_row("sam", "sam-id", 10, json!({"latest_state": "tool", "busy": true, "stalled_since": 8_000, "stalled_action": "stop"}))]));
    assert_eq!(old.row("sam").stalled_since, None);
    old.with(|r| r.apply_activity_event(&obj(json!({"session": "sam", "stalled_since": 9_000, "stalled_action": "stop"}))));
    assert_eq!(old.row("sam").stalled_since, None);
}

#[test]
fn a_stalled_turn_reads_in_whole_minutes_with_waiting_words() {
    use clarp_core::stall::{ROW_STATUS, indicator, offered_by};
    assert_eq!(indicator(0, 12 * 60_000 + 59_000, false), "Seems stuck · no progress for 12 min");
    assert_eq!(indicator(0, 3 * 60_000, true), "Seems stuck · no progress for 3 min · Your message is waiting");
    assert_eq!(indicator(10_000, 0, false), "Seems stuck · no progress for 0 min", "a stall ahead of the local clock never reads negative");
    assert_eq!(ROW_STATUS, "Seems stuck");
    assert!(offered_by(&obj(json!({"capabilities": {"features": ["live_items", "stalled_turn"]}}))));
    assert!(!offered_by(&obj(json!({"capabilities": {"features": ["live_items"]}}))));
    assert!(!offered_by(&obj(json!({}))));
}

/// #702: the Running box says "Working with <Name>" for a turn another
/// agent started, from the snapshot's `working_audience` and
/// `working_with` (Host contract 124) and from each working `agent-state`.
#[test]
fn whom_the_agent_works_with_follows_the_snapshot_and_its_states() {
    use clarp_core::protocol::Agent;
    let parse = |extra: Value| Agent::from_json(&obj(roster_row("a", "a-id", 10, extra)));
    let with = parse(json!({"busy": true, "working_audience": "Agents", "working_with": {"agent_id": "b-id", "name": "Bella"}}));
    assert_eq!((with.working_audience.as_str(), with.working_with.as_str()), ("agents", "Bella"));
    let older = parse(json!({"busy": true}));
    assert_eq!((older.working_audience.as_str(), older.working_with.as_str()), ("", ""), "an older Host says neither");

    let mut h = Harness::new();
    h.snapshot(json!([roster_row("sam", "sam-id", 10, json!({"latest_state": "idle", "busy": false}))]));
    h.with(|r| r.apply_state_event(&obj(json!({"session": "sam", "kind": "thinking", "ts": 1_000, "audience": "agents", "sender_name": "Bella"}))));
    assert_eq!((h.roster.find("sam").unwrap().working_audience.as_str(), h.roster.find("sam").unwrap().working_with.as_str()), ("agents", "Bella"));
    h.with(|r| r.apply_state_event(&obj(json!({"session": "sam", "kind": "tool", "ts": 2_000}))));
    assert_eq!(h.roster.find("sam").unwrap().working_with, "Bella", "a state without the fields leaves them");
    h.with(|r| r.apply_state_event(&obj(json!({"session": "sam", "kind": "tool", "ts": 3_000, "audience": "user"}))));
    assert_eq!((h.roster.find("sam").unwrap().working_audience.as_str(), h.roster.find("sam").unwrap().working_with.as_str()), ("user", ""), "the user stepped in");
    h.with(|r| r.apply_state_event(&obj(json!({"session": "sam", "kind": "idle", "ts": 4_000}))));
    assert_eq!(h.roster.find("sam").unwrap().working_audience, "", "the turn ending ends it");
}

/// #841: an `agent-roster` event's `fields` (feature `roster_fields`) patch
/// the one row in place; every field the event does not name keeps its
/// value, and the row equals what a fresh snapshot would make of it.
#[test]
fn a_roster_events_fields_patch_one_row_as_a_snapshot_would() {
    let parent = roster_row("parent", "p", 300, json!({"latest_state": "background", "busy": true, "status_text": "1 helper working",
        "child_count": 1, "running_children": 1, "background_jobs": {"count": 2, "sub_agents": 1}, "model": "opus", "muted": false}));
    let worker = helper_row("worker", "p", "running", 200);
    let mut h = Harness::new();
    h.snapshot(json!([parent.clone(), worker.clone()]));
    h.clear();

    let helper_fields = obj(json!({"helper_state": "reported", "archived_at": null, "a_field_not_read_here": 1}));
    let maker_fields = obj(json!({"latest_state": "idle", "status_text": null, "child_count": 1, "running_children": 0,
                                   "background_jobs": {"count": 2, "sub_agents": 0}}));
    h.with(|r| assert_eq!(r.patch_row("worker", &helper_fields), RowChange::Applied));
    h.with(|r| assert_eq!(r.patch_row("parent", &maker_fields), RowChange::Applied));
    assert_eq!(h.count(|op| matches!(op, ListOp::Insert { .. } | ListOp::Remove { .. } | ListOp::Move { .. })), 0, "rows change in place");
    assert_eq!(h.row("worker").helper_state, "reported");
    let p = h.row("parent").clone();
    assert_eq!((p.state.as_str(), p.status_text.as_str(), p.busy), ("idle", "", false));
    assert_eq!(p.running_children, 0, "the maker counts no working helper");
    assert_eq!(h.roster.find("parent").unwrap().model, "opus", "a field the event does not name keeps its value");

    // The same change read as a whole list makes the same rows.
    let mut fresh = Harness::new();
    let mut parent_after = parent;
    let mut worker_after = worker;
    for (k, v) in maker_fields { parent_after[k] = v; }
    parent_after["busy"] = json!(false);
    worker_after["helper_state"] = json!("reported");
    fresh.snapshot(json!([parent_after, worker_after]));
    assert_eq!(fresh.row("parent"), h.row("parent"));
    assert_eq!(fresh.row("worker"), h.row("worker"));
}

#[test]
fn a_patch_for_a_row_not_held_or_put_away_asks_for_a_read() {
    let mut h = Harness::new();
    h.snapshot(json!([roster_row("sam", "s", 10, json!({}))]));
    h.clear();
    h.with(|r| assert_eq!(r.patch_row("nobody", &obj(json!({"muted": true}))), RowChange::NotHeld));
    h.with(|r| assert_eq!(r.patch_row("sam", &obj(json!({"helper_state": "done", "archived_at": 1_700_000_000_000i64}))), RowChange::Moved));
    assert!(h.ops.is_empty(), "nothing changed: the list is read");
    assert_eq!(h.roster.find("sam").unwrap().helper_state, "", "the row is left as it was");

    let mut put_away = Roster::archived();
    put_away.apply_snapshot(&obj(json!({"agents": [roster_row("old", "o", 10, json!({"archived_at": 5}))]})));
    assert_eq!(put_away.patch_row("old", &obj(json!({"muted": true}))), RowChange::Applied, "a row put away is patched where it is");
    assert_eq!(put_away.patch_row("old", &obj(json!({"archived_at": null}))), RowChange::Moved, "taken back, it belongs to the list");
}

/// #841: `GET /agents/{session}` (feature `agent_row`) replaces one row in
/// its place, keeping what the list holds newer, as a snapshot does.
#[test]
fn a_read_row_replaces_its_row_in_place_and_keeps_what_is_newer() {
    let mut h = Harness::new();
    h.snapshot(json!([roster_row("a", "a1", 30, json!({})), agent("b1", "b", 20, 100, "idle")]));
    h.with(|r| r.apply_state_event(&obj(json!({"session": "b", "kind": "thinking", "ts": 500, "status_text": "on it"}))));
    h.with(|r| r.apply_notification_event(&obj(json!({"session": "b"}))));
    h.clear();
    let row = obj(json!({"agent_id": "b1", "session": "b", "persona": "Bea", "avatar_symbol": "star",
                         "last_activity": 20, "latest_state_ts": 100, "latest_state": "idle"}));
    h.with(|r| assert_eq!(r.replace_row(&row), RowChange::Applied));
    assert_eq!(h.count(|op| matches!(op, ListOp::Insert { .. } | ListOp::Remove { .. } | ListOp::Move { .. })), 0);
    let b = h.row("b").clone();
    assert_eq!((b.name.as_str(), b.avatar_symbol.as_str()), ("Bea", "star"), "the row's own fields are the Host's");
    assert_eq!((b.state.as_str(), b.status_text.as_str()), ("thinking", "on it"), "a later state event is kept");
    assert!(b.unread, "the unread mark is this desktop's");
    assert_eq!(h.first(), "a", "the row keeps its place");
    h.with(|r| assert_eq!(r.replace_row(&obj(json!({"agent_id": "z", "session": "z"}))), RowChange::NotHeld));
}

#[test]
fn the_hosts_own_queue_answer_wins_over_a_count_kept_from_a_newer_revision() {
    let mut roster = clarp_core::roster::Roster::default();
    roster.apply_snapshot(&serde_json::json!({"agents": [{"session": "hazel", "name": "Hazel", "queued_turn_count": 27, "queue_revision": 900}]}).as_object().unwrap().clone());
    assert_eq!(roster.find("hazel").unwrap().queued_turn_count, 27);
    // The Host moved the queue out; its revision went back. The /turn-queue
    // answer says nothing waits: the count follows it.
    roster.apply_queue_truth("hazel", 0, false, 3);
    let hazel = roster.find("hazel").unwrap();
    assert_eq!((hazel.queued_turn_count, hazel.queue_revision), (0, 3));
}
