//! Ports of the ConversationPresentationModel cases in
//! the C++ client's `tst_native_core.cpp`. Rows the C++ tests built with
//! QStandardItemModel are built here as Messages directly.

use std::cell::Cell;

use clarp_core::conversation::{Conversation, LoadKind};
use clarp_core::presentation::*;
use clarp_core::protocol::Message;
use serde_json::{Value, json};

fn tool_row(id: &str, summary: &str, body: &str) -> Message {
    Message {
        id: id.into(),
        role: "assistant".into(),
        display_text: body.into(),
        text: body.into(),
        tools: vec![json!({"name": id, "summary": summary})],
        ..Message::default()
    }
}

fn summary_lookup(value: &serde_json::Map<String, Value>) -> String {
    value.get("summary").and_then(Value::as_str).unwrap_or_default().to_owned()
}

fn repeat(p: &Presentation, row: usize) -> i64 {
    p.rows[row].tools[0].get("_explanationRepeat").and_then(Value::as_i64).unwrap_or(1)
}

#[test]
fn consecutive_explanations_collapse_without_changing_transcript() {
    let mut settings = Settings::default();
    let mut rows = vec![tool_row("a", "Check files", ""), tool_row("b", "Check files", "")];
    let lookup: Lookup = &summary_lookup;
    let show = |rows: &[Message], s: &mut Settings, l: Option<Lookup>| present(rows, s, l);
    let p = show(&rows, &mut settings, Some(lookup));
    assert_eq!(p.rows.len(), 1);
    assert_eq!(repeat(&p, 0), 2);
    rows.push(tool_row("c", "Check files", ""));
    let p = show(&rows, &mut settings, Some(lookup));
    assert_eq!((p.rows.len(), repeat(&p, 0)), (1, 3));
    assert_eq!(rows[1].tools.len(), 1, "source rows are never rewritten");
    rows.push(tool_row("d", "", ""));
    rows.push(tool_row("e", "Check files", ""));
    assert_eq!(show(&rows, &mut settings, Some(lookup)).rows.len(), 3, "pending explanations break the run");
    rows[3].tools = vec![json!({"summary": "Check files"})];
    let p = show(&rows, &mut settings, Some(lookup));
    assert_eq!((p.rows.len(), repeat(&p, 0)), (1, 5), "late completion extends the first row");
    let original = rows[2].tools.clone();
    rows[2].tools = vec![json!({"summary": "Different"})];
    let p = show(&rows, &mut settings, Some(lookup));
    assert_eq!(p.rows.len(), 3);
    assert_eq!((repeat(&p, 0), repeat(&p, 2)), (2, 2));
    rows[2].tools = vec![json!({"summary": "Check files", "status": "error"})];
    assert_eq!(show(&rows, &mut settings, Some(lookup)).rows.len(), 3, "equal words must not hide a different status");
    rows[2].tools = original;
    assert_eq!(repeat(&show(&rows, &mut settings, Some(lookup)), 0), 5);
    rows.push(tool_row("f", "Check files", "A message boundary"));
    let p = show(&rows, &mut settings, Some(lookup));
    assert_eq!(p.rows.len(), 2);
    assert_eq!(repeat(&p, 1), 1);
    assert_eq!(show(&rows, &mut settings, None).rows.len(), 6, "developer mode restores every original row");
    rows.remove(1);
    assert_eq!(repeat(&show(&rows, &mut settings, Some(lookup)), 0), 4);
    settings.activity_mode = GROUPED;
    let p = show(&rows, &mut settings, Some(lookup));
    assert_eq!(p.rows.len(), 2);
    assert!(p.rows[0].tools.is_empty());
    settings.toggle_group("a");
    let p = show(&rows, &mut settings, Some(lookup));
    let tools = &p.rows[0].tools;
    assert_eq!(tools.len(), 4, "details retained; delegates suppress repeats");
    assert_eq!(tools[0]["_explanationRepeat"], 4);
    assert_eq!(tools[3]["_explanationRepeat"], 0);
}

fn old_row(id: &str, time: &str, kind: &str) -> Message {
    Message {
        id: id.into(),
        role: "assistant".into(),
        timestamp: time.into(),
        kind: kind.into(),
        // The Host stamps ordinary rows "user"; grouping must not read that
        // as a foreign dispatcher and split every activity into its own group.
        origin: "user".into(),
        activity_count: 1,
        tools: vec![json!({"name": "Read"})],
        ..Message::default()
    }
}

#[test]
fn old_activity_groups_are_lazy_and_visit_scoped() {
    let mut rows = vec![old_row("a", "2020-01-01T00:00:00Z", ""), old_row("b", "2020-01-01T01:32:23Z", "")];
    let mut settings = Settings { activity_mode: GROUP_OLD, ..Settings::default() };
    let p = present(&rows, &mut settings, None);
    assert_eq!(p.rows.len(), 1);
    assert_eq!(p.rows[0].group_label, "2 tool calls · 1h 32m 23s elapsed");
    assert!(p.rows[0].tools.is_empty(), "collapsed groups carry no cards");
    settings.toggle_group("a");
    assert_eq!(present(&rows, &mut settings, None).rows[0].tools.len(), 2);
    rows.push(old_row("c", "2020-01-01T02:00:00Z", "live"));
    assert_eq!(present(&rows, &mut settings, None).rows.len(), 2);
    rows[2].kind = "assistant".into();
    assert_eq!(present(&rows, &mut settings, None).rows.len(), 2, "completion must not collapse a witnessed live row");
    settings.begin_visit();
    let p = present(&rows, &mut settings, None);
    assert_eq!(p.rows.len(), 1);
    assert!(p.rows[0].tools.is_empty());
    settings.activity_mode = ALWAYS_VISIBLE;
    assert_eq!(present(&rows, &mut settings, None).rows.len(), 3);
    // A row dispatched by a teammate or a scheduler stays outside the group.
    settings.activity_mode = GROUP_OLD;
    settings.begin_visit();
    assert_eq!(present(&rows, &mut settings, None).rows.len(), 1);
    let mut teammate = old_row("teammate", "2020-01-01T03:00:00Z", "");
    teammate.origin = "agent".into();
    rows.push(teammate);
    let p = present(&rows, &mut settings, None);
    assert_eq!(p.rows.len(), 2);
    assert!(p.rows[1].group_label.is_empty());
}

#[test]
fn ready_mode_preserves_activity_and_hides_only_provisional_body() {
    let tools = vec![json!({"name": "Bash"})];
    let cells = vec![json!({"kind": "command"})];
    let mut rows = vec![
        Message {
            id: "live".into(), role: "assistant".into(), kind: "live".into(),
            display_text: "Unfinished answer".into(), tools: tools.clone(), display_cells: cells.clone(),
            activity_count: 1, tool_details_available: true, ..Message::default()
        },
        Message {
            id: "activity".into(), activity: true, tool_name: "Bash".into(),
            display_text: "Search project files".into(), ..Message::default()
        },
    ];
    let mut settings = Settings { show_when_ready: true, ..Settings::default() };
    let p = present(&rows, &mut settings, None);
    assert_eq!(p.rows.len(), 2);
    assert_eq!(p.rows[0].body, "");
    assert_eq!((&p.rows[0].tools, &p.rows[0].display_cells), (&tools, &cells));
    assert_eq!(p.rows[0].activity_count, 1);
    assert!(p.rows[0].message.tool_details_available);
    assert_eq!(p.rows[1].body, "Search project files");
    settings.show_when_ready = false;
    assert_eq!(present(&rows, &mut settings, None).rows[0].body, "Unfinished answer");
    settings.show_when_ready = true;
    assert_eq!(present(&rows, &mut settings, None).rows[0].body, "");
    rows[0].kind = "assistant".into();
    let p = present(&rows, &mut settings, None);
    assert_eq!(p.rows[0].body, "Unfinished answer");
    assert_eq!(p.rows[0].tools, tools);
    rows[0].display_text.clear();
    rows[0].tools.clear();
    rows[0].display_cells.clear();
    assert_eq!(present(&rows, &mut settings, None).rows.len(), 2, "lazy details remain discoverable without text");
    settings.show_when_ready = false;
    assert_eq!(present(&rows, &mut settings, None).rows.len(), 2);
}

fn log(conversation: &mut Conversation, turns: Value, kind: LoadKind) {
    conversation.apply_log(json!({"conversation_id": "ready-thread", "turns": turns}).as_object().unwrap(), kind);
}

fn turn(id: &str, role: &str, kind: &str, text: &str, revision: i64) -> Value {
    json!({"id": id, "role": role, "kind": kind, "text": text, "revision": revision})
}

#[test]
fn ready_presentation_retains_canonical_stream_and_reveals_final() {
    let mut source = Conversation::new();
    source.open_session("ready");
    let mut settings = Settings::default();
    log(&mut source, json!([turn("u", "user", "", "Question", 1), turn("live", "assistant", "live", "Partial", 2)]), LoadKind::Tail);
    assert_eq!(present(source.rows(), &mut settings, None).rows.len(), 2);
    settings.show_when_ready = true;
    let before = present(source.rows(), &mut settings, None);
    assert_eq!(before.rows.len(), 1);
    assert_eq!(source.len(), 2);
    assert_eq!(before.index_of_message(source.rows(), "live"), None);
    log(&mut source, json!([turn("live", "assistant", "live", "Partial updated", 3)]), LoadKind::Delta);
    let after = present(source.rows(), &mut settings, None);
    assert!(diff(&before.rows, &after.rows).is_empty(), "a hidden stream announces nothing");
    source.show_transient_thinking("Agent");
    assert_eq!(present(source.rows(), &mut settings, None).rows.len(), 1);
    settings.show_when_ready = false;
    let all = present(source.rows(), &mut settings, None);
    assert_eq!(all.rows.len(), source.len());
    assert_eq!(all.index_of_message(source.rows(), "live"), Some(1));
    settings.show_when_ready = true;
    log(&mut source, json!([turn("final", "assistant", "assistant", "Partial updated complete", 4)]), LoadKind::Delta);
    let p = present(source.rows(), &mut settings, None);
    assert_eq!(p.rows.len(), 2);
    assert_eq!(p.rows[1].body, "Partial updated complete");
    assert!(p.rows[1].display_cells.is_empty());
}

#[test]
fn leading_day_tracks_visible_history() {
    let row = |id: &str, day: &str| Message { id: id.into(), role: "user".into(), timestamp: day.into(), ..Message::default() };
    // The C++ test sets DayLabelRole directly; here the label is the timestamp field.
    let label = |m: &Message| m.timestamp.clone();
    let mut settings = Settings::default();
    let leading = |rows: &[Message], s: &mut Settings| leading_day_label(&present(rows, s, None).rows, label);
    let mut rows: Vec<Message> = Vec::new();
    assert_eq!(leading(&rows, &mut settings), "");
    rows.push(row("1", "Today"));
    assert_eq!(leading(&rows, &mut settings), "Today");
    rows.push(row("2", "Today"));
    assert_eq!(leading(&rows, &mut settings), "Today");
    rows.insert(0, row("0", "Yesterday"));
    assert_eq!(leading(&rows, &mut settings), "Yesterday");
    rows.remove(0);
    assert_eq!(leading(&rows, &mut settings), "Today");
    rows[0].timestamp = "Yesterday".into();
    assert_eq!(leading(&rows, &mut settings), "Yesterday");
    rows = vec![row("a", ""), row("b", "Today")];
    assert_eq!(leading(&rows, &mut settings), "Today", "pending row without a timestamp");
    rows = vec![row("a", "12 June"), row("b", "Yesterday"), row("c", "Today")];
    assert_eq!(leading(&rows, &mut settings), "12 June");
}

#[test]
fn attached_tool_elapsed_uses_assistant_boundary_and_preserves_sender() {
    let mut source = Conversation::new();
    source.open_session("timing");
    let mut tools = json!({"id": "tools", "role": "assistant", "text": "Checking the build.", "activity_count": 21,
                           "timestamp": "2020-01-01T10:00:00Z"});
    let mut done = json!({"id": "done", "role": "assistant", "text": "Done.", "timestamp": "2020-01-01T10:01:23Z"});
    let load = |source: &mut Conversation, tools: &Value, done: &Value| {
        source.apply_log(json!({"turns": [tools, done]}).as_object().unwrap(), LoadKind::Replace);
    };
    load(&mut source, &tools, &done);
    let mut settings = Settings { activity_mode: GROUPED, ..Settings::default() };
    let before = present(source.rows(), &mut settings, None);
    assert_eq!(before.rows[0].activity_label, "21 tool calls · 1m 23s elapsed");
    assert!(before.rows[0].group_label.is_empty());
    done["timestamp"] = json!("2020-01-01T10:02:00Z");
    source.apply_log(json!({"turns": [done]}).as_object().unwrap(), LoadKind::Delta);
    let after = present(source.rows(), &mut settings, None);
    assert_eq!(after.rows[0].activity_label, "21 tool calls · 2m 0s elapsed");
    let announces_label = diff(&before.rows, &after.rows).iter().any(|op| {
        matches!(op, Op::Update { first: 0, roles, .. } if roles.contains(&Role::ActivityLabel))
    });
    assert!(announces_label, "a reply timestamp change re-announces the preceding label");
    done["role"] = json!("user");
    done["origin"] = json!("agent");
    done["sender_agent_id"] = json!("sender-id");
    done["sender_session"] = json!("sender-session");
    load(&mut source, &tools, &done);
    assert_eq!(present(source.rows(), &mut settings, None).rows[0].activity_label, "21 tool calls");
    assert_eq!(source.rows()[1].sender_agent_id, "sender-id");
    let mut restored = Conversation::new();
    assert!(restored.restore_cache_snapshot(&source.cache_snapshot()));
    assert_eq!(restored.rows()[1].sender_session, "sender-session");
    tools["timestamp"] = json!("invalid");
    load(&mut source, &tools, &done);
    assert_eq!(present(source.rows(), &mut settings, None).rows[0].activity_label, "21 tool calls");
}

#[test]
fn log_merge_costs_one_lookup_per_tool() {
    let turns = |count: usize, command: &str| -> Value {
        let mut rows = Vec::new();
        for turn in 0..count {
            rows.push(json!({"id": format!("tool-{turn}"), "role": "assistant", "text": "", "revision": turn + 1,
                             "tools": [{"name": "Bash", "input": {"command": format!("{command} {turn}")}}]}));
            rows.push(json!({"id": format!("reply-{turn}"), "role": "assistant", "text": format!("Step {turn} done."), "revision": turn + 1}));
        }
        Value::Array(rows)
    };
    let mut source = Conversation::new();
    source.open_session("storm");
    source.apply_log(json!({"conversation_id": "c", "turns": turns(40, "ls")}).as_object().unwrap(), LoadKind::Replace);
    let mut settings = Settings::default();
    let lookups = Cell::new(0);
    let counting = |_: &serde_json::Map<String, Value>| {
        lookups.set(lookups.get() + 1);
        "Lists files.".to_owned()
    };
    let before = present(source.rows(), &mut settings, Some(&counting));
    lookups.set(0);
    source.apply_log(json!({"conversation_id": "c", "turns": turns(50, "ls -la")}).as_object().unwrap(), LoadKind::Tail);
    assert_eq!(source.len(), 100);
    // The adapter recomputes once per source mutation (one per log batch).
    let after = present(source.rows(), &mut settings, Some(&counting));
    assert!(lookups.get() <= 50, "{} lookups for one merge", lookups.get());
    assert_eq!(after.rows.last().unwrap().body, "Step 49 done.");
    let ops = diff(&before.rows, &after.rows);
    assert!(!ops.iter().any(|op| matches!(op, Op::Reset(_))));
}

#[test]
fn diff_updates_in_place_and_keeps_order() {
    let rows = |ids: &[&str]| -> Vec<PresentedRow> {
        let messages: Vec<Message> = ids.iter().map(|id| Message { id: (*id).into(), display_text: (*id).into(), ..Message::default() }).collect();
        present(&messages, &mut Settings::default(), None).rows
    };
    let old = rows(&["a", "b", "c"]);
    let mut new = rows(&["a", "c", "d"]);
    new[0].body = "changed".into();
    let ops = diff(&old, &new);
    let mut mirror = old.clone();
    clarp_core::list_ops::replay(&mut mirror, &ops);
    assert_eq!(mirror, new);
    assert!(ops.iter().any(|op| matches!(op, Op::Update { first: 0, roles, .. } if roles == &vec![Role::Base(clarp_core::conversation::Role::Body)])));
    assert!(diff(&new, &new).is_empty());
    let moved = rows(&["c", "a", "d"]);
    let mut mirror = new.clone();
    clarp_core::list_ops::replay(&mut mirror, &diff(&new, &moved));
    assert_eq!(mirror, moved);
}

/// tst_native_core::streamedTokenAnnouncesOnlyItsRows: with narration on,
/// one streamed token must not re-announce every row of the transcript.
#[test]
fn streamed_token_announces_only_its_rows() {
    let mut rows: Vec<Message> = (0..60)
        .map(|i| {
            if i % 2 == 0 {
                tool_row(&format!("m{i}"), &format!("step {i}"), "")
            } else {
                let mut row = tool_row(&format!("m{i}"), "", &format!("Reply {i}"));
                row.tools.clear();
                row
            }
        })
        .collect();
    let mut settings = Settings::default();
    let lookup: Lookup = &summary_lookup;
    let before = present(&rows, &mut settings, Some(lookup));
    rows[59].display_text = "Reply 59, streaming more".into();
    rows[59].text = rows[59].display_text.clone();
    let after = present(&rows, &mut settings, Some(lookup));
    let ops = diff(&before.rows, &after.rows);
    assert!(!ops.is_empty());
    let mut touched = std::collections::BTreeSet::new();
    for op in &ops {
        match op {
            Op::Update { first, items, .. } => touched.extend(*first..*first + items.len()),
            other => panic!("a token only updates rows, got {other:?}"),
        }
    }
    assert!(touched.len() <= 3, "{} rows announced for one token", touched.len());
    assert_eq!(after.rows.len(), before.rows.len());
    assert_eq!(after.rows.last().unwrap().message.display_text, "Reply 59, streaming more");

    // A change that does alter a run keeps the row count.
    rows[58].tools = vec![json!({"name": "Bash", "summary": "step 56"})];
    assert_eq!(present(&rows, &mut settings, Some(lookup)).rows.len(), after.rows.len());
}
