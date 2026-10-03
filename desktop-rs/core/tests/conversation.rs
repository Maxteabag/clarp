//! Ports of the ConversationModel cases in the C++ client's `tst_native_core.cpp`.
//! Every scenario also replays the recorded ops onto a mirror and checks it
//! equals the model, which is the contract the Qt adapter relies on.

use chrono::{FixedOffset, TimeZone};
use clarp_core::conversation::*;
use clarp_core::protocol::Message;
use clarp_core::time_format::*;
use serde_json::{Value, json};

struct Harness {
    model: Conversation,
    mirror: Vec<Message>,
    ops: Vec<Op>,
}

impl Harness {
    fn new(session: &str) -> Self {
        let mut h = Self { model: Conversation::new(), mirror: Vec::new(), ops: Vec::new() };
        h.model.open_session(session);
        h.sync();
        h.ops.clear();
        h
    }
    fn sync(&mut self) {
        let ops = self.model.take_ops();
        replay(&mut self.mirror, &ops);
        assert_eq!(self.mirror, self.model.rows(), "mirror diverged after {ops:?}");
        self.ops.extend(ops);
    }
    fn log(&mut self, response: Value, kind: LoadKind) {
        self.model.apply_log(response.as_object().expect("object"), kind);
        self.sync();
    }
    fn activity(&mut self, event: Value) {
        self.model.apply_activity_event(event.as_object().expect("object"));
        self.sync();
    }
    fn optimistic(&mut self, id: &str, text: &str) {
        self.model.add_optimistic(id, text);
        self.sync();
    }
    fn ids(&self) -> Vec<&str> {
        self.model.rows().iter().map(|m| m.id.as_str()).collect()
    }
    fn bodies(&self) -> Vec<&str> {
        self.model.rows().iter().map(|m| m.display_text.as_str()).collect()
    }
    fn clear(&mut self) {
        self.ops.clear();
    }
    fn count(&self, predicate: impl Fn(&Op) -> bool) -> usize {
        self.ops.iter().filter(|op| predicate(op)).count()
    }
    fn signals(&self, wanted: &Signal) -> usize {
        self.count(|op| matches!(op, Op::Signal(s) if s == wanted))
    }
    fn data_changes(&self) -> Vec<&Vec<Role>> {
        self.ops
            .iter()
            .filter_map(|op| match op {
                Op::Update { roles, .. } if !roles.is_empty() => Some(roles),
                _ => None,
            })
            .collect()
    }
}

fn fixture_logs(path: &str) -> Vec<(Value, LoadKind)> {
    let full = format!("{}/../../contract/fixtures/{path}", env!("CARGO_MANIFEST_DIR"));
    let fixture: Value = serde_json::from_str(&std::fs::read_to_string(full).unwrap()).unwrap();
    fixture["steps"]
        .as_array()
        .unwrap()
        .iter()
        .filter_map(|s| s.get("log"))
        .map(|log| {
            let kind = match log["mode"].as_str() {
                Some("tail") => LoadKind::Tail,
                Some("older") => LoadKind::Older,
                _ => LoadKind::Delta,
            };
            (log["response"].clone(), kind)
        })
        .collect()
}

fn turn(id: &str, revision: i64) -> Value {
    json!({"id": id, "role": "assistant", "text": id, "revision": revision})
}

#[test]
fn tail_then_delta_matches_golden_fixture() {
    let logs = fixture_logs("sync/tail-then-delta.json");
    assert_eq!(logs.len(), 2);
    let mut h = Harness::new("rachel");
    h.log(logs[0].0.clone(), LoadKind::Tail);
    h.log(logs[1].0.clone(), LoadKind::Delta);
    assert_eq!(h.model.latest_revision(), 3);
    assert_eq!(h.ids(), ["u-a", "m1", "m2"]);
}

#[test]
fn streaming_rows_update_in_place_and_retire_when_finalized() {
    let mut h = Harness::new("streaming");
    h.log(json!({"conversation_id": "conversation-1", "latest_revision": 1,
        "turns": [{"id": "live-1", "role": "assistant", "kind": "live", "text": "Hello", "revision": 1}]}), LoadKind::Tail);
    h.clear();
    h.log(json!({"conversation_id": "conversation-1", "latest_revision": 2,
        "turns": [{"id": "live-1", "role": "assistant", "kind": "live", "text": "Hello world <spe", "revision": 2}]}), LoadKind::Delta);
    assert_eq!(h.model.len(), 1);
    assert_eq!(h.bodies(), ["Hello world"]);
    let changes = h.data_changes();
    assert_eq!(changes.len(), 1);
    assert!(changes[0].contains(&Role::Body) && changes[0].contains(&Role::Revision));
    assert!(!changes[0].contains(&Role::Tools) && !changes[0].contains(&Role::DisplayCells));
    assert_eq!(h.count(|op| matches!(op, Op::Reset(_) | Op::Insert { .. } | Op::Remove { .. })), 0);
    assert_eq!(h.signals(&Signal::CountChanged), 0);

    h.log(json!({"conversation_id": "conversation-1", "latest_revision": 3,
        "turns": [{"id": "final-1", "role": "assistant", "kind": "assistant", "text": "Hello world", "revision": 3}]}), LoadKind::Delta);
    assert_eq!(h.ids(), ["final-1"]);

    let mut repeated = Harness::new("repeated");
    repeated.log(json!({"conversation_id": "conversation-2", "latest_revision": 1,
        "turns": [{"id": "old-final", "role": "assistant", "text": "Same opening", "revision": 1}]}), LoadKind::Tail);
    repeated.log(json!({"conversation_id": "conversation-2", "latest_revision": 2,
        "turns": [{"id": "new-live", "role": "assistant", "kind": "live", "text": "Same", "revision": 2}]}), LoadKind::Delta);
    assert_eq!(repeated.ids(), ["old-final", "new-live"]);
}

#[test]
fn activity_rows_update_in_place_by_semantic_identity() {
    let mut h = Harness::new("activity");
    let event = |status: &str, summary: &str| json!({"activity_status": status, "activity_action": "run",
        "activity_tool": "Bash", "activity_file_path": "/tmp", "activity_summary": summary});
    h.activity(event("running", "first"));
    assert_eq!(h.model.len(), 1);
    let id = h.model.rows()[0].id.clone();
    h.clear();
    h.activity(event("running", "second"));
    h.activity(event("ok", "complete"));
    h.activity(event("ok", "duplicate"));
    assert_eq!(h.ids(), [id.as_str()]);
    assert_eq!(h.bodies(), ["complete"]);
    assert_eq!(h.model.rows()[0].activity_status, "ok");
    assert_eq!(h.data_changes().len(), 2);
    assert_eq!(h.count(|op| matches!(op, Op::Insert { .. } | Op::Remove { .. })), 0);

    let mut details = Harness::new("details");
    details.log(json!({"conversation_id": "conversation", "latest_revision": 1, "turns": [{"id": "message-1",
        "role": "assistant", "text": "Done", "tool_details_available": true, "activity_count": 1, "revision": 1}]}), LoadKind::Tail);
    assert!(details.model.apply_tool_details("message-1",
        json!({"tools": [{"name": "Read", "summary": "Loaded file"}], "display_cells": []}).as_object().unwrap()));
    details.sync();
    assert_eq!(details.model.rows()[0].tools.len(), 1);
    assert!(!details.model.rows()[0].tool_details_available);
}

#[test]
fn activity_rows_are_capped_at_eighty() {
    let mut h = Harness::new("cap");
    for i in 0..85 {
        h.activity(json!({"activity_status": "ok", "activity_action": format!("a{i}"), "activity_summary": "s"}));
    }
    assert_eq!(h.model.len(), 80);
    assert_eq!(h.model.rows()[0].tool_name, "a5");
}

#[test]
fn older_history_prepends_without_reordering_the_tail() {
    let mut h = Harness::new("history");
    h.log(json!({"conversation_id": "conversation-1", "turns": [turn("a", 2), turn("b", 3)]}), LoadKind::Tail);
    h.clear();
    h.log(json!({"conversation_id": "conversation-1", "turns": [turn("old", 1), turn("a", 2)]}), LoadKind::Older);
    assert_eq!(h.ids(), ["old", "a", "b"]);
    assert_eq!(h.signals(&Signal::RowsPrepended), 1);
}

#[test]
fn growing_reply_rejects_stale_revision() {
    let logs = fixture_logs("sync/growing-assistant-reply.json");
    assert_eq!(logs.len(), 3);
    let mut h = Harness::new("rachel");
    h.log(logs[0].0.clone(), LoadKind::Tail);
    h.log(logs[1].0.clone(), LoadKind::Delta);
    h.log(logs[2].0.clone(), LoadKind::Delta);
    assert_eq!(h.bodies(), ["go", "Hello"]);
}

#[test]
fn optimistic_delivery_stays_visible_until_confirmed() {
    let logs = fixture_logs("delivery/optimistic-bubble-until-filed.json");
    assert_eq!(logs.len(), 2);
    let mut h = Harness::new("rachel");
    h.log(logs[0].0.clone(), LoadKind::Tail);
    h.optimistic("a", "question");
    h.optimistic("b", "second");
    h.clear();
    h.log(logs[1].0.clone(), LoadKind::Delta);
    assert_eq!(h.ids(), ["m0", "u-a", "u-b"]);
    let confirmed: Vec<&Op> =
        h.ops.iter().filter(|op| matches!(op, Op::Signal(Signal::DeliveryConfirmed(_)))).collect();
    assert_eq!(confirmed, [&Op::Signal(Signal::DeliveryConfirmed("a".into()))]);
    assert!(h.model.rows()[2].pending);

    let mut cached = Harness::new("cached");
    cached.log(json!({"conversation_id": "conversation", "latest_revision": 1,
        "turns": [{"id": "one", "role": "assistant", "text": "One", "revision": 1}]}), LoadKind::Tail);
    cached.optimistic("pending", "Newest");
    cached.log(json!({"conversation_id": "conversation", "latest_revision": 2, "turns": [
        {"id": "one", "role": "assistant", "text": "One", "revision": 1},
        {"id": "two", "role": "assistant", "text": "Two", "revision": 2}]}), LoadKind::Tail);
    assert_eq!(cached.ids(), ["one", "two", "u-pending"]);

    let mut retry = Harness::new("retry");
    retry.optimistic("failed", "Try this again");
    retry.model.mark_delivery_failed("failed");
    retry.sync();
    assert_eq!(retry.model.take_failed_message_for_retry("u-failed").as_deref(), Some("Try this again"));
    retry.sync();
    assert!(retry.model.is_empty());
    assert_eq!(retry.model.take_failed_message_for_retry("u-failed"), None);
}

#[test]
fn optimistic_rows_survive_an_authoritative_replacement() {
    let mut h = Harness::new("replace");
    h.log(json!({"conversation_id": "c1", "turns": [turn("m1", 1)]}), LoadKind::Tail);
    h.optimistic("x", "hello");
    h.clear();
    h.log(json!({"conversation_id": "c2", "turns": [turn("n1", 1)]}), LoadKind::Tail);
    assert_eq!(h.ids(), ["n1", "u-x"]);
    assert_eq!(h.count(|op| matches!(op, Op::Reset(_))), 1);
}

#[test]
fn conversation_change_requests_replacement() {
    let mut h = Harness::new("rachel");
    h.log(json!({"conversation_id": "one", "turns": [], "latest_revision": 0}), LoadKind::Tail);
    h.clear();
    h.log(json!({"conversation_id": "two", "turns": [], "latest_revision": 1}), LoadKind::Delta);
    assert_eq!(h.signals(&Signal::ReplacementRequired), 1);
    h.clear();
    h.log(json!({"conversation_id": "one", "replace_required": true}), LoadKind::Delta);
    assert_eq!(h.signals(&Signal::ReplacementRequired), 1);
    assert_eq!(h.signals(&Signal::BatchStarted), 0);

    let mut authoritative = Harness::new("same-id");
    authoritative.log(json!({"conversation_id": "conversation", "latest_revision": 2, "turns": [
        {"id": "keep", "role": "assistant", "text": "Keep", "revision": 1},
        {"id": "remove", "role": "assistant", "text": "Remove", "revision": 2}]}), LoadKind::Tail);
    authoritative.log(json!({"conversation_id": "conversation", "latest_revision": 1, "turns": [
        {"id": "keep", "role": "assistant", "text": "Keep", "revision": 1}]}), LoadKind::Replace);
    assert_eq!(authoritative.ids(), ["keep"]);
    assert_eq!(authoritative.model.latest_revision(), 1);
}

#[test]
fn spawned_lifecycle_never_becomes_transcript_tool() {
    let mut h = Harness::new("");
    h.activity(json!({"type": "agent-activity", "kind": "spawned", "phase": "spawned", "status": "ok",
        "action": "started", "summary": "Started"}));
    assert_eq!(h.model.len(), 0);
    h.activity(json!({"activity_kind": "spawned", "activity_phase": "spawned", "activity_status": "ok",
        "activity_action": "started", "activity_summary": "Started"}));
    assert_eq!(h.model.len(), 0);
    h.optimistic("real-user", "Start the task session.");
    assert_eq!(h.model.len(), 1);
    h.activity(json!({"kind": "tool", "phase": "tool", "status": "running", "tool": "Bash",
        "action": "started", "summary": "Run tests"}));
    assert_eq!(h.model.len(), 2);
}

#[test]
fn new_durable_rows_clear_running_activity() {
    let mut h = Harness::new("s");
    h.log(json!({"conversation_id": "c", "turns": []}), LoadKind::Tail);
    h.model.show_transient_thinking("Rachel");
    h.sync();
    assert_eq!(h.bodies(), ["Rachel is working"]);
    h.log(json!({"conversation_id": "c", "turns": [turn("m1", 1)]}), LoadKind::Delta);
    assert_eq!(h.ids(), ["m1"]);
}

#[test]
fn cache_snapshot_keeps_only_durable_rows_and_restores() {
    let mut h = Harness::new("s");
    h.log(json!({"conversation_id": "c", "latest_revision": 4, "has_more": true, "turns": [
        turn("m1", 1), {"id": "live", "role": "assistant", "kind": "live", "text": "par", "revision": 4}]}), LoadKind::Tail);
    h.optimistic("p", "pending");
    h.model.show_transient_thinking("");
    h.sync();
    let snapshot = h.model.cache_snapshot();
    let ids: Vec<&str> = snapshot["turns"].as_array().unwrap().iter().map(|t| t["id"].as_str().unwrap()).collect();
    assert_eq!(ids, ["m1"]);
    assert_eq!(snapshot["latest_revision"], 4);
    let mut restored = Harness::new("s");
    assert!(restored.model.restore_cache_snapshot(&snapshot));
    restored.sync();
    assert_eq!(restored.ids(), ["m1"]);
    assert!(restored.model.has_more());
    assert!(!restored.model.restore_cache_snapshot(&Default::default()));
}

// The model half of tst_native_core::subagentCellsDescribePhaseNameAndTask.
#[test]
fn subagent_cells_are_annotated_on_the_way_out_only() {
    let mut h = Harness::new("s");
    h.log(json!({"conversation_id": "c", "latest_revision": 1, "turns": [{"id": "m1", "role": "assistant",
        "text": "", "revision": 1, "display_cells": [{"kind": "subagents", "title": "Spawned agent",
        "status": "recorded", "summary": "Kepler", "lines": [{"label": "Task", "text": "Audit the parser"}]}]}]}), LoadKind::Tail);
    let cells = presented_display_cells(&h.model.rows()[0]);
    assert_eq!(cells[0]["_subagent"]["phase"], "spawned");
    assert!(!serde_json::to_string(&h.model.cache_snapshot()).unwrap().contains("_subagent"));
}

#[test]
fn compact_durations_read_like_the_cpp_client() {
    assert_eq!(compact_duration(-5), "0s");
    assert_eq!(compact_duration(59_000), "59s");
    assert_eq!(compact_duration(125_000), "2m");
    assert_eq!(compact_duration(3_900_000), "1h 05m");
    assert_eq!(compact_duration(3 * 86_400_000), "3d");
}

#[test]
fn stamps_and_day_headings_follow_the_calendar() {
    let zone = FixedOffset::east_opt(2 * 3600).unwrap();
    let now = zone.with_ymd_and_hms(2026, 9, 17, 18, 10, 0).unwrap();
    let ms = |y, m, d, h, min| zone.with_ymd_and_hms(y, m, d, h, min, 0).unwrap().timestamp_millis();
    assert_eq!(chat_stamp(ms(2026, 9, 17, 8, 5), &now), "8:05 AM");
    assert_eq!(chat_stamp(ms(2026, 9, 16, 23, 0), &now), "Yesterday");
    assert_eq!(chat_stamp(ms(2026, 9, 13, 12, 0), &now), "Sunday");
    assert_eq!(chat_stamp(ms(2026, 6, 12, 12, 0), &now), "12 Jun");
    assert_eq!(chat_stamp(ms(2025, 6, 12, 12, 0), &now), "6/12/25");
    assert_eq!(chat_stamp(0, &now), "");
    assert_eq!(day_separator("2026-09-17T06:00:00Z", "", &now), "Today");
    assert_eq!(day_separator("2026-09-17T06:00:00Z", "2026-09-17T05:00:00.123Z", &now), "");
    assert_eq!(day_separator("2026-09-16T12:00:00Z", "", &now), "Yesterday");
    assert_eq!(day_separator("2026-06-12T12:00:00Z", "", &now), "12 June");
    assert_eq!(day_separator("not a time", "", &now), "");
    assert_eq!(clock_time("2026-09-17T16:10:00Z", &zone), "6:10 PM");
    assert_eq!(clock_time("", &zone), "");
}

/// tst_native_core::agentReplyKeepsItsAuthorAndNamesTheAnsweredAgent
#[test]
fn agent_reply_keeps_its_author_and_names_the_answered_agent() {
    let mut h = Harness::new("hugo");
    // The incoming prompt names its author; the answering row names the
    // agent it answers instead of claiming a sender.
    let prompt = json!({"id": "u-1", "role": "user", "text": "Status: survey done", "revision": 1, "origin": "agent",
        "sender_agent_id": "agent-cpp", "sender_name": "C++ Junior", "sender_session": "cjunior-0940",
        "reply_to_agent_id": "", "delivery": "sent"});
    let reply = json!({"id": "m-1", "role": "assistant", "text": "Good, that matches the agreed scope.", "revision": 2,
        "origin": "agent", "sender_agent_id": "", "sender_name": "", "reply_to_agent_id": "agent-cpp",
        "reply_to_name": "C++ Junior", "reply_to_session": "cjunior-0940", "delivery": "private"});
    h.log(json!({"turns": [prompt, reply.clone()]}), LoadKind::Tail);
    let rows = h.model.rows();
    assert_eq!(rows.len(), 2);
    assert_eq!((rows[0].sender_name.as_str(), rows[0].reply_to_name.as_str(), rows[0].delivery.as_str()), ("C++ Junior", "", "sent"));
    let answer = &rows[1];
    assert_eq!((answer.sender_name.as_str(), answer.sender_agent_id.as_str()), ("", ""));
    assert_eq!(
        (answer.reply_to_agent_id.as_str(), answer.reply_to_name.as_str(), answer.reply_to_session.as_str(), answer.delivery.as_str()),
        ("agent-cpp", "C++ Junior", "cjunior-0940", "private")
    );

    // A delta that only changes the marker still notifies the reply roles.
    h.ops.clear();
    let mut renamed = reply;
    renamed["reply_to_name"] = json!("C++ Renamed");
    renamed["revision"] = json!(3);
    h.log(json!({"turns": [renamed]}), LoadKind::Delta);
    assert_eq!(h.model.rows()[1].reply_to_name, "C++ Renamed");
    assert!(h.data_changes().iter().any(|roles| roles.contains(&Role::ReplyToName)), "{:?}", h.data_changes());

    let mut restored = Conversation::new();
    assert!(restored.restore_cache_snapshot(&h.model.cache_snapshot()));
    assert_eq!((restored.rows()[1].reply_to_agent_id.as_str(), restored.rows()[1].delivery.as_str()), ("agent-cpp", "private"));
}

/// A delta that arrives right after a long chat opens must not hide its
/// older history: on a delta, the Host's `has_more` is about a backlog of
/// newer rows, not older ones.
#[test]
fn a_delta_never_hides_older_history() {
    let mut h = Harness::new("mike");
    let rows: Vec<Value> = (150..250).map(|i| json!({"id": format!("m-{i}"), "role": "user", "text": "x", "revision": i})).collect();
    h.log(json!({"conversation_id": "c", "turns": rows, "latest_revision": 250, "has_more": true}), LoadKind::Tail);
    assert!(h.model.has_more());
    h.log(json!({"conversation_id": "c", "turns": [], "latest_revision": 250, "has_more": false}), LoadKind::Delta);
    assert!(h.model.has_more(), "a delta says nothing about older rows");
    let older: Vec<Value> = (0..150).map(|i| json!({"id": format!("m-{i}"), "role": "user", "text": "x", "revision": i})).collect();
    h.log(json!({"conversation_id": "c", "turns": older, "latest_revision": 250, "has_more": false}), LoadKind::Older);
    assert!(!h.model.has_more(), "the last older page ends it");
}

#[test]
fn rows_are_found_by_id_after_inserts_and_removals_in_the_middle() {
    let mut h = Harness::new("rachel");
    let indexed = |h: &Harness| {
        for (row, message) in h.model.rows().iter().enumerate() {
            assert_eq!(h.model.index_of(&message.id), Some(row), "{} is at {row}: {:?}", message.id, h.ids());
        }
    };
    h.log(json!({"conversation_id": "c1", "latest_revision": 3, "turns": [
        {"id": "a", "role": "user", "text": "one", "revision": 1},
        {"id": "b", "role": "assistant", "text": "two", "revision": 3},
    ]}), LoadKind::Tail);
    h.optimistic("p1", "pending one");
    h.optimistic("p2", "pending two");
    // A reply lands before the unconfirmed sends: an insert in the middle.
    h.log(json!({"conversation_id": "c1", "latest_revision": 4, "turns": [
        {"id": "c", "role": "assistant", "text": "three", "revision": 4},
    ]}), LoadKind::Delta);
    assert_eq!(h.ids(), ["a", "b", "c", "u-p1", "u-p2"]);
    indexed(&h);
    // The first send fails and is taken back: a removal in the middle.
    h.model.mark_delivery_failed("p1");
    h.sync();
    assert!(h.model.take_failed_message_for_retry("u-p1").is_some());
    h.sync();
    assert_eq!(h.ids(), ["a", "b", "c", "u-p2"]);
    indexed(&h);
    assert_eq!(h.model.index_of("u-p1"), None);
    h.log(json!({"conversation_id": "c1", "latest_revision": 2, "turns": [
        {"id": "z", "role": "user", "text": "older", "revision": 0},
    ]}), LoadKind::Older);
    assert_eq!(h.ids(), ["z", "a", "b", "c", "u-p2"]);
    indexed(&h);
}
