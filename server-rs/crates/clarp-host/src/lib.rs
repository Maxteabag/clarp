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

pub mod admin;
pub mod agent;
pub mod config;
pub mod edge;
pub mod fleet;
pub mod hub;
pub mod ids;
pub mod launcher;
pub mod pairing;
pub mod wire;

/// The version this Host reports.
pub const VERSION: &str = env!("CARGO_PKG_VERSION");
