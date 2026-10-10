//! Starting and changing agents (the Qt controller's "agent launch and
//! lifecycle" and "past sessions, launch directories, paths, assignment"
//! sections): the model catalog, launch defaults, idle contacts, creation
//! and its wait for the roster, per-agent settings, contact assignment and
//! the agent's native CLI in a terminal.

use std::collections::HashSet;
use std::sync::Arc;
use std::time::Duration;

use clarp_core::directory::Contact;
use clarp_core::json::{self, Object};
use clarp_core::protocol::display_name;
use clarp_core::settings::Settings;
use serde_json::{Value, json};

use crate::{Change, Engine, Message};

/// Snapshots asked for while a created session is awaited, before the
/// launch says it is still loading.
const CREATED_SNAPSHOT_ATTEMPTS: u32 = 5;
const CREATED_SNAPSHOT_RETRY: Duration = Duration::from_millis(200);

/// A terminal launch: the launcher program, its arguments and the working
/// directory it starts in.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TerminalCommand {
    pub program: String,
    pub arguments: Vec<String>,
    pub directory: String,
}

/// Runs a [`TerminalCommand`]; replaceable with [`Engine::set_terminal_launcher`].
pub type TerminalLauncher = Arc<dyn Fn(&TerminalCommand) -> std::io::Result<()> + Send + Sync>;

/// Starts the command detached, without the preview-restore variables.
/// `CLARP_TEST_TERMINAL_LOG=<file>` appends the command to that file as a
/// JSON line (`program`, `arguments`, `directory`) instead, so tests never
/// open a real terminal.
pub fn default_terminal_launcher() -> TerminalLauncher {
    Arc::new(|command: &TerminalCommand| {
        if let Some(log) = std::env::var_os("CLARP_TEST_TERMINAL_LOG").filter(|path| !path.is_empty()) {
            use std::io::Write;
            let line = json!({"program": command.program, "arguments": command.arguments, "directory": command.directory});
            let mut file = std::fs::OpenOptions::new().create(true).append(true).open(log)?;
            return writeln!(file, "{line}");
        }
        let mut process = std::process::Command::new(&command.program);
        for variable in clarp_core::launch::RESTORE_VARIABLES {
            process.env_remove(variable);
        }
        process.args(&command.arguments).current_dir(&command.directory).stdin(std::process::Stdio::null()).spawn().map(drop)
    })
}

pub(crate) struct Launch {
    catalog: Object,
    catalog_loaded: bool,
    contacts: Vec<Contact>,
    available_mcp_servers: Vec<Value>,
    starting_contact: String,
    starting_backend: String,
    last_working_directory: String,
    last_backend: String,
    /// The launch dialog's folder; empty means home. Not saved: creating
    /// an agent saves it as the last working directory.
    launch_directory: String,
    /// A session the Host created that the roster does not show yet.
    pending_created_session: String,
    created_snapshot_attempts: u32,
    past_sessions: Vec<Value>,
    past_sessions_loading: bool,
    past_sessions_generation: u64,
    launch_directories: Vec<Value>,
    launch_directories_loading: bool,
    launch_directory_generation: u64,
    directory_suggestions: Vec<Value>,
    favorite_paths: Vec<Value>,
    assignment_session: String,
    assignment_contacts: Vec<Value>,
    terminal: TerminalLauncher,
}

impl Launch {
    pub(crate) fn new(settings: &Settings) -> Self {
        Self {
            catalog: Object::new(),
            catalog_loaded: false,
            contacts: Vec::new(),
            available_mcp_servers: Vec::new(),
            starting_contact: String::new(),
            starting_backend: String::new(),
            last_working_directory: settings.string("launch/workingDirectory", "~"),
            last_backend: settings.string("launch/backend", ""),
            launch_directory: String::new(),
            pending_created_session: String::new(),
            created_snapshot_attempts: 0,
            past_sessions: Vec::new(),
            past_sessions_loading: false,
            past_sessions_generation: 0,
            launch_directories: Vec::new(),
            launch_directories_loading: false,
            launch_directory_generation: 0,
            directory_suggestions: Vec::new(),
            favorite_paths: Vec::new(),
            assignment_session: String::new(),
            assignment_contacts: Vec::new(),
            terminal: default_terminal_launcher(),
        }
    }
}

/// Paths as strings: the Host sends `/dirs` matches as strings and
/// favourites as rows with a `path`.
fn path_strings(values: &[Value]) -> Vec<String> {
    values
        .iter()
        .filter_map(|value| match value {
            Value::String(path) => Some(path.clone()),
            Value::Object(row) => Some(json::string(row, "path")),
            _ => None,
        })
        .filter(|path| !path.is_empty())
        .collect()
}

fn with_llm(mut body: Value, model: &str, effort: &str) -> Value {
    if !model.trim().is_empty() {
        body["model"] = json!(model.trim());
    }
    if !effort.trim().is_empty() {
        body["effort"] = json!(effort.trim());
    }
    body
}

impl Engine {
    // ---- catalog and backends --------------------------------------------

    pub fn model_catalog_loaded(&self) -> bool {
        self.launch.catalog_loaded
    }

    fn load_model_catalog(&mut self) {
        self.api.get("model-catalog", "/agent-model-options", &[]);
    }

    /// Installed providers as `{id, label}`; the built-in three without a catalog.
    pub fn backend_options(&self) -> Vec<Value> {
        clarp_core::catalog::backend_options(&self.launch.catalog)
    }
    pub fn models_for_backend(&self, backend: &str) -> Vec<Value> {
        clarp_core::catalog::models_for_backend(&self.launch.catalog, backend)
    }
    pub fn efforts_for_model(&self, backend: &str, model: &str) -> Vec<Value> {
        clarp_core::catalog::efforts_for_model(&self.launch.catalog, backend, model)
    }
    pub fn default_effort_for_model(&self, backend: &str, model: &str) -> String {
        clarp_core::catalog::default_effort_for_model(&self.launch.catalog, backend, model)
    }
    pub fn backend_supports_resume(&self, backend: &str) -> bool {
        clarp_core::catalog::backend_supports(&self.launch.catalog, backend, "supports_resume")
    }
    pub fn backend_supports_fork(&self, backend: &str) -> bool {
        clarp_core::catalog::backend_supports(&self.launch.catalog, backend, "supports_fork")
    }
    /// The snapshot's MCP servers a new agent may be given.
    pub fn available_mcp_servers(&self) -> &[Value] {
        &self.launch.available_mcp_servers
    }

    // ---- launch defaults -----------------------------------------------------

    pub fn last_backend(&self) -> String {
        self.launch.last_backend.clone()
    }
    pub fn last_working_directory(&self) -> String {
        self.launch.last_working_directory.clone()
    }
    pub fn launch_directory(&self) -> String {
        if self.launch.launch_directory.is_empty() { "~".into() } else { self.launch.launch_directory.clone() }
    }
    pub fn set_launch_directory(&mut self, path: &str) {
        self.launch.launch_directory = path.trim().to_owned();
    }

    /// A fresh Host proposes its workspace root; only a still-default "~"
    /// is replaced, never a folder the user chose.
    fn apply_host_launch_directory_default(&mut self, directory: &str) {
        let directory = directory.trim();
        let last = &self.launch.last_working_directory;
        if directory.is_empty() || (!last.trim().is_empty() && last != "~") {
            return;
        }
        let changed = self.launch.last_working_directory != directory || self.launch.launch_directory != directory;
        self.launch.last_working_directory = directory.to_owned();
        self.launch.launch_directory = directory.to_owned();
        if changed {
            self.changes.push(Change::Launch);
        }
    }

    /// The last launch's backend, else the selected agent's, else Claude.
    pub fn quick_start_backend(&self) -> String {
        if !self.launch.last_backend.is_empty() {
            return self.launch.last_backend.clone();
        }
        let selected = self.roster.find(&self.selected).map(|a| a.backend.clone()).unwrap_or_default();
        if selected.is_empty() { "claude".into() } else { selected }
    }

    // ---- paths -----------------------------------------------------------------

    pub fn past_sessions(&self) -> &[Value] {
        &self.launch.past_sessions
    }
    pub fn past_sessions_loading(&self) -> bool {
        self.launch.past_sessions_loading
    }
    pub fn launch_directories(&self) -> &[Value] {
        &self.launch.launch_directories
    }
    pub fn launch_directories_loading(&self) -> bool {
        self.launch.launch_directories_loading
    }
    pub fn directory_suggestions(&self) -> Vec<String> {
        path_strings(&self.launch.directory_suggestions)
    }
    pub fn favorite_paths(&self) -> Vec<String> {
        path_strings(&self.launch.favorite_paths)
    }

    /// Native sessions a backend can resume in `working_directory` (or in
    /// every project).
    pub fn load_past_sessions(&mut self, working_directory: &str, backend: &str, all_projects: bool) {
        let cwd = working_directory.trim();
        if cwd.is_empty() || backend.is_empty() {
            return;
        }
        self.launch.past_sessions_loading = true;
        self.launch.past_sessions.clear();
        self.launch.past_sessions_generation += 1;
        self.changes.push(Change::Launch);
        let mut query = vec![("cwd", cwd), ("backend", backend)];
        if all_projects {
            query.push(("scope", "all"));
        }
        self.api.get(&format!("past-sessions:{}", self.launch.past_sessions_generation), "/past-sessions", &query);
    }

    pub fn load_launch_directories(&mut self, query: &str) {
        self.launch.launch_directories.clear();
        self.launch.launch_directories_loading = true;
        self.launch.launch_directory_generation += 1;
        self.changes.push(Change::Launch);
        self.api.get(&format!("launch-directories:{}", self.launch.launch_directory_generation), "/launch-directories", &[("q", query)]);
    }

    /// Folders under a typed path; an empty path clears them.
    pub fn request_directory_suggestions(&mut self, prefix: &str) {
        let path = prefix.trim();
        if path.is_empty() {
            if !self.launch.directory_suggestions.is_empty() {
                self.launch.directory_suggestions.clear();
                self.changes.push(Change::Launch);
            }
            return;
        }
        self.api.get("directory-suggestions", "/dirs", &[("path", path)]);
    }

    pub fn load_favorite_paths(&mut self) {
        self.api.get("favorite-paths", "/favorite-paths", &[("limit", "5")]);
    }

    // ---- contacts --------------------------------------------------------------

    /// Idle contacts whose name contains `query`, as `{name, description,
    /// symbol, avatarUrl}`.
    pub fn matching_contacts(&self, query: &str) -> Vec<Value> {
        let needle = query.trim().to_lowercase();
        self.launch
            .contacts
            .iter()
            .filter(|c| c.name.to_lowercase().contains(&needle))
            .map(|c| json!({"name": c.name, "description": c.description, "symbol": c.avatar_symbol, "avatarUrl": c.avatar_url}))
            .collect()
    }

    /// The contact (or "anonymous", "pool", "resume") a launch is starting;
    /// empty when none is.
    pub fn starting_contact(&self) -> String {
        self.launch.starting_contact.clone()
    }

    /// A snapshot applied to the roster: contacts and MCP servers follow
    /// it, and an awaited creation is looked for. True while that creation
    /// is still missing, so the snapshot selects nothing yet.
    pub(crate) fn lifecycle_snapshot(&mut self, object: &Object) -> bool {
        let servers = json::array(object, "available_mcp_servers");
        let active: HashSet<String> = self.roster.agents().iter().map(|a| display_name(a).to_lowercase()).collect();
        let contacts = clarp_core::directory::contacts_from_snapshot(object, &active);
        if servers != self.launch.available_mcp_servers || contacts != self.launch.contacts {
            self.launch.available_mcp_servers = servers;
            self.launch.contacts = contacts;
            self.changes.push(Change::Launch);
        }
        self.check_pending_created()
    }

    // ---- starting agents ---------------------------------------------------------

    /// A launch already created a session the roster has not shown yet: ask
    /// again instead of creating a second agent.
    fn retry_created_agent(&mut self) -> bool {
        if self.launch.pending_created_session.is_empty() {
            return false;
        }
        self.launch.created_snapshot_attempts = 0;
        self.set_error("");
        self.request_snapshot();
        true
    }

    fn post_contact_create(&mut self, starting: &str, backend: &str, mut body: Value) {
        self.set_error("");
        self.launch.starting_contact = starting.to_owned();
        self.launch.starting_backend = backend.to_owned();
        self.changes.push(Change::Launch);
        body["cwd"] = json!(self.launch_directory());
        // A resumed native session only reopens; a new agent speaks unless muted.
        body["synthesize_audio"] = json!(starting != "resume" && !self.muted);
        self.api.post_json("contact-create", "/agents", body, None);
    }

    /// Reopens a backend's native session as an agent, anonymous or as an
    /// available contact.
    pub fn resume_launch_session(&mut self, backend: &str, session_id: &str, anonymous: bool) -> bool {
        if self.retry_created_agent() {
            return true;
        }
        if !self.connected || session_id.is_empty() || !self.launch.starting_contact.is_empty() {
            return false;
        }
        let mut body = json!({"backend": backend, "resume_session_id": session_id, "open_existing": true});
        body[if anonymous { "anonymous" } else { "auto_contact" }] = json!(true);
        self.post_contact_create("resume", backend, body);
        true
    }

    pub fn start_anonymous_agent(&mut self, backend: &str, model: &str, effort: &str) -> bool {
        if self.retry_created_agent() {
            return true;
        }
        if !self.connected || backend.is_empty() || !self.launch.starting_contact.is_empty() {
            return false;
        }
        let body = with_llm(json!({"anonymous": true, "backend": backend}), model, effort);
        self.post_contact_create("anonymous", backend, body);
        true
    }

    /// Starts whichever contact the Host's pool has free; an empty pool
    /// answers with [`Change::LaunchPoolEmpty`].
    pub fn start_available_contact(&mut self, backend: &str, model: &str, effort: &str) -> bool {
        if self.retry_created_agent() {
            return true;
        }
        if !self.connected || backend.is_empty() || !self.launch.starting_contact.is_empty() {
            return false;
        }
        let body = with_llm(json!({"auto_contact": true, "backend": backend}), model, effort);
        self.post_contact_create("pool", backend, body);
        true
    }

    /// Starts an idle contact by name (any case); an empty backend is the
    /// quick-start one.
    pub fn quick_start_contact(&mut self, name: &str, backend: &str, model: &str, effort: &str) -> bool {
        if !self.launch.starting_contact.is_empty() {
            return false;
        }
        if !self.connected {
            self.set_error("Connect to the Host before starting a contact");
            return false;
        }
        let wanted = name.trim().to_lowercase();
        let Some(contact) = self.launch.contacts.iter().find(|c| c.name.to_lowercase() == wanted).map(|c| c.name.clone()) else {
            self.set_error("This contact is no longer idle; choose its existing chat");
            return false;
        };
        let backend = backend.trim();
        let backend = if backend.is_empty() { self.quick_start_backend() } else { backend.to_owned() };
        let body = with_llm(json!({"name": contact, "backend": backend}), model, effort);
        self.post_contact_create(&contact, &backend, body);
        true
    }

    /// Creates a named agent in `working_directory`, remembering the folder
    /// and backend. `replace_session` replaces that agent; `mode` "resume"
    /// or "fork" continues `past_session_id`. MCP servers are chosen by name.
    #[allow(clippy::too_many_arguments)]
    pub fn create_agent(
        &mut self, name: &str, working_directory: &str, backend: &str, model: &str, effort: &str, replace_session: &str, mode: &str,
        past_session_id: &str, mcp_servers: Vec<Value>,
    ) {
        if self.retry_created_agent() {
            return;
        }
        let (name, directory, backend) = (name.trim(), working_directory.trim(), backend.trim());
        if name.is_empty() || directory.is_empty() || backend.is_empty() {
            self.set_error("Name, workspace, and backend are required");
            return;
        }
        let mut body = with_llm(
            json!({"name": name, "session": name.to_lowercase(), "cwd": directory, "backend": backend, "synthesize_audio": !self.muted}),
            model,
            effort,
        );
        let servers: Vec<Value> =
            mcp_servers.iter().filter_map(Value::as_str).filter(|name| !name.is_empty()).map(Value::from).collect();
        if !servers.is_empty() {
            body["mcp_servers"] = Value::Array(servers);
        }
        let changed = self.launch.last_working_directory != directory || self.launch.last_backend != backend;
        self.launch.last_working_directory = directory.to_owned();
        self.launch.last_backend = backend.to_owned();
        self.settings.set("launch/workingDirectory", directory);
        self.settings.set("launch/backend", backend);
        if changed {
            self.changes.push(Change::Launch);
        }
        if !replace_session.is_empty() {
            body["replace_sid"] = json!(replace_session);
        }
        match mode {
            "resume" if !past_session_id.is_empty() => body["resume_session_id"] = json!(past_session_id),
            "fork" if !past_session_id.is_empty() => body["fork_session_id"] = json!(past_session_id),
            _ => {}
        }
        self.api.post_json(&format!("agent-create:{replace_session}"), "/agents", body, None);
    }

    fn created_agent(&mut self, tag: &str, object: &Object) {
        if tag == "contact-create" {
            let backend = self.launch.starting_backend.clone();
            if !backend.is_empty() && self.launch.last_backend != backend {
                self.launch.last_backend = backend.clone();
                self.settings.set("launch/backend", backend);
                self.changes.push(Change::Launch);
            }
            let created_cwd = json::object(object, "agent").get("cwd").and_then(Value::as_str).map(str::to_owned);
            let directory = created_cwd.unwrap_or_else(|| self.launch.launch_directory.clone());
            if !directory.is_empty() && self.launch.last_working_directory != directory {
                self.launch.last_working_directory = directory.clone();
                self.settings.set("launch/workingDirectory", directory);
                self.changes.push(Change::Launch);
            }
            self.launch.starting_backend.clear();
        }
        let session = json::string(object, "session");
        if session.is_empty() {
            self.launch.starting_contact.clear();
            self.changes.push(Change::Launch);
            self.set_error("The Host did not return the new agent's session.");
            return;
        }
        let created = json::object(object, "agent");
        let upserted = json::string(&created, "session") == session && self.mutate_roster(|r| r.upsert_created_agent(&created));
        // Old fleet responses must not undo this authoritative creation.
        self.snapshot_generation += 1;
        self.snapshot_in_flight = false;
        self.snapshot_dirty = false;
        self.freshness.abandoned();
        if upserted {
            self.launch.pending_created_session.clear();
            self.launch.starting_contact.clear();
            self.changes.push(Change::Launch);
            self.finish_created(&session);
            return;
        }
        // Hosts that only return a session id: wait for the roster to show
        // it, ignoring any snapshot already in flight from before the creation.
        self.launch.pending_created_session = session;
        self.launch.created_snapshot_attempts = 0;
        self.force_snapshot();
    }

    fn finish_created(&mut self, session: &str) {
        self.select(session);
        self.changes.push(Change::AgentMutated(session.to_owned()));
    }

    /// After every applied snapshot while a created session is awaited.
    fn check_pending_created(&mut self) -> bool {
        let expected = self.launch.pending_created_session.clone();
        if expected.is_empty() {
            return false;
        }
        if self.roster.find(&expected).is_none() {
            self.launch.created_snapshot_attempts += 1;
            if self.launch.created_snapshot_attempts <= CREATED_SNAPSHOT_ATTEMPTS {
                self.after(CREATED_SNAPSHOT_RETRY, Message::CreatedAgentDue(expected));
            } else {
                self.set_error("Your new agent is still loading. Press Enter to retry.");
            }
            return true;
        }
        self.launch.pending_created_session.clear();
        self.launch.starting_contact.clear();
        self.changes.push(Change::Launch);
        self.finish_created(&expected);
        false
    }

    pub(crate) fn created_agent_due(&mut self, expected: &str) {
        if self.launch.pending_created_session == expected {
            self.request_snapshot();
        }
    }

    // ---- changing agents -----------------------------------------------------------

    fn post_agent_setting(&mut self, session: &str, path: &str, body: Value) {
        self.api.post_json(&format!("agent-setting:{session}"), path, body, None);
    }

    pub fn release_agent(&mut self, session: &str) {
        if session.is_empty() {
            return;
        }
        let path = format!("/agents/{}", clarp_core::endpoint::percent_encode_segment(session));
        self.api.delete(&format!("agent-release:{session}"), &path);
    }

    pub fn set_agent_heartbeat(&mut self, session: &str, enabled: bool) {
        self.post_agent_setting(session, "/agent-heartbeat", json!({"session": session, "heartbeat_enabled": enabled}));
    }

    pub fn set_agent_dreaming(&mut self, session: &str, enabled: bool) {
        self.post_agent_setting(session, "/agent-dreaming", json!({"session": session, "dreaming_enabled": enabled}));
    }

    pub fn set_agent_push_muted(&mut self, session: &str, muted: bool) {
        self.post_agent_setting(session, "/agent-mute", json!({"session": session, "muted": muted}));
    }

    /// Display name only: session and agent ids are stable, so the chat and
    /// every pairing survive a rename.
    pub fn rename_agent(&mut self, session: &str, name: &str) {
        let name = name.trim();
        if session.is_empty() || name.is_empty() {
            return;
        }
        self.post_agent_setting(session, "/agent-rename", json!({"session": session, "name": name}));
    }

    pub fn archive_agent(&mut self, session: &str) {
        self.set_agent_archived(session, true);
    }

    pub fn set_agent_archived(&mut self, session: &str, archived: bool) {
        self.post_agent_setting(session, "/agent-archive", json!({"session": session, "archived": archived}));
    }

    pub fn set_schedule_enabled(&mut self, schedule_id: &str, enabled: bool) {
        let id = schedule_id.trim();
        if !id.is_empty() {
            self.api.post_json("schedule-toggle:", "/agent-schedules/toggle", json!({"schedule_id": id, "enabled": enabled}), None);
        }
    }

    pub fn set_agent_llm(&mut self, session: &str, model: &str, effort: &str) {
        self.post_agent_setting(session, "/agent-llm", json!({"session": session, "model": model, "effort": effort}));
    }

    pub fn compact_session(&mut self, session: &str) {
        self.post_agent_setting(session, "/compact", json!({"session": session}));
    }

    pub fn set_agent_mcp(&mut self, session: &str, servers: &[String]) {
        self.post_agent_setting(session, "/agent-mcp", json!({"session": session, "mcp_servers": servers}));
    }

    /// Sends a failed message again, taking it out of the transcript first.
    pub fn retry_failed_message(&mut self, session: &str, message_id: &str) {
        let text = self.with_conversation(session, |c| c.take_failed_message_for_retry(message_id)).flatten();
        if let Some(text) = text.filter(|t| !t.is_empty()) {
            self.send_to(session, &text, false);
        }
    }

    /// The selected chat's newest failed message, sent again.
    pub fn retry_latest_failed_message(&mut self) {
        if self.selected.is_empty() || self.sending {
            return;
        }
        let failed = self
            .conversations
            .get(&self.selected)
            .and_then(|c| c.rows().iter().rev().find(|r| r.delivery_failed).map(|r| r.id.clone()));
        if let Some(id) = failed {
            let session = self.selected.clone();
            self.retry_failed_message(&session, &id);
        }
    }

    // ---- contact assignment ----------------------------------------------------------

    pub fn assignment_contacts(&self) -> &[Value] {
        &self.launch.assignment_contacts
    }

    pub fn load_assignment_contacts(&mut self, session: &str) {
        self.launch.assignment_session = session.to_owned();
        self.launch.assignment_contacts.clear();
        self.changes.push(Change::Launch);
        self.api.post_json(&format!("assignment-options:{session}"), "/agent-assign", json!({"session": session, "mode": "options"}), None);
    }

    /// Asks the UI to offer contact assignment for a chat.
    pub fn request_contact_assignment(&mut self, session: &str, automatic: bool) {
        self.changes.push(Change::AssignmentRequested { session: session.to_owned(), automatic });
    }

    pub fn assign_contact(&mut self, session: &str, mode: &str, name: &str) {
        if session.is_empty() || !self.connected {
            self.set_error("Connect and select an agent before assigning a contact");
            return;
        }
        self.set_error("");
        self.api.post_json(&format!("agent-assignment:{session}"), "/agent-assign", json!({"session": session, "mode": mode, "name": name}), None);
    }

    // ---- the native CLI --------------------------------------------------------------

    /// Replaces how terminal commands run (tests, other desktops).
    pub fn set_terminal_launcher(&mut self, launcher: TerminalLauncher) {
        self.launch.terminal = launcher;
    }

    /// The agent's working directory on this desktop, when it exists here.
    pub fn local_agent_directory(&self, session: &str) -> Option<std::path::PathBuf> {
        let path = self.roster.find(session).map(|a| a.working_directory.clone()).unwrap_or_default();
        let canonical = std::fs::canonicalize(&path).ok().filter(|_| !path.is_empty())?;
        canonical.is_dir().then_some(canonical)
    }

    /// Opens the agent's own CLI, resuming its conversation, in the default
    /// terminal; only when this desktop shares the Host's filesystem.
    pub fn open_agent_terminal(&mut self, session: &str) {
        if !self.shared_filesystem() {
            self.set_error("The native CLI requires this desktop and Host to share the local filesystem");
            return;
        }
        let Some(agent) = self.roster.find(session).cloned() else { return };
        let (program, arguments) = match clarp_core::links::native_terminal_launch(&agent) {
            Ok(launch) => launch,
            Err(message) => {
                self.set_error(&message);
                return;
            }
        };
        let Some(program_path) = clarp_core::links::find_executable(&program) else {
            self.set_error(&format!("{program} is not installed on this desktop"));
            return;
        };
        let Some(directory) = self.local_agent_directory(session) else {
            self.set_error("The agent directory is not available on this desktop");
            return;
        };
        let directory = directory.to_string_lossy().into_owned();
        let title = format!("{} — {program}", display_name(&agent));
        let xdg = clarp_core::links::find_executable("xdg-terminal-exec").is_some();
        let (launcher, launch_arguments) =
            clarp_core::links::terminal_command(xdg, session, &title, &directory, &program_path.to_string_lossy(), &arguments);
        let command = TerminalCommand { program: launcher, arguments: launch_arguments, directory };
        if let Err(error) = (self.launch.terminal)(&command) {
            eprintln!("Engine: could not start {}: {error}", command.program);
            self.set_error(if xdg { "Could not start the default terminal" } else { "No default terminal launcher was found" });
        }
    }

    // ---- replies -----------------------------------------------------------------------

    /// Replies for launch and lifecycle tags; true when handled. The
    /// server-info reply is observed and passed on.
    pub(crate) fn lifecycle_json(&mut self, tag: &str, object: &Object) -> bool {
        let array = |key: &str| json::array(object, key);
        if tag == "server-info" {
            self.apply_host_launch_directory_default(&json::string(object, "default_cwd"));
            self.load_model_catalog();
            return false;
        }
        if self.queue_json(tag, object) {
            return true;
        }
        if tag == "model-catalog" {
            self.launch.catalog = object.clone();
            self.launch.catalog_loaded = true;
            self.changes.push(Change::Launch);
        } else if tag == "contact-create" || tag.starts_with("agent-create:") {
            self.created_agent(tag, object);
        } else if let Some(session) = tag.strip_prefix("agent-release:").or_else(|| tag.strip_prefix("agent-setting:")) {
            let session = session.to_owned();
            self.request_snapshot();
            self.changes.push(Change::AgentMutated(session));
        } else if tag == "schedule-toggle:" {
            self.request_snapshot();
        } else if let Some(generation) = tag.strip_prefix("past-sessions:") {
            if generation.parse::<u64>().ok() == Some(self.launch.past_sessions_generation) {
                self.launch.past_sessions = array("sessions");
                self.launch.past_sessions_loading = false;
                self.changes.push(Change::Launch);
            }
        } else if let Some(generation) = tag.strip_prefix("launch-directories:") {
            if generation.parse::<u64>().ok() == Some(self.launch.launch_directory_generation) {
                self.launch.launch_directories = array("matches");
                self.apply_host_launch_directory_default(&json::string(object, "home"));
                self.launch.launch_directories_loading = false;
                self.changes.push(Change::Launch);
            }
        } else if tag == "directory-suggestions" {
            self.launch.directory_suggestions = array("matches");
            self.changes.push(Change::Launch);
        } else if tag == "favorite-paths" {
            self.launch.favorite_paths = array("paths");
            self.changes.push(Change::Launch);
        } else if let Some(session) = tag.strip_prefix("assignment-options:") {
            if session == self.launch.assignment_session {
                self.launch.assignment_contacts = array("contacts");
                self.changes.push(Change::Launch);
            }
        } else if let Some(session) = tag.strip_prefix("agent-assignment:") {
            let session = session.to_owned();
            self.request_snapshot();
            self.changes.push(Change::AssignmentSucceeded(session));
        } else {
            return false;
        }
        true
    }

    /// True when the failure is handled (or stale) and must not surface;
    /// other launch failures clear their loading state and fall through to
    /// the error.
    pub(crate) fn lifecycle_failure(&mut self, tag: &str, message: &str, status: u16) -> bool {
        if tag == "contact-create" {
            // The Host's own message, without an HTTP suffix: it explains
            // what to change (a forbidden folder, an occupied contact).
            self.launch.starting_backend.clear();
            self.launch.starting_contact.clear();
            if message == "contact_pool_empty" {
                self.changes.push(Change::LaunchPoolEmpty);
            } else {
                self.set_error(message);
            }
            self.changes.push(Change::Launch);
            return true;
        }
        if let Some(generation) = tag.strip_prefix("past-sessions:") {
            if generation.parse::<u64>().ok() != Some(self.launch.past_sessions_generation) {
                return true;
            }
            self.launch.past_sessions_loading = false;
            self.changes.push(Change::Launch);
        } else if let Some(generation) = tag.strip_prefix("launch-directories:") {
            if generation.parse::<u64>().ok() != Some(self.launch.launch_directory_generation) {
                return true;
            }
            self.launch.launch_directories_loading = false;
            self.changes.push(Change::Launch);
        } else if tag == "directory-suggestions" {
            self.launch.directory_suggestions.clear();
            self.changes.push(Change::Launch);
        } else if tag == "favorite-paths" {
            self.launch.favorite_paths.clear();
            self.changes.push(Change::Launch);
        }
        let detail = if status > 0 { format!("{message} (HTTP {status})") } else { message.to_owned() };
        self.queue_failure(tag, &detail)
    }
}
