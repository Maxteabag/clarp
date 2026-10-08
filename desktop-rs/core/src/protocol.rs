//! Wire types from `/agents/snapshot`, `/log` and SSE audio events.
//! Port of the C++ client's `ProtocolTypes`.

use chrono::{DateTime, FixedOffset, Local, NaiveDateTime, TimeZone, Utc};
use serde_json::{Map, Value, json};

use crate::json::{self, Object};
use crate::text::cleaned_display_text;

#[derive(Debug, Clone, PartialEq)]
pub struct Agent {
    pub agent_id: String,
    pub session: String,
    pub persona: String,
    pub backend: String,
    pub working_directory: String,
    pub model: String,
    pub effort: String,
    pub avatar_url: String,
    pub avatar_symbol: String,
    pub latest_state: String,
    pub status_text: String,
    /// The latest messages' previews as plain text (the Host's markdown
    /// stripped once, on arrival, not on every chat-list rebuild).
    pub last_message: String,
    pub last_completed_message: String,
    pub conversation_id: String,
    pub voice_id: String,
    // Helper hierarchy; an older Host sends none of these, which leaves every
    // agent a top-level peer exactly as before.
    pub parent_agent_id: String,
    pub role: String,
    pub helper_state: String,
    pub schedules: Vec<Value>,
    pub mcp_servers: Vec<Value>,
    pub team_ids: Vec<Value>,
    /// Empty unless the Host says this agent's provider cannot serve a turn.
    pub backend_quota: Object,
    pub latest_state_timestamp: i64,
    pub last_activity: i64,
    pub head_revision: i64,
    pub context_tokens: i64,
    pub context_window: i64,
    pub queue_revision: i64,
    pub queued_turn_count: i32,
    /// `background_jobs` from the snapshot: active jobs, and how many of them
    /// are Clarp sub-agents. Zero when the Host predates the field.
    pub background_job_count: i32,
    pub background_sub_agent_count: i32,
    pub child_count: i32,
    pub running_children: i32,
    pub alive: bool,
    pub busy: bool,
    pub focused: bool,
    pub muted: bool,
    pub heartbeat_enabled: bool,
    pub dreaming_enabled: bool,
    pub archived: bool,
    pub unread: bool,
    /// A Host maintenance agent: inspectable, but it refuses chat controls.
    pub janitor: bool,
    /// Never wants the reader by itself: no unread, not in Ctrl+J
    /// (docs/notification-policy.md, "Quiet agents"). The Host's `quiet`;
    /// a Host older than contract 56 lacks it, so a janitor is quiet there.
    pub quiet: bool,
}

impl Default for Agent {
    fn default() -> Self {
        Self {
            agent_id: String::new(),
            session: String::new(),
            persona: String::new(),
            backend: String::new(),
            working_directory: String::new(),
            model: String::new(),
            effort: String::new(),
            avatar_url: String::new(),
            avatar_symbol: String::new(),
            latest_state: String::new(),
            status_text: String::new(),
            last_message: String::new(),
            last_completed_message: String::new(),
            conversation_id: String::new(),
            voice_id: String::new(),
            parent_agent_id: String::new(),
            role: "agent".into(),
            helper_state: String::new(),
            schedules: Vec::new(),
            mcp_servers: Vec::new(),
            team_ids: Vec::new(),
            backend_quota: Object::new(),
            latest_state_timestamp: 0,
            last_activity: 0,
            head_revision: 0,
            context_tokens: 0,
            context_window: 0,
            queue_revision: 0,
            queued_turn_count: 0,
            background_job_count: 0,
            background_sub_agent_count: 0,
            child_count: 0,
            running_children: 0,
            alive: false,
            busy: false,
            focused: false,
            muted: false,
            heartbeat_enabled: false,
            dreaming_enabled: false,
            archived: false,
            unread: false,
            janitor: false,
            quiet: false,
        }
    }
}

fn non_negative(value: i64) -> i32 {
    value.clamp(0, i64::from(i32::MAX)) as i32
}

impl Agent {
    pub fn from_json(object: &Object) -> Self {
        let s = |key| json::string(object, key);
        let i = |key| json::integer(object, key);
        let b = |key| json::boolean(object, key);
        let jobs = json::object(object, "background_jobs");
        let quota = json::object(object, "backend_quota");
        let latest_state = s("latest_state");
        let role = s("role");
        Self {
            agent_id: s("agent_id"),
            session: s("session"),
            persona: s("persona"),
            backend: s("backend"),
            working_directory: s("cwd"),
            model: s("model"),
            effort: s("effort"),
            avatar_url: s("avatar_url"),
            avatar_symbol: s("avatar_symbol"),
            status_text: s("status_text"),
            last_message: crate::decision_receipt::preview_line(&s("last_message")),
            last_completed_message: crate::decision_receipt::preview_line(&s("last_completed_message")),
            conversation_id: s("conversation_id"),
            voice_id: s("voice_id"),
            parent_agent_id: s("parent_agent_id"),
            role: if role.is_empty() { "agent".into() } else { role },
            helper_state: s("helper_state"),
            background_job_count: non_negative(json::integer(&jobs, "count")),
            background_sub_agent_count: non_negative(json::integer(&jobs, "sub_agents")),
            child_count: non_negative(i("child_count")),
            running_children: non_negative(i("running_children")),
            schedules: json::array(object, "schedules"),
            mcp_servers: json::array(object, "mcp_servers"),
            team_ids: json::array(object, "team_ids"),
            backend_quota: if quota.get("state").and_then(Value::as_str) == Some("exhausted") {
                quota
            } else {
                Object::new()
            },
            latest_state_timestamp: i("latest_state_ts"),
            last_activity: i("last_activity"),
            head_revision: i("head_revision"),
            context_tokens: i("context_tokens"),
            context_window: i("context_window"),
            queued_turn_count: i("queued_turn_count") as i32,
            queue_revision: i("queue_revision"),
            alive: b("alive"),
            busy: b("busy") || is_busy_state(&latest_state),
            focused: b("focused"),
            muted: b("muted"),
            janitor: b("is_janitor"),
            quiet: b("quiet") || b("is_janitor"),
            heartbeat_enabled: b("heartbeat_enabled"),
            dreaming_enabled: b("dreaming_enabled"),
            archived: !matches!(object.get("archived_at"), None | Some(Value::Null)),
            latest_state,
            unread: false,
        }
    }

    pub fn is_helper(&self) -> bool {
        self.role == "helper" && !self.parent_agent_id.is_empty()
    }

    /// A finished helper collapses into its parent's "N helpers done" line.
    pub fn helper_finished(&self) -> bool {
        self.is_helper() && matches!(self.helper_state.as_str(), "done" | "reported" | "abandoned")
    }

    pub fn helper_running(&self) -> bool {
        self.is_helper() && self.helper_state == "running"
    }

    /// The warning shown above the composer before a send; empty when the
    /// provider is usable or the Host is too old to say. Advisory only.
    pub fn quota_notice(&self, now: DateTime<Utc>) -> String {
        if self.backend_quota.is_empty() || self.busy {
            return String::new();
        }
        let quota = &self.backend_quota;
        let provider = json::string(quota, "provider_id");
        let name = match provider.as_str() {
            "codex" => "Codex",
            "claude" => "Claude",
            "agy" => "Antigravity",
            "" => "This backend",
            other => other,
        };
        let credits = json::string(quota, "reason") == "credits_depleted";
        let mut text = if credits {
            format!("{name} workspace is out of credits")
        } else {
            format!("{name} is out of quota")
        };
        if let Some(reset) = parse_iso_date(&json::string(quota, "resets_at")).filter(|r| *r > now) {
            let seconds = (reset - now).num_seconds();
            let wait = if seconds >= 86_400 {
                format!("{}d {}h", seconds / 86_400, (seconds % 86_400) / 3_600)
            } else {
                format!("{}h {}m", seconds / 3_600, (seconds % 3_600) / 60)
            };
            text += if credits { "  ·  included usage resets in " } else { "  ·  resets in " };
            text += &wait;
        }
        let fallback = json::string(quota, "fallback_model");
        if fallback.is_empty() {
            text += "  ·  a message will likely fail";
        } else {
            text += "  ·  runs on ";
            text += &fallback;
        }
        text
    }
}

/// Qt::ISODate: an offset or `Z` when present, otherwise local time.
pub fn parse_iso_date(text: &str) -> Option<DateTime<Utc>> {
    if text.is_empty() {
        return None;
    }
    if let Ok(parsed) = DateTime::<FixedOffset>::parse_from_rfc3339(text) {
        return Some(parsed.with_timezone(&Utc));
    }
    ["%Y-%m-%dT%H:%M:%S%.f", "%Y-%m-%dT%H:%M"]
        .iter()
        .find_map(|format| NaiveDateTime::parse_from_str(text, format).ok())
        .and_then(|naive| Local.from_local_datetime(&naive).earliest())
        .map(|local| local.with_timezone(&Utc))
}

#[derive(Debug, Clone, Default, PartialEq)]
pub struct Message {
    pub id: String,
    pub role: String,
    pub text: String,
    pub display_text: String,
    pub timestamp: String,
    pub kind: String,
    pub tool_name: String,
    pub origin: String,
    pub sender_name: String,
    pub sender_agent_id: String,
    pub sender_session: String,
    pub reply_to_agent_id: String,
    pub reply_to_name: String,
    pub reply_to_session: String,
    pub delivery: String,
    pub trace_id: String,
    /// An assistant text row's `commentary` or `final` (`log_turn_summary`).
    pub phase: String,
    /// On a settled turn's last row: its summary, as `GET /live`'s turn.
    pub turn: Option<Object>,
    pub category: String,
    pub activity_status: String,
    pub activity_match_key: String,
    pub tools: Vec<Value>,
    pub display_cells: Vec<Value>,
    pub revision: i64,
    pub activity_count: i32,
    pub pending: bool,
    pub delivery_failed: bool,
    pub activity: bool,
    pub automated: bool,
    pub tool_details_available: bool,
}

impl Message {
    pub fn from_json(object: &Object) -> Self {
        let s = |key| json::string(object, key);
        let text = s("text");
        let kind = s("kind");
        let mut category = s("category");
        if category.is_empty() {
            category = s("automated_category");
        }
        Self {
            id: s("id"),
            role: s("role"),
            display_text: cleaned_display_text(&text, kind == "live"),
            text,
            timestamp: s("timestamp"),
            kind,
            tool_name: s("tool_name"),
            origin: s("origin"),
            sender_name: s("sender_name"),
            sender_agent_id: s("sender_agent_id"),
            sender_session: s("sender_session"),
            reply_to_agent_id: s("reply_to_agent_id"),
            reply_to_name: s("reply_to_name"),
            reply_to_session: s("reply_to_session"),
            delivery: s("delivery"),
            trace_id: s("trace_id"),
            phase: s("phase"),
            turn: object.get("turn").and_then(Value::as_object).cloned(),
            category,
            revision: json::integer(object, "revision"),
            activity_count: json::integer(object, "activity_count") as i32,
            automated: json::boolean(object, "automated") || json::boolean(object, "is_automated"),
            tool_details_available: json::boolean(object, "tool_details_available"),
            delivery_failed: json::boolean(object, "delivery_failed"),
            tools: json::array(object, "tools"),
            display_cells: json::array(object, "display_cells"),
            ..Self::default()
        }
    }
}

#[derive(Debug, Clone, Default, PartialEq)]
pub struct AudioClip {
    pub clip_id: i64,
    pub session: String,
    pub persona: String,
    pub trace_id: String,
    pub url: String,
    pub stream_url: String,
    pub playlist_url: String,
    pub complete_url: String,
    pub audio_format: Object,
    pub preview: String,
}

impl AudioClip {
    pub fn from_json(object: &Object) -> Self {
        let s = |key| json::string(object, key);
        Self {
            clip_id: json::integer(object, "clip_id"),
            session: s("session"),
            persona: s("persona"),
            trace_id: s("trace_id"),
            url: s("url"),
            stream_url: s("stream_url"),
            playlist_url: s("playlist_url"),
            complete_url: s("complete_url"),
            audio_format: json::object(object, "audio_format"),
            preview: s("preview"),
        }
    }

    /// Protocol precedence: playlist beats stream beats file.
    pub fn preferred_source(&self) -> &str {
        [&self.playlist_url, &self.stream_url, &self.url]
            .into_iter()
            .find(|source| !source.is_empty())
            .map_or("", String::as_str)
    }
}

pub const BUSY_STATES: [&str; 3] = ["thinking", "tool", "compacting"];

pub fn is_busy_state(state: &str) -> bool {
    BUSY_STATES.contains(&state)
}

pub fn display_name(agent: &Agent) -> &str {
    if agent.persona.is_empty() { &agent.session } else { &agent.persona }
}

pub fn voice_delivery_session<'a>(capture_session: &'a str, current_session: &'a str) -> &'a str {
    if capture_session.is_empty() { current_session } else { capture_session }
}

/// A Harness sub-agent display cell (`kind: "subagents"`) reduced to what the
/// transcript shows: `phase` is spawned, waiting, finished, failed or activity;
/// `running` is true while the call is in flight; `name` is the sub-agent's
/// label and `task` its prompt or input. Empty for any other kind of cell.
pub fn describe_subagent_cell(cell: &Object) -> Object {
    if cell.get("kind").and_then(Value::as_str) != Some("subagents") {
        return Map::new();
    }
    let title = json::string(cell, "title");
    let status = json::string(cell, "status");
    let lower = title.to_lowercase();
    let running = status == "running";
    let phase = if status == "error" || lower.contains("interrupted") {
        "failed"
    } else if lower.contains("wait") {
        if running { "waiting" } else { "finished" }
    } else if lower.contains("spawn") || lower.contains("start") {
        "spawned"
    } else if ["close", "finish", "complete"].iter().any(|word| lower.contains(word)) {
        "finished"
    } else {
        "activity"
    };
    let (mut task, mut agent_label, mut status_line, mut other_line) =
        (String::new(), String::new(), String::new(), String::new());
    for line in json::array(cell, "lines") {
        let line = line.as_object().cloned().unwrap_or_default();
        let label = json::string(&line, "label");
        let text = json::string(&line, "text").trim().to_owned();
        if text.is_empty() {
            continue;
        }
        let labelled = || if label.is_empty() { text.clone() } else { format!("{label}: {text}") };
        if task.is_empty() && (label == "Task" || label == "Input") {
            task = text.clone();
        } else if agent_label.is_empty() && (label == "Agent" || label == "Thread") {
            agent_label = text.clone();
        } else if status_line.is_empty() && json::string(&line, "kind") == "status" {
            status_line = labelled();
        } else if other_line.is_empty() {
            other_line = labelled();
        }
    }
    if status_line.is_empty() {
        status_line = other_line;
    }
    let mut name = json::string(cell, "summary").trim().to_owned();
    if (name.is_empty() || name == "agent" || name == "agents") && !agent_label.is_empty() {
        name = agent_label;
    }
    if name.is_empty() {
        name = "sub-agent".into();
    }
    let task = if task.is_empty() { status_line } else { task };
    json!({"phase": phase, "running": running, "name": name, "task": task})
        .as_object()
        .cloned()
        .unwrap_or_default()
}
