//! The Clarp Host: one process that serves the apps and runs the agents.
//!
//! What is here is the shell around the parts that hold the rules. An agent is a
//! [`clarp_actor::Actor`] on a thread of its own ([`agent`]); its turns run in shims that this
//! process starts and follows but does not own ([`launcher`]); who the agents are is the fleet's
//! to know ([`fleet`]); what changes is numbered and fanned out by the hub ([`hub`]); and the
//! apps talk to all of it over HTTP ([`edge`]), in the words of `clarp-protocol` ([`wire`]).
//!
//! Nothing in this crate decides what an agent does. A thread here asks its actor, does what the
//! actor answers, and tells the hub what changed.

// The snapshot's row is one literal with every field of the contract.
#![recursion_limit = "256"]

pub mod admin;
pub mod admin_tool;
pub mod agent;
pub mod android_chip;
pub mod apps;
pub mod artifact_tool;
pub mod audio;
pub mod calendar;
pub mod calls;
pub mod chip;
pub mod compact;
pub mod config;
pub mod connector;
pub mod direct;
pub mod edge;
pub mod explain;
pub mod faces;
pub mod fcm;
pub mod files;
pub mod fleet;
pub mod forks;
pub mod goaltool;
mod google_voice_guard;
pub mod heard;
pub mod heartbeat;
pub mod hub;
pub mod ids;
pub mod janitors;
pub mod joblog;
pub mod keeper;
pub mod labels;
pub mod launcher;
pub mod library;
pub mod line;
pub mod listening;
pub mod logins;
pub mod media;
pub mod model_fallbacks;
pub mod models;
pub mod once;
pub mod oracle;
pub mod owner;
pub mod pairing;
pub mod past;
pub mod peers;
pub mod personas;
pub mod places;
pub mod plans;
pub mod pool;
pub mod process;
pub mod push;
pub mod pusher;
pub mod quota;
pub mod quota_keeper;
pub mod relay;
pub mod render_spec;
pub mod request_tool;
pub mod revisions;
pub mod roster;
pub mod router;
pub mod schedules;
pub mod screens;
pub mod seats;
pub mod telling;
pub mod terminal;
pub mod tools;
pub mod unkept;
pub mod usage;
mod usage_provider;
pub mod voice;
pub mod voices;
pub mod wire;

/// The version this Host reports.
pub const VERSION: &str = env!("CARGO_PKG_VERSION");

/// The environment variable that tells an agent's own tools where their Host listens.
pub const TOOL_ADDR_ENV: &str = "CLARP_HOST_ADDR";

/// The environment variable that carries the token an agent's own tools present: see
/// [`tool_token`].
pub const TOOL_TOKEN_ENV: &str = "CLARP_TOOL_TOKEN";

/// The environment variables by which a skill's own script that speaks to its Host by itself
/// finds one, as the Python Host's skills read them: where the Host is, and the token to
/// present. A turn of this Host is told this Host under them, with the token of its agent's
/// tools ([`tool_token`]): what such a script may ask is what that token opens.
pub const SCRIPT_URL_ENV: &str = "CLARP_SERVER_URL";
pub const SCRIPT_TOKEN_ENV: &str = "CLARP_AUTH_TOKEN";

/// What in the Host's environment is the Host's alone. Nothing the Host starts is given these:
/// a turn's environment is read by every command an agent runs, and what an agent prints is in
/// its conversation.
pub const OWN_SECRETS: &[&str] = &[
    "CLARP_HOST_TOKEN",
    "CLARP_PUSH_CREDENTIAL",
    "CLARP_SOURCE_TOKEN",
    "CLARP_PEER_TOKEN",
];

/// The token an agent's own tools present, on a Host whose administrator has this token.
///
/// It follows from the administrator's token, so it is the same after the Host is started
/// again and a turn that went on meanwhile still reaches its Host; and it does not give the
/// administrator's token away. It opens the agents' tools (`/tools/`) from this machine, and
/// the one thing an agent asks of its user's phone by a script that speaks to the Host
/// itself (an event for the calendar); nothing else: not the conversations, not the
/// devices, not the pairing. A Host without a token gives its turns none.
pub fn tool_token(administrators: &str) -> String {
    if administrators.is_empty() {
        return String::new();
    }
    pairing::digest(&format!(
        "clarp-host: the token of an agent's own tools\0{administrators}"
    ))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_tools_token_follows_from_the_administrators_and_is_not_it() {
        let token = tool_token("the-administrators-token");
        assert_eq!(token.len(), 64);
        assert_eq!(token, tool_token("the-administrators-token"));
        assert_ne!(token, tool_token("another-token"));
        assert!(!token.contains("the-administrators-token"));
        // A Host that asks nobody for a token has none to give its turns.
        assert_eq!(tool_token(""), "");
    }
}

pub mod vocabulary;
