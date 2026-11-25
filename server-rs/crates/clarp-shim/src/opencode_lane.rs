//! The OpenCode driver's speech on a lane's control socket (issue 763): one `opencode serve`
//! kept per agent between its chat turns, in the lane process, which alone knows the server's
//! password. What every lane says is [`crate::lane`]; this is what an OpenCode lane adds.

use serde::{Deserialize, Serialize};

use crate::lane::Claim;

/// The suffix of an OpenCode lane's control socket in the Host's run folder, after the agent's id.
pub const SOCKET_SUFFIX: &str = "-ocl.sock";

/// What the Host asks of an OpenCode lane.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "op", rename_all = "kebab-case")]
pub enum Request {
    /// The lane's status, at once.
    Inspect,
    /// Leave once no claim is held; refused while one is.
    Retire,
    /// Run a chat turn in the attempt `claim` names. `session` is the agent's OpenCode session,
    /// empty for a new conversation: the lane makes one. A lane takes a session only while no
    /// other process writes it (its thread lock taken exclusive), and keeps it from then on.
    Prompt {
        claim: Claim,
        session: String,
        text: String,
        #[serde(default)]
        model: String,
        #[serde(default)]
        effort: String,
        /// The turn's identity for the Clarp tools it calls, written to the lane's turn file.
        #[serde(default, skip_serializing_if = "Option::is_none")]
        token: Option<String>,
    },
}

/// An OpenCode lane's status: the fields of [`crate::lane::Status`], and its own.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Snapshot {
    /// The session the lane holds; empty until its first claim takes one.
    pub thread: String,
    pub available: bool,
    pub claim: Option<Claim>,
    pub options: String,
    pub model: Option<String>,
    pub effort: Option<String>,
    pub idle_ms: u64,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub memory_bytes: Option<u64>,
    /// Whether the lane runs a claim now (an idle lane that does not let go may be ended).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub working: Option<bool>,
    /// The server's version, as it said it when the lane started.
    #[serde(default)]
    pub version: String,
    /// The server's process id.
    #[serde(default)]
    pub pid: u32,
}

/// Whether OpenCode of this version speaks the session API Clarp's driver reads: the 1.x line.
/// A 2.x server cannot open Clarp's 1.x sessions ("Session not found", 24 Nov 2026).
pub fn spoken(version: &str) -> bool {
    version.split('.').next() == Some("1")
}

#[cfg(test)]
#[allow(clippy::unwrap_used)]
mod tests {
    use super::{Request, Snapshot, spoken};
    use crate::lane::{Lifecycle, Status};

    #[test]
    fn the_lifecycle_lines_are_the_layers_and_a_snapshot_reads_as_a_status() {
        for (neutral, own) in [
            (Lifecycle::Inspect, Request::Inspect),
            (Lifecycle::Retire, Request::Retire),
        ] {
            assert_eq!(
                serde_json::to_string(&neutral).unwrap(),
                serde_json::to_string(&own).unwrap()
            );
        }
        let snapshot = Snapshot {
            thread: "ses_a".into(),
            available: true,
            claim: None,
            options: "d".into(),
            model: None,
            effort: None,
            idle_ms: 5,
            memory_bytes: Some(7),
            working: Some(false),
            version: "1.18.33".into(),
            pid: 9,
        };
        let status: Status =
            serde_json::from_value(serde_json::to_value(&snapshot).unwrap()).unwrap();
        assert_eq!(status.thread, "ses_a");
        assert_eq!(status.memory_bytes, Some(7));
        assert_eq!(status.idle_ms, 5);
        assert_eq!(status.working, Some(false));
    }

    #[test]
    fn only_the_one_line_of_opencode_is_spoken() {
        assert!(spoken("1.18.33"));
        assert!(!spoken("2.1.2"));
        assert!(!spoken(""));
        assert!(!spoken("10.0.0"));
    }
}
