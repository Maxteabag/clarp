//! A reply the turn decided stays drawn, once, when the owner sends the
//! next message (hazel-74f6, 26 Nov 2026 17:45). As the Host journaled it:
//!
//! - 0 s: turn 2235 starts on its prompt and runs four tools.
//! - 18.1 s: the owner sends a message from this desktop (turn 2237); the
//!   Host accepts it and joins it to the running 2235. The desktop holds it
//!   as its own row, delivered, not yet sent back.
//! - 23.3 s: 2235 writes its reply. Undecided: `commentary`, view `session`.
//! - 23.9 s: 2235 reads the joined message (JoinRead): the reply becomes an
//!   answer (`final`, view `conversation`, the turn's anchor) and the
//!   joined message's row is written (`read_into` 2235, `steer`), both at
//!   revision 71808.
//! - 26.6 s: the owner sends another message (2238), which stops 2235; its
//!   interrupted end is written onto the reply's row (`closes` it, revision
//!   71821); the joined message's row closes 2237 (71818); 2238 starts.
//!
//! The ids, phases, views, revisions and summaries are the Host's; no
//! message text is. Every step goes through the chat's own entry points
//! (`Conversation` loads, sends and cache, `LiveView` snapshots and events)
//! and is drawn as the pane draws it (`slint-app/src/panes.rs` `messages`):
//! live entries and history folds over the `/log` rows, less what they and
//! the Conversation view hide. Visibility (drawn once) is asserted at every
//! step; place (and whether it stays the same across live, reconnect and
//! reload) is checked on its own, so the two failures read apart.

use std::collections::HashSet;
use std::sync::LazyLock;

use clarp_core::chat_view::{self, BoxLook};
use clarp_core::conversation::{Conversation, LoadKind};
use clarp_core::history_fold::{self, Piece};
use clarp_core::live::LiveView;
use clarp_core::live_present::{self, Kind, Options, Presented};
use clarp_core::protocol::Message;
use serde_json::{Map, Value, json};

/// The turn that wrote the reply (2235) and its prompt.
const F: &str = "f160380e8d43e5a1";
const PROMPT: &str = "u-6d6b371e-2b75-4729-9ebd-e06d643abf57";
const TOOLS: [&str; 4] = ["r-011ChRspRoYZtFWWU76y2jCc", "r-011ChRspgpgVoR6wdeeMZEJT", "r-011ChRspx1j99nTxeJ49nowT", "r-011ChRsq7fAaRYSRk3vcSrPb"];
const REPLY: &str = "r-011ChRsqN2cEU8SJ9XnguE8k";
/// Its live item.
const REPLY_ITEM: &str = "cl:msg_011ChRsqN2cEU8SJ9XnguE8k";
/// The owner's message joined to the running turn (2237).
const STEER_CLIENT: &str = "0c0dd7ab-dc93-4325-8c07-ac896a286a2f";
const STEER: &str = "u-0c0dd7ab-dc93-4325-8c07-ac896a286a2f";
const STEER_TRACE: &str = "c56b8a196d8bb63e";
/// The owner's next message (2238); it stopped 2235.
const NEXT_CLIENT: &str = "46fc2d7a-a2f0-40d9-9b03-1734d58fcf01";
const NEXT: &str = "u-46fc2d7a-a2f0-40d9-9b03-1734d58fcf01";
const NEXT_TRACE: &str = "8ab15f61535d5a19";

/// The Host's revision step: a row's revision is the log seq it changed at
/// times this.
const STEP: i64 = 1 << 20;

/// When the steps happened, after the turn began (ms; the Host's journal).
const TOOL_AFTER: [i64; 4] = [3_230, 6_524, 8_734, 12_275];
const STEER_SENT_AFTER: i64 = 18_139;
const REPLY_AFTER: i64 = 23_275;
const STEER_READ_AFTER: i64 = 23_916;
const ENDED_AFTER: i64 = 26_650;

/// When the turn began, on this machine's clock: a send of one's own is
/// stamped with the clock when it is made, so the joined message's own row
/// is stamped where it was sent (18.1 s), as on the owner's desktop.
static STARTED: LazyLock<i64> = LazyLock::new(|| chrono::Utc::now().timestamp_millis() - STEER_SENT_AFTER);

fn t(after: i64) -> i64 {
    *STARTED + after
}

/// Stand-in words (no real message text).
const REPLY_WORDS: &str = "The answer, as written.";

fn stamp(ms: i64) -> String {
    chrono::DateTime::from_timestamp_millis(ms).unwrap().format("%Y-%m-%dT%H:%M:%S%.3fZ").to_string()
}

fn obj(value: Value) -> Map<String, Value> {
    value.as_object().cloned().unwrap()
}

fn page(rows: Vec<Value>, latest: i64) -> Map<String, Value> {
    obj(json!({"conversation_id": "c-hazel", "turns": rows, "latest_revision": latest, "has_more": false, "replace_required": false}))
}

fn prompt_row() -> Value {
    json!({"id": PROMPT, "role": "user", "timestamp": stamp(t(0)), "trace_id": F, "text": "Prompt.", "origin": "user", "audience": "user", "view": "conversation", "revision": 71_745 * STEP})
}

fn tool_row(index: usize) -> Value {
    let revision = [71_761, 71_771, 71_781, 71_790][index] * STEP;
    json!({"id": TOOLS[index], "role": "assistant", "timestamp": stamp(t(TOOL_AFTER[index])), "trace_id": F, "text": "", "origin": "user", "audience": "user",
        "tools": [{"id": format!("toolu_{index}"), "name": "Bash", "command": "true", "status": "ok"}], "view": "session", "revision": revision})
}

/// The reply as the Host serves it: written and undecided (commentary,
/// session), decided at JoinRead (final, conversation, the turn's anchor),
/// and closing its interrupted turn. One id, one text, one timestamp.
#[derive(Clone, Copy, PartialEq)]
enum Reply {
    Undecided,
    Decided,
    Closed,
}

fn reply_row(state: Reply) -> Value {
    let mut row = json!({"id": REPLY, "role": "assistant", "timestamp": stamp(t(REPLY_AFTER)), "trace_id": F, "text": REPLY_WORDS, "origin": "user", "audience": "user"});
    match state {
        Reply::Undecided => {
            row["phase"] = json!("commentary");
            row["view"] = json!("session");
            row["revision"] = json!(71_804 * STEP);
        }
        Reply::Decided | Reply::Closed => {
            row["phase"] = json!("final");
            row["view"] = json!("conversation");
            row["session_rows"] = json!(4);
            row["revision"] = json!(if state == Reply::Closed { 71_821 * STEP } else { 71_808 * STEP });
        }
    }
    if state == Reply::Closed {
        row["turn"] = json!({"turn_id": F, "status": "interrupted", "started_at_ms": t(0), "ended_at_ms": t(ENDED_AFTER), "worked_ms": 26_650, "tool_count": 4, "reason": "Stopped."});
    }
    row
}

/// The joined message's own row, written at JoinRead.
fn steer_row(closed: bool) -> Value {
    let mut row = json!({"id": STEER, "role": "user", "timestamp": stamp(t(STEER_READ_AFTER)), "trace_id": STEER_TRACE, "text": "Steered.", "origin": "user", "audience": "user",
        "view": "conversation", "read_into": {"trace_id": F, "turn": 2235, "at": "steer"}, "revision": 71_808 * STEP});
    if closed {
        row["session_rows"] = json!(0);
        row["turn"] = json!({"turn_id": STEER_TRACE, "status": "interrupted", "started_at_ms": t(STEER_READ_AFTER), "ended_at_ms": t(ENDED_AFTER), "worked_ms": 2_734, "tool_count": 0});
        row["revision"] = json!(71_818 * STEP);
    }
    row
}

fn next_row() -> Value {
    json!({"id": NEXT, "role": "user", "timestamp": stamp(t(ENDED_AFTER)), "trace_id": NEXT_TRACE, "text": "Next.", "origin": "user", "audience": "user", "view": "conversation", "revision": 71_824 * STEP})
}

fn tool_item(index: usize) -> Value {
    json!({"id": format!("cl:toolu_{index}"), "kind": "tool", "rev": 2, "turn_id": F, "status": "completed", "ordinal": index as i64 + 1,
        "started_at_ms": t(TOOL_AFTER[index]) - 50, "ended_at_ms": t(TOOL_AFTER[index]),
        "tool": {"name": "Bash", "call_id": format!("toolu_{index}"), "category": "exec", "group": null, "label": "true", "command": "true"}})
}

fn reply_item(phase: &str, rev: i64) -> Value {
    json!({"id": REPLY_ITEM, "kind": "message", "rev": rev, "turn_id": F, "status": "completed", "ordinal": 5, "phase": phase,
        "started_at_ms": t(REPLY_AFTER) - 400, "ended_at_ms": t(REPLY_AFTER), "text": REPLY_WORDS, "row_id": REPLY})
}

fn running_turn(read: bool) -> Value {
    let mut turn = json!({"turn_id": F, "status": "running", "started_at_ms": t(0), "tool_count": 4});
    if read {
        turn["read_rows"] = json!([STEER]);
        turn["read_rows_at"] = json!(["steer"]);
    }
    turn
}

/// `GET /live` while 2235 runs its tools (before the reply).
fn running_snapshot() -> Map<String, Value> {
    let items: Vec<Value> = (0..4).map(tool_item).collect();
    obj(json!({"conv": "c-hazel", "session": "hazel-74f6", "agent_id": "fa5dc7b71827313f", "epoch": "e1", "lseq": 9, "server_now_ms": t(STEER_SENT_AFTER),
        "activity": {"state": "thinking", "turn_id": F}, "turn": running_turn(false), "items": items}))
}

/// `GET /live` once JoinRead decided the reply (lseq 11).
fn decided_snapshot() -> Map<String, Value> {
    let mut items: Vec<Value> = (0..4).map(tool_item).collect();
    items.push(reply_item("final", 4));
    obj(json!({"conv": "c-hazel", "session": "hazel-74f6", "agent_id": "fa5dc7b71827313f", "epoch": "e1", "lseq": 11, "server_now_ms": t(STEER_READ_AFTER),
        "activity": {"state": "thinking", "turn_id": F}, "turn": running_turn(true), "items": items}))
}

fn event(lseq: i64, ops: Vec<Value>) -> Map<String, Value> {
    obj(json!({"epoch": "e1", "lseq": lseq, "server_now_ms": t(ENDED_AFTER), "ops": ops}))
}

/// The reply is written: a message, undecided.
fn reply_written() -> Map<String, Value> {
    let mut op = json!({"op": "upsert", "id": REPLY_ITEM, "kind": "message", "rev": 3});
    op["item"] = reply_item("commentary", 3);
    event(10, vec![op, json!({"op": "status", "activity": {"state": "responding", "turn_id": F}})])
}

/// JoinRead: the joined message is read; the reply is an answer from now on.
fn join_read() -> Map<String, Value> {
    event(11, vec![
        json!({"op": "upsert", "id": REPLY_ITEM, "kind": "message", "rev": 4, "item": {"phase": "final"}}),
        json!({"op": "turn", "turn": running_turn(true)}),
        json!({"op": "status", "activity": {"state": "thinking", "turn_id": F}}),
    ])
}

/// JoinRead whose promotion of the reply never reaches the live view: the
/// turn says it read the message; the reply's item still says commentary.
fn join_read_stale() -> Map<String, Value> {
    event(11, vec![
        json!({"op": "turn", "turn": running_turn(true)}),
        json!({"op": "status", "activity": {"state": "thinking", "turn_id": F}}),
    ])
}

/// The next message stops the turn: interrupted.
fn interrupted() -> Map<String, Value> {
    let mut turn = running_turn(true);
    turn["status"] = json!("interrupted");
    turn["ended_at_ms"] = json!(t(ENDED_AFTER));
    turn["worked_ms"] = json!(26_650);
    event(12, vec![json!({"op": "turn", "turn": turn}), json!({"op": "status", "activity": {"state": "idle"}})])
}

/// The next message's turn begins.
fn next_started() -> Map<String, Value> {
    event(13, vec![
        json!({"op": "turn", "turn": {"turn_id": NEXT_TRACE, "status": "running", "started_at_ms": t(ENDED_AFTER)}}),
        json!({"op": "status", "activity": {"state": "thinking", "turn_id": NEXT_TRACE}}),
    ])
}

/// A later `GET /live`: the next turn, nothing of the old one.
fn next_snapshot(epoch: &str, lseq: i64) -> Map<String, Value> {
    obj(json!({"conv": "c-hazel", "session": "hazel-74f6", "agent_id": "fa5dc7b71827313f", "epoch": epoch, "lseq": lseq, "server_now_ms": t(ENDED_AFTER) + 500,
        "activity": {"state": "thinking", "turn_id": NEXT_TRACE},
        "turn": {"turn_id": NEXT_TRACE, "status": "running", "started_at_ms": t(ENDED_AFTER)}, "items": []}))
}

#[derive(Clone, Copy, PartialEq, Debug)]
enum Mode {
    Conversation,
    Grouped,
    AlwaysVisible,
}

const MODES: [Mode; 3] = [Mode::Conversation, Mode::Grouped, Mode::AlwaysVisible];

/// The rows and entries the pane draws, top to bottom: a durable row by its
/// id (also where an entry stands for it), any other entry by its key.
/// As `panes.rs` `messages` with `live_view::present` and `history` (the
/// Running box of a turn no live turn tells of is left out), the agent busy
/// while a turn runs (`busy`).
fn drawn(rows: &[Message], view: Option<&LiveView>, mode: Mode, busy: bool) -> Vec<String> {
    let conversation = mode == Mode::Conversation && chat_view::tagged(rows);
    let live: Option<Presented> = view.map(|view| {
        let options = Options {
            fold_running: conversation || mode == Mode::Grouped,
            show_commentary: !conversation && mode == Mode::AlwaysVisible,
            whole_turn: conversation,
            ..Options::default()
        };
        let mut presented = live_present::present(view, rows, &options);
        let running = view.turn().is_some_and(|t| matches!(t.get("status").and_then(Value::as_str), Some("running") | None));
        if conversation {
            if running && !presented.turn_id.is_empty() {
                let look = BoxLook { title: chat_view::running(""), ..BoxLook::default() };
                presented.entries = chat_view::running_box(&presented.entries, &presented.turn_id, &look);
            } else {
                let shown: HashSet<&str> = rows.iter().filter(|r| chat_view::in_conversation(r)).map(|r| r.id.as_str()).collect();
                presented.entries.retain(|e| chat_view::finished_entry_stays(e, |row| shown.contains(row)));
            }
        }
        presented
    });
    let running = view.and_then(LiveView::turn).is_some_and(|t| matches!(t.get("status").and_then(Value::as_str), Some("running") | None));
    let options = history_fold::Options {
        summaries: true,
        open_turn: live.as_ref().map(|l| l.turn_id.clone()).unwrap_or_default(),
        held_rows: live.as_ref().map(|l| l.own_rows.clone()).unwrap_or_default(),
        busy: running || busy,
        show_commentary: mode == Mode::AlwaysVisible,
        whole_turn: conversation,
        read_into: true,
        ..history_fold::Options::default()
    };
    let mut history = history_fold::present(rows, view, &options);
    if conversation {
        for fold in &mut history.folds {
            fold.pieces.retain(|piece| match piece {
                Piece::Row(_) => true,
                Piece::Entry(entry) => entry.kind == Kind::Message && entry.phase != "commentary",
            });
        }
    }
    let projection = conversation.then(|| chat_view::project(rows, &HashSet::new()));
    let mut hide: HashSet<String> = history.hidden_rows.clone();
    if let Some(projection) = &projection {
        hide.extend(projection.hidden_rows.iter().cloned());
    }
    if let Some(live) = &live {
        hide.extend(live.absorbed_rows.iter().chain(&live.hidden_rows).cloned());
    }
    let fresh: Vec<&Message> = rows.iter().filter(|r| !hide.contains(&r.id)).collect();
    let mut order: Vec<String> = fresh.iter().map(|r| r.id.clone()).collect();
    // splice_history
    for fold in &history.folds {
        let position = |order: &Vec<String>, id: &str| order.iter().position(|o| o == id);
        let first_row = fold.pieces.iter().find_map(|p| if let Piece::Row(id) = p { position(&order, id) } else { None });
        let mut at = fold.prompt.as_deref().and_then(|id| position(&order, id)).map(|i| i + 1).or(first_row)
            .or_else(|| fold.next.as_deref().and_then(|id| position(&order, id))).unwrap_or(order.len());
        for piece in &fold.pieces {
            match piece {
                Piece::Row(id) => {
                    if let Some(index) = position(&order, id) {
                        at = index + 1;
                    }
                }
                Piece::Entry(entry) => {
                    order.insert(at, entry.key.clone());
                    at += 1;
                }
            }
        }
    }
    // splice_live
    if let Some(live) = &live {
        let taken: HashSet<&str> = live.entries.iter().map(|e| e.row.as_str()).filter(|r| !r.is_empty()).collect();
        order.retain(|id| !taken.contains(id.as_str()));
        let messages: Vec<&Message> = order.iter().map(|id| fresh.iter().find(|r| &r.id == id).copied().unwrap_or(&EMPTY)).collect();
        let sent = messages.iter().rposition(|r| !(r.pending || r.delivery_failed)).map_or(0, |i| i + 1);
        let anchors: Vec<usize> = live.anchors(&messages).into_iter().map(|at| at.min(sent)).collect();
        for (entry, at) in live.entries.iter().zip(anchors).rev() {
            let name = if entry.row.is_empty() { entry.key.clone() } else { entry.row.clone() };
            order.insert(at.min(order.len()), name);
        }
    }
    order
}

static EMPTY: LazyLock<Message> = LazyLock::new(Message::default);

/// How often the reply is drawn: its row, or its live item as a row.
fn replies(order: &[String]) -> usize {
    order.iter().filter(|name| *name == REPLY || *name == &format!("live:{REPLY_ITEM}")).count()
}

fn at(order: &[String], name: &str) -> Option<usize> {
    order.iter().position(|o| o == name)
}

/// Visibility: the reply is drawn once, as its own row (never twice, never
/// gone). A failure here is a reply that disappeared or doubled.
fn assert_visible(order: &[String], step: &str, mode: Mode, path: Path) {
    assert_eq!(replies(order), 1, "{step} ({mode:?}, {path:?}): the reply is drawn once: {order:?}");
    assert!(at(order, REPLY).is_some(), "{step} ({mode:?}, {path:?}): the reply is drawn as its own row: {order:?}");
}

/// Where the reply is against the joined message: above or below it.
fn reply_above_steer(order: &[String]) -> Option<bool> {
    Some(at(order, REPLY)? < at(order, STEER)?)
}

/// Place: below its prompt, above the next message. A failure here is a
/// reply in the wrong place, not a missing one.
fn order_problem(order: &[String], step: &str, mode: Mode, path: Path) -> Option<String> {
    let reply = at(order, REPLY)?;
    let mut problems = Vec::new();
    if at(order, PROMPT).is_some_and(|i| i > reply) {
        problems.push("above its prompt");
    }
    if at(order, NEXT).is_some_and(|i| i < reply) {
        problems.push("below the next message");
    }
    (!problems.is_empty()).then(|| format!("{step} ({mode:?}, {path:?}): the reply is {}: {order:?}", problems.join(", ")))
}

fn rows_of(conversation: &Conversation) -> Vec<Message> {
    conversation.rows().to_vec()
}

/// How the steps reached the desktop.
#[derive(Clone, Copy, Debug, PartialEq)]
enum Path {
    /// Each live event before the `/log` delta of the same moment.
    LiveFirst,
    /// Each `/log` delta before the live event.
    LogFirst,
    /// As LiveFirst; the closing delta lists the reply (71821) before the
    /// joined message's row (71818) and the next message (71824).
    ReplyFirst,
    /// As LiveFirst; the desktop restarts once the next message is
    /// delivered: the chat comes back from its transcript cache and the live
    /// overlay from `GET /live`, then the closing delta lands.
    CacheRestart,
    /// As LiveFirst; the live events of the turn's end are missed and a
    /// later `GET /live` (the next turn, no old reply) replaces the view.
    LiveReplaced,
    /// The joined message was sent from another device, and the desktop
    /// missed the JoinRead delta: the closing delta brings the joined row
    /// new, after the reply (a higher revision, written earlier) was updated
    /// in place.
    SentElsewhere,
    /// `/log` promotes the reply at JoinRead (final, conversation) but its
    /// live item stays commentary through the interrupted end.
    StaleLive,
}

const PATHS: [Path; 7] = [Path::LiveFirst, Path::LogFirst, Path::ReplyFirst, Path::CacheRestart, Path::LiveReplaced, Path::SentElsewhere, Path::StaleLive];

/// What a run of the incident found besides visibility, which it asserts.
#[derive(Default)]
struct Found {
    /// Order problems (`order_problem`) and places that changed.
    problems: Vec<String>,
    /// Whether the reply stood above the joined message, at each step.
    above: Vec<(String, Option<bool>)>,
    /// Steps between a live event and its `/log` delta: how often the reply
    /// was drawn there (recorded, not asserted).
    between: Vec<(String, usize)>,
}

fn incident(mode: Mode, path: Path) -> Found {
    let found = std::cell::RefCell::new(Found::default());
    let check = |c: &Conversation, v: Option<&LiveView>, busy: bool, step: &str| {
        let rows = rows_of(c);
        let order = drawn(&rows, v, mode, busy);
        // This device missed the JoinRead delta. Its durable copy is still
        // session-only until the closing delta delivers the promoted row.
        if path == Path::SentElsewhere && !rows.iter().any(|m| m.id == REPLY && m.phase == "final") {
            if mode == Mode::Conversation {
                assert_eq!(replies(&order), 0, "{step}: session-only row stays private");
            }
            return order;
        }
        assert_visible(&order, step, mode, path);
        let mut found = found.borrow_mut();
        found.above.push((step.to_owned(), reply_above_steer(&order)));
        order
    };
    let live_first = !matches!(path, Path::LogFirst | Path::StaleLive);
    let between = |c: &Conversation, v: &LiveView, step: &str| {
        let order = drawn(&rows_of(c), Some(v), mode, true);
        found.borrow_mut().between.push((step.to_owned(), replies(&order)));
    };

    // 0 s: the turn runs its tools.
    let mut conversation = Conversation::new();
    conversation.open_session("hazel-74f6");
    let mut tail = vec![prompt_row()];
    tail.extend((0..4).map(tool_row));
    conversation.apply_log(&page(tail, 71_790 * STEP), LoadKind::Tail);
    let mut view = LiveView::new();
    view.apply_snapshot(&running_snapshot());

    // 18.1 s: the owner's message, sent here, accepted and joined.
    if path != Path::SentElsewhere {
        conversation.add_optimistic(STEER_CLIENT, "Steered.");
        conversation.confirm_delivery(STEER_CLIENT);
    }

    // 23.3 s: the reply is written, undecided. It shows only where
    // commentary does (Always visible), never twice.
    let written = page(vec![reply_row(Reply::Undecided)], 71_804 * STEP);
    if live_first {
        view.apply_event(&reply_written());
        conversation.apply_log(&written, LoadKind::Delta);
    } else {
        conversation.apply_log(&written, LoadKind::Delta);
        view.apply_event(&reply_written());
    }
    let undecided = drawn(&rows_of(&conversation), Some(&view), mode, true);
    assert_eq!(replies(&undecided), usize::from(mode == Mode::AlwaysVisible), "undecided ({mode:?}, {path:?}): {undecided:?}");

    // 23.9 s: JoinRead. The joined row (71808), then the same reply (71808).
    let joined = page(vec![steer_row(false), reply_row(Reply::Decided)], 71_808 * STEP);
    match path {
        Path::LogFirst => {
            conversation.apply_log(&joined, LoadKind::Delta);
            check(&conversation, Some(&view), true, "JoinRead in /log, live not yet promoted");
            view.apply_event(&join_read());
        }
        Path::SentElsewhere => {
            view.apply_event(&join_read());
        }
        Path::StaleLive => {
            conversation.apply_log(&joined, LoadKind::Delta);
            check(&conversation, Some(&view), true, "JoinRead in /log, live never promoted");
            view.apply_event(&join_read_stale());
        }
        _ => {
            view.apply_event(&join_read());
            between(&conversation, &view, "JoinRead live, /log not yet");
            conversation.apply_log(&joined, LoadKind::Delta);
        }
    }
    check(&conversation, Some(&view), true, "decided");

    // 26.6 s: the owner sends the next message from this desktop.
    conversation.add_optimistic(NEXT_CLIENT, "Next.");
    check(&conversation, Some(&view), true, "next message pending");
    conversation.confirm_delivery(NEXT_CLIENT);
    check(&conversation, Some(&view), true, "next message delivered");
    if path == Path::CacheRestart {
        let cached = conversation.cache_snapshot();
        conversation = Conversation::new();
        conversation.open_session("hazel-74f6");
        assert!(conversation.restore_cache_snapshot(&cached), "the cache restores the chat");
        view = LiveView::new();
        view.apply_snapshot(&decided_snapshot());
        check(&conversation, Some(&view), true, "restarted from the cache");
    }

    // The turn ends interrupted and closes on the same reply; 2238 starts.
    let rows = match path {
        Path::ReplyFirst | Path::SentElsewhere => vec![reply_row(Reply::Closed), steer_row(true), next_row()],
        _ => vec![steer_row(true), reply_row(Reply::Closed), next_row()],
    };
    let closing = page(rows, 71_824 * STEP);
    match path {
        Path::LogFirst | Path::StaleLive => {
            conversation.apply_log(&closing, LoadKind::Delta);
            check(&conversation, Some(&view), true, "closed in /log, live running");
            view.apply_event(&interrupted());
            check(&conversation, Some(&view), true, "interrupted");
            view.apply_event(&next_started());
        }
        Path::LiveReplaced => {
            view.apply_snapshot(&next_snapshot("e1", 13));
            check(&conversation, Some(&view), true, "later /live, /log not yet closed");
            conversation.apply_log(&closing, LoadKind::Delta);
        }
        _ => {
            view.apply_event(&interrupted());
            check(&conversation, Some(&view), true, "interrupted, /log not yet closed");
            view.apply_event(&next_started());
            check(&conversation, Some(&view), true, "next turn started, /log not yet closed");
            conversation.apply_log(&closing, LoadKind::Delta);
        }
    }
    let settled = check(&conversation, Some(&view), true, "closed");
    found.borrow_mut().problems.extend(order_problem(&settled, "closed", mode, path));
    let same = |order: Vec<String>, step: &str| {
        if order != settled {
            found.borrow_mut().problems.push(format!("{step} ({mode:?}, {path:?}): drawn as {order:?}, was {settled:?}"));
        }
    };

    // The same delta again (a reconnect's catch-up) changes nothing.
    conversation.apply_log(&closing, LoadKind::Delta);
    same(check(&conversation, Some(&view), true, "delta again"), "delta again");
    // Reconnect: a new epoch's `GET /live` of the next turn.
    view.apply_snapshot(&next_snapshot("e2", 1));
    same(check(&conversation, Some(&view), true, "reconnected"), "reconnected");
    // The tail read again into the chat as held: the Host's order.
    let mut tail = vec![prompt_row()];
    tail.extend((0..4).map(tool_row));
    tail.extend([reply_row(Reply::Closed), steer_row(true), next_row()]);
    conversation.apply_log(&page(tail.clone(), 71_824 * STEP), LoadKind::Tail);
    let authoritative = check(&conversation, Some(&view), true, "tail read again");
    // Reload: a new chat from the tail, live items from `GET /live` only.
    let mut reloaded = Conversation::new();
    reloaded.open_session("hazel-74f6");
    reloaded.apply_log(&page(tail, 71_824 * STEP), LoadKind::Tail);
    let mut fresh_view = LiveView::new();
    fresh_view.apply_snapshot(&next_snapshot("e2", 1));
    let reopened = check(&reloaded, Some(&fresh_view), true, "reloaded");
    for (step, order) in [("tail read again", &authoritative), ("reloaded", &reopened)] {
        found.borrow_mut().problems.extend(order_problem(order, step, mode, path));
        if reply_above_steer(order) != Some(true) {
            found.borrow_mut().problems.push(format!("{step} ({mode:?}, {path:?}): /log tail places the reply before the joined row: {order:?}"));
        }
    }
    // Idle after the next turn, no live items at all.
    check(&reloaded, None, false, "reloaded, idle");
    found.into_inner()
}

/// Never gone, never doubled: every step, every view, every arrival order.
#[test]
fn a_decided_reply_stays_visible_once_when_the_next_message_stops_its_turn() {
    for mode in MODES {
        for path in PATHS {
            incident(mode, path);
        }
    }
}

/// A replay or reconnect keeps the settled order. A full read and reload
/// follow the Host's tail order, including the joined row.
#[test]
fn a_decided_reply_keeps_its_settled_place_and_follows_tail_order_on_reload() {
    let mut problems = Vec::new();
    let mut report = Vec::new();
    for mode in MODES {
        for path in PATHS {
            let found = incident(mode, path);
            problems.extend(found.problems);
            let sides: Vec<String> = found.above.iter().map(|(step, above)| format!("{step}={}", match above {
                Some(true) => "reply above joined",
                Some(false) => "joined above reply",
                None => "-",
            })).collect();
            let gaps: Vec<String> = found.between.iter().map(|(step, n)| format!("{step}: drawn {n}x")).collect();
            report.push(format!("{mode:?}/{path:?}: {} | {}", sides.join("; "), gaps.join("; ")));
        }
    }
    eprintln!("{}", report.join("\n"));
    assert!(problems.is_empty(), "place problems:\n{}\n\nreply against the joined message:\n{}", problems.join("\n"), report.join("\n"));
}

/// An early full final reply (final from its start, as a hub that does not
/// grow answers sends it) with the Host's row present: drawn once by its
/// row while the turn runs and after it ends.
#[test]
fn an_early_final_reply_with_its_row_present_is_drawn_once() {
    for mode in MODES {
        let mut conversation = Conversation::new();
        conversation.open_session("hazel-74f6");
        let mut tail = vec![prompt_row()];
        tail.extend((0..4).map(tool_row));
        tail.push(reply_row(Reply::Decided));
        conversation.apply_log(&page(tail, 71_808 * STEP), LoadKind::Tail);
        let mut items: Vec<Value> = (0..4).map(tool_item).collect();
        items.push(reply_item("final", 3));
        let snapshot = obj(json!({"conv": "c-hazel", "session": "hazel-74f6", "agent_id": "a", "epoch": "e1", "lseq": 10, "server_now_ms": t(REPLY_AFTER),
            "activity": {"state": "responding", "turn_id": F}, "turn": running_turn(false), "items": items}));
        let mut view = LiveView::new();
        view.apply_snapshot(&snapshot);
        assert_visible(&drawn(&rows_of(&conversation), Some(&view), mode, true), "early final, running", mode, Path::LiveFirst);
        let mut turn = running_turn(false);
        turn["status"] = json!("completed");
        turn["ended_at_ms"] = json!(t(ENDED_AFTER));
        view.apply_event(&event(11, vec![json!({"op": "turn", "turn": turn}), json!({"op": "status", "activity": {"state": "idle"}})]));
        assert_visible(&drawn(&rows_of(&conversation), Some(&view), mode, false), "early final, settled", mode, Path::LiveFirst);
    }
}

/// A reply streamed under a persisted `/log` live row (`kind` live, partial
/// text, the item's `row_id`), then its durable row: drawn once throughout.
#[test]
fn a_persisted_delta_then_its_durable_row_is_drawn_once() {
    for mode in MODES {
        let mut conversation = Conversation::new();
        conversation.open_session("hazel-74f6");
        let mut tail = vec![prompt_row()];
        tail.extend((0..4).map(tool_row));
        tail.push(json!({"id": REPLY, "role": "assistant", "kind": "live", "timestamp": stamp(t(REPLY_AFTER) - 400), "trace_id": F, "text": "The answer,", "origin": "user", "view": "session", "revision": 71_803 * STEP}));
        conversation.apply_log(&page(tail, 71_803 * STEP), LoadKind::Tail);
        let mut items: Vec<Value> = (0..4).map(tool_item).collect();
        let mut streaming = reply_item("final", 3);
        streaming["status"] = json!("running");
        items.push(streaming);
        let snapshot = obj(json!({"conv": "c-hazel", "session": "hazel-74f6", "agent_id": "a", "epoch": "e1", "lseq": 10, "server_now_ms": t(REPLY_AFTER),
            "activity": {"state": "responding", "turn_id": F}, "turn": running_turn(false), "items": items}));
        let mut view = LiveView::new();
        view.apply_snapshot(&snapshot);
        let drawn_streaming = drawn(&rows_of(&conversation), Some(&view), mode, true);
        if mode != Mode::Conversation {
            assert_eq!(replies(&drawn_streaming), 1, "streaming ({mode:?}): one row for the reply: {drawn_streaming:?}");
        }
        conversation.apply_log(&page(vec![reply_row(Reply::Decided)], 71_808 * STEP), LoadKind::Delta);
        assert_visible(&drawn(&rows_of(&conversation), Some(&view), mode, true), "persisted delta then durable", mode, Path::LiveFirst);
    }
}

/// What the Host keeps out stays out: a reply the Host has not decided
/// (its row commentary, view session) folds with the running turn's work
/// where commentary folds, and is not in the Conversation view, whatever
/// its live item says; a row the Host made final but left in the session
/// view stays out of the Conversation view.
#[test]
fn an_undecided_or_session_only_reply_stays_out_where_it_did() {
    for (row, item) in [(Reply::Undecided, "commentary"), (Reply::Undecided, "final")] {
        let mut conversation = Conversation::new();
        conversation.open_session("hazel-74f6");
        let mut tail = vec![prompt_row()];
        tail.extend((0..4).map(tool_row));
        tail.push(reply_row(row));
        conversation.apply_log(&page(tail, 71_804 * STEP), LoadKind::Tail);
        let mut items: Vec<Value> = (0..4).map(tool_item).collect();
        items.push(reply_item(item, 3));
        let snapshot = obj(json!({"conv": "c-hazel", "session": "hazel-74f6", "agent_id": "a", "epoch": "e1", "lseq": 10, "server_now_ms": t(REPLY_AFTER),
            "activity": {"state": "responding", "turn_id": F}, "turn": running_turn(false), "items": items}));
        let mut view = LiveView::new();
        view.apply_snapshot(&snapshot);
        let rows = rows_of(&conversation);
        let conversation_view = drawn(&rows, Some(&view), Mode::Conversation, true);
        assert_eq!(replies(&conversation_view), 0, "session-only ({item} item): not in the Conversation view: {conversation_view:?}");
        if item == "commentary" {
            let grouped = drawn(&rows, Some(&view), Mode::Grouped, true);
            assert_eq!(replies(&grouped), 0, "undecided commentary folds with the work (Grouped): {grouped:?}");
            let always = drawn(&rows, Some(&view), Mode::AlwaysVisible, true);
            assert_eq!(replies(&always), 1, "Always visible keeps commentary: {always:?}");
        }
    }
    // Final but left in the session view: still out of the Conversation view.
    let mut conversation = Conversation::new();
    conversation.open_session("hazel-74f6");
    let mut tail = vec![prompt_row()];
    tail.extend((0..4).map(tool_row));
    let mut session_final = reply_row(Reply::Decided);
    session_final["view"] = json!("session");
    session_final.as_object_mut().unwrap().remove("session_rows");
    tail.push(session_final);
    conversation.apply_log(&page(tail, 71_808 * STEP), LoadKind::Tail);
    let mut view = LiveView::new();
    view.apply_snapshot(&running_snapshot());
    view.apply_event(&reply_written());
    let order = drawn(&rows_of(&conversation), Some(&view), Mode::Conversation, true);
    assert_eq!(replies(&order), 0, "a final row in the session view stays out of the Conversation view: {order:?}");
}

/// Two live messages carry the same anchored row: which one stands for it
/// is the items' own choice (the first live final, else the first), and the
/// Host's answer row only decides that the chosen one is not folded away.
#[test]
fn the_items_choose_which_message_stands_for_an_anchored_row() {
    let rows: Vec<Message> = {
        let mut conversation = Conversation::new();
        conversation.open_session("hazel-74f6");
        let mut tail = vec![prompt_row()];
        tail.extend((0..4).map(tool_row));
        tail.push(reply_row(Reply::Decided));
        conversation.apply_log(&page(tail, 71_808 * STEP), LoadKind::Tail);
        rows_of(&conversation)
    };
    let message = |id: &str, phase: &str, ordinal: i64, after: i64| {
        json!({"id": id, "kind": "message", "rev": 1, "turn_id": F, "status": "completed", "ordinal": ordinal, "phase": phase,
            "started_at_ms": t(after) - 100, "ended_at_ms": t(after), "text": REPLY_WORDS, "row_id": REPLY})
    };
    for (second, chosen) in [("final", "cl:msg_b"), ("commentary", "cl:msg_a")] {
        let mut items: Vec<Value> = (0..4).map(tool_item).collect();
        items.push(message("cl:msg_a", "commentary", 5, REPLY_AFTER - 2_000));
        items.push(message("cl:msg_b", second, 6, REPLY_AFTER));
        let snapshot = obj(json!({"conv": "c-hazel", "session": "hazel-74f6", "agent_id": "a", "epoch": "e1", "lseq": 10, "server_now_ms": t(REPLY_AFTER),
            "activity": {"state": "responding", "turn_id": F}, "turn": running_turn(false), "items": items}));
        let mut view = LiveView::new();
        view.apply_snapshot(&snapshot);
        for fold_running in [false, true] {
            let presented = live_present::present(&view, &rows, &Options { fold_running, ..Options::default() });
            let standing: Vec<(&str, &str)> = presented.entries.iter().filter(|e| e.row == REPLY).map(|e| (e.key.as_str(), e.phase.as_str())).collect();
            assert_eq!(standing, [(format!("live:{chosen}").as_str(), "final")], "second item {second}, fold_running {fold_running}: {:?}", presented.entries);
            assert!(!presented.hidden_rows.contains(&REPLY.to_owned()), "the answer row is not hidden ({second}, {fold_running})");
        }
    }
}
