//! What every lane shares, whichever tool it keeps: a lane is one process under the shim that
//! holds an agent's tool between its turns (issue 761). The Host and the lane process speak over
//! the lane's control socket, one JSON object per line; this is the part of that speech that does
//! not depend on the tool. A tool's driver adds its own requests and its own snapshot fields
//! ([`crate::codex_lane`] is the Codex app-server's), and never changes what is here.
//!
//! The lifecycle a lane shares:
//! - it is started by the Host with options in which the keys of [`key`] are the lane's own, and
//!   taken out before the tool sees them;
//! - its process holds its owner lock ([`owner_lock`]) until its tool has been reaped, so a lane
//!   that has left is known by the lock being free, not by a reply;
//! - it answers [`Control::Inspect`] with a [`Status`] at any time, and leaves on
//!   [`Control::Retire`] only while it holds no claim;
//! - it refuses a claim with [`LOGIN_CHANGED`] once the tool's login changed, and leaves;
//! - it writes the running turn's identity to its turn file, for the tools the turn calls;
//! - it leaves by itself after its idle time.

use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

/// The refusal a lane gives when the tool's login changed since it started. The lane leaves,
/// and nothing of the attempt reached the tool: the Host starts a new lane and offers the same
/// attempt again.
pub const LOGIN_CHANGED: &str = "lane-login-changed";

/// The keys of a lane's start options that are the lane's own, not the tool's: the lane takes
/// them out before it hands the options on.
pub mod key {
    /// Where the locks of native threads are: the thread a lane starts is locked there.
    pub const THREAD_LOCK_DIR: &str = "_clarpThreadLockDir";
    /// A thread's lock, named by the Host for a lane that resumes it.
    pub const THREAD_LOCK: &str = "_clarpThreadLock";
    /// The login pool's lock, held shared while the lane owns a turn.
    pub const LOGIN_LOCK: &str = "_clarpLoginLock";
    /// The tool's login file: a lane whose login changed leaves.
    pub const LOGIN_FILE: &str = "_clarpLoginFile";
    /// How long the lane lives with no claim and no work that may continue, in milliseconds.
    pub const IDLE_MS: &str = "_clarpIdleMs";
    /// Where the identity of the lane's running turn is kept.
    pub const TURN_FILE: &str = "_clarpTurnFile";
    /// The Host's digest of what the lane was started with, told back in every status.
    pub const OPTIONS_DIGEST: &str = "_clarpOptionsDigest";
    /// The only provider the lane's tool may run on, for an agent bound to one of its own.
    pub const MODEL_PROVIDER: &str = "_clarpModelProvider";
}

/// The lock that says who may write a native thread, under `dir`. A turn of its own process
/// takes it shared; a lane takes it exclusive and holds it until its tool has been reaped: one
/// writer per conversation.
pub fn thread_lock(dir: &Path, thread: &str) -> PathBuf {
    dir.join(format!("{:x}.lock", Sha256::digest(thread.as_bytes())))
}

/// The lock a lane's process holds for as long as it, and its tool, live.
pub fn owner_lock(socket: &Path) -> PathBuf {
    socket.with_extension("lock")
}

/// The attempt a lane runs: the actor's turn and attempt, where its spool is written, and the
/// socket its Stop, Follow and Steer come on.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Claim {
    pub turn: u64,
    pub attempt: u32,
    pub spool: PathBuf,
    pub socket: PathBuf,
}

/// The control requests every lane answers, whatever its tool. They are the same lines on the
/// wire as the driver's requests of the same name (Codex's `codex_lane::Request` is a superset:
/// its goal requests are its own). Steered words and a turn's own Stop and Follow come on the
/// claim's socket instead, as [`crate::Request`], as for a turn of its own process.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "op", rename_all = "kebab-case")]
pub enum Control {
    /// The lane's status, at once.
    Inspect,
    /// The lane's status at once, then every change of it on the same connection.
    Watch,
    /// Hand an admitted turn to the tool: the lane makes the claim's spool before anything can
    /// reach the tool, and a lane that has no conversation yet starts one for it.
    Prompt {
        claim: Claim,
        text: String,
        model: Option<String>,
        effort: Option<String>,
        /// The turn's identity for the Clarp tools it calls, written to the lane's turn file.
        #[serde(default, skip_serializing_if = "Option::is_none")]
        token: Option<String>,
    },
    /// Stop the claimed attempt, if it is still the lane's.
    Stop { turn: u64, attempt: u32 },
    /// Leave once no claim is held; refused while one is.
    Retire,
}

/// What any lane's status says, read from its driver's snapshot (whose other fields are the
/// driver's own): enough to choose whether to use it, start it again, or retire it for room.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Status {
    /// The lane's own process id (a Codex snapshot's `instance`): where its memory is read.
    #[serde(default)]
    pub instance: u32,
    /// The conversation the lane holds; empty while a lane started for a new one has none yet.
    pub thread: String,
    /// Free for a new claim.
    pub available: bool,
    pub claim: Option<Claim>,
    /// The digest of the options the Host started the lane with: a lane started otherwise is
    /// retired while idle and started again.
    #[serde(default)]
    pub options: String,
    /// The model and effort the lane last gave its tool.
    #[serde(default)]
    pub model: Option<String>,
    #[serde(default)]
    pub effort: Option<String>,
    /// How long the lane has held no claim, in milliseconds.
    #[serde(default)]
    pub idle_ms: u64,
    /// What the lane holds in memory, in bytes, where its driver reads it itself (proportional
    /// set size of its tool's processes). A lane that says none is read by the Host from its
    /// process tree ([`Status::instance`]).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub memory_bytes: Option<u64>,
}

#[cfg(test)]
#[allow(clippy::unwrap_used)]
mod tests {
    use super::{Claim, Control, Status};
    use crate::codex_lane::{Request, Snapshot};

    #[test]
    fn the_control_requests_are_the_codex_lanes_own_lines() {
        let claim = Claim {
            turn: 3,
            attempt: 1,
            spool: "/s".into(),
            socket: "/t".into(),
        };
        let prompt = |token: Option<&str>| {
            (
                Control::Prompt {
                    claim: claim.clone(),
                    text: "hi".into(),
                    model: Some("m".into()),
                    effort: None,
                    token: token.map(str::to_owned),
                },
                Request::Prompt {
                    claim: claim.clone(),
                    text: "hi".into(),
                    model: Some("m".into()),
                    effort: None,
                    token: token.map(str::to_owned),
                },
            )
        };
        for (neutral, codex) in [
            (Control::Inspect, Request::Inspect),
            (Control::Watch, Request::Watch),
            (Control::Retire, Request::Retire),
            (
                Control::Stop {
                    turn: 3,
                    attempt: 1,
                },
                Request::Stop {
                    turn: 3,
                    attempt: 1,
                },
            ),
            prompt(None),
            prompt(Some("tok")),
        ] {
            assert_eq!(
                serde_json::to_string(&neutral).unwrap(),
                serde_json::to_string(&codex).unwrap()
            );
        }
    }

    #[test]
    fn a_codex_snapshot_reads_as_a_status() {
        let snapshot = Snapshot {
            thread: "t".into(),
            instance: 7,
            revision: 3,
            known_goal: true,
            deferred_goal: false,
            available: true,
            goal: None,
            claim: None,
            native_turn: None,
            options: "digest".into(),
            model: Some("m".into()),
            effort: None,
            idle_ms: 42,
        };
        let status: Status =
            serde_json::from_value(serde_json::to_value(&snapshot).unwrap()).unwrap();
        assert_eq!(
            status,
            Status {
                instance: 7,
                thread: "t".into(),
                available: true,
                claim: None,
                options: "digest".into(),
                model: Some("m".into()),
                effort: None,
                idle_ms: 42,
                memory_bytes: None,
            }
        );
    }
}
