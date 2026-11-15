//! The agents' own tools, as the Host answers them.
//!
//! An agent reaches its Host through tools that its skills call by name: `clarp-agent-bg`,
//! `clarp-goal` and the others. Here a tool is a name and a pipe. The program an agent runs
//! sends what it was called with (its arguments, where it stood, who started it) to the Host,
//! prints what the Host answers, and exits with the code the Host names. What the tool *does*
//! is decided in this module, in one place, so a tool and its Host cannot disagree.
//!
//! The command lines are the Python Host's, argument for argument and exit code for exit code:
//! the skills that call them were written against those.

use std::collections::BTreeMap;
use std::sync::Arc;

use clarp_kernel::jobs::{JobChange, JobCommand, JobRefusal, JobStatus, Worker, parse_handle};
use serde::{Deserialize, Serialize};
use serde_json::Value;

use crate::agent::Unavailable;
use crate::edge::App;
use crate::fleet::Member;
use crate::{ids, process};

/// What a tool was called with.
#[derive(Debug, Clone, Default, Deserialize)]
pub struct ToolCall {
    /// The name it was called by.
    pub tool: String,
    /// Its arguments, without its own name.
    #[serde(default)]
    pub argv: Vec<String>,
    /// The directory it was called in.
    #[serde(default)]
    pub cwd: String,
    /// What of its environment the Host may need to know.
    #[serde(default)]
    pub env: BTreeMap<String, String>,
    /// The process that called it.
    #[serde(default)]
    pub ppid: u32,
}

/// What the tool prints, and the code it exits with.
#[derive(Debug, Clone, PartialEq, Eq, Default, Serialize)]
pub struct ToolAnswer {
    pub exit: i32,
    pub stdout: String,
    pub stderr: String,
}

impl ToolAnswer {
    fn code(exit: i32) -> Self {
        Self {
            exit,
            ..Self::default()
        }
    }

    /// Exits with a line on standard error.
    fn said(exit: i32, line: impl AsRef<str>) -> Self {
        Self {
            exit,
            stdout: String::new(),
            stderr: format!("{}\n", line.as_ref()),
        }
    }

    /// Exits with a line on standard output.
    fn printed(exit: i32, line: impl AsRef<str>) -> Self {
        Self {
            exit,
            stdout: format!("{}\n", line.as_ref()),
            stderr: String::new(),
        }
    }
}

/// The exit code of a tool this Host does not have: "service unavailable".
pub const NOT_HERE: i32 = 69;

/// Answers a tool.
pub async fn run(app: &App, call: &ToolCall) -> ToolAnswer {
    match call.tool.as_str() {
        "clarp-agent-bg" => agent_bg(app, call).await,
        other => ToolAnswer::said(
            NOT_HERE,
            format!("{other}: not available on this Host yet (the Rust Host). Nothing was done."),
        ),
    }
}

const BG_USAGE: &str = "usage: agent_bg.py <session> on|off [label] | \
job-upsert|job-restart|job-start|job-heartbeat|job-finish|\
job-fail|job-active|job-cancelled|job-cancel|job-progress|job-log ...";

/// The environment variable a detached worker names itself with.
const WORKER_PID_ENV: &str = "CLARP_BACKGROUND_WORKER_PID";

/// The process that speaks for the jobs this call registers or reports on.
///
/// A worker that names itself is taken at its word. Without that, only the one detached
/// workflow that was adopted this way counts as a worker by being the caller: the message
/// watch. Anything else reports without a name, and is held to its job's generation alone.
fn worker_of(call: &ToolCall) -> Worker {
    let explicit = call
        .env
        .get(WORKER_PID_ENV)
        .map(|value| value.trim())
        .filter(|value| !value.is_empty() && value.bytes().all(|b| b.is_ascii_digit()))
        .and_then(|value| value.parse::<u32>().ok());
    let pid = explicit.unwrap_or(call.ppid);
    if explicit.is_none() && !process::command_line(pid).contains("watch_messages.py") {
        return Worker::none();
    }
    Worker {
        pid,
        start_token: process::start_token(pid),
    }
}

/// A text as the Python tools show it inside a message.
fn quoted(text: &str) -> String {
    format!("'{text}'")
}

fn could_not(error: &Unavailable) -> ToolAnswer {
    ToolAnswer::said(
        1,
        format!("agent_bg: the Host could not do it: {}", error.0),
    )
}

/// The agent a job belongs to: the one that registered it, or, for a job nobody registered
/// here, the one the tool was called for.
async fn owner_of(app: &App, job_id: &str, session: &str) -> Option<Arc<Member>> {
    if let Ok(Some(agent_id)) = app.fleet.job_owner(job_id.to_string()).await {
        return app
            .fleet
            .directory()
            .members
            .iter()
            .find(|member| member.record.agent_id == agent_id)
            .cloned();
    }
    app.member(session)
}

/// Asks the job's owner to carry out a command. `None` when there is no agent to ask.
async fn ask(
    app: &App,
    job_id: &str,
    session: &str,
    command: JobCommand,
) -> Option<Result<Result<JobChange, JobRefusal>, Unavailable>> {
    let member = owner_of(app, job_id, session).await?;
    Some(member.agent.job(command).await)
}

/// `clarp-agent-bg SESSION VERB ...`: an agent's background jobs, and its own status label.
async fn agent_bg(app: &App, call: &ToolCall) -> ToolAnswer {
    let argv: Vec<&str> = call.argv.iter().map(String::as_str).collect();
    let (Some(&session), Some(&verb)) = (argv.first(), argv.get(1)) else {
        return ToolAnswer::said(2, BG_USAGE);
    };
    // The verbs that name a run by its handle.
    let run = argv.get(2).map(|handle| parse_handle(handle));
    let handle = argv.get(2).copied().unwrap_or("");
    match (verb, run) {
        ("job-upsert" | "job-restart", _) => {
            if argv.len() < 5 {
                return ToolAnswer::said(
                    2,
                    format!("usage: agent_bg.py <session> {verb} <job-id> <kind> <title> [detail]"),
                );
            }
            let Some(member) = app.member(session) else {
                return ToolAnswer::said(1, format!("agent_bg: unknown session: {session}"));
            };
            let id = match argv[2].trim() {
                "" => ids::hex(16),
                id => id.to_string(),
            };
            match app
                .fleet
                .claim_job(id.clone(), member.record.agent_id.clone())
                .await
            {
                Err(error) => return could_not(&error),
                Ok(Err(_)) => {
                    return ToolAnswer::said(
                        1,
                        format!("agent_bg: job id already belongs to another agent: {id}"),
                    );
                }
                Ok(Ok(())) => {}
            }
            // The run belongs to the owner's turn that registered it, when it was one.
            let started_trace_id = member
                .agent
                .view()
                .status
                .live
                .as_ref()
                .and_then(|live| live.trace_id.clone())
                .unwrap_or_default();
            let command = JobCommand::Register {
                id,
                kind: argv[3].to_string(),
                title: argv[4].to_string(),
                detail: argv.get(5).copied().unwrap_or("").to_string(),
                worker: worker_of(call),
                restart_cancelled: verb == "job-restart",
                started_trace_id,
                heartbeat_timeout_ms: None,
                metadata: Value::Null,
            };
            match member.agent.job(command).await {
                Err(error) => could_not(&error),
                Ok(Err(refusal)) => ToolAnswer::said(1, format!("agent_bg: {refusal}")),
                Ok(Ok(change)) => match change.job {
                    Some(job) => {
                        ToolAnswer::printed(i32::from(!job.status.is_active()), job.handle())
                    }
                    None => ToolAnswer::code(1),
                },
            }
        }
        ("job-start", Some((id, generation))) => {
            let command = JobCommand::Start {
                id: id.clone(),
                generation: Some(generation),
                worker: Worker::none(),
            };
            let running = matches!(
                ask(app, &id, session, command).await,
                Some(Ok(Ok(JobChange { job: Some(job), .. }))) if job.status == JobStatus::Running
            );
            ToolAnswer::code(i32::from(!running))
        }
        ("job-heartbeat", Some((id, generation))) => {
            let command = JobCommand::Heartbeat {
                id: id.clone(),
                generation: Some(generation),
                worker: worker_of(call),
                source: "worker_heartbeat".to_string(),
            };
            let active = matches!(
                ask(app, &id, session, command).await,
                Some(Ok(Ok(JobChange { job: Some(job), .. }))) if job.status.is_active()
            );
            ToolAnswer::code(i32::from(!active))
        }
        ("job-finish" | "job-fail", Some((id, generation))) => {
            let failed = (verb == "job-fail")
                .then(|| argv.get(3).copied().unwrap_or("worker_failed").to_string());
            let command = JobCommand::Finish {
                id: id.clone(),
                generation,
                worker: worker_of(call),
                failed,
            };
            let taken = matches!(
                ask(app, &id, session, command).await,
                Some(Ok(Ok(JobChange { job: Some(_), .. })))
            );
            ToolAnswer::code(i32::from(!taken))
        }
        ("job-active", Some((id, generation))) => {
            // 0: it runs, and this is its run. 1: it is over, or this run no longer speaks
            // for it. 2: not known, and whoever asks must wait rather than act.
            let worker = worker_of(call);
            let command = JobCommand::Heartbeat {
                id: id.clone(),
                generation: Some(generation),
                worker: worker.clone(),
                source: "worker_heartbeat".to_string(),
            };
            let unknown = || ToolAnswer::said(2, "agent_bg: job state unknown; delivery must wait");
            let Some(member) = owner_of(app, &id, session).await else {
                return unknown();
            };
            match member.agent.job(command).await {
                Ok(Ok(JobChange { job: Some(job), .. })) => ToolAnswer::code(i32::from(
                    job.status.is_terminal() || job.generation != generation,
                )),
                Ok(Ok(_)) => {
                    // Refused: by the fence, or because there is no such job. Only the fence
                    // is an answer.
                    let fenced = member.agent.view().jobs.iter().any(|job| {
                        job.id == id
                            && (job.generation != generation
                                || job.status.is_terminal()
                                || (job.worker.pid != 0 && job.worker.pid != worker.pid)
                                || (!job.worker.start_token.is_empty()
                                    && job.worker.start_token != worker.start_token))
                    });
                    if fenced {
                        ToolAnswer::code(1)
                    } else {
                        unknown()
                    }
                }
                Ok(Err(_)) | Err(_) => {
                    ToolAnswer::said(2, "agent_bg: job state unavailable: the Host could not say")
                }
            }
        }
        ("job-cancelled", Some((id, generation))) => {
            let cancelled = owner_of(app, &id, session).await.is_some_and(|member| {
                member.agent.view().jobs.iter().any(|job| {
                    job.id == id
                        && job.generation == generation
                        && job.status == JobStatus::Cancelled
                })
            });
            ToolAnswer::code(i32::from(!cancelled))
        }
        ("job-cancel", Some((id, generation))) => {
            // The owner closes its own job, whether or not the worker that registered it is
            // still there to be asked.
            let nothing = |what: &str| {
                ToolAnswer::said(
                    1,
                    format!(
                        "agent_bg: {what} mismatch for {}; nothing cancelled",
                        quoted(handle)
                    ),
                )
            };
            let none = || {
                ToolAnswer::said(
                    1,
                    format!(
                        "agent_bg: {} has no cancellable job {}",
                        quoted(session),
                        quoted(handle)
                    ),
                )
            };
            let Some(member) = app.member(session) else {
                return none();
            };
            match app.fleet.job_owner(id.clone()).await {
                Ok(Some(agent_id)) if agent_id != member.record.agent_id => {
                    return nothing("session");
                }
                Ok(_) => {}
                Err(error) => return could_not(&error),
            }
            let command = JobCommand::Cancel {
                id,
                expected_generation: Some(generation),
                reason: "owner_cancelled".to_string(),
            };
            match member.agent.job(command).await {
                Err(error) => could_not(&error),
                Ok(Ok(change)) if change.generation_mismatch => nothing("generation"),
                Ok(Ok(JobChange { job: Some(job), .. })) if job.status == JobStatus::Cancelled => {
                    ToolAnswer::code(0)
                }
                Ok(_) => none(),
            }
        }
        ("job-progress", Some((id, generation))) => {
            let Some(text) = argv.get(3) else {
                return ToolAnswer::said(2, BG_USAGE);
            };
            let Some(member) = app.member(session) else {
                return ToolAnswer::code(1);
            };
            let command = JobCommand::Progress {
                id,
                generation,
                text: (*text).to_string(),
            };
            let taken = matches!(
                member.agent.job(command).await,
                Ok(Ok(JobChange { job: Some(_), .. }))
            );
            ToolAnswer::code(i32::from(!taken))
        }
        ("job-log", Some((id, generation))) => {
            let Some(path) = argv.get(3) else {
                return ToolAnswer::said(2, BG_USAGE);
            };
            let Some(member) = app.member(session) else {
                return ToolAnswer::code(1);
            };
            let command = JobCommand::SetLog {
                id,
                generation,
                path: (*path).to_string(),
                worker_cwd: call.cwd.clone(),
            };
            match member.agent.job(command).await {
                Ok(Err(refusal)) => ToolAnswer::said(2, format!("agent_bg: {refusal}")),
                Ok(Ok(JobChange { job: Some(_), .. })) => ToolAnswer::code(0),
                _ => ToolAnswer::code(1),
            }
        }
        ("on" | "off", _) => ToolAnswer::said(
            NOT_HERE,
            "agent_bg: a status label is not available on this Host yet (the Rust Host). Nothing was done.",
        ),
        _ => ToolAnswer::said(2, BG_USAGE),
    }
}
