//! From what the Host knows to what the contract says: the actor's types in the apps' words.
//!
//! The shapes here are the Python Host's, field for field, as the conformance run recorded them
//! (`server-rs/conformance/golden`). Where the Python Host sends a field with an empty value, so
//! does this Host: an app's decoder may count on the field being there. A field this Host has
//! nothing to say about is sent as the Python Host sends it when it has nothing to say.

use clarp_actor::conversation::{Page, Versioned};
use clarp_actor::{LiveStatus, LiveTool, Status};
use clarp_kernel::agent::Rest;
use clarp_kernel::conversation::{Entry, ToolStatus, TurnEnd};
use clarp_kernel::jobs::{Job, JobRecord};
use clarp_kernel::notify;
use clarp_kernel::turn::{Activity, Outcome, automation_kind};
use clarp_protocol::events::AgentStateKind;
use clarp_speech::markup;
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

/// The moment a time of the contract's form names, in milliseconds. `None` for anything else.
pub fn from_iso(text: &str) -> Option<i64> {
    let (date, time) = text.trim().split_once(['T', ' '])?;
    let mut date = date.splitn(3, '-').map(|part| part.parse::<i64>().ok());
    let (year, month, day) = (date.next()??, date.next()??, date.next()??);
    let time = time.trim_end_matches('Z');
    let time = time.split(['+']).next().unwrap_or(time);
    let (clock, fraction) = time.split_once('.').unwrap_or((time, "0"));
    let mut clock = clock.splitn(3, ':').map(|part| part.parse::<i64>().ok());
    let (hour, minute, second) = (
        clock.next()??,
        clock.next()??,
        clock.next().flatten().unwrap_or(0),
    );
    let millis: i64 = format!("{fraction:0<3}").get(..3)?.parse().ok()?;
    if !(1..=12).contains(&month) || !(1..=31).contains(&day) {
        return None;
    }
    // A date of the civil calendar to days since 1970 (Howard Hinnant's algorithm).
    let year = if month <= 2 { year - 1 } else { year };
    let era = year.div_euclid(400);
    let year_of_era = year.rem_euclid(400);
    let month_index = (month + 9) % 12;
    let day_of_year = (153 * month_index + 2) / 5 + day - 1;
    let day_of_era = year_of_era * 365 + year_of_era / 4 - year_of_era / 100 + day_of_year;
    let days = era * 146_097 + day_of_era - 719_468;
    Some(days * 86_400_000 + hour * 3_600_000 + minute * 60_000 + second * 1_000 + millis)
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
        // It said so itself: work it left running goes on, or no longer does.
        (None, _) if status.resting == Some(Rest::Background) => AgentStateKind::Background,
        (None, _) if status.resting == Some(Rest::Idle) => AgentStateKind::Idle,
        // An agent that came from another Host with its conversation, and has not run here yet.
        (None, None) if status.head_revision > 0 => AgentStateKind::Idle,
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

/// When the state an app shows began. An agent that never ran has been as it is since it was made.
fn state_since(record: &AgentRecord, status: &Status) -> i64 {
    match (&status.live, &status.last) {
        (Some(live), _) => live.started_at_ms,
        (None, Some(last)) => last.ended_at_ms,
        (None, None) => record.created_at_ms,
    }
}

/// The name of the tool's session: what the tool said, or, before it has said anything, the
/// name the Host gave it.
fn session_name(record: &AgentRecord, status: &Status) -> String {
    match &status.backend_session {
        Some(session) => session.clone(),
        None => conversation_id(record, status),
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
pub fn dispatched(record: &AgentRecord, live: &LiveStatus, waiting: usize) -> Value {
    json!({
        "type": "agent-state",
        "agent_id": record.agent_id,
        "session": record.session,
        "kind": "thinking",
        "client_msg_id": client_id(&live.message_id),
        "trace_id": live.trace_id.clone().unwrap_or_default(),
        "queue_started": live.from_queue,
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

/// The name the Python Host's Claude runner goes by when it reports a finished turn. Everything
/// else calls the backend by its own name.
fn runner(backend: &str) -> &str {
    if backend == "claude" {
        "clarp"
    } else {
        backend
    }
}

/// An agent's state, as the one that watches all states tells it. `ended` is how the last turn
/// ended, when it is known.
pub fn state(
    record: &AgentRecord,
    status: &Status,
    backend: &str,
    ended: Option<&TurnEnd>,
) -> Value {
    let kind = state_kind(status);
    let detail = match (&status.live, &status.last) {
        (Some(live), _) => json!({
            "backend_session_id": session_name(record, status),
            "dispatch": backend,
            "origin": live.origin.name(),
            "source": "pwa",
            "trace_id": live.trace_id.clone().unwrap_or_default(),
        }),
        (None, Some(last)) if last.outcome == Outcome::Completed => {
            let mut detail = json!({
                "dispatch": runner(backend),
                "duration_ms": last.ended_at_ms - last.started_at_ms.unwrap_or(last.ended_at_ms),
                "origin": last.origin.name(),
            });
            if let Some(end) = ended.filter(|end| end.turn == last.id.0) {
                detail["trace_id"] = json!(end.trace_id.clone().unwrap_or_default());
                if let (Some(tokens_in), Some(tokens_out)) = (end.tokens_in, end.tokens_out) {
                    detail["tokens_in"] = json!(tokens_in);
                    detail["tokens_out"] = json!(tokens_out);
                }
            }
            detail
        }
        (None, Some(last)) => ending_detail(&last.outcome),
        (None, None) => json!({}),
    };
    json!({
        "type": "agent-state",
        "agent_id": record.agent_id,
        "session": record.session,
        "persona": record.persona,
        "kind": state_name(&kind),
        "ts": state_since(record, status),
        "detail": detail,
        "status_text": status.label,
    })
}

/// The state a Stop leaves, as the route that took the Stop tells it at once.
pub fn stopped(record: &AgentRecord, status: &Status) -> Option<Value> {
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
        "ts": state_since(record, status),
        "detail": ending_detail(&last.outcome),
    }))
}

/// What an agent is doing, as a line a client can show. The same object is the snapshot's
/// `activity` and, with its number, the `agent-activity` event.
pub fn activity(record: &AgentRecord, status: &Status) -> Value {
    let kind = state_kind(status);
    let running = status.live.as_ref();
    // The tool that runs, summed up: the one that started last, or what its name alone says.
    let followed = running.and_then(|live| live.tools.last());
    let by_name = match (followed, running.map(|live| &live.activity)) {
        (None, Some(Activity::Tool { name })) => {
            Some(clarp_kernel::tools::summarize(name, &Value::Null))
        }
        _ => None,
    };
    let tool_words = match (followed, &by_name) {
        (Some(tool), _) => Some((tool.action.as_str(), tool.summary.as_str())),
        (None, Some(summed)) => Some((summed.action.as_str(), summed.summary.as_str())),
        (None, None) => None,
    };
    let responding = matches!(
        running.map(|live| &live.activity),
        Some(Activity::Responding)
    );
    let (action, phase, state, outcome, summary) = match (&kind, status.last.as_ref()) {
        (AgentStateKind::Thinking, _) if responding => (
            "responding",
            "responding",
            "responding",
            "running",
            "Responding",
        ),
        (AgentStateKind::Thinking, _) => {
            ("thinking", "thinking", "thinking", "running", "Thinking")
        }
        (AgentStateKind::Tool, _) => {
            let (action, summary) = tool_words.unwrap_or(("tool", "Using a tool"));
            (action, "tool", "tool", "running", summary)
        }
        (AgentStateKind::Compacting, _) => (
            "compacting",
            "compacting",
            "compacting",
            "running",
            "Compacting context",
        ),
        // It waits for a person: an approval, an answer.
        (AgentStateKind::Waiting, _) => (
            "needs attention",
            "waiting",
            "waiting",
            "error",
            "Needs attention",
        ),
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
        (AgentStateKind::Spawned, _) => ("started", "spawned", "idle", "ok", "Started"),
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
        "tool": followed.map_or(tool, |tool| tool.name.as_str()),
        "file_path": followed.map_or("", |tool| tool.file_path.as_str()),
        "call_id": followed.map(|tool| &tool.id),
        "started_at_ms": followed.map(|tool| tool.started_at_ms),
        "turn_started_ms": running.map(|live| live.started_at_ms),
        "ts": followed.map_or_else(|| state_since(record, status), |tool| tool.started_at_ms),
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

/// The queue changed. `message_id` names the message the change is about, when it is about
/// one: a message that joined the queue, or one that left it to run (`started`).
pub fn queue_updated(
    record: &AgentRecord,
    status: &Status,
    message_id: Option<&str>,
    started: bool,
) -> Value {
    let mut event = json!({
        "type": "queue-updated",
        "agent_id": record.agent_id,
        "session": record.session,
        "queue_depth": status.waiting.len(),
        "queue_paused": status.paused,
        "queue_started": started,
        "queue_revision": status.queue_revision,
    });
    if let Some(message_id) = message_id {
        event["client_msg_id"] = json!(client_id(message_id));
    }
    event
}

/// A background job as the apps are given it: what is kept of it, and what is read off it
/// at this moment (how long ago it was heard from, whether that is still fresh).
pub fn job(record: &AgentRecord, job: &Job, now_ms: i64) -> Value {
    let active = job.status.is_active();
    // Lost is not failed: nobody heard from it any more, which says nothing about its work.
    let lost = job.lost();
    let signs = [job.heartbeat_at_ms, job.progress_at_ms];
    let last_sign = signs.iter().flatten().max();
    let freshness = match last_sign {
        _ if lost => "stale",
        None => "unknown",
        Some(at) if (now_ms - at).max(0) <= job.heartbeat_timeout_ms => "fresh",
        Some(_) => "stale",
    };
    let outcome = job.outcome_state();
    let agent_name = if record.persona.is_empty() {
        &record.session
    } else {
        &record.persona
    };
    json!({
        "job_id": job.id,
        "agent_id": record.agent_id,
        "session": record.session,
        "kind": job.kind,
        "title": job.title,
        "detail": job.detail,
        "status": job.status.as_str(),
        "started_at": job.started_at_ms,
        "updated_at": job.updated_at_ms,
        "cancelled_at": job.cancelled_at_ms,
        "heartbeat_at": job.heartbeat_at_ms,
        "heartbeat_timeout_ms": job.heartbeat_timeout_ms,
        "heartbeat_source": job.heartbeat_source,
        "worker_pid": (job.worker.pid != 0).then_some(job.worker.pid),
        "worker_start_token": job.worker.start_token,
        "terminal_at": job.terminal_at_ms,
        "terminal_reason": job.terminal_reason,
        "revision": job.revision,
        "generation": job.generation,
        "owner_kind": "agent",
        "computer_id": null,
        "progress_text": job.progress_text,
        "progress_at": job.progress_at_ms,
        "log_path": job.log_path,
        "worker_cwd": job.worker_cwd,
        "started_trace_id": job.started_trace_id,
        "metadata": if job.metadata.is_object() { job.metadata.clone() } else { json!({}) },
        "agent_name": agent_name,
        "heartbeat_age_ms": job.heartbeat_at_ms.map(|at| (now_ms - at).max(0)),
        "worker_freshness": freshness,
        "is_terminal": !active,
        "can_cancel": active,
        "elapsed_ms": (job.terminal_at_ms.unwrap_or(now_ms) - job.started_at_ms).max(0),
        "outcome_state": outcome,
    })
}

/// An artifact changed: an app that shows it asks for it again.
pub fn artifact_updated(record: &AgentRecord, artifact_id: &str) -> Value {
    json!({
        "type": "artifact-updated",
        "session": record.session,
        "agent_id": record.agent_id,
        "artifact_id": artifact_id,
    })
}

/// How many requests wait for the user changed, or may have.
pub fn attention_updated(count: usize) -> Value {
    json!({ "type": "attention-updated", "attention_count": count })
}

/// A background job changed: the job as it now stands, under the number of the change.
pub fn job_updated(record: &AgentRecord, changed: &Job, now_ms: i64) -> Value {
    json!({
        "type": "background-job-updated",
        "change_revision": changed.revision,
        "observed_at": now_ms,
        "job_id": changed.id,
        "session": record.session,
        "agent_id": record.agent_id,
        "status": changed.status.as_str(),
        "job": job(record, changed, now_ms),
    })
}

/// A job's timeline as `GET /background-jobs/<id>` gives it: each change with what kind of
/// change it was, read off the entry before it.
pub fn job_timeline(entries: &[JobRecord]) -> Vec<Value> {
    let mut before: Option<&str> = None;
    entries
        .iter()
        .map(|entry| {
            let status = entry.job.status.as_str();
            let change = if before != Some(status) {
                "status"
            } else if !entry.note.is_empty() {
                "progress"
            } else {
                "update"
            };
            before = Some(status);
            json!({
                "event_id": entry.job.revision,
                "observed_at": entry.at_ms,
                "status": status,
                "note": entry.note,
                "change": change,
            })
        })
        .collect()
}

/// A text cut to `max` characters, the last of them an ellipsis when something was left out.
fn cut(text: String, max: usize, trim_before_mark: bool) -> String {
    if text.chars().count() <= max {
        return text;
    }
    let kept: String = text.chars().take(max.saturating_sub(1)).collect();
    let kept = if trim_before_mark {
        kept.trim_end()
    } else {
        kept.as_str()
    };
    format!("{kept}…")
}

/// The longest preview a notification carries.
const NOTIFICATION_PREVIEW_MAX: usize = 180;

/// What an automated check says when it found nothing to do. Nobody is told of those.
const NOTHING_TO_DO: [&str; 4] = [
    "heartbeat check: no action needed.",
    "dreaming check: no action needed.",
    "leader check: no action needed.",
    "automated check: no action needed.",
];

/// What a notification says of a reply, and why the user is told: what the agent said aloud,
/// when it said something aloud (`speak`), otherwise what it wrote (`text-reply`). `None` for
/// a reply that has nothing for the user in it.
pub fn notification_preview(reply: &str) -> Option<(String, &'static str)> {
    // Cleaned as what it is, something spoken: an emotion tag the engine acts out is not
    // shown. (The Python Host cleans the blocks' inner text and shows the tag.)
    let spoken = markup::speak_blocks(reply, true).join(" ");
    let spoken = markup::clean_for_display(&format!("<speak>{spoken}</speak>"), true);
    if !spoken.is_empty() {
        return Some((cut(spoken, NOTIFICATION_PREVIEW_MAX, false), "speak"));
    }
    let mut written = reply.to_string();
    for marker in ["HEARTBEAT_OK", "DREAMING_OK", "LEADER_NOOP"] {
        written = written.replace(marker, "");
    }
    let written = markup::clean_for_display(&written, true);
    if written.is_empty() || NOTHING_TO_DO.contains(&written.to_lowercase().as_str()) {
        return None;
    }
    Some((cut(written, NOTIFICATION_PREVIEW_MAX, false), "text-reply"))
}

/// The message a finished turn answered, as far as telling the user of the turn looks at it.
#[derive(Debug, Clone, Copy, Default)]
pub struct Asked<'a> {
    /// The message's id.
    pub message_id: &'a str,
    /// The app that sent it, when it said.
    pub client: &'a str,
}

/// A finished turn the user should know about: what an app badges and marks unread from.
/// `None` when the rule says nobody is told ([`clarp_kernel::notify`]): the reply it ended on
/// has nothing for the user in it, or the turn was talk between agents.
pub fn user_notification(
    record: &AgentRecord,
    notification_id: &str,
    asked: &Asked<'_>,
    reply: &Versioned,
    done_ms: i64,
) -> Option<Value> {
    let Entry::Reply(said) = &reply.entry else {
        return None;
    };
    let (preview, kind) = notification_preview(&said.text).unzip();
    let turn = notify::Completed {
        agent_id: &record.agent_id,
        has_cause: !asked.message_id.is_empty(),
        content: kind.unwrap_or(""),
        interrupted: false,
        client: asked.client,
    };
    // A helper reports to the agent that made it: what it says is marked unread and pages
    // nobody, as if it were muted. No setting treats automation specially on this Host yet.
    let agent = notify::Capabilities {
        muted: record.muted || record.is_helper(),
        ..notify::Capabilities::default()
    };
    let notify::Decision::Notify {
        reason,
        push,
        muted,
        ..
    } = notify::decide(said.origin.name(), &turn, &agent, false)
    else {
        return None;
    };
    Some(json!({
        "type": "user-notification",
        "notification_id": notification_id,
        "agent_id": record.agent_id,
        "session": record.session,
        "persona": record.persona,
        "origin": said.origin.name(),
        "reason": reason,
        "cause_message_id": asked.message_id,
        "source_message_id": reply.entry.id(),
        "preview": preview.unwrap_or_default(),
        "done_ts": done_ms,
        "badge": true,
        "unread": true,
        "push": push,
        "muted": muted,
    }))
}

pub fn roster(kind: &str, session: &str) -> Value {
    json!({ "type": "agent-roster", "kind": kind, "session": session })
}

/// The helper a job only mirrors, when it does. `clarp-sub-agent --clarp-agent` registers a
/// job of kind `sub-agent` that names the helper's session: the helper itself is the
/// sub-agent, and the job that watches it is not a second one. `helper_sessions` are the
/// sessions of the helpers the job's owner made.
pub fn mirrored_helper<'a>(job: &Job, helper_sessions: &[&'a str]) -> Option<&'a str> {
    if job.kind != "sub-agent" {
        return None;
    }
    let named = [
        job.metadata["helper_session"].as_str().unwrap_or(""),
        job.detail.as_str(),
    ];
    named
        .iter()
        .map(|name| name.trim())
        .filter(|name| !name.is_empty())
        .find_map(|name| {
            helper_sessions
                .iter()
                .copied()
                .find(|session| *session == name)
        })
}

/// An agent was made: the apps read the list again.
pub fn roster_created(record: &AgentRecord) -> Value {
    json!({
        "type": "agent-roster",
        "kind": "created",
        "session": record.session,
        "persona": record.persona,
        "voice_id": record.voice_id,
        "backend": record.backend,
    })
}

/// An agent has another name: the apps read the list again.
pub fn roster_renamed(session: &str, name: &str) -> Value {
    json!({ "type": "agent-roster", "kind": "agent-renamed", "session": session, "name": name })
}

pub fn focus(record: &AgentRecord) -> Value {
    json!({ "type": "agent-focus", "session": record.session, "agent_id": record.agent_id })
}

// ---- a conversation --------------------------------------------------------------------------

/// How a turn ended, on the row that closed it. A turn is named by the trace its message was
/// sent under, which is the name the apps already know it by.
fn turn_end(end: &TurnEnd) -> Value {
    json!({
        "turn_id": end.trace_id.clone().unwrap_or_else(|| end.turn.to_string()),
        "status": end.status,
        "started_at_ms": end.started_at_ms,
        "ended_at_ms": end.ended_at_ms,
        "worked_ms": end.started_at_ms.map(|started| end.ended_at_ms - started),
        "tool_count": end.tool_count,
    })
}

/// What a chat shows in place of the prompt of an automated turn: the prompt is the Host's
/// or a watcher's own business.
fn automated_prompt(kind: &str) -> Option<&'static str> {
    match kind {
        "heartbeat" => Some("Automated heartbeat check"),
        "leader_tick" => Some("Automated leader check"),
        "dreaming" => Some("Automated dreaming run"),
        "watcher" => Some("Automated watcher event"),
        _ => None,
    }
}

/// What a chat shows in place of an automated turn's reply that only says there was nothing
/// to do.
fn automated_reply(kind: &str, said: &str) -> Option<&'static str> {
    match kind {
        "heartbeat" if said.contains("HEARTBEAT_OK") => Some("Heartbeat check: no action needed."),
        "leader_tick" if said.contains("LEADER_NOOP") => Some("Leader check: no action needed."),
        "dreaming" if said.contains("DREAMING_OK") => Some("Dreaming check: no action needed."),
        _ => None,
    }
}

/// Whether a row is what a list of chats shows as the last thing said in one: something
/// someone said, not a tool call, and nothing automated.
pub fn previewed(version: &Versioned) -> bool {
    let by_hand = automation_kind(version.entry.origin_name()).is_empty();
    match &version.entry {
        Entry::Message(message) => by_hand && !message.text.is_empty(),
        Entry::Reply(reply) => by_hand && !reply.text.is_empty(),
        Entry::Notice(notice) => !notice.text.is_empty(),
        Entry::Imported(imported) => {
            let row = &imported.row;
            let said = row["text"].as_str().is_some_and(|text| !text.is_empty());
            let tool = row["tool_name"]
                .as_str()
                .is_some_and(|name| !name.is_empty());
            by_hand && said && !tool
        }
        Entry::Tool(_) | Entry::TurnEnd(_) => false,
    }
}

/// One thing of a conversation as the contract's "turn": a row, with every field of a row.
pub fn row(version: &Versioned) -> Value {
    if let Entry::Imported(imported) = &version.entry {
        // A row from the Host the agent came from is served as that Host served it. Only its
        // revision is this Host's: that is what orders it among the rows written here.
        let mut row = imported.row.clone();
        if row.is_object() {
            row["revision"] = json!(version.revision);
        }
        return row;
    }
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
    // What a watcher or the Host's own routine began is marked, for an app to fold away.
    let kind = automation_kind(version.entry.origin_name());
    row["automated"] = json!(!kind.is_empty());
    row["automation_kind"] = json!(kind);
    match &version.entry {
        Entry::Message(message) => {
            row["role"] = json!("user");
            row["text"] = match automated_prompt(kind) {
                Some(shown) => json!(shown),
                None => json!(markup::strip_hidden_blocks(&message.text)),
            };
            row["trace_id"] = json!(message.trace_id.clone().unwrap_or_default());
            row["origin"] = json!(message.origin.name());
            if let Some(sender) = &message.sender {
                row["sender_agent_id"] = json!(sender.agent_id);
                row["sender_name"] = json!(sender.name);
                row["sender_session"] = json!(sender.session);
            }
        }
        Entry::Reply(reply) => {
            // What is metadata and never conversation does not leave the Host. The voice
            // markup does: an app hides it itself, and speaks from it.
            let said = markup::strip_hidden_blocks(&reply.text);
            row["text"] = match automated_reply(kind, &said) {
                Some(shown) => json!(shown),
                None => json!(said),
            };
            row["trace_id"] = json!(reply.trace_id.clone().unwrap_or_default());
            row["origin"] = json!(reply.origin.name());
        }
        Entry::Tool(call) => {
            let status = match call.status {
                ToolStatus::Running => "running",
                ToolStatus::Ok => "ok",
                ToolStatus::Error => "error",
            };
            row["kind"] = json!("tool");
            row["origin"] = json!(call.origin.name());
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
        // Served as it came, above.
        Entry::Imported(_) => {}
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

/// The name of an agent's conversation. It is the Host's own: chosen when the agent was made,
/// or brought along when the agent moved here, and the same from the first message on. A tool
/// that can be told its session's name is told this one (Claude); one that names its session
/// itself (Codex) is resumed by its own name, which no client sees. An agent that has not been
/// written to has no conversation yet.
pub fn conversation_id(record: &AgentRecord, status: &Status) -> String {
    if status.head_revision > 0 || status.backend_session.is_some() {
        record.conversation_id.clone()
    } else {
        String::new()
    }
}

/// A page of a conversation as `GET /log` answers it.
pub fn log_response(
    record: &AgentRecord,
    status: &Status,
    page: &Page,
    replace_required: bool,
) -> Value {
    // Claude Code keeps a transcript of its own under the session's name. The Python Host
    // names that file; so does this one, for what it is worth to whoever looks for it.
    let conversation = conversation_id(record, status);
    let file = (record.backend == "claude" && !conversation.is_empty())
        .then(|| format!("{conversation}.jsonl"));
    json!({
        "conversation_id": conversation,
        "turns": page.entries.iter().map(row).collect::<Vec<_>>(),
        "latest_revision": page.latest_revision,
        "has_more": page.has_more,
        "missing": page.head_revision == 0,
        "replace_required": replace_required,
        "latest_ts": page.entries.last().map(|last| iso(last.ts_ms)).unwrap_or_default(),
        "cwd": record.cwd,
        "file": file,
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

/// What a row says in a list of chats: one line, as a screen shows it, no longer than 80
/// characters, and what the user wrote marked as theirs.
fn preview(version: &Versioned) -> String {
    let (theirs, text) = match &version.entry {
        Entry::Message(message) => (true, message.text.as_str()),
        Entry::Reply(reply) => (false, reply.text.as_str()),
        Entry::Notice(notice) => (false, notice.text.as_str()),
        Entry::Tool(call) => (false, call.name.as_str()),
        Entry::Imported(imported) => (
            imported.row["role"] == "user",
            imported.row["text"].as_str().unwrap_or(""),
        ),
        Entry::TurnEnd(_) => (false, ""),
    };
    let shown = markup::clean_for_display(text, true);
    if shown.is_empty() {
        return String::new();
    }
    let shown = if theirs {
        format!("You: {shown}")
    } else {
        shown
    };
    cut(shown, 80, true)
}

/// An agent's row in the snapshot, with every field of a row. `ended` is how the last turn
/// ended, when it is known.
pub fn summary(
    record: &AgentRecord,
    status: &Status,
    backend: &str,
    newest: Option<&Versioned>,
    ended: Option<&TurnEnd>,
    focused: bool,
) -> Value {
    let kind = state_kind(status);
    let live = status.live.as_ref();
    let last = status.last.as_ref();
    let ended = ended.filter(|end| last.is_some_and(|last| last.id.0 == end.turn));
    let since = state_since(record, status);
    let last_message = newest.map(preview).unwrap_or_default();
    let completed = last.filter(|last| last.outcome == Outcome::Completed);
    // The turn an app may show as the agent's current or latest one, by its trace.
    let turn_id = match live {
        Some(live) => live.trace_id.clone(),
        None => ended.and_then(|end| end.trace_id.clone()),
    };
    // An agent that never ran has nothing to show there at all.
    let live_activity = if live.is_none() && last.is_none() {
        Value::Null
    } else {
        // What the agent is doing, and the words the apps show for it. A tool that runs
        // comes first, and the line says what it runs or reads. Writing its answer is a
        // state of its own here, though the agent's row calls it thinking.
        let tools: &[LiveTool] = live.map_or(&[], |live| live.tools.as_slice());
        let line = |name: &str, category: &str, label: &str| {
            Some(clarp_kernel::tools::headline(name, category, label))
        };
        let (state, headline) = match (live.map(|live| &live.activity), tools.last(), completed) {
            (Some(_), Some(tool), _) => ("tool", line(&tool.name, tool.category, &tool.label)),
            // A tool this Host was told of by its name alone.
            (Some(Activity::Tool { name }), None, _) => {
                let described = clarp_kernel::tools::describe(name, &Value::Null);
                ("tool", line(name, described.category, &described.label))
            }
            (Some(Activity::Thinking), ..) => ("thinking", Some("Thinking".to_string())),
            (Some(Activity::Responding), ..) => ("responding", Some("Responding".to_string())),
            (Some(Activity::Compacting), ..) => ("compacting", Some("Compacting".to_string())),
            (Some(Activity::Waiting), ..) => ("waiting", None),
            (None, _, Some(_)) => ("idle", None),
            (None, _, None) => ("interrupted", Some("Interrupted".to_string())),
        };
        // The tool that started last stands for all that run.
        let tool = tools.last().map(|tool| {
            json!({
                "name": tool.name,
                "call_id": tool.id,
                "label": tool.label,
                "category": tool.category,
                "item_id": tool.id,
                "started_at_ms": tool.started_at_ms,
            })
        });
        let running_tools = match tools.len() {
            0 => usize::from(kind == AgentStateKind::Tool),
            running => running,
        };
        json!({
            "state": state,
            "headline": headline,
            "since_ms": tools.last().map_or(since, |tool| tool.started_at_ms),
            "turn_id": turn_id,
            "turn_started_ms": live.map(|live| live.started_at_ms),
            "running_tools": running_tools,
            "tool": tool,
            "item_id": tools.last().map(|tool| &tool.id),
        })
    };
    // The start of the turn that runs: what an app counts the running turn from. At rest
    // there is no such turn, however the last one ended.
    let turn_started_at = live.map_or(0, |live| live.started_at_ms);
    json!({
        "agent_id": record.agent_id,
        "session": record.session,
        "persona": record.persona,
        "backend": backend,
        "cwd": record.cwd,
        // The model the agent is set to. An agent that is set to none shows the model that
        // answered last. A turn that was cut off may or may not have said its model before
        // it went: what an app shows must not depend on that.
        "model": if record.model.is_empty() {
            ended
                .filter(|end| end.status == "completed")
                .and_then(|end| end.model.clone())
                .unwrap_or_default()
        } else {
            record.model.clone()
        },
        "effort": record.effort,
        "voice_id": record.voice_id,
        "avatar_symbol": "",
        "avatar_url": "",
        "avatar_generation_status": null,
        "model_avatar_url": "",
        "mcp_servers": [],
        "heartbeat_enabled": false,
        "dreaming_enabled": false,
        "muted": record.muted,
        "quiet": false,
        "archived_at": record.archived_at_ms,
        "role": record.role(),
        "is_janitor": false,
        "parent_agent_id": record.parent_agent_id,
        "helper_state": record.helper_state,
        // What the agent made: counted over the whole fleet, by whoever builds the list.
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

        "backend_session_id": session_name(record, status),
        "conversation_id": conversation_id(record, status),
        "head_revision": status.head_revision,
        "last_message_id": newest.map(|row| row.entry.id()).unwrap_or_default(),
        "last_message": last_message,
        "last_completed_message": last_message,

        "alive": true,
        "busy": live.is_some(),
        "focused": focused,
        "latest_state": state_name(&kind),
        "latest_state_ts": since,
        "turn_started_at": turn_started_at,
        "last_activity": since / 1_000,
        "last_turn_end": completed.map_or(0, |last| last.ended_at_ms / 1_000),
        "status_text": (!status.label.is_empty()).then_some(&status.label),
        "activity": activity(record, status),
        "live_activity": live_activity,
        "compacting": kind == AgentStateKind::Compacting,
        "context_tokens": ended.and_then(|end| end.tokens_in),
        "context_window": clarp_backend::context_window(&record.backend),
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
    use clarp_kernel::turn::{Origin, Settled, TurnId};
    use clarp_protocol::events::{Envelope, Event};
    use clarp_protocol::log::LogResponse;
    use clarp_protocol::snapshot::AgentSummary;

    use super::*;

    #[test]
    fn a_time_is_read_back_as_it_was_written() {
        for ms in [
            0,
            1_756_791_660_123,
            951_782_400_000,
            1_794_700_800_000,
            -1,
            4_102_444_799_999,
        ] {
            assert_eq!(from_iso(&iso(ms)), Some(ms), "{}", iso(ms));
        }
        assert_eq!(
            from_iso("2026-11-15T04:27:09.698Z"),
            Some(1_794_716_829_698)
        );
        assert_eq!(from_iso("2026-11-15 04:27:09"), Some(1_794_716_829_000));
        assert_eq!(from_iso("2026-11-15T04:27Z"), Some(1_794_716_820_000));
        assert_eq!(from_iso(""), None);
        assert_eq!(from_iso("yesterday"), None);
        assert_eq!(from_iso("2026-13-01T00:00:00Z"), None);
    }

    #[test]
    fn a_row_from_another_host_is_served_as_it_came() {
        let came = json!({"id": "live-abc", "role": "assistant", "text": "from before", "revision": 812,
                          "tools": [{"name": "Bash"}], "phase": "final"});
        let served = row(&version(
            Entry::Imported(clarp_kernel::conversation::Imported {
                id: "live-abc".to_string(),
                row: came.clone(),
            }),
            5 << 20,
            None,
        ));
        assert_eq!(
            served["revision"],
            5 << 20,
            "only the revision is this Host's"
        );
        let mut same = served.clone();
        same["revision"] = json!(812);
        assert_eq!(same, came);
    }

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
                origin: Origin::User,
                from_queue: false,
                starting: false,
                activity,
                started_at_ms: 5_000,
                tool_count: 1,
                tools: Vec::new(),
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
            label: String::new(),
            resting: None,
        }
    }

    #[test]
    fn an_agents_state_is_named_as_the_apps_know_it() {
        let name = |live, last| state_name(&state_kind(&status(live, last)));
        let mut new = status(None, None);
        new.head_revision = 0;
        assert_eq!(state_name(&state_kind(&new)), "spawned");
        assert_eq!(
            name(None, None),
            "idle",
            "it has a conversation and no turn here yet: it came from another Host"
        );
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
            ..AgentRecord::default()
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
            trace_id: None,
            model: None,
            tokens_in: None,
            tokens_out: None,
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
                        origin: Origin::User,
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
                        origin: Origin::User,
                        id: "r-1".to_string(),
                        turn: 1,
                        text: "One file.".to_string(),
                        trace_id: None,
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
        assert_eq!(answer["conversation_id"], "conv-1");
        assert_eq!(answer["latest_revision"], 7 << 20);
        assert_eq!(
            (&answer["has_more"], &answer["missing"]),
            (&json!(true), &json!(false))
        );
        assert_eq!(
            answer["file"], "conv-1.jsonl",
            "Claude Code's own transcript of the session"
        );
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
            json!({"turn_id": "1", "status": "completed", "started_at_ms": 10, "ended_at_ms": 40, "worked_ms": 30, "tool_count": 1}),
            "a turn without a trace is named by its number"
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
            state_kind(&fresh),
            AgentStateKind::Idle,
            "an agent that arrived with a conversation is not new"
        );
        assert_eq!(
            conversation_id(&record(), &status(None, None)),
            "conv-1",
            "and it stays that, whatever the tool calls its own session"
        );
    }

    fn ended() -> TurnEnd {
        TurnEnd {
            id: "t-1".to_string(),
            turn: 1,
            closes: "r-1".to_string(),
            status: "completed".to_string(),
            started_at_ms: Some(1_100),
            ended_at_ms: 4_000,
            tool_count: 0,
            trace_id: Some("trace-1".to_string()),
            model: Some("fable".to_string()),
            tokens_in: Some(5),
            tokens_out: Some(7),
        }
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
        let row = summary(
            &record(),
            &busy,
            "claude",
            Some(&newest),
            Some(&ended()),
            true,
        );
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
        assert_eq!(row["conversation_id"], "conv-1");
        assert_eq!(
            (&row["last_message"], &row["last_message_id"]),
            (&json!("You: newest"), &json!("u-9"))
        );
        assert_eq!(row["activity"]["tool"], "Bash");
        assert_eq!(row["turn_started_at"], 5_000);
        assert_eq!(row["live_activity"]["turn_id"], "trace-2");
        // What it is doing, in the apps' words, for as long as it does it.
        let doing = |activity: Activity| {
            let mut status = busy.clone();
            status.live.as_mut().unwrap().activity = activity;
            let row = summary(&record(), &status, "claude", Some(&newest), None, true);
            (
                row["live_activity"]["state"].clone(),
                row["live_activity"]["headline"].clone(),
                row["latest_state"].clone(),
            )
        };
        assert_eq!(
            doing(Activity::Thinking),
            (json!("thinking"), json!("Thinking"), json!("thinking"))
        );
        assert_eq!(
            doing(Activity::Responding),
            (json!("responding"), json!("Responding"), json!("thinking"))
        );
        assert_eq!(
            doing(Activity::Compacting),
            (
                json!("compacting"),
                json!("Compacting"),
                json!("compacting")
            )
        );
        // A tool it was told of by name alone, and one it follows: the line says what it runs.
        assert_eq!(row["live_activity"]["headline"], "Running Bash");
        assert_eq!(row["live_activity"]["running_tools"], 1);
        let mut followed = busy.clone();
        followed.live.as_mut().unwrap().tools = vec![
            LiveTool {
                id: "c-1".to_string(),
                name: "Read".to_string(),
                category: "read",
                label: "src/main.rs".to_string(),
                action: "reading file".to_string(),
                summary: "src/main.rs".to_string(),
                file_path: "/work/src/main.rs".to_string(),
                started_at_ms: 6_000,
            },
            LiveTool {
                id: "c-2".to_string(),
                name: "Bash".to_string(),
                category: "exec",
                label: "npm test".to_string(),
                action: "running command".to_string(),
                summary: "Run the tests".to_string(),
                file_path: String::new(),
                started_at_ms: 7_000,
            },
        ];
        let with_tools = summary(&record(), &followed, "claude", Some(&newest), None, true);
        let doing = &with_tools["live_activity"];
        assert_eq!(
            (&doing["state"], &doing["headline"], &doing["running_tools"]),
            (&json!("tool"), &json!("Running npm test"), &json!(2))
        );
        assert_eq!(
            (
                &doing["since_ms"],
                &doing["item_id"],
                &doing["turn_started_ms"]
            ),
            (&json!(7_000), &json!("c-2"), &json!(5_000))
        );
        assert_eq!(
            doing["tool"],
            json!({
                "name": "Bash", "call_id": "c-2", "label": "npm test", "category": "exec",
                "item_id": "c-2", "started_at_ms": 7_000,
            })
        );
        // The event of the tool state sums the same call up in its own, older words.
        let event = &with_tools["activity"];
        assert_eq!(
            (&event["action"], &event["summary"], &event["tool"]),
            (
                &json!("running command"),
                &json!("Run the tests"),
                &json!("Bash")
            )
        );
        assert_eq!(
            (&event["call_id"], &event["started_at_ms"], &event["ts"]),
            (&json!("c-2"), &json!(7_000), &json!(7_000))
        );
        assert_eq!(
            (&row["activity"]["action"], &row["activity"]["summary"]),
            (&json!("running command"), &json!("Running shell command"))
        );
        assert_eq!(
            (&row["model"], &row["context_tokens"]),
            (&json!("fable"), &json!(5))
        );
        assert_eq!(
            row.as_object().unwrap().len(),
            54,
            "the fields of the Python Host's row"
        );
        let typed: AgentSummary = serde_json::from_value(row).unwrap();
        assert_eq!(typed.session, "maximus");
    }

    #[test]
    fn the_row_of_an_agent_that_never_ran_and_of_one_that_was_stopped() {
        let mut fresh = status(None, None);
        fresh.backend_session = None;
        fresh.head_revision = 0;
        let row = summary(&record(), &fresh, "claude", None, None, false);
        assert_eq!(row["latest_state"], "spawned");
        assert_eq!(
            (
                &row["activity"]["action"],
                &row["activity"]["summary"],
                &row["activity"]["state"]
            ),
            (&json!("started"), &json!("Started"), &json!("idle"))
        );
        assert!(
            row["live_activity"].is_null(),
            "nothing to show of a turn that never was"
        );
        assert_eq!(
            (&row["conversation_id"], &row["last_message"]),
            (&json!(""), &json!(""))
        );
        assert_eq!(
            (&row["last_turn_end"], &row["turn_started_at"]),
            (&json!(0), &json!(0))
        );
        assert_eq!(row["latest_state_ts"], 1, "as it is since it was made");

        let halted = status(
            None,
            Some(Outcome::Stopped {
                by: "user".to_string(),
            }),
        );
        let row = summary(&record(), &halted, "claude", None, None, false);
        assert_eq!(row["latest_state"], "interrupted");
        assert_eq!(
            (
                &row["live_activity"]["state"],
                &row["live_activity"]["headline"]
            ),
            (&json!("interrupted"), &json!("Interrupted"))
        );
        assert_eq!(
            row["turn_started_at"], 0,
            "at rest no turn runs, however the last one ended"
        );
        assert_eq!(row["last_turn_end"], 0);

        let done = summary(
            &record(),
            &status(None, Some(Outcome::Completed)),
            "claude",
            None,
            Some(&ended()),
            false,
        );
        assert_eq!(
            (&done["last_turn_end"], &done["turn_started_at"]),
            (&json!(4), &json!(0))
        );
        assert_eq!(done["live_activity"]["state"], "idle");
        assert!(done["live_activity"]["headline"].is_null());
    }

    #[test]
    fn events_have_the_shapes_the_apps_read() {
        let thinking = status(Some(Activity::Thinking), None);
        let live = thinking.live.clone().unwrap();
        let sent = dispatched(&record(), &live, 1);
        assert_eq!(
            sent,
            json!({"type": "agent-state", "agent_id": "a1", "session": "maximus", "kind": "thinking",
                   "client_msg_id": "m2", "trace_id": "trace-2", "queue_started": false, "queue_remaining": 1})
        );
        // A turn that an answer or a goal began says so, and one that came through the queue.
        let woken = LiveStatus {
            origin: Origin::Trigger {
                name: "automation".to_string(),
            },
            from_queue: true,
            ..live.clone()
        };
        assert_eq!(dispatched(&record(), &woken, 0)["queue_started"], true);
        let by_itself = Status {
            live: Some(woken),
            ..thinking.clone()
        };
        assert_eq!(
            state(&record(), &by_itself, "claude", None)["detail"]["origin"],
            "automation"
        );
        let told = state(&record(), &thinking, "claude", None);
        assert_eq!(
            (&told["kind"], &told["status_text"]),
            (&json!("thinking"), &json!(""))
        );
        assert_eq!(
            told["detail"],
            json!({"backend_session_id": "sess-1", "dispatch": "claude", "origin": "user", "source": "pwa", "trace_id": "trace-2"})
        );
        let doing = activity(&record(), &thinking);
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
            state(&record(), &done, "claude", Some(&ended()))["detail"],
            json!({"dispatch": "clarp", "duration_ms": 2_900, "origin": "user", "trace_id": "trace-1", "tokens_in": 5, "tokens_out": 7})
        );
        assert_eq!(
            state(&record(), &done, "scripted", None)["detail"]["dispatch"],
            "scripted"
        );
        assert_eq!(activity(&record(), &done)["summary"], "Done");
        assert!(stopped(&record(), &done).is_none());

        let halted = status(
            None,
            Some(Outcome::Stopped {
                by: "user".to_string(),
            }),
        );
        let at_once = stopped(&record(), &halted).unwrap();
        assert_eq!(at_once["kind"], "interrupted");
        assert_eq!(at_once["detail"]["source"], "user_stop");
        assert!(
            at_once.get("status_text").is_none(),
            "the route's word has no status text"
        );
        let after = activity(&record(), &halted);
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

        let held = queue_updated(&record(), &thinking, None, false);
        assert_eq!(
            (
                &held["queue_depth"],
                &held["queue_paused"],
                &held["queue_revision"],
                &held["queue_started"]
            ),
            (&json!(1), &json!(true), &json!(7), &json!(false))
        );
        assert!(held.get("client_msg_id").is_none());
        let left = queue_updated(&record(), &thinking, Some("u-m2"), true);
        assert_eq!(
            (&left["client_msg_id"], &left["queue_started"]),
            (&json!("m2"), &json!(true))
        );
        // Each is an event of the contract: it reads as the type the apps are generated from.
        for (event, is) in [
            (sent, "agent-state"),
            (doing, "agent-activity"),
            (left, "queue-updated"),
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
                origin: Origin::User,
                id: "r-1".to_string(),
                turn: 1,
                text: "All done.".to_string(),
                trace_id: None,
            }),
            9,
            None,
        );
        let from = |client: &'static str| Asked {
            message_id: "u-1",
            client,
        };
        let told = user_notification(&record(), "pn-abc", &from(""), &reply, 4_000).unwrap();
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

        // A user who asked at the desk is told, and not paged.
        let at_the_desk =
            user_notification(&record(), "pn-abc", &from("desktop"), &reply, 4_000).unwrap();
        assert_eq!(
            (&at_the_desk["reason"], &at_the_desk["push"]),
            (&json!("text-reply-desktop"), &json!(false))
        );
        // What one agent answers another tells nobody.
        let between_agents = version(
            Entry::Reply(Reply {
                origin: Origin::Agent {
                    agent_id: "a2".to_string(),
                },
                id: "r-2".to_string(),
                turn: 2,
                text: "Done as you asked.".to_string(),
                trace_id: None,
            }),
            12,
            None,
        );
        assert_eq!(
            user_notification(&record(), "pn-abc", &from(""), &between_agents, 4_000),
            None
        );
        // What the Host brought the agent for its user (an answer, a goal) is told.
        let by_itself = version(
            Entry::Reply(Reply {
                origin: Origin::Trigger {
                    name: "automation".to_string(),
                },
                id: "r-3".to_string(),
                turn: 3,
                text: "The goal is reached.".to_string(),
                trace_id: None,
            }),
            15,
            None,
        );
        let told = user_notification(&record(), "pn-abc", &from(""), &by_itself, 4_000).unwrap();
        assert_eq!(
            (&told["origin"], &told["push"]),
            (&json!("automation"), &json!(true))
        );
    }

    #[test]
    fn what_a_watcher_or_the_hosts_routine_began_is_marked_and_shown_as_such() {
        let from = |name: &str| Origin::Trigger {
            name: name.to_string(),
        };
        let asked = |origin: Origin, text: &str| {
            version(
                Entry::Message(Message {
                    id: "u-1".to_string(),
                    text: text.to_string(),
                    origin,
                    trace_id: None,
                    sender: None,
                    client: None,
                }),
                1,
                None,
            )
        };
        let said = |origin: Origin, text: &str| {
            version(
                Entry::Reply(Reply {
                    origin,
                    id: "r-1".to_string(),
                    turn: 1,
                    text: text.to_string(),
                    trace_id: None,
                }),
                2,
                None,
            )
        };
        let event = row(&asked(from("watcher"), "poll the synthetic queue"));
        assert_eq!(
            (
                &event["text"],
                &event["automated"],
                &event["automation_kind"]
            ),
            (
                &json!("Automated watcher event"),
                &json!(true),
                &json!("watcher")
            )
        );
        let nothing = row(&said(from("heartbeat"), "HEARTBEAT_OK"));
        assert_eq!(nothing["text"], "Heartbeat check: no action needed.");
        let something = row(&said(from("heartbeat"), "The disk is full."));
        assert_eq!(
            (&something["text"], &something["automated"]),
            (&json!("The disk is full."), &json!(true))
        );
        // What the Host brings an agent for its user is no routine: shown as it is.
        let brought = row(&asked(from("automation"), "[Clarp decision resolved]"));
        assert_eq!(
            (
                &brought["text"],
                &brought["automated"],
                &brought["automation_kind"]
            ),
            (
                &json!("[Clarp decision resolved]"),
                &json!(false),
                &json!("")
            )
        );
        // A list of chats shows the last thing someone said, and nothing automated.
        assert!(!previewed(&said(from("heartbeat"), "The disk is full.")));
        assert!(!previewed(&asked(from("watcher"), "poll")));
        assert!(previewed(&asked(from("automation"), "The answer is yes.")));
        assert!(previewed(&said(Origin::User, "Done.")));
        assert!(!previewed(&said(Origin::User, "")));
    }

    #[test]
    fn a_notification_says_what_was_said_aloud_or_else_what_was_written() {
        assert_eq!(
            notification_preview(
                "Here is the table.\n<speak>Done, <vox>um,</vox> two files changed.</speak>\n| a | b |"
            ),
            Some(("Done, two files changed.".to_string(), "speak")),
            "what the agent said aloud, as a screen shows it"
        );
        assert_eq!(
            notification_preview("All <break time=\"300ms\"/> done."),
            Some(("All done.".to_string(), "text-reply"))
        );
        assert_eq!(
            notification_preview("<speak>Good. [sigh] Finally done.</speak>"),
            Some(("Good. Finally done.".to_string(), "speak")),
            "what the engine acts out is not shown"
        );
        assert_eq!(notification_preview("HEARTBEAT_OK"), None);
        assert_eq!(
            notification_preview("Heartbeat check: no action needed."),
            None
        );
        assert_eq!(notification_preview("<team>only for the team</team>"), None);
        let long = "word ".repeat(60);
        let (preview, _) = notification_preview(&long).unwrap();
        assert_eq!(preview.chars().count(), 180);
        assert!(preview.ends_with('…'));
    }

    #[test]
    fn a_preview_is_one_line_without_markup_and_not_long() {
        let reply = |text: &str| {
            version(
                Entry::Reply(Reply {
                    origin: Origin::User,
                    id: "r-1".to_string(),
                    turn: 1,
                    text: text.to_string(),
                    trace_id: None,
                }),
                9,
                None,
            )
        };
        assert_eq!(
            preview(&reply(
                "<speak>Hi <vox>um</vox> there.</speak>\n\nSecond line."
            )),
            "Hi there. Second line."
        );
        let long = preview(&reply(&"abcdefghi ".repeat(20)));
        assert_eq!(long.chars().count(), 80, "{long}");
        assert!(long.ends_with('…') && !long.contains(" …"));
        assert_eq!(preview(&reply("<team>x</team>")), "");
        // A row never carries what is metadata.
        let shown = row(&reply("Visible.\n<oai-mem-citation>c</oai-mem-citation>"));
        assert_eq!(shown["text"], "Visible.\n");
    }
}
