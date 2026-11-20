//! An agent forks itself into helpers that start from its conversation
//! (`POST /agents/<session>/fork`, `clarp-admin agent fork`).
//!
//! What a request asks for, a child's first message and the part of a conversation a child
//! is given as words are the kernel's (`clarp_kernel::fork`), the Python Host's rules. Here
//! is what a Host does about it: it finds the agent that forks, makes each child's worktree
//! where one is asked for, makes the helper, and sends it its task from its parent. The
//! children are independent: one that fails is told in its row, and its siblings are made.
//!
//! A child starts from its parent's conversation **as words**: the most recent part of it,
//! as this Host has it, at the head of its first message. The Python Host has the tool copy
//! the conversation itself where the tool can (a session of Claude Code under a new id, a
//! thread of Codex forked), and falls back to words; this Host has only the words yet, and
//! its rows say so (`fork: seed`).

use std::path::{Path, PathBuf};
use std::time::Duration;

use axum::extract::{Path as Named, State};
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::routing::post;
use axum::{Json, Router};
use clarp_actor::conversation::Which;
use clarp_kernel::conversation::{Entry, ToolCall};
use clarp_kernel::fork::{self, Child, Refused, SaidThing};
use clarp_kernel::tools::summarize;
use clarp_protocol::requests::SendRequest;
use serde_json::{Map, Value, json};

use crate::edge::{self, ApiError, App};
use crate::fleet::Member;

/// How many of the newest rows of a conversation are read for the words a child is given.
/// The words themselves are counted by the kernel (`fork::SEED_MOST`).
const ROWS: usize = 1_000;
/// How long one command of git may take.
const GIT_WITHIN: Duration = Duration::from_secs(120);

type Answered = Result<Value, (StatusCode, Value)>;

fn told(refused: &Refused) -> (StatusCode, Value) {
    let status = StatusCode::from_u16(refused.status).unwrap_or(StatusCode::BAD_REQUEST);
    (status, refused.told())
}

/// A path as a request writes it: `~` is the home of whoever runs the Host, and what does
/// not count is left out (a slash at its end, two in a row, a part that is only a dot).
fn written(path: &str) -> PathBuf {
    let home = crate::config::home();
    let named = match path.strip_prefix('~') {
        Some("") => home,
        Some(under) if under.starts_with('/') => home.join(under.trim_start_matches('/')),
        _ => PathBuf::from(path),
    };
    named.components().collect()
}

/// One command of git in a directory: whether it went well, what it printed, what it
/// complained of. A command that does not end in time did not go well, and is ended.
async fn git(directory: &Path, args: &[&str]) -> (bool, String, String) {
    let mut command = crate::child::asynchronous("git");
    command.arg("-C").arg(directory).args(args);
    command.stdin(std::process::Stdio::null());
    command.kill_on_drop(true);
    match tokio::time::timeout(GIT_WITHIN, command.output()).await {
        Ok(Ok(done)) => (
            done.status.success(),
            String::from_utf8_lossy(&done.stdout).into_owned(),
            String::from_utf8_lossy(&done.stderr).into_owned(),
        ),
        Ok(Err(error)) => (false, String::new(), error.to_string()),
        Err(_) => (false, String::new(), "git did not end in time".to_string()),
    }
}

/// Makes a child's worktree, or finds the one that is there on its branch. Answers where
/// it is. The worktree is made before the agent, so that one that cannot be made makes
/// nothing.
async fn worktree(child: &Child) -> Result<String, Refused> {
    let failed = |status: u16, why: String| Refused::new(status, "worktree_failed", why);
    let asked = written(&child.repo);
    let top = if asked.is_dir() {
        Some(git(&asked, &["rev-parse", "--show-toplevel"]).await)
    } else {
        None
    };
    let Some((true, top, _)) = top else {
        return Err(failed(
            400,
            format!("{} is not a git repository", child.repo),
        ));
    };
    let repo = PathBuf::from(top.trim());
    let place = if child.worktree_path.is_empty() {
        PathBuf::from(fork::worktree_place(
            &repo.display().to_string(),
            &child.branch,
        ))
    } else {
        written(&child.worktree_path)
    };
    let shown = place.display().to_string();
    if place.exists() {
        let (went, branch, _) = git(&place, &["branch", "--show-current"]).await;
        if went && branch.trim() == child.branch {
            return Ok(shown);
        }
        let why = format!("{shown} exists and is not a worktree on {}", child.branch);
        return Err(failed(409, why));
    }
    if let Some(above) = place.parent() {
        let _ = std::fs::create_dir_all(above);
    }
    let known = format!("refs/heads/{}", child.branch);
    let (there, _, _) = git(&repo, &["rev-parse", "--verify", "--quiet", &known]).await;
    let base = if child.base.is_empty() {
        "HEAD"
    } else {
        &child.base
    };
    let (went, _, complained) = if there {
        git(&repo, &["worktree", "add", &shown, &child.branch]).await
    } else {
        git(
            &repo,
            &["worktree", "add", "-b", &child.branch, &shown, base],
        )
        .await
    };
    if went {
        return Ok(shown);
    }
    let last = complained.trim().lines().next_back();
    Err(failed(
        409,
        last.unwrap_or("git worktree add failed").to_string(),
    ))
}

/// The tools a reply called, each with its name and a line of what it was asked.
fn called(calls: &[ToolCall]) -> Vec<(String, String)> {
    let line = |call: &ToolCall| {
        (
            call.name.clone(),
            summarize(&call.name, &call.input).summary,
        )
    };
    calls.iter().map(line).collect()
}

/// A conversation's rows as what was said in it, for the words a child is given: what was
/// said to the agent and what it said, each with the tools it called. A row that came with
/// the agent from another Host is read as that Host served it.
fn said_in(rows: &[clarp_actor::conversation::Versioned]) -> Vec<SaidThing> {
    let mut said: Vec<SaidThing> = Vec::new();
    for row in rows {
        match &row.entry {
            Entry::Message(message) => said.push(SaidThing {
                by_the_user: true,
                text: message.text.clone(),
                tools: Vec::new(),
            }),
            Entry::Reply(reply) => said.push(SaidThing {
                by_the_user: false,
                text: reply.text.clone(),
                tools: called(&row.tools),
            }),
            // A call that is a row by itself belongs to what the agent said last.
            Entry::Tool(call) => {
                let mut line = called(std::slice::from_ref(call));
                match said.last_mut() {
                    Some(last) if !last.by_the_user => last.tools.append(&mut line),
                    _ => said.push(SaidThing {
                        by_the_user: false,
                        text: String::new(),
                        tools: line,
                    }),
                }
            }
            Entry::Imported(imported) => {
                let row = &imported.row;
                let word = |value: &Value| value.as_str().unwrap_or_default().to_string();
                let by_the_user = match row["role"].as_str() {
                    Some("user") => true,
                    Some("assistant") => false,
                    _ => continue,
                };
                let tools = row["tools"].as_array().map(|tools| {
                    let tools = tools.iter().filter(|tool| tool.is_object());
                    tools
                        .map(|tool| (word(&tool["name"]), word(&tool["summary"])))
                        .collect()
                });
                said.push(SaidThing {
                    by_the_user,
                    text: word(&row["text"]),
                    tools: tools.unwrap_or_default(),
                });
            }
            _ => {}
        }
    }
    said
}

/// The most recent part of an agent's conversation as words, for a child that starts from
/// it. A conversation that cannot be read gives none: the child is told so in its message.
async fn seed_of(member: &Member) -> String {
    match member.agent.tail(ROWS, Which::All).await {
        Ok(newest) => fork::seed(&said_in(&newest.entries)),
        Err(_) => String::new(),
    }
}

/// Makes one child: its worktree, the helper, and its task sent from its parent. Answers
/// its row.
async fn child_of(app: &App, parent: &Member, child: &Child, seed: &str) -> Result<Value, Refused> {
    let record = &parent.record;
    let cwd = if child.repo.is_empty() {
        let asked = if child.cwd.is_empty() {
            &record.cwd
        } else {
            &child.cwd
        };
        written(asked).display().to_string()
    } else {
        worktree(child).await?
    };
    if !Path::new(&cwd).is_dir() {
        let why = format!("No such directory: {cwd}");
        return Err(Refused::new(400, "cwd_missing", why));
    }
    let backend = if child.backend.is_empty() {
        &record.backend
    } else {
        &child.backend
    };
    // A helper is a working identity, not a contact: it has no voice.
    let mut asked = Map::new();
    for (key, value) in [
        ("name", json!(child.name)),
        ("cwd", json!(cwd)),
        ("backend", json!(backend)),
        ("parent", json!(record.session)),
        ("role", json!("helper")),
        ("voice_id", json!("{}")),
        ("synthesize_audio", json!(false)),
    ] {
        asked.insert(key.to_string(), value);
    }
    for (key, value) in [("model", &child.model), ("effort", &child.effort)] {
        if !value.is_empty() {
            asked.insert(key.to_string(), json!(value));
        }
    }
    let made = match edge::make_agent(app, &asked).await {
        Ok(made) => made,
        // In the words the Host has for an agent it does not make.
        Err((status, why)) => {
            let code = why["error"].as_str().unwrap_or("agent_not_made");
            let words = why["message"].as_str().unwrap_or(code);
            return Err(Refused::new(
                status.as_u16(),
                code.to_string(),
                words.to_string(),
            ));
        }
    };
    let helper = made["session"].as_str().unwrap_or_default().to_string();
    let mut row = json!({
        "name": child.name, "session": helper, "cwd": cwd, "backend": backend,
        "fork": "seed", "delivered": false,
    });
    if !child.branch.is_empty() {
        row["branch"] = json!(child.branch);
    }
    let text = fork::message(&record.session, &helper, child, &cwd, Some(seed));
    let request = SendRequest {
        session: helper,
        text,
        // From its parent, as one agent writes to another. It is not said aloud, and it
        // waits its turn if the helper is at work already.
        sender: Some(json!(record.agent_id)),
        synthesize_audio: Some(false),
        force_session: Some(true),
        queue_if_busy: Some(true),
        ..SendRequest::default()
    };
    match edge::send_own(app, request).await {
        Ok(_) => row["delivered"] = json!(true),
        // The helper is there: its row says so, and that its task is not.
        Err(refused) => {
            let why = refused.message();
            row["message"] = json!(format!("created, but its task was not delivered: {why}"));
        }
    }
    Ok(row)
}

/// Forks an agent into the children a request asks for. Answers the parent and a row for
/// each child: its name and either its session, where it works, its tool, how it started
/// and whether its task was delivered, or why it was not made. A request that is malformed
/// makes nothing.
pub(crate) async fn fork_agent(app: &App, session: &str, request: &Value) -> Answered {
    let children = fork::asked(request).map_err(|refused| told(&refused))?;
    let session = session.trim_matches('/');
    let directory = app.fleet.directory();
    let Some(parent) = directory.named(session).cloned() else {
        let none = Refused::new(404, "agent_not_found", format!("No live agent: {session}"));
        return Err(told(&none));
    };
    if parent.agent.view().status.backend_session.is_none() {
        let why = format!("{} has no conversation to fork yet", parent.record.session);
        return Err(told(&Refused::new(409, "no_conversation", why)));
    }
    for child in &children {
        if !child.backend.is_empty() && clarp_backend::by_id(&child.backend).is_none() {
            let why = format!("Unknown backend: {}", child.backend);
            return Err(told(&Refused::new(400, "invalid_backend", why)));
        }
    }
    let seed = seed_of(&parent).await;
    let mut rows = Vec::with_capacity(children.len());
    for child in &children {
        rows.push(match child_of(app, &parent, child, &seed).await {
            Ok(row) => row,
            Err(why) => json!({ "name": child.name, "error": why.code, "message": why.message }),
        });
    }
    Ok(json!({ "ok": true, "parent": parent.record.session, "children": rows }))
}

/// `POST /agents/<session>/fork`.
async fn fork_route(
    State(app): State<App>,
    Named(session): Named<String>,
    headers: axum::http::HeaderMap,
    body: axum::body::Bytes,
) -> Response {
    // Nothing at all is an empty object, and what is no object is no request: as the
    // Python Host reads a body.
    let request = match serde_json::from_slice::<Value>(&body) {
        _ if body.is_empty() => json!({}),
        Ok(request @ Value::Object(_)) => request,
        _ => return ApiError::new(StatusCode::BAD_REQUEST, "bad json").into_response(),
    };
    let token = headers
        .get(crate::turn_identity::HEADER)
        .and_then(|v| v.to_str().ok());
    if let Err(why) = crate::fleet::delegations::lineage(&app, &session, token).await {
        return why.into_response();
    }
    match fork_agent(&app, &session, &request).await {
        Ok(answer) => Json(answer).into_response(),
        Err((status, why)) => (status, Json(why)).into_response(),
    }
}

pub fn routes() -> Router<App> {
    Router::new().route("/agents/{session}/fork", post(fork_route))
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use super::*;
    use clarp_actor::conversation::Versioned;
    use clarp_kernel::conversation::{Imported, Message, Reply};

    fn row(entry: Entry, tools: Vec<ToolCall>) -> Versioned {
        Versioned {
            entry,
            revision: 0,
            ts_ms: 0,
            live: false,
            turn_number: None,
            turn: None,
            tools,
            commentary: false,
            coordination: None,
        }
    }

    fn call(name: &str, input: Value, reply: Option<&str>) -> ToolCall {
        ToolCall {
            id: format!("call-{name}"),
            turn: 1,
            name: name.to_string(),
            input,
            reply: reply.map(str::to_string),
            ..ToolCall::default()
        }
    }

    #[test]
    fn a_conversation_is_read_as_what_was_said_in_it() {
        let message = Message {
            id: "u-1".to_string(),
            text: "list the files".to_string(),
            ..Message::default()
        };
        let reply = Reply {
            id: "r-1".to_string(),
            turn: 1,
            text: "Looking.".to_string(),
            ..Reply::default()
        };
        let listing = call("Bash", json!({ "command": "ls -la" }), Some("r-1"));
        let alone = call("Read", json!({ "file_path": "/srv/work/notes.md" }), None);
        let imported = |role: &str, text: &str, tools: Value| {
            Entry::Imported(Imported {
                id: format!("imported-{role}"),
                row: json!({ "role": role, "text": text, "tools": tools }),
            })
        };
        let rows = [
            row(imported("user", "earlier", json!(null)), Vec::new()),
            row(
                imported(
                    "assistant",
                    "before the move",
                    json!([{ "name": "Edit", "summary": "a file" }, "not a tool", { "name": "Bash" }]),
                ),
                Vec::new(),
            ),
            row(
                imported("system", "a notice of the other Host", json!([])),
                Vec::new(),
            ),
            row(Entry::Message(message), Vec::new()),
            row(Entry::Reply(reply), vec![listing.clone()]),
            row(Entry::Tool(alone.clone()), Vec::new()),
        ];
        let said = said_in(&rows);
        let words = |things: &[(&str, &str)]| -> Vec<(String, String)> {
            let pair = |(name, asked): &(&str, &str)| (name.to_string(), asked.to_string());
            things.iter().map(pair).collect()
        };
        let listed = summarize("Bash", &listing.input).summary;
        let read = summarize("Read", &alone.input).summary;
        assert!(!listed.is_empty() && !read.is_empty());
        assert_eq!(
            said,
            vec![
                SaidThing {
                    by_the_user: true,
                    text: "earlier".to_string(),
                    tools: Vec::new(),
                },
                SaidThing {
                    by_the_user: false,
                    text: "before the move".to_string(),
                    tools: words(&[("Edit", "a file"), ("Bash", "")]),
                },
                SaidThing {
                    by_the_user: true,
                    text: "list the files".to_string(),
                    tools: Vec::new(),
                },
                // The call that was a row by itself is with what the agent said last.
                SaidThing {
                    by_the_user: false,
                    text: "Looking.".to_string(),
                    tools: words(&[("Bash", &listed), ("Read", &read)]),
                },
            ]
        );
        // And as the words a child is given.
        let seed = fork::seed(&said);
        assert!(seed.starts_with(
            "User: earlier\n\nAssistant: before the move\n  → Edit: a file\n  → Bash\n\n"
        ));
        assert!(seed.ends_with(&format!(
            "User: list the files\n\nAssistant: Looking.\n  → Bash: {listed}\n  → Read: {read}"
        )));
    }

    #[test]
    fn a_path_is_read_as_it_is_written() {
        let home = crate::config::home();
        assert_eq!(written("~"), home);
        assert_eq!(written("~/work/"), home.join("work"));
        assert_eq!(
            written("/srv//work/./here/"),
            PathBuf::from("/srv/work/here")
        );
        assert_eq!(written("/srv/~/x"), PathBuf::from("/srv/~/x"));
    }
}
