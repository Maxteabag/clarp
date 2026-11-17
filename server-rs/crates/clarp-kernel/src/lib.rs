//! The kernel of the Clarp Host.
//!
//! One agent, one actor, one log: an agent's state is owned by one task, changed only by events,
//! and every event is appended to that agent's log before anything else happens. This crate holds
//! the state machines and the events. It does no I/O: the actor that drives it and the log it
//! writes to are given to it through `clarp-ports`.

pub mod activity;
pub mod addressed;
pub mod agent;
pub mod artifacts;
pub mod calls;
pub mod chip;
pub mod connector;
pub mod conversation;
pub mod devices;
pub mod difflib;
pub mod failures;
pub mod fork;
pub mod goals;
pub mod heard;
pub mod heartbeat;
pub mod helpers;
pub mod herald;
pub mod jobs;
pub mod journal;
pub mod kept_portraits;
pub mod languages;
pub mod live;
pub mod located;
pub mod media;
pub mod notify;
pub mod personas;
pub mod portraits;
pub mod presence;
pub mod push;
pub mod python;
pub mod recovery;
pub mod requests;
pub mod rings;
pub mod roster;
pub mod said;
pub mod schedules;
pub mod spoken;
pub mod told;
pub mod tools;
pub mod trust;
pub mod turn;
pub mod voices;
