//! `clarp-admin`, as far as an agent uses it: writing to another agent.
//!
//! On the Python Host `clarp-admin` is the owner's tool for the whole installation, and
//! agents use one part of it: `clarp-admin prompt --to SESSION --from ME --text ...` is how
//! an agent hands work or a result to another (the skill `clarp-agent-communication`). An
//! agent on this Host calls the same command; this is what it does here. The arguments, what
//! is printed and the exit codes are the Python tool's (`bin/clarp-admin.py`, `cmd_prompt`).
//!
//! With `--server NAME` the message goes to an agent of another Host the owner named
//! ([`crate::peers`]): the Python Host beside this one, while agents move over one by one.
//!
//! Two more commands are an agent's: `clarp-admin sessions` lists the agents it can write
//! to, and `clarp-admin model` shows or sets which model an agent runs (the skill
//! `clarp-sessions`). And an agent makes another, and says how a helper's work stands, with
//! `clarp-admin agent create` and `clarp-admin agent helper-state`: these are what the
//! program `clarp-sub-agent` calls (the skill `clarp-sub-agents`).
//!
//! Everything else `clarp-admin` does belongs to the Python installation: asked for here, it
//! says that it is not on this Host, and does nothing.

use clarp_backend::SESSION_ENV;
use clarp_kernel::python;
use clarp_protocol::requests::SendRequest;
use serde_json::{Value, json};

use crate::artifact_tool::parse;
use crate::edge::{self, App};
use crate::ids;
use crate::peers::{self, Failed};
use crate::tools::{NOT_HERE, ToolAnswer, ToolCall};
use crate::{request_tool, roster};

/// What argparse says of a command line it cannot read, and exits 2 with.
fn usage(why: &str) -> ToolAnswer {
    ToolAnswer::said(
        2,
        format!(
            "usage: clarp-admin prompt [-h] --to TO [--from FROM_SESSION] --text TEXT [--delay DELAY] \
             [--origin {{automation,watcher}}] [--server SERVER]\n\
             clarp-admin prompt: error: {why}"
        ),
    )
}

fn not_here(what: &str) -> ToolAnswer {
    ToolAnswer::said(
        NOT_HERE,
        format!(
            "clarp-admin {what}: not available on this Host yet (the Rust Host). Nothing was done."
        ),
    )
}

/// `clarp-admin prompt`: a message to an agent of this Host, from an agent (`--from`) or
/// from something that runs by itself (`--origin`).
async fn prompt(app: &App, args: &[String]) -> ToolAnswer {
    let valued = [
        "--to", "--from", "--text", "--delay", "--origin", "--server",
    ];
    let parsed = match parse(args, &valued, &[], &[]) {
        Ok(parsed) => parsed,
        Err(why) => return usage(&why),
    };
    let missing: Vec<&str> = ["--to", "--text"]
        .into_iter()
        .filter(|name| parsed.option(name).is_none())
        .collect();
    if !missing.is_empty() {
        return usage(&format!(
            "the following arguments are required: {}",
            missing.join(", ")
        ));
    }
    let origin = match parsed.option("--origin") {
        None => None,
        Some(origin @ ("automation" | "watcher")) => Some(origin),
        Some(other) => {
            return usage(&format!(
                "argument --origin: invalid choice: '{other}' (choose from 'automation', 'watcher')"
            ));
        }
    };
    let from = parsed.option("--from").filter(|from| !from.is_empty());
    if from.is_some() && origin.is_some() {
        return ToolAnswer::said(1, "--origin cannot be combined with --from");
    }
    // A message for later is not built yet. Nothing is sent in its place.
    if parsed
        .option("--delay")
        .is_some_and(|delay| !delay.is_empty())
    {
        return not_here("prompt --delay");
    }
    let request = SendRequest {
        session: parsed.option("--to").unwrap_or_default().to_string(),
        text: parsed.option("--text").unwrap_or_default().to_string(),
        // One id for the message, however often the tool has to ask.
        client_msg_id: Some(format!("clarp-admin-{}", ids::hex(8))),
        synthesize_audio: Some(false),
        hands_free: Some(false),
        origin: Some(if from.is_some() {
            "agent".to_string()
        } else {
            origin.unwrap_or("automation").to_string()
        }),
        sender: from.map(|from| json!(from)),
        ..SendRequest::default()
    };
    // For an agent of another Host the owner named: that Host's own send.
    if let Some(server) = parsed
        .option("--server")
        .filter(|server| !server.is_empty())
    {
        let mut payload = json!(request);
        payload["force_session"] = json!(true);
        return match peers::send(app, server, payload).await {
            Ok(answer) => {
                ToolAnswer::printed(0, serde_json::to_string_pretty(&answer).unwrap_or_default())
            }
            Err(Failed::Unknown(name)) => ToolAnswer::said(
                1,
                format!("clarp-admin prompt: unknown or disabled peer: {name}"),
            ),
            Err(Failed::Refused { status, detail }) => ToolAnswer::said(
                4,
                format!("clarp-admin prompt: the Host refused it (HTTP {status}): {detail}"),
            ),
            Err(Failed::Down(why)) => ToolAnswer::said(
                5,
                format!(
                    "clarp-admin prompt: {why}. It may have arrived: do not send it again \
                     without looking"
                ),
            ),
        };
    }
    match edge::send_message(app, request).await {
        Ok(sent) => match serde_json::to_string_pretty(&sent) {
            Ok(shown) => ToolAnswer::printed(0, shown),
            Err(error) => ToolAnswer::said(1, format!("clarp-admin prompt: {error}")),
        },
        Err(refused) if refused.status().is_client_error() => ToolAnswer::said(
            4,
            format!(
                "clarp-admin prompt: the Host refused it (HTTP {}): {}",
                refused.status().as_u16(),
                refused.message()
            ),
        ),
        Err(failed) => ToolAnswer::said(
            1,
            format!(
                "clarp-admin prompt: the Host could not do it: {}",
                failed.message()
            ),
        ),
    }
}

/// A value as the Python tool prints it: on lines of its own, two spaces in.
fn shown(value: &Value) -> ToolAnswer {
    match serde_json::to_string_pretty(value) {
        Ok(shown) => ToolAnswer::printed(0, shown),
        Err(error) => ToolAnswer::said(1, format!("clarp-admin: {error}")),
    }
}

/// `clarp-admin sessions`: the agents of this Host, by their names: what an agent reads
/// before it writes to another.
fn sessions(app: &App) -> ToolAnswer {
    let directory = app.fleet.directory();
    let mut records: Vec<_> = directory
        .members
        .iter()
        .map(|member| &member.record)
        .collect();
    records.sort_by(|a, b| (&a.persona, &a.session).cmp(&(&b.persona, &b.session)));
    let listed: Vec<Value> = records
        .into_iter()
        .map(|record| {
            json!({
                "session": record.session,
                "agent_id": record.agent_id,
                "persona": record.persona,
                "backend": record.backend,
                "cwd": record.cwd,
            })
        })
        .collect();
    shown(&json!(listed))
}

/// `clarp-admin model [MODEL] [--effort EFFORT] [--session SESSION]`: shows or sets an
/// agent's model and effort; with no session, those of the agent that calls. `default`
/// clears one back to the tool's own choice. A change holds from the agent's next turn:
/// the turn that runs the command keeps what it was started with.
async fn model(app: &App, call: &ToolCall, args: &[String]) -> ToolAnswer {
    let parsed = match parse(args, &["--effort", "--session"], &[], &["[model]"]) {
        Ok(parsed) => parsed,
        Err(why) => {
            return ToolAnswer::said(
                2,
                format!(
                    "usage: clarp-admin model [-h] [--effort EFFORT] [--session SESSION] [model]\n\
                     clarp-admin model: error: {why}"
                ),
            );
        }
    };
    let own = call.env.get(SESSION_ENV).map_or("", String::as_str);
    let session = parsed.option("--session").unwrap_or(own).trim().to_string();
    if session.is_empty() {
        return ToolAnswer::said(
            1,
            "no session: pass --session (CLAUDE_PWA_SESSION is unset)",
        );
    }
    let Some(member) = app.member(&session) else {
        return ToolAnswer::said(1, format!("no such agent session: {session}"));
    };
    let cleared = |said: &str| {
        if said == "default" {
            String::new()
        } else {
            said.to_string()
        }
    };
    let mut asked = roster::Fields::new();
    asked.insert("session".to_string(), json!(session));
    if let Some(model) = parsed.positional.first() {
        asked.insert("model".to_string(), json!(cleared(model)));
    }
    if let Some(effort) = parsed.option("--effort") {
        asked.insert("effort".to_string(), json!(cleared(effort)));
    }
    if asked.len() == 1 {
        // Which models a tool offers, this Host does not know yet: it lists none, and
        // takes the name it is given.
        return shown(&json!({
            "session": session,
            "backend": member.record.backend,
            "model": member.record.model,
            "effort": member.record.effort,
            "available": [],
        }));
    }
    match roster::tune(app, &asked).await {
        Ok(mut result) => {
            result["session"] = json!(session);
            result["applies"] = json!("next turn");
            shown(&result)
        }
        Err(refused) => {
            let (status, detail) = refused.answered();
            ToolAnswer::said(1, format!("model change refused (HTTP {status}): {detail}"))
        }
    }
}

/// What the Python tool says when the Host refuses an `agent` command, and exits 1 with.
fn refused_by_host(status: u16, body: &Value) -> ToolAnswer {
    ToolAnswer::said(
        1,
        format!("clarp-admin: HTTP {status}: {}", python::dumps(body)),
    )
}

/// `clarp-admin agent create NAME [--parent SESSION] [--role agent|helper] [--cwd DIR]
/// [--backend B] [--model M] [--effort E] [--session S] [--voice-id V]`: makes an agent;
/// with `--parent` and `--role helper`, a helper of that agent. This is how an agent makes
/// another (the skill `clarp-sub-agents`).
async fn agent_create(app: &App, call: &ToolCall, args: &[String]) -> ToolAnswer {
    let valued = [
        "--parent",
        "--role",
        "--cwd",
        "--backend",
        "--model",
        "--effort",
        "--session",
        "--voice-id",
    ];
    let usage = |why: &str| {
        ToolAnswer::said(
            2,
            format!(
                "usage: clarp-admin agent create [-h] [--parent PARENT] [--role {{agent,helper}}] \
                 [--cwd CWD] [--backend BACKEND] [--model MODEL] [--effort EFFORT] \
                 [--session SESSION] [--voice-id VOICE_ID] name\n\
                 clarp-admin agent create: error: {why}"
            ),
        )
    };
    let parsed = match parse(args, &valued, &[], &["name"]) {
        Ok(parsed) => parsed,
        Err(why) => return usage(&why),
    };
    let role = match parsed.option("--role") {
        None => None,
        Some(role @ ("agent" | "helper")) => Some(role),
        Some(other) => {
            return usage(&format!(
                "argument --role: invalid choice: '{other}' (choose from 'agent', 'helper')"
            ));
        }
    };
    if role == Some("helper") && parsed.option("--parent").is_none() {
        return ToolAnswer::said(1, "--role helper needs --parent SESSION");
    }
    // A helper is a working identity, not a contact: no voice unless one is asked for.
    let mut asked = roster::Fields::new();
    let name = parsed.positional.first().cloned().unwrap_or_default();
    asked.insert("name".to_string(), json!(name));
    asked.insert("synthesize_audio".to_string(), json!(false));
    for key in [
        "cwd", "backend", "model", "effort", "parent", "role", "session",
    ] {
        if let Some(value) = parsed.option(&format!("--{key}")) {
            asked.insert(key.to_string(), json!(value));
        }
    }
    let voice = parsed.option("--voice-id").unwrap_or("{}");
    asked.insert("voice_id".to_string(), json!(voice));
    // A directory is named from where the tool was called.
    if let Some(cwd) = parsed.option("--cwd").filter(|cwd| !cwd.is_empty()) {
        let from = std::path::Path::new(&call.cwd);
        let whole = match cwd.strip_prefix("~/") {
            Some(rest) => home().join(rest),
            None if cwd == "~" => home(),
            None => from.join(cwd),
        };
        let whole = whole.canonicalize().unwrap_or(whole);
        asked.insert("cwd".to_string(), json!(whole.display().to_string()));
    }
    match edge::make_agent(app, &asked).await {
        Ok(made) => shown(&made),
        Err((status, why)) => refused_by_host(status.as_u16(), &why),
    }
}

fn home() -> std::path::PathBuf {
    std::env::var_os("HOME").map_or_else(|| std::path::PathBuf::from("/"), std::path::PathBuf::from)
}

/// `clarp-admin agent helper-state SESSION [done|failed|running] [--from SESSION]`: shows
/// who made an agent and how its work stands, or marks a helper. `--from` names the agent
/// that marks it, which has to be the one that made it.
async fn agent_helper_state(app: &App, args: &[String]) -> ToolAnswer {
    let usage = |why: &str| {
        ToolAnswer::said(
            2,
            format!(
                "usage: clarp-admin agent helper-state [-h] [--from FROM_SESSION] \
                 session [{{done,failed,running}}]\n\
                 clarp-admin agent helper-state: error: {why}"
            ),
        )
    };
    let parsed = match parse(args, &["--from"], &[], &["session", "[state]"]) {
        Ok(parsed) => parsed,
        Err(why) => return usage(&why),
    };
    let session = parsed.positional.first().cloned().unwrap_or_default();
    let answered = match parsed.positional.get(1).map(String::as_str) {
        None => roster::lineage(app, &session),
        Some(state @ ("done" | "failed" | "running")) => {
            let mut asked = roster::Fields::new();
            asked.insert("session".to_string(), json!(session));
            asked.insert("state".to_string(), json!(state));
            if let Some(by) = parsed.option("--from").filter(|by| !by.is_empty()) {
                asked.insert("by".to_string(), json!(by));
            }
            roster::mark_helper(app, &asked).await
        }
        Some(other) => {
            return usage(&format!(
                "argument state: invalid choice: '{other}' (choose from 'done', 'failed', 'running')"
            ));
        }
    };
    match answered {
        Ok(answer) => shown(&answer),
        Err(refused) => {
            let (status, body) = refused.answered();
            ToolAnswer::said(1, format!("clarp-admin: HTTP {status}: {body}"))
        }
    }
}

/// `clarp-admin agent ...`: what an agent does about other agents.
async fn agent(app: &App, call: &ToolCall, args: &[String]) -> ToolAnswer {
    match args.split_first() {
        Some((command, rest)) if command == "create" => agent_create(app, call, rest).await,
        Some((command, rest)) if command == "helper-state" => agent_helper_state(app, rest).await,
        // A helper that starts from its maker's conversation is not built yet.
        Some((command, _)) => not_here(&format!("agent {command}")),
        None => ToolAnswer::said(2, "usage: clarp-admin agent {create,fork,helper-state} ..."),
    }
}

/// Answers `clarp-admin`.
pub async fn run(app: &App, call: &ToolCall) -> ToolAnswer {
    match call.argv.split_first() {
        Some((command, rest)) if command == "prompt" => prompt(app, rest).await,
        Some((command, rest)) if command == "agent" => agent(app, call, rest).await,
        Some((command, _)) if command == "sessions" => sessions(app),
        Some((command, rest)) if command == "model" => model(app, call, rest).await,
        Some((command, rest)) if command == "request" => request_tool::request(app, rest).await,
        Some((command, rest)) if command == "reply" => request_tool::reply(app, rest).await,
        Some((command, _)) => not_here(command),
        None => ToolAnswer::said(2, "usage: clarp-admin COMMAND ..."),
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use super::*;

    #[test]
    fn what_this_host_does_not_have_of_clarp_admin_says_so() {
        assert_eq!(not_here("update").exit, NOT_HERE);
        let said = usage("the following arguments are required: --to");
        assert_eq!(said.exit, 2);
        assert!(said.stderr.contains("{automation,watcher}"));
        assert!(
            said.stderr.ends_with(
                "clarp-admin prompt: error: the following arguments are required: --to\n"
            )
        );
    }
}
