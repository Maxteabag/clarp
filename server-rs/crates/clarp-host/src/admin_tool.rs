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

use crate::agent::Unavailable;
use crate::artifact_tool::parse;
use crate::edge::{self, App};
use crate::ids;
use crate::peers::{self, Failed};
use crate::tools::{NOT_HERE, ToolAnswer, ToolCall};
use crate::{request_tool, roster};
use clarp_kernel::calls::Refused;

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
    let asked = desk.ask(app, |calls, at, (roster, new_id), out| {
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
                        // What a live voice call would be told stays where it is: this Host
                        // holds none, and says so.
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
                rings.look(&placed, with.at.now_ms, out)
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
                rings.look(&ring_id, with.at.now_ms, out)
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

/// Answers `clarp-admin`.
pub async fn run(app: &App, call: &ToolCall) -> ToolAnswer {
    match call.argv.split_first() {
        Some((command, rest)) if command == "prompt" => prompt(app, rest).await,
        Some((command, rest)) if command == "agent" => agent(app, call, rest).await,
        Some((command, _)) if command == "sessions" => sessions(app),
        Some((command, rest)) if command == "model" => model(app, call, rest).await,
        Some((command, rest)) if command == "request" => request_tool::request(app, rest).await,
        Some((command, rest)) if command == "reply" => request_tool::reply(app, rest).await,
        Some((command, rest)) if command == "call" => self::call(app, call, rest).await,
        Some((command, rest)) if command == "schedule" => schedule(app, rest).await,
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
