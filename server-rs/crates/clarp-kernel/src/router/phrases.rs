//! Bounds and playback for the router's fixed sentences.
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};

pub const MAX_CLIP: usize = 256 * 1024;
pub const MAX_COUNT: usize = 1024;
pub const MAX_BYTES: usize = 64 * 1024 * 1024;

#[derive(Clone, Debug, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub struct Voice {
    pub provider: String,
    pub voice: String,
    pub model: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub language: Option<String>,
}

#[derive(Clone, Debug, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub struct Key {
    pub phrase: String,
    pub voice: Voice,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Clip {
    pub key: Key,
    pub text: String,
    pub name: String,
    pub size: usize,
}

pub fn blob_name(name: &str) -> bool {
    name.strip_prefix("phrase-")
        .and_then(|s| s.strip_suffix(".mp3"))
        .is_some_and(|digest| {
            digest.len() == 64
                && digest
                    .bytes()
                    .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
        })
}

/// Rows are in fill order. Return the oldest prefix to discard.
pub fn excess(clips: &[Clip]) -> usize {
    let mut bytes = 0usize;
    for (count, clip) in clips.iter().rev().enumerate() {
        bytes = bytes.saturating_add(clip.size);
        if count >= MAX_COUNT || bytes > MAX_BYTES {
            return clips.len() - count;
        }
    }
    0
}

pub fn event(name: &str, phrase: &str, session: &str, agent: Option<&str>, voice: &str) -> Value {
    json!({"type":"audio", "url":format!("/audio/{name}"), "session":session,
        "agent_id":agent,"voice_id":voice,"phrase_key":phrase,"cached_phrase":true})
}
