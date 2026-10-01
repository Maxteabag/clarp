//! The Host status the settings surface shows (the Qt controller's
//! `loadSettingsStatus`): diagnostics, speech to text, and the voice
//! providers, with a command to change the voice routing.

use clarp_core::json::Object;
use serde_json::json;

use crate::{Change, Engine};

#[derive(Debug, Default)]
pub(crate) struct HostStatus {
    generation: u64,
    pending: i32,
    diagnostics: Object,
    transcription: Object,
    tts: Object,
}

impl Engine {
    /// Asks the Host for all three again; older answers are ignored.
    pub fn load_host_status(&mut self) {
        let status = &mut self.host_status;
        status.generation += 1;
        status.pending = 3;
        let generation = status.generation;
        self.changes.push(Change::HostStatus);
        self.api.get(&format!("settings-status:{generation}:diagnostics"), "/diagnostics/health", &[]);
        self.api.get(&format!("settings-status:{generation}:transcription"), "/transcription-capabilities", &[]);
        self.api.get(&format!("settings-status:{generation}:tts"), "/tts/providers", &[]);
    }

    pub fn host_status_loading(&self) -> bool {
        self.host_status.pending > 0
    }
    pub fn diagnostics_health(&self) -> &Object {
        &self.host_status.diagnostics
    }
    pub fn transcription_capabilities(&self) -> &Object {
        &self.host_status.transcription
    }
    pub fn tts_provider_status(&self) -> &Object {
        &self.host_status.tts
    }

    /// Routes speech to `provider`, falling back to `fallback` ("none" when
    /// empty).
    pub fn set_tts_providers(&mut self, provider: &str, fallback: &str, voice: &str) {
        let provider = provider.trim();
        if provider.is_empty() {
            return;
        }
        let fallback = if fallback.trim().is_empty() { "none" } else { fallback.trim() };
        self.host_status.pending += 1;
        self.changes.push(Change::HostStatus);
        let body = json!({"provider": provider, "fallback": fallback, "voice": voice.trim()});
        self.api.post_json("settings-action:tts", "/tts/providers", body, None);
    }

    /// True when `tag` was a Host status reply.
    pub(crate) fn host_status_json(&mut self, tag: &str, object: &Object) -> bool {
        if let Some(rest) = tag.strip_prefix("settings-status:") {
            let (generation, kind) = rest.split_once(':').unwrap_or((rest, ""));
            if generation.parse::<u64>().ok() == Some(self.host_status.generation) {
                let status = &mut self.host_status;
                match kind {
                    "diagnostics" => status.diagnostics = object.clone(),
                    "transcription" => status.transcription = object.clone(),
                    "tts" => status.tts = object.clone(),
                    other => eprintln!("Engine: unknown Host status part {other}"),
                }
                status.pending = (status.pending - 1).max(0);
                self.changes.push(Change::HostStatus);
            }
            return true;
        }
        if tag == "settings-action:tts" {
            self.host_status.tts = object.clone();
            self.host_status.pending = (self.host_status.pending - 1).max(0);
            self.changes.push(Change::HostStatus);
            return true;
        }
        false
    }

    pub(crate) fn host_status_failed(&mut self, tag: &str, detail: &str) -> bool {
        if let Some(rest) = tag.strip_prefix("settings-status:") {
            if rest.split(':').next().and_then(|g| g.parse::<u64>().ok()) == Some(self.host_status.generation) {
                self.host_status.pending = (self.host_status.pending - 1).max(0);
                self.changes.push(Change::HostStatus);
            }
            return true;
        }
        if tag == "settings-action:tts" {
            self.host_status.pending = (self.host_status.pending - 1).max(0);
            self.changes.push(Change::HostStatus);
            self.set_error(detail);
            return true;
        }
        false
    }
}
