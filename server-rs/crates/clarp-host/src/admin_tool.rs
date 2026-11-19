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
//! Two more are what an agent asks of its user's phone: `clarp-admin location` gives where
//! the user is, asking the phone for it when what is known is old (the skill `location`),
//! and `clarp-admin calendar` asks for an event in the user's calendar (the skill
//! `calendar`).
//!
//! And an agent stops another's turn, or repairs it, with `clarp-admin stop` and `clarp-admin
//! repair` (the skill `clarp-sessions`), known by its own turn.
//!
//! Everything else `clarp-admin` does belongs to the Python installation: asked for here, it
//! says that it is not on this Host, and does nothing.

use clarp_backend::SESSION_ENV;
use clarp_kernel::python;
use clarp_protocol::requests::SendRequest;
use serde_json::{Value, json};

use crate::agent::Unavailable;
use crate::artifact_tool::parse;
use crate::edge::{self, App};
use crate::ids;
use crate::peers::{self, Failed};
use crate::tools::{NOT_HERE, ToolAnswer, ToolCall};
use crate::{janitors, request_tool, roster};
use axum::http::{Method, StatusCode};
use clarp_kernel::calls::{ADMINISTRATOR, Refused};

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
pub(crate) async fn prompt(app: &App, args: &[String]) -> ToolAnswer {
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

/// `clarp-admin stop` and `clarp-admin repair` (`--session --reason [--from]`): an agent stops
/// another's turn, or gives back a turn of another that nothing runs, saying who acts (`--from`,
/// recorded as `agent:<it>`; the user where it is not given) and why. Asked with the token of the
/// turn the command runs in, as the Python tool asks it (`X-Clarp-Turn`), so that an agent is
/// known by its turn and not by its word. What is printed is the Python tool's: the answer, as
/// `json.dumps(answer, indent=2)` writes it, or the refusal it does not catch.
async fn control(app: &App, call: &ToolCall, command: &str, args: &[String]) -> ToolAnswer {
    let usage = |why: &str| {
        ToolAnswer::said(
            2,
            format!(
                "usage: clarp-admin {command} [-h] --session SESSION --reason REASON \
                 [--from FROM_SESSION]\nclarp-admin {command}: error: {why}"
            ),
        )
    };
    let parsed = match parse(args, &["--session", "--reason", "--from"], &[], &[]) {
        Ok(parsed) => parsed,
        Err(why) => return usage(&why),
    };
    let missing: Vec<&str> = ["--session", "--reason"]
        .into_iter()
        .filter(|name| parsed.option(name).is_none())
        .collect();
    if !missing.is_empty() {
        let why = format!(
            "the following arguments are required: {}",
            missing.join(", ")
        );
        return usage(&why);
    }
    let session = parsed.option("--session").unwrap_or_default();
    let actor = match parsed.option("--from") {
        Some(from) if !from.is_empty() => format!("agent:{from}"),
        _ => "user".to_string(),
    };
    let body = json!({ "session": session, "actor": actor, "reason": parsed.option("--reason") });
    let token = call.env.get(crate::turn_identity::TURN_ENV);
    let token = token.map(String::as_str).filter(|token| !token.is_empty());
    let answered = if command == "stop" {
        let by =
            crate::controls::control_of(app, false, token, body.get("actor"), body.get("reason"));
        match by.await {
            Ok(by) => crate::controls::stop(app, session, by)
                .await
                .map(|stopped| {
                    json!({
                        "ok": stopped.ok, "terminated": stopped.terminated,
                        "stop_actor": stopped.stop_actor,
                        "stop_actor_verified": stopped.stop_actor_verified,
                        "stop_reason": stopped.stop_reason, "goals_paused": stopped.goals_paused,
                        "queue_paused": stopped.queue_paused,
                    })
                }),
            Err(refused) => Err(refused),
        }
    } else {
        let data = body.as_object().cloned().unwrap_or_default();
        crate::controls::repair(app, &data, false, token).await
    };
    match answered {
        Ok(answer) => ToolAnswer::printed(0, python_indented(&answer)),
        // The Python tool does not catch a refusal: it ends on its words.
        Err(refused) => {
            let status = refused.status();
            let words = status.canonical_reason().unwrap_or("Error");
            let said = format!(
                "urllib.error.HTTPError: HTTP Error {}: {words}",
                status.as_u16()
            );
            ToolAnswer::said(1, said)
        }
    }
}

/// A value as Python's `json.dumps(value, indent=2)` writes it: two spaces in, and every sign
/// beyond ASCII written as its escape.
fn python_indented(value: &Value) -> String {
    let shown = serde_json::to_string_pretty(value).unwrap_or_default();
    let mut out = String::with_capacity(shown.len());
    for sign in shown.chars() {
        if sign.is_ascii() {
            out.push(sign);
        } else {
            let mut units = [0u16; 2];
            for unit in sign.encode_utf16(&mut units) {
                out.push_str(&format!("\\u{unit:04x}"));
            }
        }
    }
    out
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
        asked.insert("cwd".to_string(), json!(named_from(call, cwd)));
    }
    match edge::make_agent(app, &asked).await {
        Ok(made) => shown(&made),
        Err((status, why)) => refused_by_host(status.as_u16(), &why),
    }
}

fn home() -> std::path::PathBuf {
    std::env::var_os("HOME").map_or_else(|| std::path::PathBuf::from("/"), std::path::PathBuf::from)
}

/// What a ring held as a live voice call is about, as a file has it: no larger than such a
/// context may be, in UTF-8, and JSON. `Err`: why it cannot be used, in words.
fn event_context(file: &std::path::Path) -> Result<Value, String> {
    use std::io::Read;
    let most = clarp_kernel::live_call::CONTEXT_BYTES;
    let opened = std::fs::File::open(file).map_err(|error| error.to_string())?;
    let mut raw = Vec::new();
    // One byte more than it may be, so that one too large is seen to be.
    let read = opened.take(most as u64 + 1).read_to_end(&mut raw);
    read.map_err(|error| error.to_string())?;
    if raw.len() > most {
        return Err(format!("over {most} bytes"));
    }
    let text = String::from_utf8(raw).map_err(|error| error.to_string())?;
    serde_json::from_str(&text).map_err(|error| error.to_string())
}

/// A path as it was written where the tool was called: `~` is the home of whoever runs
/// this Host, anything that does not begin at the root begins where the tool was called.
fn written_from(call: &ToolCall, path: &str) -> std::path::PathBuf {
    match path.strip_prefix("~/") {
        Some(rest) => home().join(rest),
        None if path == "~" => home(),
        None => std::path::Path::new(&call.cwd).join(path),
    }
}

/// The same path as the one it is, whatever it was written as: without the links on its
/// way, where it is there.
fn named_from(call: &ToolCall, path: &str) -> String {
    let whole = written_from(call, path);
    let whole = whole.canonicalize().unwrap_or(whole);
    whole.display().to_string()
}

/// `clarp-admin agent fork [NAME] (--task TEXT | --task-file FILE | --spec FILE) [--count N]
/// [--worktree REPO:BRANCH] [--cwd DIR] [--from SESSION] [--backend B] [--model M]
/// [--effort E]`: the agent that calls it forks itself into helpers that start from its
/// conversation and each get a task (the skill `clarp-sub-agents`). What it asks of the Host
/// is what the Python tool asks (`agent_fork_request`), and it prints the Host's answer as
/// that tool does: exit 1 when a child was not made.
async fn agent_fork(app: &App, call: &ToolCall, args: &[String]) -> ToolAnswer {
    let valued = [
        "--task",
        "--task-file",
        "--count",
        "--spec",
        "--worktree",
        "--cwd",
        "--from",
        "--backend",
        "--model",
        "--effort",
    ];
    let usage = |why: &str| {
        ToolAnswer::said(
            2,
            format!(
                "usage: clarp-admin agent fork [-h] [--task TASK] [--task-file TASK_FILE] \
                 [--count COUNT] [--spec SPEC] [--worktree WORKTREE] [--cwd CWD] \
                 [--from FROM_SESSION] [--backend BACKEND] [--model MODEL] [--effort EFFORT] \
                 [name]\n\
                 clarp-admin agent fork: error: {why}"
            ),
        )
    };
    let parsed = match parse(args, &valued, &[], &["[name]"]) {
        Ok(parsed) => parsed,
        Err(why) => return usage(&why),
    };
    let count = match parsed.whole("--count") {
        Ok(count) => count.unwrap_or(0),
        Err(why) => return usage(&why),
    };
    let stopped = |why: String| ToolAnswer::said(1, why);
    // What was given, and is something.
    let given = |name: &str| parsed.option(name).filter(|value| !value.is_empty());
    let told = |name: &str| call.env.get(name).map(String::as_str);
    let own = told("CLARP_SESSION").filter(|session| !session.is_empty());
    let own = own.or_else(|| told(SESSION_ENV).filter(|session| !session.is_empty()));
    let Some(parent) = given("--from").or(own) else {
        return stopped("agent fork needs --from SESSION or $CLARP_SESSION".to_string());
    };
    let read = |path: &str| {
        let file = written_from(call, path);
        std::fs::read_to_string(&file).map_err(|error| {
            stopped(format!(
                "clarp-admin agent fork: {}: {error}",
                file.display()
            ))
        })
    };
    let mut asked = roster::Fields::new();
    if let Some(spec) = given("--spec") {
        if spec == "-" {
            // A tool of this Host is a pipe to it, and what is piped into the tool is not
            // carried along.
            let why = "agent fork: --spec - (the list on standard input) is not read on this \
                       Host (the Rust Host): write the list to a file and name the file. \
                       Nothing was done.";
            return stopped(why.to_string());
        }
        let raw = match read(spec) {
            Ok(raw) => raw,
            Err(why) => return why,
        };
        let children = match serde_json::from_str::<Value>(&raw) {
            Ok(Value::Object(mut whole)) => whole.remove("children").unwrap_or(Value::Null),
            Ok(children) => children,
            Err(error) => {
                return stopped(format!(
                    "clarp-admin agent fork: --spec is not JSON: {error}"
                ));
            }
        };
        if !children
            .as_array()
            .is_some_and(|children| !children.is_empty())
        {
            let why = "--spec must hold a JSON list of {name, task, ...} children";
            return stopped(why.to_string());
        }
        asked.insert("children".to_string(), children);
    } else {
        let task = match given("--task-file") {
            Some(file) => match read(file) {
                Ok(task) => python::strip(&task).to_string(),
                Err(why) => return why,
            },
            None => python::strip(parsed.option("--task").unwrap_or("")).to_string(),
        };
        if task.is_empty() {
            let why = "agent fork needs --task TEXT, --task-file FILE or --spec FILE";
            return stopped(why.to_string());
        }
        let Some(name) = parsed.positional.first().filter(|name| !name.is_empty()) else {
            return stopped("agent fork needs a NAME (or --spec FILE)".to_string());
        };
        let mut child = roster::Fields::new();
        child.insert("name".to_string(), json!(name));
        child.insert("task".to_string(), json!(task));
        if let Some(cwd) = given("--cwd") {
            child.insert("cwd".to_string(), json!(named_from(call, cwd)));
        }
        if let Some(tree) = given("--worktree") {
            let Some((repo, branch)) = tree
                .rsplit_once(':')
                .filter(|(repo, branch)| !repo.is_empty() && !branch.is_empty())
            else {
                return stopped("--worktree must be REPO:BRANCH".to_string());
            };
            let tree = json!({ "repo": named_from(call, repo), "branch": branch });
            child.insert("worktree".to_string(), tree);
        }
        if count == 0 {
            asked.insert("children".to_string(), json!([child]));
        } else {
            // So many of the one task: the Host names them and gives each its branch.
            asked.insert("count".to_string(), json!(count));
            asked.append(&mut child);
        }
    }
    for key in ["backend", "model", "effort"] {
        if let Some(value) = given(&format!("--{key}")) {
            asked.insert(key.to_string(), json!(value));
        }
    }
    match crate::forks::fork_agent(app, parent, &Value::Object(asked)).await {
        Ok(answer) => {
            let children = answer["children"].as_array();
            let failed = children.is_some_and(|children| {
                let mut children = children.iter();
                children.any(|child| child.get("error").is_some())
            });
            let mut said = shown(&answer);
            said.exit = if failed { 1 } else { said.exit };
            said
        }
        Err((status, why)) => refused_by_host(status.as_u16(), &why),
    }
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
        Some((command, rest)) if command == "fork" => agent_fork(app, call, rest).await,
        Some((command, _)) => not_here(&format!("agent {command}")),
        None => ToolAnswer::said(2, "usage: clarp-admin agent {create,fork,helper-state} ..."),
    }
}

/// `clarp-admin call status|start|end|add|remove|hold|resume|switch|transfer`: an agent
/// manages its user's call (the skill `clarp-calls`): who is in it, on hold, or has the
/// floor. The answer is printed as the Python tool prints it: the Host's answer as JSON, and
/// exit 1 when the Host refuses. The agent that asks is the one whose turn this is.
async fn call(app: &App, tool: &ToolCall, args: &[String]) -> ToolAnswer {
    use clarp_kernel::calls::{ADMINISTRATOR, Asker};
    let usage = |why: &str| {
        let commands = "{status,start,end,add,remove,hold,resume,switch,transfer,ring,ring-status,\
                        ring-cancel,rings}";
        ToolAnswer::said(
            2,
            format!("usage: clarp-admin call [-h] {commands} ...\nclarp-admin call: error: {why}"),
        )
    };
    let Some((command, rest)) = args.split_first() else {
        return usage("the following arguments are required: call_command");
    };
    // What stands for itself after the command: no agent, one, or one and more.
    const MANY: [&str; 17] = [
        "AGENT", "[AGENT]", "[AGENT]", "[AGENT]", "[AGENT]", "[AGENT]", "[AGENT]", "[AGENT]",
        "[AGENT]", "[AGENT]", "[AGENT]", "[AGENT]", "[AGENT]", "[AGENT]", "[AGENT]", "[AGENT]",
        "[AGENT]",
    ];
    let names: &[&str] = match command.as_str() {
        "status" | "end" => &[],
        "start" => &MANY,
        "add" | "remove" | "hold" | "resume" | "switch" | "transfer" => &MANY[..1],
        "ring" | "ring-status" | "ring-cancel" | "rings" => {
            return ring(app, tool, command, rest).await;
        }
        other => {
            let said = python::text(other);
            return usage(&format!("argument call_command: invalid choice: {said}"));
        }
    };
    let parsed = match parse(rest, &["--call", "--principal"], &[], names) {
        Ok(parsed) => parsed,
        Err(why) => return usage(&why),
    };
    let agents = &parsed.positional;
    let by = tool
        .env
        .get(SESSION_ENV)
        .map_or("", |session| session.trim());
    let by = app
        .member(by)
        .map_or_else(String::new, |member| member.record.session.clone());
    let (call_id, principal) = (
        parsed.option("--call").unwrap_or("").to_string(),
        parsed.option("--principal").unwrap_or("").to_string(),
    );
    let asker = Asker {
        caller: ADMINISTRATOR,
        call_id: &call_id,
        principal: &principal,
        by: &by,
    };
    let desk = app.calls.clone();
    let asked = desk.ask_public(app, |calls, at, (roster, new_id), out| {
        match command.as_str() {
            "status" => calls.snapshot(asker, at, out),
            "start" => {
                let asker = Asker {
                    call_id: "",
                    ..asker
                };
                calls.start(asker, (agents, "", None), (roster, new_id), at, out)
            }
            "end" => calls.end(asker, at, out),
            action => calls.change(asker, (action, &agents[0]), roster, at, out),
        }
    });
    match asked.await {
        Ok(Ok(answer)) => shown(&answer),
        Ok(Err(refused)) => match serde_json::to_string_pretty(&refused.body) {
            Ok(body) => ToolAnswer::printed(1, body),
            Err(error) => ToolAnswer::said(1, format!("clarp-admin: {error}")),
        },
        Err(unavailable) => ToolAnswer::said(1, format!("clarp-admin call: {}", unavailable.0)),
    }
}

/// `clarp-admin oracle connect AGENT [--principal PRINCIPAL]`: an agent puts the user's call
/// of the Oracle through to an agent, or back to the Oracle (`docs/oracle-handoff.md`). It is
/// answered once the phone has settled the handoff, and printed as the Python tool prints
/// it: the Host's answer as JSON, and exit 1 where the Host refuses.
async fn oracle(app: &App, args: &[String]) -> ToolAnswer {
    let usage = |of: &str, line: &str, why: &str| {
        let said = format!("usage: clarp-admin {of} [-h] {line}\nclarp-admin {of}: error: {why}");
        ToolAnswer::said(2, said)
    };
    let Some((command, rest)) = args.split_first() else {
        let why = "the following arguments are required: oracle_command";
        return usage("oracle", "{connect} ...", why);
    };
    if command != "connect" {
        let said = python::text(command);
        let why =
            format!("argument oracle_command: invalid choice: {said} (choose from 'connect')");
        return usage("oracle", "{connect} ...", &why);
    }
    let parsed = match parse(rest, &["--principal"], &[], &["agent"]) {
        Ok(parsed) => parsed,
        Err(why) => return usage("oracle connect", "[--principal PRINCIPAL] agent", &why),
    };
    let agent = json!(parsed.positional[0]);
    let phone = json!(parsed.option("--principal").unwrap_or(""));
    let administrator = clarp_kernel::oracle::handoffs::ADMINISTRATOR;
    match crate::oracle::put_through(app, administrator, &agent, &phone).await {
        Ok(Ok(answer)) => shown(&answer),
        Ok(Err(refused)) => match serde_json::to_string_pretty(&refused.body()) {
            Ok(body) => ToolAnswer::printed(1, body),
            Err(error) => ToolAnswer::said(1, format!("clarp-admin: {error}")),
        },
        Err(error) => ToolAnswer::said(1, format!("clarp-admin oracle: {}", error.message())),
    }
}

/// What the Host answered a request about a ring, as the Python tool prints it: the answer
/// or the refusal as JSON, and exit 1 for a refusal.
fn ring_said(asked: Result<Result<Value, Refused>, Unavailable>) -> ToolAnswer {
    match asked {
        Ok(Ok(answer)) => shown(&answer),
        Ok(Err(refused)) => match serde_json::to_string_pretty(&refused.body) {
            Ok(body) => ToolAnswer::printed(1, body),
            Err(error) => ToolAnswer::said(1, format!("clarp-admin: {error}")),
        },
        Err(unavailable) => ToolAnswer::said(1, format!("clarp-admin call: {}", unavailable.0)),
    }
}

/// How often a tool that waits for its ring asks how it stands, and how long at most.
const RING_ASKED_EVERY_MS: u64 = 2_000;
const RING_WAITED_MS: i64 = 90_000;

/// `clarp-admin call ring --reason WHY --say LINE [--agent A] [--wait]`: an agent calls its
/// owner (the skill `clarp-calls`, `docs/agent-rings.md`): the phone rings with the reason,
/// and when the user answers, the line is heard first, in the agent's own voice. The agent
/// whose turn this is rings as itself; outside a turn, `--agent` names who rings. With
/// `--wait` the tool answers when the call was answered, declined or missed.
/// `ring-status [RING_ID]` reads a ring, `ring-cancel RING_ID` takes one back, and
/// `rings on|off|status` is the owner's switch. Printed as the Python tool prints it.
async fn ring(app: &App, tool: &ToolCall, command: &str, rest: &[String]) -> ToolAnswer {
    let usage = |line: &str, why: &str| {
        ToolAnswer::said(
            2,
            format!(
                "usage: clarp-admin call {command} [-h] {line}\nclarp-admin call {command}: error: {why}"
            ),
        )
    };
    // The agent whose turn this is: it rings, and takes back, as itself only.
    let own = tool
        .env
        .get(SESSION_ENV)
        .map_or("", |session| session.trim());
    let own = app
        .member(own)
        .map_or_else(String::new, |member| member.record.agent_id.clone());
    let desk = app.calls.clone();
    match command {
        "ring" => {
            let line = "--reason REASON --say SAY [--agent AGENT] [--wait] [--oracle] \
                        [--context-file CONTEXT_FILE] [--listen-first]";
            let valued = ["--reason", "--say", "--agent", "--context-file"];
            let flags = ["--wait", "--oracle", "--listen-first"];
            let parsed = match parse(rest, &valued, &flags, &[]) {
                Ok(parsed) => parsed,
                Err(why) => return usage(line, &why),
            };
            let missing: Vec<&str> = ["--reason", "--say"]
                .into_iter()
                .filter(|name| parsed.option(name).is_none())
                .collect();
            if !missing.is_empty() {
                let why = format!(
                    "the following arguments are required: {}",
                    missing.join(", ")
                );
                return usage(line, &why);
            }
            let (live, context) = (
                parsed.flags.contains("--oracle"),
                parsed
                    .option("--context-file")
                    .is_some_and(|file| !file.is_empty()),
            );
            if context && !live {
                return ToolAnswer::said(2, "clarp-admin call ring: --context-file needs --oracle");
            }
            if live && !context {
                return ToolAnswer::said(2, "clarp-admin call ring: --oracle needs --context-file");
            }
            // A tool that waits asks again every moment with what it was called with: the
            // ring it placed the first time is the one it reads from then on.
            let waits = parsed.flags.contains("--wait") && tool.started_ms > 0;
            let tool_call = format!("{}:{}", tool.ppid, tool.started_ms);
            let placed = match desk.waited_for(&tool_call).filter(|_| waits) {
                Some(ring_id) => ring_id,
                None => {
                    let mut body = json!({
                        "reason": parsed.option("--reason"), "opening_line": parsed.option("--say"),
                    });
                    if let Some(agent) = parsed.option("--agent").filter(|agent| !agent.is_empty())
                    {
                        body["agent"] = json!(agent);
                    }
                    if live {
                        // What the ring is about is read here, where the tool was called,
                        // and sent whole: the Host checks it as it checks any.
                        let named = parsed.option("--context-file").unwrap_or_default();
                        match event_context(&written_from(tool, named)) {
                            Ok(context) => body["event_context"] = context,
                            Err(why) => {
                                let said = format!(
                                    "clarp-admin call ring: cannot use --context-file: {why}"
                                );
                                return ToolAnswer::said(2, &said);
                            }
                        }
                        body["conversation_mode"] = json!("agent_live_v1");
                    }
                    if parsed.flags.contains("--listen-first") {
                        body["opening_mode"] = json!("listen_first");
                    }
                    let rung = desk.ring(app, &own, &body).await;
                    let ring_id = match &rung {
                        Ok(Ok(answer)) => answer["ring"]["ring_id"].as_str().unwrap_or(""),
                        _ => "",
                    };
                    if !waits || ring_id.is_empty() {
                        return ring_said(rung);
                    }
                    desk.wait_for(&tool_call, ring_id, app.clock.now_ms());
                    ring_id.to_string()
                }
            };
            let looked = desk.rings(app, |rings, _, with, (out, _)| {
                rings.look_as((&placed, ADMINISTRATOR), &with.world, with.at.now_ms, out)
            });
            let looked = looked.await;
            let rings = matches!(&looked, Ok(Ok(seen)) if seen["ring"]["state"] == "ringing");
            if rings && app.clock.now_ms() < tool.started_ms + RING_WAITED_MS {
                return ToolAnswer::again(RING_ASKED_EVERY_MS);
            }
            desk.waited(&tool_call);
            ring_said(looked)
        }
        "ring-status" => {
            let parsed = match parse(rest, &[], &[], &["[ring_id]"]) {
                Ok(parsed) => parsed,
                Err(why) => return usage("[ring_id]", &why),
            };
            let ring_id = parsed.positional.first().cloned().unwrap_or_default();
            let looked = desk.rings(app, |rings, _, with, (out, _)| {
                rings.look_as((&ring_id, ADMINISTRATOR), &with.world, with.at.now_ms, out)
            });
            ring_said(looked.await)
        }
        "ring-cancel" => {
            let parsed = match parse(rest, &[], &[], &["ring_id"]) {
                Ok(parsed) => parsed,
                Err(why) => return usage("ring_id", &why),
            };
            let ring_id = &parsed.positional[0];
            let cancelled = desk.rings(app, |rings, _, with, (out, _)| {
                rings.cancel((&own, ring_id), with.at.now_ms, out)
            });
            ring_said(cancelled.await)
        }
        _ => {
            let parsed = match parse(rest, &[], &[], &["switch"]) {
                Ok(parsed) => parsed,
                Err(why) => return usage("{on,off,status}", &why),
            };
            let switch = parsed.positional[0].as_str();
            if !matches!(switch, "on" | "off" | "status") {
                let said = python::text(switch);
                let why = format!(
                    "argument switch: invalid choice: {said} (choose from 'on', 'off', 'status')"
                );
                return usage("{on,off,status}", &why);
            }
            let changed = desk.rings(app, |rings, _, with, (out, _)| match switch {
                "status" => Ok(rings.shown()),
                switch => rings.change(&json!({ "enabled": switch == "on" }), with.at.now_ms, out),
            });
            ring_said(changed.await)
        }
    }
}

/// A moment as the Python tool prints it: `2026-11-17 09:00:00 UTC`.
fn utc(ms: i64) -> String {
    let seconds = ms.div_euclid(1000);
    let (days, of_day) = (seconds.div_euclid(86_400), seconds.rem_euclid(86_400));
    // The day counted from 1970-01-01 as a date of the calendar.
    let shifted = days + 719_468;
    let (era, of_era) = (shifted.div_euclid(146_097), shifted.rem_euclid(146_097));
    let year_of_era = (of_era - of_era / 1_460 + of_era / 36_524 - of_era / 146_096) / 365;
    let of_year = of_era - (365 * year_of_era + year_of_era / 4 - year_of_era / 100);
    let shifted_month = (5 * of_year + 2) / 153;
    let day = of_year - (153 * shifted_month + 2) / 5 + 1;
    let month = if shifted_month < 10 {
        shifted_month + 3
    } else {
        shifted_month - 9
    };
    let year = year_of_era + era * 400 + i64::from(month <= 2);
    let (hour, minute, second) = (of_day / 3_600, of_day % 3_600 / 60, of_day % 60);
    format!("{year:04}-{month:02}-{day:02} {hour:02}:{minute:02}:{second:02} UTC")
}

/// `clarp-admin schedule list|add|toggle|remove`: an agent, or the owner, sets what an agent
/// is sent at set times (the skill `clarp-self-prompt`). Printed as the Python tool prints
/// it; what the Host refuses is said in a line, with exit 1.
async fn schedule(app: &App, args: &[String]) -> ToolAnswer {
    let usage = |command: &str, line: &str, why: &str| {
        // The tool itself, or one of its commands, as argparse names it.
        let named = match command {
            "" => "clarp-admin schedule".to_string(),
            command => format!("clarp-admin schedule {command}"),
        };
        ToolAnswer::said(
            2,
            format!("usage: {named} [-h] {line}\n{named}: error: {why}"),
        )
    };
    let refused = |why: &str| ToolAnswer::said(1, format!("clarp-admin schedule: {why}"));
    let desk = app.schedules.clone();
    let Some((command, rest)) = args.split_first() else {
        let why = "the following arguments are required: schedule_command";
        return usage("", "{list,add,toggle,remove} ...", why);
    };
    match command.as_str() {
        "list" => {
            let parsed = match parse(rest, &["--session", "-s"], &[], &[]) {
                Ok(parsed) => parsed,
                Err(why) => return usage("list", "[--session SESSION]", &why),
            };
            let session = parsed.option("--session").or(parsed.option("-s"));
            let session = session.unwrap_or("").to_string();
            let listed = desk.ask(app, |schedules, _| {
                let listed = schedules.list(&session, "");
                listed.into_iter().cloned().collect::<Vec<_>>()
            });
            let listed = match listed.await {
                Ok(listed) => listed,
                Err(unavailable) => return refused(&unavailable.0),
            };
            if listed.is_empty() {
                return ToolAnswer::printed(0, "No scheduled jobs found.");
            }
            let mut lines = Vec::new();
            for schedule in listed {
                let status = if schedule.enabled {
                    "ENABLED"
                } else {
                    "DISABLED"
                };
                lines.push(format!(
                    "\u{2022} [{status}] {} (ID: {})",
                    schedule.name, schedule.schedule_id
                ));
                lines.push(format!(
                    "   Session: {} | Cron: {}",
                    schedule.session, schedule.cron_expression
                ));
                lines.push(format!("   Prompt:  {}", schedule.prompt));
                if let Some(next) = schedule.next_run_at.filter(|next| *next != 0) {
                    lines.push(format!("   Next:    {}", utc(next)));
                }
                lines.push(String::new());
            }
            ToolAnswer::printed(0, lines.join("\n"))
        }
        "add" => {
            let line = "--name NAME --cron CRON --prompt PROMPT [--disabled] session";
            let valued = ["--name", "-n", "--cron", "-c", "--prompt", "-p"];
            let parsed = match parse(rest, &valued, &["--disabled"], &["session"]) {
                Ok(parsed) => parsed,
                Err(why) => return usage("add", line, &why),
            };
            let given = |long: &str, short: &str| parsed.option(long).or(parsed.option(short));
            let (name, cron, prompt) = (
                given("--name", "-n"),
                given("--cron", "-c"),
                given("--prompt", "-p"),
            );
            let missing: Vec<&str> = [
                ("--name/-n", name),
                ("--cron/-c", cron),
                ("--prompt/-p", prompt),
            ]
            .into_iter()
            .filter(|(_, given)| given.is_none())
            .map(|(option, _)| option)
            .collect();
            if !missing.is_empty() {
                let why = format!(
                    "the following arguments are required: {}",
                    missing.join(", ")
                );
                return usage("add", line, &why);
            }
            let session = python::strip(&parsed.positional[0]).to_string();
            let said = |given: Option<&str>| python::strip(given.unwrap_or("")).to_string();
            let (name, cron, prompt) = (said(name), said(cron), said(prompt));
            if [&session, &name, &cron, &prompt]
                .iter()
                .any(|given| given.is_empty())
            {
                let why = "session, name, cron_expression, and prompt are required";
                return refused(&format!("HTTP 400: {why}"));
            }
            let enabled = !parsed.flags.contains("--disabled");
            let made = desk.create(app, (&session, &name, &cron, &prompt), enabled);
            match made.await {
                Ok(Ok(made)) => ToolAnswer::printed(
                    0,
                    format!(
                        "\u{2713} Created schedule '{}' ({}) for {session}",
                        made["name"].as_str().unwrap_or(""),
                        made["schedule_id"].as_str().unwrap_or("")
                    ),
                ),
                Ok(Err(why)) => refused(&format!("HTTP 400: {why}")),
                Err(unavailable) => refused(&unavailable.0),
            }
        }
        "toggle" => {
            let line = "[--enable] [--disable] schedule_id";
            let flags = ["--enable", "--disable"];
            let parsed = match parse(rest, &[], &flags, &["schedule_id"]) {
                Ok(parsed) => parsed,
                Err(why) => return usage("toggle", line, &why),
            };
            let (on, off) = (
                parsed.flags.contains("--enable"),
                parsed.flags.contains("--disable"),
            );
            if !on && !off {
                return ToolAnswer::said(1, "Must specify --enable or --disable");
            }
            let schedule_id = parsed.positional[0].clone();
            let change = clarp_kernel::schedules::Change {
                enabled: Some(on),
                ..Default::default()
            };
            let done = desk.ask(app, |schedules, now| {
                let done = schedules.update(&schedule_id, &change, now);
                done.map(|schedule| schedule.is_some())
            });
            match done.await {
                Ok(Ok(true)) => {
                    let word = if on { "enabled" } else { "disabled" };
                    ToolAnswer::printed(0, format!("\u{2713} Schedule {schedule_id} {word}"))
                }
                Ok(Ok(false)) => refused("HTTP 404: schedule not found"),
                Ok(Err(why)) => refused(&format!("HTTP 400: {why}")),
                Err(unavailable) => refused(&unavailable.0),
            }
        }
        "remove" => {
            let parsed = match parse(rest, &[], &[], &["schedule_id"]) {
                Ok(parsed) => parsed,
                Err(why) => return usage("remove", "schedule_id", &why),
            };
            let schedule_id = parsed.positional[0].clone();
            let removed = desk.ask(app, |schedules, _| schedules.delete(&schedule_id));
            match removed.await {
                // As the Python tool: removing what is not there is no failure.
                Ok(_) => ToolAnswer::printed(0, format!("\u{2713} Schedule {schedule_id} removed")),
                Err(unavailable) => refused(&unavailable.0),
            }
        }
        other => {
            let said = python::text(other);
            let why = format!(
                "argument schedule_command: invalid choice: {said} (choose from 'list', 'add', \
                 'toggle', 'remove')"
            );
            usage("", "{list,add,toggle,remove} ...", &why)
        }
    }
}

// ---- clarp-admin janitor --------------------------------------------------------------------

/// A name as it stands in an address: what is no letter, no digit and none of `_.-~` as its
/// bytes in hex (Python's `quote(name, safe="")`).
fn quoted(name: &str) -> String {
    let mut out = String::new();
    for byte in name.bytes() {
        if byte.is_ascii_alphanumeric() || b"_.-~".contains(&byte) {
            out.push(char::from(byte));
        } else {
            out.push_str(&format!("%{byte:02X}"));
        }
    }
    out
}

/// A JSON object an option carries: written out, or `@` and the file it is in.
fn object_of(call: &ToolCall, given: &str) -> Result<serde_json::Map<String, Value>, String> {
    let raw = match given.strip_prefix('@') {
        Some(path) => {
            std::fs::read_to_string(written_from(call, path)).map_err(|error| error.to_string())?
        }
        None => given.to_string(),
    };
    if raw.len() > 1_048_576 {
        return Err("JSON exceeds 1 MiB".to_string());
    }
    match serde_json::from_str(&raw) {
        Ok(Value::Object(fields)) => Ok(fields),
        Ok(_) => Err("expected a JSON object".to_string()),
        Err(error) => Err(error.to_string()),
    }
}

/// A UUID as Python's `str(uuid.UUID(text))` writes it; `None` for what is none.
fn as_uuid(text: &str) -> Option<String> {
    let text = text.trim_start_matches("urn:").trim_start_matches("uuid:");
    let hex: String = text.chars().filter(|sign| !"{}-".contains(*sign)).collect();
    if hex.len() != 32 || !hex.chars().all(|sign| sign.is_ascii_hexdigit()) {
        return None;
    }
    let hex = hex.to_ascii_lowercase();
    let parts = [
        &hex[..8],
        &hex[8..12],
        &hex[12..16],
        &hex[16..20],
        &hex[20..],
    ];
    Some(parts.join("-"))
}

/// One request of the janitors' routes, taken as the Python tool takes an answer: what the
/// Host refuses ends the tool, with the Host's words and the tool's own around them.
async fn janitor_asked(
    app: &App,
    method: Method,
    path: &str,
    body: Option<&Value>,
) -> Result<Value, ToolAnswer> {
    let (status, answer) = janitors::asked(app, method, path, body).await;
    if status < 400 {
        return Ok(answer);
    }
    let mut detail = match answer.get("error") {
        Some(Value::String(why)) => why.clone(),
        Some(other) => other.to_string(),
        None => {
            let reason = StatusCode::from_u16(status).ok();
            let reason = reason.and_then(|status| status.canonical_reason());
            format!("HTTP Error {status}: {}", reason.unwrap_or_default())
        }
    };
    if status == 404 {
        detail =
            format!("Janitor or route unavailable; this Host may not support Janitors. {detail}");
    } else if status == 409 {
        detail.push_str(
            " Inspect the current configuration and repeat your intent; nothing was retried.",
        );
    }
    Err(ToolAnswer::said(
        1,
        format!("Janitor request failed ({status}): {detail}"),
    ))
}

/// A request that changes something: sent, or with `--dry-run` only shown.
async fn janitor_sent(
    app: &App,
    dry: bool,
    method: Method,
    path: &str,
    body: Option<Value>,
) -> Result<Value, ToolAnswer> {
    if dry {
        let shown =
            json!({ "dry_run": true, "method": method.as_str(), "path": path, "body": body });
        return Ok(shown);
    }
    janitor_asked(app, method, path, body.as_ref()).await
}

/// `clarp-admin trigger list`: the triggers a janitor can be given.
async fn trigger(app: &App, args: &[String]) -> ToolAnswer {
    if args.first().map(String::as_str) != Some("list") || args.len() != 1 {
        let why = "the following arguments are required: trigger_command";
        return ToolAnswer::said(
            2,
            format!(
                "usage: clarp-admin trigger [-h] {{list}} ...\nclarp-admin trigger: error: {why}"
            ),
        );
    }
    match janitor_asked(app, Method::GET, "/janitor-triggers", None).await {
        Ok(triggers) => shown(&triggers),
        Err(answer) => answer,
    }
}

/// `clarp-admin janitor ...`: the janitors of this Host, read and set by an agent's own
/// tool, and what a run says of itself (`outcome`). Each command asks the Host's own routes
/// what the Python tool asks them (`server/lib/janitor_cli.py`), and prints what it prints.
async fn janitor(app: &App, call: &ToolCall, args: &[String]) -> ToolAnswer {
    let usage = |command: &str, why: &str| {
        let named = match command {
            "" => "clarp-admin janitor".to_string(),
            command => format!("clarp-admin janitor {command}"),
        };
        ToolAnswer::said(2, format!("usage: {named} [-h] ...\n{named}: error: {why}"))
    };
    let failed = |why: &str| ToolAnswer::said(1, format!("Janitor request failed: {why}"));
    let Some((command, rest)) = args.split_first() else {
        return usage("", "the following arguments are required: janitor_command");
    };
    let command = command.as_str();
    let set = ["--expected-revision"];
    let (valued, flags, names): (&[&str], &[&str], &[&str]) = match command {
        "list" | "templates" => (&[], &[], &[]),
        "inspect" | "export" => (&[], &[], &["session"]),
        "runs" => (&["--limit"], &[], &["session"]),
        "enable" | "pause" | "remove" | "reset-defaults" => (&set, &["--dry-run"], &["session"]),
        "release" => (
            &["--expected-revision", "--successor", "--successor-revision"],
            &["--dry-run"],
            &["session"],
        ),
        "configure" => (
            &[
                "--expected-revision",
                "--config",
                "--scope",
                "--template",
                "--model",
                "--effort",
            ],
            &["--dry-run"],
            &["session"],
        ),
        "attach" => (
            &["--expected-revision", "--config", "--scope", "--trigger"],
            &["--dry-run"],
            &["session"],
        ),
        "create" => (
            &[
                "--agent",
                "--name",
                "--backend",
                "--cwd",
                "--template",
                "--model",
                "--effort",
                "--config",
                "--scope",
                "--request-id",
            ],
            &["--paused", "--dry-run"],
            &[],
        ),
        "run-context" => (&[], &[], &["run_id"]),
        "review" => (
            &[
                "--session",
                "--state-id",
                "--outcome",
                "--label",
                "--reason",
            ],
            &["--dry-run"],
            &["run_id"],
        ),
        "outcome" => (&["--status", "--summary"], &["--dry-run"], &["run_id"]),
        "migrate-pilot" => return not_here("janitor migrate-pilot"),
        other => {
            let why = format!("argument janitor_command: invalid choice: '{other}'");
            return usage("", &why);
        }
    };
    let parsed = match parse(rest, valued, flags, names) {
        Ok(parsed) => parsed,
        Err(why) => return usage(command, &why),
    };
    let dry = parsed.flags.contains("--dry-run");
    let first = parsed.positional.first().map_or("", String::as_str);
    // What an option has to be before anything is asked: a whole number above nothing, an
    // object, one of a few words; and the options a command cannot do without.
    let positive = |name: &str| match parsed.whole(name) {
        Ok(Some(number)) if number < 1 => {
            Err(format!("argument {name}: must be a positive integer"))
        }
        other => other,
    };
    let carried = |name: &str| match parsed.option(name) {
        None => Ok(None),
        Some(given) => match object_of(call, given) {
            Ok(fields) => Ok(Some(fields)),
            Err(why) => Err(format!("argument {name}: {why}")),
        },
    };
    let needed: &[&str] = match command {
        "outcome" => &["--status", "--summary"],
        "review" => &["--session", "--state-id", "--outcome", "--reason"],
        "attach" => &["--trigger"],
        _ => &[],
    };
    let missing: Vec<&str> = needed
        .iter()
        .copied()
        .filter(|name| parsed.option(name).is_none())
        .collect();
    if !missing.is_empty() {
        let why = format!(
            "the following arguments are required: {}",
            missing.join(", ")
        );
        return usage(command, &why);
    }
    let numbers = (
        positive("--expected-revision"),
        positive("--limit"),
        positive("--successor-revision"),
        positive("--state-id"),
    );
    let (expected, limit, successor_revision, state_id) = match numbers {
        (Ok(expected), Ok(limit), Ok(successor), Ok(state)) => (expected, limit, successor, state),
        (Err(why), ..) | (_, Err(why), ..) | (_, _, Err(why), _) | (_, _, _, Err(why)) => {
            return usage(command, &why);
        }
    };
    let (config, scope) = match (carried("--config"), carried("--scope")) {
        (Ok(config), Ok(scope)) => (config, scope),
        (Err(why), _) | (_, Err(why)) => return usage(command, &why),
    };
    let words = (
        parsed.choice("--status", &["worked", "idle", "failed"], "worked"),
        parsed.choice(
            "--outcome",
            &["changed", "same_task", "insufficient_context", "error"],
            "error",
        ),
    );
    let (status, outcome) = match words {
        (Ok(status), Ok(outcome)) => (status, outcome),
        (Err(why), _) | (_, Err(why)) => return usage(command, &why),
    };
    let option = |name: &str| parsed.option(name).map(str::to_string);
    // What a `configure` or a `create` sends: the object `--config` carries, with what the
    // other options name over it.
    let configuration = |create: bool| -> Result<serde_json::Map<String, Value>, String> {
        let mut body = config.clone().unwrap_or_default();
        let mut allowed = vec![
            "template_id",
            "scope",
            "attachments",
            "model",
            "effort",
            "execution",
            "backend",
            "options",
        ];
        if create {
            allowed.extend(["name", "cwd", "session", "request_id"]);
        }
        let mut unknown: Vec<&str> = body.keys().map(String::as_str).collect();
        unknown.retain(|key| !allowed.contains(key));
        unknown.sort_unstable();
        if !unknown.is_empty() {
            return Err(format!(
                "unsupported configuration fields: {}",
                unknown.join(", ")
            ));
        }
        for (name, key) in [
            ("--template", "template_id"),
            ("--model", "model"),
            ("--effort", "effort"),
        ] {
            if let Some(value) = option(name) {
                body.insert(key.to_string(), json!(value));
            }
        }
        if let Some(scope) = &scope {
            body.insert("scope".to_string(), Value::Object(scope.clone()));
        }
        Ok(body)
    };
    // What the tool says before it asks: the id of a request for a new janitor, so that the
    // request can be sent again whatever becomes of its answer.
    let mut said_first = String::new();
    let done: Result<Value, ToolAnswer> = async {
        let path = format!("/janitors/{}", quoted(first));
        let run_path = format!("/janitor-runs/{}", quoted(first));
        // The revision a change says it saw: the one it names, or the one the janitor has.
        let revision = async || -> Result<i64, ToolAnswer> {
            if let Some(expected) = expected {
                return Ok(expected);
            }
            let current = janitor_asked(app, Method::GET, &path, None).await?;
            Ok(current["janitor"]["revision"].as_i64().unwrap_or(0))
        };
        match command {
            "list" => janitor_asked(app, Method::GET, "/janitors", None).await,
            "templates" => {
                let all = janitor_asked(app, Method::GET, "/janitors", None).await?;
                Ok(json!({ "templates": all["templates"] }))
            }
            "inspect" => janitor_asked(app, Method::GET, &path, None).await,
            "runs" => {
                let asked = format!("{path}/runs?limit={}", limit.unwrap_or(30));
                janitor_asked(app, Method::GET, &asked, None).await
            }
            "export" => {
                let asked = format!("{path}/migration-state");
                janitor_asked(app, Method::GET, &asked, None).await
            }
            "reset-defaults" => {
                let body = json!({ "expected_revision": revision().await? });
                let asked = format!("{path}/reset-defaults");
                janitor_sent(app, dry, Method::POST, &asked, Some(body)).await
            }
            "configure" => {
                let mut body = configuration(false).map_err(|why| failed(&why))?;
                body.insert("expected_revision".to_string(), json!(revision().await?));
                let asked = format!("{path}/configure");
                janitor_sent(app, dry, Method::POST, &asked, Some(Value::Object(body))).await
            }
            "attach" => {
                let named = option("--trigger").unwrap_or_default();
                let read = named.rsplit_once('@').and_then(|(trigger, version)| {
                    let digits = !version.is_empty() && version.chars().all(|sign| sign.is_ascii_digit());
                    let version = version.parse::<i64>().ok().filter(|version| digits && *version >= 1);
                    version.filter(|_| !trigger.is_empty()).map(|version| (trigger, version))
                });
                let Some((trigger, version)) = read else {
                    let why = "--trigger must be a versioned identifier such as agent-work-completed@1";
                    return Err(failed(why));
                };
                let current = janitor_asked(app, Method::GET, &path, None).await?;
                let current = &current["janitor"];
                let seen = expected.or_else(|| current["revision"].as_i64()).unwrap_or(0);
                let keys = ["attachment_id", "trigger_id", "trigger_version", "enabled", "config"];
                let has = current["attachments"].as_array().map_or(&[][..], Vec::as_slice);
                let mut attachments: Vec<Value> = has
                    .iter()
                    .map(|row| {
                        let kept = keys.iter().filter_map(|key| Some(((*key).to_string(), row.get(*key)?.clone())));
                        Value::Object(kept.collect())
                    })
                    .collect();
                attachments.push(json!({
                    "trigger_id": trigger, "trigger_version": version, "enabled": true,
                    "config": config.clone().unwrap_or_default(),
                }));
                let mut body = json!({ "expected_revision": seen, "attachments": attachments });
                if let Some(scope) = &scope {
                    body["scope"] = Value::Object(scope.clone());
                }
                let asked = format!("{path}/configure");
                janitor_sent(app, dry, Method::POST, &asked, Some(body)).await
            }
            "enable" | "pause" => {
                let on = command == "enable";
                let body = json!({ "expected_revision": revision().await?, "enabled": on });
                let asked = format!("{path}/enabled");
                janitor_sent(app, dry, Method::POST, &asked, Some(body)).await
            }
            "remove" => {
                let asked = format!("{path}?expected_revision={}", revision().await?);
                janitor_sent(app, dry, Method::DELETE, &asked, None).await
            }
            "release" => {
                let mut body = json!({ "expected_revision": revision().await? });
                match (option("--successor"), successor_revision) {
                    (Some(successor), given) => {
                        let seen = match given {
                            Some(given) => given,
                            None => {
                                let other = format!("/janitors/{}", quoted(&successor));
                                let other = janitor_asked(app, Method::GET, &other, None).await?;
                                other["janitor"]["revision"].as_i64().unwrap_or(0)
                            }
                        };
                        body["successor_session"] = json!(successor);
                        body["successor_revision"] = json!(seen);
                    }
                    (None, Some(_)) => {
                        return Err(failed("--successor-revision requires --successor"));
                    }
                    (None, None) => {}
                }
                let asked = format!("{path}/release");
                janitor_sent(app, dry, Method::POST, &asked, Some(body)).await
            }
            "create" => {
                let mut body = configuration(true).map_err(|why| failed(&why))?;
                let truthy = |body: &serde_json::Map<String, Value>, key: &str| {
                    body.get(key).is_some_and(|value| match value {
                        Value::Null | Value::Bool(false) => false,
                        Value::String(text) => !text.is_empty(),
                        _ => true,
                    })
                };
                let identity = ["name", "backend", "cwd"];
                if let Some(agent) = option("--agent").filter(|agent| !agent.is_empty()) {
                    if identity.iter().any(|key| truthy(&body, key)) {
                        let why = "conversion takes --agent without new-agent identity fields";
                        return Err(failed(why));
                    }
                    body.insert("session".to_string(), json!(agent));
                } else {
                    for key in identity {
                        if let Some(value) = option(&format!("--{key}")) {
                            body.insert(key.to_string(), json!(value));
                        }
                    }
                    let whole = identity.iter().all(|key| truthy(&body, key));
                    if !truthy(&body, "session") && !whole {
                        return Err(failed("provide --agent or all of --name, --backend and --cwd"));
                    }
                }
                if !body.contains_key("template_id") {
                    body.insert("template_id".to_string(), json!("task-labels"));
                }
                let supplied = option("--request-id").or_else(|| {
                    let carried = body.get("request_id").filter(|id| !id.is_null());
                    carried.map(|id| id.as_str().map_or_else(|| id.to_string(), str::to_string))
                });
                if truthy(&body, "session") {
                    if supplied.is_some() {
                        let why = "--request-id is only for creating a new identity; conversion uses its existing session";
                        return Err(failed(why));
                    }
                } else {
                    // A new agent made as a janitor is one request however often it is sent:
                    // its key is said before the request, so that it can be sent again.
                    let request_id = match &supplied {
                        Some(supplied) => as_uuid(supplied),
                        None => as_uuid(&ids::hex(16)),
                    };
                    let Some(request_id) = request_id else {
                        return Err(failed("request_id must be a UUID"));
                    };
                    said_first = format!("{}\n", python::dumps(&json!({ "request_id": request_id })));
                    body.insert("request_id".to_string(), json!(request_id));
                }
                janitor_sent(app, dry, Method::POST, "/janitors", Some(Value::Object(body))).await
            }
            "run-context" => {
                let asked = format!("{run_path}/context");
                janitor_asked(app, Method::GET, &asked, None).await
            }
            "outcome" => {
                let body = json!({ "activity": status, "summary": option("--summary") });
                let asked = format!("{run_path}/activity");
                janitor_sent(app, dry, Method::POST, &asked, Some(body)).await
            }
            "review" => {
                let label = option("--label");
                if outcome == "changed" && label.as_deref().is_none_or(str::is_empty) {
                    return Err(failed("changed requires --label"));
                }
                if let Some(label) = &label {
                    let words = label.split(python::blank).filter(|word| !word.is_empty()).count();
                    if label.chars().count() > 20 || !(2..=3).contains(&words) {
                        let why = "a label must have 2\u{2013}3 words and at most 20 characters";
                        return Err(failed(why));
                    }
                }
                let mut body = json!({
                    "target_session": option("--session"), "observed_state_id": state_id,
                    "outcome": outcome, "reason": option("--reason"),
                });
                if let Some(label) = label {
                    body["label"] = json!(label);
                }
                let asked = format!("{run_path}/review");
                janitor_sent(app, dry, Method::POST, &asked, Some(body)).await
            }
            _ => Err(not_here("janitor")),
        }
    }
    .await;
    let mut answer = match done {
        Ok(value) => shown(&value),
        Err(answer) => answer,
    };
    answer.stderr = format!("{said_first}{}", answer.stderr);
    answer
}

/// What argparse says of the command line of a command that has no commands of its own,
/// when it cannot read it; and exits 2 with.
fn misread(command: &str, line: &str, why: &str) -> ToolAnswer {
    let named = format!("clarp-admin {command}");
    ToolAnswer::said(
        2,
        format!("usage: {named} [-h] {line}\n{named}: error: {why}"),
    )
}

/// The options a command must be given, of those it was not given: what argparse says.
fn left_out(parsed: &crate::artifact_tool::Parsed, needed: &[&str]) -> Option<String> {
    let missing: Vec<&str> = needed
        .iter()
        .copied()
        .filter(|name| parsed.option(name).is_none())
        .collect();
    (!missing.is_empty()).then(|| {
        format!(
            "the following arguments are required: {}",
            missing.join(", ")
        )
    })
}

/// How long `clarp-admin location` waits between two looks for a place that was asked for.
const LOOKED_EVERY: std::time::Duration = std::time::Duration::from_secs(2);

/// `clarp-admin location --session SESSION [--max-age SECONDS] [--timeout SECONDS]`: where
/// the agent's user is. The last place the phone said is given when it is no older than
/// `--max-age` (300); else the phone is asked, and the tool waits up to `--timeout` (35)
/// for a place said since. Printed as the Python tool prints it.
async fn location(app: &App, args: &[String]) -> ToolAnswer {
    const LINE: &str = "--session SESSION [--max-age MAX_AGE] [--timeout TIMEOUT]";
    let valued = ["--session", "--max-age", "--timeout"];
    let parsed = match parse(args, &valued, &[], &[]) {
        Ok(parsed) => parsed,
        Err(why) => return misread("location", LINE, &why),
    };
    if let Some(extra) = parsed.positional.first() {
        return misread(
            "location",
            LINE,
            &format!("unrecognized arguments: {extra}"),
        );
    }
    if let Some(why) = left_out(&parsed, &["--session"]) {
        return misread("location", LINE, &why);
    }
    let (max_age, timeout) = match (parsed.whole("--max-age"), parsed.whole("--timeout")) {
        (Ok(max_age), Ok(timeout)) => (max_age.unwrap_or(300), timeout.unwrap_or(35)),
        (Err(why), _) | (_, Err(why)) => return misread("location", LINE, &why),
    };
    let session = parsed.option("--session").unwrap_or_default();
    let unavailable =
        |why: Unavailable| ToolAnswer::said(1, format!("clarp-admin location: {}", why.0));
    let told = |place: &Value| {
        ToolAnswer::printed(0, serde_json::to_string_pretty(place).unwrap_or_default())
    };
    let said_at = |place: &Value| place["ts"].as_i64().filter(|at| *at != 0);
    let now = app.clock.now_ms();
    let current = match crate::places::last(app, session).await {
        Ok(current) => current,
        Err(why) => return unavailable(why),
    };
    if said_at(&current).is_some_and(|at| now - at <= max_age.saturating_mul(1_000)) {
        return told(&current);
    }
    crate::places::ask(app, python::strip(session));
    let waits_until = tokio::time::Instant::now()
        + std::time::Duration::from_secs(u64::try_from(timeout).unwrap_or(0));
    while tokio::time::Instant::now() < waits_until {
        tokio::time::sleep(LOOKED_EVERY).await;
        match crate::places::last(app, session).await {
            Ok(current) if said_at(&current).is_some_and(|at| at >= now) => return told(&current),
            Ok(_) => {}
            Err(why) => return unavailable(why),
        }
    }
    ToolAnswer::said(1, "location was not shared before timeout")
}

/// `clarp-admin calendar --session SESSION --title TITLE --start START --end END ...`: an
/// agent asks for an event in its user's calendar. What the Host answers is printed as the
/// Python tool prints it; what it refuses is said in a line, with exit 1.
fn calendar(app: &App, args: &[String]) -> ToolAnswer {
    const LINE: &str = "--session SESSION --title TITLE --start START --end END \
                        [--time-zone TIME_ZONE] [--location LOCATION] [--notes NOTES] [--url URL] \
                        [--calendar CALENDAR] [--all-day]";
    let valued = [
        "--session",
        "--title",
        "--start",
        "--end",
        "--time-zone",
        "--location",
        "--notes",
        "--url",
        "--calendar",
    ];
    let parsed = match parse(args, &valued, &["--all-day"], &[]) {
        Ok(parsed) => parsed,
        Err(why) => return misread("calendar", LINE, &why),
    };
    if let Some(extra) = parsed.positional.first() {
        return misread(
            "calendar",
            LINE,
            &format!("unrecognized arguments: {extra}"),
        );
    }
    if let Some(why) = left_out(&parsed, &["--session", "--title", "--start", "--end"]) {
        return misread("calendar", LINE, &why);
    }
    let said = |name: &str| parsed.option(name).unwrap_or_default();
    let asked = json!({
        "session": said("--session"), "title": said("--title"),
        "start": said("--start"), "end": said("--end"),
        "time_zone": said("--time-zone"), "location": said("--location"),
        "notes": said("--notes"), "url": said("--url"), "calendar": said("--calendar"),
        "all_day": parsed.flags.contains("--all-day"),
    });
    let Some(asked) = asked.as_object() else {
        return ToolAnswer::said(1, "clarp-admin calendar: the request could not be written");
    };
    match crate::calendar::request(app, asked) {
        Ok(answered) => ToolAnswer::printed(
            0,
            serde_json::to_string_pretty(&answered).unwrap_or_default(),
        ),
        Err(why) => ToolAnswer::said(
            1,
            format!("clarp-admin calendar: HTTP Error 400: Bad Request ({why})"),
        ),
    }
}

/// Answers `clarp-admin`.
pub async fn run(app: &App, call: &ToolCall) -> ToolAnswer {
    match call.argv.split_first() {
        Some((command, rest)) if command == "controller-action" => {
            crate::controller_action_tool::run(app, call, rest).await
        }
        Some((command, rest)) if command == "prompt" => prompt(app, rest).await,
        Some((command, rest)) if command == "agent" => agent(app, call, rest).await,
        Some((command, _)) if command == "sessions" => sessions(app),
        Some((command, rest)) if command == "model" => model(app, call, rest).await,
        Some((command, rest)) if command == "request" => {
            request_tool::request(
                app,
                rest,
                call.env
                    .get(crate::turn_identity::TURN_ENV)
                    .map(String::as_str),
            )
            .await
        }
        Some((command, rest)) if command == "reply" => {
            request_tool::reply(
                app,
                rest,
                call.env
                    .get(crate::turn_identity::TURN_ENV)
                    .map(String::as_str),
            )
            .await
        }
        Some((command, rest)) if command == "call" => self::call(app, call, rest).await,
        Some((command, rest)) if command == "oracle" => oracle(app, rest).await,
        Some((command, rest)) if command == "schedule" => schedule(app, rest).await,
        Some((command, rest)) if command == "janitor" => janitor(app, call, rest).await,
        Some((command, rest)) if command == "trigger" => trigger(app, rest).await,
        Some((command, rest)) if command == "location" => location(app, rest).await,
        Some((command, rest)) if command == "calendar" => calendar(app, rest),
        Some((command, rest)) if command == "stop" || command == "repair" => {
            control(app, call, command, rest).await
        }
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
