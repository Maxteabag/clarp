//! The control protocol between the server and a shim.
//!
//! The server talks to a shim over the shim's Unix socket, one JSON object per line. Output never
//! travels this way: output is the spool. The socket is for the few things the server needs to
//! say to a running turn, and for the tool's hooks to report through the one writer of the spool.

use serde::{Deserialize, Serialize};
use serde_json::Value;

/// The environment variable that tells a hook where its shim listens.
pub const SOCKET_ENV: &str = "CLARP_SHIM_SOCKET";

/// The exit code of a shim that found the turn's spool already there: another shim has the turn.
pub const EXIT_TURN_TAKEN: u8 = 125;

/// The exit code of a shim whose program could not be started.
pub const EXIT_NOT_STARTED: u8 = 127;

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "op", rename_all = "kebab-case")]
pub enum Request {
    /// Write these bytes to the tool's standard input.
    Stdin {
        data: String,
    },
    /// Close the tool's standard input: for a tool that reads its prompt until the end.
    CloseStdin,
    /// End the tool: a polite signal to its process group, then a kill after `grace_ms`.
    Stop {
        grace_ms: u64,
    },
    /// A hook of the tool reports what it saw.
    Hook {
        name: String,
        data: Value,
    },
    /// Tell me when there is more in the spool. After the reply, this connection carries nothing
    /// but a newline whenever the spool has grown, and it closes when the shim is gone. A newline
    /// is a hint to read the spool, never the news itself.
    Follow,
    Ping,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Reply {
    pub ok: bool,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub pid: Option<u32>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
}

impl Reply {
    pub fn ok(pid: Option<u32>) -> Self {
        Self {
            ok: true,
            pid,
            error: None,
        }
    }

    pub fn failed(error: impl Into<String>) -> Self {
        Self {
            ok: false,
            pid: None,
            error: Some(error.into()),
        }
    }
}
