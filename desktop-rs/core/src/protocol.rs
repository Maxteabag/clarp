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
    /// When the running turn started (epoch ms, the Host's clock), from the
    /// snapshot's `turn_started_at`. None while idle, and on a Host too old
    /// to send it (its 0 or null).
    pub turn_started_at: Option<i64>,
    /// When the Host marked the running turn stalled (epoch ms, Host
    /// contract 149): its tool silent for 10 minutes while the user's words
    /// wait. None when not stalled, and on a Host too old to say.
    pub stalled_since: Option<i64>,
    /// What the Host offers for the stall: "stop", or None.
    pub stalled_action: Option<String>,
    /// Who the running turn is for (`working_audience`, Host contract
    /// 124): `user`, `agents`, or empty while idle or from an older Host.
    pub working_audience: String,
    /// The name of the agent whose message started the running turn
    /// (`working_with.name`), or empty: "Working with <Name>".
    pub working_with: String,
    pub last_activity: i64,
    pub head_revision: i64,
    /// What the conversation's context holds, in tokens; None while the
    /// Host knows no real usage (it sends null), never a guessed 0 (#403).
    pub context_tokens: Option<i64>,
    pub context_window: i64,
    pub queue_revision: i64,
    pub queued_turn_count: i32,
    /// A Stop paused the queue: its turns wait until sent or resumed. False
    /// when the Host predates the field.
    pub queue_paused: bool,
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
    /// Coordination between agents in this chat (`hidden_coordination`):
    /// the effective `show`/`hide`, and the chat's own choice (empty:
    /// it follows the Host).
    pub coordination_visibility: String,
    pub coordination_override: String,
    /// The agent's chat view as the Host keeps it (`chat_view`, #702):
    /// `conversation`, `session`, or empty when the Host does not say.
    pub chat_view: String,
    /// The Host lets this agent answer the user from a conversation fork
    /// while its own turn works (`conversation_fork`, contract 153).
    pub conversation_fork: bool,
    /// The chat was a helper's (`helper_chat`, Host contract 177): it
    /// stays one after a move changed its role. False from an older Host.
    pub helper_chat: bool,
    /// The `server_id` of the Host this agent moved to (`moved.to.id`), or
    /// empty: a merged list leaves the row out when that Host is in it.
    pub moved_to: String,
    /// That Host's name (`moved.to.host`): where the agent lives now.
    pub moved_to_host: String,
    /// The Host the row belongs to, as a merged chat list names it; empty
    /// while the list holds one Host's agents (`clarp_core::all_hosts`).
    pub host: String,
    /// The move to another backend running now (`transit` {transit_id,
    /// phase, backend, model}, feature `agent_backend_transit`); None
    /// otherwise.
    pub transit: Option<Object>,
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
            turn_started_at: None,
            stalled_since: None,
            stalled_action: None,
            working_audience: String::new(),
            working_with: String::new(),
            last_activity: 0,
            head_revision: 0,
            context_tokens: None,
            context_window: 0,
            queue_revision: 0,
            queued_turn_count: 0,
            queue_paused: false,
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
            coordination_visibility: String::new(),
            coordination_override: String::new(),
            chat_view: String::new(),
            conversation_fork: false,
            helper_chat: false,
            moved_to: String::new(),
            moved_to_host: String::new(),
            host: String::new(),
            transit: None,
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
            turn_started_at: object.get("turn_started_at").and_then(Value::as_i64).filter(|t| *t > 0),
            stalled_since: crate::stall::since(object),
            stalled_action: crate::stall::action(object),
            working_audience: object.get("working_audience").and_then(Value::as_str).unwrap_or_default().trim().to_ascii_lowercase(),
            working_with: object.get("working_with").and_then(|w| w.get("name")).and_then(Value::as_str).unwrap_or_default().trim().to_owned(),
            last_activity: i("last_activity"),
            head_revision: i("head_revision"),
            context_tokens: object.get("context_tokens").and_then(Value::as_i64),
            context_window: i("context_window"),
            queued_turn_count: i("queued_turn_count") as i32,
            queue_paused: b("queue_paused"),
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
            coordination_visibility: object.get("coordination_visibility").and_then(Value::as_str).unwrap_or_default().to_owned(),
            coordination_override: object.get("coordination_override").and_then(Value::as_str).unwrap_or_default().to_owned(),
            chat_view: s("chat_view").trim().to_ascii_lowercase(),
            conversation_fork: b("conversation_fork"),
            helper_chat: b("helper_chat"),
            moved_to: object
                .get("moved")
                .and_then(|m| m.get("to"))
                .and_then(|to| to.get("id"))
                .and_then(Value::as_str)
                .unwrap_or_default()
                .trim()
                .to_owned(),
            moved_to_host: object
                .get("moved")
                .and_then(|m| m.get("to"))
                .and_then(|to| to.get("host"))
                .and_then(Value::as_str)
                .unwrap_or_default()
                .trim()
                .to_owned(),
            host: String::new(),
            transit: object.get("transit").and_then(Value::as_object).cloned(),
        }
    }

    /// Takes the row's `fields` an `agent-roster` event carries (feature
    /// `roster_fields`): each named field is set as [`Agent::from_json`]
    /// reads it from a snapshot row, and every field the event does not name
    /// keeps its value. The session is the row's key and never changes; a
    /// field this desktop does not read is passed over.
    pub fn patch(&mut self, fields: &Object) {
        let new = Self::from_json(fields);
        for key in fields.keys() {
            match key.as_str() {
                "agent_id" => self.agent_id = new.agent_id.clone(),
                "persona" => self.persona = new.persona.clone(),
                "backend" => self.backend = new.backend.clone(),
                "cwd" => self.working_directory = new.working_directory.clone(),
                "model" => self.model = new.model.clone(),
                "effort" => self.effort = new.effort.clone(),
                "avatar_url" => self.avatar_url = new.avatar_url.clone(),
                "avatar_symbol" => self.avatar_symbol = new.avatar_symbol.clone(),
                "status_text" => self.status_text = new.status_text.clone(),
                "last_message" => self.last_message = new.last_message.clone(),
                "last_completed_message" => self.last_completed_message = new.last_completed_message.clone(),
                "conversation_id" => self.conversation_id = new.conversation_id.clone(),
                "voice_id" => self.voice_id = new.voice_id.clone(),
                "parent_agent_id" => self.parent_agent_id = new.parent_agent_id.clone(),
                "role" => self.role = new.role.clone(),
                "helper_state" => self.helper_state = new.helper_state.clone(),
                "background_jobs" => {
                    self.background_job_count = new.background_job_count;
                    self.background_sub_agent_count = new.background_sub_agent_count;
                }
                "child_count" => self.child_count = new.child_count,
                "running_children" => self.running_children = new.running_children,
                "schedules" => self.schedules = new.schedules.clone(),
                "mcp_servers" => self.mcp_servers = new.mcp_servers.clone(),
                "team_ids" => self.team_ids = new.team_ids.clone(),
                "backend_quota" => self.backend_quota = new.backend_quota.clone(),
                "latest_state_ts" => self.latest_state_timestamp = new.latest_state_timestamp,
                "turn_started_at" => self.turn_started_at = new.turn_started_at,
                "stalled_since" => self.stalled_since = new.stalled_since,
                "stalled_action" => self.stalled_action = new.stalled_action.clone(),
                "working_audience" => self.working_audience = new.working_audience.clone(),
                "working_with" => self.working_with = new.working_with.clone(),
                "last_activity" => self.last_activity = new.last_activity,
                "head_revision" => self.head_revision = new.head_revision,
                "context_tokens" => self.context_tokens = new.context_tokens,
                "context_window" => self.context_window = new.context_window,
                "queue_revision" => self.queue_revision = new.queue_revision,
                "queued_turn_count" => self.queued_turn_count = new.queued_turn_count,
                "queue_paused" => self.queue_paused = new.queue_paused,
                "alive" => self.alive = new.alive,
                "focused" => self.focused = new.focused,
                "muted" => self.muted = new.muted,
                "heartbeat_enabled" => self.heartbeat_enabled = new.heartbeat_enabled,
                "dreaming_enabled" => self.dreaming_enabled = new.dreaming_enabled,
                "archived_at" => self.archived = new.archived,
                "is_janitor" => self.janitor = new.janitor,
                "coordination_visibility" => self.coordination_visibility = new.coordination_visibility.clone(),
                "coordination_override" => self.coordination_override = new.coordination_override.clone(),
                "chat_view" => self.chat_view = new.chat_view.clone(),
                "conversation_fork" => self.conversation_fork = new.conversation_fork,
                "helper_chat" => self.helper_chat = new.helper_chat,
                "moved" => {
                    self.moved_to = new.moved_to.clone();
                    self.moved_to_host = new.moved_to_host.clone();
                }
                "transit" => self.transit = new.transit.clone(),
                _ => {}
            }
        }
        if fields.contains_key("quiet") || fields.contains_key("is_janitor") {
            self.quiet = json::boolean(fields, "quiet") || self.janitor;
        }
        // Busy as the row says it: its own flag, or a state that works.
        if fields.contains_key("latest_state") || fields.contains_key("busy") {
            if fields.contains_key("latest_state") {
                self.latest_state = new.latest_state.clone();
            }
            self.busy = json::boolean(fields, "busy") || is_busy_state(&self.latest_state);
            if !self.busy {
                self.turn_started_at = None;
                self.stalled_since = None;
                self.stalled_action = None;
                self.working_audience.clear();
                self.working_with.clear();
            }
        }
    }

    pub fn is_helper(&self) -> bool {
        self.is_helpers_chat() && !self.parent_agent_id.is_empty()
    }

    /// A helper's chat: its role is `helper`, or the Host says the chat was
    /// a helper's (`helper_chat`, contract 177), parent or not.
    pub fn is_helpers_chat(&self) -> bool {
        self.role == "helper" || self.helper_chat
    }

    /// A finished helper collapses into its parent's "N helpers done" line:
    /// idle and done, reported or abandoned (`sidebar::helper_finished`).
    pub fn helper_finished(&self) -> bool {
        self.is_helper() && crate::sidebar::helper_finished(&self.helper_state, self.busy)
    }

    /// Listed among its parent's active helpers: every helper that is not
    /// finished (a working one whatever its state, an idle running or
    /// ended-unreported one).
    pub fn helper_running(&self) -> bool {
        self.is_helper() && !self.helper_finished()
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
    /// A user row read into a turn other than its own (feature `read_into`,
    /// Rust Host contract 189): that turn's id, `read_into.trace_id`, as
    /// `GET /live` names it (`turn.turn_id`). Empty on every other row.
    pub read_into: String,
    /// Which view the row belongs to (`chat_view`, #702): `conversation`,
    /// `session`, or empty from a Host without the feature.
    pub view: String,
    /// On a settled turn's anchor row: how many of the turn's rows the
    /// conversation view leaves out (`chat_view`).
    pub session_rows: Option<i64>,
    /// Rows the Host left out before this one (`hidden_coordination`):
    /// `{messages, exchanges, agents, capped}`.
    pub hidden_before: Option<Object>,
    pub category: String,
    pub activity_status: String,
    pub activity_match_key: String,
    pub tools: Vec<Value>,
    pub display_cells: Vec<Value>,
    pub revision: i64,
    pub activity_count: i32,
    pub pending: bool,
    pub delivery_failed: bool,
    /// The Host's one-line reason for a failed send of this client, shown on
    /// its bubble; local only (CLIENT-SEND-500).
    pub delivery_error: String,
    /// F4 (`send_delivery_state`): this user send's delivery state from its own
    /// receipt (`pending_launch`, `stopped`, `handed_over`); empty = unknown.
    pub send_state: String,
    /// This client's own send, not yet sent back by the Host: local only. The
    /// Host's row that takes its place (by id) does not carry it.
    pub sent_here: bool,
    pub activity: bool,
    pub automated: bool,
    pub tool_details_available: bool,
    /// Why a failed turn failed, on its failure notice row (`failure_kind`,
    /// Rust Host contract 191): `ended_from_outside`, `usage_limit`, `auth`,
    /// `connection`, …; empty where the Host keeps none.
    pub failure_kind: String,
}

/// A failure that was no fault of the tool: its process was killed by a
/// signal the Host did not send (`failure_kind`, Rust Host contract 191).
pub const ENDED_FROM_OUTSIDE: &str = "ended_from_outside";

/// A row the desktop never draws: one the Host marks automated, or the note
/// the Host leaves an agent after cutting its turn for the owner's message
/// (origin `made-way`, id `u-made-way-<turn>`), which is for the agent, not
/// for the person reading the chat, whether or not the Host marks it.
pub fn automated_row(object: &Object) -> bool {
    json::boolean(object, "automated")
        || json::boolean(object, "is_automated")
        || json::string(object, "origin") == "made-way"
        || json::string(object, "id").starts_with("u-made-way-")
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
            read_into: object.get("read_into").and_then(Value::as_object).map(|r| json::string(r, "trace_id")).unwrap_or_default(),
            hidden_before: object.get("hidden_before").and_then(Value::as_object).cloned(),
            view: s("view").trim().to_ascii_lowercase(),
            session_rows: object.get("session_rows").and_then(Value::as_i64).map(|n| n.max(0)),
            category,
            revision: json::integer(object, "revision"),
            activity_count: json::integer(object, "activity_count") as i32,
            automated: automated_row(object),
            tool_details_available: json::boolean(object, "tool_details_available"),
            failure_kind: s("failure_kind"),
            delivery_failed: json::boolean(object, "delivery_failed"),
            send_state: send_state(object),
            tools: json::array(object, "tools"),
            display_cells: json::array(object, "display_cells"),
            ..Self::default()
        }
    }
}

/// A user row's F4 receipt state, only when the receipt names this row
/// (`for.client_msg_id` is the id after `u-`). A Host that does not advertise
/// `send_delivery_state` sends no object: unknown.
fn send_state(object: &Object) -> String {
    let Some(delivery) = object.get("delivery").and_then(Value::as_object) else { return String::new() };
    let id = json::string(object, "id");
    let own = delivery.get("for").and_then(Value::as_object)
        .is_some_and(|f| id.strip_prefix("u-") == Some(json::string(f, "client_msg_id").as_str()));
    if own { json::string(delivery, "state") } else { String::new() }
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
