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
pub mod agent;
pub mod artifact_tool;
pub mod config;
pub mod edge;
pub mod fleet;
pub mod goaltool;
pub mod hub;
pub mod ids;
pub mod joblog;
pub mod launcher;
pub mod library;
pub mod pairing;
pub mod plans;
pub mod process;
pub mod push;
pub mod pusher;
pub mod relay;
pub mod revisions;
pub mod tools;
pub mod wire;

/// The version this Host reports.
pub const VERSION: &str = env!("CARGO_PKG_VERSION");

/// The environment variable that tells an agent's own tools where their Host listens. The
/// Host's token is in the Host's own environment, which every turn inherits.
pub const TOOL_ADDR_ENV: &str = "CLARP_HOST_ADDR";
