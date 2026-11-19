//! A tool's past sessions at the Host's edge: what whoever makes an agent is offered to
//! take up, and an agent that begins with one.
//!
//! An agent can begin where a session of its tool left off (`resume_session_id` of `POST
//! /agents`), or from a copy of one, which leaves the session itself as it is
//! (`fork_session_id`). The agent then continues that session of its tool, and its
//! conversation shows what was said in it: the turns of the tool's own transcript
//! ([`clarp_backend::history`]), as rows that came from elsewhere.
//!
//! This Host reads the sessions of Claude Code (`<home>/.claude/projects`), those of
//! Codex (`<home>/.codex/sessions`, with Codex's own index of its threads beside them) and
//! the conversations Antigravity kept (`<home>/.gemini/antigravity-cli`, with its own
//! catalogue of them): [`clarp_backend::past`]. A session of Codex or of Antigravity is
//! taken up wherever it ran, since those tools find a conversation by its name alone; a
//! copy of one is not made, which is said in the Python Host's own words for a tool that
//! cannot fork.
//!
//! The rules are the Python Host's (`policies/agent_spec.py`, `agent_lifecycle.py`,
//! `fork.py`, `routes/settings.py`): a session another agent has is not taken up a second
//! time, and is answered as that agent where the request says to open what is there.

use std::collections::HashMap;
use std::path::{Path, PathBuf};

use axum::extract::Query;
use axum::http::StatusCode;
use axum::routing::get;
use axum::{Json, Router};
use clarp_actor::ImportedRow;
use clarp_backend::{history, past};
use clarp_kernel::told;
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};

use crate::edge::App;
use crate::{ids, wire};

/// The tools whose sessions this Host reads.
const CLAUDE: &str = "claude";
const CODEX: &str = "codex";
const AGY: &str = "agy";
/// How many sessions of a directory are offered, and how many of every directory.
const OF_A_DIRECTORY: usize = 20;
const OF_EVERY_DIRECTORY: usize = 100;

/// Where Claude Code keeps its sessions, for a user whose home is `home`.
pub(crate) fn projects(home: &Path) -> PathBuf {
    home.join(".claude").join("projects")
}

/// Where Codex keeps what is its own: where `CODEX_HOME` says, as Codex itself reads it,
/// and `.codex` at home otherwise. Its rollouts are under `sessions` there, and the index of
/// its threads is `state_5.sqlite`.
pub(crate) fn codex_home(home: &Path) -> PathBuf {
    let said = std::env::var_os("CODEX_HOME").filter(|said| !said.is_empty());
    said.map_or_else(|| home.join(".codex"), PathBuf::from)
}

/// Where Antigravity keeps what is its own, for a user whose home is `home`.
fn agy_own(home: &Path) -> PathBuf {
    home.join(clarp_backend::agy::OWN)
}

/// `GET /past-sessions?cwd=&backend=&scope=`: the sessions a tool kept in a directory
/// (`scope=all`: in every directory), the newest first, for an app that offers to take one
/// up. A tool whose sessions this Host does not read has none to offer.
async fn listed(Query(query): Query<HashMap<String, String>>) -> Json<Value> {
    let asked = |key: &str| query.get(key).map_or("", |said| said.trim());
    let home = crate::config::home();
    let cwd = crate::config::typed_path(asked("cwd"), &home)
        .display()
        .to_string();
    let all = asked("scope").eq_ignore_ascii_case("all");
    // A tool the Host has not is its default tool, as on the Python Host.
    let tool = asked("backend").to_ascii_lowercase();
    let limit = if all {
        OF_EVERY_DIRECTORY
    } else {
        OF_A_DIRECTORY
    };
    let (root, codex, of) = (projects(&home), codex_home(&home), cwd.clone());
    let opencode = history::opencode::database(&home);
    let agy = agy_own(&home);
    let read = move || match tool.as_str() {
        CODEX => {
            let index = codex.join("state_5.sqlite");
            past::codex_sessions(&codex.join("sessions"), Some(&index), &of, all, limit)
        }
        "grok" => past::native_sessions(
            clarp_kernel::grok_muse::Tool::Grok,
            &home,
            if all { "" } else { &of },
            limit,
        ),
        "opencode" | "deepseek" => history::opencode::sessions(&opencode, &of, all, limit),
        "muse" => past::native_sessions(
            clarp_kernel::grok_muse::Tool::Muse,
            &home,
            if all { "" } else { &of },
            limit,
        ),
        AGY => past::agy_sessions(&agy, if all { "" } else { &of }, limit),
        _ => past::claude_sessions(&root, &of, all, limit),
    };
    let sessions = tokio::task::spawn_blocking(read).await.unwrap_or_default();
    Json(json!({
        "cwd": cwd, "scope": if all { "all" } else { "workspace" }, "sessions": sessions,
    }))
}

/// What a new agent begins with.
pub(crate) enum Begins {
    /// Nothing of the past: an agent like any other.
    Afresh,
    /// An agent has the session already, and the request said to open what is there: that
    /// agent's session name.
    Open(String),
    /// A session of its tool to continue, what was said in it (the turns of its
    /// transcript), the agent whose session it was forked from, when it was forked from an
    /// agent's, and the model the session last ran on (empty where its transcript names
    /// none).
    With {
        helpers: Option<Box<crate::helper_source::HelperSource>>,
        session: String,
        turns: Vec<Value>,
        parent: Option<String>,
        model: String,
    },
}

type Refused = (StatusCode, Value);

fn refused(status: StatusCode, word: &str, message: &str) -> Refused {
    (status, json!({ "error": word, "message": message }))
}

/// The agent whose tool's session this is: the one that continues it now (the Python Host's
/// rule, `agents_db.get_by_backend_session`), or the one whose conversation goes by this
/// name: a session Claude Code was given its name for by the Host.
fn owner_of(app: &App, session: &str) -> Option<(String, String)> {
    let directory = app.fleet.directory();
    let owns = |member: &&std::sync::Arc<crate::fleet::Member>| {
        let status = &member.agent.view().status;
        let goes_on = status.backend_session.as_deref() == Some(session);
        goes_on || (status.head_revision > 0 && member.record.conversation_id == session)
    };
    let owner = directory.members.iter().find(owns)?;
    Some((owner.record.agent_id.clone(), owner.record.session.clone()))
}

/// The turns of a transcript as rows of the conversation of the agent that begins with
/// them, in the shape this Host's own rows have. A row is named as the Python Host names
/// the rows it reads from a transcript: `msg-` and twenty signs of a digest of the agent,
/// the session and the row's place in it, so that a row keeps its name and no two agents
/// have a row of one name. What each is in the conversation is the Python Host's rule
/// ([`history::phases`]).
pub(crate) fn rows_of(agent_id: &str, session: &str, turns: &[Value]) -> Vec<ImportedRow> {
    let phases = history::phases(turns);
    let mut at_ms = 0;
    let mut rows = Vec::with_capacity(turns.len());
    for (number, (turn, phase)) in turns.iter().zip(phases).enumerate() {
        let said_at = turn["timestamp"].as_str().and_then(wire::from_iso);
        // A turn that does not say when it was said was said when the one before it was.
        at_ms = said_at.unwrap_or(at_ms).max(at_ms);
        let digest = Sha256::digest(format!("{agent_id}\0{session}\0{number}").as_bytes());
        let digest: String = digest.iter().map(|byte| format!("{byte:02x}")).collect();
        let id = format!("msg-{}", &digest[..20]);
        let row = json!({
            "id": id, "role": turn["role"], "text": turn["text"], "timestamp": wire::iso(at_ms),
            "kind": null, "phase": phase,
            "origin": "user", "trace_id": "", "tool_name": null, "tools": turn["tools"],
            // What the agent did, where its tool's reader shows that as cells (Codex).
            "display_cells": turn.get("display_cells").cloned().unwrap_or_else(|| json!([])),
            "automated": false, "automation_kind": "",
            "sender_agent_id": "", "sender_name": "", "sender_session": "",
            "reply_to_agent_id": "", "reply_to_name": "", "reply_to_session": "",
        });
        rows.push(ImportedRow {
            id,
            ts_ms: at_ms,
            row,
        });
    }
    rows
}

/// What a session a tool kept holds: what was said and done in it, and the model it last
/// ran on (empty where nothing names one).
type Held = (Vec<Value>, String);

/// What was said and done in a session of Codex, and the model it last ran on: read from
/// its rollout ([`history::codex`]), the newest file under Codex's sessions that is named
/// after it.
fn read_of_codex(named: &str) -> Option<Held> {
    let sessions = codex_home(&crate::config::home()).join("sessions");
    let rollout = std::fs::read_to_string(past::codex_rollout(&sessions, named)?).ok()?;
    Some((history::codex(&rollout), history::model(&rollout)))
}

fn read_of_opencode(named: &str) -> Option<Held> {
    let path = history::opencode::database(&crate::config::home());
    Some((history::opencode::read(&path, named)?, String::new()))
}

/// What was said and done in a conversation Antigravity kept: read from its transcript
/// ([`history::agy`]). A conversation this Host itself carried over into a new one opens
/// with the Host's record of the one before, which is no part of what the user said. The
/// transcript names no model the conversation ran on.
fn read_of_agy(named: &str) -> Option<Held> {
    let base = agy_own(&crate::config::home());
    let transcript = std::fs::read_to_string(past::agy_transcript(&base, named)?).ok()?;
    let mut turns = history::agy(&transcript);
    for turn in &mut turns {
        if turn["role"] == "user"
            && let Some(said) = turn["text"].as_str().map(told::without_restored)
        {
            turn["text"] = json!(said);
        }
    }
    turns.retain(|turn| turn["role"] != "user" || turn["text"] != "");
    Some((turns, String::new()))
}

/// What an agent begins with on a tool that finds a conversation by its name alone (Codex,
/// Antigravity): a session is taken up wherever it ran. `label`: what the tool is called,
/// in the Python Host's own sentences; `read`: what was said and done in a session of that
/// name, and the model it last ran on.
async fn begins_where_it_ran(
    app: &App,
    asked: &Map<String, Value>,
    resume: Option<String>,
    forks: bool,
    label: &str,
    read: fn(&str) -> Option<Held>,
) -> Result<Begins, Refused> {
    let owner = resume.as_deref().and_then(|session| owner_of(app, session));
    if let Some((_, owner)) = &owner
        && asked.get("open_existing") == Some(&json!(true))
    {
        return Ok(Begins::Open(owner.clone()));
    }
    if forks {
        // The Python Host's own answer for a tool that cannot fork a session.
        let why = format!("{label} does not support session forks.");
        return Err(refused(StatusCode::BAD_REQUEST, "fork_unsupported", &why));
    }
    let Some(session) = resume else {
        return Ok(Begins::Afresh);
    };
    if let Some((_, owner)) = owner {
        let why = format!("{label} session '{session}' is already used by {owner}.");
        let told = json!({ "error": "session_in_use", "message": why, "owner": owner });
        return Err((StatusCode::CONFLICT, told));
    }
    let named = session.clone();
    let read = move || read(&named);
    let Some((turns, model)) = tokio::task::spawn_blocking(read).await.ok().flatten() else {
        let why = "Session is no longer available in this directory";
        return Err(refused(StatusCode::NOT_FOUND, why, why));
    };
    Ok(Begins::With {
        helpers: None,
        session,
        turns,
        parent: None,
        model,
    })
}

/// What the agent a request asks for begins with: nothing of the past, an agent that is
/// there already, or a session of its tool with what was said in it. `cwd`: where the agent
/// will work. A copy for a fork is written before this answers.
pub(crate) async fn begins(
    app: &App,
    asked: &Map<String, Value>,
    cwd: &Path,
) -> Result<Begins, Refused> {
    let said = |key: &str| {
        let text = asked.get(key).and_then(Value::as_str).map(str::trim);
        text.filter(|text| !text.is_empty()).map(str::to_string)
    };
    let (resume, fork) = (said("resume_session_id"), said("fork_session_id"));
    if resume.is_none() && fork.is_none() {
        return Ok(Begins::Afresh);
    }
    let tool = said("backend").unwrap_or_else(|| CLAUDE.to_string());
    if tool.eq_ignore_ascii_case(CODEX) {
        let forks = fork.is_some();
        return begins_where_it_ran(app, asked, resume, forks, "Codex", read_of_codex).await;
    }
    if tool.eq_ignore_ascii_case("grok") {
        return begins_where_it_ran(app, asked, resume, fork.is_some(), "Grok", |id| {
            let log = past::native_transcript(
                clarp_kernel::grok_muse::Tool::Grok,
                &crate::config::home(),
                id,
            )?;
            Some((clarp_kernel::grok_muse_history::grok(&log), String::new()))
        })
        .await;
    }
    if matches!(tool.to_ascii_lowercase().as_str(), "opencode" | "deepseek") {
        let label = if tool.eq_ignore_ascii_case("deepseek") {
            "DeepSeek"
        } else {
            "OpenCode"
        };
        return begins_where_it_ran(app, asked, resume, fork.is_some(), label, read_of_opencode)
            .await;
    }
    if tool.eq_ignore_ascii_case("muse") {
        return begins_where_it_ran(app, asked, resume, fork.is_some(), "Muse Code", |id| {
            let log = past::native_transcript(
                clarp_kernel::grok_muse::Tool::Muse,
                &crate::config::home(),
                id,
            )?;
            Some((clarp_kernel::grok_muse_history::muse(&log), String::new()))
        })
        .await;
    }
    if tool.eq_ignore_ascii_case(AGY) {
        let forks = fork.is_some();
        return begins_where_it_ran(app, asked, resume, forks, "Antigravity", read_of_agy).await;
    }
    if !tool.eq_ignore_ascii_case(CLAUDE) {
        let why = "Taking up or forking a past session is built for Claude Code, Codex and \
                   Antigravity on this Host, and for no other tool yet.";
        return Err(refused(StatusCode::BAD_REQUEST, "resume_unsupported", why));
    }
    let root = projects(&crate::config::home());
    let cwd = cwd.display().to_string();
    if let Some(session) = resume {
        // A session an agent has is that agent's: it is opened, or it is in use.
        if let Some((_, owner)) = owner_of(app, &session) {
            if asked.get("open_existing") == Some(&json!(true)) {
                return Ok(Begins::Open(owner));
            }
            let why = format!("Claude session '{session}' is already used by {owner}.");
            let told = json!({ "error": "session_in_use", "message": why, "owner": owner });
            return Err((StatusCode::CONFLICT, told));
        }
        // A session is taken up where its tool will look for it: among those of the
        // directory the agent works in, and nowhere else. Claude Code finds a session only
        // under the directory it is started in.
        let name = session.clone();
        let read = move || {
            let path = past::claude_transcript(&root, &cwd, &name, false)?;
            use std::io::Read;
            let resolved = root.canonicalize().ok()?;
            let relative = path.strip_prefix(&resolved).ok()?.to_str()?;
            let (_, mut file) = crate::files::open_beneath(&root, relative, false).ok()?;
            if !file.metadata().ok()?.is_file() {
                return None;
            }
            let mut text = String::new();
            file.read_to_string(&mut text).ok()?;
            let mut source = crate::helper_source::HelperSource::default();
            source.seed(&file, &text).ok()?;
            source.discover(&root, &path);
            let children = &source.children;
            let turns = history::claude_with_children(&text, children);
            Some((text, turns, Box::new(source)))
        };
        let Some((transcript, turns, helpers)) =
            tokio::task::spawn_blocking(read).await.ok().flatten()
        else {
            let why = "Session is no longer available in this directory";
            return Err(refused(StatusCode::NOT_FOUND, why, why));
        };
        return Ok(Begins::With {
            helpers: Some(helpers),
            session,
            turns,
            parent: None,
            model: history::model(&transcript),
        });
    }
    let Some(source) = fork else {
        return Ok(Begins::Afresh);
    };
    // A fork: a copy of the session under a name of its own, in the project of the
    // directory the new agent works in, where its tool looks for it.
    let parent = owner_of(app, &source).map(|(agent_id, _)| agent_id);
    let session = ids::uuid();
    let (of, name, copy_as) = (cwd.clone(), source.clone(), session.clone());
    let copied = move || -> Result<String, StatusCode> {
        let from = past::claude_transcript(&root, &of, &name, true).ok_or(StatusCode::NOT_FOUND)?;
        let failed = |_| StatusCode::INTERNAL_SERVER_ERROR;
        let transcript = std::fs::read_to_string(from).map_err(failed)?;
        let copy = history::claude_forked(&transcript, &copy_as);
        let dir = root.join(past::claude_project(&of));
        std::fs::create_dir_all(&dir).map_err(failed)?;
        std::fs::write(dir.join(format!("{copy_as}.jsonl")), &copy).map_err(failed)?;
        Ok(copy)
    };
    let copy = match tokio::task::spawn_blocking(copied).await {
        Ok(Ok(copy)) => copy,
        Ok(Err(StatusCode::NOT_FOUND)) => {
            let why = "fork source not found";
            return Err(refused(StatusCode::NOT_FOUND, why, why));
        }
        _ => {
            let why = "fork io failed";
            return Err(refused(StatusCode::INTERNAL_SERVER_ERROR, why, why));
        }
    };
    Ok(Begins::With {
        helpers: None,
        session,
        turns: history::claude(&copy),
        parent,
        model: history::model(&copy),
    })
}

pub fn routes() -> Router<App> {
    Router::new().route("/past-sessions", get(listed))
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use super::*;

    #[test]
    fn the_turns_of_a_transcript_become_rows_that_came_from_elsewhere() {
        let turns = vec![
            json!({ "role": "user", "text": "one", "tools": [], "timestamp": "2027-01-15T08:00:00.000Z" }),
            json!({ "role": "assistant", "text": "", "tools": [{ "name": "Read" }], "timestamp": "" }),
            json!({ "role": "assistant", "text": "two", "tools": [], "timestamp": "2027-01-15T08:00:05.000Z" }),
            json!({ "role": "user", "text": "three", "tools": [], "timestamp": "long ago" }),
            json!({ "role": "assistant", "text": "four", "tools": [], "timestamp": "2027-01-15T07:00:00.000Z" }),
        ];
        let rows = rows_of("a1b2c3d4e5f60718", "0b6b2f4e-aaaa-bbbb", &turns);
        // Each has a name of its own, of one shape, that is the same whenever it is read.
        let ids: Vec<&str> = rows.iter().map(|row| row.id.as_str()).collect();
        let named = |id: &&str| {
            let signs = id.strip_prefix("msg-").unwrap_or_default();
            signs.len() == 20 && signs.chars().all(|sign| sign.is_ascii_hexdigit())
        };
        assert!(ids.iter().all(named), "{ids:?}");
        let other: std::collections::BTreeSet<&str> = ids.iter().copied().collect();
        assert_eq!(other.len(), 5);
        let again = rows_of("a1b2c3d4e5f60718", "0b6b2f4e-aaaa-bbbb", &turns);
        assert_eq!(again[3].id, rows[3].id);
        // The same turns are other rows for another agent: no two agents have a row of one name.
        let another = rows_of("ffffffffffffffff", "0b6b2f4e-aaaa-bbbb", &turns);
        assert!(
            another
                .iter()
                .zip(&rows)
                .all(|(theirs, ours)| theirs.id != ours.id)
        );
        // And of another session the agent took up: a transcript's turns never name a row.
        let later = rows_of("a1b2c3d4e5f60718", "another-session", &turns);
        assert!(
            later
                .iter()
                .zip(&rows)
                .all(|(theirs, ours)| theirs.id != ours.id)
        );
        // What the agent said last before its user spoke again is the final answer.
        let phases: Vec<&Value> = rows.iter().map(|row| &row.row["phase"]).collect();
        assert_eq!(
            phases,
            [
                &Value::Null,
                &Value::Null,
                &json!("final"),
                &Value::Null,
                &json!("final")
            ]
        );
        // Each is said no earlier than the one before it, whatever it says of itself.
        let first = wire::from_iso("2027-01-15T08:00:00.000Z").unwrap();
        let times: Vec<i64> = rows.iter().map(|row| row.ts_ms).collect();
        assert_eq!(
            times,
            [first, first, first + 5_000, first + 5_000, first + 5_000]
        );
        assert_eq!(rows[1].row["tools"], json!([{ "name": "Read" }]));
        assert_eq!(rows[0].row["role"], "user");
        assert_eq!(rows[2].row["timestamp"], wire::iso(first + 5_000));
    }
}
