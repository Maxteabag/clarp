//! The Clarp client contract as types.
//!
//! `contract/schemas` and `docs/protocol.md` describe what a Host and an app say to each other.
//! This crate is that description as code, so that the Host, the desktop client and the generated
//! Swift and Kotlin types cannot drift from one another.
//!
//! The contract is additive: a reader ignores fields and event types it does not know, and a
//! writer leaves out what it has nothing to say about. Every optional field here is therefore an
//! `Option` that is skipped when empty, every enum of names has an `Other` that keeps a name from a
//! newer peer exactly as it was sent, and nothing refuses unknown fields.

pub mod events;
pub mod log;
pub mod requests;
pub mod responses;
pub mod server_info;
pub mod snapshot;
