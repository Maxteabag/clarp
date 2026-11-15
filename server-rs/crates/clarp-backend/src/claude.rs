//! Claude Code, run as `claude -p` with stream-json in both directions.
//!
//! The message goes in as one JSON line on standard input, which is then closed, so the process
//! runs one turn and ends. What it prints is a line of JSON per event: the session it runs in,
//! the reply as it is written piece by piece, each whole message, each tool result, and at the
//! end a result. Everything this file knows is what those lines mean.

use std::collections::{BTreeMap, HashSet};

use clarp_kernel::conversation::{Notice, Reply, ToolCall, ToolStatus};
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
        let message = json!({
            "type": "user",
            "message": {"role": "user", "content": [{"type": "text", "text": spec.prompt}]},
        });
        let mut stdin = message.to_string().into_bytes();
        stdin.push(b'\n');
        Launch {
            program: self.program.clone(),
            args,
            env: vec![(SESSION_ENV.to_string(), spec.session.clone())],
            stdin,
        }
    }

    fn interpreter(&self, turn: u64, name: &str) -> Box<dyn Interpreter> {
        Box::new(ClaudeInterpreter::new(turn, name))
    }
}

/// A message of the agent's while it is being written.
struct Draft {
    /// The provider's id for the message. Empty when the tool did not say.
    id: String,
    /// The text blocks of the message, by their index in it.
    blocks: Vec<(u64, String)>,
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
    /// Every tool call of this turn, as it stands.
    tools: BTreeMap<String, ToolCall>,
    /// The tool calls whose start was reported as an activity.
    announced: HashSet<String>,
    usage_limit: bool,
    /// What Claude Code itself said went wrong, in the place where the agent would have spoken.
    api_error: Option<String>,
    result: Option<ResultFacts>,
    stderr: ErrorTail,
    ended: bool,
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
            tools: BTreeMap::new(),
            announced: HashSet::new(),
            usage_limit: false,
            api_error: None,
            result: None,
            stderr: ErrorTail::default(),
            ended: false,
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
        // The provider's own name for the message, without its `msg_`: what is left is one
        // unbroken id, as every other id of a conversation is.
        match draft.id.strip_prefix("msg_").unwrap_or(&draft.id) {
            "" => format!("r-{}-{}", self.name, self.replies + 1),
            name => format!("r-{name}"),
        }
    }

    /// Shows the message being written as it stands.
    fn live(&self, out: &mut Vec<Signal>) {
        if let Some(draft) = &self.draft {
            let text = draft.text();
            if !text.is_empty() {
                out.push(Signal::Live(LiveReply {
                    id: self.reply_id(draft),
                    text,
                }));
            }
        }
    }

    /// The message being written is whole: it becomes a reply.
    fn settle(&mut self, out: &mut Vec<Signal>) {
        let Some(draft) = self.draft.take() else {
            return;
        };
        let text = draft.text();
        if text.trim().is_empty() {
            return;
        }
        let id = self.reply_id(&draft);
        self.replies += 1;
        out.push(Signal::Reply(Reply {
            id,
            turn: self.turn,
            text,
            trace_id: None,
        }));
    }

    /// The draft for the message with this id, settling the one before it.
    fn draft_for(&mut self, id: &str, out: &mut Vec<Signal>) -> &mut Draft {
        if self.draft.as_ref().is_some_and(|draft| draft.id != id) {
            self.settle(out);
        }
        self.draft.get_or_insert_with(|| Draft {
            id: id.to_string(),
            blocks: Vec::new(),
        })
    }

    /// The text block with this index in the message being written.
    fn block(&mut self, index: u64) -> &mut String {
        let draft = self.draft.get_or_insert_with(|| Draft {
            id: String::new(),
            blocks: Vec::new(),
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
        // What a sub-agent says belongs to the tool call that started it, not to this reply.
        if !text_of(&event["parent_tool_use_id"]).is_empty() {
            return;
        }
        match text_of(&event["type"]) {
            "system" => self.on_system(&event, out),
            "stream_event" => self.on_stream(&event["event"], out),
            "assistant" => self.on_assistant(&event, t, out),
            "user" => self.on_user(&event["message"], t, out),
            "result" => {
                // One process can answer more than one thing: a session resumed with background
                // work behind it answers that first, with a result of its own. The turn is over
                // when the process is, and the last result is the one that counts.
                self.settle(out);
                let tokens = |name: &str| event["usage"][name].as_u64();
                if let (Some(tokens_in), Some(tokens_out)) =
                    (tokens("input_tokens"), tokens("output_tokens"))
                {
                    out.push(Signal::Usage {
                        tokens_in,
                        tokens_out,
                    });
                }
                self.result = Some(ResultFacts {
                    is_error: event["is_error"].as_bool().unwrap_or(false),
                    text: text_of(&event["result"]).to_string(),
                });
            }
            "rate_limit_event" => {
                let status = text_of(&event["rate_limit_info"]["status"]);
                if matches!(status, "rejected" | "blocked") && !self.usage_limit {
                    self.usage_limit = true;
                    out.push(Signal::Notice(Notice {
                        id: format!("n-{}-usage-limit", self.name),
                        turn: Some(self.turn),
                        code: "usage-limit".to_string(),
                        text: USAGE_LIMIT.to_string(),
                    }));
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
            }
            "compact_boundary" => self.moved(Activity::Thinking, out),
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
                        if !text.is_empty() {
                            self.live(out);
                        }
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
                        let call = ToolCall {
                            id: call_id.to_string(),
                            turn: self.turn,
                            name: name.to_string(),
                            input: block["input"].clone(),
                            status: ToolStatus::Running,
                            output: None,
                            started_at_ms: t,
                            ended_at_ms: None,
                        };
                        self.tools.insert(call_id.to_string(), call.clone());
                        out.push(Signal::Tool(call));
                    }
                }
                "text" if !self.streamed.contains(&id) => {
                    let text = text_of(&block["text"]);
                    if text.is_empty() {
                        continue;
                    }
                    self.moved(Activity::Responding, out);
                    let draft = self.draft_for(&id, out);
                    let index = draft.blocks.len() as u64;
                    draft.blocks.push((index, text.to_string()));
                    self.live(out);
                }
                _ => {}
            }
        }
    }

    /// What came back to the agent: the results of its tool calls.
    fn on_user(&mut self, message: &Value, t: i64, out: &mut Vec<Signal>) {
        let Some(content) = message["content"].as_array() else {
            return;
        };
        let mut finished = false;
        for block in content {
            if text_of(&block["type"]) != "tool_result" {
                continue;
            }
            let Some(call) = self.tools.get_mut(text_of(&block["tool_use_id"])) else {
                continue;
            };
            if call.status != ToolStatus::Running {
                continue;
            }
            call.status = if block["is_error"].as_bool().unwrap_or(false) {
                ToolStatus::Error
            } else {
                ToolStatus::Ok
            };
            call.output = Some(shorten(&content_text(&block["content"]), MAX_TOOL_OUTPUT));
            call.ended_at_ms = Some(t);
            out.push(Signal::Tool(call.clone()));
            finished = true;
        }
        if finished {
            self.moved(Activity::Thinking, out);
        }
    }

    fn on_exit(&mut self, code: Option<i32>, signal: Option<i32>, t: i64, out: &mut Vec<Signal>) {
        if self.ended {
            return;
        }
        self.ended = true;
        self.settle(out);
        // A tool that was still running when the process went did not finish.
        for call in self.tools.values_mut() {
            if call.status == ToolStatus::Running {
                call.status = ToolStatus::Error;
                call.output = Some("The turn ended before this tool finished.".to_string());
                call.ended_at_ms = Some(t);
                out.push(Signal::Tool(call.clone()));
            }
        }
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
                self.on_exit(*code, *signal, record.t, out);
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
                    Signal::Live(_) | Signal::Model(_) | Signal::Usage { .. }
                )
            })
            .cloned()
            .collect()
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
            id: "toolu_1".to_string(),
            turn: 7,
            name: "Bash".to_string(),
            input: json!({"command": "ls"}),
            status,
            output: output.map(str::to_string),
            started_at_ms: 1_110,
            ended_at_ms,
        };
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
                Signal::Tool(tool(ToolStatus::Running, None, None)),
                Signal::Reply(Reply {
                    id: "r-1".to_string(),
                    turn: 7,
                    text: "I will look.".to_string(),
                    trace_id: None,
                }),
                Signal::Tool(tool(ToolStatus::Ok, Some("a.txt"), Some(1_130))),
                Signal::Moved(Activity::Thinking),
                Signal::Moved(Activity::Responding),
                Signal::Reply(Reply {
                    id: "r-2".to_string(),
                    turn: 7,
                    text: "One file.".to_string(),
                    trace_id: None,
                }),
                Signal::Ended(RunOutcome::Completed),
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
        assert_eq!(
            limit
                .iter()
                .filter(|signal| matches!(signal, Signal::Notice(notice) if notice.code == "usage-limit"))
                .count(),
            1,
            "the limit is told once"
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
    fn a_killed_turn_keeps_what_was_said_and_closes_what_was_running() {
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
                id: "r-1".to_string(),
                turn: 7,
                text: "Half a sen".to_string(),
                trace_id: None,
            }),
            "what the agent had written is kept"
        );
        assert!(
            matches!(&tail[1], Signal::Tool(call) if call.status == ToolStatus::Error && call.ended_at_ms.is_some()),
            "a tool that never came back is closed"
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
            vec![(SESSION_ENV.to_string(), "maximus".to_string())]
        );
        assert_eq!(fresh.stdin.last(), Some(&b'\n'));
        assert_eq!(
            fresh.stdin.iter().filter(|byte| **byte == b'\n').count(),
            1,
            "the message is one line, whatever it contains"
        );
        let sent: Value = serde_json::from_slice(&fresh.stdin).unwrap();
        assert_eq!(sent["message"]["content"][0]["text"], "say \"hi\"\nplease");

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
}
