//! Conversation and Session views (#702, feature `chat_view`): which view
//! a chat draws, the rows its Conversation view leaves out, Show work, the
//! Running box's words and the conversation fork's main-turn line.

use std::collections::HashSet;

use clarp_core::chat_view::{self, CONVERSATION, SESSION};
use clarp_core::json::Object;
use clarp_core::live::LiveView;
use clarp_core::live_present::{Entry, Kind};
use clarp_core::protocol::Message;
use serde_json::json;

fn object(value: serde_json::Value) -> Object {
    value.as_object().cloned().unwrap_or_default()
}

fn row(value: serde_json::Value) -> Message {
    Message::from_json(&object(value))
}

#[test]
fn own_choice_then_clean_chat_then_the_host_then_session() {
    assert_eq!(chat_view::effective(Some("session"), true, "conversation"), SESSION);
    assert_eq!(chat_view::effective(None, true, "session"), CONVERSATION);
    assert_eq!(chat_view::effective(None, false, "conversation"), CONVERSATION);
    assert_eq!(chat_view::effective(None, false, ""), SESSION);
    // A value this app does not know reads as Session: the view that hides nothing.
    assert_eq!(chat_view::effective(None, false, "compact"), SESSION);
    assert_eq!(chat_view::effective(Some("bogus"), false, "conversation"), CONVERSATION);
}

#[test]
fn the_own_choice_is_kept_by_chat_key_and_cleared_by_follow_settings() {
    let mut settings = clarp_core::settings::Settings::in_memory();
    chat_view::set_own(&mut settings, "@srv/rachel", Some("conversation"));
    assert_eq!(chat_view::own(&settings, "@srv/rachel"), Some(CONVERSATION));
    assert_eq!(chat_view::own(&settings, "@srv/mike"), None);
    chat_view::set_own(&mut settings, "@srv/rachel", None);
    assert_eq!(chat_view::own(&settings, "@srv/rachel"), None);
    assert!(settings.get(chat_view::OWN_KEY).is_none(), "nothing left stored");
}

fn rows() -> Vec<Message> {
    vec![
        row(json!({"id": "u-1", "role": "user", "text": "Is it green?", "origin": "user", "trace_id": "t-1", "view": "conversation"})),
        row(json!({"id": "w-1", "role": "assistant", "text": "Checking.", "phase": "commentary", "trace_id": "t-1", "view": "session"})),
        row(json!({"id": "w-2", "role": "assistant", "text": "", "trace_id": "t-1", "view": "session", "tools": [{"id": "c-1", "name": "Bash"}]})),
        row(json!({"id": "f-1", "role": "assistant", "text": "Green.", "phase": "final", "trace_id": "t-1", "view": "conversation", "session_rows": 2})),
        row(json!({"id": "a-1", "role": "user", "text": "Look at this", "origin": "agent", "sender_name": "Arnold", "trace_id": "t-a", "view": "session"})),
        row(json!({"id": "old", "role": "assistant", "text": "Untagged"})),
    ]
}

#[test]
fn conversation_view_leaves_out_session_rows_and_offers_show_work_on_the_anchor() {
    let rows = rows();
    assert!(chat_view::tagged(&rows));
    let projection = chat_view::project(&rows, &HashSet::new());
    let hidden: HashSet<&str> = projection.hidden_rows.iter().map(String::as_str).collect();
    assert_eq!(hidden, HashSet::from(["w-1", "w-2", "a-1", "old"]));
    let work = projection.anchors.get("f-1").expect("the reply offers Show work");
    assert_eq!((work.key.as_str(), work.rows, work.open), ("live:work:t-1", 2, false));
    assert_eq!(projection.anchors.len(), 1);

    let opened = chat_view::project(&rows, &HashSet::from(["live:work:t-1".to_owned()]));
    let hidden: HashSet<&str> = opened.hidden_rows.iter().map(String::as_str).collect();
    assert_eq!(hidden, HashSet::from(["a-1", "old"]), "the opened turn's rows show; another turn's stay out");
    assert!(opened.anchors["f-1"].open);
}

#[test]
fn an_unsent_message_of_ones_own_is_conversation_and_untagged_rows_are_not() {
    let mut own = row(json!({"id": "u-x", "role": "user", "text": "hi"}));
    own.pending = true;
    assert!(chat_view::in_conversation(&own));
    assert!(!chat_view::in_conversation(&row(json!({"id": "x", "role": "user", "text": "hi"}))));
    assert!(!chat_view::tagged(&[row(json!({"id": "x", "role": "user"}))]), "a cache from before the tags is drawn as Session");
}

#[test]
fn the_running_box_says_what_runs() {
    assert_eq!(chat_view::running(""), "Running…");
    assert_eq!(chat_view::running("Thinking"), "Thinking…");
    assert_eq!(chat_view::running("Running swift build..."), "Running swift build…");
    let tool = object(json!({"state": "tool", "tool": {"name": "Bash", "label": "swift build\n--verbose"}}));
    assert_eq!(chat_view::status_words(&tool), "Running swift build");
    let named = object(json!({"state": "tool", "tool": {"name": "Read"}}));
    assert_eq!(chat_view::status_words(&named), "Reading files");
    let headline = object(json!({"state": "tool", "headline": "Running the tests", "tool": {"name": "Bash", "label": "npm test"}}));
    assert_eq!(chat_view::status_words(&headline), "Running the tests");
    assert_eq!(chat_view::status_words(&object(json!({"state": "thinking"}))), "Thinking");
    assert_eq!(chat_view::status_words(&object(json!({"state": "responding"}))), "Writing");
    assert_eq!(chat_view::status_words(&object(json!({"state": "idle"}))), "");
}

#[test]
fn the_main_turn_line_names_who_and_the_kind_of_step_never_its_text() {
    let main = object(json!({"turn_id": "t-m", "audience": "agents", "status": "running", "activity": {"state": "tool", "tool": {"name": "Bash"}}}));
    assert_eq!(chat_view::main_turn_line(&main, "").as_deref(), Some("Also working with another agent · Running a command"));
    assert_eq!(chat_view::main_turn_line(&main, " Bella ").as_deref(), Some("Also working with Bella · Running a command"), "named as iOS names it");
    let own = object(json!({"audience": "user", "status": "running", "activity": {"state": "thinking"}}));
    assert_eq!(chat_view::main_turn_line(&own, "Bella").as_deref(), Some("Also working · Thinking"), "a turn for the user names no one");
    assert_eq!(chat_view::main_turn_line(&object(json!({"status": "completed"})), ""), None);
    assert_eq!(chat_view::second_line(1, Some("Also working")), "1 step failed · Also working");
    assert_eq!(chat_view::second_line(2, None), "2 steps failed");
    assert_eq!(chat_view::second_line(0, None), "");
}

fn entry(key: &str, kind: Kind, status: &str) -> Entry {
    Entry { key: key.into(), kind, status: status.into(), ..Entry::default() }
}

#[test]
fn a_running_turn_is_one_box_that_opens_to_its_steps() {
    let fold = Entry { expandable: true, items: vec!["i1".into()], ..entry("live:fold:t-2", Kind::Fold, "running") };
    let collapsed = vec![entry("live:m0", Kind::Message, "completed"), fold.clone(), entry("live:bad", Kind::Tool, "failed")];
    let look = |title: &str| chat_view::BoxLook { title: title.into(), ..Default::default() };
    let boxed = chat_view::running_box(&collapsed, "t-2", &chat_view::BoxLook { meta: "0:05".into(), other: Some("Also working".into()), ..look("Running npm test…") });
    assert_eq!(boxed.len(), 1);
    assert_eq!(boxed[0].key, "live:fold:t-2");
    assert_eq!(boxed[0].title, "Running npm test…");
    assert_eq!(boxed[0].secondary, "1 step failed · Also working");
    assert!(boxed[0].expandable && !boxed[0].expanded);

    let open_fold = Entry { expanded: true, ..fold };
    let opened = vec![open_fold, entry("live:i1", Kind::Tool, "running"), entry("live:m1", Kind::Message, "completed")];
    let boxed = chat_view::running_box(&opened, "t-2", &look("Running…"));
    let keys: Vec<&str> = boxed.iter().map(|e| e.key.as_str()).collect();
    assert_eq!(keys, ["live:fold:t-2", "live:i1"], "opened: its steps under it, never its words in between");

    let nothing = chat_view::running_box(&[], "busy", &look("Running…"));
    assert_eq!(nothing.len(), 1);
    assert!(!nothing[0].expandable);
    assert_eq!((nothing[0].status.as_str(), nothing[0].stop.as_str()), ("running", ""), "a working box holds no Stop");
}

#[test]
fn a_stalled_turn_says_so_in_the_box_and_holds_stop() {
    let fold = Entry { expandable: true, ..entry("live:fold:t-2", Kind::Fold, "running") };
    let words = clarp_core::stall::indicator(0, 12 * 60_000, true);
    let look = chat_view::BoxLook { title: "Running npm test…".into(), meta: "14:02".into(), stall: Some(words.clone()), stop_key: "Ctrl+.".into(), ..Default::default() };
    let boxed = chat_view::running_box(&[fold.clone()], "t-2", &look);
    assert_eq!(boxed[0].title, "Seems stuck · no progress for 12 min · Your message is waiting", "the desktop's own words");
    assert_eq!((boxed[0].status.as_str(), boxed[0].stop.as_str(), boxed[0].meta.as_str()), (chat_view::STALLED, "Ctrl+.", "14:02"));
    assert!(boxed[0].expandable, "it still opens to its steps");
    // With no key bound, Stop still shows.
    let unbound = chat_view::running_box(&[fold], "t-2", &chat_view::BoxLook { stop_key: String::new(), ..look.clone() });
    assert!(!unbound[0].stop.is_empty());
    // A stall that clears gives the box back its words.
    let cleared = chat_view::running_box(&[], "t-2", &chat_view::BoxLook { stall: None, ..look });
    assert_eq!((cleared[0].title.as_str(), cleared[0].stop.as_str()), ("Running npm test…", ""));
}

#[test]
fn a_turn_between_agents_says_only_who_and_the_kind_of_step() {
    assert_eq!(chat_view::working_with("Bella"), "Working with Bella");
    assert_eq!(chat_view::working_with("  "), "Working with another agent");
    assert!(chat_view::between_agents("Agents") && !chat_view::between_agents("user") && !chat_view::between_agents(""));
    let fold = Entry { expandable: true, expanded: true, ..entry("live:fold:t-a", Kind::Fold, "running") };
    let steps = vec![fold, entry("live:i1", Kind::Tool, "failed")];
    let look = chat_view::BoxLook {
        title: "Running clarp-admin prompt --to bella…".into(),
        with: Some(chat_view::working_with("Bella")),
        step: Some("Running a command".into()),
        ..Default::default()
    };
    let boxed = chat_view::running_box(&steps, "t-a", &look);
    assert_eq!(boxed.len(), 1, "its steps never open");
    assert_eq!(boxed[0].title, "Working with Bella", "never the turn's activity");
    assert_eq!(boxed[0].secondary, "Running a command · 1 step failed");
    assert!(!boxed[0].expandable);
    // Stuck wins over who: a stalled agent is never hidden.
    let stuck = chat_view::running_box(&steps, "t-a", &chat_view::BoxLook { stall: Some("Seems stuck · no progress for 10 min".into()), ..look });
    assert_eq!((stuck[0].title.as_str(), stuck[0].status.as_str()), ("Seems stuck · no progress for 10 min", chat_view::STALLED));
}

#[test]
fn the_sender_is_the_turns_agent_prompt_else_the_agent_rows() {
    let rows = rows();
    assert_eq!(chat_view::sender_of(&rows, "t-a", "Mike"), "Arnold");
    assert_eq!(chat_view::sender_of(&rows, "t-z", "Mike"), "Mike", "a turn whose prompt is not read yet");
    assert_eq!(chat_view::sender_of(&rows, "", ""), "");
}

#[test]
fn a_finished_turn_keeps_its_reply_only() {
    let shown = |row: &str| row == "f-1";
    let reply = Entry { phase: "final".into(), ..entry("live:m", Kind::Message, "completed") };
    let commentary = Entry { phase: "commentary".into(), ..entry("live:c", Kind::Message, "completed") };
    let taken = Entry { row: "w-1".into(), ..entry("live:w", Kind::Message, "completed") };
    let anchor = Entry { row: "f-1".into(), ..entry("live:f", Kind::Message, "completed") };
    assert!(chat_view::finished_entry_stays(&reply, shown));
    assert!(!chat_view::finished_entry_stays(&commentary, shown));
    assert!(!chat_view::finished_entry_stays(&taken, shown));
    assert!(chat_view::finished_entry_stays(&anchor, shown));
    assert!(!chat_view::finished_entry_stays(&entry("live:fold:t", Kind::Fold, "completed"), shown));
}

#[test]
fn the_busy_turn_is_the_newest_prompts_until_its_anchor_lands() {
    let mut rows = rows();
    assert_eq!(chat_view::busy_turn(&rows).as_deref(), Some("t-a"), "the newest prompt's turn, another agent's too");
    rows.push(row(json!({"id": "u-2", "role": "user", "trace_id": "t-2", "view": "conversation"})));
    assert_eq!(chat_view::busy_turn(&rows).as_deref(), Some("t-2"));
    rows.push(row(json!({"id": "f-2", "role": "assistant", "trace_id": "t-2", "view": "conversation", "session_rows": 0})));
    assert_eq!(chat_view::busy_turn(&rows), None);
}

#[test]
fn live_keeps_the_main_turn_beside_a_forks() {
    let mut view = LiveView::new();
    view.apply_snapshot(&object(json!({"epoch": "e", "lseq": 0, "activity": {"state": "idle"}, "turn": null, "items": [],
        "main_turn": {"turn_id": "t-m", "status": "running", "audience": "agents"}})));
    assert_eq!(view.main_turn().and_then(|m| m.get("turn_id")).and_then(|t| t.as_str()), Some("t-m"));
    view.apply_event(&object(json!({"epoch": "e", "lseq": 1, "ops": [{"op": "main_turn", "main_turn": null}]})));
    assert!(view.main_turn().is_none(), "null: it no longer runs");
    view.apply_event(&object(json!({"epoch": "e", "lseq": 2, "ops": [{"op": "main_turn", "main_turn": {"turn_id": "t-n", "status": "running"}}]})));
    assert_eq!(view.lseq(), Some(2));
    assert!(view.main_turn().is_some());
}

#[test]
fn rows_carry_their_view_and_the_anchor_its_session_rows() {
    let anchor = row(json!({"id": "f", "role": "assistant", "view": "Conversation", "session_rows": 3}));
    assert_eq!(anchor.view, "conversation");
    assert_eq!(anchor.session_rows, Some(3));
    let cached = clarp_core::conversation::message_to_json(&anchor);
    assert_eq!(cached["view"], "conversation");
    assert_eq!(cached["session_rows"], 3);
    let agent = clarp_core::protocol::Agent::from_json(&object(json!({"session": "s", "chat_view": "conversation", "conversation_fork": true})));
    assert_eq!(agent.chat_view, "conversation");
    assert!(agent.conversation_fork);
}

/// #727: an opened turn's work is one box under its reply, as on iOS.
#[test]
fn opened_work_moves_under_its_reply_in_one_box() {
    use chat_view::{Edge, WorkBox, gather};
    let ids = ["u-1", "w-1", "w-2", "f-1", "u-2"];
    let boxed = WorkBox { anchor: "f-1".into(), inside: false, members: ["w-1", "w-2"].iter().map(|s| (*s).to_owned()).collect() };
    let (order, edges) = gather(&ids, &[boxed]);
    let drawn: Vec<&str> = order.iter().map(|&i| ids[i]).collect();
    assert_eq!(drawn, ["u-1", "f-1", "w-1", "w-2", "u-2"]);
    assert_eq!(edges, [Edge::None, Edge::None, Edge::Top, Edge::Bottom, Edge::None]);
}

#[test]
fn the_running_box_begins_its_own_box_and_one_row_is_a_whole_box() {
    use chat_view::{Edge, WorkBox, gather};
    let ids = ["u-2", "live:fold:t-2", "live:it-1"];
    let running = WorkBox { anchor: "live:fold:t-2".into(), inside: true, members: ["live:it-1".to_owned()].into_iter().collect() };
    let (order, edges) = gather(&ids, &[running]);
    assert_eq!(order, [0, 1, 2]);
    assert_eq!(edges, [Edge::None, Edge::Top, Edge::Bottom]);
    let single = WorkBox { anchor: "f".into(), inside: false, members: ["w".to_owned()].into_iter().collect() };
    assert_eq!(gather(&["w", "f"], &[single]).1, [Edge::None, Edge::Only]);
}

#[test]
fn a_box_whose_reply_is_not_drawn_leaves_its_rows_alone() {
    use chat_view::{Edge, WorkBox, gather};
    let boxed = WorkBox { anchor: "gone".into(), inside: false, members: ["w-1".to_owned()].into_iter().collect() };
    let (order, edges) = gather(&["u-1", "w-1"], &[boxed]);
    assert_eq!(order, [0, 1]);
    assert_eq!(edges, [Edge::None, Edge::None]);
}

/// Peter's own message stays in the conversation from send to read-back:
/// pending, then delivered (the /send answer) while the Host has not sent
/// the row back yet (a message steered into a running turn comes back only
/// when the turn reads it), then as the Host's tagged row.
#[test]
fn an_own_message_never_leaves_the_conversation_before_the_host_sends_it_back() {
    let mut own = row(json!({"id": "u-1", "role": "user", "text": "Is the server under load?"}));
    own.pending = true;
    own.sent_here = true;
    assert!(chat_view::in_conversation(&own), "pending");
    own.pending = false;
    assert!(chat_view::in_conversation(&own), "delivered, not yet read back by the Host");
    let back = row(json!({"id": "u-1", "role": "user", "text": "Is the server under load?", "revision": 24750587904_i64, "view": CONVERSATION}));
    assert!(chat_view::in_conversation(&back), "the Host's row, tagged conversation");
    let untagged_host_row = row(json!({"id": "u-2", "role": "user", "text": "from another agent", "revision": 7}));
    assert!(!chat_view::in_conversation(&untagged_host_row), "a Host row without a tag still reads as Session");
}

#[test]
fn a_limited_turn_without_words_says_what_it_waits_for() {
    use clarp_core::chat_view::limited_words;
    let o = |v: serde_json::Value| v.as_object().unwrap().clone();
    assert_eq!(limited_words(&o(serde_json::json!({"state": "limited", "waits_for": "machine"}))), "Waiting for the machine");
    assert_eq!(limited_words(&o(serde_json::json!({"state": "limited", "waits_for": "network"}))), "Waiting for the network");
    assert_eq!(limited_words(&o(serde_json::json!({"state": "limited", "waits_for": "quota"}))), "Waiting for the usage limit");
    // An older Host sends no waits_for: a usage limit, as before.
    assert_eq!(limited_words(&o(serde_json::json!({"state": "limited"}))), "Waiting for the usage limit");
}
