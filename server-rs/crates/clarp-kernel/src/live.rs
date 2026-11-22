//! Live items: an agent's open turn as items that appear, grow and settle in place.
//!
//! An app that shows an agent at work does not ask again and again what happened. It is sent
//! small operations (an item appeared, its text grew, a tool printed lines, it settled, the
//! status line changed, the turn ended) and changes the view it holds of the turn by them.
//! `docs/live-items.md` in the repository is the contract.
//!
//! Three things are here, none with I/O or a clock of its own:
//!
//! - [`View`], the reducer every client runs over the events and over a snapshot. The Host
//!   runs the same one, so what it answers to `GET /live` is by construction what a client
//!   that saw every event holds.
//! - [`Hub`], one agent's side of it on the Host. What is seen of a turn becomes events,
//!   numbered one by one; growth of a text or of a tool's output is held back for a moment,
//!   so that a fast stream is a few events a second and not hundreds.
//! - [`Glimpse`], what a backend saw of a running turn, in words every backend can say it in.
//!
//! All of it is the Python Host's (`server/lib/live_projection.py`, `live_core.py`,
//! `live_claude.py`, `tool_output.py`), held to it by `tests/fixtures/live-vectors.json`,
//! which `server-rs/tools/live-vectors.py` writes: the recorded streams of the contract, and
//! what the Python hub sent for many scripted turns.
//!
//! What lasts of a turn is in the agent's log. Nothing here is kept: a Host that starts
//! again builds the view of a running turn from that turn's own records, under a new epoch,
//! and a client that sees the epoch change asks for the snapshot.

use std::collections::HashMap;

use clarp_speech::listen::{is_space, strip};
use serde_json::{Map, Value, json};

use crate::{difflib, python, tools};

/// How many of a tool's last lines an item holds.
pub const OUTPUT_TAIL_LINES: usize = 50;
/// What a client must do when it missed something: ask for the snapshot.
pub const FETCH_LIVE: &str = "fetch_live";
/// How long the growth of a text is held back before it is sent, in milliseconds.
pub const TEXT_PACE_MS: i64 = 100;
/// How long a tool's new lines are held back.
pub const OUTPUT_PACE_MS: i64 = 250;
/// No line of a tool's output is shown longer than this.
const LINE_SIGNS: usize = 400;
/// A reasoning item's headline is no longer than this.
const TITLE_SIGNS: usize = 80;
/// How much of what a tool was given an item shows: this many fields, none longer than
/// [`LINE_SIGNS`].
const PREVIEW_FIELDS: usize = 8;
/// How many lines of an edit an item shows.
const DIFF_LINES: usize = 40;

type Fields = Map<String, Value>;

/// Whether Python takes a value for true: nothing, zero, and what is empty are not.
pub fn truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(flag) => *flag,
        Value::String(text) => !text.is_empty(),
        Value::Number(number) => number.as_f64() != Some(0.0),
        Value::Array(items) => !items.is_empty(),
        Value::Object(fields) => !fields.is_empty(),
    }
}

fn text_of(value: &Value) -> &str {
    value.as_str().unwrap_or("")
}

fn whole_of(value: &Value) -> i64 {
    value.as_i64().unwrap_or(0)
}

fn idle() -> Value {
    json!({"state": "idle"})
}

/// Whether an item is still to settle.
fn unsettled(item: &Value) -> bool {
    matches!(text_of(&item["status"]), "pending" | "running")
}

/// Fields that are absent stay, `null` clears, objects merge, other values replace.
fn merge_patch(target: &mut Fields, patch: &Fields) {
    for (key, value) in patch {
        match (target.get_mut(key), value) {
            (Some(Value::Object(held)), Value::Object(given)) => merge_patch(held, given),
            _ => {
                target.insert(key.clone(), value.clone());
            }
        }
    }
}

/// Adds lines to the end of a tool item's output and keeps the last of them.
fn append_output(item: &mut Fields, lines: &[Value], total_lines: i64) {
    let tool = item.entry("tool").or_insert_with(|| json!({}));
    if !tool.is_object() {
        *tool = json!({});
    }
    let Some(tool) = tool.as_object_mut() else {
        return;
    };
    if !tool.get("output").is_some_and(Value::is_object) {
        tool.insert(
            "output".to_string(),
            json!({"tail": [], "total_lines": 0, "truncated": false, "exit_code": null}),
        );
    }
    let Some(output) = tool.get_mut("output").and_then(Value::as_object_mut) else {
        return;
    };
    let mut tail: Vec<Value> = output
        .get("tail")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    tail.extend(lines.iter().cloned());
    let keep_from = tail.len().saturating_sub(OUTPUT_TAIL_LINES);
    let tail = tail.split_off(keep_from);
    let held = i64::try_from(tail.len()).unwrap_or(i64::MAX);
    output.insert("tail".to_string(), Value::Array(tail));
    output.insert("total_lines".to_string(), json!(total_lines));
    output.insert("truncated".to_string(), json!(total_lines > held));
}

/// One conversation's live state, built from snapshots and events: the reference reducer.
#[derive(Debug, Clone, PartialEq)]
pub struct View {
    epoch: Value,
    lseq: Option<i64>,
    activity: Value,
    turn: Option<Fields>,
    /// By id, in the order they were first seen.
    items: Fields,
    awaiting_snapshot: bool,
}

impl Default for View {
    fn default() -> Self {
        Self {
            epoch: Value::Null,
            lseq: None,
            activity: idle(),
            turn: None,
            items: Fields::new(),
            awaiting_snapshot: false,
        }
    }
}

impl View {
    pub fn new() -> Self {
        Self::default()
    }

    /// The number of the last event applied. `None` before the first snapshot.
    pub fn lseq(&self) -> Option<i64> {
        self.lseq
    }

    /// The status line.
    pub fn activity(&self) -> &Value {
        &self.activity
    }

    /// The open turn, or the last one.
    pub fn turn(&self) -> Option<&Fields> {
        self.turn.as_ref()
    }

    /// Whether a snapshot was asked for and has not come.
    pub fn awaiting_snapshot(&self) -> bool {
        self.awaiting_snapshot
    }

    pub fn item(&self, id: &str) -> Option<&Value> {
        self.items.get(id)
    }

    /// The items in the order they are shown in.
    pub fn items(&self) -> Vec<&Value> {
        let mut items: Vec<&Value> = self.items.values().collect();
        items.sort_by_key(|item| whole_of(&item["ordinal"]));
        items
    }

    /// Replaces everything held with what the Host answered to `GET /live`.
    pub fn apply_snapshot(&mut self, snapshot: &Value) {
        self.epoch = snapshot["epoch"].clone();
        self.lseq = Some(whole_of(&snapshot["lseq"]));
        self.activity = if truthy(&snapshot["activity"]) {
            snapshot["activity"].clone()
        } else {
            idle()
        };
        self.turn = snapshot["turn"]
            .as_object()
            .filter(|turn| !turn.is_empty())
            .cloned();
        self.items = Fields::new();
        for item in snapshot["items"].as_array().into_iter().flatten() {
            if let Some(id) = item["id"].as_str() {
                self.items.insert(id.to_string(), item.clone());
            }
        }
        self.awaiting_snapshot = false;
    }

    /// Applies one `live` event. Answers with what the client must do besides: nothing, or
    /// ask for the snapshot ([`FETCH_LIVE`]), after which events are left alone until it came.
    pub fn apply_event(&mut self, event: &Value) -> Vec<&'static str> {
        if self.awaiting_snapshot {
            return Vec::new();
        }
        let lseq = whole_of(&event["lseq"]);
        let Some(held) = self.lseq else {
            return self.need_snapshot();
        };
        if event["epoch"] != self.epoch {
            return self.need_snapshot();
        }
        if lseq <= held {
            return Vec::new();
        }
        if lseq != held + 1 {
            return self.need_snapshot();
        }
        let mut staged = self.clone();
        for op in event["ops"].as_array().into_iter().flatten() {
            if !staged.apply_op(op) {
                return self.need_snapshot();
            }
        }
        staged.lseq = Some(lseq);
        *self = staged;
        Vec::new()
    }

    fn need_snapshot(&mut self) -> Vec<&'static str> {
        self.awaiting_snapshot = true;
        vec![FETCH_LIVE]
    }

    /// Applies one operation. `false`: it does not fit what is held, so something was missed.
    fn apply_op(&mut self, op: &Value) -> bool {
        let name = text_of(&op["op"]);
        match name {
            "status" => {
                self.activity = if truthy(&op["activity"]) {
                    op["activity"].clone()
                } else {
                    idle()
                };
                return true;
            }
            "turn" => {
                let turn = op["turn"]
                    .as_object()
                    .filter(|turn| !turn.is_empty())
                    .cloned();
                let another = match (&self.turn, &turn) {
                    (Some(held), Some(new)) => {
                        held.get("turn_id").unwrap_or(&Value::Null)
                            != new.get("turn_id").unwrap_or(&Value::Null)
                    }
                    _ => false,
                };
                if another {
                    // A new turn: the rows of the last one are in the conversation now.
                    self.items = Fields::new();
                }
                self.turn = turn;
                return true;
            }
            "upsert" | "append" | "done" => {}
            // What is not known is left alone.
            _ => return true,
        }
        let Some(id) = op["id"].as_str() else {
            return false;
        };
        let rev = op["rev"].as_i64();
        match self.items.get(id) {
            None => {
                if name != "upsert" {
                    return false;
                }
                self.items.insert(
                    id.to_string(),
                    json!({"id": id, "kind": op["kind"], "rev": 0}),
                );
            }
            Some(item) => {
                if rev != Some(whole_of(&item["rev"]) + 1) {
                    return false;
                }
            }
        }
        let Some(item) = self.items.get_mut(id).and_then(Value::as_object_mut) else {
            return false;
        };
        match name {
            "upsert" => {
                if let Some(patch) = op["item"].as_object() {
                    merge_patch(item, patch);
                }
            }
            "append" => match text_of(&op["field"]) {
                "text" => {
                    let grown = format!(
                        "{}{}",
                        item.get("text").map_or("", text_of),
                        text_of(&op["chunk"])
                    );
                    item.insert("text".to_string(), Value::String(grown));
                }
                "tool.output" => {
                    let lines = op["lines"].as_array().cloned().unwrap_or_default();
                    append_output(item, &lines, whole_of(&op["total_lines"]));
                }
                _ => {}
            },
            _ => {
                item.insert("status".to_string(), op["status"].clone());
                for key in ["started_at_ms", "ended_at_ms"] {
                    if let Some(value) = op.get(key) {
                        item.insert(key.to_string(), value.clone());
                    }
                }
                if let Some(patch) = op["item"].as_object() {
                    merge_patch(item, patch);
                }
            }
        }
        let rev = rev.unwrap_or_else(|| item.get("rev").map_or(0, whole_of) + 1);
        item.insert("rev".to_string(), json!(rev));
        true
    }
}

/// A tool call as it is first shown, or shown again with more known of it.
#[derive(Debug, Clone, Copy)]
pub struct ToolStart<'a> {
    pub name: &'a str,
    /// The provider's own id of the call.
    pub call_id: &'a str,
    pub category: &'a str,
    pub label: &'a str,
    pub command: Option<&'a str>,
    /// The short fields of what the tool was given. `None`: not known yet.
    pub input_preview: Option<&'a Fields>,
}

/// What a backend saw of a running turn. The same for every backend: what each reads in its
/// own tool's output becomes these, and the hub makes the items of them.
#[derive(Debug, Clone, PartialEq)]
pub enum Glimpse {
    /// A message of the agent, as it stands now. `row` is the row of the conversation the
    /// text is written into, said when the message is first seen.
    Message {
        id: String,
        text: String,
        row: Option<String>,
    },
    /// The summary of what the model thinks, as it stands now.
    Reasoning {
        id: String,
        text: String,
    },
    /// A tool call: first by its name alone, and when what it was given is whole, with that.
    Tool {
        id: String,
        call_id: String,
        name: String,
        input: Value,
        whole: bool,
    },
    Output {
        id: String,
        lines: Vec<String>,
        total: i64,
    },
    Patch {
        id: String,
        fields: Value,
    },
    /// A call whose output was already followed to its last line.
    Settled {
        id: String,
        failed: bool,
        exit_code: Option<i64>,
    },
    /// A tool returned. `exit_code` is the code its command ended with, for a tool that
    /// says one; without one, a tool that failed is told as 1 and one that did not as 0.
    Returned {
        id: String,
        content: Value,
        failed: bool,
        exit_code: Option<i64>,
    },
    /// The agent's plan for the turn, whole: each step with its text and where it stands
    /// (`pending`, `in_progress`, `completed`). `prefix` is what the ids of this backend's
    /// items begin with.
    Plan {
        prefix: String,
        steps: Value,
    },
    /// A message or a summary is whole.
    Done {
        id: String,
    },
    /// The context is being made smaller, or that is over. `prefix` is what the ids of this
    /// backend's items begin with.
    Compacting {
        prefix: String,
        over: bool,
    },
}

/// One agent's live items on the Host.
#[derive(Debug, Clone)]
pub struct Hub {
    /// Names this run of the Host: a client that sees another one asks for the snapshot.
    epoch: String,
    agent_id: String,
    session: String,
    /// The conversation the view is of. Empty before the first turn.
    conv: String,
    begun: bool,
    view: View,
    /// Growth that is held back: of texts, and of tools' output with the count of all lines.
    pending_text: HashMap<String, String>,
    pending_output: HashMap<String, (Vec<String>, i64)>,
    /// The items with growth held back, in the order it came.
    order: Vec<String>,
    /// When what is held back is sent at the latest.
    due_ms: Option<i64>,
    ordinal: i64,
    /// The group a tool that looks at things joins: the first of the run it is in.
    explore_group: Option<String>,
    /// Why the turn waits for an account, while it does.
    account_wait: Option<String>,
}

impl Hub {
    pub fn new(epoch: &str, agent_id: &str, session: &str) -> Self {
        Self {
            epoch: epoch.to_string(),
            agent_id: agent_id.to_string(),
            session: session.to_string(),
            conv: String::new(),
            begun: false,
            view: View::new(),
            pending_text: HashMap::new(),
            pending_output: HashMap::new(),
            order: Vec::new(),
            due_ms: None,
            ordinal: 0,
            explore_group: None,
            account_wait: None,
        }
    }

    // --- reads ---

    /// When [`Hub::fire`] is to be called at the latest: something is held back until then.
    pub fn due_ms(&self) -> Option<i64> {
        self.due_ms
    }

    pub fn has_open_turn(&self) -> bool {
        self.current_turn_id().is_some()
    }

    /// The running turn's id.
    pub fn current_turn_id(&self) -> Option<&str> {
        let turn = self.view.turn.as_ref().filter(|_| self.begun)?;
        (text_of(turn.get("status").unwrap_or(&Value::Null)) == "running")
            .then(|| text_of(turn.get("turn_id").unwrap_or(&Value::Null)))
    }

    pub fn has_item(&self, id: &str) -> bool {
        self.begun && self.view.items.contains_key(id)
    }

    /// The status line as it stands, for a list of agents. `None` before the first turn.
    pub fn activity(&self) -> Option<&Value> {
        self.begun.then_some(&self.view.activity)
    }

    /// Everything held, as `GET /live` answers it, and the events that sending what was held
    /// back made. `None` before the first turn: there is no conversation to show.
    pub fn snapshot(&mut self, now: i64) -> Option<(Value, Vec<Value>)> {
        if !self.begun {
            return None;
        }
        let mut out = Vec::new();
        self.flush(now, &mut out);
        let items: Vec<Value> = self.view.items().into_iter().cloned().collect();
        let snapshot = json!({
            "conv": self.conv, "session": self.session, "agent_id": self.agent_id,
            "epoch": self.epoch, "lseq": self.view.lseq.unwrap_or(0), "server_now_ms": now,
            "activity": self.view.activity,
            "turn": self.view.turn.clone().map_or(Value::Null, Value::Object),
            "items": items,
        });
        Some((snapshot, out))
    }

    // --- turns ---

    /// A turn begins. A turn that never settled is ended first, interrupted. The same turn
    /// begun again while it runs changes nothing. `audience` is whom it is for
    /// (`coordination::AGENTS` for a turn that answers another agent, else `USER`), as the
    /// Host decided it at the turn's message.
    pub fn begin_turn(
        &mut self,
        session: &str,
        conv: &str,
        turn_id: &str,
        started_at_ms: Option<i64>,
        audience: &str,
        now: i64,
    ) -> Vec<Value> {
        let mut out = Vec::new();
        if !self.begun || self.conv != conv {
            // Another conversation: its events count from one again.
            let epoch = self.epoch.clone();
            let agent_id = self.agent_id.clone();
            *self = Self::new(&epoch, &agent_id, session);
            self.view.apply_snapshot(&json!({
                "epoch": self.epoch, "lseq": 0, "activity": idle(), "turn": null, "items": [],
            }));
            self.conv = conv.to_string();
            self.begun = true;
        }
        if !session.is_empty() {
            self.session = session.to_string();
        }
        let running = self.current_turn_id().map(str::to_string);
        if running.as_deref() == Some(turn_id) {
            return out;
        }
        if running.is_some() {
            self.end(("interrupted", None), now, &mut out);
        }
        self.flush(now, &mut out);
        self.explore_group = None;
        self.account_wait = None;
        let started = started_at_ms.filter(|at| *at != 0).unwrap_or(now);
        let turn = json!({
            "turn_id": turn_id, "status": "running", "started_at_ms": started,
            "ended_at_ms": null, "worked_ms": null, "tool_count": 0, "audience": audience,
        });
        let op = json!({"op": "turn", "conv": self.conv, "turn": turn});
        self.emit(vec![op], (true, ""), now, &mut out);
        out
    }

    /// A user stepped into a running coordination turn. Keep its identity and state intact.
    pub fn set_turn_audience(&mut self, audience: &str, now: i64) -> Vec<Value> {
        let mut out = Vec::new();
        if self.has_open_turn()
            && let Some(mut turn) = self.view.turn.clone()
            && turn.get("audience").and_then(Value::as_str) != Some(audience)
        {
            turn.insert("audience".to_owned(), json!(audience));
            let op = json!({"op": "turn", "conv": self.conv, "turn": turn});
            self.emit(vec![op], (false, ""), now, &mut out);
        }
        out
    }

    /// The turn ended: `completed`, `failed` or `interrupted`. What still ran is settled
    /// with it. A turn that failed says why (`error`: reason, message, the provider's words).
    pub fn end_turn(&mut self, status: &str, error: Option<&Value>, now: i64) -> Vec<Value> {
        let mut out = Vec::new();
        self.end((status, error), now, &mut out);
        out
    }

    fn end(&mut self, how: (&str, Option<&Value>), now: i64, out: &mut Vec<Value>) {
        let (status, error) = how;
        if !self.open(None) {
            return;
        }
        self.flush(now, out);
        let settle = if status == "completed" {
            "completed"
        } else {
            "interrupted"
        };
        let mut ops: Vec<Value> = self
            .view
            .items()
            .into_iter()
            .filter(|item| unsettled(item))
            .map(|item| self.done_op(item, settle, now, None))
            .collect();
        let held = self.view.turn.clone().unwrap_or_default();
        let started = held
            .get("started_at_ms")
            .map(whole_of)
            .filter(|at| *at != 0)
            .unwrap_or(now);
        let mut turn: Fields = held.into_iter().filter(|(key, _)| key != "error").collect();
        turn.insert("status".to_string(), json!(status));
        turn.insert("ended_at_ms".to_string(), json!(now));
        turn.insert("worked_ms".to_string(), json!(now - started));
        if let Some(error) = error.filter(|error| status == "failed" && truthy(error)) {
            turn.insert("error".to_string(), error.clone());
        }
        ops.push(json!({"op": "turn", "conv": self.conv, "turn": turn}));
        self.emit(ops, (true, status), now, out);
    }

    /// The turn waits for an account that can run it (`message` says so), or no longer.
    pub fn account_recovery(&mut self, message: Option<&str>, now: i64) -> Vec<Value> {
        let mut out = Vec::new();
        if !self.open(None) || self.account_wait.as_deref() == message {
            return out;
        }
        self.flush(now, &mut out);
        self.account_wait = message.map(str::to_string);
        self.emit(Vec::new(), (true, ""), now, &mut out);
        out
    }

    // --- items ---

    /// What a backend saw, made into items. `turn_id` is the turn that it saw it of: what a
    /// turn that is over still says is left out, never given to the turn after it.
    pub fn show(&mut self, glimpse: &Glimpse, turn_id: Option<&str>, now: i64) -> Vec<Value> {
        match glimpse {
            Glimpse::Message { id, text, row } => {
                let phase = row.as_ref().map(|_| "final");
                self.message_text(id, text, (phase, row.as_deref()), turn_id, now)
            }
            Glimpse::Reasoning { id, text } => self.reasoning_text(id, text, turn_id, now),
            Glimpse::Tool {
                id,
                call_id,
                name,
                input,
                whole,
            } => {
                let described = tools::describe(name, input);
                let shown = whole.then(|| preview(input));
                let start = ToolStart {
                    name,
                    call_id,
                    category: described.category,
                    label: &described.label,
                    command: described.command.as_deref(),
                    input_preview: shown.as_ref(),
                };
                let mut out = self.tool_start(id, &start, "running", turn_id, now);
                if let Some(diff) = whole.then(|| diff_stats(name, input)).flatten() {
                    let fields = json!({"tool": {"diff": diff}});
                    out.extend(self.patch(id, &fields, turn_id, now));
                }
                out
            }
            Glimpse::Output { id, lines, total } => {
                self.tool_output(id, lines, *total, turn_id, now);
                Vec::new()
            }
            Glimpse::Patch { id, fields } => self.patch(id, fields, turn_id, now),
            Glimpse::Settled {
                id,
                failed,
                exit_code,
            } => self.end_tool(id, *failed, *exit_code, turn_id, now),
            Glimpse::Returned {
                id,
                content,
                failed,
                exit_code,
            } => {
                let mut out = Vec::new();
                if !self.has_item(id) {
                    return out;
                }
                let (tail, total) = output_tail(content);
                if !tail.is_empty() {
                    self.tool_output(id, &tail, total, turn_id, now);
                }
                out.extend(self.end_tool(id, *failed, *exit_code, turn_id, now));
                out
            }
            Glimpse::Done { id } => self.done(id, "completed", None, turn_id, now),
            Glimpse::Plan { prefix, steps } => {
                // One plan to a turn, replaced whole each time it is told.
                let held = self.view.turn.as_ref().and_then(|turn| turn.get("turn_id"));
                let id = format!("{prefix}:{}:plan", held.map_or("", text_of));
                let fields = json!({"plan": {"steps": steps}});
                self.item(&id, "plan", &fields, turn_id, now)
            }
            Glimpse::Compacting { prefix, over } => {
                let held = self.view.turn.as_ref().and_then(|turn| turn.get("turn_id"));
                let id = format!("{prefix}:{}:compaction", held.map_or("", text_of));
                if *over {
                    self.done(&id, "completed", None, turn_id, now)
                } else {
                    self.item(
                        &id,
                        "compaction",
                        &json!({"status": "running"}),
                        turn_id,
                        now,
                    )
                }
            }
        }
    }

    /// A message as it stands now. `first` is how it is made when it is new: its phase
    /// (`final` unless said otherwise) and the row of the conversation it is written into.
    pub fn message_text(
        &mut self,
        id: &str,
        text: &str,
        first: (Option<&str>, Option<&str>),
        turn_id: Option<&str>,
        now: i64,
    ) -> Vec<Value> {
        let (phase, row) = first;
        let made = json!({"phase": phase.filter(|phase| !phase.is_empty()).unwrap_or("final"), "row_id": row});
        let mut out = Vec::new();
        self.grow_text((id, "message"), text, &made, turn_id, now, &mut out);
        out
    }

    /// The summary of the model's thinking as it stands now. Its headline is told when it
    /// has one, and again when it changes.
    pub fn reasoning_text(
        &mut self,
        id: &str,
        text: &str,
        turn_id: Option<&str>,
        now: i64,
    ) -> Vec<Value> {
        let mut out = Vec::new();
        self.grow_text(
            (id, "reasoning"),
            text,
            &json!({"title": null}),
            turn_id,
            now,
            &mut out,
        );
        if !self.open(turn_id) {
            return out;
        }
        let Some(item) = self.view.items.get(id) else {
            return out;
        };
        let full = format!(
            "{}{}",
            text_of(&item["text"]),
            self.pending_text.get(id).map_or("", String::as_str)
        );
        let Some(title) = reasoning_title(&full) else {
            return out;
        };
        if item["title"].as_str() != Some(title.as_str()) {
            self.flush(now, &mut out);
            let op = self.upsert_op(id, "reasoning", json!({"title": title}));
            self.emit(vec![op], (true, ""), now, &mut out);
        }
        out
    }

    /// A tool call is shown, or shown again with more known of it. Tools that look at things
    /// (reading, listing, searching) one after another are one group.
    pub fn tool_start(
        &mut self,
        id: &str,
        start: &ToolStart<'_>,
        status: &str,
        turn_id: Option<&str>,
        now: i64,
    ) -> Vec<Value> {
        let mut out = Vec::new();
        if !self.open(turn_id) {
            return out;
        }
        let group = if matches!(start.category, "read" | "list" | "search") {
            Some(
                self.explore_group
                    .get_or_insert_with(|| format!("explore:{id}"))
                    .clone(),
            )
        } else {
            self.explore_group = None;
            None
        };
        self.flush(now, &mut out);
        let known = self
            .view
            .items
            .get(id)
            .map(|item| item["tool"]["group"].clone());
        let mut tool = json!({
            "name": start.name, "call_id": start.call_id, "category": start.category,
            "group": group, "label": start.label, "command": start.command,
            "input_preview": start.input_preview.cloned().unwrap_or_default(),
        });
        let mut ops = Vec::new();
        match known {
            Some(group) => {
                tool["group"] = group;
                ops.push(self.upsert_op(id, "tool", json!({"status": status, "tool": tool})));
            }
            None => {
                tool["explain"] = Value::Null;
                tool["output"] = Value::Null;
                tool["diff"] = Value::Null;
                // What was said before a tool call was narration, not the answer.
                let said: Vec<String> = self
                    .view
                    .items()
                    .into_iter()
                    .filter(|item| {
                        text_of(&item["kind"]) == "message" && text_of(&item["phase"]) == "final"
                    })
                    .map(|item| text_of(&item["id"]).to_string())
                    .collect();
                for message in said.iter().rev() {
                    ops.push(self.upsert_op(message, "message", json!({"phase": "commentary"})));
                }
                ops.push(self.new_op(id, "tool", json!({"status": status, "tool": tool}), now));
                if let Some(held) = self.view.turn.clone().filter(|turn| !turn.is_empty()) {
                    let count = held.get("tool_count").map_or(0, whole_of) + 1;
                    let mut turn = held;
                    turn.insert("tool_count".to_string(), json!(count));
                    ops.push(json!({"op": "turn", "conv": self.conv, "turn": turn}));
                }
            }
        }
        self.emit(ops, (true, ""), now, &mut out);
        out
    }

    fn end_tool(
        &mut self,
        id: &str,
        failed: bool,
        exit_code: Option<i64>,
        turn_id: Option<&str>,
        now: i64,
    ) -> Vec<Value> {
        let status = if failed { "failed" } else { "completed" };
        let patch = json!({"tool":{"output":{"exit_code":exit_code.unwrap_or(i64::from(failed))}}});
        self.done(id, status, Some(&patch), turn_id, now)
    }

    /// A tool printed lines. `total_lines` counts every line it printed so far. Nothing is
    /// sent at once: the lines are held back and go out with [`Hub::fire`] or with the next
    /// thing that happens.
    pub fn tool_output(
        &mut self,
        id: &str,
        lines: &[String],
        total_lines: i64,
        turn_id: Option<&str>,
        now: i64,
    ) {
        if !self.open(turn_id) || !self.view.items.contains_key(id) {
            return;
        }
        let (mut held, _) = self.pending_output.remove(id).unwrap_or_default();
        held.extend(
            lines
                .iter()
                .map(|line| python::first(line, LINE_SIGNS).to_string()),
        );
        let keep_from = held.len().saturating_sub(OUTPUT_TAIL_LINES);
        let held = held.split_off(keep_from);
        self.pending_output
            .insert(id.to_string(), (held, total_lines));
        self.remember(id);
        self.arm(OUTPUT_PACE_MS, now);
    }

    /// Fields are merged into an item: a diff, the steps of a plan, an explanation.
    pub fn patch(
        &mut self,
        id: &str,
        fields: &Value,
        turn_id: Option<&str>,
        now: i64,
    ) -> Vec<Value> {
        let mut out = Vec::new();
        if !self.open(turn_id) || !self.view.items.contains_key(id) {
            return out;
        }
        self.flush(now, &mut out);
        let kind = self.kind_of(id);
        let op = self.upsert_op(id, &kind, fields.clone());
        self.emit(vec![op], (false, ""), now, &mut out);
        out
    }

    /// An item of any kind is made, or changed when it is there: a plan, a diff, a compaction.
    pub fn item(
        &mut self,
        id: &str,
        kind: &str,
        fields: &Value,
        turn_id: Option<&str>,
        now: i64,
    ) -> Vec<Value> {
        let mut out = Vec::new();
        if !self.open(turn_id) {
            return out;
        }
        self.flush(now, &mut out);
        let op = if self.view.items.contains_key(id) {
            self.upsert_op(id, kind, fields.clone())
        } else {
            self.new_op(id, kind, fields.clone(), now)
        };
        self.emit(vec![op], (true, ""), now, &mut out);
        out
    }

    /// An item settled. One that settled before stays as it settled.
    pub fn done(
        &mut self,
        id: &str,
        status: &str,
        patch: Option<&Value>,
        turn_id: Option<&str>,
        now: i64,
    ) -> Vec<Value> {
        let mut out = Vec::new();
        if !self.open(turn_id) || !self.view.items.get(id).is_some_and(unsettled) {
            return out;
        }
        self.flush(now, &mut out);
        let Some(item) = self.view.items.get(id) else {
            return out;
        };
        let op = self.done_op(item, status, now, patch);
        self.emit(vec![op], (true, ""), now, &mut out);
        out
    }

    /// Everything that still runs is settled, and the turn goes on: an attempt of the turn
    /// ended and another will follow, which shows its own items.
    pub fn settle(&mut self, status: &str, now: i64) -> Vec<Value> {
        let mut out = Vec::new();
        if self.current_turn_id().is_none() {
            return out;
        }
        self.flush(now, &mut out);
        let ops: Vec<Value> = self
            .view
            .items()
            .into_iter()
            .filter(|item| unsettled(item))
            .map(|item| self.done_op(item, status, now, None))
            .collect();
        if !ops.is_empty() {
            self.emit(ops, (true, ""), now, &mut out);
        }
        out
    }

    /// The moment [`Hub::due_ms`] named has come: what was held back is sent.
    pub fn fire(&mut self, now: i64) -> Vec<Value> {
        let mut out = Vec::new();
        self.flush(now, &mut out);
        out
    }

    // --- inside ---

    /// Whether there is a turn to add to. With the id of the turn that something was seen
    /// of: only while that very turn runs.
    fn open(&self, turn_id: Option<&str>) -> bool {
        let Some(turn) = self.view.turn.as_ref().filter(|_| self.begun) else {
            return false;
        };
        match turn_id {
            None => true,
            Some(turn_id) => {
                text_of(turn.get("turn_id").unwrap_or(&Value::Null)) == turn_id
                    && text_of(turn.get("status").unwrap_or(&Value::Null)) == "running"
            }
        }
    }

    fn kind_of(&self, id: &str) -> String {
        self.view
            .items
            .get(id)
            .map_or("", |item| text_of(&item["kind"]))
            .to_string()
    }

    fn remember(&mut self, id: &str) {
        if !self.order.iter().any(|held| held == id) {
            self.order.push(id.to_string());
        }
    }

    fn arm(&mut self, pace_ms: i64, now: i64) {
        if self.due_ms.is_none() {
            self.due_ms = Some(now + pace_ms);
        }
    }

    fn grow_text(
        &mut self,
        which: (&str, &str),
        text: &str,
        made: &Value,
        turn_id: Option<&str>,
        now: i64,
        out: &mut Vec<Value>,
    ) {
        let (id, kind) = which;
        if !self.open(turn_id) {
            return;
        }
        let Some(item) = self.view.items.get(id) else {
            self.flush(now, out);
            // A message, or thinking, ends a run of tools that look at things.
            self.explore_group = None;
            let mut fields = made.clone();
            fields["text"] = json!(text);
            let op = self.new_op(id, kind, fields, now);
            self.emit(vec![op], (true, ""), now, out);
            return;
        };
        if !unsettled(item) {
            return;
        }
        let known = format!(
            "{}{}",
            text_of(&item["text"]),
            self.pending_text.get(id).map_or("", String::as_str)
        );
        if text == known {
            return;
        }
        if let Some(more) = text.strip_prefix(known.as_str()) {
            self.pending_text
                .entry(id.to_string())
                .or_default()
                .push_str(more);
            self.remember(id);
            self.arm(TEXT_PACE_MS, now);
            return;
        }
        // The provider wrote the text anew: it is replaced whole.
        self.flush(now, out);
        let op = self.upsert_op(id, kind, json!({"text": text}));
        self.emit(vec![op], (false, ""), now, out);
    }

    /// Sends what was held back, in the order it came.
    fn flush(&mut self, now: i64, out: &mut Vec<Value>) {
        self.due_ms = None;
        if self.order.is_empty() {
            return;
        }
        let mut ops = Vec::new();
        for id in std::mem::take(&mut self.order) {
            let Some(item) = self.view.items.get(&id) else {
                continue;
            };
            let kind = item["kind"].clone();
            let mut rev = whole_of(&item["rev"]);
            if let Some(chunk) = self
                .pending_text
                .remove(&id)
                .filter(|chunk| !chunk.is_empty())
            {
                rev += 1;
                ops.push(json!({
                    "op": "append", "conv": self.conv, "id": id, "kind": kind, "rev": rev,
                    "field": "text", "chunk": chunk,
                }));
            }
            if let Some((lines, total)) = self.pending_output.remove(&id) {
                rev += 1;
                ops.push(json!({
                    "op": "append", "conv": self.conv, "id": id, "kind": kind, "rev": rev,
                    "field": "tool.output", "lines": lines, "total_lines": total,
                }));
            }
        }
        self.pending_text.clear();
        self.pending_output.clear();
        if !ops.is_empty() {
            self.emit(ops, (true, ""), now, out);
        }
    }

    fn new_op(&mut self, id: &str, kind: &str, fields: Value, now: i64) -> Value {
        self.ordinal += 1;
        let turn_id = self
            .view
            .turn
            .as_ref()
            .and_then(|turn| turn.get("turn_id"))
            .filter(|turn_id| truthy(turn_id))
            .cloned()
            .unwrap_or_else(|| json!(""));
        let mut item = json!({
            "id": id, "conv": self.conv, "turn_id": turn_id, "kind": kind, "status": "running",
            "ordinal": self.ordinal, "started_at_ms": now, "ended_at_ms": null,
        });
        if let (Some(item), Some(fields)) = (item.as_object_mut(), fields.as_object()) {
            for (key, value) in fields {
                item.insert(key.clone(), value.clone());
            }
        }
        json!({"op": "upsert", "conv": self.conv, "id": id, "kind": kind, "rev": 1, "item": item})
    }

    fn upsert_op(&self, id: &str, kind: &str, fields: Value) -> Value {
        let rev = self
            .view
            .items
            .get(id)
            .map_or(0, |item| whole_of(&item["rev"]))
            + 1;
        json!({"op": "upsert", "conv": self.conv, "id": id, "kind": kind, "rev": rev, "item": fields})
    }

    fn done_op(&self, item: &Value, status: &str, now: i64, patch: Option<&Value>) -> Value {
        let started = Some(whole_of(&item["started_at_ms"]))
            .filter(|at| *at != 0)
            .unwrap_or(now);
        let mut op = json!({
            "op": "done", "conv": self.conv, "id": item["id"], "kind": item["kind"],
            "rev": whole_of(&item["rev"]) + 1, "status": status,
            "started_at_ms": started, "ended_at_ms": now,
        });
        if let Some(patch) = patch.filter(|patch| truthy(patch)) {
            op["item"] = patch.clone();
        }
        op
    }

    /// Sends operations as one event, with the status line after them when it changed
    /// (`status_after`, or always when the turn `ended`). What the event says is applied to
    /// the view here, by the reducer the clients run.
    fn emit(&mut self, mut ops: Vec<Value>, status: (bool, &str), now: i64, out: &mut Vec<Value>) {
        let (status_after, ended) = status;
        let mut next = self.view.clone();
        if !ops.iter().all(|op| next.apply_op(op)) {
            // An operation the reducer does not take is a fault of this hub. It is not sent:
            // a client could do nothing with it but ask for a snapshot that says the same.
            return;
        }
        if status_after || !ended.is_empty() {
            let activity = self.activity_of(&next, ended, now);
            if changed(&self.view.activity, &activity) {
                let op = json!({"op": "status", "conv": self.conv, "activity": activity});
                next.apply_op(&op);
                ops.push(op);
            }
        }
        let lseq = self.view.lseq.unwrap_or(0) + 1;
        next.lseq = Some(lseq);
        self.view = next;
        out.push(json!({
            "type": "live", "agent_id": self.agent_id, "session": self.session,
            "conv": self.conv, "epoch": self.epoch, "lseq": lseq, "server_now_ms": now,
            "ops": ops,
        }));
    }

    /// The status line that follows from a view: what the turn does now.
    fn activity_of(&self, view: &View, ended: &str, now: i64) -> Value {
        let nothing = Fields::new();
        let turn = view.turn.as_ref().unwrap_or(&nothing);
        let of_turn = |key: &str| turn.get(key).cloned().unwrap_or(Value::Null);
        let mut activity = json!({
            "tool": null, "running_tools": 0, "headline": null, "item_id": null,
            "since_ms": now, "turn_id": of_turn("turn_id"),
            "turn_started_ms": of_turn("started_at_ms"),
        });
        let status = of_turn("status");
        if !ended.is_empty() || !matches!(status.as_str(), None | Some("running")) {
            let last = match ended {
                "" => status
                    .as_str()
                    .filter(|status| !status.is_empty())
                    .unwrap_or("completed"),
                ended => ended,
            };
            let interrupted = matches!(last, "interrupted" | "failed");
            activity["state"] = json!(if interrupted { "interrupted" } else { "idle" });
            activity["turn_started_ms"] = Value::Null;
            if interrupted {
                let error = of_turn("error");
                let why = &error["message"];
                activity["headline"] = if truthy(why) {
                    json!(text_of(why))
                } else {
                    json!("Interrupted")
                };
            }
            return activity;
        }
        if let Some(wait) = &self.account_wait {
            activity["state"] = json!("limited");
            activity["headline"] = json!(wait);
            activity["since_ms"] = json!(previous_since(&self.view.activity, "limited", now));
            return activity;
        }
        let running: Vec<&Value> = view
            .items()
            .into_iter()
            .filter(|item| unsettled(item))
            .collect();
        let since = |item: &Value| {
            if truthy(&item["started_at_ms"]) {
                item["started_at_ms"].clone()
            } else {
                json!(now)
            }
        };
        let tools: Vec<&Value> = running
            .iter()
            .copied()
            .filter(|item| text_of(&item["kind"]) == "tool")
            .collect();
        if let Some(newest) = tools.last() {
            let tool = &newest["tool"];
            activity["state"] = json!("tool");
            activity["running_tools"] = json!(tools.len());
            activity["item_id"] = newest["id"].clone();
            activity["since_ms"] = since(newest);
            activity["headline"] = json!(tools::headline(
                text_of(&tool["name"]),
                text_of(&tool["category"]),
                text_of(&tool["label"])
            ));
            activity["tool"] = json!({
                "name": tool["name"], "call_id": tool["call_id"], "label": tool["label"],
                "category": tool["category"], "item_id": newest["id"],
                "started_at_ms": newest["started_at_ms"],
            });
            return activity;
        }
        for (kind, state) in [
            ("compaction", "compacting"),
            ("message", "responding"),
            ("reasoning", "thinking"),
        ] {
            let Some(item) = running
                .iter()
                .copied()
                .rfind(|item| text_of(&item["kind"]) == kind)
            else {
                continue;
            };
            activity["state"] = json!(state);
            activity["item_id"] = item["id"].clone();
            activity["since_ms"] = since(item);
            activity["headline"] = match state {
                "compacting" => json!("Compacting"),
                "responding" => json!("Responding"),
                _ if truthy(&item["title"]) => {
                    json!(format!("Thinking: {}", text_of(&item["title"])))
                }
                _ => json!("Thinking"),
            };
            return activity;
        }
        activity["state"] = json!("thinking");
        activity["headline"] = json!("Thinking");
        activity["since_ms"] = json!(previous_since(&self.view.activity, "thinking", now));
        activity
    }
}

/// What `GET /live` answers for an agent of which nothing is shown: an idle, empty view.
/// `names` are its conversation and session, `whose` the agent and this run of the Host.
pub fn empty_snapshot(names: (&str, &str), whose: (&str, &str), now: i64) -> Value {
    let ((conv, session), (agent_id, epoch)) = (names, whose);
    json!({
        "conv": conv, "session": session, "agent_id": agent_id, "epoch": epoch, "lseq": 0,
        "server_now_ms": now,
        "activity": {
            "state": "idle", "tool": null, "running_tools": 0, "headline": null,
            "item_id": null, "since_ms": null, "turn_id": null, "turn_started_ms": null,
        },
        "turn": null, "items": [],
    })
}

/// Since when a state has held: since before, when the status line already said it.
fn previous_since(activity: &Value, state: &str, now: i64) -> i64 {
    if text_of(&activity["state"]) == state && truthy(&activity["since_ms"]) {
        return whole_of(&activity["since_ms"]);
    }
    now
}

/// Whether a status line says something else than the one before it. Since when is not
/// something else by itself.
fn changed(old: &Value, new: &Value) -> bool {
    let (Some(old), Some(new)) = (
        old.as_object().filter(|old| !old.is_empty()),
        new.as_object(),
    ) else {
        return true;
    };
    let differs = |key: &String| {
        key != "since_ms"
            && old.get(key).unwrap_or(&Value::Null) != new.get(key).unwrap_or(&Value::Null)
    };
    old.keys().any(differs) || new.keys().any(differs)
}

/// A one-line headline of what the model thinks: a first line in bold or a heading, else
/// the first sentence. Eighty signs at most.
pub fn reasoning_title(text: &str) -> Option<String> {
    let text = strip(text);
    let first = python::splitlines(text).first().copied()?;
    let title = match bold_title(first).or_else(|| heading_title(first)) {
        Some(title) => strip(title),
        None => strip(first_sentence(first)),
    };
    let cut = python::first(title, TITLE_SIGNS).trim_end_matches(is_space);
    (!cut.is_empty()).then(|| cut.to_string())
}

/// `^\s*\*\*(.+?)\*\*`: what stands between the first two pairs of stars of a line that
/// begins with one.
fn bold_title(line: &str) -> Option<&str> {
    let rest = line.trim_start_matches(is_space).strip_prefix("**")?;
    // At least one sign, which is not a line feed, and as few as there can be.
    let mut signs = rest.char_indices();
    let (_, first) = signs.next()?;
    if first == '\n' {
        return None;
    }
    for (at, sign) in signs {
        if rest[at..].starts_with("**") {
            return Some(&rest[..at]);
        }
        if sign == '\n' {
            return None;
        }
    }
    None
}

/// `^\s*#+\s*(.+)$`: what follows the hashes of a heading. A pattern gives back what it took
/// when it must: a line of hashes alone has its last hash for a title, and hashes with only
/// space after them the last of that space.
fn heading_title(line: &str) -> Option<&str> {
    let line = line.trim_start_matches(is_space);
    let after = line.trim_start_matches('#');
    let hashes = line.len() - after.len();
    if hashes == 0 {
        return None;
    }
    let title = after.trim_start_matches(is_space);
    if !title.is_empty() {
        return Some(title);
    }
    match after.char_indices().next_back() {
        Some((at, _)) => Some(&after[at..]),
        None if hashes >= 2 => Some(&line[hashes - 1..]),
        None => None,
    }
}

/// A line up to the first space that follows the end of a sentence.
fn first_sentence(line: &str) -> &str {
    let mut before = None;
    for (at, sign) in line.char_indices() {
        if is_space(sign) && matches!(before, Some('.' | '!' | '?')) {
            return &line[..at];
        }
        before = Some(sign);
    }
    line
}

/// The short fields of what a tool was given: texts, numbers and yes-or-no, eight at most,
/// none longer than four hundred signs.
pub fn preview(input: &Value) -> Fields {
    let mut shown = Fields::new();
    for (key, value) in input.as_object().into_iter().flatten() {
        let short = match value {
            Value::String(text) => text.chars().count() <= LINE_SIGNS,
            Value::Bool(_) | Value::Number(_) => true,
            _ => false,
        };
        if short {
            shown.insert(key.clone(), value.clone());
        }
        if shown.len() >= PREVIEW_FIELDS {
            break;
        }
    }
    shown
}

/// The last lines of what a tool returned, and how many lines there were in all. What a
/// tool returns is a text, or parts of which each has one.
pub fn output_tail(content: &Value) -> (Vec<String>, i64) {
    let joined;
    let text = match content {
        Value::String(text) => text.as_str(),
        Value::Array(parts) => {
            let texts: Vec<String> = parts
                .iter()
                .map(|part| match part {
                    Value::Object(part) => match part.get("text") {
                        Some(Value::String(text)) => text.clone(),
                        Some(other) if truthy(other) => python::repr(other),
                        _ => String::new(),
                    },
                    Value::String(part) => part.clone(),
                    other => python::repr(other),
                })
                .collect();
            if texts.is_empty() {
                return (Vec::new(), 0);
            }
            joined = texts.join("\n");
            joined.as_str()
        }
        other if truthy(other) => {
            joined = python::repr(other);
            joined.as_str()
        }
        _ => "",
    };
    let lines = python::splitlines(text);
    let total = i64::try_from(lines.len()).unwrap_or(i64::MAX);
    let from = lines.len().saturating_sub(OUTPUT_TAIL_LINES);
    let tail = lines[from..]
        .iter()
        .map(|line| python::first(line, LINE_SIGNS).to_string())
        .collect();
    (tail, total)
}

/// The lines an edit adds and removes, counted, with the first of them to show: for a tool
/// that edits or writes a file, or that was given a diff. `None` for any other.
pub fn diff_stats(name: &str, input: &Value) -> Option<Value> {
    let nothing = Fields::new();
    let input = input.as_object().unwrap_or(&nothing);
    let given = |key: &str| input.get(key).filter(|value| truthy(value));
    // A text that is there, an empty one for what is not, and no diff at all for what is
    // there and is no text: the Python Host breaks on that and shows none.
    let text_at = |fields: &'_ Fields, key: &str| -> Option<String> {
        match fields.get(key).filter(|value| truthy(value)) {
            None => Some(String::new()),
            Some(Value::String(text)) => Some(text.clone()),
            Some(_) => None,
        }
    };
    let key = name.to_lowercase();
    let path = match given("file_path")
        .or_else(|| given("filePath"))
        .or_else(|| given("path"))
    {
        Some(Value::String(path)) => path.clone(),
        Some(other) => python::repr(other),
        None => String::new(),
    };
    let (mut added, mut removed) = (0, 0);
    let mut body: Vec<String> = Vec::new();
    let patch = given("diff").or_else(|| given("patch"));
    if matches!(key.as_str(), "edit" | "str_replace") {
        let old = text_at(input, "old_string")?;
        let new = text_at(input, "new_string")?;
        (added, removed, body) = count_diff(&old, &new);
    } else if key == "multiedit" {
        for edit in input
            .get("edits")
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
        {
            let Some(edit) = edit.as_object() else {
                continue;
            };
            let old = text_at(edit, "old_string")?;
            let new = text_at(edit, "new_string")?;
            let (more, fewer, lines) = count_diff(&old, &new);
            added += more;
            removed += fewer;
            body.extend(lines);
        }
    } else if let (true, Some(Value::String(content))) = (
        matches!(key.as_str(), "write" | "create" | "write_file"),
        input.get("content"),
    ) {
        let lines = python::splitlines(content);
        added = lines.len();
        body = lines.iter().map(|line| format!("+{line}")).collect();
    } else if let Some(Value::String(patch)) = patch {
        let lines = python::splitlines(patch);
        added = lines.iter().filter(|line| marks(line, '+')).count();
        removed = lines.iter().filter(|line| marks(line, '-')).count();
        body = lines
            .iter()
            .filter(|line| !names_a_file(line))
            .map(|line| (*line).to_string())
            .collect();
    } else {
        return None;
    }
    let shown: Vec<&str> = body
        .iter()
        .take(DIFF_LINES)
        .map(|line| python::first(line, LINE_SIGNS))
        .collect();
    let files = if path.is_empty() {
        json!([])
    } else {
        json!([{"path": path, "added": added, "removed": removed}])
    };
    Some(json!({"added": added, "removed": removed, "files": files, "preview": shown.join("\n")}))
}

/// Whether a line of a diff adds (`+`) or removes (`-`) a line: it begins with the sign, and
/// not with three of them, which is how a diff names a file.
fn marks(line: &str, sign: char) -> bool {
    line.starts_with(sign) && !line.starts_with(&String::from(sign).repeat(3))
}

fn names_a_file(line: &str) -> bool {
    line.starts_with("---") || line.starts_with("+++")
}

/// How many lines the change from one text to another adds and removes, and the lines of
/// its diff without the two that name files.
fn count_diff(old: &str, new: &str) -> (usize, usize, Vec<String>) {
    let lines = difflib::unified(&python::splitlines(old), &python::splitlines(new), 1);
    let added = lines.iter().filter(|line| marks(line, '+')).count();
    let removed = lines.iter().filter(|line| marks(line, '-')).count();
    let body = lines
        .into_iter()
        .filter(|line| !names_a_file(line))
        .collect();
    (added, removed, body)
}
