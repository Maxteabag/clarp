//! Both picker entries use the same executable and native session store.
use crate::{
    Backend, ErrorTail, Interpreter, Launch, LiveReply, Question, SESSION_ENV, Signal, TurnSpec,
    shorten,
};
use clarp_kernel::conversation::{Reply, ToolCall, ToolStatus};
use clarp_kernel::live::Glimpse;
use clarp_kernel::turn::{Activity, RunOutcome};
use clarp_kernel::{opencode as rules, python, told};
use clarp_spool::{Entry, Record, ShimEvent};
use serde_json::{Map, Value, json};
use std::collections::{BTreeMap, BTreeSet};

pub struct OpenCode(pub &'static str, pub String);

fn live() -> bool {
    std::env::var("CLARP_OPENCODE_LIVE").as_deref() != Ok("0")
}
fn permissions() -> (String, String) {
    let raw = std::env::var("OPENCODE_PERMISSION").unwrap_or_default();
    (
        "OPENCODE_PERMISSION".into(),
        rules::permissions(&raw).to_string(),
    )
}

impl Backend for OpenCode {
    fn id(&self) -> &'static str {
        self.0
    }
    fn runs_through(&self) -> Option<&'static str> {
        (self.0 == "deepseek").then_some("opencode")
    }
    fn efforts(&self) -> &'static [&'static str] {
        rules::EFFORTS
    }
    fn message_most(&self) -> Option<usize> {
        (!live()).then_some(60 * 1024)
    }
    fn launch(&self, spec: &TurnSpec) -> Launch {
        let session = spec.backend_session.as_deref().unwrap_or("");
        let model = spec.model.as_deref().unwrap_or("");
        let effort = spec.effort.as_deref().unwrap_or("");
        let prompt = spec.told.as_ref().map_or_else(
            || spec.prompt.clone(),
            |t| {
                let head = told::in_call(
                    told::at_head(&t.persona, &spec.session, t.spoken),
                    t.call.as_deref(),
                );
                told::headed(&head, None, &spec.prompt)
            },
        );
        let env = vec![(SESSION_ENV.into(), spec.session.clone()), permissions()];
        if live() {
            return Launch {
                program: "clarp-shim".into(),
                args: vec!["opencode-turn".into(), self.1.clone()],
                prompt_as_file: false,
                before_stop: Some(("SIGUSR1", 3000)),
                env,
                stdin: json!({"model":model,"effort":effort,"prompt":prompt,"session":session})
                    .to_string()
                    .into_bytes(),
            };
        }
        let mut args = rules::command(session, model, effort, true);
        args.push(prompt);
        Launch {
            program: self.1.clone(),
            prompt_as_file: false,
            before_stop: None,
            args,
            env,
            stdin: Vec::new(),
        }
    }
    fn interpreter(&self, turn: u64, name: &str) -> Box<dyn Interpreter> {
        Box::new(Stream {
            turn,
            name: name.into(),
            ..Stream::default()
        })
    }
    fn once(&self, q: &Question) -> Option<Launch> {
        let mut args = rules::command(
            "",
            q.model.as_deref().unwrap_or(""),
            q.effort.as_deref().unwrap_or(""),
            false,
        );
        args.push(q.prompt.clone());
        Some(Launch {
            program: self.1.clone(),
            prompt_as_file: false,
            before_stop: None,
            args,
            env: vec![permissions()],
            stdin: Vec::new(),
        })
    }
    fn answered(&self, s: &str) -> Result<String, String> {
        Ok(rules::answer(s))
    }
}

#[derive(Default)]
struct Stream {
    turn: u64,
    name: String,
    bound: bool,
    ended: bool,
    run_failed: bool,
    /// The model the turn's answers named, once one did.
    model: Option<String>,
    lines: BTreeMap<String, i64>,
    step: u64,
    /// The replies a step has written so far: its words before each of its calls are one.
    part: u64,
    assistant: BTreeSet<String>,
    types: BTreeMap<String, String>,
    texts: BTreeMap<String, String>,
    order: Vec<String>,
    /// What each text part has written into a reply so far, whole. OpenCode tells a text part
    /// whole once more when it ends, and that can come after the call its words announced has
    /// begun (opencode/big-pickle, 25 Nov 2026): those words are written already, and only
    /// words beyond them are new (issue 858).
    filed: BTreeMap<String, String>,
    tools: BTreeMap<String, ToolCall>,
    tokens: Option<(u64, u64)>,
    cost: Option<f64>,
    errors: Vec<String>,
    stderr: ErrorTail,
}

fn word<'a>(v: &'a Value, k: &str) -> &'a str {
    v[k].as_str().unwrap_or("")
}
fn error(e: &Value) -> String {
    if let Some(s) = e.as_str() {
        return s.into();
    }
    for field in [&e["data"]["message"], &e["message"]] {
        if let Some(s) = field.as_str().map(str::trim).filter(|s| !s.is_empty()) {
            return s.into();
        }
    }
    if let Some(n) = e["name"].as_str().filter(|n| !n.is_empty()) {
        return format!("OpenCode {n}");
    }
    if e.is_null() {
        String::new()
    } else {
        python::first(&python::dumps_ascii(e), 300).into()
    }
}

pub(crate) fn tool_name(name: &str) -> &str {
    match name {
        "bash" => "Bash",
        "read" => "Read",
        "edit" => "Edit",
        "write" => "Write",
        "glob" => "Glob",
        "grep" => "Grep",
        "webfetch" => "WebFetch",
        "websearch" => "WebSearch",
        "todowrite" => "TodoWrite",
        "skill" => "Skill",
        "" => "tool",
        other => other,
    }
}
fn input(raw: &Value) -> Value {
    let mut fields = Map::new();
    for (key, v) in raw.as_object().into_iter().flatten() {
        let key = match key.as_str() {
            "filePath" => "file_path",
            "oldString" => "old_string",
            "newString" => "new_string",
            "replaceAll" => "replace_all",
            other => other,
        };
        if !v.is_object() && !v.is_null() {
            fields.insert(key.into(), bounded(v));
        }
    }
    Value::Object(fields)
}
fn bounded(v: &Value) -> Value {
    match v {
        Value::String(s) => json!(clip(s, 400)),
        Value::Array(a) => Value::Array(
            a.iter()
                .take(50)
                .filter(|x| !x.is_null() && !x.is_array())
                .map(|x| {
                    if let Some(fields) = x.as_object() {
                        Value::Object(
                            fields
                                .iter()
                                .filter(|(_, v)| !v.is_array() && !v.is_object() && !v.is_null())
                                .map(|(k, v)| {
                                    (
                                        k.clone(),
                                        v.as_str()
                                            .map_or_else(|| v.clone(), |s| json!(clip(s, 200))),
                                    )
                                })
                                .collect(),
                        )
                    } else {
                        bounded(x)
                    }
                })
                .collect(),
        ),
        value => value.clone(),
    }
}
fn clip(s: &str, n: usize) -> String {
    let head = python::first(s, n);
    if head.len() == s.len() {
        s.into()
    } else {
        format!("{head}…")
    }
}

impl Stream {
    fn row(&self) -> String {
        match self.part {
            0 => format!("r-{}-{}", self.name, self.step),
            part => format!("r-{}-{}-{part}", self.name, self.step),
        }
    }
    fn said(&self) -> String {
        self.order
            .iter()
            .filter_map(|i| self.texts.get(i))
            .map(|s| s.trim())
            .filter(|s| !s.is_empty())
            .collect::<Vec<_>>()
            .join("\n\n")
    }
    fn flush(&mut self, out: &mut Vec<Signal>) {
        self.flush_with(Vec::new(), out);
    }
    /// Writes what the step said so far as a reply, with the calls (`tools`) that follow its
    /// words: a step's words before its call announce the call, as one message of the model
    /// that says something and calls a tool does. A call with no words before it is a reply
    /// too, the row its tool is shown on.
    fn flush_with(&mut self, tools: Vec<String>, out: &mut Vec<Signal>) {
        let text = self.said();
        // The words go into the reply: the part keeps only what it says after them.
        for id in std::mem::take(&mut self.order) {
            let new = self
                .texts
                .get_mut(&id)
                .map(std::mem::take)
                .unwrap_or_default();
            self.filed.entry(id).or_default().push_str(&new);
        }
        if text.is_empty() && tools.is_empty() {
            return;
        }
        let id = self.row();
        if !text.is_empty() {
            out.push(Signal::Glimpse(Glimpse::Done { id: id.clone() }));
        }
        out.push(Signal::Reply(Reply {
            id,
            turn: self.turn,
            text,
            tools,
            ..Reply::default()
        }));
        self.part += 1;
    }
    fn text(&mut self, id: &str, text: &str, delta: bool, out: &mut Vec<Signal>) {
        let s = self.texts.entry(id.into()).or_default();
        if delta {
            s.push_str(text);
        } else {
            *s = text.into();
        }
        // A part whose words a reply holds already: told whole again, it says nothing new, and
        // is no second reply of the same words. Only what it says beyond them is new.
        if let Some(filed) = self.filed.get(id) {
            let whole = if delta {
                format!("{filed}{s}")
            } else {
                s.clone()
            };
            match whole.strip_prefix(filed.as_str()) {
                Some(rest) if rest.trim().is_empty() => {
                    s.clear();
                    return;
                }
                Some(rest) => *s = rest.to_owned(),
                None => {}
            }
        }
        if !self.order.iter().any(|x| x == id) {
            self.order.push(id.into());
        }
        let text = self.said();
        let id = self.row();
        out.push(Signal::Moved(Activity::Responding));
        out.push(Signal::Live(LiveReply {
            id: id.clone(),
            text: text.clone(),
        }));
        out.push(Signal::Glimpse(Glimpse::Message {
            id: id.clone(),
            text,
            row: Some(id),
        }));
    }
    fn tool(&mut self, p: &Value, t: i64, out: &mut Vec<Signal>) {
        let state = &p["state"];
        let status = word(state, "status");
        if status == "pending" {
            return;
        }
        let raw = p["callID"]
            .as_str()
            .filter(|s| !s.is_empty())
            .unwrap_or(word(p, "id"));
        let sid = format!("oc:{raw}");
        let name = tool_name(p["tool"].as_str().unwrap_or(word(p, "name"))).to_string();
        let id = format!("t-{}-{raw}", self.name);
        if !self.tools.contains_key(raw) {
            // The step's words so far go with this call, written before it: what announced a
            // call is never written after it, where it would read as words after work.
            let reply = self.row();
            self.flush_with(vec![id.clone()], out);
            let args = input(&state["input"]);
            let call = ToolCall {
                id: id.clone(),
                turn: self.turn,
                name: name.clone(),
                input: args.clone(),
                status: ToolStatus::Running,
                started_at_ms: t,
                reply: Some(reply),
                ..ToolCall::default()
            };
            out.push(Signal::Glimpse(Glimpse::Tool {
                id: sid.clone(),
                call_id: id.clone(),
                name: name.clone(),
                input: args,
                whole: true,
            }));
            out.push(Signal::Tool(call.clone()));
            out.push(Signal::Moved(Activity::Tool { name }));
            self.tools.insert(raw.into(), call);
        }
        let Some(call) = self.tools.get_mut(raw) else {
            return;
        };
        let output = if matches!(status, "completed" | "error") {
            &state["output"]
        } else {
            &state["metadata"]["output"]
        };
        let (tail, total) = clarp_kernel::live::output_tail(output);
        let seen = self.lines.entry(sid.clone()).or_default();
        if total > *seen {
            let from = tail.len().saturating_sub((total - *seen) as usize);
            out.push(Signal::Glimpse(Glimpse::Output {
                id: sid.clone(),
                lines: tail[from..].to_vec(),
                total,
            }));
            *seen = total;
        }
        if let Some(diff) = state["metadata"]["diff"].as_str().filter(|d| !d.is_empty()) {
            let fields =
                json!({"diff":diff,"path":state["input"]["filePath"].as_str().unwrap_or("")});
            if let Some(diff) = clarp_kernel::live::diff_stats("patch", &fields) {
                out.push(Signal::Glimpse(Glimpse::Patch {
                    id: sid.clone(),
                    fields: json!({"tool":{"diff":diff}}),
                }));
            }
        }
        if matches!(status, "completed" | "error") && call.status == ToolStatus::Running {
            let failed = status == "error";
            let content = state["output"].as_str().unwrap_or(word(state, "error"));
            call.output = Some(shorten(content, crate::TOOL_OUTPUT_KEPT));
            call.ended_at_ms = Some(t);
            call.status = if failed {
                ToolStatus::Error
            } else {
                ToolStatus::Ok
            };
            out.push(Signal::Glimpse(Glimpse::Settled {
                id: sid,
                failed,
                exit_code: state["metadata"]["exit"].as_i64(),
            }));
            out.push(Signal::Tool(call.clone()));
            out.push(Signal::Moved(Activity::Thinking));
        }
    }
    fn usage(&mut self, p: &Value) {
        let tokens = &p["tokens"];
        if tokens["input"].is_number() || tokens["output"].is_number() {
            let old = self.tokens.get_or_insert((0, 0));
            old.0 = old
                .0
                .saturating_add(tokens["input"].as_f64().unwrap_or(0.0) as u64);
            old.1 = old
                .1
                .saturating_add(tokens["output"].as_f64().unwrap_or(0.0) as u64);
        }
        if let Some(cost) = p["cost"].as_f64() {
            *self.cost.get_or_insert(0.0) += cost;
        }
    }
    fn step(&mut self, out: &mut Vec<Signal>) {
        self.flush(out);
        self.step += 1;
        self.part = 0;
        out.push(Signal::Moved(Activity::Thinking));
    }
    fn serve(&mut self, p: &Value, t: i64, out: &mut Vec<Signal>) {
        let kind = word(p, "type");
        let msg = word(p, "messageID");
        if kind == "step-start" {
            self.assistant.insert(msg.into());
        }
        if !self.assistant.contains(msg) {
            return;
        }
        let id = word(p, "id");
        match kind {
            "step-start" => self.step(out),
            "step-finish" => self.usage(p),
            "tool" => self.tool(p, t, out),
            "reasoning" => {
                self.types.insert(id.into(), kind.into());
                let s = self.texts.entry(id.into()).or_default();
                if !word(p, "text").is_empty() {
                    *s = word(p, "text").into();
                }
                out.push(Signal::Moved(Activity::Thinking));
                out.push(Signal::Glimpse(Glimpse::Reasoning {
                    id: format!("oc:{id}"),
                    text: s.clone(),
                }));
                if python::truthy(&p["time"]["end"]) {
                    out.push(Signal::Glimpse(Glimpse::Done {
                        id: format!("oc:{id}"),
                    }));
                }
            }
            "text" if !python::truthy(&p["synthetic"]) && !python::truthy(&p["ignored"]) => {
                self.types.insert(id.into(), kind.into());
                self.text(id, word(p, "text"), false, out);
            }
            _ => {}
        }
    }
    fn line(&mut self, v: &Value, t: i64, out: &mut Vec<Signal>) {
        if self.run_failed {
            return;
        }
        if !self.bound
            && let Some(s) = rules::session(v)
        {
            self.bound = true;
            out.push(Signal::Bound {
                backend_session: s.into(),
            });
        }
        let props = &v["properties"];
        let part = &v["part"];
        match rules::event_kind(v) {
            "message.part.updated" => self.serve(&props["part"], t, out),
            "message.part.delta" if props["field"] == "text" => {
                let id = word(props, "partID");
                let delta = word(props, "delta");
                match self.types.get(id).map(String::as_str) {
                    Some("text") => self.text(id, delta, true, out),
                    Some("reasoning") => {
                        let s = self.texts.entry(id.into()).or_default();
                        s.push_str(delta);
                        out.push(Signal::Glimpse(Glimpse::Reasoning {
                            id: format!("oc:{id}"),
                            text: s.clone(),
                        }));
                    }
                    _ => {}
                }
            }
            // An answer names the model that gave it: as the agent's model is named, its
            // provider and its id (`opencode/big-pickle`).
            "message.updated" if props["info"]["role"] == "assistant" => {
                let (provider, id) = (
                    word(&props["info"], "providerID"),
                    word(&props["info"], "modelID"),
                );
                let model = match (provider, id) {
                    (_, "") => String::new(),
                    ("", id) => id.to_string(),
                    (provider, id) => format!("{provider}/{id}"),
                };
                if !model.is_empty() && self.model.as_deref() != Some(model.as_str()) {
                    self.model = Some(model.clone());
                    out.push(Signal::Model(model));
                }
            }
            "step_start" => self.step(out),
            "step_finish" => self.usage(part),
            "reasoning" => out.push(Signal::Moved(Activity::Thinking)),
            "tool_use" | "tool_start" | "tool.running" => {
                self.tool(if part.is_object() { part } else { v }, t, out)
            }
            // The provider refused the account and OpenCode would retry without end: the
            // turn ends on the provider's words, as a usage limit ends one of another tool.
            "session.status" => {
                if let Some(why) = rules::refused(&props["status"]) {
                    self.errors.push(why);
                }
            }
            "error" | "session.error" => {
                self.run_failed = props.get("error").is_none();
                let e = error(props.get("error").unwrap_or(&v["error"]));
                self.errors.push(if e.is_empty() {
                    v["message"].as_str().unwrap_or("opencode error").into()
                } else {
                    e
                });
            }
            _ => {
                let s = rules::assistant_text(v);
                if !s.is_empty() {
                    self.text(
                        &format!("{}:{}", self.step, self.order.len()),
                        &s,
                        false,
                        out,
                    );
                }
            }
        }
    }
}

impl Interpreter for Stream {
    fn on_record(&mut self, r: &Record, out: &mut Vec<Signal>) {
        if self.ended {
            return;
        }
        match &r.entry {
            Entry::Out(line) => {
                if let Ok(v) = serde_json::from_str(line) {
                    self.line(&v, r.t, out);
                }
            }
            Entry::Err(line) => self.stderr.push(line),
            Entry::Shim(ShimEvent::Exited { code, signal }) => {
                self.ended = true;
                self.flush(out);
                let result = if let Some(sig) = signal {
                    RunOutcome::Interrupted {
                        reason: format!("OpenCode turn killed by signal {sig}"),
                    }
                } else if !self.errors.is_empty() {
                    RunOutcome::Failed {
                        reason: self.errors.join("\n"),
                    }
                } else if *code != Some(0) {
                    RunOutcome::Failed {
                        reason: self.stderr.text().unwrap_or_else(|| {
                            format!("opencode exited rc={}", code.unwrap_or(-1))
                        }),
                    }
                } else {
                    RunOutcome::Completed
                };
                if let Some((tokens_in, tokens_out)) = self.tokens {
                    out.push(Signal::Usage {
                        tokens_in,
                        tokens_out,
                    });
                }
                if let Some(cost) = self.cost {
                    out.push(Signal::TurnCost {
                        micros: (cost * 1e6).round() as u64,
                    });
                }
                out.push(Signal::Ended(result));
            }
            _ => {}
        }
    }
}
