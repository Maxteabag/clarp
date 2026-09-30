//! The agent profile panel (the Qt controller's profile, voices, media,
//! orchestrator and agent settings): an agent's task plan, heartbeat,
//! prompt history, gallery, voice and model, and the settings it changes.
//! Audio playback is the UI's: a voice preview only asks the Host for it.

use std::collections::{HashMap, HashSet};
use std::path::PathBuf;

use clarp_core::directory::Voice;
use clarp_core::json::{self, Object};
use clarp_core::protocol::display_name;
use serde_json::{Value, json};

use crate::{Change, Engine};

#[derive(Default)]
pub(crate) struct Profile {
    session: String,
    task_plan: Object,
    heartbeat: Object,
    prompts: Vec<Value>,
    prompt_cursor: String,
    prompts_have_more: bool,
    prompts_loading: bool,
    loading: bool,
    error: String,
    generation: u64,
    prompt_history_generation: u64,
    /// tag → (session, generation, load_more)
    prompt_history_requests: HashMap<String, (String, u64, bool)>,

    voices: Vec<Voice>,
    voice_bio: String,
    voices_loading: bool,

    orchestrator_settings: Object,
    orchestrator_last_decision: String,
    orchestrator_loading: bool,

    media_revision: u64,
    media_generations: HashMap<String, u64>,
    /// tag → (session, generation)
    media_list_requests: HashMap<String, (String, u64)>,
    media_assets: HashMap<String, Vec<Value>>,
    /// asset id → cached file URL
    media_sources: HashMap<String, String>,
    /// tag → (asset id, session)
    media_content_requests: HashMap<String, (String, String)>,
    media_directory: Option<PathBuf>,

    workspace: clarp_core::workspace::WorkspaceContext,
}


impl Engine {
    // ---- profile ---------------------------------------------------------

    pub fn profile_session(&self) -> &str {
        &self.profile.session
    }
    pub fn profile_task_plan(&self) -> &Object {
        &self.profile.task_plan
    }
    pub fn profile_heartbeat(&self) -> &Object {
        &self.profile.heartbeat
    }
    pub fn profile_prompts(&self) -> &[Value] {
        &self.profile.prompts
    }
    pub fn profile_prompts_have_more(&self) -> bool {
        self.profile.prompts_have_more
    }
    pub fn profile_prompts_loading(&self) -> bool {
        self.profile.prompts_loading
    }
    pub fn profile_loading(&self) -> bool {
        self.profile.loading
    }
    pub fn profile_error(&self) -> &str {
        &self.profile.error
    }

    /// Opens an agent's profile: its plan, heartbeat and prompts load, and
    /// with them the updates (artifacts) and teams it shows.
    pub fn load_agent_profile(&mut self, session: &str) {
        if session.is_empty() {
            return;
        }
        let profile = &mut self.profile;
        profile.session = session.to_owned();
        profile.task_plan.clear();
        profile.heartbeat.clear();
        profile.prompts.clear();
        profile.error.clear();
        profile.prompt_cursor.clear();
        profile.prompts_have_more = false;
        profile.loading = true;
        profile.generation += 1;
        let generation = profile.generation;
        self.changes.push(Change::Profile);
        self.api.get(&format!("profile:{generation}:{session}"), "/task-plan", &[("session", session)]);
        self.api.get(&format!("profile-heartbeat:{generation}:{session}"), "/agent-heartbeat/status", &[("session", session)]);
        self.load_prompt_history(session, false);
        self.load_updates();
        self.load_teams();
    }

    /// The first page of prompts, or (`load_more`) the next one.
    pub fn load_prompt_history(&mut self, session: &str, load_more: bool) {
        if session.is_empty()
            || (load_more && (session != self.profile.session || !self.profile.prompts_have_more || self.profile.prompt_cursor.is_empty()))
        {
            return;
        }
        let tag = format!("prompt-history:{}", uuid::Uuid::new_v4());
        let cursor = self.profile.prompt_cursor.clone();
        let profile = &mut self.profile;
        if !load_more {
            profile.session = session.to_owned();
            profile.prompts.clear();
            profile.prompt_cursor.clear();
            profile.prompts_have_more = false;
        }
        profile.prompt_history_generation += 1;
        profile.prompts_loading = true;
        let generation = profile.prompt_history_generation;
        profile.prompt_history_requests.insert(tag.clone(), (session.to_owned(), generation, load_more));
        self.changes.push(Change::Profile);
        let mut query = vec![("session", session), ("limit", "20")];
        if load_more {
            query.push(("before", &cursor));
        }
        self.api.get(&tag, "/identity/prompt-history", &query);
    }

    /// What the profile shows about an agent; None when it is not in the
    /// roster.
    pub fn agent_details(&mut self, session: &str) -> Option<Value> {
        let agent = self.roster.find(session)?.clone();
        let shared = self.shared_filesystem();
        let workspace = self.profile.workspace.describe(&agent.working_directory, shared);
        let state = self.roster.display_state(session).unwrap_or_default();
        Some(json!({
            "agent_id": agent.agent_id, "session": agent.session, "name": display_name(&agent),
            "backend": agent.backend, "working_directory": agent.working_directory, "workspace": workspace,
            "model": agent.model, "effort": agent.effort,
            "default_effort": self.default_effort_for_model(&agent.backend, &agent.model),
            "state": state, "status_text": agent.status_text, "context_tokens": agent.context_tokens,
            "context_window": agent.context_window, "queue_count": agent.queued_turn_count, "muted": agent.muted,
            "heartbeat_enabled": agent.heartbeat_enabled, "dreaming_enabled": agent.dreaming_enabled,
            "schedules": agent.schedules, "mcp_servers": agent.mcp_servers, "team_ids": agent.team_ids,
        }))
    }

    // ---- files and terminal --------------------------------------------------

    /// The `file://` URL the UI opens for the agent's files; None (and the
    /// error shown) when the directory is not on this desktop.
    pub fn agent_files_url(&mut self, session: &str) -> Option<String> {
        let url = self.local_agent_directory(session).and_then(|dir| url::Url::from_file_path(dir).ok()).map(|u| u.to_string());
        if url.is_none() {
            self.agent_files_open_failed();
        }
        url
    }

    /// The UI could not open the agent's directory.
    pub fn agent_files_open_failed(&mut self) {
        self.set_error("The agent directory is not available on this desktop");
    }

    // ---- voices ------------------------------------------------------------

    /// The voices offered to the agent loading in `voices()`, its own
    /// marked `current`.
    pub fn voices(&self) -> &[Voice] {
        &self.profile.voices
    }
    pub fn voice_bio(&self) -> &str {
        &self.profile.voice_bio
    }
    pub fn voices_loading(&self) -> bool {
        self.profile.voices_loading
    }

    pub fn load_voices(&mut self, session: &str) {
        if session.is_empty() {
            return;
        }
        self.profile.voice_bio.clear();
        self.profile.voices_loading = true;
        self.changes.push(Change::Voices);
        self.api.get(&format!("voices:{session}"), "/voices", &[("for", session)]);
    }

    pub fn choose_voice(&mut self, session: &str, voice_id: &str) {
        if session.is_empty() || voice_id.is_empty() {
            return;
        }
        self.api.post_json(&format!("voice-select:{session}"), "/agent-voice", json!({"session": session, "voice_id": voice_id}), None);
    }

    /// Asks the Host to speak a greeting in the voice; the clip arrives
    /// like any other and the UI plays it.
    pub fn preview_voice(&mut self, session: &str, name: &str, voice_id: &str) {
        if session.is_empty() || voice_id.is_empty() {
            return;
        }
        let spoken = if name.is_empty() { session } else { name };
        self.api.post_json(
            &format!("voice-preview:{session}"),
            "/preview",
            json!({"voice_id": voice_id, "session": session, "text": format!("Hi, I'm {spoken}.")}),
            None,
        );
    }

    // ---- orchestrator --------------------------------------------------------

    pub fn orchestrator_settings(&self) -> &Object {
        &self.profile.orchestrator_settings
    }
    /// The latest routing decision as one line, or "No decisions logged yet.".
    pub fn orchestrator_last_decision(&self) -> &str {
        &self.profile.orchestrator_last_decision
    }
    pub fn orchestrator_loading(&self) -> bool {
        self.profile.orchestrator_loading
    }

    pub fn load_orchestrator(&mut self) {
        self.profile.orchestrator_loading = true;
        self.changes.push(Change::Orchestrator);
        self.api.get("orchestrator-load", "/orchestrator/settings", &[]);
    }

    #[allow(clippy::too_many_arguments)]
    pub fn save_orchestrator(&mut self, enabled: bool, fallback_only: bool, confidence: f64, provider: &str, model: &str, effort: &str, timeout_ms: i32) {
        self.profile.orchestrator_loading = true;
        self.changes.push(Change::Orchestrator);
        let body = json!({
            "enabled": enabled, "fallback_only": fallback_only, "confidence_threshold": confidence.clamp(0.5, 0.99),
            "provider": if provider.is_empty() { "openai" } else { provider },
            "model": model.trim(), "effort": effort.trim(), "timeout_ms": timeout_ms.clamp(250, 60_000),
        });
        self.api.post_json("orchestrator-save", "/orchestrator/settings", body, None);
    }

    // ---- media gallery -------------------------------------------------------

    pub fn media_revision(&self) -> u64 {
        self.profile.media_revision
    }
    /// The chat's media assets (`asset_id`, `mime_type`, `url`, …).
    pub fn media_for_session(&self, session: &str) -> &[Value] {
        self.profile.media_assets.get(session).map_or(&[][..], Vec::as_slice)
    }
    /// The cached image's `file://` URL; None until it arrives.
    pub fn media_source(&self, asset_id: &str) -> Option<&str> {
        self.profile.media_sources.get(asset_id).map(String::as_str)
    }
    pub fn resolve_media_markdown(&self, markdown: &str) -> String {
        clarp_core::media::resolve_media_markdown(markdown, &self.profile.media_sources)
    }
    /// Where fetched images are written; by default a per-process folder in
    /// the Rust client's cache.
    pub fn set_media_directory(&mut self, directory: PathBuf) {
        self.profile.media_directory = Some(directory);
    }

    pub fn load_media(&mut self, session: &str) {
        if session.is_empty() {
            return;
        }
        let tag = format!("media-list:{}", uuid::Uuid::new_v4());
        let generation = self.profile.media_generations.get(session).copied().unwrap_or(0) + 1;
        self.profile.media_generations.insert(session.to_owned(), generation);
        self.profile.media_list_requests.insert(tag.clone(), (session.to_owned(), generation));
        self.api.get(&tag, "/media", &[("session", session), ("limit", "100")]);
    }

    fn handle_media_list(&mut self, tag: &str, object: &Object) {
        let Some((session, generation)) = self.profile.media_list_requests.remove(tag) else { return };
        if self.profile.media_generations.get(&session) != Some(&generation) {
            return;
        }
        let assets = json::array(object, "assets");
        let mut fetch = Vec::new();
        for asset in &assets {
            let text = |key: &str| asset.get(key).and_then(Value::as_str).unwrap_or_default();
            let (id, mime, url) = (text("asset_id"), text("mime_type"), text("url"));
            if id.is_empty() || !mime.starts_with("image/") || url.is_empty() || self.profile.media_sources.contains_key(id) {
                continue;
            }
            fetch.push((format!("media-content:{}", uuid::Uuid::new_v4()), id.to_owned(), url.to_owned()));
        }
        self.profile.media_assets.insert(session.clone(), assets);
        for (tag, id, url) in fetch {
            self.profile.media_content_requests.insert(tag.clone(), (id, session.clone()));
            self.api.get_bytes(&tag, &url);
        }
        self.profile.media_revision += 1;
        self.changes.push(Change::Media);
    }

    fn handle_media_bytes(&mut self, tag: &str, bytes: &[u8], content_type: &str) {
        let Some((asset, session)) = self.profile.media_content_requests.remove(tag) else { return };
        let still_current = self
            .profile
            .media_assets
            .get(&session)
            .is_some_and(|assets| assets.iter().any(|a| a.get("asset_id").and_then(Value::as_str) == Some(asset.as_str())));
        let mime = clarp_core::media::mime(content_type);
        if asset.is_empty() || !still_current || bytes.is_empty() || bytes.len() > clarp_core::media::MAX_INLINE_MEDIA_BYTES || !mime.starts_with("image/") {
            return;
        }
        // Image bytes stay out of strings the UI copies: a data URL would be
        // copied each time a transcript row is rebuilt.
        if self.profile.media_directory.is_none() {
            let directory = clarp_core::media::cache_dir().map(|root| root.join(format!("media-{}", std::process::id())));
            self.profile.media_directory = directory;
        }
        let Some(directory) = self.profile.media_directory.clone() else {
            self.set_error("Unable to cache chat images locally");
            return;
        };
        if let Err(error) = std::fs::create_dir_all(&directory) {
            eprintln!("Engine: could not create the media cache: {error}");
            self.set_error("Unable to cache chat images locally");
            return;
        }
        let path = directory.join(clarp_core::media::media_file_name(&self.base_url, &asset));
        if let Err(error) = std::fs::write(&path, bytes) {
            eprintln!("Engine: could not cache {}: {error}", path.display());
            self.set_error("Unable to cache chat image");
            return;
        }
        let url = url::Url::from_file_path(&path).map(|u| u.to_string()).unwrap_or_default();
        self.profile.media_sources.insert(asset, url);
        self.profile.media_revision += 1;
        self.changes.push(Change::Media);
    }

    // ---- replies -------------------------------------------------------------

    pub(crate) fn profile_bytes(&mut self, tag: &str, bytes: &[u8], content_type: &str) -> bool {
        if tag.starts_with("media-content:") {
            self.handle_media_bytes(tag, bytes, content_type);
            return true;
        }
        false
    }

    /// Replies the profile owns (agent settings, releases and the model
    /// catalog are lifecycle's).
    pub(crate) fn profile_json(&mut self, tag: &str, object: &Object) -> bool {
        let fenced = |rest: &str, generation: u64, session: &str| {
            let (g, s) = rest.split_once(':').unwrap_or((rest, ""));
            g.parse::<u64>().ok() == Some(generation) && s == session
        };
        if tag.starts_with("media-list:") {
            self.handle_media_list(tag, object);
        } else if tag.starts_with("voice-preview:") {
            // The clip arrives on the event stream.
        } else if let Some(rest) = tag.strip_prefix("profile:") {
            if fenced(rest, self.profile.generation, &self.profile.session) {
                self.profile.task_plan = json::object(object, "plan");
                self.profile.loading = false;
                self.profile.error.clear();
                self.changes.push(Change::Profile);
            }
        } else if let Some(rest) = tag.strip_prefix("profile-heartbeat:") {
            if fenced(rest, self.profile.generation, &self.profile.session) {
                self.profile.heartbeat = object.clone();
                self.changes.push(Change::Profile);
            }
        } else if tag.starts_with("prompt-history:") {
            let Some((session, generation, load_more)) = self.profile.prompt_history_requests.remove(tag) else { return true };
            if session != self.profile.session || generation != self.profile.prompt_history_generation {
                return true;
            }
            let incoming = json::array(object, "prompts");
            let page = json::object(object, "page");
            let profile = &mut self.profile;
            if load_more {
                let mut seen: HashSet<String> =
                    profile.prompts.iter().filter_map(|p| p.get("turn_id").and_then(Value::as_str)).map(str::to_owned).collect();
                for prompt in incoming {
                    let id = prompt.get("turn_id").and_then(Value::as_str).unwrap_or_default().to_owned();
                    if id.is_empty() || seen.insert(id) {
                        profile.prompts.push(prompt);
                    }
                }
            } else {
                profile.prompts = incoming;
            }
            profile.prompts_have_more = json::boolean(&page, "has_more");
            profile.prompt_cursor = json::string(&page, "next_before");
            profile.prompts_loading = false;
            self.changes.push(Change::Profile);
        } else if let Some(session) = tag.strip_prefix("voices:") {
            let voice_id = self.roster.find(session).map(|a| a.voice_id.clone()).unwrap_or_default();
            self.profile.voices = clarp_core::directory::voices_from_response(object, &voice_id);
            self.profile.voice_bio = json::string(object, "bio");
            self.profile.voices_loading = false;
            self.changes.push(Change::Voices);
        } else if let Some(session) = tag.strip_prefix("voice-select:") {
            let session = session.to_owned();
            self.request_snapshot();
            self.load_voices(&session);
            self.changes.push(Change::AgentMutated(session));
        } else if tag == "orchestrator-load" || tag == "orchestrator-save" {
            let recent = json::array(object, "recent_decisions");
            self.profile.orchestrator_settings = json::object(object, "settings");
            if let Some(decision) = recent.first().and_then(Value::as_object) {
                let action =
                    decision.get("final_action").and_then(Value::as_str).map_or_else(|| json::string(decision, "decision_kind"), str::to_owned);
                let target = decision.get("target_session").and_then(Value::as_str).unwrap_or("none");
                let confidence = decision.get("confidence").and_then(Value::as_f64).unwrap_or(0.0);
                self.profile.orchestrator_last_decision = format!("{action}: {target} ({confidence:.2})");
            } else if object.contains_key("recent_decisions") {
                self.profile.orchestrator_last_decision = "No decisions logged yet.".into();
            }
            self.profile.orchestrator_loading = false;
            self.changes.push(Change::Orchestrator);
        } else {
            return false;
        }
        true
    }

    /// Failures the profile owns. Voice previews and selections, agent
    /// settings, schedule toggles and the model catalog are left to the
    /// engine's error.
    pub(crate) fn profile_failure(&mut self, tag: &str, message: &str, status: u16, detail: &str) -> bool {
        if tag.starts_with("media-list:") || tag.starts_with("media-content:") {
            self.profile.media_list_requests.remove(tag);
            self.profile.media_content_requests.remove(tag);
            eprintln!("Engine: {tag} failed: {message} (HTTP {status})");
        } else if let Some(rest) = tag.strip_prefix("profile:") {
            if rest.split(':').next().and_then(|g| g.parse::<u64>().ok()) == Some(self.profile.generation) {
                self.profile.loading = false;
                self.profile.error = detail.to_owned();
                self.changes.push(Change::Profile);
            }
        } else if tag.starts_with("profile-heartbeat:") {
            // Optional on older Hosts; the profile stays usable without it.
            eprintln!("Engine: {tag} failed: {message} (HTTP {status})");
        } else if tag.starts_with("prompt-history:") {
            let request = self.profile.prompt_history_requests.remove(tag);
            if request.is_some_and(|(_, generation, _)| generation == self.profile.prompt_history_generation) {
                self.profile.prompts_loading = false;
                self.profile.error = detail.to_owned();
                self.changes.push(Change::Profile);
            }
        } else if tag.starts_with("voices:") {
            self.profile.voices_loading = false;
            self.changes.push(Change::Voices);
            self.set_error(detail);
        } else if tag.starts_with("orchestrator-") {
            self.profile.orchestrator_loading = false;
            self.changes.push(Change::Orchestrator);
            self.set_error(detail);
        } else {
            return false;
        }
        true
    }
}
