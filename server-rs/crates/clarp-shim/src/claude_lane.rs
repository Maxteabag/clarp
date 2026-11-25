//! Control messages for one kept Claude Code process: the stream-json driver of the lane layer
//! ([`crate::lane`]), while the owner's `claude_chat_lane` is on (issue 761).
//!
//! The lane keeps one `claude -p --input-format stream-json` per agent between turns. A turn's
//! message is written to its standard input, the turn ends at the `result` that settles every
//! message written into it (its `user_message_uuids`), and a Stop is Claude Code's own interrupt
//! on the same input, which keeps the process.

use serde::{Deserialize, Serialize};

pub use crate::lane::{Claim, LOGIN_CHANGED, thread_lock};

/// The keys of a Claude lane's start options. The lane's shared keys ([`crate::lane::key`]) are
/// beside them.
pub mod key {
    /// The program to run: Claude Code.
    pub const TOOL_PROGRAM: &str = "program";
    /// Its arguments, the conversation's `--resume` among them.
    pub const ARGS: &str = "args";
    /// Where it runs.
    pub const CWD: &str = "cwd";
    /// The conversation the lane holds.
    pub const SESSION: &str = "session";
    /// Files the lane owns for as long as it lives (the turn's private MCP configuration): it
    /// removes them when it leaves.
    pub const OWNED_FILES: &str = "_clarpOwnedFiles";
    /// Claude Code's credentials file: a lane whose login there changed leaves, as one whose
    /// account in its own file changed does.
    pub const CREDENTIALS_FILE: &str = "_clarpCredentialsFile";
}

/// The `type` of Claude Code's own replies to a control request: they are the lane's, not the
/// turn's, and never reach a spool.
pub const CONTROL_RESPONSE: &str = "control_response";

/// The line that asks Claude Code to interrupt its running turn: it answers with the turn's
/// `result` some 40 ms later and keeps running (measured with 2.1.321 on 24 Nov 2026).
pub fn interrupt(request_id: &str) -> String {
    serde_json::json!({
        "type": "control_request",
        "request_id": request_id,
        "request": {"subtype": "interrupt"},
    })
    .to_string()
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "op", rename_all = "kebab-case")]
pub enum Request {
    /// The lane's state, at once.
    Inspect,
    /// Inspect first, then a snapshot whenever something changes.
    Watch,
    /// Run this attempt: `text` is the turn's stream-json user line, written to Claude Code's
    /// input once the attempt's spool is made.
    Prompt {
        claim: Claim,
        text: String,
        /// The turn's identity for the Clarp tools it calls, written to the lane's turn file.
        #[serde(default, skip_serializing_if = "Option::is_none")]
        token: Option<String>,
    },
    /// Leave once no claim is held; refused while one is.
    Retire,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Snapshot {
    /// The conversation the lane holds.
    pub thread: String,
    /// The lane process's id.
    pub instance: u32,
    pub revision: u64,
    pub available: bool,
    pub claim: Option<Claim>,
    /// The digest of what the Host started the lane with.
    #[serde(default)]
    pub options: String,
    /// Claude Code is started with its model and effort: a lane keeps none of a turn's.
    #[serde(default)]
    pub model: Option<String>,
    #[serde(default)]
    pub effort: Option<String>,
    #[serde(default)]
    pub idle_ms: u64,
    /// How many attempts the lane has taken: none yet means the next is its first.
    #[serde(default)]
    pub claims: u64,
    /// What the lane holds in memory, in bytes, as it weighed itself after its last attempt
    /// (proportional set size of its process and Claude Code's group). None before its first:
    /// the Host counts Claude's typical size until then.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub memory_bytes: Option<u64>,
    /// Whether the lane runs work now: a claim, or (Codex) a turn its tool started by itself or
    /// a goal that may continue. None from a lane that does not say: the Host then never ends
    /// it, only waits for it (issue #772).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub working: Option<bool>,
}

#[cfg(test)]
#[allow(clippy::unwrap_used)]
mod tests {
    use super::{Request, Snapshot, interrupt};
    use crate::lane::{Lifecycle, Status};

    #[test]
    fn the_lifecycle_requests_are_the_claude_lanes_own_lines() {
        for (neutral, claude) in [
            (Lifecycle::Inspect, Request::Inspect),
            (Lifecycle::Retire, Request::Retire),
        ] {
            assert_eq!(
                serde_json::to_string(&neutral).unwrap(),
                serde_json::to_string(&claude).unwrap()
            );
        }
    }

    #[test]
    fn a_claude_snapshot_reads_as_a_status() {
        let snapshot = Snapshot {
            thread: "s".into(),
            instance: 9,
            revision: 2,
            available: true,
            claim: None,
            options: "digest".into(),
            model: None,
            effort: None,
            idle_ms: 5,
            claims: 3,
            memory_bytes: Some(250 << 20),
            working: Some(false),
        };
        let status: Status =
            serde_json::from_value(serde_json::to_value(&snapshot).unwrap()).unwrap();
        assert_eq!(status.thread, "s");
        assert_eq!(status.options, "digest");
        assert_eq!(status.idle_ms, 5);
        assert_eq!(status.working, Some(false));
        assert_eq!(status.memory_bytes, Some(250 << 20));
    }

    #[test]
    fn the_interrupt_is_claude_codes_control_request() {
        let line: serde_json::Value = serde_json::from_str(&interrupt("r1")).unwrap();
        assert_eq!(line["type"], "control_request");
        assert_eq!(line["request_id"], "r1");
        assert_eq!(line["request"]["subtype"], "interrupt");
    }
}
