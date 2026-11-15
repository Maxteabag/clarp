//! A tool whose every move is scripted: for tests, for the simulator, and for trying a Host
//! without spending a token.
//!
//! The tool prints lines. A line that is a JSON object is an instruction; any other line is
//! text of the reply.
//!
//! | line | means |
//! |---|---|
//! | `{"session": "s1"}` | the tool's own session is `s1` |
//! | `{"say": "text"}` | more of the reply being written |
//! | `{"reply": "text"}` | a whole reply |
//! | `{"tool": "Bash", "id": "t1", "input": {...}}` | a tool call starts |
//! | `{"done": "t1", "output": "...", "error": false}` | a tool call ends |
//! | `{"activity": "thinking"}` | thinking, responding, waiting or compacting |
//! | `{"fail": "why"}` | the turn fails, whatever the exit code |

use std::collections::BTreeMap;

use clarp_kernel::conversation::{Reply, ToolCall, ToolStatus};
use clarp_kernel::turn::{Activity, RunOutcome};
use clarp_spool::{Entry, Record, ShimEvent};
use serde_json::Value;

use crate::{Backend, ErrorTail, Interpreter, Launch, LiveReply, SESSION_ENV, Signal, TurnSpec};

pub struct Scripted {
    program: String,
    args: Vec<String>,
}

impl Scripted {
    /// A tool that reads the message and does what `program` does with it.
    pub fn new(program: impl Into<String>, args: Vec<String>) -> Self {
        Self {
            program: program.into(),
            args,
        }
    }

    /// A tool that answers every message with the message's first line. A message that begins
    /// with `sleep` and a number of seconds is answered after that long, which is how a test
    /// gets a turn that is still running when it wants one.
    pub fn echo() -> Self {
        let script = "IFS= read -r message; \
            case \"$message\" in sleep\\ [0-9]*) sleep \"${message#sleep }\";; esac; \
            printf '%s\\n' \"$message\"";
        Self::new("sh", vec!["-c".to_string(), script.to_string()])
    }
}

impl Backend for Scripted {
    fn id(&self) -> &'static str {
        "scripted"
    }

    fn launch(&self, spec: &TurnSpec) -> Launch {
        let mut stdin = spec.prompt.clone().into_bytes();
        stdin.push(b'\n');
        Launch {
            program: self.program.clone(),
            args: self.args.clone(),
            env: vec![(SESSION_ENV.to_string(), spec.session.clone())],
            stdin,
        }
    }

    fn interpreter(&self, turn: u64, name: &str) -> Box<dyn Interpreter> {
        Box::new(ScriptedInterpreter::new(turn, name))
    }
}

pub struct ScriptedInterpreter {
    turn: u64,
    /// The turn's own name, for the ids of its replies.
    name: String,
    draft: String,
    replies: u32,
    tools: BTreeMap<String, ToolCall>,
    failure: Option<String>,
    stderr: ErrorTail,
    ended: bool,
}

impl ScriptedInterpreter {
    pub fn new(turn: u64, name: &str) -> Self {
        Self {
            turn,
            name: name.to_string(),
            draft: String::new(),
            replies: 0,
            tools: BTreeMap::new(),
            failure: None,
            stderr: ErrorTail::default(),
            ended: false,
        }
    }

    fn reply_id(&self) -> String {
        format!("r-{}-{}", self.name, self.replies + 1)
    }

    fn say(&mut self, text: &str, out: &mut Vec<Signal>) {
        if !self.draft.is_empty() {
            self.draft.push('\n');
        }
        self.draft.push_str(text);
        out.push(Signal::Live(LiveReply {
            id: self.reply_id(),
            text: self.draft.clone(),
        }));
    }

    fn settle(&mut self, out: &mut Vec<Signal>) {
        let text = std::mem::take(&mut self.draft);
        if text.trim().is_empty() {
            return;
        }
        let id = self.reply_id();
        self.replies += 1;
        out.push(Signal::Reply(Reply {
            id,
            turn: self.turn,
            text,
            trace_id: None,
        }));
    }

    fn on_line(&mut self, line: &str, t: i64, out: &mut Vec<Signal>) {
        let instruction = match serde_json::from_str::<Value>(line) {
            Ok(Value::Object(instruction)) => instruction,
            _ => return self.say(line, out),
        };
        let text = |name: &str| instruction.get(name).and_then(Value::as_str);
        if let Some(session) = text("session") {
            out.push(Signal::Bound {
                backend_session: session.to_string(),
            });
        }
        if let Some(more) = text("say") {
            self.say(more, out);
        }
        if let Some(whole) = text("reply") {
            self.settle(out);
            self.draft = whole.to_string();
            self.settle(out);
        }
        if let Some(name) = text("tool") {
            self.settle(out);
            let id = text("id").unwrap_or(name).to_string();
            let call = ToolCall {
                id: id.clone(),
                turn: self.turn,
                name: name.to_string(),
                input: instruction.get("input").cloned().unwrap_or(Value::Null),
                status: ToolStatus::Running,
                output: None,
                started_at_ms: t,
                ended_at_ms: None,
            };
            self.tools.insert(id, call.clone());
            out.push(Signal::Moved(Activity::Tool {
                name: name.to_string(),
            }));
            out.push(Signal::Tool(call));
        }
        if let Some(call) = text("done").and_then(|id| self.tools.get_mut(id)) {
            let failed = instruction
                .get("error")
                .and_then(Value::as_bool)
                .unwrap_or(false);
            call.status = if failed {
                ToolStatus::Error
            } else {
                ToolStatus::Ok
            };
            call.output = Some(text("output").unwrap_or("").to_string());
            call.ended_at_ms = Some(t);
            out.push(Signal::Tool(call.clone()));
            out.push(Signal::Moved(Activity::Thinking));
        }
        match text("activity") {
            Some("thinking") => out.push(Signal::Moved(Activity::Thinking)),
            Some("responding") => out.push(Signal::Moved(Activity::Responding)),
            Some("waiting") => out.push(Signal::Moved(Activity::Waiting)),
            Some("compacting") => out.push(Signal::Moved(Activity::Compacting)),
            _ => {}
        }
        if let Some(why) = text("fail") {
            self.failure = Some(why.to_string());
        }
    }
}

impl Interpreter for ScriptedInterpreter {
    fn on_record(&mut self, record: &Record, out: &mut Vec<Signal>) {
        match &record.entry {
            Entry::Out(line) => self.on_line(line, record.t, out),
            Entry::Err(line) => self.stderr.push(line),
            Entry::Shim(ShimEvent::Exited { code, signal }) if !self.ended => {
                self.ended = true;
                self.settle(out);
                let outcome = match (self.failure.take(), signal, code) {
                    (Some(reason), _, _) => RunOutcome::Failed { reason },
                    (None, Some(signal), _) => RunOutcome::Interrupted {
                        reason: format!("the tool was ended by signal {signal}"),
                    },
                    (None, None, Some(0)) => RunOutcome::Completed,
                    (None, None, code) => RunOutcome::Failed {
                        reason: self.stderr.text().unwrap_or_else(|| {
                            format!("the tool exited with code {}", code.unwrap_or(-1))
                        }),
                    },
                };
                out.push(Signal::Ended(outcome));
            }
            Entry::Shim(_) | Entry::Hook { .. } => {}
        }
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use super::*;

    fn read(lines: &[&str], code: Option<i32>, signal: Option<i32>) -> Vec<Signal> {
        let mut interpreter = ScriptedInterpreter::new(2, "2");
        let mut out = Vec::new();
        for (n, line) in lines.iter().enumerate() {
            let entry = Entry::Out((*line).to_string());
            interpreter.on_record(
                &Record {
                    t: n as i64 + 1,
                    entry,
                },
                &mut out,
            );
        }
        let gone = Entry::Shim(ShimEvent::Exited { code, signal });
        interpreter.on_record(
            &Record {
                t: 100,
                entry: gone,
            },
            &mut out,
        );
        out
    }

    #[test]
    fn a_script_becomes_the_turn_it_describes() {
        let signals = read(
            &[
                r#"{"session": "s1"}"#,
                "plain text is the reply",
                r#"{"say": "and so is this"}"#,
                r#"{"tool": "Bash", "id": "t1", "input": {"command": "ls"}}"#,
                r#"{"done": "t1", "output": "a.txt"}"#,
                r#"{"reply": "One file."}"#,
            ],
            Some(0),
            None,
        );
        let durable: Vec<Signal> = signals
            .into_iter()
            .filter(|signal| !matches!(signal, Signal::Live(_)))
            .collect();
        assert_eq!(durable.len(), 8);
        assert_eq!(
            durable[0],
            Signal::Bound {
                backend_session: "s1".to_string()
            }
        );
        assert_eq!(
            durable[1],
            Signal::Reply(Reply {
                id: "r-2-1".to_string(),
                turn: 2,
                text: "plain text is the reply\nand so is this".to_string(),
                trace_id: None,
            })
        );
        assert!(matches!(&durable[3], Signal::Tool(call) if call.status == ToolStatus::Running));
        assert!(
            matches!(&durable[4], Signal::Tool(call) if call.status == ToolStatus::Ok && call.output.as_deref() == Some("a.txt"))
        );
        assert_eq!(
            durable[6],
            Signal::Reply(Reply {
                id: "r-2-2".to_string(),
                turn: 2,
                text: "One file.".to_string(),
                trace_id: None,
            })
        );
        assert_eq!(durable[7], Signal::Ended(RunOutcome::Completed));
    }

    #[test]
    fn a_script_can_fail_and_can_be_killed() {
        assert_eq!(
            read(&[r#"{"fail": "the model said no"}"#], Some(0), None),
            vec![Signal::Ended(RunOutcome::Failed {
                reason: "the model said no".to_string()
            })]
        );
        assert_eq!(
            read(&[], Some(2), None),
            vec![Signal::Ended(RunOutcome::Failed {
                reason: "the tool exited with code 2".to_string()
            })]
        );
        assert_eq!(
            read(&["half"], None, Some(9)).last(),
            Some(&Signal::Ended(RunOutcome::Interrupted {
                reason: "the tool was ended by signal 9".to_string()
            }))
        );
    }

    #[test]
    fn the_echo_tool_is_given_the_message_on_standard_input() {
        let launch = Scripted::echo().launch(&TurnSpec {
            agent_id: "a1".to_string(),
            session: "maximus".to_string(),
            turn: 1,
            cwd: std::path::PathBuf::from("/work"),
            prompt: "hello there".to_string(),
            backend_session: None,
            new_session: None,
            model: None,
            effort: None,
            identity: None,
        });
        assert_eq!(launch.program, "sh");
        assert_eq!(launch.stdin, b"hello there\n");
    }
}
