//! Evidence from a Claude background Bash invocation. No clock, storage,
//! process ownership or provider I/O belongs to these rules.
use crate::{janitors, python};
use regex::Regex;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::sync::LazyLock;

pub const RECORD_LIMIT: usize = 4 * 1024 * 1024;
pub const OUTPUT_LIMIT: usize = 64 * 1024;
const DAY: i64 = 86_400_000;
pub fn valid_id(id: &str) -> bool {
    (1..=160).contains(&id.len())
        && id
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'-' | b'_'))
}
pub fn job_id(agent: &str, native: &str, tool: &str) -> String {
    let mut hash = Sha256::new();
    hash.update(agent);
    hash.update([0]);
    hash.update(native);
    hash.update([0]);
    hash.update(tool);
    format!("claude-task:{:x}", hash.finalize())
}
pub fn safe_text(text: &str) -> String {
    static SECRET: LazyLock<Option<Regex>> = LazyLock::new(|| {
        Regex::new(r#"(?i)((?:[\w-]*(?:token|password|secret|api[_-]?key))["']?[\s\x1c-\x1f]*[:=][\s\x1c-\x1f]*["']?)[^\s\x1c-\x1f"';,]+"#).ok()
    });
    SECRET.as_ref().map_or_else(String::new, |re| {
        re.replace_all(&janitors::runs::redact(text), "${1}[redacted]")
            .into_owned()
    })
}
fn field<'a>(body: &'a str, tag: &str) -> &'a str {
    body.split_once(&format!("<{tag}>"))
        .and_then(|(_, s)| s.split_once(&format!("</{tag}>")))
        .map_or("", |(s, _)| python::strip(s))
}

pub fn parse_actions(native: &str, project: &str, record: &Value, now: i64) -> Vec<Value> {
    let mut actions = Vec::new();
    if record.get("sessionId").is_some_and(|id| id != native)
        || python::truthy(&record["isSidechain"])
    {
        return actions;
    }
    let Some(at) =
        janitors::quota::stamp(&record["timestamp"]).filter(|at| *at <= now.saturating_add(60_000))
    else {
        return actions;
    };
    let kind = record["type"].as_str();
    let meta = &record["toolUseResult"];
    for block in record["message"]["content"]
        .as_array()
        .into_iter()
        .flatten()
    {
        let get = |key: &str| block.get(key).cloned().unwrap_or_else(|| json!(""));
        if kind == Some("assistant")
            && block["type"] == "tool_use"
            && block["name"] == "Bash"
            && block["input"]["run_in_background"] == true
        {
            let desc = &block["input"]["description"];
            let title = if python::truthy(desc) {
                desc.as_str()
                    .map(str::to_owned)
                    .unwrap_or_else(|| python::repr(desc))
            } else {
                String::new()
            };
            actions.push(json!({"tool":get("id"),"at":at,"state":"launching","title":python::first(&safe_text(&title),120),"project":project}));
        } else if kind == Some("user") && block["type"] == "tool_result" {
            let tool = get("tool_use_id");
            if python::truthy(&block["is_error"]) {
                actions.push(json!({"tool":tool,"at":at,"state":"failed"}));
            } else if let Some(task) = meta["backgroundTaskId"].as_str().filter(|id| valid_id(id)) {
                actions.push(json!({"tool":tool,"at":at,"state":"running","task":task}));
            } else if meta["exitCode"].is_i64() || meta["exitCode"].is_u64() {
                actions.push(json!({"tool":tool,"at":at,"state":if meta["exitCode"]==0{"completed"}else{"failed"},"exit_code":meta["exitCode"]}));
            }
        }
    }
    if kind == Some("queue-operation")
        && (record["operation"].is_null() || record["operation"] == "enqueue")
    {
        let mut text = record["content"].as_str().unwrap_or("");
        while let Some((_, rest)) = text.split_once("<task-notification>") {
            let Some((body, tail)) = rest.split_once("</task-notification>") else {
                break;
            };
            text = tail;
            let state = field(body, "status");
            let task = field(body, "task-id");
            if matches!(state, "completed" | "failed" | "stopped" | "killed") && valid_id(task) {
                actions.push(json!({"tool":field(body,"tool-use-id"),"at":at,"state":if state=="killed"{"stopped"}else{state},"task":task,"require_receipt":true}));
            }
        }
    }
    actions
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct Task {
    pub title: String,
    pub status: String,
    pub started_at: i64,
    pub updated_at: i64,
    pub terminal_at: Option<i64>,
    pub terminal_reason: String,
    pub progress_text: String,
    pub metadata: Value,
}
/// Apply an action to the job identified by its agent, native session and
/// tool ID. A title or a command is never an identity. Return whether a
/// timeline entry is owed; a late identity receipt can change metadata alone.
pub fn observe(slot: &mut Option<Task>, native: &str, bound_at: i64, a: &Value, now: i64) -> bool {
    let Some(tool) = a["tool"].as_str().filter(|id| valid_id(id)) else {
        return false;
    };
    let Some(at) = a["at"].as_i64() else {
        return false;
    };
    let task = a["task"].as_str().unwrap_or("");
    let mut state = a["state"].as_str().unwrap_or("");
    if !matches!(
        state,
        "launching" | "running" | "unknown" | "completed" | "failed" | "stopped"
    ) {
        return false;
    }
    if a["require_receipt"] == true
        && !slot
            .as_ref()
            .is_some_and(|j| j.metadata["provider_task_id"] == task)
    {
        return false;
    }
    if slot.is_none() {
        if state != "launching" || at < bound_at {
            return false;
        }
        let title = safe_text(a["title"].as_str().unwrap_or(""));
        *slot = Some(Task {
            title: if title.is_empty() {
                "Claude background command".into()
            } else {
                python::first(&title, 120).into()
            },
            status: "queued".into(),
            started_at: at,
            updated_at: at,
            progress_text: String::new(),
            terminal_at: None,
            terminal_reason: String::new(),
            metadata: json!({"provider":"claude","native_session_id":native,"tool_use_id":tool,"provider_task_id":"","provider_project":a["project"].as_str().unwrap_or(""),"provider_observed_at":at}),
        });
    }
    let Some(job) = slot else { return false };
    let old = &job.metadata;
    let prior = old["provider_observed_at"].as_i64().unwrap_or(at);
    let receipt = old["provider_task_id"].as_str().unwrap_or("");
    let inferred = job.terminal_at.is_some()
        && matches!(
            old["stop_basis"].as_str(),
            Some("turn_exit" | "evidence_lost")
        );
    let correction = inferred
        && matches!(state, "completed" | "failed")
        && (task.is_empty() || receipt.is_empty() || receipt == task);
    if inferred && state == "running" && receipt.is_empty() && !task.is_empty() {
        job.metadata["provider_task_id"] = json!(task);
        return false;
    }
    if job.terminal_at.is_some() && !correction
        || at < prior
        || !task.is_empty() && !receipt.is_empty() && receipt != task
    {
        return false;
    }
    if old.get("provider_state").is_some()
        && (state == "launching"
            || state == "running"
                && old["provider_state"] == "unknown"
                && !receipt.is_empty()
                && at <= prior)
    {
        return false;
    }
    let mut meta = old.clone();
    if correction && let Some(map) = meta.as_object_mut() {
        for k in [
            "stop_basis",
            "stop_turn",
            "turn_exited_at",
            "evidence_lost_at",
        ] {
            map.remove(k);
        }
    }
    if !task.is_empty() {
        meta["provider_task_id"] = json!(task);
    }
    if !a["exit_code"].is_null() {
        meta["exit_code"] = a["exit_code"].clone();
    }
    if stale(at, now) && matches!(state, "launching" | "running") {
        state = "unknown";
    }
    meta["provider_state"] = json!(state);
    meta["provider_observed_at"] = json!(at);
    if meta == *old {
        return false;
    }
    let (status, progress, reason) = match state {
        "launching" => ("queued", "Launching; awaiting provider receipt", ""),
        "unknown" => ("queued", "Unknown: no recent provider task evidence", ""),
        "running" => (
            "running",
            "Provider reported running; no process ownership claimed",
            "",
        ),
        "completed" => ("succeeded", "Provider task completed", "provider_completed"),
        "failed" => ("failed", "Provider task failed", "provider_failed"),
        _ => (
            "failed",
            "Session ended without a completion record; outcome unknown",
            "provider_session_ended_without_result",
        ),
    };
    job.metadata = meta;
    job.status = status.into();
    job.progress_text = progress.into();
    job.terminal_reason = reason.into();
    job.terminal_at = (!reason.is_empty()).then_some(at);
    job.updated_at = if state == "unknown" { now } else { at };
    true
}
fn stale(at: i64, now: i64) -> bool {
    now.saturating_sub(at) > 600_000
}
pub fn turn_link_expired(exited: i64, now: i64) -> bool {
    now.saturating_sub(exited) > 7 * DAY
}
impl Task {
    pub fn reconcile(&mut self, ended: bool, now: i64) -> bool {
        let at = self.metadata["provider_observed_at"]
            .as_i64()
            .unwrap_or(now);
        if self.terminal_at.is_some()
            || self.metadata["provider_state"] == "unknown"
            || !(ended || stale(at, now))
        {
            return false;
        }
        self.metadata["provider_state"] = json!("unknown");
        self.updated_at = now;
        self.status = "queued".into();
        self.progress_text = "Unknown: no recent provider task evidence".into();
        true
    }
    pub fn close_turn(&mut self, token: &str, at: i64) -> bool {
        if self.terminal_at.is_some() {
            return false;
        }
        self.metadata["stop_basis"] = json!("turn_exit");
        self.metadata["stop_turn"] = json!(token);
        self.metadata["turn_exited_at"] = json!(at);
        self.stop(at,"provider_turn_exited_without_result","Its Claude turn ended, and the task ended with it. No completion record; outcome unknown")
    }
    pub fn expire(&mut self, protected: bool, now: i64) -> bool {
        if protected
            || self.terminal_at.is_some()
            || now.saturating_sub(
                self.metadata["provider_observed_at"]
                    .as_i64()
                    .unwrap_or(self.updated_at),
            ) <= DAY
        {
            return false;
        }
        self.metadata["stop_basis"] = json!("evidence_lost");
        self.metadata["evidence_lost_at"] = json!(now);
        self.stop(now,"provider_task_evidence_lost","No word from Claude about this task for a long time; it is treated as ended. Outcome unknown")
    }
    fn stop(&mut self, at: i64, reason: &str, progress: &str) -> bool {
        self.terminal_at = Some(at);
        self.updated_at = at;
        self.status = "failed".into();
        self.progress_text = progress.into();
        self.terminal_reason = reason.into();
        self.metadata["provider_state"] = json!("stopped");
        true
    }
}
pub fn mirror(job: &Value, registered: &[Value], at: i64) -> Value {
    let mut shown = job.clone();
    let m = &job["metadata"];
    if job["heartbeat_source"] == "provider_event"
        && python::truthy(&m["provider"])
        && python::truthy(&m["tool_use_id"])
        && let Some(other) = registered.iter().find(|r| {
            r["agent_id"] == job["agent_id"]
                && r["heartbeat_source"] != "provider_event"
                && ["provider", "native_session_id", "tool_use_id"]
                    .iter()
                    .all(|k| r["metadata"][*k] == m[*k])
        })
    {
        shown["status"] = json!("superseded");
        shown["projection_only"] = json!(true);
        shown["replaced_by_job_id"] = other["job_id"].clone();
        shown["can_cancel"] = json!(false);
        shown["updated_at"] = json!(at.max(job["updated_at"].as_i64().unwrap_or(0)));
    }
    shown
}
pub fn cancel_refusal(job: &Value) -> (u16, Value) {
    (
        409,
        json!({"ok":false,"error":"provider_task_cancel_unsupported","changed":false,"job":job,"job_handle":format!("bg1:{}:{}",job["generation"].as_i64().filter(|x|*x!=0).unwrap_or(1),job["job_id"].as_str().unwrap_or(""))}),
    )
}

impl Task {
    /// Fields specific to an observed task in the existing job response.
    pub fn display(&self, now: i64) -> Value {
        let outcome = if matches!(
            self.metadata["provider_state"].as_str(),
            Some("unknown" | "stopped")
        ) {
            "unknown"
        } else if self.terminal_at.is_none() {
            "pending"
        } else {
            &self.status
        };
        json!({"can_cancel":false,"outcome_state":outcome,"worker_freshness":"unknown","elapsed_ms":self.terminal_at.unwrap_or(now).saturating_sub(self.started_at).max(0),"heartbeat_at":Value::Null,"worker_pid":Value::Null})
    }
}
