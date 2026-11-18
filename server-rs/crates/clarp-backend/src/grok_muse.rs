//! The Grok and Muse line-oriented backends, one process per turn.
use std::collections::BTreeMap;

use clarp_kernel::conversation::{Reply, ToolCall, ToolStatus};
use clarp_kernel::grok_muse::{Tool, content, event_kind};
use clarp_kernel::live::Glimpse;
use clarp_kernel::told;
use clarp_kernel::turn::{Activity, RunOutcome};
use clarp_spool::{Entry, Record, ShimEvent};
use serde_json::Value;

use crate::{
    Backend, ErrorTail, Interpreter, Launch, LiveReply, Question, SESSION_ENV, Signal, TurnSpec,
    shorten,
};

pub(crate) struct Adapter {
    pub tool: Tool,
    pub program: String,
}

impl Backend for Adapter {
    fn id(&self) -> &'static str {
        self.tool.id()
    }

    fn binds_at_spawn(&self) -> bool {
        true
    }

    fn efforts(&self) -> &'static [&'static str] {
        match self.tool {
            Tool::Grok => &["low", "medium", "high"],
            Tool::Muse => &[
                "none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra",
            ],
        }
    }

    fn launch(&self, spec: &TurnSpec) -> Launch {
        let session = spec.backend_session.as_ref().or(spec.new_session.as_ref());
        let mut args = self.tool.arguments(
            session.map_or("", String::as_str),
            spec.backend_session.is_none(),
            spec.model.as_deref().unwrap_or(""),
            spec.effort.as_deref().unwrap_or(""),
        );
        let prompt = match &spec.told {
            Some(t) => {
                let head = told::at_head(&t.persona, &spec.session, t.spoken);
                told::headed(&told::in_call(head, t.call.as_deref()), None, &spec.prompt)
            }
            None => spec.prompt.clone(),
        };
        let mut stdin = Vec::new();
        if prompt.len() > 64 * 1024 {
            args.extend(["--prompt-file".into(), "/dev/stdin".into()]);
            stdin = prompt.into_bytes();
        } else {
            args.extend([self.tool.prompt_flag().into(), prompt]);
        }
        Launch {
            program: self.program.clone(),
            args,
            stdin,
            env: vec![(SESSION_ENV.into(), spec.session.clone())],
        }
    }

    fn once(&self, q: &Question) -> Option<Launch> {
        let args = self.tool.question(
            q.model.as_deref().unwrap_or(""),
            q.effort.as_deref().unwrap_or(""),
            &q.prompt,
        );
        Some(Launch {
            program: self.program.clone(),
            args,
            stdin: Vec::new(),
            env: Vec::new(),
        })
    }

    fn answered(&self, printed: &str) -> Result<String, String> {
        let answer = self.tool.answer(printed);
        if answer.trim().is_empty() {
            Err(format!("{} returned no answer", self.tool.id()))
        } else {
            Ok(answer)
        }
    }

    fn interpreter(&self, turn: u64, name: &str) -> Box<dyn Interpreter> {
        Box::new(Stream {
            tool: self.tool,
            turn,
            name: name.into(),
            part: 0,
            text: String::new(),
            calls: BTreeMap::new(),
            session: None,
            saw_output: false,
            outcome: None,
            stderr: ErrorTail::default(),
        })
    }
}

struct Stream {
    tool: Tool,
    turn: u64,
    name: String,
    part: usize,
    text: String,
    calls: BTreeMap<String, ToolCall>,
    session: Option<String>,
    saw_output: bool,
    outcome: Option<RunOutcome>,
    stderr: ErrorTail,
}

fn word(v: &Value) -> &str {
    v.as_str().unwrap_or("")
}

/// Counts in both streams use Python's int(value or 0). A bad field
/// abandons the rest of that conversion.
fn tokens(value: &Value) -> Option<u64> {
    if !clarp_kernel::python::truthy(value) {
        return Some(0);
    }
    match value {
        Value::Bool(_) => Some(1),
        Value::Number(n) => n
            .as_u64()
            .or_else(|| n.as_f64().filter(|v| *v >= 0.0).map(|v| v as u64)),
        Value::String(s) => clarp_kernel::python::int(s).and_then(|v| v.try_into().ok()),
        _ => None,
    }
}

impl Stream {
    fn id(&self) -> String {
        format!("{}-reply-{}", self.name, self.part)
    }

    fn flush(&mut self, tools: Vec<String>, out: &mut Vec<Signal>) {
        if self.text.is_empty() && tools.is_empty() {
            return;
        }
        if !self.text.is_empty() {
            out.push(Signal::Glimpse(Glimpse::Done { id: self.id() }));
        }
        out.push(Signal::Reply(Reply {
            id: self.id(),
            turn: self.turn,
            text: std::mem::take(&mut self.text),
            tools,
            ..Reply::default()
        }));
        self.part += 1;
    }

    fn call(&mut self, raw: &str, name: &str, input: Value, t: i64, out: &mut Vec<Signal>) {
        let id = format!("{}-tool-{raw}", self.name);
        let name = clarp_kernel::grok_muse_history::tool_name(self.tool, name);
        let input = clarp_kernel::grok_muse_history::arguments(self.tool, &input);
        let call = ToolCall {
            id: id.clone(),
            turn: self.turn,
            name: name.into(),
            input,
            reply: Some(self.id()),
            started_at_ms: t,
            ..ToolCall::default()
        };
        self.calls.insert(raw.into(), call.clone());
        out.push(Signal::Moved(Activity::Tool { name: name.into() }));
        out.push(Signal::Glimpse(Glimpse::Tool {
            id: id.clone(),
            call_id: id.clone(),
            name: name.to_string(),
            input: call.input.clone(),
            whole: true,
        }));
        out.push(Signal::Tool(call));
        self.flush(vec![id], out);
    }

    fn settle(&mut self, raw: &str, ok: bool, text: String, t: i64, out: &mut Vec<Signal>) {
        if let Some(mut call) = self.calls.remove(raw) {
            call.status = if ok {
                ToolStatus::Ok
            } else {
                ToolStatus::Error
            };
            call.output = Some(shorten(&text, 4000));
            call.ended_at_ms = Some(t);
            out.push(Signal::Glimpse(Glimpse::Returned {
                id: call.id.clone(),
                content: Value::String(text),
                failed: !ok,
                exit_code: None,
            }));
            out.push(Signal::Tool(call));
            if self.tool == Tool::Grok {
                out.push(Signal::Moved(Activity::Thinking));
            }
        }
    }

    fn grok(&mut self, e: &Value, t: i64, out: &mut Vec<Signal>) {
        let raw = word(&e["toolCallId"]);
        match event_kind(e) {
            "turn_started" | "turn.started" | "thought" => {
                out.push(Signal::Moved(Activity::Thinking))
            }
            "usage" | "usage_update" | "end" => {
                let usage = &e["usage"];
                let (tokens_in, tokens_out) = tokens(&usage["input_tokens"])
                    .zip(tokens(&usage["output_tokens"]))
                    .unwrap_or_default();
                if event_kind(e) != "end" || (tokens_in, tokens_out) != (0, 0) {
                    out.push(Signal::Usage {
                        tokens_in,
                        tokens_out,
                    });
                }
            }
            "tool_call" | "tool_started" | "tool_start" | "tool_use" | "item.started" => {
                let name = ["toolName", "title", "name", "tool"]
                    .iter()
                    .find_map(|k| e[*k].as_str())
                    .unwrap_or("tool");
                self.call(raw, name, e["rawInput"].clone(), t, out);
            }
            "tool_call_update"
                if matches!(
                    word(&e["status"]),
                    "completed" | "failed" | "error" | "cancelled"
                ) =>
            {
                let text = e["content"]
                    .as_array()
                    .into_iter()
                    .flatten()
                    .map(|p| content(&p["content"]))
                    .collect::<Vec<_>>()
                    .join("\n");
                self.settle(raw, e["status"] == "completed", text, t, out);
            }
            "error" | "turn.failed" | "turn_failed" => {
                let why = e["error"]["message"]
                    .as_str()
                    .or(e["error"].as_str())
                    .or(e["message"].as_str())
                    .unwrap_or("grok error");
                self.outcome = Some(RunOutcome::Failed { reason: why.into() });
            }
            _ => {}
        }
    }

    fn muse(&mut self, e: &Value, t: i64, out: &mut Vec<Signal>) {
        let p = &e["payload"];
        match word(&e["payload_type"]) {
            "run.lifecycle.started" => out.push(Signal::Moved(Activity::Thinking)),
            "run.model.configured" => out.push(Signal::Model(word(&p["model_id"]).into())),
            "run.usage" => {
                let mut tokens_in: u64 = 0;
                let mut tokens_out: u64 = 0;
                for row in p["models"].as_array().into_iter().flatten() {
                    let Some(input) = tokens(&row["input_tokens"]) else {
                        continue;
                    };
                    tokens_in = tokens_in.saturating_add(input);
                    if let Some(output) = tokens(&row["output_tokens"]) {
                        tokens_out = tokens_out.saturating_add(output);
                    }
                }
                if (tokens_in, tokens_out) != (0, 0) {
                    out.push(Signal::Usage {
                        tokens_in,
                        tokens_out,
                    });
                }
                if p["allowance"].is_object() {
                    out.push(Signal::Limits(p["allowance"].clone()));
                }
            }
            "tool.result" => {
                let facts = &p["correlation_facts"];
                let raw = word(&p["call_id"]);
                let name = facts["tool_name"]
                    .as_str()
                    .or(p["tool_name"].as_str())
                    .unwrap_or("tool");
                self.call(raw, name, Value::Null, t, out);
                self.settle(
                    raw,
                    facts["outcome"] == "success",
                    word(&p["text"]).into(),
                    t,
                    out,
                );
            }
            k if k.starts_with("run.terminal.") => {
                let end = p["terminal"].as_str().unwrap_or(&k[13..]);
                let reason = word(&p["reason"]).trim();
                self.outcome = match end {
                    "cancelled" | "interrupted" => Some(RunOutcome::Stopped { by: "muse".into() }),
                    "failed" | "aborted" => Some(RunOutcome::Failed {
                        reason: if reason.is_empty() {
                            format!("Muse Code {end}")
                        } else {
                            format!("Muse Code {end}: {reason}")
                        },
                    }),
                    _ => None,
                };
            }
            _ => {}
        }
    }
}

impl Interpreter for Stream {
    fn on_record(&mut self, record: &Record, out: &mut Vec<Signal>) {
        match &record.entry {
            Entry::Out(line) if self.outcome.is_none() => {
                self.saw_output |= !line.trim().is_empty();
                let Ok(e) = serde_json::from_str::<Value>(line) else {
                    return;
                };
                match self.tool {
                    Tool::Grok => self.grok(&e, record.t, out),
                    Tool::Muse => self.muse(&e, record.t, out),
                }
                let delta = self.tool.delta(&e);
                if !delta.is_empty() {
                    self.text.push_str(&delta);
                    out.push(Signal::Moved(Activity::Responding));
                    out.push(Signal::Glimpse(Glimpse::Message {
                        id: self.id(),
                        row: Some(self.id()),
                        text: self.text.clone(),
                    }));
                    out.push(Signal::Live(LiveReply {
                        id: self.id(),
                        text: self.text.clone(),
                    }));
                }
            }
            Entry::Shim(ShimEvent::Session { id }) => self.session = Some(id.clone()),
            Entry::Shim(ShimEvent::Spawned { .. }) => {
                if let Some(id) = &self.session {
                    out.push(Signal::Bound {
                        backend_session: id.clone(),
                    });
                }
            }
            Entry::Err(line) => self.stderr.push(line),
            Entry::Shim(ShimEvent::Exited { code, signal }) => {
                self.flush(Vec::new(), out);
                let tool = self.tool.id();
                let outcome = self.outcome.take().unwrap_or_else(|| match (code,signal) {
                    (_,Some(n)) => RunOutcome::Interrupted { reason: format!("{tool} was ended by signal {n}") },
                    (Some(0),_) => RunOutcome::Completed,
                    _ => {
                        let mut reason = self.stderr.text().unwrap_or_else(|| format!("{tool} exited rc={}",code.unwrap_or(-1)));
                        if self.tool == Tool::Muse && !self.saw_output && self.session.as_ref().is_some_and(|id| reason.trim() == format!("session {id} is already in use")) {
                            reason = "muse_session_locked: Muse session is held or could not be reclaimed; unverifiable. Close the other Muse process and resend.".into();
                        }
                        RunOutcome::Failed {reason}
                    }
                });
                out.push(Signal::Ended(outcome));
            }
            _ => {}
        }
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn composed_prompt_byte_bounds_match_the_reference() {
        let fixture: Value = serde_json::from_str(include_str!(
            "../../clarp-kernel/tests/fixtures/backend-grok-vectors.json"
        ))
        .unwrap();
        for tool in [Tool::Grok, Tool::Muse] {
            let adapter = Adapter {
                tool,
                program: "synthetic".into(),
            };
            for row in fixture["bounds"].as_array().unwrap() {
                let prompt = format!(
                    "{}{}",
                    "ß".repeat(row["pairs"].as_u64().unwrap() as usize),
                    row["tail"].as_str().unwrap()
                );
                let spec = TurnSpec {
                    agent_id: "a".into(),
                    session: "my-session".into(),
                    turn: 1,
                    cwd: "/example".into(),
                    prompt: prompt.clone(),
                    backend_session: None,
                    new_session: Some("native".into()),
                    model: None,
                    effort: None,
                    identity: None,
                    told: None,
                };
                let launch = adapter.launch(&spec);
                assert_eq!(launch.program, "synthetic");
                assert_eq!(launch.env, [(SESSION_ENV.into(), "my-session".into())]);
                if row["inline"] == true {
                    assert!(launch.args.ends_with(&[tool.prompt_flag().into(), prompt]));
                    assert!(launch.stdin.is_empty());
                } else {
                    assert!(
                        launch
                            .args
                            .ends_with(&["--prompt-file".into(), "/dev/stdin".into()])
                    );
                    assert_eq!(launch.stdin, prompt.as_bytes());
                }
            }
        }
    }

    #[test]
    fn recorded_streams_follow_the_python_turn_after_every_line() {
        let fixture: Value = serde_json::from_str(include_str!(
            "../../clarp-kernel/tests/fixtures/backend-grok-vectors.json"
        ))
        .unwrap();
        let mut count = 0;
        for story in fixture["streams"].as_array().unwrap() {
            let backend = crate::by_id(story["tool"].as_str().unwrap()).unwrap();
            let mut reader = backend.interpreter(1, "synthetic");
            let (mut saved, mut live, mut model) = (String::new(), String::new(), String::new());
            let mut usage = (0, 0);
            let mut phase = "";
            let mut last = Value::Null;
            for step in story["steps"].as_array().unwrap() {
                last = step.clone();
                let mut signals = Vec::new();
                reader.on_record(
                    &Record {
                        t: 123,
                        entry: Entry::Out(step["event"].to_string()),
                    },
                    &mut signals,
                );
                for signal in signals {
                    match signal {
                        Signal::Moved(activity) => {
                            phase = match activity {
                                Activity::Thinking => "thinking",
                                Activity::Tool { .. } => "tool",
                                Activity::Responding => "speaking",
                                _ => "other",
                            };
                        }
                        Signal::Live(reply) => live = reply.text,
                        Signal::Reply(reply) => {
                            saved.push_str(&reply.text);
                            live.clear();
                        }
                        Signal::Usage {
                            tokens_in,
                            tokens_out,
                        } => usage = (tokens_in, tokens_out),
                        Signal::Model(m) => model = m,
                        _ => {}
                    }
                }
                assert_eq!(step["phase"], phase, "{step}");
                assert_eq!(step["text"], live, "{step}");
                assert_eq!(step["all_text"], format!("{saved}{live}"));
                assert_eq!(step["usage"], json!([usage.0, usage.1]));
                assert_eq!(step["model"], model);
                count += 1;
            }
            let mut signals = Vec::new();
            reader.on_record(
                &Record {
                    t: 124,
                    entry: Entry::Shim(ShimEvent::Exited {
                        code: Some(0),
                        signal: None,
                    }),
                },
                &mut signals,
            );
            let expected = if last["stopped"] == true {
                RunOutcome::Stopped { by: "muse".into() }
            } else if let Some(reason) = last["error"].as_str().filter(|s| !s.is_empty()) {
                RunOutcome::Failed {
                    reason: reason.into(),
                }
            } else {
                RunOutcome::Completed
            };
            assert_eq!(signals.last(), Some(&Signal::Ended(expected)));
        }
        assert_eq!(count, 73);
    }
}
