//! The worktree-preview version picker (C++ `PreviewVersions` and
//! `PreviewRelaunch.h`). A python helper lists built versions (`--catalog`)
//! and pins one (`--select`); after a pin the window relaunches itself into
//! the same Host and conversation. This is the Qt-free state; the bridge runs
//! the helper and applies what it returns.

use serde_json::{Value, json};

pub const REFRESH_MS: u64 = 15_000;
pub const PREVIEW_PYTHON: &str = "/usr/bin/python3";

#[derive(Debug, Default)]
pub struct PreviewVersions {
    pub restart_allowed: bool,
    pub selected_session: String,
    pub selected_host: String,
    requested_session: String,
    requested_host: String,
    enabled: bool,
    fixture: bool,
    selecting: bool,
    /// The manager window (`--preview-versions`) never restarts itself.
    manager: bool,
    helper: String,
    running_hash: String,
    error: String,
    notice: String,
    catalog: Value,
}

/// How the app was started, gathered by the caller from its environment.
#[derive(Debug, Default, Clone)]
pub struct Launch {
    pub home: String,
    pub instance_name: String,
    pub manager: bool,
    pub screenshot: bool,
    pub screenshot_scenario: String,
    pub helper_exists: bool,
    /// SHA-256 of the running executable, hex.
    pub executable_hash: String,
}

/// What the finished helper run asks the app to do next.
#[derive(Debug, PartialEq, Eq)]
pub enum Finished {
    Nothing,
    Refresh,
    /// Save settings, then run the relaunch helper and quit.
    Relaunch,
}

/// SHA-256 of a file, hex: identifies the running build.
pub fn file_sha256(path: &std::path::Path) -> Option<String> {
    use sha2::{Digest, Sha256};
    let bytes = std::fs::read(path).ok()?;
    Some(Sha256::digest(bytes).iter().map(|b| format!("{b:02x}")).collect())
}

pub fn helper_path(home: &str) -> String {
    format!("{home}/.local/lib/clarp-desktop-preview/auto_update.py")
}

pub fn relaunch_arguments(helper: &str, pid: u32) -> Vec<String> {
    vec![helper.to_owned(), "--restart-after".into(), pid.to_string()]
}

/// The relaunched window reopens the same Host and conversation.
pub fn relaunch_environment(host: &str, session: &str) -> Vec<(String, String)> {
    vec![
        ("CLARP_BASE_URL".into(), host.into()),
        ("CLARP_RESTORE_SESSION".into(), session.into()),
        ("CLARP_RESTORE_DESKTOP".into(), "1".into()),
    ]
}

impl PreviewVersions {
    pub fn new(launch: &Launch) -> Self {
        let fixture = launch.screenshot && launch.screenshot_scenario == "preview-versions";
        let helper = helper_path(&launch.home);
        let enabled = fixture
            || ((launch.instance_name == "com.maxteabag.Clarp.WorktreePreview" || launch.manager)
                && launch.helper_exists
                && !launch.screenshot);
        let mut state = Self { restart_allowed: true, enabled, fixture, manager: launch.manager, helper, ..Self::default() };
        if !enabled {
            return state;
        }
        state.running_hash = if launch.manager { String::new() } else { launch.executable_hash.clone() };
        if fixture {
            state.running_hash = "old".into();
            state.catalog = json!({"current": "new", "latest": "new", "pinned": "", "versions": [
                {"hash": "new", "label": "v1.1.3 · abcd1234"},
                {"hash": "old", "label": "v1.1.2 · 1234abcd"}]});
        }
        state
    }

    pub fn enabled(&self) -> bool {
        self.enabled
    }
    pub fn fixture(&self) -> bool {
        self.fixture
    }
    pub fn running_hash(&self) -> &str {
        &self.running_hash
    }
    pub fn catalog(&self) -> &Value {
        &self.catalog
    }
    pub fn error(&self) -> &str {
        &self.error
    }
    pub fn notice(&self) -> &str {
        &self.notice
    }
    pub fn helper(&self) -> &str {
        &self.helper
    }
    /// Whether the refresh timer runs at all.
    pub fn polls(&self) -> bool {
        self.enabled && !self.fixture
    }

    pub fn restart_context(&self, pid: u32) -> Value {
        json!({"host": self.requested_host, "session": self.requested_session,
               "arguments": relaunch_arguments(&self.helper, pid)})
    }

    /// Helper arguments for a catalog refresh, when one should run.
    pub fn refresh(&self, busy: bool) -> Option<Vec<String>> {
        (self.enabled && !self.fixture && !busy).then(|| vec![self.helper.clone(), "--catalog".into()])
    }

    /// Helper arguments to pin `hash`, when that should run.
    pub fn select_version(&mut self, hash: &str, busy: bool) -> Option<Vec<String>> {
        if !self.enabled || busy || self.catalog.as_object().is_none_or(|c| c.is_empty()) {
            return None;
        }
        if !self.restart_allowed {
            self.error = "Finish sending, uploading, recording or playback before updating this window.".into();
            return None;
        }
        self.requested_host = self.selected_host.clone();
        self.requested_session = self.selected_session.clone();
        if self.fixture {
            self.error.clear();
            self.notice = "Fixture captured restart request".into();
            return None;
        }
        self.selecting = true;
        self.error.clear();
        self.notice.clear();
        let current = self.catalog.get("current").and_then(Value::as_str).unwrap_or_default().to_owned();
        Some(vec![self.helper.clone(), "--select".into(), hash.into(), "--expected-current".into(), current])
    }

    pub fn start_failed(&mut self) {
        self.error = "Cannot run the preview updater.".into();
    }

    /// Applies a finished helper run. `succeeded` is a zero exit status from
    /// a normal exit; `stdout` is the helper's JSON reply.
    pub fn finished(&mut self, succeeded: bool, stdout: &[u8]) -> Finished {
        let reply = serde_json::from_slice::<Value>(stdout).ok().filter(|v| v.as_object().is_some_and(|o| !o.is_empty()));
        let selecting = std::mem::take(&mut self.selecting);
        let Some(reply) = reply.filter(|_| succeeded) else {
            let fallback = "Update action failed; the open window was kept.";
            self.error = serde_json::from_slice::<Value>(stdout)
                .ok()
                .and_then(|v| v.get("error").and_then(Value::as_str).map(str::to_owned))
                .unwrap_or_else(|| fallback.into());
            return Finished::Nothing;
        };
        if !selecting {
            self.catalog = reply;
            self.error.clear();
            return Finished::Nothing;
        }
        if !reply.get("ok").and_then(Value::as_bool).unwrap_or(false) {
            return Finished::Nothing;
        }
        if self.manager {
            self.notice = "Version selected. Close the preview window and reopen with Super+Alt+A.".into();
            return Finished::Refresh;
        }
        if !self.restart_allowed || self.selected_session != self.requested_session || self.selected_host != self.requested_host {
            self.error = "Version selected. Finish local activity and update again to reopen this conversation.".into();
            return Finished::Nothing;
        }
        Finished::Relaunch
    }

    pub fn settings_unsaved(&mut self) {
        self.error = "Version selected, but settings could not be saved. Window kept open.".into();
    }

    pub fn relaunch_failed(&mut self) {
        self.error = "Version selected. Close and reopen the preview manually.".into();
    }
}
