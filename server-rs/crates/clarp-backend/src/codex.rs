//! Codex, run for one turn as `codex exec --json`.
//!
//! One process per turn: the message goes in on standard input, and Codex prints one JSON
//! object per line until the turn is over and then exits. A turn that continues a conversation
//! is `codex exec resume <thread>`. Codex names its thread itself, in its first line; the Host
//! cannot choose the name, so it learns it there and resumes it from then on.
//!
//! The stream, as codex-cli 0.167.1 prints it (the fixtures in `tests/fixtures` are recordings):
//!
//! ```text
//! {"type":"thread.started","thread_id":"…"}
//! {"type":"turn.started"}
//! {"type":"item.started","item":{"id":"item_0","type":"command_execution","command":"…","status":"in_progress",…}}
//! {"type":"item.completed","item":{"id":"item_0","type":"command_execution","aggregated_output":"…","exit_code":0,"status":"completed"}}
//! {"type":"item.completed","item":{"id":"item_1","type":"agent_message","text":"…"}}
//! {"type":"turn.completed","usage":{"input_tokens":1,"output_tokens":1,…}}
//! ```
//!
//! An item's id counts from `item_0` in every turn, so the ids of what a turn puts into the
//! conversation are built from the turn's own name. A turn that fails says so with `turn.failed`
//! (and an `error` line) and exits with 1. An item of type `error` is not a failure: it is
//! Codex remarking on its own configuration, and it is left out.

use std::collections::{BTreeMap, BTreeSet};
use std::path::{Path, PathBuf};

use clarp_kernel::conversation::{Reply, ToolCall, ToolStatus};
use clarp_kernel::live::Glimpse;
use clarp_kernel::told;
use clarp_kernel::turn::{Activity, RunOutcome};
use clarp_spool::{Entry, Record, ShimEvent};
use serde_json::{Value, json};

use crate::{
    Apart, Backend, ErrorTail, Interpreter, Launch, LiveReply, Question, Reading, SESSION_ENV,
    Signal, TurnSpec, shorten,
};

/// How much of what a tool gave back is kept with the call.
const MAX_TOOL_OUTPUT: usize = 4_000;
/// What Codex is started without when it is only to read ([`Backend::reads`]).
const NOTHING_TO_ACT_WITH: [&str; 18] = [
    "shell_tool",
    "unified_exec",
    "apps",
    "plugins",
    "hooks",
    "memories",
    "multi_agent",
    "multi_agent_v2",
    "browser_use",
    "computer_use",
    "image_generation",
    "view_image",
    "code_mode_host",
    "remote_plugin",
    "skill_search",
    "shell_snapshot",
    "goals",
    "sleep_tool",
];
/// What of the Host's environment Codex is left with then: where it is, who it runs as,
/// where its login lies, and how it reaches its provider.
const KEPT_APART: [&str; 22] = [
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "LANG",
    "LC_ALL",
    "CODEX_HOME",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_RUNTIME_DIR",
    "OPENAI_API_KEY",
    "DBUS_SESSION_BUS_ADDRESS",
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "https_proxy",
    "http_proxy",
    "all_proxy",
    "no_proxy",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
];

pub struct Codex {
    program: String,
}

impl Codex {
    pub fn new() -> Self {
        Self::with_program("codex")
    }

    /// Codex under another name or path.
    pub fn with_program(program: impl Into<String>) -> Self {
        Self {
            program: program.into(),
        }
    }
}

impl Default for Codex {
    fn default() -> Self {
        Self::new()
    }
}

/// A value for `-c key=value`, which Codex reads as TOML: a JSON string is a TOML string.
fn setting(key: &str, value: &str) -> String {
    format!("{key}={}", Value::String(value.to_string()))
}

impl Backend for Codex {
    fn id(&self) -> &'static str {
        "codex"
    }

    fn efforts(&self) -> &'static [&'static str] {
        &["low", "medium", "high", "xhigh", "max", "ultra"]
    }

    fn launch(&self, spec: &TurnSpec) -> Launch {
        let mut args: Vec<String> = [
            "exec",
            "--json",
            "--dangerously-bypass-approvals-and-sandbox",
        ]
        .iter()
        .map(|arg| (*arg).to_string())
        .collect();
        let given = |value: &Option<String>| value.clone().filter(|value| !value.trim().is_empty());
        if let Some(model) = given(&spec.model) {
            args.push("--model".to_string());
            args.push(model);
        }
        if let Some(effort) = given(&spec.effort) {
            args.push("-c".to_string());
            args.push(setting("model_reasoning_effort", &effort));
        }
        // Who the agent is, as a developer message on top of Codex's own instructions.
        if let Some(identity) = given(&spec.identity) {
            args.push("-c".to_string());
            args.push(setting("developer_instructions", &identity));
        }
        // A thread it has: continue it. Codex names a new one itself.
        if let Some(thread) = given(&spec.backend_session) {
            args.push("resume".to_string());
            args.push(thread);
        }
        // The message is read from standard input: it never has to fit a command line.
        args.push("-".to_string());
        // What the Host says with the prompt comes before it, between the marks the Python
        // Host puts around it for this tool, so that whoever reads Codex's own record of the
        // conversation finds the user's words again (`told::unheaded`).
        let prompt = match &spec.told {
            Some(told) => {
                let head = told::at_head(&told.persona, &spec.session, told.spoken);
                let head = told::in_call(head, told.call.as_deref());
                told::headed(&head, None, &spec.prompt)
            }
            None => spec.prompt.clone(),
        };
        Launch {
            prompt_as_file: false,
            before_stop: None,
            program: self.program.clone(),
            args,
            env: vec![(SESSION_ENV.to_string(), spec.session.clone())],
            stdin: prompt.into_bytes(),
        }
    }

    /// Codex's own directory (`CODEX_HOME`, else `.codex` at home) holds the login it runs
    /// with.
    fn login_file(&self, home: &Path) -> Option<PathBuf> {
        let dir = std::env::var_os("CODEX_HOME")
            .filter(|dir| !dir.is_empty())
            .map_or_else(|| home.join(".codex"), PathBuf::from);
        Some(dir.join("auth.json"))
    }

    fn interpreter(&self, turn: u64, name: &str) -> Box<dyn Interpreter> {
        Box::new(CodexInterpreter::new(turn, name))
    }

    /// One `codex exec` that is not kept (`--ephemeral`), may write only where it runs, and
    /// starts where there is no repository too: as the Python Host asks Codex one question
    /// (`backend/codex.py`, `routing_cmd`).
    fn once(&self, question: &Question) -> Option<Launch> {
        let mut args: Vec<String> = [
            "exec",
            "--json",
            "--sandbox",
            "workspace-write",
            "--ephemeral",
            "--skip-git-repo-check",
        ]
        .iter()
        .map(|arg| (*arg).to_string())
        .collect();
        let given = |value: &Option<String>| value.clone().filter(|value| !value.trim().is_empty());
        if let Some(model) = given(&question.model) {
            args.push("--model".to_string());
            args.push(model);
        }
        if let Some(effort) = given(&question.effort) {
            args.push("-c".to_string());
            args.push(setting("model_reasoning_effort", &effort));
        }
        args.push("-".to_string());
        Some(Launch {
            prompt_as_file: false,
            before_stop: None,
            program: self.program.clone(),
            args,
            env: Vec::new(),
            stdin: question.prompt.clone().into_bytes(),
        })
    }

    /// One `codex exec` that can read and do nothing else: no shell, no tools, no plugins,
    /// no search, none of the user's own settings and rules, a sandbox it may only read in,
    /// and an answer that has to have the shape it is given. As the Python Host starts
    /// Codex to explain the use of tools (`lib/tool_explanations.py`, `_run_codex`).
    fn reads(&self, reading: &Reading) -> Option<Apart> {
        let mut args: Vec<String> = [
            "exec",
            "--ignore-user-config",
            "--ignore-rules",
            "--ephemeral",
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
            "--json",
            "--color",
            "never",
            "--output-schema",
            "{dir}/schema.json",
        ]
        .iter()
        .map(|arg| (*arg).to_string())
        .collect();
        let given = |value: &Option<String>| value.clone().filter(|value| !value.trim().is_empty());
        if let Some(model) = given(&reading.model) {
            args.push("--model".to_string());
            args.push(model);
        }
        let mut settings = vec![
            setting("approval_policy", "never"),
            setting("web_search", "disabled"),
            "project_doc_max_bytes=0".to_string(),
            "mcp_servers={}".to_string(),
            setting("model_instructions_file", "{dir}/instructions.txt"),
        ];
        if let Some(effort) = given(&reading.effort) {
            settings.push(setting("model_reasoning_effort", &effort));
        }
        for setting in settings {
            args.push("-c".to_string());
            args.push(setting);
        }
        for feature in NOTHING_TO_ACT_WITH {
            args.push("--disable".to_string());
            args.push(feature.to_string());
        }
        for arg in ["--enable", "skip_host_skill_discovery", "-"] {
            args.push(arg.to_string());
        }
        Some(Apart {
            launch: Launch {
                prompt_as_file: false,
                before_stop: None,
                program: self.program.clone(),
                args,
                env: Vec::new(),
                stdin: reading.asked.clone().into_bytes(),
            },
            files: vec![
                ("schema.json".to_string(), reading.schema.clone()),
                ("instructions.txt".to_string(), reading.instructions.clone()),
            ],
            keeps: KEPT_APART.to_vec(),
        })
    }

    /// The last message of the agent among what `codex exec --json` printed.
    fn answered(&self, printed: &str) -> Result<String, String> {
        let events = printed
            .lines()
            .filter_map(|line| serde_json::from_str::<Value>(line).ok());
        let mut messages = events.filter_map(|event| {
            let item = &event["item"];
            let is_message = event["type"] == "item.completed" && item["type"] == "agent_message";
            let text = item["text"].as_str().filter(|text| !text.trim().is_empty());
            text.filter(|_| is_message).map(str::to_string)
        });
        messages
            .next_back()
            .ok_or_else(|| "codex returned no agent message".to_string())
    }
}

fn text_of(value: &Value) -> &str {
    value.as_str().unwrap_or("")
}

/// What a tool call is called in the conversation, and what it was asked, by the kind of item.
fn call_of(item: &Value) -> (String, Value) {
    let kind = text_of(&item["type"]);
    match kind {
        "command_execution" | "local_shell_call" => {
            ("shell".to_string(), json!({ "command": item["command"] }))
        }
        "file_change" | "patch_apply" => (
            "apply_patch".to_string(),
            json!({ "changes": item["changes"] }),
        ),
        "mcp_tool_call" => {
            let name = match (text_of(&item["server"]), text_of(&item["tool"])) {
                ("", "") => kind.to_string(),
                ("", tool) => tool.to_string(),
                (server, tool) => format!("{server}.{tool}"),
            };
            (name, item["arguments"].clone())
        }
        "web_search" | "web_search_call" => {
            ("web_search".to_string(), json!({ "query": item["query"] }))
        }
        other => {
            let name = match text_of(&item["name"]) {
                "" => other,
                name => name,
            };
            (name.to_string(), item["arguments"].clone())
        }
    }
}

/// The cell a conversation shows of a call: what Codex's own reader makes of the item, read
/// while all of it is there (`history::codex_cell`). One call is one row of the conversation
/// here, so its cell is known by the call, also where it only looked at things.
fn cell_of(step: &str, item: &Value, call_id: &str) -> Option<Value> {
    let mut cell = crate::history::codex_cell(step, item)?;
    cell["id"] = Value::from(call_id);
    Some(cell)
}

/// What a finished tool call gave back.
fn result_of(item: &Value) -> String {
    for field in ["aggregated_output", "output"] {
        if let Some(text) = item[field].as_str().filter(|text| !text.is_empty()) {
            return text.to_string();
        }
    }
    if let Some(message) = item["error"]["message"].as_str() {
        return message.to_string();
    }
    match &item["result"] {
        Value::Null => String::new(),
        Value::String(text) => text.clone(),
        other => other.to_string(),
    }
}

/// Whether a finished tool call went wrong: it says so, or its command did not exit with 0.
fn went_wrong(item: &Value) -> bool {
    matches!(text_of(&item["status"]), "failed" | "declined" | "error")
        || item["exit_code"].as_i64().is_some_and(|code| code != 0)
}

pub struct CodexInterpreter {
    turn: u64,
    /// The turn's own name: what the ids of its pieces are built from.
    name: String,
    /// The tool calls of this turn, by Codex's item id.
    tools: BTreeMap<String, ToolCall>,
    activity: Option<Activity>,
    /// Codex said the turn is complete.
    completed: bool,
    /// What Codex said went wrong.
    failure: Option<String>,
    stderr: ErrorTail,
    ended: bool,
    /// The calls those who watch the turn were shown, by Codex's item id.
    shown: BTreeSet<String>,
}

/// What the ids of Codex's live items begin with.
const SHOWN: &str = "cx";

impl CodexInterpreter {
    pub fn new(turn: u64, name: &str) -> Self {
        Self {
            turn,
            name: name.to_string(),
            tools: BTreeMap::new(),
            activity: None,
            completed: false,
            failure: None,
            stderr: ErrorTail::default(),
            ended: false,
            shown: BTreeSet::new(),
        }
    }

    fn moved(&mut self, activity: Activity, out: &mut Vec<Signal>) {
        if self.activity.as_ref() != Some(&activity) {
            self.activity = Some(activity.clone());
            out.push(Signal::Moved(activity));
        }
    }

    fn on_line(&mut self, line: &str, t: i64, out: &mut Vec<Signal>) {
        let Ok(event) = serde_json::from_str::<Value>(line) else {
            return; // not one of its events: a line some tool of its printed
        };
        match text_of(&event["type"]) {
            "thread.started" => {
                let thread = text_of(&event["thread_id"]).trim();
                if !thread.is_empty() {
                    out.push(Signal::Bound {
                        backend_session: thread.to_string(),
                    });
                }
            }
            "turn.started" => self.moved(Activity::Thinking, out),
            step @ ("item.started" | "item.updated" | "item.completed") => {
                self.on_item(step, &event["item"], t, out);
            }
            "turn.completed" => {
                self.completed = true;
                let count = |field: &str| event["usage"][field].as_u64();
                // Codex counts on from turn to turn of a thread: what it says here is what the
                // whole thread has taken so far (a first turn said 87,566 in, the next turn of
                // the same thread 131,412: the recordings in `tests/fixtures`).
                if let (Some(tokens_in), Some(tokens_out)) =
                    (count("input_tokens"), count("output_tokens"))
                {
                    out.push(Signal::Total {
                        tokens_in,
                        tokens_out,
                    });
                }
            }
            "turn.failed" | "thread.error" | "error" => {
                let said = match &event["error"] {
                    Value::String(message) => message.as_str(),
                    error => match text_of(&error["message"]) {
                        "" => text_of(&event["message"]),
                        message => message,
                    },
                };
                if self.failure.is_none() && !said.trim().is_empty() {
                    self.failure = Some(shorten(said.trim(), 600));
                }
            }
            _ => {}
        }
    }

    /// What those who watch the turn are shown of an item. Codex says an item when it begins
    /// and when it is whole, and nothing in between: a command is shown from when it starts,
    /// with what it printed when it ends; what the model thought and said is shown whole.
    fn show(&mut self, done: bool, item: &Value, out: &mut Vec<Signal>) {
        let raw = text_of(&item["id"]);
        if raw.is_empty() {
            return;
        }
        let id = format!("{SHOWN}:{raw}");
        let mut glimpse = |glimpse: Glimpse| out.push(Signal::Glimpse(glimpse));
        match text_of(&item["type"]) {
            "agent_message" => {
                let text = match text_of(&item["text"]) {
                    "" => text_of(&item["message"]),
                    text => text,
                };
                if text.trim().is_empty() {
                    return;
                }
                glimpse(Glimpse::Message {
                    id: id.clone(),
                    text: text.to_string(),
                    row: Some(format!("r-{}-{raw}", self.name)),
                });
                if done {
                    glimpse(Glimpse::Done { id });
                }
            }
            "reasoning" => {
                glimpse(Glimpse::Reasoning {
                    id: id.clone(),
                    text: text_of(&item["text"]).to_string(),
                });
                if done {
                    glimpse(Glimpse::Done { id });
                }
            }
            "context_compaction" => glimpse(Glimpse::Compacting {
                prefix: SHOWN.to_string(),
                over: done,
            }),
            // Its plan, whole each time: every step with whether it is done.
            "todo_list" => {
                let steps: Vec<Value> = item["items"]
                    .as_array()
                    .into_iter()
                    .flatten()
                    .map(|step| {
                        let status = match step["completed"].as_bool() {
                            Some(true) => "completed",
                            _ => "pending",
                        };
                        json!({"text": text_of(&step["text"]), "status": status})
                    })
                    .collect();
                glimpse(Glimpse::Plan {
                    prefix: SHOWN.to_string(),
                    steps: Value::Array(steps),
                });
            }
            "error" | "" => {}
            _ => {
                // A call is shown once when it is first seen, under the id its row in the
                // conversation has, and settled when it is whole.
                if self.shown.insert(raw.to_string()) {
                    let (name, input) = call_of(item);
                    glimpse(Glimpse::Tool {
                        id: id.clone(),
                        call_id: format!("c-{}-{raw}", self.name),
                        name,
                        input,
                        whole: true,
                    });
                }
                if done {
                    glimpse(Glimpse::Returned {
                        id,
                        content: Value::String(result_of(item)),
                        failed: went_wrong(item),
                        exit_code: item["exit_code"].as_i64(),
                    });
                }
            }
        }
    }

    fn on_item(&mut self, step: &str, item: &Value, t: i64, out: &mut Vec<Signal>) {
        let id = text_of(&item["id"]);
        let done = step == "item.completed";
        self.show(done, item, out);
        match text_of(&item["type"]) {
            "agent_message" => {
                let text = match text_of(&item["text"]) {
                    "" => text_of(&item["message"]),
                    text => text,
                };
                if text.trim().is_empty() {
                    return;
                }
                let reply = format!("r-{}-{id}", self.name);
                if done {
                    out.push(Signal::Reply(Reply {
                        origin: clarp_kernel::turn::Origin::User,
                        id: reply,
                        turn: self.turn,
                        text: text.to_string(),
                        trace_id: None,
                        ..Reply::default()
                    }));
                    self.moved(Activity::Thinking, out);
                } else {
                    self.moved(Activity::Responding, out);
                    out.push(Signal::Live(LiveReply {
                        id: reply,
                        text: text.to_string(),
                    }));
                }
            }
            "reasoning" => self.moved(Activity::Thinking, out),
            "context_compaction" => {
                let now = if done {
                    Activity::Thinking
                } else {
                    Activity::Compacting
                };
                self.moved(now, out);
            }
            // Codex on its own configuration, and its plan: nothing the conversation holds.
            "error" | "todo_list" | "" => {}
            _ if id.is_empty() => {}
            _ if done => {
                let (name, input) = call_of(item);
                let call = self
                    .tools
                    .entry(id.to_string())
                    .or_insert_with(|| ToolCall {
                        origin: clarp_kernel::turn::Origin::User,
                        id: format!("c-{}-{id}", self.name),
                        turn: self.turn,
                        name,
                        input,
                        status: ToolStatus::Running,
                        output: None,
                        started_at_ms: t,
                        ended_at_ms: None,
                        ..ToolCall::default()
                    });
                if call.status != ToolStatus::Running {
                    return; // told twice
                }
                call.status = if went_wrong(item) {
                    ToolStatus::Error
                } else {
                    ToolStatus::Ok
                };
                call.output = Some(shorten(&result_of(item), MAX_TOOL_OUTPUT));
                call.ended_at_ms = Some(t);
                call.cell = cell_of("item.completed", item, &call.id);
                out.push(Signal::Tool(call.clone()));
                self.moved(Activity::Thinking, out);
            }
            _ => {
                if self.tools.contains_key(id) {
                    return; // an update of a call that is known: nothing new to store
                }
                let (name, input) = call_of(item);
                let call_id = format!("c-{}-{id}", self.name);
                let call = ToolCall {
                    origin: clarp_kernel::turn::Origin::User,
                    cell: cell_of("item.started", item, &call_id),
                    id: call_id,
                    turn: self.turn,
                    name: name.clone(),
                    input,
                    status: ToolStatus::Running,
                    output: None,
                    started_at_ms: t,
                    ended_at_ms: None,
                    ..ToolCall::default()
                };
                self.tools.insert(id.to_string(), call.clone());
                out.push(Signal::Tool(call));
                self.moved(Activity::Tool { name }, out);
            }
        }
    }

    fn on_exit(&mut self, code: Option<i32>, signal: Option<i32>, out: &mut Vec<Signal>) {
        if self.ended {
            return;
        }
        self.ended = true;
        // A tool that still runs when the process goes is left as it is: what becomes of
        // it follows from how the turn ended, which is the Host's to say.
        let outcome = if let Some(signal) = signal {
            RunOutcome::Interrupted {
                reason: format!("codex was ended by signal {signal}"),
            }
        } else if let Some(reason) = self.failure.take() {
            RunOutcome::Failed { reason }
        } else if code != Some(0) {
            RunOutcome::Failed {
                reason: self
                    .stderr
                    .text()
                    .unwrap_or_else(|| format!("codex exited with code {}", code.unwrap_or(-1))),
            }
        } else if !self.completed {
            RunOutcome::Failed {
                reason: self
                    .stderr
                    .text()
                    .unwrap_or_else(|| "codex ended without completing its turn".to_string()),
            }
        } else {
            RunOutcome::Completed
        };
        out.push(Signal::Ended(outcome));
    }
}

impl Interpreter for CodexInterpreter {
    fn on_record(&mut self, record: &Record, out: &mut Vec<Signal>) {
        match &record.entry {
            Entry::Out(line) => self.on_line(line, record.t, out),
            Entry::Err(line) => self.stderr.push(line),
            Entry::Shim(ShimEvent::Exited { code, signal }) => {
                self.on_exit(*code, *signal, out);
            }
            Entry::Shim(_) | Entry::Hook { .. } => {}
        }
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use std::path::PathBuf;

    use super::*;

    fn spec() -> TurnSpec {
        TurnSpec {
            agent_id: "a1".to_string(),
            session: "rachel".to_string(),
            turn: 3,
            cwd: PathBuf::from("/work"),
            prompt: "What is in here?".to_string(),
            backend_session: None,
            new_session: Some("conv-1".to_string()),
            model: None,
            effort: None,
            identity: None,
            told: None,
        }
    }

    #[test]
    fn a_turn_is_started_with_the_message_on_standard_input() {
        let codex = Codex::new();
        let fresh = codex.launch(&spec());
        assert_eq!(fresh.program, "codex");
        assert_eq!(
            fresh.args,
            vec![
                "exec",
                "--json",
                "--dangerously-bypass-approvals-and-sandbox",
                "-"
            ],
            "a new thread: Codex names it, the Host cannot"
        );
        assert_eq!(fresh.stdin, b"What is in here?");
        assert_eq!(
            fresh.env,
            vec![(SESSION_ENV.to_string(), "rachel".to_string())]
        );

        let told = codex.launch(&TurnSpec {
            backend_session: Some("thread-9".to_string()),
            model: Some("gpt-9".to_string()),
            effort: Some("low".to_string()),
            identity: Some("You are \"Rachel\".\nBe brief.".to_string()),
            ..spec()
        });
        assert_eq!(
            told.args,
            vec![
                "exec",
                "--json",
                "--dangerously-bypass-approvals-and-sandbox",
                "--model",
                "gpt-9",
                "-c",
                r#"model_reasoning_effort="low""#,
                "-c",
                r#"developer_instructions="You are \"Rachel\".\nBe brief.""#,
                "resume",
                "thread-9",
                "-"
            ],
            "the settings come before the thread to resume, the message last"
        );
        assert_eq!(
            Codex::with_program("/opt/codex").launch(&spec()).program,
            "/opt/codex"
        );

        // What the Host says with the prompt comes before the user's words, and can be
        // taken off again.
        let headed = codex.launch(&TurnSpec {
            told: Some(told::Turn {
                persona: "Rachel".to_string(),
                spoken: Some(told::Sound::default()),
                call: Some(
                    "[Group call] You are Rachel, on a voice call with the user.".to_string(),
                ),
            }),
            ..spec()
        });
        let prompt = String::from_utf8(headed.stdin).unwrap();
        assert!(prompt.starts_with("[voice-mode] You are running inside Clarp."));
        assert!(prompt.contains("This turn belongs to Rachel (Clarp session rachel)."));
        assert!(prompt.contains("<speak>") && prompt.contains("Humanness: 5/10."));
        // The header of a call the turn is a part of comes after everything else the Host
        // says, before the user's words.
        assert!(prompt.ends_with(
            "\n\n[Group call] You are Rachel, on a voice call with the user.\n\n--- user message ---\nWhat is in here?"
        ));
        assert_eq!(told::unheaded(&prompt), "What is in here?");
        assert_eq!(
            headed.args, fresh.args,
            "nothing of it is on the command line"
        );
    }

    /// What a turn means for the conversation: everything but what is only shown.
    fn read(lines: &[&str], exit: (Option<i32>, Option<i32>)) -> Vec<Signal> {
        let mut signals = read_all(lines, exit);
        signals.retain(|signal| !matches!(signal, Signal::Glimpse(_)));
        signals
    }

    /// A turn as those who watch it hold it at its end: every glimpse given to a hub, and
    /// every event the hub sent applied by the reducer a client runs.
    fn watched(lines: &[&str]) -> clarp_kernel::live::View {
        use clarp_kernel::live::{Hub, View};

        let mut hub = Hub::new("epoch-1", "agent-1", "rachel");
        let mut client = View::new();
        client.apply_snapshot(&json!({"epoch": "epoch-1", "lseq": 0}));
        let mut sent = hub.begin_turn("rachel", "conv-1", "tr-1", None, 100);
        for (at, signal) in read_all(lines, (Some(0), None)).into_iter().enumerate() {
            if let Signal::Glimpse(glimpse) = signal {
                sent.extend(hub.show(&glimpse, Some("tr-1"), 101 + at as i64));
            }
        }
        sent.extend(hub.end_turn("completed", None, 10_000));
        for event in &sent {
            assert!(
                client.apply_event(event).is_empty(),
                "a client takes {event}"
            );
        }
        client
    }

    #[test]
    fn a_turn_is_shown_as_items_from_what_codex_says_of_each() {
        let view = watched(&[
            r#"{"type":"thread.started","thread_id":"t-1"}"#,
            r#"{"type":"item.completed","item":{"id":"item_0","type":"reasoning","text":"**Looking at the tests**\nThey are few."}}"#,
            r#"{"type":"item.started","item":{"id":"item_1","type":"todo_list","items":[{"text":"run the tests","completed":false},{"text":"answer","completed":false}]}}"#,
            r#"{"type":"item.started","item":{"id":"item_2","type":"command_execution","command":"bash -lc 'cargo test'","aggregated_output":"","exit_code":null,"status":"in_progress"}}"#,
            r#"{"type":"item.completed","item":{"id":"item_2","type":"command_execution","command":"bash -lc 'cargo test'","aggregated_output":"running 2 tests\ntest a ... ok\ntest b ... FAILED\n","exit_code":101,"status":"failed"}}"#,
            r#"{"type":"item.completed","item":{"id":"item_3","type":"file_change","changes":[{"path":"src/b.rs","kind":"update"}],"status":"completed"}}"#,
            r#"{"type":"item.updated","item":{"id":"item_1","type":"todo_list","items":[{"text":"run the tests","completed":true},{"text":"answer","completed":false}]}}"#,
            r#"{"type":"item.completed","item":{"id":"item_4","type":"agent_message","text":"One test fails; I changed b.rs."}}"#,
            r#"{"type":"turn.completed","usage":{"input_tokens":1,"output_tokens":1}}"#,
        ]);
        let items = view.items();
        let shown: Vec<(&str, &str, &str)> = items
            .iter()
            .map(|item| {
                let text = |name: &str| item[name].as_str().unwrap();
                (text("id"), text("kind"), text("status"))
            })
            .collect();
        assert_eq!(
            shown,
            [
                ("cx:item_0", "reasoning", "completed"),
                ("cx:tr-1:plan", "plan", "completed"),
                ("cx:item_2", "tool", "failed"),
                ("cx:item_3", "tool", "completed"),
                ("cx:item_4", "message", "completed"),
            ]
        );
        assert_eq!(items[0]["title"], "Looking at the tests");
        assert_eq!(
            items[1]["plan"]["steps"],
            json!([{"text": "run the tests", "status": "completed"}, {"text": "answer", "status": "pending"}]),
            "the plan is replaced whole"
        );
        let command = &items[2]["tool"];
        assert_eq!(
            (
                &command["name"],
                &command["category"],
                &command["label"],
                &command["call_id"]
            ),
            (
                &json!("shell"),
                &json!("exec"),
                &json!("cargo test"),
                &json!("c-m1-item_2")
            ),
            "a command is shown by what it runs, under the id its row in the conversation has"
        );
        assert_eq!(
            command["output"],
            json!({"tail": ["running 2 tests", "test a ... ok", "test b ... FAILED"], "total_lines": 3,
                   "truncated": false, "exit_code": 101}),
            "with what it printed and the code it ended with"
        );
        assert_eq!(items[3]["tool"]["category"], "edit");
        assert_eq!(
            (&items[4]["text"], &items[4]["phase"], &items[4]["row_id"]),
            (
                &json!("One test fails; I changed b.rs."),
                &json!("final"),
                &json!("r-m1-item_4")
            )
        );
        assert_eq!(view.turn().unwrap()["tool_count"], 2);
        assert_eq!(view.activity()["state"], "idle");
    }

    fn read_all(lines: &[&str], exit: (Option<i32>, Option<i32>)) -> Vec<Signal> {
        let mut interpreter = CodexInterpreter::new(3, "m1");
        let mut out = Vec::new();
        let mut t = 100;
        for line in lines {
            t += 1;
            interpreter.on_record(
                &Record {
                    t,
                    entry: Entry::Out((*line).to_string()),
                },
                &mut out,
            );
        }
        interpreter.on_record(
            &Record {
                t: t + 1,
                entry: Entry::Shim(ShimEvent::Exited {
                    code: exit.0,
                    signal: exit.1,
                }),
            },
            &mut out,
        );
        out
    }

    #[test]
    fn a_reply_that_grows_is_shown_and_then_stored_under_the_same_id() {
        let signals = read(
            &[
                r#"{"type":"thread.started","thread_id":"t-1"}"#,
                r#"{"type":"turn.started"}"#,
                r#"{"type":"item.started","item":{"id":"item_0","type":"reasoning","text":"hm"}}"#,
                r#"{"type":"item.updated","item":{"id":"item_1","type":"agent_message","text":"One"}}"#,
                r#"{"type":"item.updated","item":{"id":"item_1","type":"agent_message","text":"One file."}}"#,
                r#"{"type":"item.completed","item":{"id":"item_1","type":"agent_message","text":"One file."}}"#,
                r#"{"type":"turn.completed","usage":{"input_tokens":12,"output_tokens":3}}"#,
            ],
            (Some(0), None),
        );
        assert_eq!(
            signals,
            vec![
                Signal::Bound {
                    backend_session: "t-1".to_string()
                },
                Signal::Moved(Activity::Thinking),
                Signal::Moved(Activity::Responding),
                Signal::Live(LiveReply {
                    id: "r-m1-item_1".to_string(),
                    text: "One".to_string()
                }),
                Signal::Live(LiveReply {
                    id: "r-m1-item_1".to_string(),
                    text: "One file.".to_string()
                }),
                Signal::Reply(Reply {
                    origin: clarp_kernel::turn::Origin::User,
                    id: "r-m1-item_1".to_string(),
                    turn: 3,
                    text: "One file.".to_string(),
                    trace_id: None,
                    ..Reply::default()
                }),
                Signal::Moved(Activity::Thinking),
                Signal::Total {
                    tokens_in: 12,
                    tokens_out: 3
                },
                Signal::Ended(RunOutcome::Completed),
            ]
        );
    }

    #[test]
    fn the_kinds_of_tool_calls_are_named_and_a_failed_one_is_an_error() {
        let signals = read(
            &[
                r#"{"type":"item.started","item":{"id":"item_0","type":"command_execution","command":"false","status":"in_progress"}}"#,
                r#"{"type":"item.completed","item":{"id":"item_0","type":"command_execution","command":"false","aggregated_output":"","exit_code":1,"status":"failed"}}"#,
                r#"{"type":"item.completed","item":{"id":"item_1","type":"file_change","changes":[{"path":"a.txt","kind":"add"}],"status":"completed"}}"#,
                r#"{"type":"item.started","item":{"id":"item_2","type":"mcp_tool_call","server":"docs","tool":"search","arguments":{"q":"x"},"status":"in_progress"}}"#,
                r#"{"type":"item.completed","item":{"id":"item_2","type":"mcp_tool_call","server":"docs","tool":"search","arguments":{"q":"x"},"result":{"hits":2},"status":"completed"}}"#,
                r#"{"type":"item.completed","item":{"id":"item_3","type":"web_search","query":"clarp"}}"#,
                r#"{"type":"turn.completed","usage":{"input_tokens":1,"output_tokens":1}}"#,
            ],
            (Some(0), None),
        );
        let calls: Vec<(&str, &str, ToolStatus, Option<&str>)> = signals
            .iter()
            .filter_map(|signal| match signal {
                Signal::Tool(call) => Some((
                    call.id.as_str(),
                    call.name.as_str(),
                    call.status,
                    call.output.as_deref(),
                )),
                _ => None,
            })
            .collect();
        assert_eq!(
            calls,
            vec![
                ("c-m1-item_0", "shell", ToolStatus::Running, None),
                ("c-m1-item_0", "shell", ToolStatus::Error, Some("")),
                ("c-m1-item_1", "apply_patch", ToolStatus::Ok, Some("")),
                ("c-m1-item_2", "docs.search", ToolStatus::Running, None),
                (
                    "c-m1-item_2",
                    "docs.search",
                    ToolStatus::Ok,
                    Some(r#"{"hits":2}"#)
                ),
                ("c-m1-item_3", "web_search", ToolStatus::Ok, Some("")),
            ]
        );
        let moves: Vec<&Activity> = signals
            .iter()
            .filter_map(|signal| match signal {
                Signal::Moved(activity) => Some(activity),
                _ => None,
            })
            .collect();
        assert_eq!(
            moves,
            vec![
                &Activity::Tool {
                    name: "shell".to_string()
                },
                &Activity::Thinking,
                &Activity::Tool {
                    name: "docs.search".to_string()
                },
                &Activity::Thinking,
            ],
            "a call that is only told when it is over was never the agent's activity"
        );
    }

    #[test]
    fn the_ways_a_turn_fails_are_told_apart() {
        let outcome = |lines: &[&str], exit, stderr: &[&str]| {
            let mut interpreter = CodexInterpreter::new(1, "m1");
            let mut out = Vec::new();
            for line in stderr {
                interpreter.on_record(
                    &Record {
                        t: 1,
                        entry: Entry::Err((*line).to_string()),
                    },
                    &mut out,
                );
            }
            for line in lines {
                interpreter.on_record(
                    &Record {
                        t: 2,
                        entry: Entry::Out((*line).to_string()),
                    },
                    &mut out,
                );
            }
            let (code, signal) = exit;
            let gone = Entry::Shim(ShimEvent::Exited { code, signal });
            interpreter.on_record(&Record { t: 3, entry: gone }, &mut out);
            out.pop().unwrap()
        };
        let failed = |reason: &str| {
            Signal::Ended(RunOutcome::Failed {
                reason: reason.to_string(),
            })
        };
        assert_eq!(
            outcome(
                &[
                    r#"{"type":"error","message":"The model is not supported."}"#,
                    r#"{"type":"turn.failed","error":{"message":"The model is not supported."}}"#
                ],
                (Some(1), None),
                &[]
            ),
            failed("The model is not supported."),
            "what Codex said went wrong"
        );
        assert_eq!(
            outcome(
                &[],
                (Some(1), None),
                &["Error: no rollout found for thread id t-9"]
            ),
            failed("Error: no rollout found for thread id t-9"),
            "it said nothing on its output: what it wrote to its error stream"
        );
        assert_eq!(
            outcome(&[], (Some(2), None), &[]),
            failed("codex exited with code 2")
        );
        assert_eq!(
            outcome(&[r#"{"type":"turn.started"}"#], (Some(0), None), &[]),
            failed("codex ended without completing its turn"),
            "an exit with 0 is not enough"
        );
        assert_eq!(
            outcome(&[r#"{"type":"turn.started"}"#], (None, Some(15)), &[]),
            Signal::Ended(RunOutcome::Interrupted {
                reason: "codex was ended by signal 15".to_string()
            })
        );
        // An item of type error is Codex remarking on its configuration, not a failed turn.
        assert_eq!(
            outcome(
                &[
                    r#"{"type":"item.completed","item":{"id":"item_0","type":"error","message":"Model metadata not found."}}"#,
                    r#"{"type":"turn.completed","usage":{"input_tokens":1,"output_tokens":1}}"#
                ],
                (Some(0), None),
                &[]
            ),
            Signal::Ended(RunOutcome::Completed)
        );
    }

    #[test]
    fn the_same_lines_mean_the_same_however_often_they_are_read() {
        let lines = [
            r#"{"type":"thread.started","thread_id":"t-1"}"#,
            r#"{"type":"item.started","item":{"id":"item_0","type":"command_execution","command":"ls","status":"in_progress"}}"#,
            r#"{"type":"item.updated","item":{"id":"item_0","type":"command_execution","command":"ls","status":"in_progress"}}"#,
            r#"{"type":"item.completed","item":{"id":"item_0","type":"command_execution","command":"ls","aggregated_output":"a\n","exit_code":0,"status":"completed"}}"#,
            r#"{"type":"item.completed","item":{"id":"item_0","type":"command_execution","command":"ls","aggregated_output":"a\n","exit_code":0,"status":"completed"}}"#,
            "not json at all",
            r#"{"type":"turn.completed","usage":{"input_tokens":1,"output_tokens":1}}"#,
        ];
        let once = read(&lines, (Some(0), None));
        assert_eq!(once, read(&lines, (Some(0), None)));
        let tools = once
            .iter()
            .filter(|signal| matches!(signal, Signal::Tool(_)))
            .count();
        assert_eq!(tools, 2, "started and ended, each once");
    }

    #[test]
    fn codex_is_started_to_read_with_nothing_to_act_with() {
        let codex = Codex::with_program("/opt/codex");
        let reading = Reading {
            instructions: "Write plainly.".to_string(),
            asked: "{\"requests\": []}".to_string(),
            schema: "{}".to_string(),
            model: Some("gpt-6-sol".to_string()),
            effort: Some("low".to_string()),
        };
        let apart = codex.reads(&reading).unwrap();
        let args: Vec<&str> = apart.launch.args.iter().map(String::as_str).collect();
        assert_eq!(
            &args[..14],
            [
                "exec",
                "--ignore-user-config",
                "--ignore-rules",
                "--ephemeral",
                "--skip-git-repo-check",
                "--sandbox",
                "read-only",
                "--json",
                "--color",
                "never",
                "--output-schema",
                "{dir}/schema.json",
                "--model",
                "gpt-6-sol",
            ]
        );
        let after = |flag: &str| -> Vec<&str> {
            let pairs = args.windows(2).filter(|pair| pair[0] == flag);
            pairs.map(|pair| pair[1]).collect()
        };
        assert_eq!(
            after("-c"),
            [
                "approval_policy=\"never\"",
                "web_search=\"disabled\"",
                "project_doc_max_bytes=0",
                "mcp_servers={}",
                "model_instructions_file=\"{dir}/instructions.txt\"",
                "model_reasoning_effort=\"low\"",
            ]
        );
        let without = after("--disable");
        assert_eq!(without.len(), 18);
        assert!(without.contains(&"shell_tool") && without.contains(&"unified_exec"));
        assert_eq!(
            &args[args.len() - 3..],
            ["--enable", "skip_host_skill_discovery", "-"]
        );
        // What it reads comes on standard input, and its files lie beside it.
        assert_eq!(apart.launch.stdin, reading.asked.as_bytes());
        let files: Vec<&str> = apart.files.iter().map(|(name, _)| name.as_str()).collect();
        assert_eq!(files, ["schema.json", "instructions.txt"]);
        // It is left with where it is and who it is, and with nothing of the Host's own.
        assert!(apart.keeps.contains(&"HOME") && apart.keeps.contains(&"CODEX_HOME"));
        assert!(apart.keeps.iter().all(|name| !name.starts_with("CLARP")));
        // Without a model and an effort it is Codex's own choice of both.
        let plain = Reading::default();
        let args = codex.reads(&plain).unwrap().launch.args;
        assert!(!args.contains(&"--model".to_string()));
        assert!(
            !args
                .iter()
                .any(|arg| arg.starts_with("model_reasoning_effort"))
        );
    }

    #[test]
    fn one_question_is_asked_of_codex_outside_every_conversation() {
        let codex = Codex::with_program("/opt/codex");
        let question = Question {
            prompt: "Whom is this for?".to_string(),
            model: Some("gpt-6-sol".to_string()),
            effort: Some("low".to_string()),
        };
        let launch = codex.once(&question).unwrap();
        assert_eq!(launch.program, "/opt/codex");
        assert_eq!(
            launch.args,
            [
                "exec",
                "--json",
                "--sandbox",
                "workspace-write",
                "--ephemeral",
                "--skip-git-repo-check",
                "--model",
                "gpt-6-sol",
                "-c",
                "model_reasoning_effort=\"low\"",
                "-"
            ]
        );
        // The question is read, not passed: no command line shows it.
        assert_eq!(launch.stdin, b"Whom is this for?");
        assert!(launch.env.is_empty());
        // Without a model and an effort it is Codex's own choice of both.
        let plain = Question {
            prompt: "x".to_string(),
            ..Question::default()
        };
        assert_eq!(codex.once(&plain).unwrap().args.len(), 7);

        // The answer is the agent's last message. What is no message is passed over.
        let printed = [
            r#"{"type":"thread.started","thread_id":"t1"}"#,
            "not json",
            r#"{"type":"item.completed","item":{"type":"reasoning","text":"thinking"}}"#,
            r#"{"type":"item.completed","item":{"type":"agent_message","text":"first"}}"#,
            r#"{"type":"item.completed","item":{"type":"agent_message","text":"  "}}"#,
            r#"{"type":"item.started","item":{"type":"agent_message","text":"not yet"}}"#,
            r#"{"type":"item.completed","item":{"type":"agent_message","text":"{\"action\":\"route\"}"}}"#,
            r#"{"type":"turn.completed"}"#,
        ]
        .join("\n");
        assert_eq!(
            codex.answered(&printed),
            Ok("{\"action\":\"route\"}".to_string())
        );
        assert_eq!(
            codex.answered("nothing of the kind"),
            Err("codex returned no agent message".to_string())
        );
    }
}
