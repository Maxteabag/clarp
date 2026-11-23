//! The Agent Client Protocol (ACP, version 1), as Clarp speaks it to a tool and reads it back.
//!
//! ACP is JSON-RPC 2.0, one message per line, over a tool's standard input and output. The
//! client (the shim's turn driver) asks the agent to `initialize`, to open or continue a
//! conversation (`session/new`, `session/resume`, `session/load`), to choose its options
//! (`session/set_config_option`) and to answer a prompt (`session/prompt`), and tells it to
//! cancel (`session/cancel`). The agent tells what it does as it goes (`session/update`) and
//! asks the client for what it may not decide alone (`session/request_permission`).
//!
//! [`schema`] holds the messages Clarp sends and reads; [`client`] the JSON-RPC client the
//! shim runs; [`agy`] what is Antigravity's own (`agy acp`): the turn order the Host gives
//! the shim, the limits on what a message carries, and how its failures read. Nothing here
//! knows the Host: the shim and the Host's backend both use it.

pub mod agy;
pub mod client;
pub mod schema;

/// The protocol version this client speaks.
pub const PROTOCOL_VERSION: u64 = 1;

/// The version Clarp gives of itself as a client of the protocol.
pub const CLIENT_VERSION: &str = env!("CARGO_PKG_VERSION");

/// The methods of the protocol Clarp uses.
pub mod method {
    pub const INITIALIZE: &str = "initialize";
    pub const SESSION_NEW: &str = "session/new";
    pub const SESSION_RESUME: &str = "session/resume";
    pub const SESSION_LOAD: &str = "session/load";
    pub const SESSION_SET_CONFIG_OPTION: &str = "session/set_config_option";
    pub const SESSION_SET_MODEL: &str = "session/set_model";
    pub const SESSION_PROMPT: &str = "session/prompt";
    pub const SESSION_CANCEL: &str = "session/cancel";
    pub const SESSION_UPDATE: &str = "session/update";
    pub const REQUEST_PERMISSION: &str = "session/request_permission";
}

/// The error codes Clarp tells apart.
pub mod code {
    /// JSON-RPC: the method is not one the other side serves.
    pub const METHOD_NOT_FOUND: i64 = -32601;
    /// ACP: the agent needs a sign-in first.
    pub const AUTH_REQUIRED: i64 = -32000;
    /// ACP: what was asked for (a conversation) is not there.
    pub const NOT_FOUND: i64 = -32002;
}

/// A name of a conversation that can be used as one component of a path: what a tool names
/// its conversations by, and nothing that could reach outside a directory.
pub fn session_name(name: &str) -> bool {
    let bytes = name.as_bytes();
    bytes.len() <= 128
        && bytes.first().is_some_and(u8::is_ascii_alphanumeric)
        && bytes
            .iter()
            .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'.' | b'_' | b'-'))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_session_name_is_one_component_of_a_path() {
        assert!(session_name("conv-1.a_b"));
        for odd in ["", "../x", "a/b", ".hidden", "-x", &"a".repeat(129)] {
            assert!(!session_name(odd), "{odd:?}");
        }
    }
}
