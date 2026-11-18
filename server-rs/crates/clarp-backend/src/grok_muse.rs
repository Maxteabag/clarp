//! The Grok and Muse line-oriented backends, one process per turn.
use std::collections::BTreeMap;

use clarp_kernel::conversation::{Reply, ToolCall, ToolStatus};
use clarp_kernel::grok_muse::{Tool, content, event_kind};
use clarp_kernel::told;
use clarp_kernel::turn::{Activity, RunOutcome};
use clarp_spool::{Entry, Record, ShimEvent};
use serde_json::Value;

use crate::{Backend, ErrorTail, Interpreter, Launch, LiveReply, Question, SESSION_ENV, Signal, TurnSpec, shorten};

pub(crate) struct Adapter {
    pub tool: Tool,
    pub program: String,
}

impl Backend for Adapter {
    fn id(&self) -> &'static str { self.tool.id() }

    fn efforts(&self) -> &'static [&'static str] {
        match self.tool {
            Tool::Grok => &["low", "medium", "high"],
            Tool::Muse => &["none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"],
        }
    }

    fn launch(&self, spec: &TurnSpec) -> Launch {
        let session = spec.backend_session.as_ref().or(spec.new_session.as_ref());
        let mut args = self.tool.arguments(session.map_or("", String::as_str), spec.backend_session.is_none(),
            spec.model.as_deref().unwrap_or(""), spec.effort.as_deref().unwrap_or(""));
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
        Launch { program: self.program.clone(), args, stdin, env: vec![(SESSION_ENV.into(), spec.session.clone())] }
    }

    fn once(&self, q: &Question) -> Option<Launch> {
        let args = self.tool.question(q.model.as_deref().unwrap_or(""), q.effort.as_deref().unwrap_or(""), &q.prompt);
        Some(Launch { program: self.program.clone(), args, stdin: Vec::new(), env: Vec::new() })
    }

    fn answered(&self, printed: &str) -> Result<String,String> { Ok(self.tool.answer(printed)) }

    fn interpreter(&self, turn: u64, name: &str) -> Box<dyn Interpreter> {
        Box::new(Stream { tool: self.tool, turn, name: name.into(), part: 0, text: String::new(),
            calls: BTreeMap::new(), bound: false, outcome: None, stderr: ErrorTail::default() })
    }
}

struct Stream {
    tool: Tool,
    turn: u64,
    name: String,
    part: usize,
    text: String,
    calls: BTreeMap<String,ToolCall>,
    bound: bool,
    outcome: Option<RunOutcome>,
    stderr: ErrorTail,
}

fn word(v: &Value) -> &str { v.as_str().unwrap_or("") }

impl Stream {
    fn id(&self) -> String { format!("{}-reply-{}", self.name, self.part) }

    fn flush(&mut self, tools: Vec<String>, out: &mut Vec<Signal>) {
        if self.text.is_empty() && tools.is_empty() { return; }
        out.push(Signal::Reply(Reply { id: self.id(), turn: self.turn, text: std::mem::take(&mut self.text), tools, ..Reply::default() }));
        self.part += 1;
    }

    fn call(&mut self, raw: &str, name: &str, input: Value, t: i64, out: &mut Vec<Signal>) {
        let id = format!("{}-tool-{raw}", self.name);
        let call = ToolCall { id: id.clone(), turn: self.turn, name: name.into(), input, reply: Some(self.id()), started_at_ms: t, ..ToolCall::default() };
        self.calls.insert(raw.into(), call.clone());
        out.push(Signal::Moved(Activity::Tool { name: name.into() }));
        out.push(Signal::Tool(call));
        self.flush(vec![id],out);
    }

    fn settle(&mut self, raw: &str, ok: bool, text: String, t: i64, out: &mut Vec<Signal>) {
        if let Some(mut call) = self.calls.remove(raw) {
            call.status = if ok { ToolStatus::Ok } else { ToolStatus::Error };
            call.output = Some(shorten(&text, 4000));
            call.ended_at_ms = Some(t);
            out.push(Signal::Tool(call));
            out.push(Signal::Moved(Activity::Thinking));
        }
    }

    fn grok(&mut self, e: &Value, t: i64, out: &mut Vec<Signal>) {
        let raw = word(&e["toolCallId"]);
        match event_kind(e) {
            "turn_started" | "turn.started" | "thought" => out.push(Signal::Moved(Activity::Thinking)),
            "usage" | "usage_update" | "end" => {
                let usage = &e["usage"];
                let tokens_in = usage["input_tokens"].as_u64().unwrap_or(0);
                let tokens_out = usage["output_tokens"].as_u64().unwrap_or(0);
                if (tokens_in,tokens_out) != (0,0) { out.push(Signal::Usage { tokens_in, tokens_out }); }
            }
            "tool_call" | "tool_started" | "tool_start" | "tool_use" | "item.started" => {
                let name = ["toolName","title","name","tool"].iter().find_map(|k| e[*k].as_str()).unwrap_or("tool");
                self.call(raw,name,e["rawInput"].clone(),t,out);
            }
            "tool_call_update" if matches!(word(&e["status"]), "completed" | "failed" | "error" | "cancelled") => {
                let text = e["content"].as_array().into_iter().flatten().map(|p| content(&p["content"])).collect::<Vec<_>>().join("\n");
                self.settle(raw,e["status"] == "completed",text,t,out);
            }
            "error" | "turn.failed" | "turn_failed" => {
                let why = e["error"]["message"].as_str().or(e["error"].as_str()).or(e["message"].as_str()).unwrap_or("grok error");
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
                    tokens_in = tokens_in.saturating_add(row["input_tokens"].as_u64().unwrap_or(0));
                    tokens_out = tokens_out.saturating_add(row["output_tokens"].as_u64().unwrap_or(0));
                }
                if (tokens_in,tokens_out) != (0,0) { out.push(Signal::Usage { tokens_in, tokens_out }); }
                if p["allowance"].is_object() { out.push(Signal::Limits(p["allowance"].clone())); }
            }
            "tool.result" => {
                let facts = &p["correlation_facts"];
                let raw = word(&p["call_id"]);
                let name = facts["tool_name"].as_str().or(p["tool_name"].as_str()).unwrap_or("tool");
                self.call(raw,name,Value::Null,t,out);
                self.settle(raw,facts["outcome"] == "success",word(&p["text"]).into(),t,out);
            }
            k if k.starts_with("run.terminal.") => {
                let end = p["terminal"].as_str().unwrap_or(&k[13..]);
                let reason = word(&p["reason"]).trim();
                self.outcome = match end {
                    "cancelled" | "interrupted" => Some(RunOutcome::Stopped { by: "muse".into() }),
                    "failed" | "aborted" => Some(RunOutcome::Failed { reason: if reason.is_empty() {format!("Muse Code {end}")} else {format!("Muse Code {end}: {reason}")} }),
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
                let Ok(e) = serde_json::from_str::<Value>(line) else { return };
                if !self.bound {
                    let sid = match self.tool {
                        Tool::Muse => e["stream"]["id"].as_str().filter(|_| e["stream"]["kind"] == "session"),
                        Tool::Grok => [&e,&e["params"],&e["params"]["update"],&e["result"]].into_iter().find_map(|src| ["session_id","sessionId","id"].into_iter().find_map(|k| src[k].as_str().map(str::trim).filter(|s| !s.is_empty()))),
                    };
                    if let Some(sid) = sid {
                        out.push(Signal::Bound { backend_session: sid.into() });
                        self.bound = true;
                    }
                }
                match self.tool {
                    Tool::Grok => self.grok(&e,record.t,out),
                    Tool::Muse => self.muse(&e,record.t,out),
                }
                let delta = self.tool.delta(&e);
                if !delta.is_empty() {
                    self.text.push_str(&delta);
                    out.push(Signal::Moved(Activity::Responding));
                    out.push(Signal::Live(LiveReply { id: self.id(), text: self.text.clone() }));
                }
            }
            Entry::Err(line) => self.stderr.push(line),
            Entry::Shim(ShimEvent::Exited {code,signal}) => {
                self.flush(Vec::new(),out);
                let tool = self.tool.id();
                let outcome = self.outcome.take().unwrap_or_else(|| match (code,signal) {
                    (_,Some(n)) => RunOutcome::Interrupted { reason: format!("{tool} was ended by signal {n}") },
                    (Some(0),_) => RunOutcome::Completed,
                    _ => RunOutcome::Failed {reason: self.stderr.text().unwrap_or_else(|| format!("{tool} exited rc={}",code.unwrap_or(-1)))},
                });
                out.push(Signal::Ended(outcome));
            }
            _ => {}
        }
    }
}
