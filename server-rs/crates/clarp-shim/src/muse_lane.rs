//! The Muse driver's part of the lane layer's speech on the control socket (issue 762): its
//! requests and its snapshot. What every lane shares is in [`crate::lane`].
//!
//! A Muse lane is one `muse serve` speaking MSP (JSON-RPC 2.0, one object per line) for one
//! agent and one Muse session. It never opens a conversation: `session/start` names only UUIDv7
//! ids and Clarp's are UUIDv4, so a conversation's first turn runs on exec, which names the
//! session, and the lane resumes it afterwards. No turn waits for a cold lane: a lane starts
//! beside an exec turn *without* the session (Muse's own `.session.lock` would refuse one of
//! the two) and takes it with [`Request::Take`] once that turn's process is gone.

use std::io::Read;

use serde::{Deserialize, Serialize};

pub use crate::lane::Claim;

/// The keys of a Muse lane's start options that are its own (beside [`crate::lane::key`]).
pub mod key {
    /// The Muse session the lane is for.
    pub const SESSION: &str = "sessionId";
    /// The agent's working folder: the session's workspace.
    pub const CWD: &str = "cwd";
    /// The model `muse serve` starts with, where the agent names one.
    pub const MODEL: &str = "model";
    /// The settings `muse serve` is started with, each as `-c KEY=VALUE`: the Host names them, so
    /// they are part of what makes one lane differ from another.
    pub const CONFIG: &str = "config";
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(tag = "op", rename_all = "kebab-case")]
pub enum Request {
    /// The lane's state, at once.
    Inspect,
    /// Leave once no claim is held.
    Retire,
    /// Take the session now: the exec turn that held it has ended and its process is gone.
    Take,
    /// Run a chat turn in its already admitted slot. A lane that has not taken its session yet
    /// takes it first.
    Prompt {
        claim: Claim,
        text: String,
        model: Option<String>,
        effort: Option<String>,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        token: Option<String>,
    },
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Snapshot {
    /// The Muse session the lane is for.
    pub thread: String,
    /// The lane holds the session (Muse's lock on it is this process's).
    pub loaded: bool,
    pub instance: u32,
    /// Free for a new claim.
    pub available: bool,
    pub claim: Option<Claim>,
    /// Muse's id of the running turn.
    pub native_turn: Option<String>,
    #[serde(default)]
    pub options: String,
    /// The model the lane last gave Muse, which it keeps for the turns after.
    #[serde(default)]
    pub model: Option<String>,
    #[serde(default)]
    pub effort: Option<String>,
    #[serde(default)]
    pub idle_ms: u64,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub memory_bytes: Option<u64>,
}

/// A UUIDv7, which every MSP command names itself by (`commandId`): the millisecond, then
/// random bits. `None` where the system gives no randomness.
pub fn command_id(now_ms: i64) -> Option<String> {
    let mut bytes = [0u8; 16];
    std::fs::File::open("/dev/urandom")
        .and_then(|mut random| random.read_exact(&mut bytes))
        .ok()?;
    let ms = u64::try_from(now_ms.max(0)).unwrap_or(0).to_be_bytes();
    bytes[..6].copy_from_slice(&ms[2..]);
    bytes[6] = (bytes[6] & 0x0f) | 0x70;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    let hex: String = bytes.iter().map(|b| format!("{b:02x}")).collect();
    Some(format!(
        "{}-{}-{}-{}-{}",
        &hex[..8],
        &hex[8..12],
        &hex[12..16],
        &hex[16..20],
        &hex[20..]
    ))
}

#[cfg(test)]
#[allow(clippy::unwrap_used)]
mod tests {
    use super::{Request, Snapshot, command_id};
    use crate::lane::{Lifecycle, Status};

    #[test]
    fn the_lifecycle_requests_are_the_muse_lanes_own_lines() {
        for (neutral, muse) in [
            (Lifecycle::Inspect, Request::Inspect),
            (Lifecycle::Retire, Request::Retire),
        ] {
            assert_eq!(
                serde_json::to_string(&neutral).unwrap(),
                serde_json::to_string(&muse).unwrap()
            );
        }
    }

    #[test]
    fn a_muse_snapshot_reads_as_a_status() {
        let snapshot = Snapshot {
            thread: "s".into(),
            loaded: true,
            instance: 7,
            available: true,
            claim: None,
            native_turn: None,
            options: "digest".into(),
            model: Some("m".into()),
            effort: None,
            idle_ms: 42,
            memory_bytes: Some(300),
        };
        let status: Status =
            serde_json::from_value(serde_json::to_value(&snapshot).unwrap()).unwrap();
        assert_eq!(status.thread, "s");
        assert_eq!(status.options, "digest");
        assert_eq!(status.memory_bytes, Some(300));
        assert_eq!(status.idle_ms, 42);
    }

    #[test]
    fn a_command_id_is_a_uuid_v7_of_its_millisecond() {
        let id = command_id(0x0123_4567_89ab).unwrap();
        assert_eq!(id.len(), 36);
        assert!(id.starts_with("01234567-89ab-7"), "{id}");
        assert!(matches!(&id[19..20], "8" | "9" | "a" | "b"), "{id}");
        assert_ne!(command_id(1).unwrap(), command_id(1).unwrap());
    }
}
