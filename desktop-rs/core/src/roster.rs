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

/// What patching or replacing one row did.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RowChange {
    /// The row holds the change (or already held it).
    Applied,
    /// The list has no row of that session.
    NotHeld,
    /// The row was put away or taken back: it belongs to the other list,
    /// and the lists' counts changed. Nothing was changed here.
    Moved,
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
    /// The running turn's start (epoch ms), or None: idle, or an older Host.
    pub turn_started_at: Option<i64>,
    /// When the running turn stalled (epoch ms) while the Host offers Stop
    /// for it (`stall::stop_offered`), else None.
    pub stalled_since: Option<i64>,
    /// Why the agent cannot answer now (#918), or None: it can, or the
    /// Host is out of reach (and not updating).
    pub availability: Option<crate::availability::Availability>,
    pub conversation_id: String,
    pub head_revision: i64,
    pub context_tokens: Option<i64>,
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
    /// The chat was a helper's (`helper_chat`, Host contract 177).
    pub helper_chat: bool,
    pub helper_state: String,
    pub child_count: i32,
    pub running_children: i32,
    pub background_job_count: i32,
    pub sub_agent_count: i32,
    pub process_count: i32,
    /// The Host's name in a merged list of several Hosts, else empty.
    pub host: String,
    /// "Moved to <Host>" for an agent that moved (Host contract 125).
    pub moved: String,
}

impl AgentRow {
    /// A helper's chat: its role is `helper`, or the Host says the chat was
    /// a helper's (`helper_chat`, contract 177).
    pub fn is_helpers_chat(&self) -> bool {
        self.agent_role == "helper" || self.helper_chat
    }
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
    /// The Host's snapshot carries `turn_started_at`, so a turn that starts
    /// between snapshots may take its start from the state event.
    turn_clock: bool,
    /// The Host lists the `stalled_turn` feature (contract 149): its stall
    /// fields are read. Off, an older Host shows nothing new.
    stall_feature: bool,
    /// The Host lists `agent_availability` (#918): its `availability` is
    /// read. Off, an older Host shows nothing new.
    availability_feature: bool,
    /// The Host said it restarts for an update (`host-updating`): every
    /// agent's line until the desktop has reconnected and read the list.
    host_updating: Option<crate::availability::Availability>,
    /// The stream dropped since that event: the next snapshot is the Host
    /// back up, and ends it.
    host_updating_down: bool,
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

    /// A list built from agents already merged (`clarp_core::all_hosts`):
    /// their order is kept, and each row's state is its own Host's.
    pub fn from_agents(agents: Vec<Agent>) -> Self {
        let mut roster = Self { transport_available: true, ..Self::default() };
        roster.running_helpers_by_parent = Self::running_map(&agents);
        roster.agents = agents;
        roster.rebuild_index();
        roster
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
            turn_started_at: agent.turn_started_at,
            stalled_since: if live { crate::stall::stop_offered(agent) } else { None },
            availability: self.host_updating.clone().or_else(|| if live { agent.availability.clone() } else { None }),
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
            helper_chat: agent.helper_chat,
            helper_state: agent.helper_state.clone(),
            child_count: agent.child_count,
            running_children: if live { self.running_children(agent) } else { 0 },
            background_job_count: if live { counts.total } else { 0 },
            sub_agent_count: if live { counts.sub_agents } else { 0 },
            process_count: if live { self.running_work(agent) } else { 0 },
            host: agent.host.clone(),
            moved: crate::all_hosts::moved_line(agent),
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
        // A snapshot never says the Host is updating: read after the
        // stream dropped, it is the Host back up (every row is rewritten).
        if std::mem::take(&mut self.host_updating_down) {
            self.host_updating = None;
        }
        let mut next: Vec<Agent> = Vec::new();
        let mut seen: HashSet<String> = HashSet::new();
        for value in json::array(snapshot, "agents") {
            let Some(object) = value.as_object() else { continue };
            self.turn_clock |= object.contains_key("turn_started_at");
            let mut agent = Agent::from_json(object);
            self.gate(&mut agent);
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
            self.keep_newer(&mut agent);
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

    /// What the list holds newer than a row the Host made earlier: this
    /// desktop's unread mark, a later activity, revision, state or queue
    /// an event already brought.
    fn keep_newer(&mut self, agent: &mut Agent) {
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
                agent.turn_started_at = old.turn_started_at;
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
    }

    /// Patches one row with the `fields` of an `agent-roster` event
    /// (feature `roster_fields`): those fields change, every other one
    /// keeps its value, and the row keeps its place.
    pub fn patch_row(&mut self, session: &str, fields: &Object) -> RowChange {
        let Some(row) = self.index_of_session(session) else { return RowChange::NotHeld };
        let mut agent = self.agents[row].clone();
        agent.patch(fields);
        self.gate(&mut agent);
        self.put_row(row, agent)
    }

    /// Replaces one row with the Host's own (`GET /agents/{session}`,
    /// feature `agent_row`), as a snapshot would, in its place.
    pub fn replace_row(&mut self, object: &Object) -> RowChange {
        let mut agent = Agent::from_json(object);
        let Some(row) = self.index_of_session(&agent.session).filter(|_| !agent.session.is_empty()) else { return RowChange::NotHeld };
        self.turn_clock |= object.contains_key("turn_started_at");
        self.gate(&mut agent);
        self.keep_newer(&mut agent);
        self.put_row(row, agent)
    }

    /// Leaves out what a Host that lacks the feature for it sent anyway:
    /// the stall without `stalled_turn`, availability without
    /// `agent_availability`.
    fn gate(&self, agent: &mut Agent) {
        if !self.stall_feature {
            agent.stalled_since = None;
            agent.stalled_action = None;
        }
        if !self.availability_feature {
            agent.availability = None;
        }
    }

    fn put_row(&mut self, row: usize, agent: Agent) -> RowChange {
        if agent.archived != self.archived_only {
            return RowChange::Moved;
        }
        if self.agents[row] != agent {
            self.agents[row] = agent;
            self.notify_row(row, &ALL_ROLES);
            self.recount_helpers();
        }
        RowChange::Applied
    }

    pub fn apply_state_event(&mut self, event: &Object) {
        let Some(row) = self.index_of_session(&json::string(event, "session")) else { return };
        let turn_clock = self.turn_clock;
        let agent = &mut self.agents[row];
        if let Some(kind) = event.get("kind").and_then(Value::as_str) {
            agent.latest_state = kind.to_owned();
        }
        agent.status_text = json::string(event, "status_text");
        agent.latest_state_timestamp = json::integer(event, "ts");
        let was_busy = agent.busy;
        agent.busy = is_busy_state(&agent.latest_state);
        // A turn that starts between snapshots counts from its first busy
        // state; the next snapshot brings the Host's own start.
        // A working state carries its turn's audience and, for a turn
        // another agent started, who (Host contract 124): "Working with".
        if let Some(audience) = event.get("audience").and_then(Value::as_str).filter(|_| agent.busy) {
            agent.working_audience = audience.trim().to_ascii_lowercase();
            agent.working_with = json::string(event, "sender_name").trim().to_owned();
        }
        if !agent.busy {
            agent.turn_started_at = None;
            agent.stalled_since = None;
            agent.stalled_action = None;
            agent.working_audience.clear();
            agent.working_with.clear();
        } else if !was_busy && agent.turn_started_at.is_none() && turn_clock && agent.latest_state_timestamp > 0 {
            agent.turn_started_at = Some(agent.latest_state_timestamp);
        }
        self.notify_row(row, &[Role::State, Role::StatusText, Role::Busy]);
    }

    /// `/server-info` answered: whether the Host offers `stalled_turn`. A
    /// Host without it shows no stall (its next snapshot fills one in).
    pub fn set_stall_feature(&mut self, on: bool) {
        self.stall_feature = on;
        if on {
            return;
        }
        for row in 0..self.agents.len() {
            let agent = &mut self.agents[row];
            let stalled = agent.stalled_since.is_some() || agent.stalled_action.is_some();
            agent.stalled_since = None;
            agent.stalled_action = None;
            if stalled {
                self.notify_row(row, &[Role::State, Role::StatusText]);
            }
        }
    }

    /// `/server-info` answered: whether the Host offers
    /// `agent_availability`. A Host without it shows no availability line;
    /// a Host answering is up, so a `host-updating` line ends.
    pub fn set_availability_feature(&mut self, on: bool) {
        self.availability_feature = on;
        self.host_updating_down = false;
        let updating = self.host_updating.take().is_some();
        for row in 0..self.agents.len() {
            let cleared = !on && self.agents[row].availability.take().is_some();
            if cleared || updating {
                self.notify_row(row, &[Role::State, Role::StatusText]);
            }
        }
    }

    /// A `host-updating` event: the Host restarts for an update, so every
    /// agent says so until the desktop has reconnected and read the list.
    pub fn apply_host_updating(&mut self, event: &Object) {
        if !self.availability_feature {
            return;
        }
        self.host_updating = Some(crate::availability::host_updating(event));
        self.host_updating_down = false;
        self.notify_all(&[Role::State, Role::StatusText]);
    }

    /// A `/log` delta for `session` arrived: the agent's answer in it ends
    /// its availability line (`availability::answered`) before the Host's
    /// `ok` does.
    pub fn apply_log_delta(&mut self, session: &str, log: &Object) {
        let Some(row) = self.index_of_session(session) else { return };
        let Some(availability) = &self.agents[row].availability else { return };
        if crate::availability::answered(log, availability.since_ms) {
            self.agents[row].availability = None;
            self.notify_row(row, &[Role::State, Role::StatusText]);
        }
    }

    /// Why the agent of `session` cannot answer now: the Host updating,
    /// else its own state while the Host is in reach; None otherwise.
    pub fn availability(&self, session: &str) -> Option<&crate::availability::Availability> {
        let agent = self.find(session)?;
        self.host_updating.as_ref().or_else(|| agent.availability.as_ref().filter(|_| self.transport_available))
    }

    /// An `agent-activity` event: a Host of contract 149 or later says on
    /// it whether the turn is stalled, so it sets the stall when one starts
    /// and clears it when it ends. An event without the fields (an older
    /// Host) leaves the row as it is.
    pub fn apply_activity_event(&mut self, event: &Object) {
        if !self.stall_feature || !crate::stall::reported(event) {
            return;
        }
        let Some(row) = self.index_of_session(&json::string(event, "session")) else { return };
        let since = crate::stall::since(event);
        let action = since.and(crate::stall::action(event));
        let agent = &mut self.agents[row];
        if agent.stalled_since == since && agent.stalled_action == action {
            return;
        }
        agent.stalled_since = since;
        agent.stalled_action = action;
        self.notify_row(row, &[Role::State, Role::StatusText]);
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

    /// The Host's own `/turn-queue` answer for `session`: what waits there
    /// now, whatever revision this row held. A count kept from an older
    /// revision (the Host's queue moved or restarted) never outlives it.
    pub fn apply_queue_truth(&mut self, session: &str, count: i32, paused: bool, revision: i64) {
        let Some(row) = self.index_of_session(session) else { return };
        let agent = &mut self.agents[row];
        if agent.queued_turn_count == count && agent.queue_paused == paused {
            return;
        }
        agent.queued_turn_count = count;
        agent.queue_paused = paused;
        if revision > 0 {
            agent.queue_revision = revision;
        }
        self.pending_queue_events.remove(session);
        self.notify_row(row, &[Role::QueueCount]);
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

    /// The event stream dropped and is trying again: nothing shown changes
    /// yet, but the next snapshot is the Host back up and ends a
    /// `host-updating` line.
    pub fn stream_dropped(&mut self) {
        self.host_updating_down |= self.host_updating.is_some();
    }

    pub fn mark_transport_unavailable(&mut self) {
        self.host_updating_down |= self.host_updating.is_some();
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

    /// False after the event stream dropped, until the next snapshot.
    pub fn transport_available(&self) -> bool {
        self.transport_available
    }

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
    /// after it and wrapping around. A quiet agent (a janitor) never does,
    /// and neither does a helper: its parent is where the work is read.
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
                agent.session != current && !agent.archived && !agent.quiet && !agent.is_helper() && wants
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
