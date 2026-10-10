//! The agent roster. Port of the C++ client's `AgentListModel`.
//!
//! Ops carry fully presented [`AgentRow`]s: offline state, live job counts
//! and helper counts are resolved here, so the Qt adapter's `data()` is a
//! lookup into its mirror and model-wide changes arrive as row updates.

use std::collections::{HashMap, HashSet};

use serde_json::{Value, json};

use crate::jobs::{JobCounts, JobTracker, is_sub_agent};
use crate::json::{self, Object};
use crate::list_ops::ListOp;
use crate::protocol::{Agent, display_name, is_busy_state};
use crate::text::name_order;
use crate::time_format::compact_duration;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Role {
    AgentId,
    Session,
    Name,
    Backend,
    WorkingDirectory,
    Model,
    Effort,
    AvatarUrl,
    AvatarSymbol,
    State,
    StatusText,
    LastMessage,
    LastCompletedMessage,
    LastActivity,
    ConversationId,
    HeadRevision,
    ContextTokens,
    ContextWindow,
    QueueCount,
    Alive,
    Busy,
    Focused,
    Muted,
    HeartbeatEnabled,
    DreamingEnabled,
    Schedules,
    McpServers,
    Unread,
    ParentAgentId,
    AgentRole,
    HelperState,
    ChildCount,
    RunningChildren,
    BackgroundJobCount,
    SubAgentCount,
    ProcessCount,
}

pub const ALL_ROLES: [Role; 36] = [
    Role::AgentId, Role::Session, Role::Name, Role::Backend, Role::WorkingDirectory, Role::Model,
    Role::Effort, Role::AvatarUrl, Role::AvatarSymbol, Role::State, Role::StatusText,
    Role::LastMessage, Role::LastCompletedMessage, Role::LastActivity, Role::ConversationId,
    Role::HeadRevision, Role::ContextTokens, Role::ContextWindow, Role::QueueCount, Role::Alive,
    Role::Busy, Role::Focused, Role::Muted, Role::HeartbeatEnabled, Role::DreamingEnabled,
    Role::Schedules, Role::McpServers, Role::Unread, Role::ParentAgentId, Role::AgentRole,
    Role::HelperState, Role::ChildCount, Role::RunningChildren, Role::BackgroundJobCount,
    Role::SubAgentCount, Role::ProcessCount,
];

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Signal {
    CountChanged,
}

/// One roster row exactly as views see it.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct AgentRow {
    pub agent_id: String,
    pub session: String,
    pub name: String,
    pub backend: String,
    pub working_directory: String,
    pub model: String,
    pub effort: String,
    pub avatar_url: String,
    pub avatar_symbol: String,
    pub state: String,
    pub status_text: String,
    pub last_message: String,
    pub last_completed_message: String,
    pub last_activity: i64,
    pub conversation_id: String,
    pub head_revision: i64,
    pub context_tokens: i64,
    pub context_window: i64,
    pub queue_count: i32,
    pub queue_paused: bool,
    pub alive: bool,
    pub busy: bool,
    pub focused: bool,
    pub muted: bool,
    pub heartbeat_enabled: bool,
    pub dreaming_enabled: bool,
    pub schedules: Vec<Value>,
    pub mcp_servers: Vec<Value>,
    pub unread: bool,
    pub parent_agent_id: String,
    pub agent_role: String,
    pub helper_state: String,
    pub child_count: i32,
    pub running_children: i32,
    pub background_job_count: i32,
    pub sub_agent_count: i32,
    pub process_count: i32,
}

pub type Op = ListOp<AgentRow, Role, Signal>;

#[derive(Debug, Default)]
pub struct Roster {
    agents: Vec<Agent>,
    by_session: HashMap<String, usize>,
    pending_queue_events: HashMap<String, Object>,
    outgoing_ranks: HashMap<String, (u64, i64)>,
    live_job_counts: HashMap<String, JobCounts>,
    running_helpers_by_parent: HashMap<String, i32>,
    live_jobs_known: bool,
    outgoing_counter: u64,
    archived_only: bool,
    transport_available: bool,
    ops: Vec<Op>,
}

impl Roster {
    pub fn new() -> Self {
        Self::default()
    }

    /// The archive view: only archived agents, never created ones.
    pub fn archived() -> Self {
        Self { archived_only: true, ..Self::default() }
    }

    pub fn take_ops(&mut self) -> Vec<Op> {
        std::mem::take(&mut self.ops)
    }

    pub fn len(&self) -> usize {
        self.agents.len()
    }
    pub fn is_empty(&self) -> bool {
        self.agents.is_empty()
    }
    pub fn agents(&self) -> &[Agent] {
        &self.agents
    }

    pub fn rows(&self) -> Vec<AgentRow> {
        self.agents.iter().map(|agent| self.present(agent)).collect()
    }

    pub fn present(&self, agent: &Agent) -> AgentRow {
        let live = self.transport_available;
        let counts = self.job_counts(agent);
        AgentRow {
            agent_id: agent.agent_id.clone(),
            session: agent.session.clone(),
            name: display_name(agent).to_owned(),
            backend: agent.backend.clone(),
            working_directory: agent.working_directory.clone(),
            model: agent.model.clone(),
            effort: agent.effort.clone(),
            avatar_url: agent.avatar_url.clone(),
            avatar_symbol: agent.avatar_symbol.clone(),
            state: if live { agent.latest_state.clone() } else { "offline".into() },
            status_text: if live { agent.status_text.clone() } else { String::new() },
            last_message: agent.last_message.clone(),
            last_completed_message: agent.last_completed_message.clone(),
            last_activity: agent.last_activity,
            conversation_id: agent.conversation_id.clone(),
            head_revision: agent.head_revision,
            context_tokens: agent.context_tokens,
            context_window: agent.context_window,
            queue_count: agent.queued_turn_count,
            queue_paused: agent.queue_paused,
            alive: live && agent.alive,
            busy: live && agent.busy,
            focused: agent.focused,
            muted: agent.muted,
            heartbeat_enabled: agent.heartbeat_enabled,
            dreaming_enabled: agent.dreaming_enabled,
            schedules: agent.schedules.clone(),
            mcp_servers: agent.mcp_servers.clone(),
            unread: agent.unread,
            parent_agent_id: agent.parent_agent_id.clone(),
            agent_role: agent.role.clone(),
            helper_state: agent.helper_state.clone(),
            child_count: agent.child_count,
            running_children: if live { self.running_children(agent) } else { 0 },
            background_job_count: if live { counts.total } else { 0 },
            sub_agent_count: if live { counts.sub_agents } else { 0 },
            process_count: if live { self.running_work(agent) } else { 0 },
        }
    }

    fn rebuild_index(&mut self) {
        self.by_session =
            self.agents.iter().enumerate().map(|(row, a)| (a.session.clone(), row)).collect();
    }

    fn notify_row(&mut self, row: usize, roles: &[Role]) {
        let item = self.present(&self.agents[row]);
        self.ops.push(Op::Update { first: row, items: vec![item], roles: roles.to_vec() });
    }

    fn notify_all(&mut self, roles: &[Role]) {
        if !self.agents.is_empty() {
            self.ops.push(Op::Update { first: 0, items: self.rows(), roles: roles.to_vec() });
        }
    }

    fn move_row(&mut self, from: usize, to: usize) {
        self.ops.push(Op::Move { from, to });
        let agent = self.agents.remove(from);
        self.agents.insert(to, agent);
        self.rebuild_index();
    }

    fn insert_row(&mut self, at: usize, agent: Agent) {
        self.ops.push(Op::Insert { at, rows: vec![self.present(&agent)] });
        self.agents.insert(at, agent);
        self.rebuild_index();
    }

    fn remove_row(&mut self, at: usize) {
        self.ops.push(Op::Remove { at, count: 1 });
        self.agents.remove(at);
        self.rebuild_index();
    }

    fn running_map(agents: &[Agent]) -> HashMap<String, i32> {
        let mut running: HashMap<String, i32> = HashMap::new();
        for agent in agents.iter().filter(|a| a.helper_running()) {
            *running.entry(agent.parent_agent_id.clone()).or_default() += 1;
        }
        running
    }

    fn recount_helpers(&mut self) {
        let running = Self::running_map(&self.agents);
        if running == self.running_helpers_by_parent {
            return;
        }
        let touched: HashSet<String> =
            running.keys().chain(self.running_helpers_by_parent.keys()).cloned().collect();
        self.running_helpers_by_parent = running;
        for row in 0..self.agents.len() {
            if touched.contains(&self.agents[row].agent_id) {
                self.notify_row(row, &[Role::RunningChildren, Role::ProcessCount]);
            }
        }
    }

    // ---- mutations -------------------------------------------------------

    pub fn upsert_created_agent(&mut self, object: &Object) -> bool {
        let mut agent = Agent::from_json(object);
        if agent.session.is_empty() || agent.agent_id.is_empty() || self.archived_only {
            return false;
        }
        self.transport_available = true;
        if let Some(row) = self.index_of_session(&agent.session) {
            agent.unread = self.agents[row].unread;
            self.agents[row] = agent;
            self.notify_row(row, &ALL_ROLES);
        } else {
            self.insert_row(0, agent);
            self.ops.push(Op::Signal(Signal::CountChanged));
        }
        self.recount_helpers();
        true
    }

    pub fn apply_snapshot(&mut self, snapshot: &Object) {
        self.transport_available = true;
        let mut next: Vec<Agent> = Vec::new();
        let mut seen: HashSet<String> = HashSet::new();
        for value in json::array(snapshot, "agents") {
            let Some(object) = value.as_object() else { continue };
            let mut agent = Agent::from_json(object);
            if agent.session.is_empty() || agent.archived != self.archived_only {
                continue;
            }
            // A session is one row: a snapshot listing it twice keeps the
            // first (reordering assumes unique sessions).
            if !seen.insert(agent.session.clone()) {
                eprintln!("Roster: the snapshot lists {} more than once; keeping the first", agent.session);
                continue;
            }
            if self.outgoing_ranks.get(&agent.session).is_some_and(|(_, at)| agent.last_activity > *at) {
                self.outgoing_ranks.remove(&agent.session);
            }
            if let Some(old) = self.find(&agent.session) {
                agent.unread = old.unread;
                agent.last_activity = agent.last_activity.max(old.last_activity);
                if !agent.conversation_id.is_empty()
                    && agent.conversation_id == old.conversation_id
                    && agent.head_revision < old.head_revision
                {
                    agent.head_revision = old.head_revision;
                    agent.last_message = old.last_message.clone();
                    agent.last_completed_message = old.last_completed_message.clone();
                }
                if agent.latest_state_timestamp < old.latest_state_timestamp {
                    agent.latest_state_timestamp = old.latest_state_timestamp;
                    agent.latest_state = old.latest_state.clone();
                    agent.status_text = old.status_text.clone();
                    agent.busy = old.busy;
                }
                if agent.queue_revision < old.queue_revision {
                    agent.queue_revision = old.queue_revision;
                    agent.queued_turn_count = old.queued_turn_count;
                    agent.queue_paused = old.queue_paused;
                }
            }
            if let Some(pending) = self.pending_queue_events.remove(&agent.session) {
                let revision = json::integer(&pending, "queue_revision");
                if revision >= agent.queue_revision {
                    agent.queue_revision = revision;
                    agent.queued_turn_count = json::integer(&pending, "queue_depth") as i32;
                    agent.queue_paused = json::boolean(&pending, "queue_paused");
                }
            }
            next.push(agent);
        }

        let rank = |session: &str, ranks: &HashMap<String, (u64, i64)>| ranks.get(session).map_or(0, |r| r.0);
        next.sort_by(|left, right| {
            rank(&right.session, &self.outgoing_ranks)
                .cmp(&rank(&left.session, &self.outgoing_ranks))
                .then_with(|| right.last_activity.cmp(&left.last_activity))
                .then_with(|| name_order(display_name(left), display_name(right)))
                .then_with(|| left.session.cmp(&right.session))
        });

        let desired: HashSet<String> = next.iter().map(|a| a.session.clone()).collect();
        self.outgoing_ranks.retain(|session, _| desired.contains(session));
        let mut structure_changed = false;
        for row in (0..self.agents.len()).rev() {
            if !desired.contains(&self.agents[row].session) {
                self.remove_row(row);
                structure_changed = true;
            }
        }
        // Parent rows count their helpers, so the counts must be current
        // before the first row notification below reaches a view.
        self.running_helpers_by_parent = Self::running_map(&next);

        for (desired_row, agent) in next.into_iter().enumerate() {
            match self.index_of_session(&agent.session) {
                None => {
                    self.insert_row(desired_row, agent);
                    structure_changed = true;
                }
                Some(current_row) => {
                    if current_row != desired_row {
                        self.move_row(current_row, desired_row);
                        structure_changed = true;
                    }
                    self.agents[desired_row] = agent;
                    self.notify_row(desired_row, &ALL_ROLES);
                }
            }
        }
        if structure_changed {
            self.ops.push(Op::Signal(Signal::CountChanged));
        }
    }

    pub fn apply_state_event(&mut self, event: &Object) {
        let Some(row) = self.index_of_session(&json::string(event, "session")) else { return };
        let agent = &mut self.agents[row];
        if let Some(kind) = event.get("kind").and_then(Value::as_str) {
            agent.latest_state = kind.to_owned();
        }
        agent.status_text = json::string(event, "status_text");
        agent.latest_state_timestamp = json::integer(event, "ts");
        agent.busy = is_busy_state(&agent.latest_state);
        self.notify_row(row, &[Role::State, Role::StatusText, Role::Busy]);
    }

    pub fn apply_focus_event(&mut self, event: &Object) {
        let selected = json::string(event, "session");
        for row in 0..self.agents.len() {
            let focused = !selected.is_empty() && self.agents[row].session == selected;
            if self.agents[row].focused != focused {
                self.agents[row].focused = focused;
                self.notify_row(row, &[Role::Focused]);
            }
        }
    }

    pub fn apply_queue_event(&mut self, event: &Object) {
        let session = json::string(event, "session");
        let revision = json::integer(event, "queue_revision");
        let Some(row) = self.index_of_session(&session) else {
            let pending = self.pending_queue_events.get(&session).map_or(0, |e| json::integer(e, "queue_revision"));
            if !session.is_empty() && revision >= pending {
                self.pending_queue_events.insert(session, event.clone());
            }
            return;
        };
        let agent = &mut self.agents[row];
        if (revision == 0 && agent.queue_revision > 0) || revision < agent.queue_revision {
            return;
        }
        agent.queue_revision = revision;
        agent.queued_turn_count = json::integer(event, "queue_depth") as i32;
        agent.queue_paused = json::boolean(event, "queue_paused");
        self.notify_row(row, &[Role::QueueCount]);
    }

    /// `user-notification` is the only unread decision.
    pub fn apply_notification_event(&mut self, event: &Object) {
        if event.get("unread").and_then(Value::as_bool) == Some(false) {
            return;
        }
        let Some(row) = self.index_of_session(&json::string(event, "session")) else { return };
        if self.agents[row].unread || self.agents[row].quiet {
            return;
        }
        self.agents[row].unread = true;
        self.notify_row(row, &[Role::Unread]);
    }

    pub fn clear_unread(&mut self, session: &str) {
        let Some(row) = self.index_of_session(session) else { return };
        if !self.agents[row].unread {
            return;
        }
        self.agents[row].unread = false;
        self.notify_row(row, &[Role::Unread]);
    }

    pub fn mark_transport_unavailable(&mut self) {
        if !self.transport_available {
            return;
        }
        self.transport_available = false;
        self.notify_all(&[
            Role::State, Role::StatusText, Role::Alive, Role::Busy, Role::RunningChildren,
            Role::BackgroundJobCount, Role::SubAgentCount, Role::ProcessCount,
        ]);
    }

    /// Live counts from the job tracker replace the snapshot's once known.
    pub fn apply_live_job_counts(&mut self, by_agent: HashMap<String, JobCounts>) {
        let changed: Vec<usize> = (0..self.agents.len())
            .filter(|&row| {
                let agent = &self.agents[row];
                self.job_counts(agent) != by_agent.get(&agent.agent_id).copied().unwrap_or_default()
            })
            .collect();
        self.live_job_counts = by_agent;
        self.live_jobs_known = true;
        for row in changed {
            self.notify_row(row, &[Role::BackgroundJobCount, Role::SubAgentCount, Role::ProcessCount]);
        }
    }

    pub fn clear_live_job_counts(&mut self) {
        if !self.live_jobs_known {
            return;
        }
        self.live_jobs_known = false;
        self.live_job_counts.clear();
        self.notify_all(&[Role::BackgroundJobCount, Role::SubAgentCount, Role::ProcessCount]);
    }

    /// Move a chat the user just wrote to to the top until the Host reports
    /// newer activity for it.
    pub fn record_outgoing_activity(&mut self, session: &str) -> bool {
        let Some(row) = self.index_of_session(session) else { return false };
        self.outgoing_counter += 1;
        self.outgoing_ranks.insert(session.to_owned(), (self.outgoing_counter, self.agents[row].last_activity));
        if row > 0 {
            self.move_row(row, 0);
        }
        true
    }

    // ---- queries ---------------------------------------------------------

    pub fn index_of_session(&self, session: &str) -> Option<usize> {
        self.by_session.get(session).copied()
    }

    pub fn find(&self, session: &str) -> Option<&Agent> {
        self.index_of_session(session).map(|row| &self.agents[row])
    }

    pub fn find_by_agent_id(&self, agent_id: &str) -> Option<&Agent> {
        (!agent_id.is_empty()).then(|| self.agents.iter().find(|a| a.agent_id == agent_id)).flatten()
    }

    /// Helpers whose parent is `agent_id`, in list order.
    pub fn helpers_of(&self, agent_id: &str) -> Vec<&Agent> {
        if agent_id.is_empty() {
            return Vec::new();
        }
        self.agents.iter().filter(|a| a.is_helper() && a.parent_agent_id == agent_id).collect()
    }

    pub fn sessions(&self) -> Vec<String> {
        self.agents.iter().map(|a| a.session.clone()).collect()
    }

    /// A fresh Host lists only its janitors; prefer a working agent.
    pub fn first_session(&self) -> Option<&str> {
        self.agents
            .iter()
            .find(|a| !a.janitor)
            .or_else(|| self.agents.first())
            .map(|a| a.session.as_str())
    }

    pub fn next_attention_session(&self, current: &str, pending: &[String]) -> Option<String> {
        self.attention_queue(current, pending).into_iter().next()
    }

    /// Every agent that wants the reader, in the order Ctrl+J (next
    /// attention) visits them from `current`: the roster order, starting
    /// after it and wrapping around. A quiet agent (a janitor) never does.
    pub fn attention_queue(&self, current: &str, pending: &[String]) -> Vec<String> {
        let count = self.agents.len();
        if count == 0 {
            return Vec::new();
        }
        // An unknown current starts the scan at the first row.
        let start = self.index_of_session(current).unwrap_or(count - 1);
        (1..=count)
            .map(|offset| &self.agents[(start + offset) % count])
            .filter(|agent| {
                let wants = agent.unread || agent.latest_state == "waiting" || pending.contains(&agent.session);
                agent.session != current && !agent.archived && !agent.quiet && wants
            })
            .map(|agent| agent.session.clone())
            .collect()
    }

    pub fn display_state(&self, session: &str) -> Option<String> {
        let agent = self.find(session)?;
        Some(if self.transport_available { agent.latest_state.clone() } else { "offline".into() })
    }

    pub fn job_counts(&self, agent: &Agent) -> JobCounts {
        if self.live_jobs_known {
            return self.live_job_counts.get(&agent.agent_id).copied().unwrap_or_default();
        }
        // Since Host contract 16 the snapshot counts processes apart from
        // running helpers; fold it into the live job-list shape.
        JobCounts {
            total: agent.background_job_count + agent.background_sub_agent_count,
            sub_agents: agent.background_sub_agent_count,
        }
    }

    /// Running helpers: the Host's `running_children`, or the helpers visible
    /// in this list when that is larger (or the Host predates the field).
    pub fn running_children(&self, agent: &Agent) -> i32 {
        if agent.agent_id.is_empty() {
            return agent.running_children;
        }
        agent.running_children.max(self.running_helpers_by_parent.get(&agent.agent_id).copied().unwrap_or(0))
    }

    /// Background processes plus running helpers, each helper counted once
    /// (a helper shows up both as a running child and as its mirror job).
    pub fn running_work(&self, agent: &Agent) -> i32 {
        let counts = self.job_counts(agent);
        (counts.total - counts.sub_agents) + counts.sub_agents.max(self.running_children(agent))
    }
}

/// What the process popover lists for one agent: active `jobs` and running
/// `helpers`, plus the counts behind the list and header indicators.
pub fn describe_agent_processes(roster: &Roster, jobs: &JobTracker, session: &str, now_ms: i64) -> Option<Object> {
    let agent = roster.find(session)?;
    let job_rows: Vec<Value> = if jobs.loaded() {
        jobs.active_jobs(&agent.agent_id, &agent.session)
            .into_iter()
            .map(|job| {
                let started = json::integer(job, "started_at");
                let heartbeat = json::integer(job, "heartbeat_at");
                let sub_agent = is_sub_agent(job);
                let mut title = json::string(job, "title").trim().to_owned();
                if title.is_empty() {
                    title = if sub_agent { "Sub-agent" } else { "Background job" }.into();
                }
                json!({
                    "jobId": json::string(job, "job_id"),
                    "kind": json::string(job, "kind"),
                    "subAgent": sub_agent,
                    "title": title,
                    "detail": json::string(job, "detail"),
                    "status": json::string(job, "status"),
                    "elapsed": if started > 0 { compact_duration(now_ms - started) } else { String::new() },
                    "heartbeat": if heartbeat > 0 { format!("{} ago", compact_duration(now_ms - heartbeat)) } else { String::new() },
                })
            })
            .collect()
    } else {
        Vec::new()
    };
    let helper_rows: Vec<Value> = roster
        .helpers_of(&agent.agent_id)
        .into_iter()
        .filter(|h| h.helper_running())
        .map(|helper| {
            json!({
                "session": helper.session,
                "name": display_name(helper),
                "statusText": helper.status_text,
                "state": roster.display_state(&helper.session).unwrap_or_default(),
            })
        })
        .collect();
    let counts = roster.job_counts(agent);
    json!({
        "jobs": job_rows,
        "helpers": helper_rows,
        "jobCount": counts.total,
        "subAgentCount": counts.sub_agents,
        "runningChildren": roster.running_children(agent),
        "total": roster.running_work(agent),
    })
    .as_object()
    .cloned()
}

/// How well a quick-switcher row matches `needle` (C++ `matchingAgents`):
/// the exact name, then a name that starts with it, then one with a word
/// starting with it, then any name containing it, then the session. Enter
/// opens the first row, so typing "Ada" must open Ada, not a more recently
/// active Adam; rows keep their recency order within a rank.
pub fn switcher_rank(name: &str, session: &str, needle: &str) -> u8 {
    let (name, needle) = (name.to_lowercase(), needle.trim().to_lowercase());
    if name == needle {
        return 0;
    }
    if name.starts_with(&needle) {
        return 1;
    }
    let word_start = name.match_indices(&needle).any(|(at, _)| at > 0 && name[..at].chars().next_back().is_some_and(|c| !c.is_alphanumeric()));
    if word_start {
        2
    } else if name.contains(&needle) {
        3
    } else if session.to_lowercase().contains(&needle) {
        4
    } else {
        5
    }
}
