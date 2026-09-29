//! Qt-free core of the Clarp Rust desktop client: wire types, the SSE parser,
//! the conversation sync and delivery reducers, and transcript text rules.
//! Everything here is pure so `cargo test` needs no display or Qt runtime.

pub mod conversation;
pub mod delivery;
pub mod endpoint;
pub mod json;
pub mod protocol;
pub mod sse;
pub mod sync;
pub mod text;
pub mod time_format;
