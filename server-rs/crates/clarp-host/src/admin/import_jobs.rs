//! Offline carry of the Python Host's running background jobs to the agents that moved here.
//!
//! An agent's jobs do not move with it, but their workers keep running and ask
//! `clarp-agent-bg <session> job-active <handle>` every few seconds. A job written here as it
//! stood there (the same id, run, worker and start token) answers them 0, and their heartbeats
//! land; a job registered afresh would fence them and they would end themselves.

use std::collections::{HashMap, HashSet};
use std::path::{Path, PathBuf};

use clarp_kernel::jobs::{Job, JobRecord, JobStatus, Worker};
use clarp_kernel::journal::kind;
use clarp_ports::clock::{Clock, SystemClock};
use clarp_ports::kv::{KeyValue, StoreError, Write};
use clarp_ports::log::{AgentLog, LogEngine, NewItem};
use clarp_store_sqlite::{LOG_FILE, SqliteEngine, SqliteKeyValue};
use rusqlite::{Connection, OpenFlags};
use serde_json::Value;

use crate::fleet::{AGENTS, JOBS};
use crate::process;

use super::offline_import::{self, Files};

/// What the timeline of a carried job says of its first entry here.
const NOTE: &str = "carried over from the Python Host";

pub(super) fn run(args: impl Iterator<Item = String>) -> Result<String, String> {
    let files = Files::parse(args, "job import", true)?;
    let data = files
        .target
        .parent()
        .ok_or("the Rust Host's data directory was not found")?;
    let rows = read_source(&files.source)?;
    let now_ms = SystemClock.now_ms();
    let lines = if files.dry_run {
        look(&rows, &files.target, data, now_ms)?
    } else {
        carry(&rows, &files.target, data, now_ms)?
    };
    Ok(lines.join("\n"))
}

/// A running job of the Python Host, as its row has it.
struct Row {
    job: Job,
    agent_id: String,
    session: String,
    owner_kind: String,
}

fn read_source(path: &Path) -> Result<Vec<Row>, String> {
    let conn = offline_import::source(path)?;
    let unreadable = |_| "the Python background_jobs table could not be read".to_string();
    let mut select = conn
        .prepare(
            "SELECT job_id, agent_id, session, kind, title, detail, started_at, updated_at,
                    metadata_json, heartbeat_at, heartbeat_timeout_ms, heartbeat_source,
                    COALESCE(worker_pid, 0), worker_start_token, generation, progress_text,
                    progress_at, log_path, worker_cwd, started_trace_id, owner_kind
               FROM background_jobs
              WHERE status IN ('running', 'active') AND terminal_at IS NULL
              ORDER BY session, job_id",
        )
        .map_err(unreadable)?;
    let mut rows = select.query([]).map_err(unreadable)?;
    let mut found = Vec::new();
    while let Some(row) = rows.next().map_err(unreadable)? {
        found.push(source_row(row).map_err(|_| "a Python job row is invalid")?);
    }
    Ok(found)
}

fn source_row(row: &rusqlite::Row<'_>) -> Result<Row, rusqlite::Error> {
    let metadata: String = row.get(8)?;
    let pid: i64 = row.get(12)?;
    let generation: i64 = row.get(14)?;
    let job = Job {
        id: row.get(0)?,
        kind: row.get(3)?,
        title: row.get(4)?,
        detail: row.get(5)?,
        status: JobStatus::Running,
        generation: u64::try_from(generation).unwrap_or(1).max(1),
        revision: 0,
        started_at_ms: row.get(6)?,
        updated_at_ms: row.get(7)?,
        heartbeat_at_ms: row.get(9)?,
        heartbeat_timeout_ms: row.get(10)?,
        heartbeat_source: row.get(11)?,
        worker: Worker {
            pid: u32::try_from(pid).unwrap_or(0),
            start_token: row.get(13)?,
        },
        terminal_at_ms: None,
        terminal_reason: String::new(),
        cancelled_at_ms: None,
        progress_text: row.get(15)?,
        progress_at_ms: row.get(16)?,
        log_path: row.get(17)?,
        worker_cwd: row.get(18)?,
        started_trace_id: row.get(19)?,
        metadata: serde_json::from_str(&metadata).unwrap_or(Value::Null),
    };
    Ok(Row {
        job,
        agent_id: row.get(1)?,
        session: row.get(2)?,
        owner_kind: row.get(20)?,
    })
}

/// The rows of one table of the fleet store.
type Table<'a> = dyn Fn(&str) -> Result<Vec<(String, Vec<u8>)>, String> + 'a;

/// What the Rust store already has: its agents, who owns which job id, and its own jobs.
struct Here {
    /// Agents that are not deleted, by id: their session.
    agents: HashMap<String, String>,
    owners: HashMap<String, String>,
    computer: HashSet<String>,
}

impl Here {
    fn read(list: &Table<'_>) -> Result<Self, String> {
        let mut agents = HashMap::new();
        for (id, body) in list(AGENTS)? {
            let Ok(record) = serde_json::from_slice::<Value>(&body) else {
                continue;
            };
            if record["deleted_at_ms"].is_null()
                && let Some(session) = record["session"].as_str()
            {
                agents.insert(id, session.to_string());
            }
        }
        let owners = list(JOBS)?
            .into_iter()
            .map(|(id, agent)| (id, String::from_utf8_lossy(&agent).into_owned()))
            .collect();
        let computer = list(crate::fleet::computer_jobs::TABLE)?
            .into_iter()
            .map(|(id, _)| id)
            .collect();
        Ok(Self {
            agents,
            owners,
            computer,
        })
    }
}

/// What becomes of one row.
enum Step {
    Carry,
    There,
    Skip(String),
}

/// Decides one row. `in_journal` says whether the agent's own log already has the job.
fn decide(
    row: &Row,
    here: &Here,
    now_ms: i64,
    in_journal: &mut dyn FnMut(&Row) -> Result<bool, String>,
) -> Result<Step, String> {
    let job = &row.job;
    if job.id.trim().starts_with("claude-task:") {
        return Ok(Step::Skip(
            "a provider task, which the Rust Host follows by itself".into(),
        ));
    }
    if row.owner_kind != "agent" {
        return Ok(Step::Skip("a job of the Python Host itself".into()));
    }
    let worker = worker_state(job, now_ms);
    if !here.agents.contains_key(&row.agent_id) {
        let worker = match &worker {
            Ok(said) => said.clone(),
            Err(why) => why.clone(),
        };
        return Ok(Step::Skip(format!(
            "its agent is not on the Rust Host ({worker})"
        )));
    }
    if here.computer.contains(&job.id) {
        return Ok(Step::Skip(
            "the id belongs to a job of the Rust Host itself".into(),
        ));
    }
    match here.owners.get(&job.id) {
        Some(owner) if *owner != row.agent_id => {
            return Ok(Step::Skip(
                "the id belongs to another agent on the Rust Host".into(),
            ));
        }
        Some(_) => return Ok(Step::There),
        None => {}
    }
    if in_journal(row)? {
        return Ok(Step::There);
    }
    match worker {
        Ok(_) => Ok(Step::Carry),
        Err(why) => Ok(Step::Skip(why)),
    }
}

/// Whether the job's worker still runs: `Ok` with what was seen, or `Err` with why it is gone.
fn worker_state(job: &Job, now_ms: i64) -> Result<String, String> {
    let Worker { pid, start_token } = &job.worker;
    if *pid == 0 {
        // Nobody is named to ask after: it is as alive as it was last heard.
        let silent_ms = now_ms - job.heartbeat_at_ms.unwrap_or(job.updated_at_ms);
        return if silent_ms <= job.heartbeat_timeout_ms {
            Ok("no worker named, heard from in time".into())
        } else {
            Err(format!(
                "no worker named, and not heard from for {} s",
                silent_ms / 1_000
            ))
        };
    }
    let live = process::start_token(*pid);
    if live.is_empty() {
        return Err(format!("its worker process {pid} is gone"));
    }
    if !start_token.is_empty() && live != *start_token {
        return Err(format!(
            "pid {pid} is another process now: its start token differs"
        ));
    }
    if !start_token.is_empty() && !process::is_alive(*pid, start_token) {
        return Err(format!("its worker process {pid} has ended"));
    }
    Ok(format!("worker {pid} runs"))
}

fn said(row: &Row, step: &Step, dry_run: bool) -> String {
    let (session, job) = (&row.session, &row.job);
    match step {
        Step::Carry => format!(
            "{} {session} {} (run {}, {})",
            if dry_run { "would carry" } else { "carried" },
            job.id,
            job.generation,
            match job.worker.pid {
                0 => "no worker named".to_string(),
                pid => format!("worker {pid}"),
            }
        ),
        Step::There => format!("already there {session} {}", job.id),
        Step::Skip(why) => format!("skipped {session} {}: {why}", job.id),
    }
}

fn tally(steps: &[Step], dry_run: bool) -> String {
    let count = |want: fn(&Step) -> bool| steps.iter().filter(|step| want(step)).count();
    format!(
        "{} {}, {} skipped, {} already there",
        count(|step| matches!(step, Step::Carry)),
        if dry_run {
            "would be carried"
        } else {
            "carried"
        },
        count(|step| matches!(step, Step::Skip(_))),
        count(|step| matches!(step, Step::There)),
    )
}

fn agent_dir(data: &Path, agent_id: &str) -> PathBuf {
    data.join("agents").join(agent_id)
}

fn codecs() -> Result<clarp_ports::codec::CodecSet, String> {
    clarp_codec::standard_set("zstd").ok_or_else(|| "no zstd codec in this build".into())
}

/// The dry run: reads the Rust store read-only and writes nothing.
fn look(rows: &[Row], fleet: &Path, data: &Path, now_ms: i64) -> Result<Vec<String>, String> {
    let conn = Connection::open_with_flags(
        fleet,
        OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX,
    )
    .map_err(|_| "the Rust fleet.db could not be opened read-only")?;
    let list = |table: &str| -> Result<Vec<(String, Vec<u8>)>, String> {
        let unreadable = |_| "the Rust fleet.db could not be read".to_string();
        let mut select = conn
            .prepare_cached("SELECT key, value FROM kv WHERE tbl = ?1 ORDER BY key")
            .map_err(unreadable)?;
        let found = select
            .query_map([table], |row| Ok((row.get(0)?, row.get(1)?)))
            .map_err(unreadable)?;
        found.collect::<Result<_, _>>().map_err(unreadable)
    };
    let here = Here::read(&list)?;
    let codecs = codecs()?;
    let mut logs: HashMap<String, Option<Box<dyn AgentLog>>> = HashMap::new();
    let mut in_journal = |row: &Row| -> Result<bool, String> {
        let dir = agent_dir(data, &row.agent_id);
        let log = match logs.entry(row.agent_id.clone()) {
            std::collections::hash_map::Entry::Occupied(log) => log.into_mut(),
            std::collections::hash_map::Entry::Vacant(slot) => {
                // A log that is not there is not made by a dry run.
                let log = if dir.join(LOG_FILE).is_file() {
                    Some(
                        SqliteEngine::new()
                            .open(&dir, codecs.clone())
                            .map_err(|e| format!("the agent's log could not be opened: {e}"))?,
                    )
                } else {
                    None
                };
                slot.insert(log)
            }
        };
        has_job(log.as_deref(), &row.job.id)
    };
    let mut lines = Vec::new();
    let mut steps = Vec::new();
    for row in rows {
        let step = decide(row, &here, now_ms, &mut in_journal)?;
        lines.push(said(row, &step, true));
        steps.push(step);
    }
    lines.push(tally(&steps, true));
    Ok(lines)
}

fn has_job(log: Option<&dyn AgentLog>, id: &str) -> Result<bool, String> {
    let Some(log) = log else {
        return Ok(false);
    };
    let found = log
        .find(&format!("j-{id}"))
        .map_err(|e| format!("the agent's log could not be read: {e}"))?;
    Ok(found.is_some_and(|item| item.kind == kind::JOB))
}

/// The import: each job written to its agent's log as the Host writes a job, and its id
/// claimed for the agent in the fleet store, as `job-upsert` claims it. A job whose log could
/// not be written is said and the others go on; the command then ends in an error.
fn carry(rows: &[Row], fleet: &Path, data: &Path, now_ms: i64) -> Result<Vec<String>, String> {
    let codecs = codecs()?;
    let (lines, failed) = SqliteKeyValue::update_existing(fleet, |store: &dyn KeyValue| {
        let list = |table: &str| store.list(table).map_err(|error| error.0);
        let here = Here::read(&list).map_err(StoreError)?;
        let mut logs: HashMap<String, Box<dyn AgentLog>> = HashMap::new();
        let (mut lines, mut steps, mut writes, mut failed) =
            (Vec::new(), Vec::new(), Vec::<Write>::new(), 0);
        for (n, row) in rows.iter().enumerate() {
            let mut in_journal = |row: &Row| -> Result<bool, String> {
                let log = log_of(&mut logs, data, row, &codecs)?;
                has_job(Some(log.as_ref()), &row.job.id)
            };
            let step = match decide(row, &here, now_ms, &mut in_journal) {
                Ok(step) => step,
                Err(why) => {
                    failed += 1;
                    lines.push(format!("failed {} {}: {why}", row.session, row.job.id));
                    continue;
                }
            };
            if matches!(step, Step::Carry) {
                let mut job = row.job.clone();
                // Above every number the Host gave out before now, as its own next one is.
                job.revision = u64::try_from(now_ms)
                    .unwrap_or(0)
                    .saturating_mul(1_000)
                    .saturating_add(n as u64);
                let record = JobRecord {
                    job,
                    at_ms: now_ms,
                    note: NOTE.into(),
                    timeline: true,
                };
                let body = serde_json::to_vec(&record)
                    .map_err(|_| StoreError("a job could not be encoded".into()))?;
                let item = NewItem::new(kind::JOB, now_ms, body).keyed(format!("j-{}", row.job.id));
                let written = log_of(&mut logs, data, row, &codecs).and_then(|log| {
                    log.append(&[item])
                        .map_err(|e| format!("the agent's log could not be written: {e}"))
                });
                if let Err(why) = written {
                    failed += 1;
                    lines.push(format!("failed {} {}: {why}", row.session, row.job.id));
                    continue;
                }
                writes.push((
                    JOBS.into(),
                    row.job.id.clone(),
                    Some(row.agent_id.as_bytes().to_vec()),
                ));
            }
            lines.push(said(row, &step, false));
            steps.push(step);
        }
        let mut summary = tally(&steps, false);
        if failed > 0 {
            summary.push_str(&format!(", {failed} failed"));
        }
        lines.push(summary);
        Ok((writes, (lines, failed)))
    })
    .map_err(|error| error.0)?;
    if failed > 0 {
        return Err(lines.join("\n"));
    }
    Ok(lines)
}

/// The log of the row's agent, opened once.
fn log_of<'a>(
    logs: &'a mut HashMap<String, Box<dyn AgentLog>>,
    data: &Path,
    row: &Row,
    codecs: &clarp_ports::codec::CodecSet,
) -> Result<&'a mut Box<dyn AgentLog>, String> {
    if !logs.contains_key(&row.agent_id) {
        let log = SqliteEngine::new()
            .open(&agent_dir(data, &row.agent_id), codecs.clone())
            .map_err(|e| format!("the agent's log could not be opened: {e}"))?;
        logs.insert(row.agent_id.clone(), log);
    }
    logs.get_mut(&row.agent_id)
        .ok_or_else(|| "the agent's log could not be opened".to_string())
}
