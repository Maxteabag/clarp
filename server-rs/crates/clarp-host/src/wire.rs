//! From what the Host knows to what the contract says: the actor's types in the apps' words.
//!
//! The shapes here are the Python Host's, field for field, as the conformance run recorded them
//! (`server-rs/conformance/golden`). Where the Python Host sends a field with an empty value, so
//! does this Host: an app's decoder may count on the field being there. A field this Host has
//! nothing to say about is sent as the Python Host sends it when it has nothing to say.

use clarp_actor::conversation::{Page, Versioned};
use clarp_actor::{LiveStatus, Status};
use clarp_kernel::conversation::{Entry, ToolStatus, TurnEnd};
use clarp_kernel::turn::{Activity, Origin, Outcome};
use clarp_protocol::events::AgentStateKind;
use serde_json::{Value, json};

use crate::fleet::AgentRecord;

/// A time as the contract writes it: `2026-09-02T05:41:00.123Z`.
pub fn iso(ms: i64) -> String {
    let days = ms.div_euclid(86_400_000);
    let in_day = ms.rem_euclid(86_400_000);
    // Days since 1970 to a date of the civil calendar (Howard Hinnant's algorithm).
    let z = days + 719_468;
    let era = z.div_euclid(146_097);
    let day_of_era = z.rem_euclid(146_097);
    let year_of_era =
        (day_of_era - day_of_era / 1_460 + day_of_era / 36_524 - day_of_era / 146_096) / 365;
    let day_of_year = day_of_era - (365 * year_of_era + year_of_era / 4 - year_of_era / 100);
    let month_index = (5 * day_of_year + 2) / 153;
    let day = day_of_year - (153 * month_index + 2) / 5 + 1;
    let month = if month_index < 10 {
        month_index + 3
    } else {
        month_index - 9
    };
    let year = year_of_era + era * 400 + i64::from(month <= 2);
    format!(
        "{year:04}-{month:02}-{day:02}T{:02}:{:02}:{:02}.{:03}Z",
        in_day / 3_600_000,
        in_day / 60_000 % 60,
        in_day / 1_000 % 60,
        in_day % 1_000
    )
}

/// The state an app shows for an agent.
pub fn state_kind(status: &Status) -> AgentStateKind {
    match (&status.live, &status.last) {
        (Some(live), _) => match &live.activity {
            Activity::Thinking | Activity::Responding => AgentStateKind::Thinking,
            Activity::Tool { .. } => AgentStateKind::Tool,
            Activity::Waiting => AgentStateKind::Waiting,
            Activity::Compacting => AgentStateKind::Compacting,
        },
        // An agent that has never run a turn. The Python Host calls it `spawned`.
        (None, None) => AgentStateKind::Spawned,
        (None, Some(last)) => match &last.outcome {
            Outcome::Completed => AgentStateKind::Done,
            // A turn that was stopped, cut off or failed did not finish: the contract's word
            // for all three is "interrupted".
            Outcome::Stopped { .. } | Outcome::Interrupted { .. } | Outcome::Failed { .. } => {
                AgentStateKind::Interrupted
            }
            Outcome::Withdrawn { .. } => AgentStateKind::Idle,
        },
    }
}

/// A state's name on the wire.
pub fn state_name(kind: &AgentStateKind) -> String {
    serde_json::to_value(kind)
        .ok()
        .and_then(|value| value.as_str().map(str::to_string))
        .unwrap_or_else(|| "idle".to_string())
}

/// When the state an app shows began.
fn state_since(status: &Status) -> Option<i64> {
    match (&status.live, &status.last) {
        (Some(live), _) => Some(live.started_at_ms),
        (None, Some(last)) => Some(last.ended_at_ms),
        (None, None) => None,
    }
}

/// The id a client gave a message, from the id the conversation files it under.
pub fn client_id(message_id: &str) -> &str {
    message_id.strip_prefix("u-").unwrap_or(message_id)
}

// ---- events ---------------------------------------------------------------------------------

/// The Host says which version it is: a client that sees another one reloads.
pub fn server_version(version: &str, now_ms: i64) -> Value {
    json!({ "type": "server-version", "version": version, "ts": now_ms })
}

/// The conversation changed. A wake-up, not the data.
pub fn transcript_updated(record: &AgentRecord) -> Value {
    json!({ "type": "transcript-updated", "agent_id": record.agent_id, "session": record.session })
}

/// A message was handed to the agent and its turn began: the answer to a send, for every
/// client that did not send it.
pub fn dispatched(
    record: &AgentRecord,
    live: &LiveStatus,
    from_queue: bool,
    waiting: usize,
) -> Value {
    json!({
        "type": "agent-state",
        "agent_id": record.agent_id,
        "session": record.session,
        "kind": "thinking",
        "client_msg_id": client_id(&live.message_id),
        "trace_id": live.trace_id.clone().unwrap_or_default(),
        "queue_started": from_queue,
        "queue_remaining": waiting,
    })
}

/// What an ended turn's state says about how it ended.
fn ending_detail(outcome: &Outcome) -> Value {
    match outcome {
        Outcome::Stopped { by } => json!({
            "message": "Turn stopped",
            "source": "user_stop",
            "stop_actor": by,
            "stop_actor_verified": false,
            "stop_reason": "",
        }),
        Outcome::Failed { reason } | Outcome::Interrupted { reason } => json!({
            "message": reason,
            "source": "turn_failed",
        }),
        Outcome::Completed | Outcome::Withdrawn { .. } => json!({}),
    }
}

/// An agent's state, as the one that watches all states tells it.
pub fn state(record: &AgentRecord, status: &Status, backend: &str, now_ms: i64) -> Value {
    let kind = state_kind(status);
    let detail = match (&status.live, &status.last) {
        (Some(live), _) => json!({
            "backend_session_id": status.backend_session.clone().unwrap_or_default(),
            "dispatch": backend,
            "origin": "user",
            "source": "pwa",
            "trace_id": live.trace_id.clone().unwrap_or_default(),
        }),
        (None, Some(last)) if last.outcome == Outcome::Completed => json!({
            "dispatch": backend,
            "duration_ms": last.ended_at_ms - last.started_at_ms.unwrap_or(last.ended_at_ms),
            "origin": "user",
        }),
        (None, Some(last)) => ending_detail(&last.outcome),
        (None, None) => json!({}),
    };
    json!({
        "type": "agent-state",
        "agent_id": record.agent_id,
        "session": record.session,
        "persona": record.persona,
        "kind": state_name(&kind),
        "ts": state_since(status).unwrap_or(now_ms),
        "detail": detail,
        "status_text": "",
    })
}

/// The state a Stop leaves, as the route that took the Stop tells it at once.
pub fn stopped(record: &AgentRecord, status: &Status, now_ms: i64) -> Option<Value> {
    let last = status.last.as_ref()?;
    let Outcome::Stopped { .. } = last.outcome else {
        return None;
    };
    Some(json!({
        "type": "agent-state",
        "agent_id": record.agent_id,
        "session": record.session,
        "persona": record.persona,
        "kind": "interrupted",
        "ts": state_since(status).unwrap_or(now_ms),
        "detail": ending_detail(&last.outcome),
    }))
}

/// What an agent is doing, as a line a client can show. The same object is the snapshot's
/// `activity` and, with its number, the `agent-activity` event.
pub fn activity(record: &AgentRecord, status: &Status, now_ms: i64) -> Value {
    let kind = state_kind(status);
    let running = status.live.as_ref();
    let (action, phase, state, outcome, summary) = match (&kind, status.last.as_ref()) {
        (AgentStateKind::Thinking, _) => {
            ("thinking", "thinking", "thinking", "running", "Thinking")
        }
        (AgentStateKind::Tool, _) => ("tool", "tool", "tool", "running", "Using a tool"),
        (AgentStateKind::Compacting, _) => (
            "compacting",
            "compacting",
            "compacting",
            "running",
            "Compacting",
        ),
        (AgentStateKind::Waiting, _) => ("waiting", "waiting", "waiting", "running", "Waiting"),
        (AgentStateKind::Done, _) => ("done", "done", "idle", "ok", "Done"),
        (AgentStateKind::Interrupted, Some(last)) => match &last.outcome {
            Outcome::Stopped { .. } => (
                "needs attention",
                "interrupted",
                "interrupted",
                "error",
                "Turn stopped",
            ),
            _ => (
                "needs attention",
                "interrupted",
                "interrupted",
                "error",
                "Turn failed",
            ),
        },
        _ => ("idle", "idle", "idle", "ok", "Idle"),
    };
    let tool = match running.map(|live| &live.activity) {
        Some(Activity::Tool { name }) => name.as_str(),
        _ => "",
    };
    let mut activity = json!({
        "type": "agent-activity",
        "agent_id": record.agent_id,
        "session": record.session,
        "persona": record.persona,
        "kind": state_name(&kind),
        "phase": phase,
        "action": action,
        "state": state,
        "status": outcome,
        "summary": summary,
        "tool": tool,
        "file_path": "",
        "call_id": null,
        "started_at_ms": null,
        "turn_started_ms": running.map(|live| live.started_at_ms),
        "ts": state_since(status).unwrap_or(now_ms),
    });
    if let Some(Outcome::Stopped { by }) = status.last.as_ref().map(|last| &last.outcome)
        && running.is_none()
    {
        activity["stop_actor"] = json!(by);
        activity["stop_actor_verified"] = json!(false);
        activity["stop_reason"] = json!("");
    }
    activity
}

pub fn queue_updated(record: &AgentRecord, status: &Status) -> Value {
    json!({
        "type": "queue-updated",
        "agent_id": record.agent_id,
        "session": record.session,
        "queue_depth": status.waiting.len(),
        "queue_paused": status.paused,
        "queue_started": false,
        "queue_revision": status.queue_revision,
    })
}

/// A finished turn the user should know about: what an app badges and marks unread from.
pub fn user_notification(
    record: &AgentRecord,
    notification_id: &str,
    cause_message_id: &str,
    reply: &Versioned,
    done_ms: i64,
) -> Value {
    let preview = match &reply.entry {
        Entry::Reply(reply) => reply.text.as_str(),
        _ => "",
    };
    json!({
        "type": "user-notification",
        "notification_id": notification_id,
        "agent_id": record.agent_id,
        "session": record.session,
        "persona": record.persona,
        "origin": "user",
        "reason": "text-reply",
        "cause_message_id": cause_message_id,
        "source_message_id": reply.entry.id(),
        "preview": preview,
        "done_ts": done_ms,
        "badge": true,
        "unread": true,
        "push": true,
        "muted": false,
    })
}

pub fn roster(kind: &str, session: &str) -> Value {
    json!({ "type": "agent-roster", "kind": kind, "session": session })
}

pub fn focus(record: &AgentRecord) -> Value {
    json!({ "type": "agent-focus", "session": record.session, "agent_id": record.agent_id })
}

// ---- a conversation --------------------------------------------------------------------------

/// How a turn ended, on the row that closed it.
fn turn_end(end: &TurnEnd) -> Value {
    json!({
        "turn_id": end.turn.to_string(),
        "status": end.status,
        "started_at_ms": end.started_at_ms,
        "ended_at_ms": end.ended_at_ms,
        "worked_ms": end.started_at_ms.map(|started| end.ended_at_ms - started),
        "tool_count": end.tool_count,
    })
}

/// One thing of a conversation as the contract's "turn": a row, with every field of a row.
pub fn row(version: &Versioned) -> Value {
    let mut row = json!({
        "id": version.entry.id(),
        "role": "assistant",
        "text": "",
        "revision": version.revision,
        "timestamp": iso(version.ts_ms),
        "kind": null,
        "phase": null,
        "origin": "user",
        "trace_id": "",
        "tool_name": null,
        "tools": [],
        "display_cells": [],
        "automated": false,
        "automation_kind": "",
        "sender_agent_id": "",
        "sender_name": "",
        "sender_session": "",
        "reply_to_agent_id": "",
        "reply_to_name": "",
        "reply_to_session": "",
    });
    match &version.entry {
        Entry::Message(message) => {
            row["role"] = json!("user");
            row["text"] = json!(message.text);
            row["trace_id"] = json!(message.trace_id.clone().unwrap_or_default());
            row["origin"] = json!(match &message.origin {
                Origin::User => "user",
                Origin::Agent { .. } => "agent",
                Origin::Trigger { name } => name.as_str(),
            });
            if let Some(sender) = &message.sender {
                row["sender_agent_id"] = json!(sender.agent_id);
                row["sender_name"] = json!(sender.name);
                row["sender_session"] = json!(sender.session);
            }
        }
        Entry::Reply(reply) => row["text"] = json!(reply.text),
        Entry::Tool(call) => {
            let status = match call.status {
                ToolStatus::Running => "running",
                ToolStatus::Ok => "ok",
                ToolStatus::Error => "error",
            };
            row["kind"] = json!("tool");
            row["tool_name"] = json!(call.name);
            row["tools"] = json!([{
                "id": call.id,
                "name": call.name,
                "input": call.input,
                "status": status,
                "output": call.output,
                "started_at_ms": call.started_at_ms,
                "ended_at_ms": call.ended_at_ms,
            }]);
        }
        Entry::Notice(notice) => {
            row["kind"] = json!("notice");
            row["text"] = json!(notice.text);
        }
        // Never a row of its own: it is told on the row it closes.
        Entry::TurnEnd(_) => {}
    }
    if let Some(end) = &version.turn {
        row["turn"] = turn_end(end);
        // The reply that closes a turn that ran to its end is the agent's final word in it.
        if end.status == "completed" && matches!(version.entry, Entry::Reply(_)) {
            row["phase"] = json!("final");
        }
    }
    row
}

/// The name of an agent's conversation: the tool's own session, which for a tool that can be
/// told is the name the Host chose. An agent that has not been written to has none yet.
pub fn conversation_id(record: &AgentRecord, status: &Status) -> String {
    match &status.backend_session {
        Some(session) => session.clone(),
        None if status.head_revision > 0 => record.conversation_id.clone(),
        None => String::new(),
    }
}

/// A page of a conversation as `GET /log` answers it.
pub fn log_response(
    record: &AgentRecord,
    status: &Status,
    page: &Page,
    replace_required: bool,
) -> Value {
    json!({
        "conversation_id": conversation_id(record, status),
        "turns": page.entries.iter().map(row).collect::<Vec<_>>(),
        "latest_revision": page.latest_revision,
        "has_more": page.has_more,
        "missing": page.head_revision == 0,
        "replace_required": replace_required,
        "latest_ts": page.entries.last().map(|last| iso(last.ts_ms)).unwrap_or_default(),
        "cwd": record.cwd,
        "file": null,
        "includes_automated": true,
    })
}

/// The answer to `GET /log` for a name the Host has no agent under.
pub fn no_conversation(cwd: &str) -> Value {
    json!({
        "conversation_id": "",
        "turns": [],
        "latest_revision": 0,
        "has_more": false,
        "missing": true,
        "latest_ts": "",
        "cwd": cwd,
        "includes_automated": true,
    })
}

/// What a row says in a list of chats.
fn preview(version: &Versioned) -> String {
    match &version.entry {
        Entry::Message(message) => format!("You: {}", message.text),
        Entry::Reply(reply) => reply.text.clone(),
        Entry::Notice(notice) => notice.text.clone(),
        Entry::Tool(call) => call.name.clone(),
        Entry::TurnEnd(_) => String::new(),
    }
}

/// An agent's row in the snapshot, with every field of a row.
pub fn summary(
    record: &AgentRecord,
    status: &Status,
    backend: &str,
    newest: Option<&Versioned>,
    focused: bool,
    now_ms: i64,
) -> Value {
    let kind = state_kind(status);
    let live = status.live.as_ref();
    let since = state_since(status);
    let last_message = newest.map(preview).unwrap_or_default();
    let conversation = conversation_id(record, status);
    let live_state = if live.is_some() {
        state_name(&kind)
    } else {
        "idle".to_string()
    };
    json!({
        "agent_id": record.agent_id,
        "session": record.session,
        "persona": record.persona,
        "backend": backend,
        "cwd": record.cwd,
        "model": record.model,
        "effort": record.effort,
        "voice_id": "",
        "avatar_symbol": "",
        "avatar_url": "",
        "avatar_generation_status": null,
        "model_avatar_url": "",
        "mcp_servers": [],
        "heartbeat_enabled": false,
        "dreaming_enabled": false,
        "muted": false,
        "quiet": false,
        "archived_at": null,
        "role": "agent",
        "is_janitor": false,
        "parent_agent_id": null,
        "helper_state": null,
        "child_count": 0,
        "running_children": 0,
        "team_ids": [],
        "schedules": [],
        "goal": null,
        "call_mode": "classic",
        "interaction_capabilities": {
            "can_chat": true,
            "can_inspect": true,
            "can_restart": true,
            "can_voice_target": true,
        },

        "backend_session_id": status.backend_session.clone().unwrap_or_default(),
        "conversation_id": conversation,
        "head_revision": status.head_revision,
        "last_message_id": newest.map(|row| row.entry.id()).unwrap_or_default(),
        "last_message": last_message,
        "last_completed_message": last_message,

        "alive": true,
        "busy": live.is_some(),
        "focused": focused,
        "latest_state": state_name(&kind),
        "latest_state_ts": since,
        "turn_started_at": live.map_or(0, |live| live.started_at_ms),
        "last_activity": since.map(|ms| ms / 1_000),
        "last_turn_end": status.last.as_ref().map(|last| last.ended_at_ms / 1_000),
        "status_text": null,
        "activity": activity(record, status, now_ms),
        "live_activity": {
            "state": live_state,
            "since_ms": since,
            "turn_id": live.map(|live| live.turn.0.to_string()),
            "turn_started_ms": live.map(|live| live.started_at_ms),
            "running_tools": u8::from(kind == AgentStateKind::Tool),
            "tool": null,
            "headline": null,
            "item_id": null,
        },
        "compacting": kind == AgentStateKind::Compacting,
        "context_tokens": null,
        "context_window": null,
        "queued_turn_count": status.waiting.len(),
        "queued_turn_revision": status.queue_revision,
        "queue_paused": status.paused,
        "backend_quota": null,
        "background_jobs": { "count": 0, "sub_agents": 0 },
    })
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use clarp_kernel::conversation::{Message, Reply, ToolCall};
    use clarp_kernel::turn::{Settled, TurnId};
    use clarp_protocol::events::{Envelope, Event};
    use clarp_protocol::log::LogResponse;
    use clarp_protocol::snapshot::AgentSummary;

    use super::*;

    #[test]
    fn a_time_is_written_as_the_contract_writes_it() {
        assert_eq!(iso(0), "1970-01-01T00:00:00.000Z");
        assert_eq!(iso(1_756_791_660_123), "2025-09-02T05:41:00.123Z");
        assert_eq!(iso(951_782_400_000), "2000-02-29T00:00:00.000Z");
        assert_eq!(iso(1_794_700_800_000), "2026-11-15T00:00:00.000Z");
        assert_eq!(iso(-1), "1969-12-31T23:59:59.999Z");
    }

    fn status(live: Option<Activity>, last: Option<Outcome>) -> Status {
        Status {
            live: live.map(|activity| LiveStatus {
                turn: TurnId(2),
                message_id: "u-m2".to_string(),
                trace_id: Some("trace-2".to_string()),
                starting: false,
                activity,
                started_at_ms: 5_000,
                tool_count: 1,
                stopping: false,
            }),
            last: last.map(|outcome| Settled {
                id: TurnId(1),
                message_seq: 1,
                origin: Origin::User,
                outcome,
                accepted_at_ms: 1_000,
                started_at_ms: Some(1_100),
                ended_at_ms: 4_000,
                tool_count: 0,
            }),
            waiting: vec![TurnId(3)],
            paused: true,
            queue_revision: 7,
            head_revision: 42,
            backend_session: Some("sess-1".to_string()),
        }
    }

    #[test]
    fn an_agents_state_is_named_as_the_apps_know_it() {
        let name = |live, last| state_name(&state_kind(&status(live, last)));
        assert_eq!(name(None, None), "spawned");
        assert_eq!(name(Some(Activity::Thinking), None), "thinking");
        assert_eq!(
            name(Some(Activity::Responding), Some(Outcome::Completed)),
            "thinking"
        );
        assert_eq!(
            name(
                Some(Activity::Tool {
                    name: "Bash".to_string()
                }),
                None
            ),
            "tool"
        );
        assert_eq!(name(Some(Activity::Compacting), None), "compacting");
        assert_eq!(name(None, Some(Outcome::Completed)), "done");
        assert_eq!(
            name(
                None,
                Some(Outcome::Stopped {
                    by: "user".to_string()
                })
            ),
            "interrupted"
        );
        assert!(state_kind(&status(Some(Activity::Thinking), None)).is_busy());
        assert!(!state_kind(&status(None, Some(Outcome::Completed))).is_busy());
    }

    fn record() -> AgentRecord {
        AgentRecord {
            agent_id: "a1".to_string(),
            session: "maximus".to_string(),
            persona: "Maximus".to_string(),
            backend: "claude".to_string(),
            cwd: "/work".to_string(),
            model: String::new(),
            effort: String::new(),
            conversation_id: "conv-1".to_string(),
            created_at_ms: 1,
            deleted_at_ms: None,
        }
    }

    fn version(entry: Entry, revision: u64, turn: Option<TurnEnd>) -> Versioned {
        Versioned {
            entry,
            revision,
            ts_ms: 0,
            live: false,
            turn_number: Some(1),
            turn,
        }
    }

    fn page() -> Page {
        let end = TurnEnd {
            id: "t-1".to_string(),
            turn: 1,
            closes: "r-1".to_string(),
            status: "completed".to_string(),
            started_at_ms: Some(10),
            ended_at_ms: 40,
            tool_count: 1,
        };
        Page {
            entries: vec![
                version(
                    Entry::Message(Message {
                        id: "u-1".to_string(),
                        text: "go".to_string(),
                        origin: Origin::User,
                        trace_id: Some("t-1".to_string()),
                        sender: None,
                        client: None,
                    }),
                    1 << 20,
                    None,
                ),
                version(
                    Entry::Tool(ToolCall {
                        id: "toolu_1".to_string(),
                        turn: 1,
                        name: "Bash".to_string(),
                        input: json!({"command": "ls"}),
                        status: ToolStatus::Ok,
                        output: Some("a.txt".to_string()),
                        started_at_ms: 10,
                        ended_at_ms: Some(20),
                    }),
                    5 << 20,
                    None,
                ),
                version(
                    Entry::Reply(Reply {
                        id: "r-1".to_string(),
                        turn: 1,
                        text: "One file.".to_string(),
                    }),
                    7 << 20,
                    Some(end),
                ),
            ],
            latest_revision: 7 << 20,
            has_more: true,
            head_revision: 7 << 20,
        }
    }

    #[test]
    fn a_page_becomes_the_answer_to_log_with_every_field_of_a_row() {
        let answer = log_response(
            &record(),
            &status(None, Some(Outcome::Completed)),
            &page(),
            false,
        );
        assert_eq!(answer["conversation_id"], "sess-1");
        assert_eq!(answer["latest_revision"], 7 << 20);
        assert_eq!(
            (&answer["has_more"], &answer["missing"]),
            (&json!(true), &json!(false))
        );
        assert!(answer["file"].is_null() && answer.get("file").is_some());
        let turns = answer["turns"].as_array().unwrap();
        let said: Vec<(&str, &str, &str)> = turns
            .iter()
            .map(|turn| {
                (
                    turn["id"].as_str().unwrap(),
                    turn["role"].as_str().unwrap(),
                    turn["text"].as_str().unwrap(),
                )
            })
            .collect();
        assert_eq!(
            said,
            vec![
                ("u-1", "user", "go"),
                ("toolu_1", "assistant", ""),
                ("r-1", "assistant", "One file.")
            ]
        );
        // Every row has the same fields, whatever it is: a decoder can count on them.
        let fields = |turn: &Value| {
            let mut keys: Vec<String> = turn.as_object().unwrap().keys().cloned().collect();
            keys.retain(|key| key != "turn");
            keys
        };
        assert_eq!(fields(&turns[0]), fields(&turns[1]));
        assert_eq!(fields(&turns[0]), fields(&turns[2]));
        assert_eq!(fields(&turns[0]).len(), 20);
        assert_eq!(turns[0]["trace_id"], "t-1");
        assert_eq!(turns[0]["timestamp"], "1970-01-01T00:00:00.000Z");
        assert_eq!(turns[1]["tools"][0]["status"], "ok");
        // The row that closed the turn says how it ended, and is the agent's final word.
        assert!(turns[0].get("turn").is_none() && turns[1].get("turn").is_none());
        assert_eq!(turns[2]["phase"], "final");
        assert_eq!(
            turns[2]["turn"],
            json!({"turn_id": "1", "status": "completed", "started_at_ms": 10, "ended_at_ms": 40, "worked_ms": 30, "tool_count": 1})
        );
        // And it is the contract's shape: it reads as the type the apps are generated from.
        let typed: LogResponse = serde_json::from_value(answer).unwrap();
        assert_eq!(typed.turns.len(), 3);

        let nothing = no_conversation("/home/me");
        assert_eq!(
            (&nothing["missing"], &nothing["conversation_id"]),
            (&json!(true), &json!(""))
        );
        assert!(nothing.get("file").is_none());
    }

    #[test]
    fn a_conversation_has_no_name_until_it_has_a_message() {
        let mut fresh = status(None, None);
        fresh.backend_session = None;
        fresh.head_revision = 0;
        assert_eq!(conversation_id(&record(), &fresh), "");
        fresh.head_revision = 5;
        assert_eq!(
            conversation_id(&record(), &fresh),
            "conv-1",
            "the name the Host chose"
        );
        assert_eq!(
            conversation_id(&record(), &status(None, None)),
            "sess-1",
            "the tool's own word for it"
        );
    }

    #[test]
    fn a_status_becomes_a_snapshot_row_with_every_field() {
        let busy = status(
            Some(Activity::Tool {
                name: "Bash".to_string(),
            }),
            Some(Outcome::Completed),
        );
        let newest = version(
            Entry::Message(Message {
                id: "u-9".to_string(),
                text: "newest".to_string(),
                origin: Origin::User,
                trace_id: None,
                sender: None,
                client: None,
            }),
            42,
            None,
        );
        let row = summary(&record(), &busy, "claude", Some(&newest), true, 9_000);
        assert_eq!(row["latest_state"], "tool");
        assert_eq!(
            (&row["busy"], &row["focused"]),
            (&json!(true), &json!(true))
        );
        assert_eq!(
            (
                &row["head_revision"],
                &row["queued_turn_count"],
                &row["queued_turn_revision"],
                &row["queue_paused"]
            ),
            (&json!(42), &json!(1), &json!(7), &json!(true))
        );
        assert_eq!(row["conversation_id"], "sess-1");
        assert_eq!(
            (&row["last_message"], &row["last_message_id"]),
            (&json!("You: newest"), &json!("u-9"))
        );
        assert_eq!(row["activity"]["tool"], "Bash");
        assert_eq!(row["turn_started_at"], 5_000);
        assert_eq!(
            row.as_object().unwrap().len(),
            54,
            "the fields of the Python Host's row"
        );
        let typed: AgentSummary = serde_json::from_value(row).unwrap();
        assert_eq!(typed.session, "maximus");
    }

    #[test]
    fn events_have_the_shapes_the_apps_read() {
        let thinking = status(Some(Activity::Thinking), None);
        let live = thinking.live.clone().unwrap();
        let sent = dispatched(&record(), &live, false, 1);
        assert_eq!(
            sent,
            json!({"type": "agent-state", "agent_id": "a1", "session": "maximus", "kind": "thinking",
                   "client_msg_id": "m2", "trace_id": "trace-2", "queue_started": false, "queue_remaining": 1})
        );
        let told = state(&record(), &thinking, "claude", 9_000);
        assert_eq!(
            (&told["kind"], &told["status_text"]),
            (&json!("thinking"), &json!(""))
        );
        assert_eq!(
            told["detail"],
            json!({"backend_session_id": "sess-1", "dispatch": "claude", "origin": "user", "source": "pwa", "trace_id": "trace-2"})
        );
        let doing = activity(&record(), &thinking, 9_000);
        assert_eq!(
            (
                &doing["action"],
                &doing["state"],
                &doing["status"],
                &doing["summary"],
                &doing["turn_started_ms"]
            ),
            (
                &json!("thinking"),
                &json!("thinking"),
                &json!("running"),
                &json!("Thinking"),
                &json!(5_000)
            )
        );
        assert!(doing["call_id"].is_null() && doing.get("stop_actor").is_none());

        let done = status(None, Some(Outcome::Completed));
        assert_eq!(
            state(&record(), &done, "claude", 9_000)["detail"]["duration_ms"],
            2_900
        );
        assert_eq!(activity(&record(), &done, 9_000)["summary"], "Done");
        assert!(stopped(&record(), &done, 9_000).is_none());

        let halted = status(
            None,
            Some(Outcome::Stopped {
                by: "user".to_string(),
            }),
        );
        let at_once = stopped(&record(), &halted, 9_000).unwrap();
        assert_eq!(at_once["kind"], "interrupted");
        assert_eq!(at_once["detail"]["source"], "user_stop");
        assert!(
            at_once.get("status_text").is_none(),
            "the route's word has no status text"
        );
        let after = activity(&record(), &halted, 9_000);
        assert_eq!(
            (
                &after["action"],
                &after["status"],
                &after["summary"],
                &after["stop_actor"]
            ),
            (
                &json!("needs attention"),
                &json!("error"),
                &json!("Turn stopped"),
                &json!("user")
            )
        );

        let queue = queue_updated(&record(), &thinking);
        assert_eq!(
            (
                &queue["queue_depth"],
                &queue["queue_paused"],
                &queue["queue_revision"]
            ),
            (&json!(1), &json!(true), &json!(7))
        );
        // Each is an event of the contract: it reads as the type the apps are generated from.
        for (event, is) in [
            (sent, "agent-state"),
            (doing, "agent-activity"),
            (queue, "queue-updated"),
        ] {
            let mut numbered = event;
            numbered["event_id"] = json!(5);
            let typed: Envelope = serde_json::from_value(numbered).unwrap();
            assert_eq!(typed.event_id, Some(5));
            let name = match typed.event {
                Event::AgentState(_) => "agent-state",
                Event::AgentActivity(_) => "agent-activity",
                Event::QueueUpdated(_) => "queue-updated",
                _ => "other",
            };
            assert_eq!(name, is);
        }
    }

    #[test]
    fn a_finished_turn_is_told_to_the_user() {
        let reply = version(
            Entry::Reply(Reply {
                id: "r-1".to_string(),
                turn: 1,
                text: "All done.".to_string(),
            }),
            9,
            None,
        );
        let told = user_notification(&record(), "pn-abc", "u-1", &reply, 4_000);
        assert_eq!(
            (
                &told["type"],
                &told["preview"],
                &told["source_message_id"],
                &told["cause_message_id"]
            ),
            (
                &json!("user-notification"),
                &json!("All done."),
                &json!("r-1"),
                &json!("u-1")
            )
        );
        assert_eq!(
            (&told["badge"], &told["unread"], &told["push"]),
            (&json!(true), &json!(true), &json!(true))
        );
        assert_eq!(told["reason"], "text-reply");
    }
}
