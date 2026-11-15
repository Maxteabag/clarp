//! From what the Host knows to what the contract says: the actor's types in the apps' words.

use clarp_actor::Status;
use clarp_actor::conversation::{Page, Versioned};
use clarp_kernel::conversation::{Entry, ToolStatus};
use clarp_kernel::turn::{Activity, Origin, Outcome};
use clarp_protocol::events::{
    ActivityStatus, AgentActivity, AgentState, AgentStateKind, Event, QueueUpdated,
    TranscriptUpdated,
};
use clarp_protocol::log::{LogResponse, Turn};
use clarp_protocol::snapshot::AgentSummary;
use serde_json::json;

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
        (None, None) => AgentStateKind::Idle,
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

pub fn state_event(record: &AgentRecord, status: &Status, now_ms: i64) -> Event {
    let detail = match status.last.as_ref().map(|last| &last.outcome) {
        Some(Outcome::Failed { reason } | Outcome::Interrupted { reason })
            if status.live.is_none() =>
        {
            Some(json!({ "reason": reason }))
        }
        Some(Outcome::Stopped { by }) if status.live.is_none() => Some(json!({ "stopped_by": by })),
        _ => None,
    };
    Event::AgentState(AgentState {
        session: record.session.clone(),
        agent_id: record.agent_id.clone(),
        kind: state_kind(status),
        persona: Some(record.persona.clone()),
        ts: Some(state_since(status).unwrap_or(now_ms)),
        detail,
        status_text: None,
        trace_id: None,
        client_msg_id: None,
        queue_started: None,
        queue_remaining: None,
        origin: None,
    })
}

/// What the agent is doing inside its turn, when it is using a tool or writing.
pub fn activity_event(record: &AgentRecord, status: &Status, now_ms: i64) -> Option<Event> {
    let live = status.live.as_ref()?;
    let (kind, tool, phase) = match &live.activity {
        Activity::Tool { name } => ("tool", Some(name.clone()), "tool"),
        Activity::Responding => ("responding", None, "responding"),
        Activity::Thinking => ("thinking", None, "thinking"),
        Activity::Compacting => ("compacting", None, "compacting"),
        Activity::Waiting => ("waiting", None, "waiting"),
    };
    Some(Event::AgentActivity(AgentActivity {
        session: record.session.clone(),
        agent_id: Some(record.agent_id.clone()),
        persona: Some(record.persona.clone()),
        kind: Some(kind.to_string()),
        phase: Some(phase.to_string()),
        status: Some(ActivityStatus::Running),
        tool,
        ts: Some(now_ms),
        ..AgentActivity::default()
    }))
}

pub fn queue_event(record: &AgentRecord, status: &Status, revision: u64) -> Event {
    Event::QueueUpdated(QueueUpdated {
        session: record.session.clone(),
        agent_id: record.agent_id.clone(),
        queue_depth: status.waiting.len() as u64,
        queue_paused: status.paused,
        queue_revision: revision,
        queue_started: None,
        client_msg_id: None,
    })
}

pub fn transcript_event(record: &AgentRecord, status: &Status) -> Event {
    Event::TranscriptUpdated(TranscriptUpdated {
        session: record.session.clone(),
        agent_id: Some(record.agent_id.clone()),
        backend_session_id: status.backend_session.clone(),
    })
}

/// One thing of a conversation as the contract's "turn".
pub fn turn(version: &Versioned) -> Turn {
    let mut turn = Turn {
        id: version.entry.id().to_string(),
        revision: version.revision,
        timestamp: Some(iso(version.ts_ms)),
        ..Turn::default()
    };
    match &version.entry {
        Entry::Message(message) => {
            turn.role = "user".to_string();
            turn.text = message.text.clone();
            turn.trace_id = message.trace_id.clone();
            turn.origin = Some(
                match &message.origin {
                    Origin::User => "user",
                    Origin::Agent { .. } => "agent",
                    Origin::Trigger { name } => name.as_str(),
                }
                .to_string(),
            );
            if let Some(sender) = &message.sender {
                turn.sender_agent_id = Some(sender.agent_id.clone());
                turn.sender_name = Some(sender.name.clone()).filter(|name| !name.is_empty());
                turn.sender_session =
                    Some(sender.session.clone()).filter(|session| !session.is_empty());
            }
        }
        Entry::Reply(reply) => {
            turn.role = "assistant".to_string();
            turn.text = reply.text.clone();
        }
        Entry::Tool(call) => {
            turn.role = "assistant".to_string();
            turn.kind = Some("tool".to_string());
            turn.tool_name = Some(call.name.clone());
            let status = match call.status {
                ToolStatus::Running => "running",
                ToolStatus::Ok => "ok",
                ToolStatus::Error => "error",
            };
            turn.tools = Some(vec![json!({
                "id": call.id,
                "name": call.name,
                "input": call.input,
                "status": status,
                "output": call.output,
                "started_at_ms": call.started_at_ms,
                "ended_at_ms": call.ended_at_ms,
            })]);
        }
        Entry::Notice(notice) => {
            turn.role = "assistant".to_string();
            turn.kind = Some("notice".to_string());
            turn.text = notice.text.clone();
        }
    }
    turn
}

/// A page of a conversation as `GET /log` answers it.
pub fn log_response(record: &AgentRecord, page: &Page, replace_required: bool) -> LogResponse {
    LogResponse {
        conversation_id: record.conversation_id.clone(),
        turns: page.entries.iter().map(turn).collect(),
        latest_revision: page.latest_revision,
        has_more: page.has_more,
        missing: false,
        replace_required: Some(replace_required),
        latest_ts: page.entries.last().map(|last| iso(last.ts_ms)),
        cwd: Some(record.cwd.clone()),
        ..LogResponse::default()
    }
}

/// An agent's row in the snapshot.
pub fn summary(
    record: &AgentRecord,
    status: &Status,
    backend: &str,
    focused: bool,
) -> AgentSummary {
    let kind = state_kind(status);
    AgentSummary {
        agent_id: record.agent_id.clone(),
        session: record.session.clone(),
        persona: record.persona.clone(),
        backend: backend.to_string(),
        cwd: record.cwd.clone(),
        model: Some(record.model.clone()),
        effort: Some(record.effort.clone()),
        backend_session_id: status.backend_session.clone(),
        conversation_id: Some(record.conversation_id.clone()),
        head_revision: Some(status.head_revision),
        alive: Some(true),
        busy: Some(status.live.is_some()),
        focused: Some(focused),
        latest_state: Some(state_name(&kind)),
        latest_state_ts: state_since(status),
        turn_started_at: Some(status.live.as_ref().map_or(0, |live| live.started_at_ms)),
        last_turn_end: status.last.as_ref().map(|last| last.ended_at_ms / 1_000),
        compacting: Some(kind == AgentStateKind::Compacting),
        queued_turn_count: Some(status.waiting.len() as u64),
        queue_paused: Some(status.paused),
        muted: Some(false),
        ..AgentSummary::default()
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use clarp_actor::LiveStatus;
    use clarp_kernel::conversation::{Message, Reply, ToolCall};
    use clarp_kernel::turn::{Settled, TurnId};

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
            head_revision: 42,
            backend_session: Some("sess-1".to_string()),
        }
    }

    #[test]
    fn an_agents_state_is_named_as_the_apps_know_it() {
        let name = |live, last| state_name(&state_kind(&status(live, last)));
        assert_eq!(name(None, None), "idle");
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
                    by: "peter".to_string()
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

    #[test]
    fn a_page_becomes_the_answer_to_log() {
        let page = Page {
            entries: vec![
                Versioned {
                    entry: Entry::Message(Message {
                        id: "u-1".to_string(),
                        text: "go".to_string(),
                        origin: Origin::User,
                        trace_id: Some("t-1".to_string()),
                        sender: None,
                        client: None,
                    }),
                    revision: 1 << 20,
                    ts_ms: 0,
                    live: false,
                },
                Versioned {
                    entry: Entry::Tool(ToolCall {
                        id: "toolu_1".to_string(),
                        turn: 1,
                        name: "Bash".to_string(),
                        input: json!({"command": "ls"}),
                        status: ToolStatus::Ok,
                        output: Some("a.txt".to_string()),
                        started_at_ms: 10,
                        ended_at_ms: Some(20),
                    }),
                    revision: 5 << 20,
                    ts_ms: 10,
                    live: false,
                },
                Versioned {
                    entry: Entry::Reply(Reply {
                        id: "r-1".to_string(),
                        turn: 1,
                        text: "One file.".to_string(),
                    }),
                    revision: (6 << 20) + 3,
                    ts_ms: 30,
                    live: true,
                },
            ],
            latest_revision: (6 << 20) + 3,
            has_more: true,
            head_revision: (6 << 20) + 3,
        };
        let answer = log_response(&record(), &page, false);
        assert_eq!(answer.conversation_id, "conv-1");
        assert_eq!(answer.latest_revision, (6 << 20) + 3);
        assert!(answer.has_more && !answer.missing);
        let roles: Vec<(&str, &str, &str)> = answer
            .turns
            .iter()
            .map(|turn| (turn.id.as_str(), turn.role.as_str(), turn.text.as_str()))
            .collect();
        assert_eq!(
            roles,
            vec![
                ("u-1", "user", "go"),
                ("toolu_1", "assistant", ""),
                ("r-1", "assistant", "One file.")
            ]
        );
        assert_eq!(answer.turns[0].origin.as_deref(), Some("user"));
        assert_eq!(
            answer.turns[0].timestamp.as_deref(),
            Some("1970-01-01T00:00:00.000Z")
        );
        assert_eq!(answer.turns[1].tool_name.as_deref(), Some("Bash"));
        assert_eq!(answer.turns[1].tools.as_ref().unwrap()[0]["status"], "ok");
        // And it is the contract's shape: it reads back as the type the apps are generated from.
        let wire = serde_json::to_string(&answer).unwrap();
        assert_eq!(serde_json::from_str::<LogResponse>(&wire).unwrap(), answer);
    }

    #[test]
    fn a_status_becomes_a_snapshot_row_and_events() {
        let busy = status(
            Some(Activity::Tool {
                name: "Bash".to_string(),
            }),
            Some(Outcome::Completed),
        );
        let row = summary(&record(), &busy, "claude", true);
        assert_eq!(row.latest_state.as_deref(), Some("tool"));
        assert_eq!((row.busy, row.focused), (Some(true), Some(true)));
        assert_eq!(
            (row.head_revision, row.queued_turn_count, row.queue_paused),
            (Some(42), Some(1), Some(true))
        );
        assert_eq!(row.backend_session_id.as_deref(), Some("sess-1"));

        let wire = serde_json::to_value(state_event(&record(), &busy, 9_000)).unwrap();
        assert_eq!(wire["type"], "agent-state");
        assert_eq!(
            (&wire["kind"], &wire["session"]),
            (&json!("tool"), &json!("maximus"))
        );
        let activity =
            serde_json::to_value(activity_event(&record(), &busy, 9_000).unwrap()).unwrap();
        assert_eq!(
            (&activity["type"], &activity["tool"]),
            (&json!("agent-activity"), &json!("Bash"))
        );
        let queue = serde_json::to_value(queue_event(&record(), &busy, 7)).unwrap();
        assert_eq!(
            (
                &queue["queue_depth"],
                &queue["queue_paused"],
                &queue["queue_revision"]
            ),
            (&json!(1), &json!(true), &json!(7))
        );

        let stopped = status(
            None,
            Some(Outcome::Stopped {
                by: "peter".to_string(),
            }),
        );
        let wire = serde_json::to_value(state_event(&record(), &stopped, 9_000)).unwrap();
        assert_eq!(
            (&wire["kind"], &wire["detail"]["stopped_by"]),
            (&json!("interrupted"), &json!("peter"))
        );
        assert!(activity_event(&record(), &stopped, 9_000).is_none());
    }
}
