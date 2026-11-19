//! Antigravity, run for one turn as `agy --output-format stream-json --print=<message>`.
//!
//! One process per turn. The message is the value of `--print`, bound to it with `=`: the
//! flag takes the next word as its value, whatever that word is. A turn that continues a
//! conversation names it (`--conversation <id>`); Antigravity names a new one itself, in its
//! first line, and the Host learns it there and continues it from then on.
//!
//! The stream, as agy 1.1.21 and 1.2.16 print it (the fixtures in `tests/fixtures` are the
//! Python Host's own recordings):
//!
//! ```text
//! {"event":"init","conversation_id":"…","init":{"model":"…","cwd":"…","tools":[…]}}
//! {"event":"step_update","step_update":{"step_index":1,"state":"ACTIVE","step_type":"agent_response","text_delta":"…"}}
//! {"event":"step_update","step_update":{"step_index":1,"state":"DONE","step_type":"agent_response","usage":{…}}}
//! {"event":"step_update","step_update":{"step_index":2,"state":"ACTIVE","step_type":"tool","tool_name":"run_command","tool_info":{"name":"…","parameters":{…}}}}
//! {"event":"step_update","step_update":{"step_index":2,"state":"DONE","step_type":"tool","tool_info":{"name":"…","parameters":{…},"output":"…"}}}
//! {"event":"result","result":{"conversation_id":"…","status":"SUCCESS","response":"…","usage":{…}}}
//! ```
//!
//! A step is told when it begins and when it is done; what the agent says in a step comes
//! in pieces (`text_delta`), the last of them with the step's end. Each step in which the
//! agent says something is a message of its own in the conversation, as with every tool. A
//! tool is named as the conversation names tools everywhere (`run_command` is `Bash`,
//! `view_file` is `Read`), with what it was given under the names those tools have: so the
//! Python Host tells them (`backend/agy.py`, `_canonical_tool_input`).

use std::collections::{BTreeMap, BTreeSet};

use clarp_kernel::conversation::{Reply, ToolCall, ToolStatus};
use clarp_kernel::live::Glimpse;
use clarp_kernel::told;
use clarp_kernel::turn::{Activity, RunOutcome};
use clarp_spool::{Entry, Record, ShimEvent};
use serde_json::{Map, Value, json};

use crate::{
    Apart, Backend, ErrorTail, Interpreter, Launch, LiveReply, OwnHome, Question, Reading,
    SESSION_ENV, Signal, TurnSpec, shorten,
};

/// How much of what a tool gave back is kept with the call.
const MAX_TOOL_OUTPUT: usize = 4_000;
/// Antigravity ends a turn by itself after five minutes unless it is told otherwise, and
/// the turn then looks as if the agent fell silent. The Host has no clock of its own on a
/// turn: the tool's is put out of the way.
const PRINT_TIMEOUT: &str = "24h";
/// What tells the tool's own hooks that this turn is the Host's, and not one somebody ran
/// in a terminal.
const MANAGED: &str = "CLARP_AGY_MANAGED_TURN";
/// What the ids of Antigravity's live items begin with.
const SHOWN: &str = "ag";
/// The tools of Antigravity under the names the conversation knows tools by: the ten the
/// Python Host knows (`_TOOL_NAMES`), and the three names agy gives its file tools today
/// (`read_file`, `write_file`, `edit_file`, with the file under `Path`), which that Host
/// shows as tools nobody knows.
const TOOL_NAMES: [(&str, &str); 13] = [
    ("run_command", "Bash"),
    ("view_file", "Read"),
    ("write_to_file", "Write"),
    ("replace_file_content", "Edit"),
    ("multi_replace_file_content", "Edit"),
    ("grep_search", "Grep"),
    ("find_by_name", "Glob"),
    ("list_dir", "LS"),
    ("search_web", "WebSearch"),
    ("read_url_content", "WebFetch"),
    ("read_file", "Read"),
    ("write_file", "Write"),
    ("edit_file", "Edit"),
];

/// Antigravity keeps every conversation under its user's home and lets go of all but the
/// newest five hundred or so. What a Host asks it once (a janitor's question, the router's)
/// would push its agents' conversations out within hours, and a conversation that is gone
/// is silently begun anew. Such a question is therefore asked in a home of its own that has
/// Antigravity's login, linked, and its settings, copied, and nothing else: as the Python
/// Host does (`backend/agy.py`, `isolated_home`).
const OWN_HOME: OwnHome = OwnHome {
    linked: &[
        ".gemini/oauth_creds.json",
        ".gemini/google_accounts.json",
        ".gemini/installation_id",
        ".gemini/antigravity-cli/antigravity-oauth-token",
        ".gemini/antigravity-cli/installation_id",
    ],
    copied: &[
        ".gemini/antigravity-cli/settings.json",
        ".gemini/antigravity-cli/cache/onboarding.json",
    ],
};
/// How long Antigravity gives itself for a question it only reads for.
const READS_WITHIN: &str = "120s";
/// Where Antigravity keeps what is its own, under its user's home.
pub const OWN: &str = ".gemini/antigravity-cli";
/// What of the Host's environment Antigravity is left with when it only reads: where it
/// is, who it runs as, and how it reaches its provider. Its home is the one made for it.
const KEPT_APART: [&str; 16] = [
    "PATH",
    "USER",
    "LOGNAME",
    "LANG",
    "LC_ALL",
    "XDG_RUNTIME_DIR",
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

pub struct Agy {
    program: String,
}

impl Agy {
    pub fn new() -> Self {
        Self::with_program("agy")
    }

    /// Antigravity under another name or path.
    pub fn with_program(program: impl Into<String>) -> Self {
        Self {
            program: program.into(),
        }
    }
}

impl Default for Agy {
    fn default() -> Self {
        Self::new()
    }
}

impl Backend for Agy {
    fn id(&self) -> &'static str {
        "agy"
    }

    /// As the Python Host declares them. (agy 1.5.10 takes `xhigh` and `max` too, by its own
    /// help; a model's name carries its effort anyway.)
    fn efforts(&self) -> &'static [&'static str] {
        &["low", "medium", "high"]
    }

    fn launch(&self, spec: &TurnSpec) -> Launch {
        let mut args: Vec<String> = [
            "--dangerously-skip-permissions",
            "--output-format",
            "stream-json",
            "--print-timeout",
            PRINT_TIMEOUT,
        ]
        .iter()
        .map(|arg| (*arg).to_string())
        .collect();
        let given = |value: &Option<String>| value.clone().filter(|value| !value.trim().is_empty());
        // A model's name carries its effort (`gemini-3.7-flash-low`): an effort is said only
        // where no model is named.
        match (given(&spec.model), given(&spec.effort)) {
            (Some(model), _) => {
                args.push("--model".to_string());
                args.push(model);
            }
            (None, Some(effort)) => {
                args.push("--effort".to_string());
                args.push(effort.trim().to_lowercase());
            }
            (None, None) => {}
        }
        // A conversation it has: continue it. Antigravity names a new one itself.
        if let Some(conversation) = given(&spec.backend_session) {
            args.push("--conversation".to_string());
            args.push(conversation);
        }
        // What the Host says with the prompt comes before it, as for Codex: the Python Host
        // gives both the same words.
        let prompt = match &spec.told {
            Some(told) => {
                let head = told::at_head(&told.persona, &spec.session, told.spoken);
                let head = told::in_call(head, told.call.as_deref());
                told::headed(&head, None, &spec.prompt)
            }
            None => spec.prompt.clone(),
        };
        // Bound to its flag: a message that begins with a dash is no flag.
        args.push(format!("--print={prompt}"));
        Launch {
            prompt_as_file: false,
            before_stop: None,
            program: self.program.clone(),
            args,
            env: vec![
                (SESSION_ENV.to_string(), spec.session.clone()),
                (MANAGED.to_string(), "1".to_string()),
            ],
            stdin: Vec::new(),
        }
    }

    fn interpreter(&self, turn: u64, name: &str) -> Box<dyn Interpreter> {
        Box::new(AgyInterpreter::new(turn, name))
    }

    fn own_home(&self) -> Option<OwnHome> {
        Some(OWN_HOME)
    }

    /// Antigravity keeps the newest few hundred of its conversations, each in a file of its
    /// name. Only that file counts: what it lets go of leaves other things behind. Where it
    /// has no store at all on this machine, nothing is known, and the conversation counts
    /// as kept (the Python Host's `agy_transcript.conversation_exists`).
    fn keeps(&self, session: &str) -> bool {
        let home = std::env::var_os("HOME").unwrap_or_default();
        crate::past::agy_kept(&std::path::Path::new(&home).join(OWN), session)
    }

    /// Antigravity takes its message as the value of a flag and in no other way: 60 KiB, as
    /// the Python Host allows it (`ARGV_TEXT_LIMIT`).
    fn message_most(&self) -> Option<usize> {
        Some(60 * 1024)
    }

    /// One `agy --print` whose answer is printed as one JSON object with how it ended
    /// ([`Self::answered`]): as the Python Host asks Antigravity one question
    /// (`backend/agy.py`, `routing_cmd`), which reads the answer as plain text.
    fn once(&self, question: &Question) -> Option<Launch> {
        let mut args: Vec<String> = ["--dangerously-skip-permissions", "--output-format", "json"]
            .iter()
            .map(|arg| (*arg).to_string())
            .collect();
        let given = |value: &Option<String>| value.clone().filter(|value| !value.trim().is_empty());
        match (given(&question.model), given(&question.effort)) {
            (Some(model), _) => {
                args.push("--model".to_string());
                args.push(model);
            }
            (None, Some(effort)) => {
                args.push("--effort".to_string());
                args.push(effort.trim().to_lowercase());
            }
            (None, None) => {}
        }
        args.push(format!("--print={}", question.prompt));
        Some(Launch {
            prompt_as_file: false,
            before_stop: None,
            program: self.program.clone(),
            args,
            env: Vec::new(),
            stdin: Vec::new(),
        })
    }

    /// One `agy --print` in its sandbox, with its commands and skills off, for no longer
    /// than two minutes, and held to the shape its answer has to have: as the Python Host
    /// runs a janitor's question apart (`isolated_run`), which says the shape in words.
    fn reads(&self, reading: &Reading) -> Option<Apart> {
        let mut args: Vec<String> = [
            "--sandbox",
            "--disable-slash-commands",
            "--print-timeout",
            READS_WITHIN,
            "--output-format",
            "json",
            "--json-schema",
            "{dir}/schema.json",
        ]
        .iter()
        .map(|arg| (*arg).to_string())
        .collect();
        if let Some(model) = reading
            .model
            .clone()
            .filter(|model| !model.trim().is_empty())
        {
            args.push("--model".to_string());
            args.push(model);
        }
        args.push(format!(
            "--print={}\n\n{}",
            reading.instructions, reading.asked
        ));
        Some(Apart {
            launch: Launch {
                prompt_as_file: false,
                before_stop: None,
                program: self.program.clone(),
                args,
                env: Vec::new(),
                stdin: Vec::new(),
            },
            files: vec![("schema.json".to_string(), reading.schema.clone())],
            keeps: KEPT_APART.to_vec(),
        })
    }

    /// The answer in what Antigravity printed for one question: one JSON object, with how
    /// the question ended and what was answered.
    fn answered(&self, printed: &str) -> Result<String, String> {
        let from = printed.find('{');
        let read = from.and_then(|from| serde_json::from_str::<Value>(printed[from..].trim()).ok());
        let Some(said) = read else {
            let tail: String = printed.trim().chars().rev().take(300).collect();
            let tail: String = tail.chars().rev().collect();
            return Err(format!("agy returned no JSON: {tail}"));
        };
        match &said["status"] {
            Value::Null => {}
            Value::String(status) if status == "SUCCESS" => {}
            status => {
                let why = text_of(&said["error"]);
                return Err(shorten(&format!("agy status {status}: {why}"), 300));
            }
        }
        match text_of(&said["response"]).trim() {
            "" => Err("agy returned no answer".to_string()),
            answer => Ok(answer.to_string()),
        }
    }
}

fn text_of(value: &Value) -> &str {
    value.as_str().unwrap_or("")
}

/// What a tool is called in the conversation.
pub(crate) fn tool_name(raw: &str) -> String {
    let known = TOOL_NAMES.iter().find(|(theirs, _)| *theirs == raw);
    match known {
        Some((_, ours)) => (*ours).to_string(),
        None if raw.is_empty() => "tool".to_string(),
        None => raw.to_string(),
    }
}

/// What a tool was given, under the names the conversation's tools have. Antigravity
/// spells its own otherwise (`CommandLine`, `AbsolutePath`, `TargetFile`, `Query`).
fn tool_input(name: &str, given: &Value) -> Value {
    let none = Map::new();
    let given = given.as_object().unwrap_or(&none);
    let first = |keys: &[&str]| {
        let said = keys.iter().filter_map(|key| given.get(*key));
        let mut said = said.filter(|value| match value {
            Value::Null | Value::Bool(false) => false,
            Value::String(text) => !text.is_empty(),
            _ => true,
        });
        match said.next() {
            Some(Value::String(text)) => text.clone(),
            Some(other) => other.to_string(),
            None => String::new(),
        }
    };
    match name {
        "Bash" => json!({
            "command": first(&["command", "CommandLine"]),
            "description": first(&["description"]),
        }),
        "Read" | "Write" | "Edit" => json!({
            "file_path": first(&[
                "file_path",
                "FilePath",
                "AbsolutePath",
                "TargetFile",
                "path",
                "Path",
            ]),
        }),
        "Grep" | "Glob" => json!({
            "pattern": first(&["pattern", "Query"]),
            "path": first(&["path", "SearchPath"]),
        }),
        "LS" => json!({ "path": first(&["path", "DirectoryPath"]) }),
        "WebSearch" | "WebFetch" => json!({
            "query": first(&["query", "Query"]),
            "url": first(&["url", "Url"]),
        }),
        _ => json!({}),
    }
}

/// What a tool gave back, without what Antigravity writes around it (agy 1.5.10: when the
/// command was made and done, how it ended, and after `Output:` what it printed), and the
/// code the command ended with where that is said. As the Python Host reads it in
/// Antigravity's own record of a conversation (`agy_transcript._settle`).
fn came_back(output: &str) -> (String, Option<i64>) {
    let mut body = output;
    while let Some(rest) = body
        .strip_prefix("Created At:")
        .or_else(|| body.strip_prefix("Completed At:"))
    {
        match rest.split_once('\n') {
            Some((_, after)) => body = after,
            None => break,
        }
    }
    body = body.strip_prefix('\n').unwrap_or(body);
    let head: String = body.chars().take(300).collect();
    let code = head.split_once("exited with code ").and_then(|(_, after)| {
        let signs = after.strip_prefix('-').unwrap_or(after);
        let digits = signs.chars().take_while(char::is_ascii_digit).count();
        after[..after.len() - signs.len() + digits]
            .parse::<i64>()
            .ok()
    });
    // What it printed comes after a line that says so: `Command output:` in what agy 1.5.10
    // writes of a step, `Output:` in its own record of a conversation.
    let marks = ["Command output:\n", "Output:\n"];
    let found = marks.iter().filter_map(|mark| {
        let at = body.find(mark)?;
        Some((at, at + mark.len()))
    });
    if let Some((_, after)) = found.min() {
        body = &body[after..];
    }
    (clarp_kernel::python::strip(body).to_string(), code)
}

/// The figure that says only how much in all: no usage of a step's own.
const TOTAL: &str = "total_tokens";

/// The figures of a usage Antigravity tells, under the names the Python Host gives them, as
/// its `int()` reads each: a whole number, a fraction cut to one, or a text of one; what is
/// none of these, or a truth, is left out.
fn figures(usage: &Value) -> Vec<(&'static str, i128)> {
    const NAMES: [(&str, &str); 5] = [
        ("input_tokens", "input_tokens"),
        ("output_tokens", "output_tokens"),
        ("thinking_tokens", "thinking_tokens"),
        ("cache_read_tokens", "cache_read_input_tokens"),
        ("total_tokens", TOTAL),
    ];
    let mut read = Vec::new();
    for (written, named) in NAMES {
        let figure = match &usage[written] {
            Value::Number(number) => number
                .as_i64()
                .map(i128::from)
                .or_else(|| number.as_u64().map(i128::from))
                .or_else(|| number.as_f64().filter(|value| value.is_finite()).map(cut)),
            Value::String(text) => clarp_kernel::python::int(text),
            _ => None,
        };
        if let Some(figure) = figure {
            read.push((named, figure));
        }
    }
    read
}

/// A fraction cut to the whole number below it, toward nothing, as `int()` cuts one.
#[allow(clippy::cast_possible_truncation)]
fn cut(value: f64) -> i128 {
    value.trunc() as i128
}

/// What steps took, figure by figure.
#[derive(Debug, Default, Clone, Copy)]
struct Took {
    any: bool,
    input: i128,
    output: i128,
    thinking: i128,
    cache_read: i128,
}

impl Took {
    fn add(&mut self, figures: &[(&'static str, i128)]) {
        self.any = true;
        for (name, figure) in figures {
            match *name {
                "input_tokens" => self.input += figure,
                "output_tokens" => self.output += figure,
                "thinking_tokens" => self.thinking += figure,
                "cache_read_input_tokens" => self.cache_read += figure,
                _ => {}
            }
        }
    }

    /// What went in (cache reads with it) and what came out (what the model thought is billed
    /// as what it wrote), as the Python Host counts a turn of any tool.
    fn tokens(self) -> (u64, u64) {
        let whole = |figure: i128| u64::try_from(figure.max(0)).unwrap_or(u64::MAX);
        (
            whole(self.input + self.cache_read),
            whole(self.output + self.thinking),
        )
    }
}

pub struct AgyInterpreter {
    turn: u64,
    /// The turn's own name: what the ids of its pieces are built from.
    name: String,
    /// The conversation was named to the Host.
    bound: bool,
    /// What the agent said in each step in which it answered, by the step's number.
    said: BTreeMap<u64, String>,
    /// The steps whose message is in the conversation, and the last thing so written.
    written: BTreeSet<u64>,
    last_written: String,
    /// The tool calls of this turn, by the id Antigravity or their step gives them.
    tools: BTreeMap<String, ToolCall>,
    activity: Option<Activity>,
    /// What the steps said they took, added up, by figure: each step counted once, by the
    /// first of its updates that said anything of what it took.
    took: Took,
    /// The steps whose usage is counted, by their index as the Python Host writes it.
    counted: BTreeSet<String>,
    /// The lines that are not taken twice: a DONE, a tool's step, the first line and the result,
    /// each by the digest of it as written, keys in order (a tool's step can carry all the tool
    /// printed: the line itself is not kept a second time).
    seen: BTreeSet<String>,
    /// Antigravity said how the turn ended: well, or with what went wrong.
    result: Option<Result<(), String>>,
    stderr: ErrorTail,
    ended: bool,
}

impl AgyInterpreter {
    pub fn new(turn: u64, name: &str) -> Self {
        Self {
            turn,
            name: name.to_string(),
            bound: false,
            said: BTreeMap::new(),
            written: BTreeSet::new(),
            last_written: String::new(),
            tools: BTreeMap::new(),
            activity: None,
            took: Took::default(),
            counted: BTreeSet::new(),
            seen: BTreeSet::new(),
            result: None,
            stderr: ErrorTail::default(),
            ended: false,
        }
    }

    fn moved(&mut self, activity: Activity, out: &mut Vec<Signal>) {
        if self.activity.as_ref() != Some(&activity) {
            self.activity = Some(activity.clone());
            out.push(Signal::Moved(activity));
        }
    }

    /// The conversation's name, the first time it is said: on the first line, on a step,
    /// or with the result.
    fn bind(&mut self, named: &Value, out: &mut Vec<Signal>) {
        let conversation = text_of(named).trim();
        if !self.bound && !conversation.is_empty() {
            self.bound = true;
            out.push(Signal::Bound {
                backend_session: conversation.to_string(),
            });
        }
    }

    fn on_line(&mut self, line: &str, t: i64, out: &mut Vec<Signal>) {
        let Ok(event) = serde_json::from_str::<Value>(line) else {
            return; // not one of its events: a line some tool of its printed
        };
        if self.result.is_some() {
            return; // nothing after the result is of the turn
        }
        // A line Antigravity tells again word for word is told once: a step that is done, a
        // tool's step, the first line and the result each happen once.
        let update = &event["step_update"];
        let once = matches!(text_of(&event["event"]), "init" | "result")
            || update["state"] == "DONE"
            || update["step_type"] == "tool";
        if once && !self.seen.insert(clarp_kernel::live_call::digest(&event)) {
            return;
        }
        match text_of(&event["event"]) {
            "init" => {
                self.bind(&event["conversation_id"], out);
                let model = text_of(&event["init"]["model"]).trim();
                if !model.is_empty() {
                    out.push(Signal::Model(model.to_string()));
                }
                self.moved(Activity::Thinking, out);
            }
            "step_update" => {
                let update = &event["step_update"];
                match &update["conversation_id"] {
                    Value::Null => self.bind(&event["conversation_id"], out),
                    named => self.bind(named, out),
                }
                self.on_step(update, t, out);
            }
            "result" => self.on_result(&event["result"], out),
            _ => {}
        }
    }

    fn on_step(&mut self, update: &Value, t: i64, out: &mut Vec<Signal>) {
        let done = text_of(&update["state"]).eq_ignore_ascii_case("DONE");
        if done {
            // A step is counted once, by the first of its updates that says what it took (an
            // explicit nothing included); one without an index is a step of its own.
            let figures = figures(&update["usage"]);
            let usable = figures.iter().any(|(name, _)| *name != TOTAL);
            let key = match &update["step_index"] {
                Value::Null => None,
                Value::String(index) => Some(index.clone()),
                index => Some(clarp_kernel::python::repr(index)),
            };
            if usable && key.is_none_or(|key| self.counted.insert(key)) {
                self.took.add(&figures);
            }
        }
        let step = update["step_index"].as_u64();
        match text_of(&update["step_type"]) {
            "agent_response" => {
                let Some(step) = step else {
                    return;
                };
                let piece = text_of(&update["text_delta"]);
                let said = self.said.entry(step).or_default();
                said.push_str(piece);
                let said = said.clone();
                let reply = format!("r-{}-{step}", self.name);
                let shown = format!("{SHOWN}:{step}");
                if !said.trim().is_empty() && !piece.is_empty() {
                    out.push(Signal::Glimpse(Glimpse::Message {
                        id: shown.clone(),
                        text: said.clone(),
                        row: Some(reply.clone()),
                    }));
                }
                if !done {
                    if !piece.is_empty() {
                        self.moved(Activity::Responding, out);
                        out.push(Signal::Live(LiveReply {
                            id: reply,
                            text: said,
                        }));
                    }
                    return;
                }
                // The step is over: what was said in it is a message of the conversation.
                if !said.trim().is_empty() && self.written.insert(step) {
                    out.push(Signal::Glimpse(Glimpse::Done { id: shown }));
                    self.last_written.clone_from(&said);
                    out.push(Signal::Reply(Reply {
                        origin: clarp_kernel::turn::Origin::User,
                        id: reply,
                        turn: self.turn,
                        text: said,
                        trace_id: None,
                        ..Reply::default()
                    }));
                }
                self.moved(Activity::Thinking, out);
            }
            "tool" => self.on_tool(update, done, t, out),
            "checkpoint" => self.moved(Activity::Thinking, out),
            _ => {}
        }
    }

    fn on_tool(&mut self, update: &Value, done: bool, t: i64, out: &mut Vec<Signal>) {
        let info = &update["tool_info"];
        // The call's own id where it has one, else its step: a step is one call.
        let raw = match (text_of(&info["id"]), text_of(&update["tool_call_id"])) {
            ("", "") => match update["step_index"].as_u64() {
                Some(step) => format!("step-{step}"),
                None => return,
            },
            ("", id) | (id, _) => id.to_string(),
        };
        let theirs = match text_of(&update["tool_name"]) {
            "" => text_of(&info["name"]),
            name => name,
        };
        let name = tool_name(theirs);
        let input = tool_input(&name, &info["parameters"]);
        let call_id = format!("c-{}-{raw}", self.name);
        let shown = format!("{SHOWN}:{raw}");
        if !self.tools.contains_key(&raw) {
            let call = ToolCall {
                origin: clarp_kernel::turn::Origin::User,
                id: call_id.clone(),
                turn: self.turn,
                name: name.clone(),
                input: input.clone(),
                status: ToolStatus::Running,
                output: None,
                started_at_ms: t,
                ended_at_ms: None,
                ..ToolCall::default()
            };
            self.tools.insert(raw.clone(), call.clone());
            out.push(Signal::Glimpse(Glimpse::Tool {
                id: shown.clone(),
                call_id,
                name: name.clone(),
                input,
                whole: true,
            }));
            out.push(Signal::Tool(call));
            self.moved(Activity::Tool { name }, out);
        }
        if !done {
            return;
        }
        let Some(call) = self.tools.get_mut(&raw) else {
            return;
        };
        if call.status != ToolStatus::Running {
            return; // told twice
        }
        let said_wrong = match &info["error"] {
            Value::Null | Value::Bool(false) => false,
            Value::String(said) => !said.is_empty(),
            _ => true,
        };
        let (came_back, exit_code) = match (&info["output"], &info["error"]) {
            (Value::String(output), _) if !output.is_empty() => came_back(output),
            (_, Value::String(error)) => (error.clone(), None),
            _ => (String::new(), None),
        };
        // A command that ended with another code than nought went wrong, whatever else is said.
        let failed = said_wrong || exit_code.is_some_and(|code| code != 0);
        call.status = if failed {
            ToolStatus::Error
        } else {
            ToolStatus::Ok
        };
        call.output = Some(shorten(&came_back, MAX_TOOL_OUTPUT));
        call.ended_at_ms = Some(t);
        out.push(Signal::Glimpse(Glimpse::Returned {
            id: shown,
            content: Value::String(came_back),
            failed,
            exit_code,
        }));
        out.push(Signal::Tool(call.clone()));
        self.moved(Activity::Thinking, out);
    }

    /// The turn's last line: how it ended, what the agent's answer is, and what it took.
    /// A turn Antigravity refuses before it opens a conversation (a login it does not
    /// take) says so here too, with no conversation named.
    fn on_result(&mut self, result: &Value, out: &mut Vec<Signal>) {
        self.bind(&result["conversation_id"], out);
        let status = text_of(&result["status"]);
        let wrong = text_of(&result["error"]).trim();
        if !status.eq_ignore_ascii_case("SUCCESS") || !wrong.is_empty() {
            let why = if wrong.is_empty() {
                "provider turn failed"
            } else {
                wrong
            };
            let status = if status.is_empty() { "unknown" } else { status };
            self.result = Some(Err(shorten(&format!("status={status}: {why}"), 600)));
            return;
        }
        // The answer as the result says it stands, where no step's message is that already.
        let response = text_of(&result["response"]);
        if !response.trim().is_empty() && response.trim() != self.last_written.trim() {
            out.push(Signal::Reply(Reply {
                origin: clarp_kernel::turn::Origin::User,
                id: format!("r-{}-result", self.name),
                turn: self.turn,
                text: response.to_string(),
                trace_id: None,
                ..Reply::default()
            }));
        }
        // What the turn took: the sum of what its steps say they took. The result's own count
        // is no turn's where a conversation is continued: it is what the whole conversation
        // has taken so far (four calls in one conversation on 17 Nov 2026 said 8,805, 13,774,
        // 18,950 and 24,431 tokens in, for steps of 8,805, 4,969, 5,176 and 5,481). It is
        // taken only where no step said anything (the Python Host's rule since its PR 413).
        let took = if self.took.any {
            Some(self.took)
        } else {
            let terminal = figures(&result["usage"]);
            let mut took = Took::default();
            took.add(&terminal);
            (!terminal.is_empty()).then_some(took)
        };
        if let Some(took) = took {
            let (tokens_in, tokens_out) = took.tokens();
            out.push(Signal::Usage {
                tokens_in,
                tokens_out,
            });
        }
        self.result = Some(Ok(()));
    }

    fn on_exit(&mut self, code: Option<i32>, signal: Option<i32>, out: &mut Vec<Signal>) {
        if self.ended {
            return;
        }
        self.ended = true;
        let said = |fallback: String| self.stderr.text().unwrap_or(fallback);
        let outcome = if let Some(signal) = signal {
            RunOutcome::Interrupted {
                reason: format!("agy was ended by signal {signal}"),
            }
        } else if let Some(Err(reason)) = &self.result {
            RunOutcome::Failed {
                reason: reason.clone(),
            }
        } else if code != Some(0) {
            RunOutcome::Failed {
                reason: said(format!("agy exited with code {}", code.unwrap_or(-1))),
            }
        } else if self.result.is_none() {
            RunOutcome::Failed {
                reason: said("agy ended without saying how its turn ended".to_string()),
            }
        } else {
            RunOutcome::Completed
        };
        out.push(Signal::Ended(outcome));
    }
}

impl Interpreter for AgyInterpreter {
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

    #[test]
    fn a_conversation_is_kept_while_its_file_is_in_the_store() {
        let root = std::env::temp_dir().join(format!("clarp-agy-store-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&root);
        let base = root.join(OWN);
        let kept = |session: &str| crate::past::agy_kept(&base, session);
        // No store on this machine: nothing is known, and nothing counts as let go of.
        assert!(kept("c-1"));
        std::fs::create_dir_all(base.join("conversations")).unwrap();
        // A store without the conversation: Antigravity let go of it. What it leaves behind
        // elsewhere (its notes on the conversation) is no conversation.
        assert!(!kept("c-1"));
        std::fs::create_dir_all(base.join("brain/c-1")).unwrap();
        assert!(!kept("c-1"));
        std::fs::write(base.join("conversations/c-1.db"), "").unwrap();
        assert!(kept("c-1"));
        assert!(!kept("c-2"));
        // What is no name of a file is not looked for.
        for odd in ["..", "../c-1", "a/b", "a\\b"] {
            assert!(!kept(odd), "{odd:?}");
        }
        std::fs::remove_dir_all(&root).unwrap();
    }

    fn spec() -> TurnSpec {
        TurnSpec {
            agent_id: "a1".to_string(),
            session: "rachel".to_string(),
            turn: 3,
            cwd: PathBuf::from("/work"),
            prompt: "-what is in here?".to_string(),
            backend_session: None,
            new_session: Some("conv-1".to_string()),
            model: None,
            effort: None,
            identity: None,
            told: None,
        }
    }

    #[test]
    fn a_turn_is_started_with_the_message_bound_to_its_flag() {
        let agy = Agy::new();
        let fresh = agy.launch(&spec());
        assert_eq!(fresh.program, "agy");
        assert_eq!(
            fresh.args,
            vec![
                "--dangerously-skip-permissions",
                "--output-format",
                "stream-json",
                "--print-timeout",
                "24h",
                "--print=-what is in here?",
            ],
            "a new conversation: Antigravity names it, the Host cannot"
        );
        assert!(fresh.stdin.is_empty());
        assert!(
            fresh
                .env
                .contains(&("CLARP_AGY_MANAGED_TURN".to_string(), "1".to_string()))
        );
        // A conversation it has is continued; a model is named, and then no effort is, since
        // the model's name carries one.
        let next = agy.launch(&TurnSpec {
            backend_session: Some("66666666-7777-8888-9999-aaaaaaaaaaaa".to_string()),
            model: Some("gemini-3.7-flash-low".to_string()),
            effort: Some("high".to_string()),
            ..spec()
        });
        assert_eq!(
            next.args[5..],
            [
                "--model",
                "gemini-3.7-flash-low",
                "--conversation",
                "66666666-7777-8888-9999-aaaaaaaaaaaa",
                "--print=-what is in here?",
            ]
        );
        let effort = agy.launch(&TurnSpec {
            effort: Some(" High ".to_string()),
            ..spec()
        });
        assert_eq!(effort.args[5..7], ["--effort", "high"]);
    }

    #[test]
    fn one_question_is_asked_apart_and_its_answer_read_from_what_is_printed() {
        let agy = Agy::new();
        let asked = agy
            .once(&Question {
                prompt: "Who is this for?".to_string(),
                model: Some("gemini-4-hydrogen-low".to_string()),
                effort: Some("high".to_string()),
            })
            .unwrap();
        assert_eq!(
            asked.args,
            [
                "--dangerously-skip-permissions",
                "--output-format",
                "json",
                "--model",
                "gemini-4-hydrogen-low",
                "--print=Who is this for?",
            ]
        );
        let home = agy.own_home().unwrap();
        assert!(home.linked.contains(&".gemini/oauth_creds.json"));
        assert!(
            home.copied
                .contains(&".gemini/antigravity-cli/settings.json")
        );
        // To read and nothing else: in its sandbox, held to a shape, and with no more of the
        // Host's environment than it needs. Its home is not the Host's.
        let reads = agy
            .reads(&Reading {
                instructions: "Keep the books.".to_string(),
                asked: "GOAL AS RECORDED: ...".to_string(),
                schema: r#"{"type":"object"}"#.to_string(),
                model: None,
                effort: None,
            })
            .unwrap();
        assert_eq!(
            reads.launch.args,
            [
                "--sandbox",
                "--disable-slash-commands",
                "--print-timeout",
                "120s",
                "--output-format",
                "json",
                "--json-schema",
                "{dir}/schema.json",
                "--print=Keep the books.\n\nGOAL AS RECORDED: ...",
            ]
        );
        assert_eq!(
            reads.files,
            [(
                "schema.json".to_string(),
                r#"{"type":"object"}"#.to_string()
            )]
        );
        assert!(reads.keeps.contains(&"PATH") && !reads.keeps.contains(&"HOME"));
        // What it prints: one object, after whatever it wrote before it.
        let fine = "warming up\n{\"conversation_id\":\"c\",\"status\":\"SUCCESS\",\"response\":\" {\\\"a\\\": 1}\\n\"}\n";
        assert_eq!(agy.answered(fine), Ok("{\"a\": 1}".to_string()));
        let failed = r#"{"status":"ERROR","error":"sign-in rejected"}"#;
        assert_eq!(
            agy.answered(failed),
            Err("agy status \"ERROR\": sign-in rejected".to_string())
        );
        assert_eq!(
            agy.answered("no login"),
            Err("agy returned no JSON: no login".to_string())
        );
        assert_eq!(
            agy.answered(r#"{"status":"SUCCESS","response":""}"#),
            Err("agy returned no answer".to_string())
        );
    }

    #[test]
    fn what_a_command_gave_back_is_read_without_what_is_written_around_it() {
        let wrapped = "Created At: 2026-11-17T14:01:27+01:00\nCompleted At: 2026-11-17T14:01:27+01:00\n\
                       Command exited with code 0.\n\nCommand output:\nAGY_OK";
        assert_eq!(came_back(wrapped), ("AGY_OK".to_string(), Some(0)));
        let failed =
            "Created At: x\nCommand exited with code -2.\n\nCommand output:\n  no such file\n";
        assert_eq!(came_back(failed), ("no such file".to_string(), Some(-2)));
        // As older versions gave it: what the command printed, and nothing around it.
        assert_eq!(came_back("AGY_TOOL_OK"), ("AGY_TOOL_OK".to_string(), None));
        // As its own record of a conversation words it.
        let recorded = "Completed At: x\nThe command exited with code 1\nOutput:\nboom\n";
        assert_eq!(came_back(recorded), ("boom".to_string(), Some(1)));
        assert_eq!(came_back("a\r\n"), ("a".to_string(), None));
        // A first line with no end is no line written around it.
        assert_eq!(
            came_back("Created At: now"),
            ("Created At: now".to_string(), None)
        );
        assert_eq!(came_back(""), (String::new(), None));
    }

    #[test]
    fn its_tools_are_named_and_given_as_the_conversation_names_tools() {
        assert_eq!(tool_name("run_command"), "Bash");
        assert_eq!(tool_name("multi_replace_file_content"), "Edit");
        assert_eq!(tool_name("browser_subagent"), "browser_subagent");
        assert_eq!(tool_name(""), "tool");
        assert_eq!(
            tool_input("Bash", &json!({ "CommandLine": "ls -la", "Cwd": "/work" })),
            json!({ "command": "ls -la", "description": "" })
        );
        assert_eq!(
            tool_input("Read", &json!({ "AbsolutePath": "/work/a.txt" })),
            json!({ "file_path": "/work/a.txt" })
        );
        assert_eq!(
            tool_input(
                "Grep",
                &json!({ "Query": "fn main", "SearchPath": "/work" })
            ),
            json!({ "pattern": "fn main", "path": "/work" })
        );
        assert_eq!(
            tool_input("WebFetch", &json!({ "Url": "https://example.org" })),
            json!({ "query": "", "url": "https://example.org" })
        );
        assert_eq!(
            tool_input("browser_subagent", &json!({ "Task": "x" })),
            json!({})
        );
        assert_eq!(
            tool_input("LS", &json!("not an object")),
            json!({ "path": "" })
        );
    }
}
