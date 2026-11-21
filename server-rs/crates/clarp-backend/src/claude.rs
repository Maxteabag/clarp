//! Claude Code, run as `claude -p` with stream-json in both directions.
//!
//! The message goes in as one JSON line on standard input, which is then closed, so the process
//! runs one turn and ends. What it prints is a line of JSON per event: the session it runs in,
//! the reply as it is written piece by piece, each whole message, each tool result, and at the
//! end a result. Everything this file knows is what those lines mean.

use std::collections::{BTreeMap, HashMap, HashSet};
use std::path::{Path, PathBuf};

use clarp_kernel::conversation::{Reply, ToolCall, ToolStatus};
use clarp_kernel::live::Glimpse;
use clarp_kernel::told;
use clarp_kernel::turn::{Activity, RunOutcome};
use clarp_spool::{Entry, Record, ShimEvent};
use serde_json::{Value, json};

use crate::{
    Backend, ErrorTail, Interpreter, Launch, LiveReply, SESSION_ENV, Signal, TurnSpec, shorten,
};

/// What a turn that ran into the provider's limit failed with.
pub const USAGE_LIMIT: &str = "Claude usage limit reached";

/// The most of a tool's output that is kept with the conversation.
const MAX_TOOL_OUTPUT: usize = 4_000;

pub struct Claude {
    program: String,
}

impl Claude {
    pub fn new() -> Self {
        Self::with_program("claude")
    }

    /// Claude Code under another name or path.
    pub fn with_program(program: impl Into<String>) -> Self {
        Self {
            program: program.into(),
        }
    }
}

impl Default for Claude {
    fn default() -> Self {
        Self::new()
    }
}

impl Backend for Claude {
    fn id(&self) -> &'static str {
        "claude"
    }

    fn context_window(&self) -> Option<u64> {
        // What the Python Host declares for Claude Code, whatever model runs.
        Some(1_000_000)
    }

    fn efforts(&self) -> &'static [&'static str] {
        &["low", "medium", "high", "xhigh", "max"]
    }

    /// Claude Code takes its own command for it as a message, also outside its screen: it
    /// sums the conversation up, goes on under the same name, and ends without a word
    /// (measured with 2.1.321 on 17 Nov 2026, `tests/fixtures/claude-2.1.321-compact.jsonl`).
    fn shortens_with(&self) -> Option<&'static str> {
        Some("/compact")
    }

    fn launch(&self, spec: &TurnSpec) -> Launch {
        let mut args: Vec<String> = [
            "-p",
            "--dangerously-skip-permissions",
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--include-partial-messages",
            // Without this the stream is not a stream: the lines come when the turn is over.
            "--verbose",
            // Only the MCP servers the Host names, and so far it names none: a slow or broken
            // server in the user's own configuration must not hold up every turn's start.
            "--strict-mcp-config",
        ]
        .iter()
        .map(|arg| (*arg).to_string())
        .collect();
        let mut option = |flag: &str, value: &Option<String>| {
            if let Some(value) = value.as_ref().filter(|value| !value.is_empty()) {
                args.push(flag.to_string());
                args.push(value.clone());
            }
        };
        option("--model", &spec.model);
        option("--effort", &spec.effort);
        option("--append-system-prompt", &spec.identity);
        // A session it has: continue it. None yet: start one under the name the Host chose,
        // or, with no name given, under one of the tool's own, which its first line tells.
        if spec
            .backend_session
            .as_ref()
            .is_some_and(|session| !session.is_empty())
        {
            option("--resume", &spec.backend_session);
        } else {
            option("--session-id", &spec.new_session);
        }
        let mut content = vec![json!({"type": "text", "text": spec.prompt})];
        // What the Host says with the prompt: after the user's own words, and marked as
        // not theirs. On the Python Host a hook of Claude Code's says it, and Claude Code
        // hands the model what a hook says between the same marks.
        if let Some(told) = &spec.told {
            let said = told::beside(&told.persona, &spec.session, told.spoken);
            let said = told::in_call(said, told.call.as_deref());
            let (opens, ends) = REMINDER;
            content.push(json!({"type": "text", "text": format!("{opens}\n{said}\n{ends}")}));
        }
        let message = json!({
            "type": "user",
            "message": {"role": "user", "content": content},
        });
        let mut stdin = message.to_string().into_bytes();
        stdin.push(b'\n');
        Launch {
            prompt_as_file: false,
            before_stop: None,
            program: self.program.clone(),
            args,
            env: vec![
                (SESSION_ENV.to_string(), spec.session.clone()),
                (crate::MANAGED_CLAUDE_ENV.into(), "1".into()),
            ],
            stdin,
        }
    }

    /// Claude Code's own directory (`CLAUDE_CONFIG_DIR`, else `.claude` at home) holds the
    /// login it runs with.
    fn login_file(&self, home: &Path) -> Option<PathBuf> {
        let dir = std::env::var_os("CLAUDE_CONFIG_DIR")
            .filter(|dir| !dir.is_empty())
            .map_or_else(|| home.join(".claude"), PathBuf::from);
        Some(dir.join(".credentials.json"))
    }

    fn interpreter(&self, turn: u64, name: &str) -> Box<dyn Interpreter> {
        Box::new(ClaudeInterpreter::new(turn, name))
    }

    /// One `claude -p` whose conversation is not kept, as the Python Host asks Claude Code
    /// one question (`backend/claude.py`, `routing_cmd`), and with none of the user's own
    /// MCP servers, which a question has no use for and would wait for. What it prints is
    /// its answer.
    fn once(&self, question: &crate::Question) -> Option<Launch> {
        let mut args: Vec<String> = [
            "-p",
            "--dangerously-skip-permissions",
            "--no-session-persistence",
            "--strict-mcp-config",
        ]
        .iter()
        .map(|arg| (*arg).to_string())
        .collect();
        for (flag, value) in [("--model", &question.model), ("--effort", &question.effort)] {
            if let Some(value) = value.as_ref().filter(|value| !value.is_empty()) {
                args.push(flag.to_string());
                args.push(value.clone());
            }
        }
        Some(Launch {
            prompt_as_file: false,
            before_stop: None,
            program: self.program.clone(),
            args,
            env: Vec::new(),
            stdin: question.prompt.clone().into_bytes(),
        })
    }
}

/// A message of the agent's while it is being written.
struct Draft {
    /// The provider's id for the message. Empty when the tool did not say.
    id: String,
    /// The text blocks of the message, by their index in it.
    blocks: Vec<(u64, String)>,
    /// The tools the message calls, by the ids of their uses, in the order it calls them.
    tools: Vec<String>,
}

impl Draft {
    fn text(&self) -> String {
        let mut text = String::new();
        for (_, block) in &self.blocks {
            if block.is_empty() {
                continue;
            }
            if !text.is_empty() {
                text.push_str("\n\n");
            }
            text.push_str(block);
        }
        text
    }
}

struct ResultFacts {
    is_error: bool,
    text: String,
}

/// What the ids of Claude Code's live items begin with.
const SHOWN: &str = "cl";

/// The marks between which Claude Code hands the model what is not the user's own words.
const REMINDER: (&str, &str) = ("<system-reminder>", "</system-reminder>");

/// What kind of item a block of a message is shown as.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Block {
    Reasoning,
    Message,
    Tool,
}

/// The row a standalone block reader associates with a text block's provider message.
type BlockRow<'a> = &'a dyn Fn(&str) -> Option<String>;

/// What those who watch a turn are shown of it: every block of a message as an item of its
/// own, a tool call from the moment its name is known, and what each tool returned.
///
/// The Python Host's `ClaudeLiveItems` (`server/lib/live_claude.py`), held to it by the
/// streams in `clarp-kernel/tests/fixtures/live-vectors.json`.
#[derive(Debug, Default)]
struct Shown {
    /// The message being written, by the provider's id.
    message: String,
    /// Its blocks so far, in the order they began: which block, what it is shown as, and
    /// the id of its item.
    blocks: Vec<(i64, Block, String)>,
    /// The text of each message and thought so far, by item.
    texts: HashMap<String, String>,
    /// What each tool call is given, as far as it has arrived, by block.
    inputs: HashMap<i64, String>,
    /// The name of each tool that was called, by the provider's id of the call.
    tools: HashMap<String, String>,
}

impl Shown {
    fn block(&self, index: i64) -> Option<(Block, String)> {
        self.blocks
            .iter()
            .find(|(at, _, _)| *at == index)
            .map(|(_, kind, id)| (*kind, id.clone()))
    }

    fn begin(&mut self, index: i64, kind: Block, id: &str) {
        let block = (index, kind, id.to_string());
        match self.blocks.iter_mut().find(|(at, _, _)| *at == index) {
            Some(held) => *held = block,
            None => self.blocks.push(block),
        }
    }

    /// Takes one event of the stream. Root replies are shown from their draft instead.
    /// A block reader can still show text from a nested stream, which has no parent row.
    fn see(&mut self, event: &Value, row: Option<BlockRow<'_>>, out: &mut Vec<Signal>) {
        let piece = match text_of(&event["type"]) {
            "user" => {
                self.returned(&event["message"]["content"], out);
                return;
            }
            "stream_event" if event["event"].is_object() => &event["event"],
            _ => return,
        };
        let index = piece["index"].as_i64();
        match (text_of(&piece["type"]), index) {
            ("message_start", _) => {
                self.message = text_of(&piece["message"]["id"]).to_string();
                self.blocks.clear();
                self.inputs.clear();
            }
            ("message_stop", _) => {
                for (_, kind, id) in &self.blocks {
                    if matches!(kind, Block::Reasoning | Block::Message) {
                        out.push(Signal::Glimpse(Glimpse::Done { id: id.clone() }));
                    }
                }
            }
            ("content_block_start", Some(index)) => {
                self.block_began(index, &piece["content_block"], row, out);
            }
            ("content_block_delta", Some(index)) => self.block_grew(index, &piece["delta"], out),
            ("content_block_stop", Some(index)) => self.block_ended(index, out),
            _ => {}
        }
    }

    fn block_began(
        &mut self,
        index: i64,
        block: &Value,
        row: Option<BlockRow<'_>>,
        out: &mut Vec<Signal>,
    ) {
        let id = format!("{SHOWN}:{}:{index}", self.message);
        match text_of(&block["type"]) {
            "thinking" | "redacted_thinking" => {
                let text = text_of(&block["thinking"]).to_string();
                self.begin(index, Block::Reasoning, &id);
                self.texts.insert(id.clone(), text.clone());
                out.push(Signal::Glimpse(Glimpse::Reasoning { id, text }));
            }
            "text" => {
                if let Some(row) = row {
                    let text = text_of(&block["text"]).to_string();
                    self.begin(index, Block::Message, &id);
                    self.texts.insert(id.clone(), text.clone());
                    let row = row(&self.message);
                    out.push(Signal::Glimpse(Glimpse::Message { id, text, row }));
                }
            }
            "tool_use" | "server_tool_use" if !text_of(&block["id"]).is_empty() => {
                let call_id = text_of(&block["id"]).to_string();
                let name = match text_of(&block["name"]) {
                    "" => "tool",
                    name => name,
                };
                let id = format!("{SHOWN}:{call_id}");
                self.begin(index, Block::Tool, &id);
                self.tools.insert(call_id.clone(), name.to_string());
                self.inputs.insert(index, String::new());
                out.push(Signal::Glimpse(Glimpse::Tool {
                    id,
                    call_id,
                    name: name.to_string(),
                    input: block["input"].clone(),
                    whole: false,
                }));
            }
            _ => {}
        }
    }

    fn block_grew(&mut self, index: i64, delta: &Value, out: &mut Vec<Signal>) {
        let Some((kind, id)) = self.block(index) else {
            return;
        };
        match (kind, text_of(&delta["type"])) {
            (Block::Reasoning, "thinking_delta") => {
                let text = self.texts.entry(id.clone()).or_default();
                text.push_str(text_of(&delta["thinking"]));
                let text = text.clone();
                out.push(Signal::Glimpse(Glimpse::Reasoning { id, text }));
            }
            (Block::Message, "text_delta") => {
                let text = self.texts.entry(id.clone()).or_default();
                text.push_str(text_of(&delta["text"]));
                let text = text.clone();
                out.push(Signal::Glimpse(Glimpse::Message {
                    id,
                    text,
                    row: None,
                }));
            }
            (Block::Tool, "input_json_delta") => {
                self.inputs
                    .entry(index)
                    .or_default()
                    .push_str(text_of(&delta["partial_json"]));
            }
            _ => {}
        }
    }

    fn block_ended(&mut self, index: i64, out: &mut Vec<Signal>) {
        let Some((kind, id)) = self.block(index) else {
            return;
        };
        if kind != Block::Tool {
            out.push(Signal::Glimpse(Glimpse::Done { id }));
            return;
        }
        // What the tool is given is whole now. What is no JSON is taken for nothing given;
        // what is JSON and no object is left alone.
        let given = self.inputs.get(&index).map_or("", String::as_str);
        let input = match given {
            "" => json!({}),
            given => serde_json::from_str::<Value>(given).unwrap_or_else(|_| json!({})),
        };
        if !input.is_object() {
            return;
        }
        let call_id = id.split_once(':').map_or("", |(_, call)| call).to_string();
        let name = self.tools.get(&call_id).map_or("tool", String::as_str);
        out.push(Signal::Glimpse(Glimpse::Tool {
            id,
            name: name.to_string(),
            call_id,
            input,
            whole: true,
        }));
    }

    /// What tools returned: the parts of a message of the user's that are results.
    fn returned(&self, content: &Value, out: &mut Vec<Signal>) {
        for part in content.as_array().into_iter().flatten() {
            if text_of(&part["type"]) != "tool_result" {
                continue;
            }
            out.push(Signal::Glimpse(Glimpse::Returned {
                id: format!("{SHOWN}:{}", text_of(&part["tool_use_id"])),
                content: part["content"].clone(),
                failed: clarp_kernel::live::truthy(&part["is_error"]),
                exit_code: None,
            }));
        }
    }
}

pub struct ClaudeInterpreter {
    turn: u64,
    /// The turn's own name, for the ids of pieces the tool does not name.
    name: String,
    session: Option<String>,
    /// The activity last reported, so that only a change is reported.
    activity: Option<Activity>,
    draft: Option<Draft>,
    /// The messages that arrive piece by piece. Their text is taken from the pieces, so the
    /// whole message that follows the pieces must not be taken again.
    streamed: HashSet<String>,
    replies: u32,
    /// How many replies each message has become. One, unless the tool goes on with a message
    /// after something already answered it: what comes then is a reply of its own.
    told: HashMap<String, u32>,
    /// Every tool call of this turn, as it stands.
    tools: BTreeMap<String, ToolCall>,
    helpers: BTreeMap<String, Value>,
    /// The tool calls whose start was reported as an activity.
    announced: HashSet<String>,
    usage_limit: bool,
    /// What Claude Code itself said went wrong, in the place where the agent would have spoken.
    api_error: Option<String>,
    result: Option<ResultFacts>,
    stderr: ErrorTail,
    ended: bool,
    /// What those who watch the turn are shown of it.
    shown: Shown,
    /// A row's first visible item keeps its name even when an empty draft was discarded.
    live_items: HashMap<String, String>,
    nested_shown: HashMap<String, Shown>,
}

fn text_of(value: &Value) -> &str {
    value.as_str().unwrap_or("")
}

/// The text of a tool result, which is a string or a list of blocks.
fn content_text(content: &Value) -> String {
    match content {
        Value::String(text) => text.clone(),
        Value::Array(blocks) => blocks
            .iter()
            .filter(|block| text_of(&block["type"]) == "text")
            .map(|block| text_of(&block["text"]))
            .collect::<Vec<_>>()
            .join("\n"),
        _ => String::new(),
    }
}

/// What a tool gave back, as a text: its own text, or what its parts say one under the
/// other. A part says its `text`, or its `content`; one that says nothing is left out. (The
/// Python Host's rule for what a card shows of a result.)
fn result_text(content: &Value) -> String {
    match content {
        Value::String(text) => text.clone(),
        Value::Array(_) => clarp_kernel::tools::result_parts(content),
        _ => String::new(),
    }
}

fn update_helper(call: &mut ToolCall, state: &Value, t: i64, out: &mut Vec<Signal>) {
    let cell = clarp_kernel::helper_cells::shown(state, &Default::default());
    if call.cell.as_ref() == Some(&cell) {
        return;
    }
    call.status = match cell["status"].as_str() {
        Some("ok") => ToolStatus::Ok,
        Some("error") => ToolStatus::Error,
        _ => ToolStatus::Running,
    };
    call.ended_at_ms = (call.status != ToolStatus::Running).then_some(t);
    call.cell = Some(cell);
    out.push(Signal::Tool(call.clone()));
}

impl ClaudeInterpreter {
    pub fn new(turn: u64, name: &str) -> Self {
        Self {
            turn,
            name: name.to_string(),
            session: None,
            activity: None,
            draft: None,
            streamed: HashSet::new(),
            replies: 0,
            told: HashMap::new(),
            tools: BTreeMap::new(),
            helpers: BTreeMap::new(),
            announced: HashSet::new(),
            usage_limit: false,
            api_error: None,
            result: None,
            stderr: ErrorTail::default(),
            ended: false,
            shown: Shown::default(),
            live_items: HashMap::new(),
            nested_shown: HashMap::new(),
        }
    }

    /// Reports an activity. A tool call is always news; anything else only when it changes.
    fn moved(&mut self, activity: Activity, out: &mut Vec<Signal>) {
        if matches!(activity, Activity::Tool { .. }) || self.activity.as_ref() != Some(&activity) {
            self.activity = Some(activity.clone());
            out.push(Signal::Moved(activity));
        }
    }

    fn reply_id(&self, draft: &Draft) -> String {
        self.row_of(&draft.id)
    }

    /// The id of the reply that the message with this id of the provider's becomes.
    fn row_of(&self, message: &str) -> String {
        // The provider's own name for the message, without its `msg_`: what is left is one
        // unbroken id, as every other id of a conversation is.
        match message.strip_prefix("msg_").unwrap_or(message) {
            "" => format!("r-{}-{}", self.name, self.replies + 1),
            // A reply is written once. More of a message that became one is another.
            name => match self.told.get(message) {
                Some(before) => format!("r-{name}-{}", before + 1),
                None => format!("r-{name}"),
            },
        }
    }

    /// The first text block names the live item. A later reply using an earlier provider id
    /// has a different durable row and must therefore have a different item too.
    fn message_item(&self, draft: &Draft, row: &str) -> Option<String> {
        let (first, _) = draft.blocks.first()?;
        if draft.id.is_empty() || self.told.contains_key(&draft.id) {
            Some(format!("{SHOWN}:reply:{row}"))
        } else {
            Some(format!("{SHOWN}:{}:{first}", draft.id))
        }
    }

    /// The live item and the provisional log row are two views of the same reply.
    fn live(&mut self, out: &mut Vec<Signal>) {
        if let Some(draft) = &self.draft {
            let text = draft.text();
            let row = self.reply_id(draft);
            if let Some(id) = self.message_item(draft, &row) {
                let id = self.live_items.entry(row.clone()).or_insert(id).clone();
                out.push(Signal::Glimpse(Glimpse::Message {
                    id,
                    text: text.clone(),
                    row: Some(row.clone()),
                }));
            }
            if !text.is_empty() {
                out.push(Signal::Live(LiveReply { id: row, text }));
            }
        }
    }

    /// The message being written is whole: it becomes a reply, with what it said and the
    /// tools it calls, and the uses of those tools begin. A message that says nothing and
    /// calls a tool is a reply too: it is the row its tools are shown on.
    fn settle(&mut self, out: &mut Vec<Signal>) {
        let Some(draft) = self.draft.take() else {
            return;
        };
        let text = draft.text();
        if text.trim().is_empty() && draft.tools.is_empty() {
            return;
        }
        let id = self.reply_id(&draft);
        if let Some(item) = self.live_items.remove(&id) {
            out.push(Signal::Glimpse(Glimpse::Done { id: item }));
        }
        self.replies += 1;
        if !draft.id.is_empty() {
            *self.told.entry(draft.id).or_default() += 1;
        }
        out.push(Signal::Reply(Reply {
            origin: clarp_kernel::turn::Origin::User,
            id: id.clone(),
            turn: self.turn,
            text: if text.trim().is_empty() {
                String::new()
            } else {
                text
            },
            trace_id: None,
            tools: draft.tools.clone(),
        }));
        for call in draft.tools {
            if let Some(call) = self.tools.get_mut(&call) {
                call.reply = Some(id.clone());
                out.push(Signal::Tool(call.clone()));
            }
        }
    }

    /// The draft for the message with this id, settling the one before it.
    fn draft_for(&mut self, id: &str, out: &mut Vec<Signal>) -> &mut Draft {
        if self.draft.as_ref().is_some_and(|draft| draft.id != id) {
            self.settle(out);
        }
        self.draft.get_or_insert_with(|| Draft {
            id: id.to_string(),
            blocks: Vec::new(),
            tools: Vec::new(),
        })
    }

    /// The text block with this index in the message being written.
    fn block(&mut self, index: u64) -> &mut String {
        let draft = self.draft.get_or_insert_with(|| Draft {
            id: String::new(),
            blocks: Vec::new(),
            tools: Vec::new(),
        });
        let at = match draft.blocks.iter().position(|(i, _)| *i == index) {
            Some(at) => at,
            None => {
                draft.blocks.push((index, String::new()));
                draft.blocks.len() - 1
            }
        };
        &mut draft.blocks[at].1
    }

    fn on_line(&mut self, line: &str, t: i64, out: &mut Vec<Signal>) {
        // The tool sometimes prints a line that is not an event. It is not ours to read.
        let Ok(event) = serde_json::from_str::<Value>(line) else {
            return;
        };
        // What a sub-agent says belongs to its own stream. It has no row in the parent's
        // conversation, and its raw block indexes must not replace the parent's.
        let parent = text_of(&event["parent_tool_use_id"]);
        if !parent.is_empty() {
            if event["isMeta"].as_bool() != Some(true) {
                self.nested_shown
                    .entry(parent.to_string())
                    .or_default()
                    .see(&event, Some(&|_| None), out);
            }
            return;
        }
        if event["isMeta"].as_bool() != Some(true) {
            self.shown.see(&event, None, out);
        }
        match text_of(&event["type"]) {
            "system" => self.on_system(&event, out),
            "stream_event" => self.on_stream(&event["event"], out),
            "assistant" => self.on_assistant(&event, t, out),
            "user" => {
                self.on_user(&event["message"], &event["toolUseResult"], t, out);
                let content = &event["message"]["content"];
                if let Some(s) = content.as_str() {
                    self.helper_notifications(s, t, out);
                } else if let Some(parts) = content.as_array() {
                    let texts = parts
                        .iter()
                        .filter(|b| b["type"] == "text")
                        .filter_map(|b| b["text"].as_str())
                        .collect::<Vec<_>>();
                    self.helper_notifications(&texts.join("\n"), t, out);
                }
            }
            "queue-operation" => {
                if let Some(s) = event["content"].as_str() {
                    self.helper_notifications(s, t, out);
                }
            }
            "result" => {
                // One process can answer more than one thing: a session resumed with background
                // work behind it answers that first, with a result of its own. The turn is over
                // when the process is, and the last result is the one that counts.
                self.settle(out);
                // What the turn took in is what was sent, what was written to the cache and
                // what was read from it, as the Python Host counts it
                // (`turn_stop._result_detail`): a turn that goes on in a long conversation
                // reads nearly all of it from the cache.
                let said = event["usage"].as_object();
                if let Some(usage) = said.filter(|usage| !usage.is_empty()) {
                    let count = |name: &str| usage.get(name).and_then(Value::as_u64).unwrap_or(0);
                    out.push(Signal::Usage {
                        tokens_in: count("input_tokens")
                            + count("cache_creation_input_tokens")
                            + count("cache_read_input_tokens"),
                        tokens_out: count("output_tokens"),
                    });
                }
                // What it says its session has cost so far: what the calls would be billed
                // at, also on an account that pays by the month. The running total of the
                // session, carried over where one is continued, and not the turn's own cost:
                // three calls in one session on 17 Nov 2026 said 0.0765, 0.1266 and 0.1436
                // dollars, the second for a turn that took less than the first.
                let cost = event["total_cost_usd"].as_f64();
                if let Some(cost) = cost.filter(|cost| cost.is_finite() && *cost >= 0.0) {
                    out.push(Signal::Cost {
                        micros: (cost * 1_000_000.0).round() as u64,
                    });
                }
                self.result = Some(ResultFacts {
                    is_error: event["is_error"].as_bool().unwrap_or(false),
                    text: text_of(&event["result"]).to_string(),
                });
                out.push(Signal::Concluded);
            }
            "rate_limit_event" => {
                let info = &event["rate_limit_info"];
                let status = text_of(&info["status"]);
                // The account is out. The turn ends on it, and its end says so: nothing
                // is put into the conversation at this moment.
                if matches!(status, "rejected" | "blocked") {
                    self.usage_limit = true;
                }
                // What it says of the account's windows with that is told on.
                if info["unifiedWindows"].is_object() {
                    out.push(Signal::Limits(info.clone()));
                }
            }
            _ => {}
        }
    }

    fn on_system(&mut self, event: &Value, out: &mut Vec<Signal>) {
        match text_of(&event["subtype"]) {
            "init" => {
                let model = text_of(&event["model"]);
                if !model.is_empty() {
                    out.push(Signal::Model(model.to_string()));
                }
                let session = text_of(&event["session_id"]).trim();
                if !session.is_empty() && self.session.as_deref() != Some(session) {
                    self.session = Some(session.to_string());
                    out.push(Signal::Bound {
                        backend_session: session.to_string(),
                    });
                }
            }
            "status" if text_of(&event["status"]) == "compacting" => {
                self.moved(Activity::Compacting, out);
                out.push(Signal::Glimpse(Glimpse::Compacting {
                    prefix: SHOWN.to_string(),
                    over: false,
                }));
            }
            "compact_boundary" => {
                self.moved(Activity::Thinking, out);
                out.push(Signal::Glimpse(Glimpse::Compacting {
                    prefix: SHOWN.to_string(),
                    over: true,
                }));
                // How much the conversation holds now, where the tool says so: how full it is
                // from here on, until an answer says otherwise.
                if let Some(held) = event["compact_metadata"]["post_tokens"].as_u64() {
                    out.push(Signal::Context { tokens: held });
                }
                if let Some(record) = clarp_kernel::compaction::boundary(event) {
                    out.push(Signal::Compacted(record));
                }
            }
            _ => {}
        }
    }

    /// A piece of a message, as the provider streams it.
    fn on_stream(&mut self, piece: &Value, out: &mut Vec<Signal>) {
        let index = piece["index"].as_u64().unwrap_or(0);
        match text_of(&piece["type"]) {
            "message_start" => {
                let id = text_of(&piece["message"]["id"]).to_string();
                self.draft_for(&id, out);
                self.streamed.insert(id);
            }
            "content_block_start" => {
                let block = &piece["content_block"];
                match text_of(&block["type"]) {
                    "text" => {
                        self.moved(Activity::Responding, out);
                        let text = text_of(&block["text"]);
                        self.block(index).push_str(text);
                        self.live(out);
                    }
                    "thinking" | "redacted_thinking" => self.moved(Activity::Thinking, out),
                    "tool_use" | "server_tool_use" => {
                        self.announce(text_of(&block["id"]), text_of(&block["name"]), out);
                    }
                    _ => {}
                }
            }
            "content_block_delta" => {
                let delta = &piece["delta"];
                match text_of(&delta["type"]) {
                    "text_delta" => {
                        let text = text_of(&delta["text"]);
                        if !text.is_empty() {
                            self.moved(Activity::Responding, out);
                            self.block(index).push_str(text);
                            self.live(out);
                        }
                    }
                    "thinking_delta" | "signature_delta" => self.moved(Activity::Thinking, out),
                    _ => {}
                }
            }
            "message_stop" => self.settle(out),
            _ => {}
        }
    }

    /// Reports a tool call as the agent's activity, once.
    fn announce(&mut self, id: &str, name: &str, out: &mut Vec<Signal>) {
        if !id.is_empty() && self.announced.insert(id.to_string()) {
            let name = if name.is_empty() { "tool" } else { name };
            self.moved(
                Activity::Tool {
                    name: name.to_string(),
                },
                out,
            );
        }
    }

    /// A whole message, or a whole block of one: where the tool calls are complete.
    fn on_assistant(&mut self, event: &Value, t: i64, out: &mut Vec<Signal>) {
        let message = &event["message"];
        let id = text_of(&message["id"]).to_string();
        let Some(content) = message["content"].as_array() else {
            return;
        };
        // When the provider cannot be reached or refuses, Claude Code writes the error where a
        // message of the agent would be. The agent did not say it: it is why the turn failed.
        let marked = |value: &Value| value["isApiErrorMessage"].as_bool() == Some(true);
        if marked(event) || marked(message) || text_of(&message["model"]) == "<synthetic>" {
            let said = content_text(&Value::Array(content.clone()));
            if !said.trim().is_empty() {
                self.api_error = Some(said);
            }
            return;
        }
        // How full the conversation is: what this answer was made from, sent anew, read from
        // the cache and written to it. The sum a turn's result says counts every step of the
        // turn anew, and says nothing of that (the Python Host reads the same off the
        // transcript's last answer, `claude_transcript.context_tokens_from_jsonl`).
        let said = message["usage"].as_object();
        if let Some(usage) = said.filter(|usage| !usage.is_empty()) {
            let count = |name: &str| usage.get(name).and_then(Value::as_u64).unwrap_or(0);
            out.push(Signal::Context {
                tokens: count("input_tokens")
                    + count("cache_read_input_tokens")
                    + count("cache_creation_input_tokens"),
            });
        }
        for block in content {
            match text_of(&block["type"]) {
                "tool_use" | "server_tool_use" => {
                    let call_id = text_of(&block["id"]);
                    if call_id.is_empty() {
                        continue;
                    }
                    let name = match text_of(&block["name"]) {
                        "" => "tool",
                        name => name,
                    };
                    self.announce(call_id, name, out);
                    if !self.tools.contains_key(call_id) {
                        // The call belongs to its message, and is told when that message
                        // is whole: after the reply it is shown on.
                        self.draft_for(&id, out).tools.push(call_id.to_string());
                        let cell = if clarp_kernel::helper_cells::is_helper(name)
                            && block["type"] == "tool_use"
                        {
                            let state =
                                clarp_kernel::helper_cells::started(call_id, &block["input"]);
                            let cell =
                                clarp_kernel::helper_cells::shown(&state, &Default::default());
                            self.helpers.insert(call_id.to_owned(), state);
                            Some(cell)
                        } else {
                            None
                        };
                        let call = ToolCall {
                            cell,
                            origin: clarp_kernel::turn::Origin::User,
                            id: call_id.to_string(),
                            turn: self.turn,
                            name: name.to_string(),
                            input: block["input"].clone(),
                            status: ToolStatus::Running,
                            output: None,
                            started_at_ms: t,
                            ended_at_ms: None,
                            ..ToolCall::default()
                        };
                        self.tools.insert(call_id.to_string(), call);
                    }
                }
                "text" if !self.streamed.contains(&id) => {
                    let text = text_of(&block["text"]);
                    if text.is_empty() {
                        continue;
                    }
                    // A message that arrives whole was not seen being written: the agent is
                    // said to be answering only while its answer streams, as the Python Host
                    // says it.
                    let draft = self.draft_for(&id, out);
                    let index = draft.blocks.len() as u64;
                    draft.blocks.push((index, text.to_string()));
                    self.live(out);
                }
                _ => {}
            }
        }
        // A complete Agent/Task block can run for minutes before another
        // parent message arrives. Publish its row as soon as the call is known.
        if self
            .draft
            .as_ref()
            .is_some_and(|d| d.tools.iter().any(|id| self.helpers.contains_key(id)))
        {
            self.settle(out);
        }
    }

    /// What came back to the agent: the results of its tool calls.
    fn on_user(&mut self, message: &Value, meta: &Value, t: i64, out: &mut Vec<Signal>) {
        let Some(content) = message["content"].as_array() else {
            return;
        };
        // A result of a call the message being written makes: that message is whole enough.
        let answered = |draft: &Draft| {
            content.iter().any(|block| {
                text_of(&block["type"]) == "tool_result"
                    && draft
                        .tools
                        .iter()
                        .any(|call| call == text_of(&block["tool_use_id"]))
            })
        };
        if self.draft.as_ref().is_some_and(answered) {
            self.settle(out);
        }
        let mut finished = false;
        for block in content {
            if text_of(&block["type"]) != "tool_result" {
                continue;
            }
            let Some(call) = self.tools.get_mut(text_of(&block["tool_use_id"])) else {
                continue;
            };
            if let Some(state) = self.helpers.get_mut(&call.id) {
                clarp_kernel::helper_cells::returned(state, block, meta);
                update_helper(call, state, t, out);
                finished |= call.status != ToolStatus::Running;
                continue;
            }
            if call.status != ToolStatus::Running {
                continue;
            }
            call.status = if block["is_error"].as_bool().unwrap_or(false) {
                ToolStatus::Error
            } else {
                ToolStatus::Ok
            };
            call.output = Some(shorten(&result_text(&block["content"]), MAX_TOOL_OUTPUT));
            call.ended_at_ms = Some(t);
            out.push(Signal::Tool(call.clone()));
            finished = true;
        }
        if finished {
            self.moved(Activity::Thinking, out);
        }
    }

    fn helper_notifications(&mut self, text: &str, t: i64, out: &mut Vec<Signal>) {
        if !text.contains("<task-notification>") {
            return;
        }
        clarp_kernel::helper_cells::notifications(text, |id, status, agent, result| {
            if let Some(state) = self.helpers.get_mut(id)
                && let Some(call) = self.tools.get_mut(id)
            {
                clarp_kernel::helper_cells::notified(state, status, agent, result);
                update_helper(call, state, t, out);
            }
        });
    }

    fn on_exit(&mut self, code: Option<i32>, signal: Option<i32>, out: &mut Vec<Signal>) {
        if self.ended {
            return;
        }
        self.ended = true;
        // A tool that still runs when the process goes is left as it is: what becomes of
        // it follows from how the turn ended, which is the Host's to say.
        self.settle(out);
        // What Claude Code said went wrong comes first; what else is known comes after it.
        let api_error = self.api_error.take();
        let failed = |detail: String| RunOutcome::Failed {
            reason: match &api_error {
                Some(said) if !detail.contains(said.as_str()) => {
                    shorten(&format!("{said}\n{detail}"), 900)
                }
                _ => detail,
            },
        };
        let outcome = if self.usage_limit {
            RunOutcome::Failed {
                reason: USAGE_LIMIT.to_string(),
            }
        } else if let Some(signal) = signal {
            RunOutcome::Interrupted {
                reason: format!("claude was ended by signal {signal}"),
            }
        } else if code != Some(0) {
            failed(
                self.stderr
                    .text()
                    .unwrap_or_else(|| format!("claude exited with code {}", code.unwrap_or(-1))),
            )
        } else {
            match &self.result {
                None => failed(
                    self.stderr
                        .text()
                        .unwrap_or_else(|| "claude ended without a result".to_string()),
                ),
                Some(result) if result.is_error => failed(if result.text.trim().is_empty() {
                    "claude reported an error".to_string()
                } else {
                    shorten(&result.text, 600)
                }),
                Some(_) => RunOutcome::Completed,
            }
        };
        out.push(Signal::Ended(outcome));
    }
}

impl Interpreter for ClaudeInterpreter {
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

    fn read(lines: &[&str], exit: (Option<i32>, Option<i32>)) -> Vec<Signal> {
        let mut interpreter = ClaudeInterpreter::new(7, "m7");
        let mut out = Vec::new();
        let mut t = 1_000;
        for line in lines {
            t += 10;
            let entry = match line.strip_prefix("ERR ") {
                Some(error) => Entry::Err(error.to_string()),
                None => Entry::Out((*line).to_string()),
            };
            interpreter.on_record(&Record { t, entry }, &mut out);
        }
        let gone = Entry::Shim(ShimEvent::Exited {
            code: exit.0,
            signal: exit.1,
        });
        interpreter.on_record(
            &Record {
                t: t + 10,
                entry: gone.clone(),
            },
            &mut out,
        );
        let settled = out.len();
        interpreter.on_record(
            &Record {
                t: t + 20,
                entry: gone,
            },
            &mut out,
        );
        assert_eq!(out.len(), settled, "a turn ends once");
        out
    }

    /// What a turn did: the signals without the reply as it grows, and without what the tool
    /// says of itself.
    fn durable(signals: &[Signal]) -> Vec<Signal> {
        signals
            .iter()
            .filter(|signal| {
                !matches!(
                    signal,
                    Signal::Live(_)
                        | Signal::Glimpse(_)
                        | Signal::Model(_)
                        | Signal::Usage { .. }
                        | Signal::Context { .. }
                        | Signal::Compacted(_)
                        | Signal::Concluded
                        | Signal::Cost { .. }
                )
            })
            .cloned()
            .collect()
    }

    #[test]
    fn what_is_shown_of_a_stream_is_what_the_python_host_shows() {
        use clarp_kernel::live::Hub;

        let vectors: Value = serde_json::from_str(include_str!(
            "../../clarp-kernel/tests/fixtures/live-vectors.json"
        ))
        .unwrap();
        let cases = vectors["claude"].as_array().unwrap();
        assert!(cases.len() >= 4);
        let row = |message: &str| {
            Some(format!(
                "r-{}",
                message.strip_prefix("msg_").unwrap_or(message)
            ))
        };
        let mut held = 0;
        for case in cases {
            let name = case["name"].as_str().unwrap();
            let mut hub = Hub::new("epoch-1", "agent-1", "");
            let mut shown = Shown::default();
            for (place, step) in case["steps"].as_array().unwrap().iter().enumerate() {
                let now = step["at"].as_i64().unwrap();
                let events = if let Some(turn) = step["begin"].as_str() {
                    hub.begin_turn("claire", "conv-1", turn, None, "user", now)
                } else if let Some(status) = step["end"].as_str() {
                    hub.end_turn(status, None, now)
                } else if step.get("fire").is_some() {
                    hub.fire(now)
                } else {
                    let mut signals = Vec::new();
                    shown.see(&step["event"], Some(&row), &mut signals);
                    let mut events = Vec::new();
                    for signal in &signals {
                        if let Signal::Glimpse(glimpse) = signal {
                            events.extend(hub.show(glimpse, Some("tr-1"), now));
                        }
                    }
                    events
                };
                let said = format!("{name}, step {place}: {step}");
                assert_eq!(json!(events), step["events"], "{said}");
                assert_eq!(json!(hub.due_ms()), step["due"], "{said}: held back until");
                held += events.len();
            }
            let after = &case["after"];
            let (snapshot, events) = hub
                .snapshot(after["at"].as_i64().unwrap())
                .unwrap_or((Value::Null, Vec::new()));
            assert_eq!(json!(events), after["events"], "{name}: at the end");
            assert_eq!(snapshot, after["snapshot"], "{name}: the snapshot");
        }
        assert!(held > 50, "{held} events were compared");
    }

    #[test]
    fn what_the_tool_says_of_itself_is_passed_on() {
        let signals = read(
            &[
                INIT,
                r#"{"type":"result","subtype":"success","is_error":false,"result":"done","usage":{"input_tokens":5,"output_tokens":7}}"#,
            ],
            (Some(0), None),
        );
        assert_eq!(signals[0], Signal::Model("m".to_string()));
        assert!(signals.contains(&Signal::Usage {
            tokens_in: 5,
            tokens_out: 7
        }));
    }

    #[test]
    fn what_a_turn_took_in_counts_what_it_read_from_the_cache_and_wrote_to_it() {
        let used = |usage: &str| {
            let result = format!(
                r#"{{"type":"result","subtype":"success","is_error":false,"result":"done","usage":{usage}}}"#
            );
            let signals = read(&[INIT, &result], (Some(0), None));
            let mut said = signals.into_iter().filter_map(|signal| match signal {
                Signal::Usage {
                    tokens_in,
                    tokens_out,
                } => Some((tokens_in, tokens_out)),
                _ => None,
            });
            said.next()
        };
        assert_eq!(
            used(
                r#"{"input_tokens":5,"cache_creation_input_tokens":100,"cache_read_input_tokens":1000,"output_tokens":7}"#
            ),
            Some((1105, 7))
        );
        // How full the conversation is, is what the last answer was made from: not that sum.
        let answer = |usage: &str| {
            format!(
                r#"{{"type":"assistant","message":{{"id":"msg_1","role":"assistant","content":[{{"type":"text","text":"x"}}],"usage":{usage}}}}}"#
            )
        };
        let (first, last) = (
            answer(r#"{"input_tokens":3,"cache_read_input_tokens":900,"output_tokens":9}"#),
            answer(
                r#"{"input_tokens":4,"cache_creation_input_tokens":50,"cache_read_input_tokens":1000}"#,
            ),
        );
        let result = r#"{"type":"result","subtype":"success","is_error":false,"result":"x","usage":{"input_tokens":7,"cache_creation_input_tokens":50,"cache_read_input_tokens":1900,"output_tokens":20}}"#;
        let signals = read(&[INIT, &first, &last, result], (Some(0), None));
        let held: Vec<u64> = signals
            .iter()
            .filter_map(|signal| match signal {
                Signal::Context { tokens } => Some(*tokens),
                _ => None,
            })
            .collect();
        assert_eq!(
            held,
            [903, 1054],
            "each answer says how full it found the conversation"
        );
        assert!(signals.contains(&Signal::Usage {
            tokens_in: 1957,
            tokens_out: 20
        }));
        // What a turn cost is told where the tool says it, to the millionth of a dollar.
        let costing = |said: &str| {
            let result = format!(
                r#"{{"type":"result","subtype":"success","is_error":false,"result":"x","total_cost_usd":{said}}}"#
            );
            let signals = read(&[INIT, &result], (Some(0), None));
            let mut costs = signals.into_iter().filter_map(|signal| match signal {
                Signal::Cost { micros } => Some(micros),
                _ => None,
            });
            costs.next()
        };
        assert_eq!(costing("0.0125"), Some(12_500));
        assert_eq!(costing("0.01234567"), Some(12_346));
        assert_eq!(costing("0"), Some(0));
        assert_eq!(costing("null"), None);
        assert_eq!(costing("-1"), None);
        assert_eq!(costing("\"0.5\""), None);
        // What it does not say counts as none; a turn that says nothing of its use says none.
        assert_eq!(used(r#"{"output_tokens":3}"#), Some((0, 3)));
        assert_eq!(used(r#"{"cache_read_input_tokens":40}"#), Some((40, 0)));
        assert_eq!(used("{}"), None);
        assert_eq!(used("null"), None);
    }

    const INIT: &str = r#"{"type":"system","subtype":"init","session_id":"sess-1","model":"m"}"#;
    const RESULT: &str =
        r#"{"type":"result","subtype":"success","is_error":false,"result":"done"}"#;

    fn stream(piece: &str) -> String {
        format!(r#"{{"type":"stream_event","event":{piece},"parent_tool_use_id":null}}"#)
    }

    #[test]
    fn a_streamed_turn_with_a_tool_call_becomes_replies_a_tool_and_an_end() {
        let lines = [
            INIT.to_string(),
            stream(r#"{"type":"message_start","message":{"id":"msg_1","role":"assistant"}}"#),
            stream(r#"{"type":"content_block_start","index":0,"content_block":{"type":"thinking","thinking":""}}"#),
            stream(r#"{"type":"content_block_delta","index":0,"delta":{"type":"thinking_delta","thinking":"hm"}}"#),
            stream(r#"{"type":"content_block_start","index":1,"content_block":{"type":"text","text":""}}"#),
            stream(r#"{"type":"content_block_delta","index":1,"delta":{"type":"text_delta","text":"I will "}}"#),
            stream(r#"{"type":"content_block_delta","index":1,"delta":{"type":"text_delta","text":"look."}}"#),
            r#"{"type":"assistant","message":{"id":"msg_1","content":[{"type":"text","text":"I will look."}]}}"#.to_string(),
            stream(r#"{"type":"content_block_start","index":2,"content_block":{"type":"tool_use","id":"toolu_1","name":"Bash","input":{}}}"#),
            stream(r#"{"type":"content_block_delta","index":2,"delta":{"type":"input_json_delta","partial_json":"{\"command\""}}"#),
            r#"{"type":"assistant","message":{"id":"msg_1","content":[{"type":"tool_use","id":"toolu_1","name":"Bash","input":{"command":"ls"}}]}}"#.to_string(),
            stream(r#"{"type":"message_stop"}"#),
            r#"{"type":"user","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"toolu_1","content":[{"type":"text","text":"a.txt"}]}]}}"#.to_string(),
            stream(r#"{"type":"message_start","message":{"id":"msg_2","role":"assistant"}}"#),
            stream(r#"{"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}"#),
            stream(r#"{"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"One file."}}"#),
            r#"{"type":"assistant","message":{"id":"msg_2","content":[{"type":"text","text":"One file."}]}}"#.to_string(),
            stream(r#"{"type":"message_stop"}"#),
            RESULT.to_string(),
        ];
        let lines: Vec<&str> = lines.iter().map(String::as_str).collect();
        let signals = read(&lines, (Some(0), None));

        let live: Vec<&str> = signals
            .iter()
            .filter_map(|signal| match signal {
                Signal::Live(live) => Some(live.text.as_str()),
                _ => None,
            })
            .collect();
        assert_eq!(
            live,
            vec!["I will ", "I will look.", "One file."],
            "the reply is shown as it grows, and the whole message is not taken twice"
        );
        assert!(
            signals
                .iter()
                .any(|signal| matches!(signal, Signal::Live(live) if live.id == "r-1")),
            "the growing reply has the id the finished one will have"
        );

        let tool = |status, output: Option<&str>, ended_at_ms| ToolCall {
            origin: clarp_kernel::turn::Origin::User,
            id: "toolu_1".to_string(),
            turn: 7,
            name: "Bash".to_string(),
            input: json!({"command": "ls"}),
            status,
            output: output.map(str::to_string),
            started_at_ms: 1_110,
            ended_at_ms,
            reply: Some("r-1".to_string()),
            ..ToolCall::default()
        };
        // The message is one reply: what it said and the tool it calls. It is told when it
        // is whole, and the tool's use after it.
        assert_eq!(
            durable(&signals),
            vec![
                Signal::Bound {
                    backend_session: "sess-1".to_string()
                },
                Signal::Moved(Activity::Thinking),
                Signal::Moved(Activity::Responding),
                Signal::Moved(Activity::Tool {
                    name: "Bash".to_string()
                }),
                Signal::Reply(Reply {
                    origin: clarp_kernel::turn::Origin::User,
                    id: "r-1".to_string(),
                    turn: 7,
                    text: "I will look.".to_string(),
                    trace_id: None,
                    tools: vec!["toolu_1".to_string()],
                }),
                Signal::Tool(tool(ToolStatus::Running, None, None)),
                Signal::Tool(tool(ToolStatus::Ok, Some("a.txt"), Some(1_130))),
                Signal::Moved(Activity::Thinking),
                Signal::Moved(Activity::Responding),
                Signal::Reply(Reply {
                    origin: clarp_kernel::turn::Origin::User,
                    id: "r-2".to_string(),
                    turn: 7,
                    text: "One file.".to_string(),
                    trace_id: None,
                    ..Reply::default()
                }),
                Signal::Ended(RunOutcome::Completed),
            ]
        );
    }

    /// What a turn put into the conversation, in a line each: a reply with the tools it
    /// calls, a tool's use with the reply it is shown on and how it stands.
    fn pieces(signals: &[Signal]) -> Vec<String> {
        signals
            .iter()
            .filter_map(|signal| match signal {
                Signal::Reply(reply) => {
                    Some(format!("{} {:?} {:?}", reply.id, reply.text, reply.tools))
                }
                Signal::Tool(call) => Some(format!(
                    "{} on {} {:?} {:?}",
                    call.id,
                    call.reply.as_deref().unwrap_or("nothing"),
                    call.status,
                    call.output.as_deref().unwrap_or("")
                )),
                _ => None,
            })
            .collect()
    }

    #[test]
    fn a_message_that_only_calls_tools_is_a_reply_that_says_nothing() {
        // Whole messages, as a tool that does not stream says them: the message is whole
        // when what its calls gave back arrives.
        let signals = read(
            &[
                INIT,
                r#"{"type":"assistant","message":{"id":"msg_1","content":[{"type":"tool_use","id":"toolu_1","name":"Read","input":{"file_path":"/a"}},{"type":"tool_use","id":"toolu_2","name":"Grep","input":{"pattern":"x"}}]}}"#,
                r#"{"type":"user","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"toolu_1","content":"one"},{"type":"tool_result","tool_use_id":"toolu_2","content":[{"type":"text","text":"two"},{"type":"image"},{"type":"text","text":""},{"content":"three"}],"is_error":true}]}}"#,
                r#"{"type":"assistant","message":{"id":"msg_2","content":[{"type":"text","text":"Done."}]}}"#,
                RESULT,
            ],
            (Some(0), None),
        );
        assert_eq!(
            pieces(&signals),
            [
                r#"r-1 "" ["toolu_1", "toolu_2"]"#,
                r#"toolu_1 on r-1 Running """#,
                r#"toolu_2 on r-1 Running """#,
                r#"toolu_1 on r-1 Ok "one""#,
                // What came back in parts is what the parts say, one under the other.
                "toolu_2 on r-1 Error \"two\\nthree\"",
                r#"r-2 "Done." []"#,
            ]
        );
    }

    #[test]
    fn a_reply_is_told_once_and_more_of_its_message_is_another() {
        // What a call gave back arrives before the message that made it has stopped: the
        // message is whole enough to be told. What it goes on with is a reply of its own.
        let lines = [
            INIT.to_string(),
            stream(r#"{"type":"message_start","message":{"id":"msg_1","role":"assistant"}}"#),
            r#"{"type":"assistant","message":{"id":"msg_1","content":[{"type":"tool_use","id":"toolu_1","name":"Bash","input":{"command":"ls"}}]}}"#.to_string(),
            r#"{"type":"user","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"toolu_1","content":"a.txt"}]}}"#.to_string(),
            r#"{"type":"assistant","message":{"id":"msg_1","content":[{"type":"tool_use","id":"toolu_2","name":"Bash","input":{"command":"pwd"}}]}}"#.to_string(),
            stream(r#"{"type":"message_stop"}"#),
        ];
        let lines: Vec<&str> = lines.iter().map(String::as_str).collect();
        // The process goes before the second call returns.
        let signals = read(&lines, (None, Some(9)));
        assert_eq!(
            pieces(&signals),
            [
                r#"r-1 "" ["toolu_1"]"#,
                r#"toolu_1 on r-1 Running """#,
                r#"toolu_1 on r-1 Ok "a.txt""#,
                r#"r-1-2 "" ["toolu_2"]"#,
                r#"toolu_2 on r-1-2 Running """#,
            ]
        );
    }

    #[test]
    fn whole_messages_alone_are_enough() {
        // A tool that does not stream piece by piece still says everything in whole messages.
        let signals = read(
            &[
                INIT,
                r#"{"type":"assistant","message":{"id":"msg_1","content":[{"type":"text","text":"First."}]}}"#,
                r#"{"type":"assistant","message":{"id":"msg_1","content":[{"type":"text","text":"Second."}]}}"#,
                r#"{"type":"assistant","message":{"id":"msg_2","content":[{"type":"text","text":"Third."}]}}"#,
                RESULT,
            ],
            (Some(0), None),
        );
        let replies: Vec<(&str, &str)> = signals
            .iter()
            .filter_map(|signal| match signal {
                Signal::Reply(reply) => Some((reply.id.as_str(), reply.text.as_str())),
                _ => None,
            })
            .collect();
        assert_eq!(
            replies,
            vec![("r-1", "First.\n\nSecond."), ("r-2", "Third.")]
        );
        assert_eq!(signals.last(), Some(&Signal::Ended(RunOutcome::Completed)));
    }

    #[test]
    fn what_is_not_this_agents_reply_is_left_alone() {
        let signals = read(
            &[
                "Warning: something the tool printed that is not an event",
                INIT,
                r#"{"type":"assistant","parent_tool_use_id":"toolu_9","message":{"id":"msg_sub","content":[{"type":"text","text":"a sub-agent speaking"}]}}"#,
                r#"{"type":"stream_event","parent_tool_use_id":"toolu_9","event":{"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"more of it"}}}"#,
                r#"{"type":"something_new","value":1}"#,
                RESULT,
            ],
            (Some(0), None),
        );
        assert_eq!(
            durable(&signals),
            vec![
                Signal::Bound {
                    backend_session: "sess-1".to_string()
                },
                Signal::Ended(RunOutcome::Completed)
            ]
        );
    }

    #[test]
    fn the_ways_a_turn_fails_are_told_apart() {
        let limit = read(
            &[
                INIT,
                r#"{"type":"rate_limit_event","rate_limit_info":{"status":"rejected","rateLimitType":"five_hour"}}"#,
                r#"{"type":"rate_limit_event","rate_limit_info":{"status":"rejected"}}"#,
            ],
            (Some(1), None),
        );
        assert!(
            !limit
                .iter()
                .any(|signal| matches!(signal, Signal::Notice(_) | Signal::Reply(_))),
            "the limit is told by the turn's end, not by a row of its own"
        );
        assert_eq!(
            limit.last(),
            Some(&Signal::Ended(RunOutcome::Failed {
                reason: USAGE_LIMIT.to_string()
            }))
        );

        let crashed = read(&[INIT, "ERR ", "ERR Error: no such model"], (Some(1), None));
        assert_eq!(
            crashed.last(),
            Some(&Signal::Ended(RunOutcome::Failed {
                reason: "Error: no such model".to_string()
            })),
            "a tool that fails says why on its standard error"
        );

        let silent = read(&[], (Some(3), None));
        assert_eq!(
            silent,
            vec![Signal::Ended(RunOutcome::Failed {
                reason: "claude exited with code 3".to_string()
            })]
        );

        let no_result = read(&[INIT], (Some(0), None));
        assert_eq!(
            no_result.last(),
            Some(&Signal::Ended(RunOutcome::Failed {
                reason: "claude ended without a result".to_string()
            }))
        );

        let refused = read(
            &[
                INIT,
                r#"{"type":"result","subtype":"error_during_execution","is_error":true,"result":"API Error: overloaded"}"#,
            ],
            (Some(0), None),
        );
        assert_eq!(
            refused.last(),
            Some(&Signal::Ended(RunOutcome::Failed {
                reason: "API Error: overloaded".to_string()
            }))
        );
    }

    #[test]
    fn what_claude_code_says_went_wrong_is_the_reason_and_not_a_reply() {
        // As recorded on 15 Nov 2026 from a Claude Code whose login had expired.
        let signals = read(
            &[
                INIT,
                r#"{"type":"assistant","isApiErrorMessage":true,"error":"authentication_failed","message":{"id":"e1","model":"<synthetic>","role":"assistant","content":[{"type":"text","text":"Failed to authenticate: OAuth session expired"}]}}"#,
                r#"{"type":"result","subtype":"success","is_error":true,"result":"Failed to authenticate: OAuth session expired"}"#,
                "ERR [claude-code:unrecognized_model]",
            ],
            (Some(1), None),
        );
        assert!(
            !signals
                .iter()
                .any(|signal| matches!(signal, Signal::Reply(_) | Signal::Live(_))),
            "the agent said nothing"
        );
        assert_eq!(
            signals.last(),
            Some(&Signal::Ended(RunOutcome::Failed {
                reason: "Failed to authenticate: OAuth session expired\n[claude-code:unrecognized_model]"
                    .to_string()
            }))
        );
        // When the result says the same as the error, it is said once.
        let once = read(
            &[
                INIT,
                r#"{"type":"assistant","message":{"id":"e1","isApiErrorMessage":true,"content":[{"type":"text","text":"Credit balance is too low"}]}}"#,
                r#"{"type":"result","subtype":"success","is_error":true,"result":"Credit balance is too low"}"#,
            ],
            (Some(0), None),
        );
        assert_eq!(
            once.last(),
            Some(&Signal::Ended(RunOutcome::Failed {
                reason: "Credit balance is too low".to_string()
            }))
        );
    }

    #[test]
    fn a_killed_turn_keeps_what_was_said_and_the_tool_it_had_called() {
        let lines = [
            INIT.to_string(),
            stream(r#"{"type":"message_start","message":{"id":"msg_1"}}"#),
            stream(r#"{"type":"content_block_start","index":0,"content_block":{"type":"text","text":"Half a sen"}}"#),
            r#"{"type":"assistant","message":{"id":"msg_1","content":[{"type":"tool_use","id":"toolu_1","name":"Bash","input":{"command":"sleep 99"}}]}}"#.to_string(),
        ];
        let lines: Vec<&str> = lines.iter().map(String::as_str).collect();
        let signals = durable(&read(&lines, (None, Some(15))));
        let tail = &signals[signals.len() - 3..];
        assert_eq!(
            tail[0],
            Signal::Reply(Reply {
                origin: clarp_kernel::turn::Origin::User,
                id: "r-1".to_string(),
                turn: 7,
                text: "Half a sen".to_string(),
                trace_id: None,
                tools: vec!["toolu_1".to_string()],
            }),
            "what the agent had written is kept, with the tool it had called"
        );
        let on_it = Some("r-1".to_string());
        // What becomes of a tool that never came back follows from how the turn ended:
        // the Host says it, not this reader.
        assert!(
            matches!(&tail[1], Signal::Tool(call) if call.status == ToolStatus::Running && call.reply == on_it),
            "the tool's use begins on the reply"
        );
        assert_eq!(
            tail[2],
            Signal::Ended(RunOutcome::Interrupted {
                reason: "claude was ended by signal 15".to_string()
            })
        );
    }

    #[test]
    fn a_turn_is_started_with_the_message_on_standard_input() {
        let spec = TurnSpec {
            agent_id: "a1".to_string(),
            session: "maximus".to_string(),
            turn: 1,
            cwd: PathBuf::from("/work"),
            prompt: "say \"hi\"\nplease".to_string(),
            backend_session: None,
            new_session: None,
            model: Some("opus".to_string()),
            effort: Some(String::new()),
            identity: None,
            told: None,
        };
        let fresh = Claude::new().launch(&spec);
        assert_eq!(fresh.program, "claude");
        assert!(
            fresh
                .args
                .windows(2)
                .any(|pair| pair == ["--model", "opus"])
        );
        assert!(
            !fresh
                .args
                .iter()
                .any(|arg| arg == "--resume" || arg == "--effort"),
            "what is not set is not passed"
        );
        assert_eq!(
            fresh.env,
            vec![
                (SESSION_ENV.to_string(), "maximus".to_string()),
                (crate::MANAGED_CLAUDE_ENV.into(), "1".into())
            ]
        );
        assert_eq!(fresh.stdin.last(), Some(&b'\n'));
        assert_eq!(
            fresh.stdin.iter().filter(|byte| **byte == b'\n').count(),
            1,
            "the message is one line, whatever it contains"
        );
        let sent: Value = serde_json::from_slice(&fresh.stdin).unwrap();
        assert_eq!(sent["message"]["content"][0]["text"], "say \"hi\"\nplease");
        assert_eq!(
            sent["message"]["content"].as_array().unwrap().len(),
            1,
            "a Host that tells nothing sends the user's words and no more"
        );
        assert!(!fresh.args.iter().any(|arg| arg == "--append-system-prompt"));

        // Who the agent is goes where it holds for the whole conversation; what holds for
        // this turn goes after the user's words, marked as the Host's.
        let told = Claude::new().launch(&TurnSpec {
            identity: Some("You are Maximus. Personality: exact.".to_string()),
            told: Some(told::Turn {
                persona: "Maximus".to_string(),
                spoken: Some(told::Sound {
                    humanness: 0,
                    tags: false,
                    narration: 1,
                }),
                call: Some(
                    "[Group call] You are Maximus, on a voice call with the user.".to_string(),
                ),
            }),
            ..spec.clone()
        });
        let identity = [
            "--append-system-prompt",
            "You are Maximus. Personality: exact.",
        ];
        assert!(told.args.windows(2).any(|pair| pair == identity));
        assert_eq!(
            told.stdin.iter().filter(|byte| **byte == b'\n').count(),
            1,
            "still one line"
        );
        let sent: Value = serde_json::from_slice(&told.stdin).unwrap();
        let content = sent["message"]["content"].as_array().unwrap();
        assert_eq!(content.len(), 2);
        assert_eq!(
            content[0]["text"], "say \"hi\"\nplease",
            "the user's words first"
        );
        let said = content[1]["text"].as_str().unwrap();
        assert!(said.starts_with("<system-reminder>\nYou are running inside Clarp."));
        assert!(said.ends_with("\n</system-reminder>"));
        assert!(said.contains("This turn belongs to Maximus (Clarp session maximus)."));
        assert!(said.contains("<speak>") && said.contains("Humanness: 0/10."));
        assert!(said.contains("Narration level: milestones."));
        // The header of a call the turn is a part of comes after everything else.
        assert!(
            said.ends_with("\n\n[Group call] You are Maximus, on a voice call with the user.\n</system-reminder>"),
            "{said}"
        );
        let silent = Claude::new().launch(&TurnSpec {
            told: Some(told::Turn {
                persona: "Maximus".to_string(),
                spoken: None,
                call: None,
            }),
            ..spec.clone()
        });
        let sent: Value = serde_json::from_slice(&silent.stdin).unwrap();
        let said = sent["message"]["content"][1]["text"].as_str().unwrap();
        assert!(said.contains("This turn belongs to Maximus") && !said.contains("<speak>"));

        let named = Claude::new().launch(&TurnSpec {
            new_session: Some("conv-1".to_string()),
            ..spec.clone()
        });
        assert!(
            named
                .args
                .windows(2)
                .any(|pair| pair == ["--session-id", "conv-1"]),
            "a first turn starts the session under the name the Host chose"
        );
        let resumed = Claude::with_program("/opt/claude").launch(&TurnSpec {
            backend_session: Some("sess-1".to_string()),
            new_session: Some("conv-1".to_string()),
            ..spec
        });
        assert!(
            !resumed.args.iter().any(|arg| arg == "--session-id"),
            "a session that exists is continued, not named again"
        );
        assert_eq!(resumed.program, "/opt/claude");
        assert!(
            resumed
                .args
                .windows(2)
                .any(|pair| pair == ["--resume", "sess-1"])
        );
    }

    #[test]
    fn one_question_is_asked_of_claude_code_outside_every_conversation() {
        let claude = Claude::with_program("/opt/claude");
        let question = crate::Question {
            prompt: "Whom is this for?".to_string(),
            model: Some("sonnet".to_string()),
            effort: Some("low".to_string()),
        };
        let launch = claude.once(&question).unwrap();
        assert_eq!(launch.program, "/opt/claude");
        assert_eq!(
            launch.args,
            [
                "-p",
                "--dangerously-skip-permissions",
                "--no-session-persistence",
                "--strict-mcp-config",
                "--model",
                "sonnet",
                "--effort",
                "low"
            ]
        );
        assert_eq!(launch.stdin, b"Whom is this for?");
        let plain = crate::Question::default();
        assert_eq!(claude.once(&plain).unwrap().args.len(), 4);
        // What it prints is its answer, as it is.
        assert_eq!(
            claude.answered(" {\"a\": 1}\n"),
            Ok(" {\"a\": 1}\n".to_string())
        );
    }
}
