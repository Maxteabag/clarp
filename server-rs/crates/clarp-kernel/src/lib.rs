//! The kernel of the Clarp Host.
//!
//! One agent, one actor, one log: an agent's state is owned by one task, changed only by events,
//! and every event is appended to that agent's log before anything else happens. This crate holds
//! the state machines and the events. It does no I/O: the actor that drives it and the log it
//! writes to are given to it through `clarp-ports`.

pub mod agent;
pub mod journal;
pub mod turn;
