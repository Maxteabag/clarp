//! An explanation on the row of a tool while its turn is shown.
//!
//! Whoever watches a turn sees each tool the agent uses as a row. Where the owner has the
//! use of tools explained, the Host puts a plain sentence on the row by itself
//! (`docs/live-items.md`, section 6): every new tool item (and one that is shown again with
//! another command) is announced as waiting for its explanation at once, the desk is asked
//! about an agent's items in rounds of at most eight, and what it answers is put on the
//! item: the sentence, or that there is none. No item waits for ever: one that has waited
//! forty-five seconds, one that falls off a backlog of twenty-four for a newer one, and one
//! whose turn ended fifteen seconds ago is settled as skipped, and a new turn takes the
//! place of the items of the one before.
//!
//! This is a book that is told the time: it says what to put on which item and what to
//! ask, and whoever runs it asks the desk and puts the patches on the rows.
//!
//! The rules are the Python Host's (`server/lib/live_explain.py`, `LiveExplainer`), held by
//! `tests/fixtures/tool-explanation-rows-vectors.json`. What that class does for a patch
//! its other process cannot take, and for what it missed while it was away, has no
//! counterpart here: the rows and their explainer are one process on this Host.

use std::collections::HashMap;

use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};

use crate::python;

/// How long an item waits for its explanation at most, and how long after its turn ended.
pub const WAIT_MS: i64 = 45_000;
pub const AFTER_TURN_MS: i64 = 15_000;
/// How many items of an agent the desk is asked about at once, and how many may wait.
pub const AT_ONCE: usize = 8;
pub const BACKLOG: usize = 24;
/// Of how many items it is remembered what they were when they were last seen, and how
/// many the desk is told at once to let go of.
const SEEN_MOST: usize = 4096;
const LET_GO_MOST: usize = 64;

/// What is to be put on an item: the value of its `tool.explain`.
#[derive(Clone, Debug, PartialEq)]
pub struct Patch {
    pub agent_id: String,
    pub item_id: String,
    pub explain: Value,
}

impl Patch {
    /// The fields an item is patched with.
    pub fn fields(&self) -> Value {
        json!({ "tool": { "explain": self.explain } })
    }
}

/// What the desk is to be asked: about these items of an agent, each under the name it is
/// wanted by, and which names it may let go of.
#[derive(Clone, Debug, PartialEq)]
pub struct Asking {
    /// `None`: nothing is asked about, the desk is only told what to let go of.
    pub agent_id: Option<String>,
    pub items: Vec<Value>,
    pub release: Vec<String>,
}

/// An item that waits for its explanation.
#[derive(Clone, Debug)]
struct Job {
    item_id: String,
    tool: Value,
    level: u8,
    demand: String,
    deadline: i64,
    asked: bool,
}

/// The rows that wait.
#[derive(Debug, Default)]
pub struct Rows {
    /// What each item was when it was last seen: its tool and its command.
    seen: Map<String, Value>,
    /// The agents in the order their first item came, each with its items in theirs.
    jobs: Vec<(String, Vec<Job>)>,
    turns: HashMap<String, String>,
    let_go: Vec<String>,
}

/// A value as Python writes it into a text (`str`): nothing is `None`.
fn said(value: Option<&Value>) -> String {
    match value {
        None | Some(Value::Null) => "None".to_string(),
        Some(Value::String(said)) => said.clone(),
        Some(other) => python::repr(other),
    }
}

/// The first of some values that says anything, as a text; nothing where none does.
fn first_said(values: &[Option<&Value>]) -> String {
    let saying = values.iter().flatten().find(|value| python::truthy(value));
    saying.map_or_else(String::new, |value| said(Some(value)))
}

fn settled(level: u8, status: &str, reason: &str) -> Value {
    json!({ "text": null, "level": level, "status": status, "reason": reason })
}

/// A tool as the desk is asked about it: its name, its command, what it was given as far
/// as it is shown, and its label as its title.
fn activity(tool: &Value) -> Value {
    let preview = tool
        .get("input_preview")
        .filter(|preview| preview.is_object());
    let preview = preview.cloned().unwrap_or_else(|| json!({}));
    let or_empty = |key: &str| {
        let value = tool.get(key).filter(|value| python::truthy(value));
        value.cloned().unwrap_or_else(|| json!(""))
    };
    let mut activity = Map::new();
    activity.insert("name".to_string(), or_empty("name"));
    activity.insert("command".to_string(), or_empty("command"));
    activity.insert("input".to_string(), preview.clone());
    activity.insert("title".to_string(), or_empty("label"));
    for key in ["file_path", "path", "pattern"] {
        if let Some(value) = preview.get(key).filter(|value| python::truthy(value)) {
            activity.insert(key.to_string(), value.clone());
        }
    }
    Value::Object(activity)
}

impl Rows {
    /// Whether anything waits, or the desk is still to be told what to let go of.
    pub fn busy(&self) -> bool {
        self.jobs.iter().any(|(_, jobs)| !jobs.is_empty()) || !self.let_go.is_empty()
    }

    fn of(&mut self, agent_id: &str) -> Option<&mut Vec<Job>> {
        let found = self.jobs.iter_mut().find(|(agent, _)| agent == agent_id);
        found.map(|(_, jobs)| jobs)
    }

    /// An event that shows a turn. Answers what is to be put on its new tool items right
    /// away: that each waits for its explanation, and that one that fell off the backlog
    /// gets none. `enabled`, `level`: the owner's setting as it is now.
    pub fn observe(&mut self, event: &Value, enabled: bool, level: u8, now: i64) -> Vec<Patch> {
        if event["type"] != "live" || !enabled || level == 0 {
            return Vec::new();
        }
        let agent_id = first_said(&[event.get("agent_id")]);
        let (mut admitted, mut pushed_out): (Vec<Patch>, Vec<Patch>) = (Vec::new(), Vec::new());
        for op in event["ops"].as_array().into_iter().flatten() {
            if op["op"] == "turn" {
                self.turn(&agent_id, &op["turn"], now);
                continue;
            }
            let tool = &op["item"]["tool"];
            let named = ["name", "command"]
                .iter()
                .any(|key| python::truthy(&tool[*key]));
            if op["op"] != "upsert" || op["kind"] != "tool" || !tool.is_object() || !named {
                continue;
            }
            let about = first_said(&[tool.get("command"), tool.get("label")]);
            let key = format!("{}\0{about}", said(tool.get("name")));
            let item_id = first_said(&[op.get("id")]);
            if self.seen.get(&item_id).and_then(Value::as_str) == Some(key.as_str()) {
                continue;
            }
            self.seen.insert(item_id.clone(), json!(key));
            if self.seen.len() > SEEN_MOST {
                let oldest = self.seen.keys().next().cloned().unwrap_or_default();
                self.seen.shift_remove(&oldest);
            }
            if agent_id.is_empty() {
                continue;
            }
            let wanted = Sha256::digest(format!("{agent_id}\0{item_id}\0{key}").as_bytes());
            let wanted: String = wanted.iter().map(|byte| format!("{byte:02x}")).collect();
            let job = Job {
                item_id: item_id.clone(),
                tool: tool.clone(),
                level,
                demand: format!("live-{}", &wanted[..40]),
                deadline: now + WAIT_MS,
                asked: false,
            };
            if self.of(&agent_id).is_none() {
                self.jobs.push((agent_id.clone(), Vec::new()));
            }
            let mut released: Vec<String> = Vec::new();
            if let Some(jobs) = self.of(&agent_id) {
                // The item as it was is asked about no longer.
                if let Some(at) = jobs.iter().position(|job| job.item_id == item_id) {
                    released.push(jobs.remove(at).demand);
                }
                jobs.push(job);
                while jobs.len() > BACKLOG {
                    let oldest = jobs.remove(0);
                    pushed_out.push(Patch {
                        agent_id: agent_id.clone(),
                        item_id: oldest.item_id,
                        explain: settled(oldest.level, "failed", "skipped"),
                    });
                    released.push(oldest.demand);
                }
            }
            self.let_go.extend(released);
            // Said at once, and not after the tools before it were answered: whoever
            // watches is shown an honest wait, not the bare command.
            admitted.push(Patch {
                agent_id: agent_id.clone(),
                item_id,
                explain: json!({ "text": null, "level": level, "status": "pending" }),
            });
        }
        admitted.extend(pushed_out);
        admitted
    }

    /// A turn of an agent began, or is over. A new turn takes the place of the items of
    /// the one before; one that is over leaves its items a moment more.
    fn turn(&mut self, agent_id: &str, turn: &Value, now: i64) {
        let turn_id = first_said(&[turn.get("turn_id")]);
        if turn["status"] == "running" {
            let before = self.turns.insert(agent_id.to_string(), turn_id.clone());
            let another = before.is_some_and(|before| !before.is_empty() && before != turn_id);
            let mut released = Vec::new();
            if let Some(jobs) = self.of(agent_id).filter(|_| another) {
                released.extend(jobs.drain(..).map(|job| job.demand));
            }
            self.let_go.extend(released);
        } else if let Some(jobs) = self.of(agent_id) {
            for job in jobs {
                job.deadline = job.deadline.min(now + AFTER_TURN_MS);
            }
        }
    }

    /// One round: what waited too long is settled, and for each agent the desk is to be
    /// asked about its items: those it was asked about before until they settle, and in the
    /// places that are free the newest that wait. Answers what to put on which item, and
    /// what to ask. With explaining off, what would have been asked about gets none.
    pub fn round(&mut self, enabled: bool, level: u8, now: i64) -> (Vec<Patch>, Vec<Asking>) {
        let mut patches = Vec::new();
        let mut batches: Vec<(String, Vec<Job>)> = Vec::new();
        for (agent_id, jobs) in &mut self.jobs {
            let (late, in_time): (Vec<Job>, Vec<Job>) = std::mem::take(jobs)
                .into_iter()
                .partition(|job| job.deadline <= now);
            *jobs = in_time;
            for job in late {
                self.let_go.push(job.demand);
                patches.push(Patch {
                    agent_id: agent_id.clone(),
                    item_id: job.item_id,
                    explain: settled(job.level, "failed", "skipped"),
                });
            }
            // Those asked about before keep their place until they settle: the model may be
            // at them. The places that are free go to the newest that wait.
            let mut batch: Vec<Job> = jobs.iter().filter(|job| job.asked).cloned().collect();
            let waiting: Vec<usize> = (0..jobs.len())
                .filter(|place| !jobs[*place].asked)
                .collect();
            let free = AT_ONCE.saturating_sub(batch.len());
            for place in &waiting[waiting.len().saturating_sub(free)..] {
                jobs[*place].asked = true;
                batch.push(jobs[*place].clone());
            }
            if !batch.is_empty() {
                batches.push((agent_id.clone(), batch));
            }
        }
        self.jobs.retain(|(_, jobs)| !jobs.is_empty());
        let from = self.let_go.len().saturating_sub(LET_GO_MOST);
        let mut release = std::mem::take(&mut self.let_go).split_off(from);
        if !enabled || level == 0 {
            for (agent_id, batch) in &batches {
                let wanted: Vec<&str> = batch.iter().map(|job| job.demand.as_str()).collect();
                patches.extend(self.settle(agent_id, &wanted, "disabled"));
            }
            return (patches, Vec::new());
        }
        let mut askings = Vec::new();
        if batches.is_empty() && !release.is_empty() {
            askings.push(Asking {
                agent_id: None,
                items: Vec::new(),
                release: std::mem::take(&mut release),
            });
        }
        for (agent_id, batch) in batches {
            let items = batch.iter().map(|job| {
                json!({ "id": job.demand, "demand_id": job.demand, "activity": activity(&job.tool) })
            });
            askings.push(Asking {
                agent_id: Some(agent_id),
                items: items.collect(),
                release: std::mem::take(&mut release),
            });
        }
        (patches, askings)
    }

    /// Takes items out of the wait, each with that it gets no explanation and why.
    fn settle(&mut self, agent_id: &str, wanted: &[&str], reason: &str) -> Vec<Patch> {
        let mut patches = Vec::new();
        for demand in wanted {
            let Some(jobs) = self.of(agent_id) else {
                continue;
            };
            let Some(at) = jobs.iter().position(|waits| waits.demand == *demand) else {
                continue;
            };
            let job = jobs.remove(at);
            patches.push(Patch {
                agent_id: agent_id.to_string(),
                item_id: job.item_id,
                explain: settled(job.level, "failed", reason),
            });
        }
        self.jobs.retain(|(_, jobs)| !jobs.is_empty());
        patches
    }

    /// What the desk answered to an asking. An item it has a sentence for gets it; one that
    /// still waits there stays; any other gets none, and why.
    pub fn answered(&mut self, asking: &Asking, answer: &Value, level: u8) -> Vec<Patch> {
        let Some(agent_id) = asking.agent_id.as_deref() else {
            return Vec::new();
        };
        let answers = answer["items"].as_array();
        let mut patches = Vec::new();
        for item in &asking.items {
            let wanted = &item["id"];
            let of = answers
                .into_iter()
                .flatten()
                .rfind(|answer| answer["id"] == *wanted);
            let status = of.map_or(&Value::Null, |answer| &answer["status"]);
            if status == "pending" || status == "busy" {
                continue;
            }
            let sentence = of
                .map(|answer| &answer["text"])
                .filter(|text| python::truthy(text));
            let explain = match sentence.filter(|_| status == "ready") {
                Some(sentence) => json!({ "text": sentence, "level": level, "status": "ready" }),
                None if status == "cancelled" => settled(level, "failed", "skipped"),
                None => {
                    let reason = of.and_then(|answer| answer.get("reason"));
                    let why = first_said(&[reason, Some(status)]);
                    let why = if why.is_empty() {
                        "invalid_response"
                    } else {
                        why.as_str()
                    };
                    settled(level, "failed", why)
                }
            };
            let Some(jobs) = self.of(agent_id) else {
                continue;
            };
            let waits = jobs.iter().position(|job| wanted == job.demand.as_str());
            if let Some(at) = waits {
                let job = jobs.remove(at);
                patches.push(Patch {
                    agent_id: agent_id.to_string(),
                    item_id: job.item_id,
                    explain,
                });
            }
        }
        self.jobs.retain(|(_, jobs)| !jobs.is_empty());
        patches
    }

    /// The desk could not be asked: there is none (`unavailable`), or asking it failed
    /// (`error`). What was asked about gets no explanation, and why.
    pub fn unanswered(&mut self, asking: &Asking, reason: &str) -> Vec<Patch> {
        let Some(agent_id) = asking.agent_id.as_deref() else {
            return Vec::new();
        };
        let wanted: Vec<&str> = asking
            .items
            .iter()
            .filter_map(|item| item["id"].as_str())
            .collect();
        self.settle(agent_id, &wanted, reason)
    }
}
